#!/usr/bin/env python3
"""calibrate_d3.py — 用前瞻采集的 24h 真实 Swap 日志，标定 D3 换手率阈值。

为什么要重做（2026-10-24）：
  原 D3_FULL_MARK=0.10 是 **n=1 单池**标定（USDT/WBNB 0.0412 倍/天 × 2.4 冗余），
  自证欠功效。评审要求「≥20 池分位」才算可复测。

数据源（免 RPC，已落盘）：
  ai_finance/state/revival/micro_forward/days/<day>/swaps.jsonl，字段：
    b=block, pool=池地址, a0/a1=token0/token1 净变动（带符号，负数=池子流出）
  → 单笔成交量取 |a0| or |a1|（**只取一侧**，两侧相加会重复计账约 2 倍，PIT-094）

口径声明（写进输出，禁止事后改）：
  - 换手率定义 = 24h 成交额(USD) / 当前 TVL(USD)
  - TVL 用链上**当前**储备（D3 是「现在的活跃度」，不用历史 TVL）
  - 成交额**只对能定美元的一侧计价**：
      WBNB 侧 × 现价；稳定币侧按面值；两边都不是（如 AKE/WBNB 的 AKE 侧）
      则退回用 WBNB 侧；连 WBNB 都没有 → 该池标 UNPRICEABLE，剔除出分布（不填 0）
  - 结果分两组：全样本 / TVL≥$50k 组（主流池），避免长尾冷池污染分位

用法：
  python scripts/calibrate_d3.py --min-pools 20 [--days 2026-10-24] [--top 400]
"""
import argparse
import json
import math
import os
import statistics
import sys
import time
import urllib.request

WS = "/root/.openclaw/workspace"
DAYS_DIR = f"{WS}/ai_finance/state/revival/micro_forward/days"
OUT = f"{WS}/open_source/pool-health-scanner/state/d3_calibration"
RPC = "https://bsc.rpc.blxrbdn.com"   # 2026-10-24 实测：binance.org / defibit 均不可达，仅 blxrbdn 可用
UA = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
STABLES = {
    "0x55d398326f99059ff775485246999027b3197955": 18,  # USDT
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": 18,  # USDC
    "0xe9e7cea3dedca5984780bafc599bd69add087d56": 18,  # BUSD
    "0x1af3f329e8be154074d8769d1ffa4ee058b1dbc3": 18,  # DAI
    "0x40af3827f39d0eacbf4a168f8d4ee67c121d11c9": 18,  # TUSD
}
SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_RESERVES = "0x0902f1ac"
SEL_DECIMALS = "0x313ce567"


def norm(a):
    """micro_forward 的 pool 字段是 32 字节 tx hash 形态，eth_call 要 20 字节地址。"""
    a = (a or "").strip()
    if a.startswith("0x") and len(a) == 66:      # 32 字节 → 前 20 字节就是地址
        return "0x" + a[2:42]
    if a.startswith("0x") and len(a) == 42:
        return a.lower()
    return a


def rpc_batch(calls, timeout=60):
    """批量 eth_call，一次 HTTP 拿多条结果（公共 RPC 限速下必须批）。"""
    body = [{"jsonrpc": "2.0", "id": i, "method": "eth_call",
             "params": [{"to": to, "data": data}, "latest"]}
            for i, (to, data) in enumerate(calls)]
    body = [dict(x, params=[{"to": norm(x["params"][0]["to"]),
                             "data": x["params"][0]["data"]}, "latest"]) for x in body]
    req = urllib.request.Request(RPC, data=json.dumps(body).encode(), headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as f:
        return json.loads(f.read())


def addr_of(h):
    return "0x" + h[-40:]


def wbnb_usd():
    """BNB 价：WBNB/USDT 主池储备现算，不硬编码。"""
    pair = "0x16b9a82891338f9ba80e2d6970fdda79d1eb0dae"
    d = rpc_batch([(pair, SEL_RESERVES)])
    r = d[0]["result"][2:]
    r0 = int(r[0:64], 16) / 1e18          # USDT
    r1 = int(r[64:128], 16) / 1e18        # WBNB
    return r0 / r1


def load_swaps(day):
    p = f"{DAYS_DIR}/{day}/swaps.jsonl"
    if not os.path.exists(p):
        sys.exit(f"缺数据：{p}")
    vol, cnt = {}, {}
    with open(p) as f:
        for line in f:
            try:
                o = json.loads(line)
            except Exception:                        # noqa: BLE001
                continue
            pool = o.get("pool")
            if not pool:
                continue
            v = abs(o.get("a0") or 0) + abs(o.get("a1") or 0)
            # 这里只用来「排序挑池」，真正计价在下面按币种分侧算
            vol[pool] = vol.get(pool, 0) + v
            cnt[pool] = cnt.get(pool, 0) + 1
    return vol, cnt


def pool_meta(pools):
    """批量拿 token0/token1/reserves/decimals。"""
    calls = []
    for p in pools:
        calls += [(p, SEL_TOKEN0), (p, SEL_TOKEN1), (p, SEL_RESERVES)]
    res = rpc_batch(calls)
    byid = {}
    for r in res:
        if "result" in r:
            byid[r["id"]] = r["result"]
    meta = {}
    tok_need = set()
    for i, p in enumerate(pools):
        t0 = byid.get(i * 3)
        t1 = byid.get(i * 3 + 1)
        rv = byid.get(i * 3 + 2)
        if not (t0 and t1 and rv) or rv in ("0x", "0x0") or len(rv) < 130:
            continue                     # 非 V2 pair 或未初始化 → 跳过（不填假值）
        a0, a1 = addr_of(t0), addr_of(t1)
        tok_need.update([a0, a1])
        r = rv[2:]
        meta[p] = {"t0": a0, "t1": a1,
                   "r0": int(r[0:64], 16), "r1": int(r[64:128], 16)}
    dec_calls = [(t, SEL_DECIMALS) for t in tok_need]
    dec = {}
    if dec_calls:
        for i, r in enumerate(rpc_batch(dec_calls)):
            t = dec_calls[i][0]
            if "result" in r:
                dec[t] = int(r["result"][2:66], 16)
    for p in meta:
        meta[p]["d0"] = dec.get(meta[p]["t0"], 18)
        meta[p]["d1"] = dec.get(meta[p]["t1"], 18)
    return meta


def usd_per_unit(tok, bnb_px):
    if tok in STABLES:
        return 1.0
    if tok == WBNB:
        return bnb_px
    return None


def analyze(day, top, min_tvl, min_swaps):
    vol_raw, cnt = load_swaps(day)
    ranked = sorted(vol_raw, key=lambda p: vol_raw[p], reverse=True)[:top]
    ranked = [p for p in ranked if cnt.get(p, 0) >= min_swaps]
    print(f"日志池数 {len(vol_raw)}｜取成交额 Top{top} 并过滤 swap≥{min_swaps} → {len(ranked)} 池")
    bnb = wbnb_usd()
    print(f"BNB/USD = {bnb:.2f}")
    meta = pool_meta(ranked)
    print(f"链上元数据取到 {len(meta)}/{len(ranked)}")

    # 重新逐笔计价
    target = set(meta)
    per_pool = {p: 0.0 for p in target}
    with open(f"{DAYS_DIR}/{day}/swaps.jsonl") as f:
        for line in f:
            try:
                o = json.loads(line)
            except Exception:                        # noqa: BLE001
                continue
            p = o.get("pool")
            if p not in target:
                continue
            m = meta[p]
            a0 = (o.get("a0") or 0) / 10 ** m["d0"]
            a1 = (o.get("a1") or 0) / 10 ** m["d1"]
            u0 = abs(a0) * (usd_per_unit(m["t0"], bnb) or 0)
            u1 = abs(a1) * (usd_per_unit(m["t1"], bnb) or 0)
            a0n, a1n = abs(a0) > 0, abs(a1) > 0
            # 只取**一侧**：优先取能定美元的那侧（PIT-094 禁止双侧相加）
            if a0n and usd_per_unit(m["t0"], bnb) is not None:
                per_pool[p] += u0
            elif a1n and usd_per_unit(m["t1"], bnb) is not None:
                per_pool[p] += u1
            elif a1n:
                per_pool[p] += 0.0   # 两侧都不可定价 → 留待 UNPRICEABLE
            else:
                per_pool[p] += u0

    rows = []
    for p, m in meta.items():
        r0 = m["r0"] / 10 ** m["d0"]
        r1 = m["r1"] / 10 ** m["d1"]
        u0 = usd_per_unit(m["t0"], bnb)
        u1 = usd_per_unit(m["t1"], bnb)
        if u0 is not None:
            tvl = r0 * u0 + (r1 * u1 if u1 is not None else r0 * u0)
        elif u1 is not None:
            tvl = r1 * u1 * 2
        else:
            continue                                  # 双不可定价 → 剔除
        if tvl <= 0:
            continue
        rows.append({"pool": p, "tvl_usd": round(tvl, 2),
                     "volume_usd": round(per_pool[p], 2),
                     "swaps": cnt.get(p, 0),
                     "turnover_daily": round(per_pool[p] / tvl, 6)})

    def stats(sel, label):
        xs = sorted(r["turnover_daily"] for r in sel)
        if not xs:
            return None
        def q(f):
            k = (len(xs) - 1) * f
            lo, hi = math.floor(k), math.ceil(k)
            return xs[lo] if lo == hi else xs[lo] + (xs[hi] - xs[lo]) * (k - lo)
        return {"label": label, "n": len(xs),
                "p10": round(q(.10), 4), "p25": round(q(.25), 4),
                "p50": round(q(.50), 4), "p75": round(q(.75), 4),
                "p90": round(q(.90), 4), "p95": round(q(.95), 4),
                "max": round(xs[-1], 4), "mean": round(statistics.fmean(xs), 4)}

    allg = stats(rows, "全样本")
    big = [r for r in rows if r["tvl_usd"] >= min_tvl]
    bigg = stats(big, f"TVL≥${min_tvl}", )
    return {"day": day, "bnb_usd": round(bnb, 4), "rows": rows,
            "stats_all": allg, "stats_big": bigg}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=time.strftime("%Y-%m-%d"))
    ap.add_argument("--top", type=int, default=400)
    ap.add_argument("--min-tvl", type=float, default=50000)
    ap.add_argument("--min-swaps", type=int, default=50)
    ap.add_argument("--min-pools", type=int, default=20)
    a = ap.parse_args()
    r = analyze(a.day, a.top, a.min_tvl, a.min_swaps)
    os.makedirs(OUT, exist_ok=True)
    p = f"{OUT}/d3_calib_{a.day}.json"
    json.dump(r, open(p, "w"), ensure_ascii=False, indent=1)
    print("\n== 分位 ==")
    for k in ("stats_all", "stats_big"):
        s = r[k]
        if s:
            print(f"{s['label']:14s} n={s['n']:3d} "
                  f"p10={s['p10']:<8} p25={s['p25']:<8} p50={s['p50']:<8} "
                  f"p75={s['p75']:<8} p90={s['p90']:<8} p95={s['p95']:<7} max={s['max']}")
    print(f"\n落盘 {p}")
    n = r["stats_big"]["n"] if r["stats_big"] else 0
    if n < a.min_pools:
        print(f"⚠️ 主流池样本 n={n} < {a.min_pools}，仍属 UNDERPOWERED")
        return 2
    print(f"✅ 主流池 n={n} ≥ {a.min_pools}，样本量达标")
    return 0


if __name__ == "__main__":
    sys.exit(main())

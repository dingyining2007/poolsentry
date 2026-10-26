#!/usr/bin/env python3
"""turnover_24h.py — 用归档 RPC 分片拉取 24h Swap 日志，计算真实日换手率。

背景（2026-10-23 实测）：
  之前结论「24h 窗口需付费 RPC」是**错的**。
  `https://bsc.rpc.blxrbdn.com` 免费支持归档日志，单次上限 5000 块；
  24h ≈ 115200 块 → 分 24 片全部成功，16628 条 Swap，429 秒。
  所以换手率数据源问题已解决，不需要付费 RPC。

用法：
  python turnover_24h.py 0x池地址 [--hours 24] [--json]
"""
import argparse
import json
import sys
import time
import urllib.request

ARCHIVE_RPC = "https://bsc.rpc.blxrbdn.com"
CHUNK = 2000              # 实测 5000 在频繁调用下易 -32002 超时；2000 稳定
RETRY = 3
RETRY_BACKOFF = [5, 15, 40]
STEP_S = 0.45             # 实测出块间隔（fetch_swaps 内用 1000 块真实时间戳刷新）
SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_RESERVES = "0x0902f1ac"
SEL_DECIMALS = "0x313ce567"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
STABLES = {
    "0x55d398326f99059ff775485246999027b3197955",   # USDT
    "0xe9e7cea3dedca5984780bafc599bd69add087d56",   # BUSD
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",   # USDC
    "0x40af3827f39d0eacbf4a168f8d4ee67c121d11c9",   # TUSD
    "0x1af3f329e8be154074d8769d1ffa4ee058b1dbc3",   # DAI
}
UA = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}


def rpc(method, params, timeout=60):
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": method, "params": params}).encode()
    req = urllib.request.Request(ARCHIVE_RPC, data=body, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as f:
        d = json.loads(f.read())
    if "error" in d:
        raise RuntimeError(str(d["error"])[:160])
    return d["result"]


def call(addr, selector, pad=None):
    data = selector + (pad or "")
    return rpc("eth_call", [{"to": addr, "data": data}, "latest"])


def addr_of(hexstr):
    return "0x" + hexstr[-40:]


def decimals(tok):
    try:
        return int(call(tok, SEL_DECIMALS)[2:66], 16)
    except Exception:                                   # noqa: BLE001
        return 18


def wbnb_usd():
    """BNB 价：从 WBNB/USDT 主池储备反推（现算，不硬编码）。"""
    pair = "0x16b9a82891338f9ba80e2d6970fdda79d1eb0dae"
    r = call(pair, SEL_RESERVES)[2:]
    r0 = int(r[0:64], 16) / 1e18                        # USDT
    r1 = int(r[64:128], 16) / 1e18                      # WBNB
    return r0 / r1 if r1 else 0.0


def side_usd(tok, amt_h, bnb_px):
    if tok in STABLES:
        return amt_h
    if tok == WBNB:
        return amt_h * bnb_px
    return None


def pool_info(pair):
    t0 = addr_of(call(pair, SEL_TOKEN0))
    t1 = addr_of(call(pair, SEL_TOKEN1))
    r = call(pair, SEL_RESERVES)[2:]
    r0 = int(r[0:64], 16) / 10 ** decimals(t0)
    r1 = int(r[64:128], 16) / 10 ** decimals(t1)
    return t0, t1, r0, r1


def fetch_swaps(pair, hours=24, verbose=False, timeout_s=600):
    head = int(rpc("eth_blockNumber", []), 16)
    # 出块间隔必须**实测**，不能硬编码（PIT-102）。
    # 2026-10-23 实测 BSC 平均 0.4502 秒/块；若按 0.75s 假设，24h 窗口实际只覆盖 14.4h，
    # 换手率会被系统性低估 40%。做法：取 1000 块的真实时间戳算平均间隔。
    global STEP_S
    try:
        b_head = rpc("eth_getBlockByNumber", [hex(head), False])
        b_back = rpc("eth_getBlockByNumber", [hex(head - 1000), False])
        _st = (int(b_head["timestamp"], 16) - int(b_back["timestamp"], 16)) / 1000
        STEP_S = _st if 0.1 <= _st <= 3.0 else 0.45
    except Exception:                                   # noqa: BLE001
        STEP_S = 0.45
    span = int(hours * 3600 / STEP_S)
    t_deadline = time.time() + timeout_s
    logs, ok, fail = [], 0, 0
    partial = False
    err = ""
    t_start = time.time()
    for fb in range(head - span, head, CHUNK):
        tb = min(fb + CHUNK - 1, head)
        if time.time() > t_deadline:                    # 预算耗尽 → 提前收工，返回已有数据 + 标记
            partial = True
            break
        got = None
        for attempt in range(RETRY):
            try:
                got = rpc("eth_getLogs", [{"fromBlock": hex(fb), "toBlock": hex(tb),
                                           "address": pair, "topics": [SWAP_TOPIC]}],
                          timeout=90)
                break
            except Exception as e:                      # noqa: BLE001
                err = str(e)[:60]
                if attempt < RETRY - 1:
                    time.sleep(RETRY_BACKOFF[attempt])
        if got is not None:
            logs.extend(got)
            ok += 1
        else:
            fail += 1
            if verbose:
                print(f"  片 {fb}-{tb} 重试{RETRY}次仍失败：{err}", file=sys.stderr)
        time.sleep(0.35)
    if verbose:
        print(f"  分片 {ok} 成功 / {fail} 失败，日志 {len(logs)} 条，"
              f"耗时 {time.time()-t_start:.0f}s", file=sys.stderr)
    return logs, head - span, head, {"ok": ok, "fail": fail, "partial": partial,
                                     "elapsed_s": round(time.time() - t_start, 1)}


def decode_swap_amounts(log, t0, t1, d0, d1, bnb_px):
    """只按**单侧**计价（PIT：Swap 的 amountIn/amountOut 双侧相加会重复计账约 2 倍）。

    Swap(address,uint256,uint256,uint256,uint256,address) 的 data 是 4 个 uint256。
    判定哪一侧是流入：4 个字段中，前两个为 (amount0In, amount1In)，后两个为
    (amount0Out, amount1Out)。**只能取其一侧**计入成交量。
    """
    data = log["data"][2:]
    if len(data) < 256:
        return 0.0
    a0in = int(data[0:64], 16) / 10 ** d0
    a1in = int(data[64:128], 16) / 10 ** d1
    a0out = int(data[128:192], 16) / 10 ** d0
    a1out = int(data[192:256], 16) / 10 ** d1
    u0 = side_usd(t0, a0in if a0in > 0 else a0out, bnb_px)
    u1 = side_usd(t1, a1in if a1in > 0 else a1out, bnb_px)
    # 逐侧非零判断后，**取已知美元的一侧**，不把两侧相加
    if u0 is not None and a0in + a0out > 0:
        return u0
    if u1 is not None and a1in + a1out > 0:
        return u1
    return 0.0


def turnover(pair, hours=24, verbose=False, timeout_s=900):
    bnb_px = wbnb_usd()
    t0, t1, r0, r1 = pool_info(pair)
    d0 = decimals(t0)
    d1 = decimals(t1)
    usd0 = side_usd(t0, r0, bnb_px)
    usd1 = side_usd(t1, r1, bnb_px)
    tvl = (usd0 or 0) + (usd1 if usd1 is not None else (usd0 or 0))

    logs, from_blk, to_blk, fetch_meta = fetch_swaps(pair, hours, verbose, timeout_s=timeout_s)
    vol = 0.0
    for lg in logs:
        try:
            vol += decode_swap_amounts(lg, t0, t1, d0, d1, bnb_px)
        except Exception:                               # noqa: BLE001
            pass

    # 窗口实际时长（用真实块号算，不用假设）
    try:
        b_from = rpc("eth_getBlockByNumber", [hex(from_blk), False])
        b_to = rpc("eth_getBlockByNumber", [hex(to_blk), False])
        dur_h = (int(b_to["timestamp"], 16) - int(b_from["timestamp"], 16)) / 3600
    except Exception:                                   # noqa: BLE001
        dur_h = hours

    return {
        "pair": pair, "token0": t0, "token1": t1,
        "reserves": {"t0": r0, "t1": r1},
        "tvl_usd": round(tvl, 2),
        "bnb_usd": round(bnb_px, 4),
        "window_hours": round(dur_h, 3),
        "blocks": {"from": from_blk, "to": to_blk},
        "swap_count": len(logs),
        "volume_usd": round(vol, 2),
        "turnover_daily": round(vol / tvl * (24 / dur_h), 6) if tvl > 0 and dur_h > 0 else None,
        "turnover_raw": round(vol / tvl, 6) if tvl > 0 else None,
        "block_interval_s": round(STEP_S, 4),
        "fetch": fetch_meta,
        "complete": (fetch_meta["fail"] == 0 and not fetch_meta["partial"]),
        "source": f"{ARCHIVE_RPC} 归档日志分片 {CHUNK} 块 + {RETRY} 次重试",
        "basis": ("[链上实测]" if (fetch_meta["fail"] == 0 and not fetch_meta["partial"])
                  else "[链上实测·不完整] 有分片失败或被超时截断，换手率偏低，不可用于阈值标定"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pair")
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    r = turnover(a.pair, a.hours, verbose=not a.json)
    if a.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
    else:
        print(f"池 {r['pair']}")
        print(f"  TVL ${r['tvl_usd']:,.2f}   BNB ${r['bnb_usd']}")
        print(f"  窗口 {r['window_hours']}h  Swap {r['swap_count']} 条  成交额 ${r['volume_usd']:,.2f}")
        print(f"  日换手率 {r['turnover_daily']} 倍/天")
    return 0


if __name__ == "__main__":
    sys.exit(main())

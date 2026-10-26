#!/usr/bin/env python3
"""roundtrip_gate.py — 往返损耗硬闸（引擎侧）

为什么单独成一个模块
--------------------
2026-10-23 评审指出的产品风险：
  CLI 能一眼认出蜜罐（往返损耗 99.9%），但引擎 pool_health 对同一个池子输出
  「INSUFFICIENT / 证据不足」。用户看到「数据不足」而不是「有坑」——这是误导，
  比不输出更糟。

所以往返损耗必须做**独立硬闸**，与六维评分并行，不依赖任何维度是否有数据。

设计铁律
--------
1. 纯读链，不依赖 eth_abi（本机系统 Python 没有装）。calldata 用 selector +
   32 字节定长参数手工拼 —— 仅限「全 uint256 / address 定长」的情形，
   这里路径是 [WBNB, token] 两跳，用 encodeAbiParameters 同构实现。
   ⚠️ 记录在案：这是唯一允许手拼的位置，因为动态数组无法用更简单方式绕开依赖；
      一旦环境有 eth_abi，优先用 eth_abi（见 encode_path）。
2. 取不到数 → None + reason，**禁止填 0**（PIT-041）。
3. 池子太小（TVL < $1000 或最小单占池 > 5%）→ 并列归因，不许只报 HONEYPOT_TAX。
4. 判定阈值固定写在代码里，可测可复现，不靠「感觉」。

判定分级
--------
REJECT  往返损耗 ≥ 20%（最小档）或任何档卖不出  → 直接否决，不看评分
WARN    往返损耗 >  2%（最小档）                → 记录，不否决
CLEAR   ≤ 2%（V2 正常往返约 0.5%）
"""
import json
import urllib.request

RPC = "https://bsc.rpc.blxrbdn.com"
UA = {"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"}

PANCAKE_V2_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
STABLES = {
    "0x55d398326f99059ff775485246999027b3197955",   # USDT
    "0xe9e7cea3dedca5984780bafc599bd69add087d56",   # BUSD
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",   # USDC
    "0x40af3827f39d0eacbf4a168f8d4ee67c121d11c9",   # TUSD
    "0x1af3f329e8be154074d8769d1ffa4ee058b1dbc3",   # DAI
}

SEL_GET_AMOUNTS_OUT = "0xd06ca61f"   # getAmountsOut(uint256,address[])
SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_RESERVES = "0x0902f1ac"
SEL_DECIMALS = "0x313ce567"
SEL_SYMBOL = "0x95d89b41"

# 硬闸阈值（可测可复现）
REJECT_LOSS_PCT = 20.0      # 最小档往返损耗 ≥ 此值 → REJECT
WARN_LOSS_PCT = 2.0         # V2 正常往返约 0.5%
V2_NORMAL_ROUNDTRIP_PCT = 0.5
TVL_TINY_USD = 1000.0
SMALLEST_TRADE_SHARE_MAX = 0.05   # 最小测试单占池 > 5% → 高损耗是必然的


# ────────────────────────── 基础 RPC ──────────────────────────
def _rpc(method, params, timeout=30):
    body = json.dumps({"jsonrpc": "2.0", "id": 1,
                       "method": method, "params": params}).encode()
    req = urllib.request.Request(RPC, data=body, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as f:
        d = json.loads(f.read())
    if "error" in d:
        raise RuntimeError(str(d["error"])[:160])
    return d["result"]


def _call(to, data, timeout=30):
    return _rpc("eth_call", [{"to": to, "data": data}, "latest"], timeout)


def _addr(word):
    return "0x" + word[-40:].lower()


def _dec(tok):
    try:
        return int(_call(tok, SEL_DECIMALS)[2:66], 16)
    except Exception:                                  # noqa: BLE001
        return 18


def _sym(tok):
    try:
        raw = _call(tok, SEL_SYMBOL)[2:]
        ln = int(raw[64:128], 16)
        s = bytes.fromhex(raw[128:128 + ln * 2]).decode("utf-8", "ignore")
        return "".join(ch for ch in s if ch.isprintable()).strip() or tok[:8]
    except Exception:                                  # noqa: BLE001
        return tok[:8]


def encode_path(amount_in, path):
    """getAmountsOut(uint256,address[]) 的 calldata。

    有 eth_abi 就用 eth_abi（首选）；没有则按 ABI 规范逐段拼：
      head: uint256 amountIn | offset(=0x40) | len | 地址×n
    """
    try:
        import eth_abi                                  # noqa: F811
        return SEL_GET_AMOUNTS_OUT + eth_abi.encode(
            ["uint256", "address[]"], [amount_in, path]).hex()
    except ImportError:
        pass
    head = f"{amount_in:064x}" + f"{0x40:064x}"
    tail = f"{len(path):064x}" + "".join(
        "0" * 24 + a[2:].lower() for a in path)
    return SEL_GET_AMOUNTS_OUT + head + tail


def get_amounts_out(amount_in, path):
    r = _call(PANCAKE_V2_ROUTER, encode_path(amount_in, path))[2:]
    if len(r) < 128:
        raise RuntimeError("getAmountsOut 返回空（路径不存在或池子无流动性）")
    ln = int(r[64:128], 16)
    if ln < 2:
        raise RuntimeError("getAmountsOut 返回长度异常")
    return [int(r[128 + i * 64: 192 + i * 64], 16) for i in range(ln)]


# ────────────────────────── 池信息 ──────────────────────────
def pool_info(pair):
    t0 = _addr(_call(pair, SEL_TOKEN0))
    t1 = _addr(_call(pair, SEL_TOKEN1))
    r = _call(pair, SEL_RESERVES)[2:]
    r0 = int(r[0:64], 16)
    r1 = int(r[64:128], 16)
    d0, d1 = _dec(t0), _dec(t1)
    return {"token0": t0, "token1": t1,
            "reserves_h": (r0 / 10 ** d0, r1 / 10 ** d1),
            "decimals": (d0, d1), "symbols": (_sym(t0), _sym(t1))}


def bnb_usd():
    """1 BNB 的美元价：链上读 WBNB/USDT 主池（不凭记忆写价，PIT-037）"""
    pair = "0x16b9a82891338f9ba80e2d6970fdda79d1eb0dae"
    info = pool_info(pair)
    r0, r1 = info["reserves_h"]
    t0, t1 = info["token0"], info["token1"]
    if t0 in STABLES:
        return r0 / r1 if r1 else None
    if t1 in STABLES:
        return r1 / r0 if r0 else None
    return None


# ────────────────────────── 主闸 ──────────────────────────
def run(pair, capital=100.0, max_size_usd=20000.0):
    """对池子做往返损耗硬闸。

    返回：
      {"verdict": "REJECT"|"WARN"|"CLEAR"|"UNKNOWN",
       "blocking": bool,
       "round_trips": [...], "detail": str, "notes": [...],
       "basis": "[链上实测]"|"[链上实测·不完整]"}
    """
    out = {"pair": pair, "verdict": "UNKNOWN", "blocking": False,
           "round_trips": [], "detail": "", "notes": [],
           "basis": "[链上实测]", "thresholds": {
               "reject_loss_pct": REJECT_LOSS_PCT,
               "warn_loss_pct": WARN_LOSS_PCT,
               "v2_normal_roundtrip_pct": V2_NORMAL_ROUNDTRIP_PCT}}

    try:
        info = pool_info(pair)
    except Exception as e:                             # noqa: BLE001
        out["detail"] = f"池信息读不出来（{str(e)[:80]}），硬闸判不了"
        out["basis"] = "[链上实测·不完整]"
        return out

    t0, t1 = info["token0"], info["token1"]
    h0, h1 = info["reserves_h"]
    s0, s1 = info["symbols"]
    wu = bnb_usd()

    # 定 USD 与「计价币」
    def _usd(tok, amt):
        if tok in STABLES:
            return amt
        if tok == WBNB and wu:
            return amt * wu
        return None

    u0, u1 = _usd(t0, h0), _usd(t1, h1)
    if u0 is not None and u1 is not None:
        tvl = u0 + u1
    elif u0 is not None:
        tvl, out["notes"] = u0 * 2, out["notes"] + ["另一侧无 USD 锚，TVL 按对称假设估算"]
    elif u1 is not None:
        tvl, out["notes"] = u1 * 2, out["notes"] + ["另一侧无 USD 锚，TVL 按对称假设估算"]
    else:
        out["detail"] = f"池内既无稳定币也无 WBNB（{s0}/{s1}），无法折算美元"
        return out

    out.update({"pair_name": f"{s0}/{s1}", "tvl_usd": round(tvl, 2),
                "bnb_usd": round(wu, 4) if wu else None})

    # 选交易路径：WBNB ↔ 另一侧
    if t0 == WBNB:
        other, other_dec, other_sym = t1, info["decimals"][1], s1
    elif t1 == WBNB:
        other, other_dec, other_sym = t0, info["decimals"][0], s0
    else:
        # 非 WBNB 对：若有稳定币侧，走 稳定币↔另一侧
        if t0 in STABLES:
            other, other_dec, other_sym = t1, info["decimals"][1], s1
            base, base_sym = t0, s0
        elif t1 in STABLES:
            other, other_dec, other_sym = t0, info["decimals"][0], s0
            base, base_sym = t1, s1
        else:
            out["detail"] = f"{s0}/{s1} 既非 WBNB 对也无稳定币，无标准路径可测"
            return out

    if t0 == WBNB or t1 == WBNB:
        base, base_dec, base_sym = WBNB, 18, "WBNB"
        if not wu:
            out["detail"] = "BNB 价取不到，往返损耗无法折算美元"
            return out
    else:
        base_dec = _dec(base)

    # 金额分档：按 TVL 占比，夹到 $1~max，另补 $10（普通人视角）
    pcts = [0.00001, 0.0001, 0.001]
    sizes = []
    for p in pcts:
        u = tvl * p if tvl > 0 else capital * p / 0.0001
        sizes.append(round(max(min(u, max_size_usd), 1.0), 2))
    if 10.0 not in sizes:
        sizes = sorted(set(sizes + [10.0]))

    usd_per_base = wu if base == WBNB else (tvl / 2 / (h0 if base == t0 else h1))
    if not usd_per_base or usd_per_base <= 0:
        out["detail"] = "计价币美元价算不出来"
        return out

    ok = 0
    for usd in sizes:
        amt_base = int(usd / usd_per_base * 10 ** base_dec)
        if amt_base <= 0:
            continue
        rec = {"in_usd": usd, "pct_of_tvl": round(usd / tvl * 100, 6) if tvl else None}
        try:
            back_mid = get_amounts_out(amt_base, [base, other])[-1]
            if back_mid <= 0:
                raise RuntimeError("买入报价为 0")
            back = get_amounts_out(back_mid, [other, base])[-1]
            if back <= 0:
                raise RuntimeError("卖出报价为 0")
            loss = (1 - back / amt_base) * 100
            rec["loss_pct"] = round(loss, 4)
            rec["out_usd"] = round(back / 10 ** base_dec * usd_per_base, 4)
            ok += 1
        except Exception as e:                         # noqa: BLE001
            rec["loss_pct"] = None
            rec["error"] = str(e)[:120]
        out["round_trips"].append(rec)

    rts = [x for x in out["round_trips"] if x.get("loss_pct") is not None]
    fails = [x for x in out["round_trips"] if x.get("loss_pct") is None]
    if not rts:
        out["detail"] = "所有档位都报不出价，无法判断（池子可能已无流动性）"
        out["basis"] = "[链上实测·不完整]"
        return out

    best = min(x["loss_pct"] for x in rts)
    worst = max(x["loss_pct"] for x in rts)
    smallest = min(x["in_usd"] for x in rts)
    impact = smallest / tvl if tvl else None
    out["smallest_trade_pct_of_tvl"] = round(impact * 100, 4) if impact else None

    too_small = (tvl < TVL_TINY_USD) or (impact is not None and impact > SMALLEST_TRADE_SHARE_MAX)
    if too_small:
        out["notes"].append(
            f"池子 TVL 仅 ${tvl:,.2f}，最小测试单 ${smallest:,.2f} 已占池子 "
            f"{(impact or 0)*100:.2f}%——此规模下高损耗是必然的，不能据此判定代币抽税")

    if fails:
        out["verdict"] = "REJECT"
        out["blocking"] = True
        out["detail"] = f"{len(fails)}/{len(out['round_trips'])} 档卖不出（路由 revert）：本金可能取不回"
        return out

    if too_small:
        out["verdict"] = "WARN" if best <= WARN_LOSS_PCT else "REJECT"
        out["blocking"] = out["verdict"] == "REJECT"
        out["verdict_tag"] = "ILLIQUID_OR_TAX"
        out["detail"] = (f"往返损耗 {best:.2f}%~{worst:.2f}%，但池子只有 ${tvl:,.2f}。"
                         f"两种可能无法区分：① 深度不足（几乎确定）；② 代币抽税（未排除）")
        return out

    if best >= REJECT_LOSS_PCT:
        out["verdict"] = "REJECT"
        out["blocking"] = True
        out["verdict_tag"] = "HIGH_COST_OR_TAX"
        out["detail"] = (f"最小单往返损耗已达 {best:.2f}%（V2 正常约 "
                         f"{V2_NORMAL_ROUNDTRIP_PCT}%，是其 {best/V2_NORMAL_ROUNDTRIP_PCT:.0f} 倍）")
    elif best > WARN_LOSS_PCT:
        out["verdict"] = "WARN"
        out["verdict_tag"] = "HIGH_FEE_OR_TAX"
        out["detail"] = (f"最小单往返 {best:.2f}% / 最大单 {worst:.2f}%，"
                         f"高于 V2 正常的 {V2_NORMAL_ROUNDTRIP_PCT}%")
    else:
        out["verdict"] = "CLEAR"
        out["verdict_tag"] = "NORMAL"
        out["detail"] = f"往返损耗 {best:.2f}%~{worst:.2f}%（V2 正常约 {V2_NORMAL_ROUNDTRIP_PCT}%）"
    return out


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        r = run(p)
        print(json.dumps({k: v for k, v in r.items() if k != "round_trips"},
                         ensure_ascii=False, indent=1))
        for rt in r["round_trips"]:
            print(f"  ${rt['in_usd']:>9,.2f}  占池 {rt.get('pct_of_tvl')}%  "
                  f"loss={rt.get('loss_pct')}")

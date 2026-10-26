#!/usr/bin/env python3
"""快速复算「PancakeSwap V2 新建对的往返损耗分布」。

只算一个数：$10 买进再卖出（本池单跳 x*y=k 闭式解）损耗百分比。
不做完整体检、不算 TVL/持币/合约维度，所以能扫几百个池。

口径（与 src/cli.py 完全一致，参考 ai_finance/src/onchain/depth_formula.py）：
  买进：dy = r_out - r_in*r_out/(r_in + dx_in)      dx_in 含 0.25% 手续费
  卖出：同上反向
  损耗 = (dx0 - dx_back) / dx0

输出：>20% 占比、>99.99% 占比、零储备占比 —— 直接检验 README 的 68% 说法。
"""
import sys, os, json, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import pool_probe as pp

N     = int(os.environ.get("N", "60"))
LO    = int(os.environ.get("LO", "0"))
USD   = float(os.environ.get("USD", "10"))
FACTORY = pp.FACTORY_PCS_V2

SEL_ALL_PAIRS_LENGTH = "0x574f2ba3"
SEL_ALL_PAIRS        = "0x1e3dd18b"
SEL_GET_RESERVES     = "0x0902f1ac"
SEL_TOKEN0           = "0x0dfe1681"
SEL_TOKEN1           = "0xd21220a7"
SEL_DECIMALS         = "0x313ce567"
SEL_SYMBOL           = "0x95d89b41"

FEE = 0.0025          # PancakeSwap V2: 0.25%


def call(to, data, timeout=8):
    return pp.rpc("eth_call", [{"to": to, "data": data}, "latest"], timeout=timeout)


def h2i(h):
    h = h[2:] if h.startswith("0x") else h
    return int(h, 16) if h else 0


def sym(addr):
    try:
        raw = call(addr, SEL_SYMBOL)
        b = bytes.fromhex(raw[2:])
        if len(b) >= 64:                       # string
            n = int.from_bytes(b[32:64], "big")
            return b[64:64 + n].decode(errors="ignore") or "?"
        return b.rstrip(b"\x00").decode(errors="ignore") or "?"
    except Exception:
        return "?"


def dec(addr):
    try:
        return h2i(call(addr, SEL_DECIMALS))
    except Exception:
        return None


def roundtrip_loss_usd(r_in, r_out, d_in, d_out, usd):
    """本池单跳：USD -> token -> USD。返回损耗百分比，卖不出返回 None。"""
    if r_in <= 0 or r_out <= 0 or d_in is None or d_out is None:
        return None
    dx0 = usd * (10 ** d_in)
    if dx0 <= 0:
        return None
    # 买：token_in(=quote) -> token_out(=base)
    dx_in = dx0 * (1 - FEE)
    out1 = r_out - (r_in * r_out) / (r_in + dx_in)
    if out1 <= 0:
        return None
    # 卖：反向
    dx_in2 = out1 * (1 - FEE)
    back = r_in - (r_out * r_in) / (r_out + dx_in2)
    if back <= 0:
        return None
    return (dx0 - back) / dx0 * 100.0


def main():
    total = h2i(call(FACTORY, SEL_ALL_PAIRS_LENGTH))
    start = LO if LO else max(0, total - N)
    end = min(total, start + N)
    print(f"allPairsLength={total}   扫 index {start}..{end-1}  共 {end-start} 个"
          f"   单笔 ${USD:.0f}\n", flush=True)

    rows, t0 = [], time.time()
    for idx in range(start, end):
        d = SEL_ALL_PAIRS + hex(idx)[2:].rjust(64, "0")
        rec = {"idx": idx}
        try:
            pair = "0x" + call(FACTORY, d)[-40:]
            rec["pair"] = pair
            w = call(pair, SEL_GET_RESERVES)[2:]
            r0, r1 = h2i("0x" + w[0:64]), h2i("0x" + w[64:128])
            rec["r0"], rec["r1"] = r0, r1
            if r0 == 0 or r1 == 0:
                rec["status"] = "ZERO_RESERVE"
                rows.append(rec)
                print(f"  [{idx}] {pair[:12]}… 零储备（还没人加流动性）", flush=True)
                continue

            t0a = "0x" + call(pair, SEL_TOKEN0)[-40:]
            t1a = "0x" + call(pair, SEL_TOKEN1)[-40:]
            base, quote = t0a, t1a
            rb, rq = r0, r1
            if t0a.lower() > t1a.lower():          # 常规约定：base 在前
                base, quote, rb, rq = t1a, t0a, r1, r0
            db, dq = dec(base), dec(quote)
            loss = roundtrip_loss_usd(rq, rb, dq, db, USD)
            rec.update({"base": base, "quote": quote,
                        "sym_base": sym(base), "sym_quote": sym(quote),
                        "loss_pct": loss})
            rec["status"] = "OK" if loss is not None else "NO_SELL"
            rows.append(rec)
            ls = f"{loss:9.2f}%" if loss is not None else "    卖不出"
            print(f"  [{idx}] {rec['sym_base']}/{rec['sym_quote']:<10} "
                  f"${USD:.0f} 往返 = {ls}", flush=True)
        except Exception as e:
            rec["status"] = "ERROR"
            rec["err"] = str(e)[:100]
            rows.append(rec)
            print(f"  [{idx}] ERROR {str(e)[:60]}", flush=True)

    ok      = [r for r in rows if r.get("status") == "OK"]
    nosell  = [r for r in rows if r.get("status") == "NO_SELL"]
    zero    = [r for r in rows if r.get("status") == "ZERO_RESERVE"]
    err     = [r for r in rows if r.get("status") == "ERROR"]
    over20  = [r for r in ok if r["loss_pct"] > 20]
    over9999= [r for r in ok if r["loss_pct"] > 99.99]
    n = len(rows)

    print("\n" + "=" * 62)
    print(f"样本 {n} 个新建对（耗时 {time.time()-t0:.1f}s）")
    print(f"  零储备/无流动性   {len(zero):3d}  {len(zero)/n*100:5.1f}%")
    print(f"  有储备可测算     {len(ok):3d}  {len(ok)/n*100:5.1f}%")
    print(f"  有储备但卖不出   {len(nosell):3d}  {len(nosell)/n*100:5.1f}%")
    print(f"  扫描失败         {len(err):3d}")
    if ok:
        print(f"\n  在可测算的 {len(ok)} 个池里：")
        print(f"    ${USD:.0f} 往返损耗 >20%  ：{len(over20):3d}  {len(over20)/len(ok)*100:5.1f}%")
        print(f"    ${USD:.0f} 往返损耗 >99.99%：{len(over9999):3d}  {len(over9999)/len(ok)*100:5.1f}%")
    out = os.path.join(os.path.dirname(__file__), "..", "samples",
                       f"newpairs_{n}_{USD:.0f}usd_{time.strftime('%Y%m%d_%H%M')}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump({"factory": FACTORY, "all_pairs_length": total,
               "index_range": [start, end - 1], "usd": USD,
               "fee": FEE, "rows": rows,
               "stats": {"n": n, "zero": len(zero), "ok": len(ok),
                         "no_sell": len(nosell), "err": len(err),
                         "over20": len(over20), "over99_99": len(over9999)}},
              open(out, "w"), indent=1)
    print(f"\n落盘: {out}")


if __name__ == "__main__":
    main()

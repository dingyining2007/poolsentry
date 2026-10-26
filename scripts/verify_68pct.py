#!/usr/bin/env python3
"""复算 README 里的「150 个新建对 / 68% 往返损耗 >20%」是否成立。

做法：直接查 PancakeSwap V2 Factory 的 allPairsLength，取最后 N 个 PairCreated 出来的池，
对每个池用本工具同一套 x*y=k 单跳闭式解算 $10/$100 往返损耗，统计 >20% 的占比。
不改任何既有代码，只读链上 + 复用 src/cli 的测算函数。
"""
import sys, os, json, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pool_probe as pp

N = int(os.environ.get("N", "40"))
FACTORY = pp.FACTORY_PCS_V2

SEL_ALL_PAIRS_LENGTH = "0x574f2ba3"   # allPairsLength()
SEL_ALL_PAIRS = "0x1e3dd18b"          # allPairs(uint256)


def call(to, data):
    return pp.rpc("eth_call", [{"to": to, "data": data}, "latest"])


def main():
    raw = call(FACTORY, SEL_ALL_PAIRS_LENGTH)
    total = int(raw, 16)
    print(f"PancakeSwap V2 allPairsLength = {total}")
    start = max(0, total - N)
    print(f"取最后 {total - start} 个新建对（index {start}..{total-1}）\n")

    import cli
    rows = []
    for idx in range(start, total):
        d = SEL_ALL_PAIRS + hex(idx)[2:].rjust(64, "0")
        try:
            pair = "0x" + call(FACTORY, d)[-40:]
        except Exception as e:
            print(f"  [{idx}] 取地址失败 {e}")
            continue
        try:
            res = cli.analyse_subject(pair, capital=100.0, opts={"turnover_cached": None})
        except Exception as e:
            rows.append({"idx": idx, "pair": pair, "err": str(e)[:80]})
            print(f"  [{idx}] {pair} 分析失败: {str(e)[:60]}")
            continue

        rts = res.get("round_trips") or []
        small = next((r for r in rts if r.get("in_usd") and r["in_usd"] <= 10.5), None)
        loss = small.get("loss_pct") if small else None
        if loss is None and rts:
            loss = rts[0].get("loss_pct")
        name = res.get("pair_name") or "?"
        tvl = res.get("tvl_usd") or 0
        rows.append({"idx": idx, "pair": pair, "name": name,
                     "tvl": tvl, "loss10": loss})
        ls = f"{loss:8.2f}%" if loss is not None else "  卖不出"
        print(f"  [{idx}] {name:22s} TVL=${tvl:>12,.0f}  $10往返={ls}")

    ok = [r for r in rows if r.get("loss10") is not None]
    bad = [r for r in rows if r.get("loss10") is None]
    over20 = [r for r in ok if r["loss10"] > 20]
    over9999 = [r for r in ok if r["loss10"] > 99.99]
    print("\n" + "=" * 60)
    print(f"样本 {len(rows)} 个池（成功测算 {len(ok)}，失败/卖不出 {len(bad)}）")
    if rows:
        print(f"往返损耗 >20%  ：{len(over20)}/{len(rows)} = {len(over20)/len(rows)*100:.1f}%")
        print(f"往返损耗 >99.99%：{len(over9999)}/{len(rows)}")
    out = os.path.join(os.path.dirname(__file__), "..", "samples",
                       f"newpairs_verify_{N}_{time.strftime('%Y%m%d_%H%M')}.json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    json.dump({"factory": FACTORY, "all_pairs_length": total, "n": N,
               "rows": rows, "over20": len(over20), "over9999": len(over9999)},
              open(out, "w"), indent=1)
    print(f"\n落盘: {out}")


if __name__ == "__main__":
    main()

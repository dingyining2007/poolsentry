#!/usr/bin/env python3
"""tests_selfcheck.py — 独立复算校验（不同机制验真，不能用被验对象自证）

用途：不依赖 pool_health 的 D1 计算路径，直接从链上 reserves 手算 1% 价格冲击深度，
     与引擎输出对比。偏差 >1% 即判实现有误。

运行：python3 tests_selfcheck.py
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from pool_probe import call                    # noqa: E402
from depth_formula import compute_depth, DEPTH_COEF_TVL   # noqa: E402

PAIR = "0x16b9a82891338f9ba80e2d6970fdda79d1eb0dae"   # PancakeSwap V2 WBNB/USDT


def reserves():
    raw = call(PAIR, "0x0902f1ac")[2:]
    r0 = int(raw[0:64], 16) / 1e18      # USDT
    r1 = int(raw[64:128], 16) / 1e18    # WBNB
    return r0, r1


def manual_depth_1pct(quote_reserve, base_reserve):
    """完全独立的手算：卖出 dx base 使价格从 p0 跌 1%

    x*y=k，价格 p = Q/B。卖 dx 后新价格 p' = (Q+dy)/(B-dx)。
    用恒等式推导出 dx = B*(1/sqrt(0.99)-1)，成交额 USD = Q - Q*B/(B+dx)。
    """
    B, Q = base_reserve, quote_reserve
    dx = B * (1 / math.sqrt(0.99) - 1)
    return Q - Q * B / (B + dx)


def main():
    q, b = reserves()
    tvl = 2 * q
    manual = manual_depth_1pct(q, b)
    d = compute_depth(tvl, base_reserve=b, quote_reserve=q)
    engine_exact = d["depth_1pct_exact"]      # 引擎精确解
    coef = d["depth_1pct_linear"]             # 引擎线性系数解
    dev = abs(manual - engine_exact) / manual * 100
    dev2 = abs(manual - coef) / manual * 100
    print(f"reserves   : USDT={q:,.0f}  WBNB={b:,.2f}")
    print(f"TVL(quote) : ${tvl:,.0f}")
    print(f"手算 depth : ${manual:,.2f}")
    print(f"引擎精确解 : ${engine_exact:,.2f}")
    print(f"引擎系数解 : ${coef:,.2f}（residual {d.get('residual_vs_linear_pct')}%）")
    print(f"手算 vs 引擎精确解 偏差: {dev:.4f}%")
    print(f"手算 vs 引擎系数解 偏差: {dev2:.3f}%  ← 系数法是近似，允许 <1%")
    # 前提：x*y=k 里 quote 与 base 的 USD 价值相等（这里 WBNB 与 USDT 池天然满足）
    usd_q = q
    usd_b = b * (q / b)      # base 以池内价折算 = q
    print(f"量纲自检：quote=${usd_q:,.0f}  base折USD=${usd_b:,.0f}  差 {abs(usd_q-usd_b):,.0f}")
    ok = dev < 0.01 and dev2 < 1.0
    print(("✅ PASS" if ok else "❌ FAIL") +
          f"（精确解阈值 <0.01% 实测 {dev:.4f}%；系数解阈值 <1% 实测 {dev2:.3f}%）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

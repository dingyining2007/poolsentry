#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
depth_formula.py — depth_1pct 唯一权威实现（PIT-042b）

建立：2026-10-18
背景：WorkBuddy 主审指出我方 depth 公式口径混乱，判「少除一个 2，低估一半」。
      我方复算后结论不同：**414.50 是对的，831.52 是错的**。
      三方分歧根因 = 对「1%」的定义不同：
        定义A（价格冲击 1%）：需卖出单边储备的 0.5038%  ≈ TVL 的 0.2519%
        定义B（吃进 TVL 的 1%）：成交额 = TVL 的 1%，价格冲击约 2%
      WorkBuddy 用 TVL*1%/2 = TVL*0.5% → 恰好是定义A 的 2 倍。

本模块只实现定义A（业界「price impact 1%」标准），并由 compute_depth 统一出口，
禁止在业务代码里手写公式（PIT-042b 断言会扫）。

精确解推导（x*y=k，卖出 base 使价格从 p0 跌到 0.99*p0）：
    (B+dx)^2 = B^2 / 0.99
    dx = B * (1/sqrt(0.99) - 1) = B * 0.0050378
    USD 成交额 = Q - Q*B/(B+dx)   （我实际收到的 quote 数）
线性系数：0.0050378/2 = 0.0025189 → TVL * 0.25189%
不要用 TVL*0.01/2 = 0.5%（那是定义B，价格实际冲击约 2%）
"""
import math

# 精确系数：TVL → 1% 价格冲击所需成交额
# = (1/sqrt(0.99) - 1) / 2
DEPTH_COEF_TVL = (1 / math.sqrt(0.99) - 1) / 2      # 0.00251891
DEPTH_COEF_BASE = 1 / math.sqrt(0.99) - 1           # 0.00503782（占单边储备）


def compute_depth(tvl_usd, base_reserve=None, quote_reserve=None):
    """depth_1pct 唯一实现。

    参数
      tvl_usd        双边 TVL（USD）
      base_reserve   可选，base 侧储备量（用于精确解）
      quote_reserve  可选，quote 侧储备量（USD）

    返回 dict，含公式版本与两个口径的数，便于交叉核对。
    """
    if not tvl_usd or tvl_usd <= 0:
        return None
    out = {
        "formula": "PIT-042b/v1",
        "definition": "price_impact_1pct",
        "depth_1pct_linear": round(tvl_usd * DEPTH_COEF_TVL, 2),
        # 错误口径留档，禁止使用，仅用于审计对照
        "_forbidden_tvl_1pct_halved": round(tvl_usd * 0.01 / 2, 2),
    }
    if base_reserve and quote_reserve:
        p0 = quote_reserve / base_reserve
        dx = base_reserve * (1 / math.sqrt(0.99) - 1)
        # 我实际收到的 quote = Q - Q*B/(B+dx)
        usd = quote_reserve - quote_reserve * base_reserve / (base_reserve + dx)
        out["depth_1pct_exact"] = round(usd, 2)
        out["residual_vs_linear_pct"] = round((out["depth_1pct_linear"] / usd - 1) * 100, 3)
    return out


def assert_depth_consistent(depth_1pct, tvl_usd, tol=0.10):
    """PIT-042b 口径断言：depth 必须由 tvl 按唯一定义推出。

    跨池、跨时点跑这条断言，任何一个池掉出来就是口径漂了。
    """
    expect = tvl_usd * DEPTH_COEF_TVL
    if expect <= 0:
        raise ValueError("tvl_usd 必须 > 0")
    ratio = depth_1pct / expect
    if abs(ratio - 1) > tol:
        raise ValueError(
            f"depth 口径不自洽: depth={depth_1pct} 期望≈{expect:.2f} "
            f"(=tvl*{DEPTH_COEF_TVL:.8f})，偏差 {(ratio-1)*100:+.1f}% "
            f"超容差 {tol*100:.0f}%（PIT-042b）"
        )
    return True


if __name__ == "__main__":
    # 用 A 池真实数据自检
    Q, B = 83152.0, 25754044.0
    tvl = Q * 2
    r = compute_depth(tvl, B, Q)
    print("A 池:", r)
    assert_depth_consistent(r["depth_1pct_linear"], tvl)
    # 反例：WorkBuddy 的公式必须被判不自洽
    try:
        assert_depth_consistent(tvl * 0.01 / 2, tvl)
        print("❌ 反例未被拦下")
    except ValueError as e:
        print("✅ 反例被拦:", e)

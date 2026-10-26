#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
chain_adapter.py — 链适配层（v1.0，2026-10-25）
================================================
归属：**B（链适配/精度线）**。A 只负责接口契约，不实现链逻辑。

背景（产品完善优先（2026-10-25 决策））：
  产品当前最大结构性缺陷是 —— `src/cli.py` 里 WBNB / USDT / PancakeFactory / Router
  全是**模块级常量写死 BSC 地址**，而本机 10-24 实测数据 **100% 是 Robinhood v4**。
  这意味着「卖得出去的那张卡」和「我们真正在测的链」不是同一条链。

本适配器的职责（只做这一件事）：
  把「链 → 该链的报价引擎参数」抽成数据表，让 cli / evidence_card / api_server
  不再直接引用常量，而是先 `resolve(chain)`。

接口契约（修改前请先阅读本模块全部调用方）：

    from chain_adapter import resolve, CHAINS, UnsupportedChain

    cfg = resolve("bsc") | resolve("robinhood") | resolve(chain_id:int)
    cfg.chain_id      int
    cfg.name          str            # 展示名
    cfg.block_sec     float          # 出块秒数（用于窗口换算，必须实测值）
    cfg.quote_engine  str            # "pancake_v2" | "univ4_router" | None
    cfg.factory       str | None     # v2 工厂
    cfg.router        str | None     # v2 路由
    cfg.wrapped_native str | None    # WBNB / WETH 等
    cfg.stables       set[str]       # 计价稳定币集合
    cfg.pool_manager  str | None     # v4 PoolManager
    cfg.basis         str            # 【实测】/【读码】/【推断】 —— 必须标
    cfg.verified_at   str            # 该地址最近一次链上核验日期

设计原则：
  1. **不可达的链不许伪造可用**：`quote_engine=None` 表示尚未实现报价，
     `resolve()` 仍返回配置，但调用方必须自己判 `supports_quote()` → 返回 UNKNOWN，
     禁止降级成「猜一个地址硬算」。
  2. **每条地址必带 basis**：地址类常量历来是本产品最大错误源
     （10-24 就发生过 PancakeSwap CLPoolManager 被当成 RH PoolManager 的事故）。
  3. **未实现 ≠ 不支持**：新增链只加一行数据 + 一个 quote_engine 实现，不改调用逻辑。

当前状态：
  bsc        v2 报价已实测可用（10-23/10-24 多次实跑）
  robinhood  v4 数据已实测（Pool.State 读全 223 池），**报价引擎未实现** → 只出「活跃度/死盘」，
             不出「往返损耗」。这是诚实的产品边界，必须写进卡片。
"""

from dataclasses import dataclass, field
from typing import Optional, Set


class UnsupportedChain(Exception):
    """链不在登记表里。调用方必须把异常转成 UNKNOWN，不许当 NORMAL。"""


@dataclass(frozen=True)
class ChainCfg:
    chain_id: int
    name: str
    block_sec: float
    quote_engine: Optional[str]
    factory: Optional[str] = None
    router: Optional[str] = None
    wrapped_native: Optional[str] = None
    stables: Set[str] = field(default_factory=set)
    pool_manager: Optional[str] = None
    basis: str = "【推断】"
    verified_at: str = ""

    def supports_quote(self) -> bool:
        return self.quote_engine is not None


# ─────────────────────────────────────────────────────────────
# 登记表：新链只加一行
# ─────────────────────────────────────────────────────────────
CHAINS = {
    "bsc": ChainCfg(
        chain_id=56,
        name="BSC",
        block_sec=0.45,          # 【实测】2026-10-23（PIT-102：按 0.75s 会低估 40%）
        quote_engine="pancake_v2",
        factory="0xca143ce32fe78f1f7019d7d551a6402fc5350c73",   # PancakeSwap V2 Factory
        router="0x10ed43c718714eb63d5aa57b78b54704e256024e",    # PancakeSwap V2 Router02
        wrapped_native="0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
        stables={
            "0x55d398326f99059ff775485246999027b3197955",       # USDT(BSC)
            "0xe9e7cea3dedca5984780bafc599bd69add087d56",       # BUSD
        },
        basis="【实测】",
        verified_at="2026-10-23（报价 4 档实跑，$10→0.50% / $20,000→0.67%）",
    ),
    "robinhood": ChainCfg(
        chain_id=4663,
        name="Robinhood Chain",
        block_sec=0.1041,        # 【实测】activity_gate 默认值，10-24 实跑
        quote_engine=None,       # ⬜ 未实现：只有 v4，没有 v2 工厂/路由
        pool_manager="0x8366a39cc670b4001a1121b8f6a443a643e40951",
        wrapped_native="0x0bd7d308f8e1639fab988df18a8011f41eacad73",  # T27 A 实测 2026-10-25
        stables={
            "0x5fc5360d0400a0fd4f2af552add042d716f1d168",       # USDG（样本 24 池出现率 54.2%）
        },
        basis="【实测】",
        verified_at="2026-10-25（wrapped_native 实测 symbol()=WETH/decimals=18；Pool.State 读全 223 池 10-24；USDG 近 40 块 29 条日志）",
    ),
}

# T27 结案（A 【实测】2026-10-25 08:05，RH 链 4663）：
#   WETH 完整地址 = 0x0bd7d308f8e1639fab988df18a8011f41eacad73
#   验证法：近 60 块全链 Transfer 日志聚类出 56 个 ERC20 → 对该地址 eth_call symbol() = "WETH"、decimals() = 18
#   ⚠️ 旧值 0x0bd7d308e8c0e2be9e1b0b7a0a9e5c7d9f0a4b3c 是**拼错的**（eth_getCode = 2 bytes 空地址），
#      若沿用会导致 RH 侧所有 USD 折算失败且不报错 —— 这是「8 位 hex 前缀相同即当同一地址」的再现。
#   方法论（新增铁律候选）：**地址补全后必须 eth_getCode 复核非空 + symbol() 对得上**，禁止按前缀拼接。

# 2026-10-25 T27 结案：wrapped_native 已实测补齐（见上），此处不再置 None。
# 保留本注释是为了说明——若日后 再次 需要临时禁用 RH 侧 USD 折算，
# 请由 B 在 resolve() 层加 gate，**不要**再把配置改成 None（那会静默改变全部下游行为）。

_BY_ID = {c.chain_id: k for k, c in CHAINS.items()}


def resolve(chain) -> ChainCfg:
    """按链名或 chain_id 取配置。未知链抛 UnsupportedChain（不许静默兜底）。"""
    if isinstance(chain, ChainCfg):
        return chain
    if isinstance(chain, int):
        key = _BY_ID.get(chain)
        if key is None:
            raise UnsupportedChain(f"未登记的 chain_id={chain}")
        return CHAINS[key]
    key = str(chain).strip().lower()
    aliases = {"bnb": "bsc", "binance": "bsc", "rh": "robinhood", "4663": "robinhood", "56": "bsc"}
    key = aliases.get(key, key)
    if key not in CHAINS:
        raise UnsupportedChain(f"未登记的链：{chain}（已知：{', '.join(CHAINS)}）")
    return CHAINS[key]


def capability_matrix() -> str:
    """给文档/卡片用的能力矩阵（避免把「没实现」说成「支持」）"""
    rows = ["| 链 | chain_id | 往返损耗报价 | 死盘判据 | 24h 换手率 | basis |",
            "|---|---|---|---|---|---|"]
    for c in CHAINS.values():
        q = "✅" if c.supports_quote() else "⬜ 未实现"
        rows.append(f"| {c.name} | {c.chain_id} | {q} | ✅ | ✅（夜批） | {c.basis} |")
    return "\n".join(rows)


if __name__ == "__main__":
    for k, c in CHAINS.items():
        print(f"{k:12s} id={c.chain_id:<6d} quote={c.quote_engine or '-':12s} "
              f"stables={len(c.stables)} basis={c.basis}")
    print()
    print(capability_matrix())

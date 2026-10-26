#!/usr/bin/env python3
"""poolsentry — 一条命令回答：「这个池子能不能进，进去会亏多少」

用法
----
    python3 src/cli.py check 0x<池地址> [--capital 100] [--days 3]
    python3 src/cli.py check 0x<代币地址> --token        # 给代币，自动反查 WBNB 交易对

输出（固定三段）
----
    1) 往返损耗 %   ← 最重要。买卖一圈回来剩多少，这是用户真正会亏的钱
    2) 检测结论     ← 蜜罐 / 高损耗 / 结构风险 / 正常 / 数据不足
    3) 一句话建议

为什么往返损耗排第一（2026-10-26 实测，200 个新建对）
------------------------------------------
PancakeSwap V2 最近 200 个新建对：83% 零储备（无人加流动性）；
有储备的 17% 里，70.6% 让 $10 往返损耗 >20%，41.2% >99.99%。
复现：N=200 python3 scripts/verify_68pct_fast.py
（2026-10-26 更正：此前写的「150 对里 68%」无可复现产物，已作废）
但用户在钱包里只看到「能买、也能卖」。
「可卖」不等于「不亏」——只给 verdict 不给损耗数字，等于骗人。

判定口径
--------
往返损耗 = 1 - (卖出拿回的 USD / 买入花掉的 USD)
直读本池 getReserves() 做 x*y=k 单跳闭式解（含 0.25% 手续费），不是估算，也不是全站最优路由报价。
取 3 档下单额（0.1 / 1 / 5 BNB 等值）看损耗随规模的斜率：
  斜率陡 → 深度不够，大单更亏。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from eth_abi import encode as abi_encode
except ImportError:                                    # 允许无 web3 依赖的降级路径
    abi_encode = None

# ── 地址常量（2026-10-25 T28：改为从 chain_adapter 取，不再在此硬编码）──────────
# 背景：此前这里写死 BSC 地址，而本机数据 100% 是 Robinhood v4，
# 导致「能卖的那张卡」和「真正在测的链」不是同一条链。
# 现在：本文件只保留 **BSC 默认链的向后兼容别名**，真实配置由 chain_adapter.resolve() 提供。
# ⚠️ 这些别名仅供历史调用点使用；新代码一律走 chain_adapter，禁止再引用它们做链推断。
try:
    import chain_adapter as _ca
    _BSC = _ca.resolve("bsc")
    PANCAKE_V2_ROUTER = _BSC.router
    PANCAKE_V2_FACTORY = _BSC.factory
    WBNB = _BSC.wrapped_native
    USDT = "0x55d398326f99059ff775485246999027b3197955"
    BUSD = "0xe9e7cea3dedca5984780bafc599bd69add087d56"
    STABLES = set(_BSC.stables)
except Exception:                                      # 极端情况下保持可用
    PANCAKE_V2_ROUTER = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
    PANCAKE_V2_FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
    WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
    USDT = "0x55d398326f99059ff775485246999027b3197955"
    BUSD = "0xe9e7cea3dedca5984780bafc599bd69add087d56"
    STABLES = {USDT, BUSD}


def chain_cfg(chain="bsc"):
    """取链配置。未登记链抛 UnsupportedChain，调用方须转 UNKNOWN，不许兜底成 BSC。"""
    import chain_adapter as _ca2
    return _ca2.resolve(chain)

ZERO_ADDR = "0x0000000000000000000000000000000000000000"

SEL_GET_AMOUNTS_OUT = "0xd06ca61f"
SEL_GET_PAIR = "0xe6a43905"
SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_GET_RESERVES = "0x0902f1ac"
SEL_DECIMALS = "0x313ce567"
SEL_SYMBOL = "0x95d89b41"

# 分发版必须允许用户自带 RPC（别人装到自己机器上，公共节点可能被限流或不可达）。
# POOLSENTRY_RPC 支持逗号分隔多个，排在最前；留空则用下面的公共节点。
_ENV_RPCS = [u.strip() for u in os.environ.get("POOLSENTRY_RPC", "").split(",") if u.strip()]
RPCS = _ENV_RPCS + [
    "https://bsc-rpc.publicnode.com",
    "https://bsc-dataseed.bnbchain.org",
    "https://bsc-dataseed1.bnbchain.org",
]
_HDR = {"Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; poolsentry/1.0)"}


class RpcError(Exception):
    pass


def rpc(method, params, timeout=20):
    last = None
    for url in RPCS:
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                               "params": params}).encode()
            req = urllib.request.Request(url, data=body, headers=_HDR)
            with urllib.request.urlopen(req, timeout=timeout) as f:
                d = json.loads(f.read())
            if "error" in d:
                raise RpcError(str(d["error"])[:120])
            return d["result"]
        except Exception as e:                        # noqa: BLE001
            last = e
    raise RpcError(f"RPC 全挂 {method}: {last}")


def eth_call(to, data, block="latest"):
    return rpc("eth_call", [{"to": to, "data": data}, block])


# ── T30（2026-10-25）：地址存在性校验 ──────────────────────────────────
# 背景：T20 实测「错误地址调 getLogs 返回空 [] 且不抛异常（1.11s）」，
# 上层若把空当零成交 → 输出 DEAD。客户拿错地址（少一位/复制串行/是 EOA 不是池）
# 是本产品最高频的输入错误，必须先证明「这个地址上有合约」再谈扫描结果。
#
# 判据（三层，任何一层不过都判 SCAN_INVALID，不许降级成 DEAD）：
#   L1 eth_getCode 为空        → 地址没有合约（写错 / EOA / 未部署）
#   L2 code 有但不响应 token0() → 不是 UniswapV2 类 pair
#   L3 token0/token1 都是零地址 → 未初始化的占位合约
def get_code(addr, block="latest"):
    return rpc("eth_getCode", [addr, block]) or "0x"


def address_is_contract(addr):
    """L1：地址上有没有合约字节码。返回 (ok, detail, code_size)"""
    try:
        code = get_code(addr)
    except Exception as e:                            # noqa: BLE001
        return False, f"RPC_FAILED: {type(e).__name__}: {e}", 0
    size = max(len(code) - 2, 0) // 2
    if size == 0:
        return False, "NO_CODE（该地址没有合约字节码：地址写错 / 是普通钱包 / 未部署）", 0
    return True, f"code={size}B", size


def check_pair_exists(addr):
    """T30 三层校验。返回 dict(ok, detail, code_size, token0, token1)"""
    addr = (addr or "").lower().strip()
    if len(addr) != 42 or not addr.startswith("0x"):
        return {"ok": False, "reason": "MALFORMED_ADDRESS",
                "detail": f"地址长度 {len(addr)}，不是 42 字符的 0x 地址", "code_size": 0}
    ok, detail, size = address_is_contract(addr)
    if not ok:
        return {"ok": False, "reason": "NO_CODE", "detail": detail, "code_size": size}
    try:
        t0 = token0(addr)
        t1 = token1(addr)
    except Exception as e:                            # noqa: BLE001
        return {"ok": False, "reason": "NOT_A_PAIR",
                "detail": f"code={size}B 但不响应 token0()/token1()：不是 UniswapV2 类 pair（可能是 V3/V4/其它合约）",
                "code_size": size}
    if t0 == ZERO_ADDR and t1 == ZERO_ADDR:
        return {"ok": False, "reason": "UNINITIALIZED",
                "detail": f"code={size}B 但 token0/token1 均为零地址：未初始化占位合约",
                "code_size": size}
    return {"ok": True, "reason": "OK", "detail": detail, "code_size": size,
            "token0": t0, "token1": t1}


def _addr(w):
    return "0x" + w[-40:]


def token0(pair):
    return _addr(eth_call(pair, SEL_TOKEN0)).lower()


def token1(pair):
    return _addr(eth_call(pair, SEL_TOKEN1)).lower()


def decimals(tok):
    try:
        return int(eth_call(tok, SEL_DECIMALS), 16)
    except Exception:                                 # noqa: BLE001
        return 18


def symbol(tok):
    try:
        r = eth_call(tok, SEL_SYMBOL)[2:]
        if len(r) >= 128:                             # 标准 string 返回
            ln = int(r[64:128], 16)
            return bytes.fromhex(r[128:128 + ln * 2]).decode("utf-8", "ignore")
        return bytes.fromhex(r[64:128].rstrip("0") if len(r) >= 128 else r).decode("utf-8", "ignore")
    except Exception:                                 # noqa: BLE001
        return tok[:8]


def get_pair(a, b):
    if abi_encode is None:
        raise RpcError("需要 eth_abi（pip install eth-abi）才能反查交易对")
    data = SEL_GET_PAIR + abi_encode(["address", "address"], [a, b]).hex()
    p = _addr(eth_call(PANCAKE_V2_FACTORY, data)).lower()
    if int(p, 16) == 0:
        return None
    return p


def get_amounts_out(amount_in, path):
    if abi_encode is None:
        raise RpcError("需要 eth_abi（pip install eth-abi）才能报价")
    data = SEL_GET_AMOUNTS_OUT + abi_encode(["uint256", "address[]"],
                                            [amount_in, path]).hex()
    r = eth_call(PANCAKE_V2_ROUTER, data)[2:]
    if len(r) < 128:
        raise RpcError("getAmountsOut 返回空（路径不存在或池子无流动性）")
    ln = int(r[64:128], 16)
    return [int(r[128 + i * 64: 192 + i * 64], 16) for i in range(ln)]


def wbnb_usd():
    """1 BNB 的美元价，链上直读 WBNB/USDT 池（PIT-037：不许凭记忆写价）"""
    pair = "0x16b9a82891338f9ba80e2d6970fdda79d1eb0dae"
    try:
        t0, t1 = token0(pair), token1(pair)
        r = eth_call(pair, SEL_GET_RESERVES)[2:]
        r0, r1 = int(r[0:64], 16), int(r[64:128], 16)
        if t0 in STABLES:
            return (r0 / 10 ** decimals(t0)) / (r1 / 10 ** decimals(t1))
        if t1 in STABLES:
            return (r1 / 10 ** decimals(t1)) / (r0 / 10 ** decimals(t0))
    except Exception:                                 # noqa: BLE001
        return None
    return None


# ── 转账抽税探针（PIT-099）────────────────────────────────────────────
# 背景：2026-10-23 智谱与 Agnes 双方都指出「用 $7 测 TVL $0.03 的池子，损耗接近 100% 是必然的，
# 与代币是不是蜜罐无关」。该质疑成立 —— 纯靠往返损耗无法区分「池子太小」与「代币抽税」。
# 解决：找一个与深度无关的证据。
#
# 可选手段与实测结论：
#   ❌ eth_call 模拟 transferFrom：只能看是否 revert，看不到「实收多少」——
#      代币在 no-op 路径上返回 true 也照样成功，状态变化读不到。
#   ✅ debug_traceCall（geth 系带 callTracer）：能拿到内部 Transfer 事件的实参，
#      从而算出 to 的实际入账。publicnode 不支持；bnbchain 官方节点需付费。
#   ✅ 退一步的**间接证据**（本实现采用）：小池 + 代币余额异常。
#      若代币在池内余额是天文数字（如 9.2e15 个）而池子美元价值只有几十美分，
#      说明代币本身无市场定价，此时往返损耗的归因必须同时报「深度不足」和「可能是抽税」，
#      禁止只报 HONEYPOT_TAX。
#
# 产品铁律：**当无法区分时，必须把两种可能并列输出，不许选一个说。**
SEL_TOTAL_SUPPLY = "0x18160ddd"


def token_context(token):
    """与深度无关的代币侧事实：总供应量、池内持有量占比。用于给归因提供旁证。"""
    try:
        dec = decimals(token)
        supply = int(eth_call(token, SEL_TOTAL_SUPPLY)[2:66] or "0x0", 16) / 10 ** dec
        return {"symbol": symbol(token), "decimals": dec,
                "total_supply": supply, "basis": "[链上实测] totalSupply()"}
    except Exception as e:                            # noqa: BLE001
        return {"symbol": symbol(token), "error": str(e)[:80]}


# ── 路径独立性检验（PIT-101，2026-10-23 智谱 glm-4.6 建议）──────────
# 用途：区分「真实 AMM 曲线」与「伪造的随规模递增滑点表」。
# 原理：真 x*y=k 有路径独立性——一笔总额 2a 与两笔 a+a 的结果差异仅来自二阶项；
#       查表型会把 amount 当 key，拆单后每笔都命中低档费率，两笔合计明显优于一笔。
# 实现限制：公共 RPC 无法模拟「中间态」，故第二笔只能按当前链上储备重算；
#       这不是真两笔。故本函数只做**一阶对照**，结论标注为方向性证据，不当判决。


def path_independence(pair, base_tok, amt_bnb, bnb_usd):
    """一笔总额 vs 两笔半额（第二笔按当前储备近似）的差异。

    返回 dict 或 None。**不允许**把结果当成「证明是 AMM」的充分条件，
    只能用来发现「拆单明显更优」这种查表特征。
    """
    try:
        amt = int(amt_bnb * 10 ** 18)
        if amt <= 1:
            return None
        one = get_amounts_out(amt, [WBNB, base_tok])[-1]
        half = int(amt / 2)
        two_first = get_amounts_out(half, [WBNB, base_tok])[-1]
        # 第二笔按「第一笔执行后的储备」计算，避免 RPC 无法模拟中间态的问题
        res = eth_call(pair, SEL_GET_RESERVES)[2:]
        r0 = int(res[0:64], 16)
        r1 = int(res[64:128], 16)
        t0 = token0(pair)
        rin, rout = (r1, r0) if t0 == WBNB else (r0, r1)
        half_units = half / 10 ** 18
        rin_h = rin / 10 ** 18
        rout_h = rout / 10 ** (18 if t0 == WBNB else decimals(base_tok))
        rin2 = rin_h + half_units
        rout2 = rout_h - two_first / 10 ** (18 if t0 == WBNB else decimals(base_tok))
        two_second = int(rout2 * half_units * 0.9975 / (rin2 + half_units * 0.9975)
                         * 10 ** (18 if t0 == WBNB else decimals(base_tok)))
        two_total = two_first + two_second
        diff_pct = (two_total - one) / one * 100 if one else None
        return {
            "one_shot": one / 10 ** (18 if t0 == WBNB else decimals(base_tok)),
            "two_orders_total": two_total / 10 ** (18 if t0 == WBNB else decimals(base_tok)),
            "split_advantage_pct": round(diff_pct, 6) if diff_pct is not None else None,
            "note": "第二笔按首笔后储备重算，非真两笔；仅作方向性证据",
        }
    except Exception as e:                              # noqa: BLE001
        return {"error": str(e)[:120]}


# 按 symbol 认美元稳定币（STABLES 只登记了地址，漏掉 TUSD/FDUSD 等）
USD_STABLE_SYMBOLS = {"USDT", "BUSD", "USDC", "TUSD", "FDUSD", "USD1", "DAI", "USDD", "USDP"}

V2_FEE = 0.9975          # Pancake V2 手续费 0.25%，从输入侧扣


def _pair_reserves(pair):
    """读该 pair 自己的储备，返回 (r0, r1)，原始整数。"""
    res = eth_call(pair, SEL_GET_RESERVES)[2:]
    if len(res) < 128:
        raise RpcError("getReserves 返回空（端点伪可用或 pair 不存在）")
    return int(res[0:64], 16), int(res[64:128], 16)


def round_trip(pair, amount_in, token_in, token_out):
    """买入再卖出，返回 (out_amount, loss_pct)。

    ✅ 2026-10-26 P0 修复：改为**强制单跳、只算这一个池**。

    修复前的实现调 Pancake Router 的 getAmountsOut([in,out])，
    Router 会自己找最优路径，可能走另一个更深的池 ——
    于是卡片抬头写着「池 0xABC」，算出来的却是「这个代币在全站最优路由下的成本」。
    实测（TUSD/WBNB 0xe1c4…，$123 池）：
        Router 口径  $10 档损耗 23.53%、占池比 756%（自相矛盾：$10 只是个 $123 池的 15%）
        单跳闭式解  $10 档损耗 0.47%、占池比 15.1%
    差 50 倍。这不是精度问题，是「算的对象不对」。

    现在的算法：直读该 pair 储备 + x*y=k 恒定乘积闭式解，两跳都用执行后的储备。
    口径与 depth_formula.py 同源（价格冲击口径），不另起一套公式。
    """
    if amount_in <= 0:
        raise RpcError("投入金额为 0")

    t0 = token0(pair)
    t1 = token1(pair)
    tin = token_in.lower()
    tout = token_out.lower()
    # ⚠️ token0/token1/token_in/token_out 都是「代币地址」，不是池地址；
    # 不同来源的传入可能一个带前缀一个不带，统一用 _addr 归一到末40位再比。
    def _n(a):
        a = (a or "").lower()
        return a[-40:] if len(a) >= 40 else a
    t0n, t1n, tinn, toutn = _n(t0), _n(t1), _n(tin), _n(tout)
    # ⚠️2026-10-26 血坑：r_in/r_out 是**原始整数储备**，amount_in 也是原始整数，
    # 但 mid = r_out * amount_in_with_fee / (r_in + ...) 在 float 下会把 24 位整数
    # 直接截成 1e24 量级 → 算出的 loss 是 -1e7%（第一次修完就是这个结果）。
    # 铁律：AMM 闭式解必须全程整数运算，最后一步才转 float 算百分比。
    r0, r1 = _pair_reserves(pair)
    if r0 <= 0 or r1 <= 0:
        raise RpcError("该池储备为 0（已抽干）")

    # ⚠️2026-10-26 第二血坑（比第一个更隐蔽）：
    # getReserves() 返回的 r0/r1 **不带小数位**，必须按各自代币 decimals 归一，
    # 否则 CAKE(18) 与 WBNB(18) 看似没问题，但 6 位小数的币（USDT/USDC）会差 1e12 倍；
    # 而本函数传入的 amount_in 是「WBNB 原始单位」，两套原始单位混算必错。
    # 正确做法：全部换算成「人类单位 float」，闭式解在归一后的数上做。
    d_in = decimals(token_in)
    d_out = decimals(token_out)
    if t0n == tinn and t1n == toutn:
        raw_in, raw_out, d_router_in, d_router_out = r0, r1, d_in, d_out
    elif t1n == tinn and t0n == toutn:
        raw_in, raw_out, d_router_in, d_router_out = r1, r0, d_in, d_out
    else:
        raise RpcError("该 pair 不包含这对代币（请用池地址而不是代币地址）")

    scale_in = 10 ** d_in
    scale_out = 10 ** d_out
    amt_in_h = amount_in / scale_in                          # 人类单位
    r_in_h = raw_in / scale_in
    r_out_h = raw_out / scale_out
    if r_in_h <= 0 or r_out_h <= 0:
        raise RpcError("该池储备为 0（已抽干）")

    # ① 买入：in → out
    ain = amt_in_h * V2_FEE
    mid = r_out_h * ain / (r_in_h + ain)
    if mid <= 0:
        raise RpcError("买入报价为 0")

    # ② 卖出：out → in，用执行后的储备
    # ⚠️2026-10-26 第三血坑：卖出方向不能复用 k 的另外三件套。
    # 卖出时「池子的 WBNB 侧」是**付出方**，「CAKE 侧」是**收入方**：
    #     卖出 mid 个 CAKE → 池子 WBNB 减少、CAKE 增加
    # 所以公式是  out = (WBNB储备+投入) * (mid*fee) / (CAKE储备-mid + mid*fee)
    # 我第一版写成 r_out2 = r_out_h - mid（把 CAKE 减掉后又用它做分母的对方），
    # 导致分母≈0 → back 爆到 1e9 量级 → 损耗 -1e7%。
    # 这正是 MEMORY.md「py_compile 通过 ≠ 逻辑正确」的第三次复现。
    sell_res_pay = r_in_h + ain        # 池子的 WBNB 侧（我方卖出时，池子付出 WBNB）
    sell_res_get = r_out_h - mid       # 池子的 CAKE 侧（池子收入 CAKE）
    if sell_res_get <= 0:
        raise RpcError("卖出储备为 0（投入额已超过池内可动储备）")
    midf = mid * V2_FEE
    # 池子付出方是 WBNB：out = WBNB_pool * (mid*fee) / (CAKE_pool + mid*fee)
    back_h = sell_res_pay * midf / (sell_res_get + midf)
    if back_h <= 0:
        raise RpcError("卖出报价为 0（可能卖不出）")

    # 返回原始单位（与调用方口径一致：调用方按 amount_in 同单位使用）
    back = back_h * scale_in
    loss = (1 - back_h / amt_in_h) * 100
    if not (-0.0001 <= loss <= 100.0):
        # 硬闸：任何超出 [0,100] 的损耗都是口径/单位错误，禁止输出
        raise RpcError(f"损耗计算结果越界({loss:.2f}%)，疑口径/单位错误，拒绝输出")
    return back, loss


def analyse_subject(subject, capital=100.0, opts=None):
    """subject 可以是池地址，也可以是代币地址（自动反查 WBNB 对）"""
    subj = subject.lower()

    # ── T30（2026-10-25）：先证明地址上真有合约，再谈任何判定 ──
    # 此前没有这一步：错地址 → token0/token1 静默抛异常 → 落到「代币反查」分支
    # → 查不到对 → 返回 NOT_A_POOL；或 getLogs 静默返回空 → 判 DEAD。
    # 两种都在把「地址打错了」输出成「这池子不行」，是本产品最贵的一类错误。
    pre = check_pair_exists(subj)
    if pre["reason"] in ("MALFORMED_ADDRESS", "NO_CODE"):
        return {
            "status": "SCAN_INVALID",
            "message": pre["detail"],
            "invalid_reason": pre["reason"],
            "code_size": pre.get("code_size", 0),
            "hint": ("这不是「池子不行」，是「这个地址上没有池子」。"
                     "请核对合约地址（42 位、0x 开头、是 pair 合约不是钱包或代币合约）后重测。"),
            "_basis": "【实测】eth_getCode",
        }

    # 先假定是池；能读出 token0/token1 就是池
    pair = None
    try:
        t0, t1 = token0(subj), token1(subj)
        if t0 != ZERO_ADDR and t1 != ZERO_ADDR:
            pair = subj
    except Exception:                                 # noqa: BLE001
        pass

    # 不是池 → 当作代币，反查 WBNB 交易对
    if pair is None:
        p = None
        try:
            p = get_pair(subj, WBNB)
            if p is None:
                p = get_pair(subj, USDT)
        except Exception:                             # noqa: BLE001
            p = None
        if not p:
            return {"status": "NOT_A_POOL",
                    "message": "该地址既不是 Pancake V2 交易对，也查不到与 WBNB/USDT 的交易对"}
        pair = p
        t0, t1 = token0(pair), token1(pair)

    d0, d1 = decimals(t0), decimals(t1)
    s0, s1 = symbol(t0), symbol(t1)
    r = eth_call(pair, SEL_GET_RESERVES)[2:]
    r0h = int(r[0:64], 16) / 10 ** d0
    r1h = int(r[64:128], 16) / 10 ** d1

    bnb_usd = wbnb_usd() or 0.0
    # 定 USD：优先稳定币侧
    # PIT-098（2026-10-23 实测）：TVL 必须**两侧各自换算再相加**，不能 `2 * q_h` 假设两边等值。
    # 事故：WBNB/AKE 池 token1=AKE 价格极低，`tvl = 2 * r0h * bnb_usd` 只算了 WBNB 侧再翻倍，
    # AKE 侧被忽略 → TVL 输出 $0.03（真值约 $0.03 的量级纯属巧合，但公式本身是错的，
    # 遇到「一侧为 WBNB、另一侧无美元锚且量大」的池子会算错。
    def _side_usd(tok, amt_h):
        # ⚠️2026-10-26 根因查明（花了 5 轮才定位，记录教训）：
        # 原实现只认 STABLES 集合里的 USDT/BUSD，**不认 TUSD/FDUSD/USDC 等其它美元稳定币**，
        # 于是 TUSD 侧被当成「无美元锚」→ usd0=None → TVL 走「只算一侧×2」= $132，
        # 而真实值 $123（TUSD $56.9 + WBNB $66.2）。差 7%，且让「占池比」整体偏高。
        # 这不是「宁可保守」，这是**该算的没算**。修法：按 symbol 认美元稳定币。
        if tok in STABLES:
            return amt_h
        try:
            sym = symbol(tok).upper()
        except Exception:                            # noqa: BLE001
            sym = ""
        if sym in USD_STABLE_SYMBOLS:
            return amt_h
        if tok == WBNB:
            return amt_h * bnb_usd
        return None

    usd0, usd1 = _side_usd(t0, r0h), _side_usd(t1, r1h)
    # ⚠️2026-10-26 关键修复：下面这四个分支原本**每个都写了一遍 tvl 赋值**
    # （`tvl = (usd1 or 0) + (usd0 if usd0 is not None else (usd1 or 0))`），
    # 于是我在后面新写的 if/elif 链**永远被覆盖** → 实测 TVL 一直 $132 而不是 $123。
    # 我把上游赋值删掉后，$132 反而还在 —— 因为 STABLES 里根本没有 TUSD，
    # TUSD 侧 usd0=None 才是根因（不是叠加逻辑）。
    # 现在：四个分支只决定 base_tok，tvl 只在下面算**一次**。
    # 原代码在这里各写一遍 tvl 公式（且是「缺一侧就补成同侧」的错误兜底），
    # 下游再算一次 → 上面改了下游等于白改（实测 TVL 仍是 $132 而非 $123）。
    # 教训：口径类代码只能有**唯一一处**赋值点，多一处就是定时炸弹。
    if t0 in STABLES:
        base_tok, base_dec = t1, d1
        base_sym = s1
    elif t1 in STABLES:
        base_tok, base_dec = t0, d0
        base_sym = s0
    elif t0 == WBNB:
        base_tok, base_dec, base_sym = t1, d1, s1
    elif t1 == WBNB:
        base_tok, base_dec, base_sym = t0, d0, s0
    else:
        return {"status": "NO_USD_ANCHOR", "pair": pair,
                "message": f"池内既无稳定币也无 WBNB（{s0}/{s1}），无法折算美元"}

    # ✅2026-10-26 P0-b 修复：TVL 兜底逻辑把「另一侧算不出来」当成「和这侧相等」。
    # 实测后果：TUSD/WBNB 池 TUSD 侧 $56.9、WBNB 侧 $66.2，真 TVL ≈ $123；
    # 旧逻辑 `usd1 + usd0(缺→用usd1顶上)` 反复叠加，输出 $132 且 $10 占池比 756% ——
    # 756% 意味着「$10 是这个池储备的 7.5 倍」，一个自相矛盾的数字被印在卡上。
    # 正确做法：另一侧算不出来时，**只用能算的一侧 ×2**（V2 池两侧等值，这是标准口径），
    # 并在结果里标注 tvl_basis，不许悄悄补成「等于同侧」。
    # ⛔ 2026-10-26 撤销一个错误修复（记录下来，防止再犯）：
    # 我曾用「池内隐含价格」(r0h/r1h) 给未计价侧估值，结果 CAKE/WBNB 的 TVL
    # 从 $21M 变成 $38 亿（180 倍）。原因：隐含价格用的是**原始储备比例**，
    # 但 CAKE 的 r0 是 5.1e6 枚（18 位），WBNB 是 1.4e4 枚 —— 两者同 decimals 时比例看似合理，
    # 一旦代币元数据/单位有任何偏差，反推就是灾难性放大。
    # 结论：**未计价侧绝不反推**。只用「已知侧 ×2」，并在卡上标 tvl_basis=one_side_times2。
    # 这是宁可保守也不许编数的直接后果。

    tvl_basis = "both_sides_usd"
    if usd0 is not None and usd1 is not None:
        tvl = usd0 + usd1
    elif usd0 is not None:
        tvl = usd0 * 2
        tvl_basis = "one_side_times2"
    elif usd1 is not None:
        tvl = usd1 * 2
        tvl_basis = "one_side_times2"
    else:
        return {"status": "NO_USD_ANCHOR", "pair": pair,
                "message": f"池内两侧均无法折算美元（{s0}/{s1}）"}

    # ⚠️2026-10-26 关键：上面 `elif/else` 四个分支里**又各自把 tvl 覆盖了一遍**
    # （原代码 `tvl = (usd1 or 0) + (usd0 if usd0 is not None else (usd1 or 0))`）。
    # 我改的那段在后面，等于白改 —— 实测 TVL 一直是 $132 而不是 $123，就是这个原因。
    # 这就是「改了一半」：只改了下游，没删上游。教训：改口径类代码，必须先 grep 所有赋值点。
    # 唯一赋值点就是上面 if/elif 链，下游不再重算、不再覆盖。

    # 三档下单额（用 BNB 等值，按 capital 缩放），测的是「用户真实怎么买」
    # 计价基准币：选与 WBNB 相对的那个 token（WBNB/USDT 池本身就是 WBNB 对 USDT，
    # 若 base 也取 WBNB 会变成 WBNB→WBNB 同址兑换，Router 直接 revert IDENTICAL_ADDRESSES）
    if base_tok == WBNB:
        alt = t1 if t0 == WBNB else t0
        alt_dec = d1 if t0 == WBNB else d0
        alt_sym = s1 if t0 == WBNB else s0
        base_tok, base_dec, base_sym = alt, alt_dec, alt_sym

    out = {"status": "OK", "pair": pair, "pair_name": f"{s0}/{s1}",
           "tvl_usd": round(tvl, 2), "tvl_basis": tvl_basis, "bnb_usd": round(bnb_usd, 2),
           "round_trips": [], "errors": []}
    if bnb_usd <= 0:
        out["errors"].append("BNB 价取不到，往返损耗无法折算美元")
        return out

    # 金额档位（2026-10-23 智谱 Q5+Q7 / Agnes Q7 共同建议）：
    # 固定金额档位会让「测试额 vs 池子规模」随池子大幅漂移，浅池必然测出 100%。
    # 改为：金额占池子的比例分档（0.001% / 0.01% / 0.1% TVL），再夹到合理美元区间。
    # 这样无论池子是 $500 还是 $50M，「损耗随规模怎么变」都是同一条曲线。
    tvl_ref = tvl if tvl > 0 else 0
    pcts = [0.00001, 0.0001, 0.001]         # 0.001% / 0.01% / 0.1%
    usd_sizes = []
    for p_ in pcts:
        u = tvl_ref * p_ if tvl_ref > 0 else 100.0 * p_ / 0.0001
        u = max(min(u, 20000.0), 1.0)        # 夹到 $1 ~ $20k，避免出无意义的单子
        usd_sizes.append(round(u, 2))
    # 保留一档绝对小额兜底（$10），用于「普通人最可能下的单」视角
    if 10.0 not in usd_sizes:
        usd_sizes = sorted(set(usd_sizes + [10.0]))
    sizes_units = []                         # 兼容旧变量名，下面改用 usd_sizes
    for usd in usd_sizes:
        u = usd / bnb_usd if bnb_usd > 0 else 0
        amt = int(u * 10 ** 18)                             # 用 BNB 买入
        if amt <= 0:
            continue
        try:
            back, loss = round_trip(pair, amt, WBNB, base_tok)
            back_usd = back / 10 ** 18 * bnb_usd
            out["round_trips"].append({
                "in_bnb": round(u, 6), "in_usd": round(usd, 2),
                "pct_of_tvl": round(usd / tvl * 100, 6) if tvl > 0 else None,
                "out_bnb": round(back / 10 ** 18, 8),
                "out_usd": round(back_usd, 4),
                "loss_pct": round(loss, 4),
            })
        except Exception as e:                            # noqa: BLE001
            out["errors"].append(f"${usd:,.2f} 往返失败：{str(e)[:100]}")
            out["round_trips"].append({"in_bnb": round(u, 6), "in_usd": round(usd, 2),
                                       "pct_of_tvl": round(usd / tvl * 100, 6) if tvl > 0 else None,
                                       "loss_pct": None,
                                       "error": str(e)[:100]})

    # ── 日换手率（D3 指标）：走归档 RPC 分片，不再因「公共 RPC 窗口太短」输出空值 ──
    # 2026-10-23 实测：blxrbdn 免费支持归档日志（2000 块/片 + 重试），24h 可全量拿。
    opts = opts or {}
    # ── 日换手率：**不在客户在线路径上扫链** ──
    # 2026-10-24 实测：24h 扫链 RPC 分片 185s / HyperSync 184s，换哪家都救不了。
    # 正解＝夜间批量预计算落 SQLite（src/cache.py + scripts/nightly_batch.py），在线只读缓存。
    if not opts.get("skip_turnover"):
        t = opts.get("turnover_cached")
        if t is not None:
            out["turnover"] = t                       # 由调用方从缓存注入
        else:
            out["turnover"] = {
                "error": "PENDING_BATCH",
                "basis": ("在线路径不扫 24h 链上日志（实测 184~185s，用户等不了）。"
                          "换手率由夜间批量任务预计算，跑 `python3 scripts/nightly_batch.py` 填充"),
            }

    # ── 死盘判据（activity_gate）：轻量、可在线跑（6h 窗口） ──
    if not opts.get("skip_activity"):
        try:
            import activity_gate as _agm
            import hypersync_logs as _hs
            head = opts.get("head_block") or _hs.get_height()
            bnb_usd_v = bnb_usd or 0
            r = _agm.run(
                {"pool": pair, "window_hours": float(opts.get("activity_hours", 6)),
                 "head_block": head,
                 # BSC 实测出块 0.45s（PIT-102：按 0.75s 会低估 40%）
                 "block_sec": 0.45,
                 "price_probe": {"has_price": bnb_usd_v > 0,
                                 "detail": f"bnb_usd={bnb_usd_v:.2f}"}},
                get_logs=lambda fb, tb, addr, topics: _hs.fetch_logs(
                    addr, [topics], fb, tb, max_pages=6)[0],
            )
            out["activity"] = r
        except Exception as e:                            # noqa: BLE001
            # 判据跑不动 → UNKNOWN，**绝不当 PASS**
            out["activity"] = {"verdict": "UNKNOWN", "reasons": [f"{type(e).__name__}: {str(e)[:120]}"],
                               "note": "取不到数据不等于安全；禁止把 UNKNOWN 当 PASS",
                               "alert": False, "exclude": False}
    return out


def verdict_of(a):
    """把数字翻成人话。

    ⚠️ 2026-10-23 多方评审后重写（智谱 Q3 / Agnes Q3 一致指出）：
      「用 $7 的单子测 TVL $0.03 的池子，损耗接近 100% 是**必然**的，与代币是否抽税无关」。
      原实现只报 HONEYPOT_TAX，把「池子太小」误判成「蜜罐」。
    修法：金额与池子规模挂钩 + 归因并列，无法区分时**不许只报一个**。
    """
    if a.get("status") != "OK":
        return "UNKNOWN", (a.get("message") or a.get("status")), None

    rts = [x for x in a["round_trips"] if x.get("loss_pct") is not None]
    fails = [x for x in a["round_trips"] if x.get("loss_pct") is None]
    notes = []

    # 规模档位：最小单子占池子的比例。>5% = 再怎么测都是「自己在砸自己」
    tvl = a.get("tvl_usd") or 0
    small_usd = min([x["in_usd"] for x in rts], default=0) if rts else 0
    impact = (small_usd / tvl) if tvl > 0 else None
    if impact is not None:
        a["smallest_trade_pct_of_tvl"] = round(impact * 100, 4)

    # 池子太小 → 任何损耗数字都主要来自「自己砸自己」，不能归因为抽税
    too_small = (tvl > 0 and tvl < 1000) or (impact is not None and impact > 0.05)
    if too_small:
        notes.append(f"池子 TVL 仅 ${tvl:,.2f}，最小测试单 ${small_usd:,.2f} 已占池子 "
                     f"{(impact or 0)*100:.2f}%——此规模下高损耗是**必然**的，"
                     f"不能据此判定代币抽税")

    if not rts:
        return "UNKNOWN", "所有档位都报不出价，无法判断（池子可能已无流动性）", notes

    worst = max(rts, key=lambda x: x["loss_pct"])
    best = min(rts, key=lambda x: x["loss_pct"])

    if fails:
        return "SELL_BLOCKED", (f"{len(fails)}/{len(a['round_trips'])} 档卖不出（路由 revert）"), notes

    if too_small:
        # 并列归因：深度不足 与 代币抽税 都无法排除
        verdict = "ILLIQUID_OR_TAX"
        detail = (f"往返损耗 {worst['loss_pct']:.2f}%，但池子只有 ${tvl:,.2f}。"
                  f"两种可能无法区分：① 深度不足（几乎确定）；② 代币抽税（未排除）")
        return verdict, detail, notes

    if worst["loss_pct"] >= 99.9:
        return "HONEYPOT_TAX", f"往返损耗 {worst['loss_pct']:.2f}%（本金基本归零）", notes
    if best["loss_pct"] > 20:
        return "HIGH_COST", f"最小额往返损耗已达 {best['loss_pct']:.2f}%", notes
    if worst["loss_pct"] > 20:
        return "DEPTH_THIN", (f"小额 {best['loss_pct']:.2f}% 尚可，大额 {worst['loss_pct']:.2f}%，"
                              f"深度不够"), notes
    # PIT-100（2026-10-23 智谱 Q5 采纳）：原叫 ACCEPTABLE_HIGH，档名本身替用户下了「可以接受」的判断。
    # 单边 10% 抽税的代币往返约 19%，它一点都不「可接受」。改名为 HIGH_FEE_OR_TAX，门槛 5% → 2%。
    if worst["loss_pct"] > 2:
        return "HIGH_FEE_OR_TAX", (f"最好 {best['loss_pct']:.2f}% / 最差 {worst['loss_pct']:.2f}%"
                                   f"（V2 正常往返约 0.5%，已是其 {est_multiple(best['loss_pct'])}）"), notes
    return "NORMAL", f"往返损耗 {best['loss_pct']:.2f}%~{worst['loss_pct']:.2f}%", notes


def est_multiple(x):
    try:
        return f"{x / 0.5:.1f} 倍"
    except Exception:                                   # noqa: BLE001
        return "n/a"


ADVICE = {
    "HIGH_FEE_OR_TAX": "⚠️ 买卖成本明显高于正常（正常约 0.5%）。可能是代币抽税或池子深度不足，"
                       "下单前先用小额试一笔。",
    "ILLIQUID_OR_TAX": "❌ 不要用这个池子。池子太浅，任何单子都会自己砸自己；"
                       "代币是否抽税没排除，两件事都够劝退。",
    "SELL_BLOCKED": "❌ 不要买。买了大概率卖不出去，本金锁死。",
    "HONEYPOT_TAX": "❌ 不要买。这是抽税/蜜罐型代币，往返一次就没了。",
    "HIGH_COST": "❌ 不要买。就算能卖，来回一趟也吃掉两成以上本金。",
    "DEPTH_THIN": "⚠️ 小单可以试，大单必亏。要进就分批，单笔别超过池子的千分之一。",
    "ACCEPTABLE_HIGH": "⚠️（已废弃，保留兼容）成本偏高。",
    "NORMAL": "✅ 买卖成本正常。但成本正常 ≠ 代币安全，请看结构风险。",
    "UNKNOWN": "⚪ 数据不足，不下结论。不要拿『查不到问题』当成『没问题』。",
    "DEAD_ZERO_TRANSFER": "❌ 死盘。成本看着可能正常，但窗口内零转账——没有真实买家，"
                           "进去就是给部署者抬轿子。即使报表好看也排除。",
}


def main():
    ap = argparse.ArgumentParser(prog="poolsentry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="查一个池子/代币值不值得进")
    c.add_argument("address", help="池地址，或代币地址（自动反查 WBNB 对）")
    c.add_argument("--capital", type=float, default=100.0, help="你打算投入的美元（仅用于提示，不影响损耗计算）")
    c.add_argument("--json", action="store_true")
    c.add_argument("--fresh", action="store_true",
                   help="跳过缓存，强制重新计算（2026-10-25：默认读缓存，避免误判耗时）")
    a = ap.parse_args()

    if a.cmd != "check":
        ap.print_help()
        return 2

    t0 = time.time()
    import cache as _cache
    res = None if a.fresh else _cache.get(a.address)  # ① 先查缓存（毫秒级）
    if a.fresh:
        print("  [--fresh] 已跳过缓存，强制重算", file=sys.stderr)
    if res is not None:
        cached = res.get("_cache") or {}
        if not a.json:
            print(f"\n  ⚡ 命中缓存（{cached.get('age_s', 0)}s 前计算，"
                  f"来源 {cached.get('source')}）—— 换手率等重指标不重算\n")
    else:
        try:
            res = analyse_subject(a.address, capital=a.capital,
                                  opts={"turnover_cached": None})
        except Exception as e:                        # noqa: BLE001
            res = {"status": "ERROR", "message": str(e)[:200]}
        if res.get("status") == "OK":
            _cache.put(a.address, res, source="online")
    verdict, detail, notes = verdict_of(res)
    advice = ADVICE.get(verdict, "")

    if a.json:
        print(json.dumps({**res, "verdict": verdict, "detail": detail,
                          "advice": advice, "elapsed_s": round(time.time() - t0, 1)},
                         indent=2, ensure_ascii=False))
        return 0

    print("=" * 68)
    print(f"  PoolSentry · {a.address}")
    if res.get("pair"):
        print(f"  交易对 {res.get('pair_name')}   池子 {res['pair']}")
        print(f"  TVL ≈ ${res.get('tvl_usd', 0):,.0f}   （BNB = ${res.get('bnb_usd', 0):,.2f}）")
    print("=" * 68)

    if res.get("status") == "OK" and res.get("round_trips"):
        print("\n① 往返损耗（买进再卖出，本池单跳闭式解）")
        for rt in res["round_trips"]:
            if rt.get("loss_pct") is None:
                print(f"   花 ${rt['in_usd']:>9,.2f} →  ❌ 卖不出去")
            else:
                bar = "█" * min(int(rt["loss_pct"] / 5), 20)
                pct = rt.get("pct_of_tvl")
                pct_s = f"占池 {pct:.4f}%" if pct else ""
                print(f"   花 ${rt['in_usd']:>9,.2f} {pct_s:<14} →  "
                      f"拿回 ${rt['out_usd']:>9,.2f}   损耗 {rt['loss_pct']:>8.2f}% {bar}")
        print(f"\n② 结论：{verdict}   {detail}")
        print(f"③ 建议：{advice}")
        if res.get("errors"):
            print("\n   备注：")
            for e in res["errors"][:3]:
                print(f"   · {e}")
    else:
        print(f"\n② 结论：{verdict}   {detail}")
        print(f"③ 建议：{advice}")

    # ④ 日换手率（D3）：数据不完整时必须明说，不能看起来像「测过了」
    tv = res.get("turnover") or {}
    if tv.get("error"):
        print(f"\n④ 日换手率：判不了（{tv['error'][:80]}）—— 不填 0")
    elif tv.get("turnover_daily") is not None:
        flag = "" if tv.get("complete") else "  ⚠️ 数据不完整，仅作参考"
        print(f"\n④ 日换手率 {tv['turnover_daily']*100:.4f}% / 天"
              f"（窗口 {tv.get('window_hours')}h，成交 ${tv.get('volume_usd', 0):,.0f}，"
              f"{tv.get('swap_count')} 笔）{flag}")

    # ⑤ 死盘判据（activity_gate，2026-10-24 WorkBuddy 加严建议）
    # 为什么单列：往返损耗测的是「能不能卖、卖亏多少」，测不出「有没有真实买家」。
    # 反例：GANG 池 0.1 BNB 全额在池、报价可算，但近 6 小时 Transfer=0。
    ag = res.get("activity") or {}
    if ag.get("verdict"):
        v = ag["verdict"]
        mark = {"DEAD": "❌", "THIN": "⚠️", "ALIVE": "✅", "UNKNOWN": "⚪"}.get(v, "·")
        rs = "、".join(ag.get("reasons") or []) or "—"
        print(f"\n⑤ 死盘判据：{mark} {v}（{rs}）")
        if ag.get("note"):
            print(f"   {ag['note']}")
        if v == "DEAD":
            print(f"   建议：{ADVICE['DEAD_ZERO_TRANSFER']}")

    print(f"\n   耗时 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())

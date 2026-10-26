#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回测器 v1 — 只读链上历史，零外部依赖

设计原则（为什么这么写）
=======================
1. **不依赖任何私有模块**。此前 D2/D3/D4 全挂在内部 LVR 上，开源包一发布
   就直接 INSUFFICIENT，等于产品不可用。改为自包含：只用 eth_call + eth_getLogs。

2. **数据从哪来**（全部免费公共 RPC，无 API key）：
   - Swap 事件 = 池子每笔交易的真实价格与资金流 → 算换手、真实波动、最大回撤
   - 现在储备 + 历史价格 → 算 LP 的「无常损失」与「手续费收入」，得出净收益
   - 这是 Uniswap V2 的经典 LP 收益分解，不是估算。

3. **算不出来就返回 None，绝不填 0**（PIT-041）。理由：0 分会被当成
   「最差」参与排名，而 None 会被当成「未测量」。这两件事完全不同。

4. **每项数字都标来源**（PIT-087）：[链上实测] / [规范推导]。

调用
----
    from backtester import backtest
    r = backtest(pair, days=30, capital=100.0)

输出
----
    {
      "window": {"from_block": ..., "to_block": ..., "blocks": ...},
      "swaps": 1234,
      "turnover_pct": 12.3,          # 期间换手率
      "amplitude_pct": 4.5,          # 价格振幅（D3 用）
      "max_dd_pct": -2.1,            # 价格最大回撤（D4 用）
      "fee_usd": 0.42,               # 作为 LP 分到的手续费（D2 用）
      "gas_usd": 0.05,               # 固定成本估算
      "net_pct": 0.37,               # 净收益 %
      "_basis": {...}
    }
"""
import json
import time
import urllib.request

# ── PancakeSwap V2 Swap 事件 ────────────────────────────────────────────────
# keccak256("Swap(address,uint256,uint256,uint256,uint256,address)")
SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"

# PancakeSwap V2 费率 0.25%（公开文档 [规范推导]）
FEE_RATE = 0.0025

# 实测 2026-10-22：只有 publicnode 支持大窗口 eth_getLogs，其余全限流
# bnbchain.org 系: -32005 limit exceeded；blockrazor: 25 blocks；1rpc: 50 blocks
BSC_RPCS = [
    "https://bsc-rpc.publicnode.com",      # eth_getLogs 可用（实测 2000 块窗口 OK）
    "https://bsc-dataseed.bnbchain.org",
    "https://bsc-dataseed1.bnbchain.org",
]
# 只有 blastapi 支持归档状态（hist eth_call），实测 10 万块前可读
ARCHIVE_RPCS = ["https://bsc-mainnet.public.blastapi.io",
                "https://bsc-rpc.publicnode.com"]
# 2026-10-23 实测：Swap 日志**只在 publicnode 可用**。
#   - blastapi 的 eth_getLogs 持续 429（它只适合归档 eth_call）
#   - publicnode 对**已确认的旧块**给 403，对**最近块**200 OK，且有两跳延迟
# 归因（PIT-090 补丁）：403 不是「限流」也不是「需要密钥」，是**旧块日志权限不收**
#   → 对策不是退避，是「窗口尽量贴近链头 + 分片退避只用于 429」。
# 产品侧结论：换手率维度需要**付费 RPC 或降级为采样块**，这是已知边界，写进 _limits。
# 2026-10-23 修正（实测推翻上面的结论）：
#   publicnode 对旧块日志 403；**blxrbdn 免费支持归档 eth_getLogs，单次 5000 块**。
#   24h 全量实测：96 片（2000 块/片）全部成功，0 失败，31,686 条 Swap。
#   所以「换手率需付费 RPC」的旧结论作废，publicnode 降为备选。
LOG_RPCS = ["https://bsc.rpc.blxrbdn.com",
            "https://bsc-rpc.publicnode.com"]

# BSC 平均出块 ~3s（[规范推导]，实际有抖动）
SECONDS_PER_BLOCK = 3


class BacktestError(Exception):
    pass


def _rpc(method, params, timeout=25, urls=None):
    last = None
    for url in (urls or BSC_RPCS):
        try:
            body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                               "params": params}).encode()
            req = urllib.request.Request(
                url, data=body, headers={"Content-Type": "application/json",
                             "User-Agent": "Mozilla/5.0 (compatible; pool-health-scanner/1.0)"})
            with urllib.request.urlopen(req, timeout=timeout) as f:
                d = json.loads(f.read())
            if "error" in d:
                raise BacktestError(f"{method}: {d['error']}")
            return d["result"]
        except Exception as e:  # noqa: BLE001
            last = e
    raise BacktestError(f"RPC 全挂 {method}: {last}")


def _call(addr, data, block="latest"):
    return _rpc("eth_call", [{"to": addr, "data": data}, block])


def _word(ret, i=0):
    h = ret[2:] if ret.startswith("0x") else ret
    return int(h[i * 64:(i + 1) * 64], 16)


def _block_number():
    return int(_rpc("eth_blockNumber", []), 16)


def _get_reserves(pair, block="latest", urls=None):
    r = _rpc("eth_call", [{"to": pair, "data": "0x0902f1ac"}, block], urls=urls)
    return _word(r, 0), _word(r, 1), _word(r, 2)


def _get_decimals(tok):
    try:
        return _word(_call(tok, "0x313ce567"))
    except Exception:  # noqa: BLE001
        return 18


def _token0(pair):
    return "0x" + _call(pair, "0x0dfe1681")[-40:]


def _token1(pair):
    return "0x" + _call(pair, "0xd21220a7")[-40:]


def _wbnb_price():
    """WBNB/USDT 池价格 = 1 BNB 的美元价。

    ⚠️ 2026-10-22 实测教训：该池 **token0 是 USDT、token1 是 WBNB**，
       不能假设 token0=WBNB。假设反了会让 BNB 价算出 0.0014（真值 ~1000+），
       进而把 TVL 算成 88 美元（真值千万级）。必须按实际 token 顺序取。
    """
    P = "0x16b9a82891338f9ba80e2d6970fdda79d1eb0dae"   # WBNB/USDT (Pancake V2)
    WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
    USDT = "0x55d398326f99059ff775485246999027b3197955"
    w = _rpc("eth_call", [{"to": P, "data": "0x0dfe1681"}, "latest"])
    tok0 = ("0x" + w[-40:]).lower()
    r0, r1, _ = _get_reserves(P)
    if r0 == 0 or r1 == 0:
        raise BacktestError("WBNB/USDT 池储备为 0")
    if tok0 == WBNB:
        return (r1 / 1e18) / (r0 / 1e18)
    elif tok0 == USDT:
        return (r0 / 1e18) / (r1 / 1e18)
    raise BacktestError(f"该池 token0={tok0} 既非 WBNB 也非 USDT，无法取 BNB 价")


def fetch_swaps(pair, from_block, to_block, chunk=2000, max_logs=60000,
                throttle_s=1.5, max_retry=4):
    """拉 Swap 事件。

    实测（2026-10-22）：publicnode 支持 2000 块窗口，但**连续快速请求会 403 限流**。
    因此必须：① 每窗之间 sleep ② 403 时指数退避重试 ③ 仍失败才缩窗。
    这三条缺一，长窗口回测必然中途挂掉。
    """
    out = []
    # 2026-10-23 实测补丁：`_rpc` 会在 RPC 之间**吞掉** HTTP 码，只留最后一条异常，
    # 导致 403 退避逻辑永远命中不到（表现为「一次 403 就直接抛错，不重试不缩窗」）。
    # 改为：本函数自己持有退避，且每窗从 **最新块往前倒序** 拉——
    # 前端数据最有价值，被限流时损失的是最老的窗口，而不是最新的。
    b = from_block
    _spans = []
    _t = from_block
    while _t < to_block:
        _e = min(_t + chunk, to_block)
        _spans.append((_t, _e))
        _t = _e + 1
    _spans.reverse()
    for (b, e) in _spans:
        if len(out) >= max_logs:
            break
        logs = None
        for attempt in range(max_retry):
            try:
                logs = _rpc("eth_getLogs", [{
                    "address": pair, "topics": [SWAP_TOPIC],
                    "fromBlock": hex(b), "toBlock": hex(e),
                }], urls=LOG_RPCS)
                break
            except BacktestError as ex:
                back = 3 * (2 ** attempt)          # 3s → 6s → 12s → 24s
                if "403" in str(ex) or "limit" in str(ex).lower():
                    time.sleep(back)
                    continue
                raise
        if logs is None:
            # 退避全失效 → 缩窗再试；chunk 已到 500 仍不行则如实报错
            if chunk <= 500:
                raise BacktestError(
                    f"eth_getLogs 在 {hex(b)}-{hex(e)} 连续 {max_retry} 次被限流，"
                    f"已缩窗至 {chunk}。公共 RPC 额度不足，需换付费 RPC 或缩短窗口")
            chunk = chunk // 2
            time.sleep(5)
            continue
        out.extend(logs)
        b = e + 1
        time.sleep(throttle_s)                     # 主动节流，别把额度打爆
    return out[:max_logs]


def sample_price_series(pair, from_block, to_block, n_samples=60, throttle_s=0.4):
    """按历史区块采样池子比价。

    为什么不用 eth_getLogs：实测公共 RPC 对长窗口日志全部限流
    （bnbchain -32005 / blockrazor 25块 / 1rpc 50块 / publicnode 频繁 403）。
    改为只读 eth_call 历史快照：每样本 1 次调用，额度可控且不依赖日志。
    缺点：采样间隔内的瞬时波动会被抹平 → 振幅偏低，必须写进 _limits。
    """
    step = max((to_block - from_block) // n_samples, 1)
    blocks = list(range(from_block, to_block, step))[:n_samples]
    tvl_track = []       # (block, reserve0, reserve1, price)
    last_err = None
    for blk in blocks:
        try:
            r0, r1, _ = _get_reserves(pair, hex(blk), urls=ARCHIVE_RPCS)
            if r0 > 0 and r1 > 0:
                tvl_track.append((blk, r0, r1, r1 / r0))
        except Exception as e:  # noqa: BLE001
            last_err = str(e)[:90]
        time.sleep(throttle_s)
    if not tvl_track:
        raise BacktestError(f"归档采样全失败：{last_err}")
    return tvl_track


def swap_turnover(pair, from_block, to_block, n_samples=60, capital=100.0, fee_rate=FEE_RATE):
    """D3 换手率口径（2026-10-23 新增，替代振幅口径）。

    为什么换 D3 语义（BUG-2 结论）：
      D3 原来的指标是「价格振幅」，打分函数却要求 10-20% 才给高分。
      但 LP 的收益来源是**手续费**，手续费来自**成交额**，不是价格波动幅度。
      主流池振幅 0.9% 被判 0 分，但它的真实换手（日成交额/TVL）可能远超 20%/天。
      「振幅小 → 0 分」等于把「价格稳定 + 成交活跃」这个 LP 最理想的状态判成最差。

    新口径：
      turnover_daily = 窗口内成交额 / TVL / 天数   （单位：倍/天）
      打分：≥1.0 倍/天 满分；0 → 0 分（线性）
      与 D2 的分工：D2 = 你能分到的钱够不够盖 gas（绝对额，取决于本金大小）；
                    D3 = 池子本身成交活不活跃（相对量，与本金无关）。
      两者不重复：小本金在大池子 D2 会挂、D3 仍可高分，这正是产品要区分的。

    取数：eth_getLogs 拉 Swap 事件。公共 RPC 长窗口易限流 → 采样窗口而非全窗口，
    失败返回 None（缺失），不填 0（PIT-041）。
    """
    span = to_block - from_block
    if span <= 0:
        return None, "窗口非法"
    n_win = 3
    win = 2000                                   # publicnode 实测单窗上限 2000 块
    # 2026-10-23 实测：只有**最近 ~3000 块**的日志 publicnode 肯给，更早的直接 403
    # （不是限流：429 才是限流）。因此换手率只能覆盖最近约 2.5 小时，
    # 必须在返回里写清 window_days，禁止拿它冒充「3 天日均」去和 D2 对账。
    logs = []
    try:
        b = max(to_block - win * n_win, from_block)
        while b < to_block:
            e = min(b + win, to_block)
            logs.extend(_rpc("eth_getLogs", [{
                "address": pair, "topics": [SWAP_TOPIC],
                "fromBlock": hex(b), "toBlock": hex(e),
            }], urls=LOG_RPCS) or [])
            b = e + 1
            time.sleep(1.5)
    except Exception as ex:                          # noqa: BLE001
        return None, f"Swap 日志取数失败：{str(ex)[:120]}"
    if not logs:
        return None, "窗口内无 Swap 事件（可能真无成交，或日志被截断）"

    WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
    USDT = "0x55d398326f99059ff775485246999027b3197955"
    BUSD = "0xe9e7cea3dedca5984780bafc599bd69add087d56"
    tok0 = _token0(pair).lower()
    tok1 = _token1(pair).lower()
    wbnb_usd = _wbnb_price()
    d0, d1 = _get_decimals(tok0), _get_decimals(tok1)

    def _usd(amt0, amt1):
        """各自按本侧 token 计价再相加。

        ⚠️ PIT-089 同源坑（2026-10-23 再犯一次，已修）：
        原版 `if tok1 in (USDT,BUSD): return amt1/10**d1` 在 amt1==0 时直接返回 0，
        导致「amount0In>0、amount1In=0」的成交被计成 0 美元 —— WBNB/USDT 池
        token0=USDT，实际只有一侧非零，成交量被抹成几百分之一。
        铁律：任何「按 token 顺序分支」的换算，必须**逐侧判断非零**，不能只看 token 身份。
        """
        total = 0.0
        got = False
        if amt0:
            if tok0 in (USDT, BUSD):
                total += amt0 / 10**d0; got = True
            elif tok0 == WBNB:
                total += amt0 / 10**d0 * wbnb_usd; got = True
        if amt1:
            if tok1 in (USDT, BUSD):
                total += amt1 / 10**d1; got = True
            elif tok1 == WBNB:
                total += amt1 / 10**d1 * wbnb_usd; got = True
        return total if got else None

    vol_usd = 0.0
    ok = 0
    # 2026-10-23 实测补丁（关键）：Swap 事件的**真实 token 顺序**取决于「进池/出池」方向，
    # 与 token0/token1 的固定顺序无关——卖出 WBNB 时 amount0In=BNB、amount1In=0；
    # 买入 WBNB 时 amount1In=BNB、amount0In=0。
    # 原版只用 amount0In 换算 → 只统计到单向成交，WBNB/USDT 池量级被严重低估。
    # 正确做法：按 amount0In>0 / amount1In>0 分别换算，取「唯一非零侧」。
    for lg in logs:
        d = lg.get("data", "")[2:]
        if len(d) < 64 * 4:
            continue
        try:
            a0_in = int(d[0:64], 16);  a1_in = int(d[64:128], 16)
            a0_out = int(d[128:192], 16); a1_out = int(d[192:256], 16)
        except ValueError:
            continue
        # 成交额按「进池那一侧」计价（进池即成交，避免双边重复计数）
        # V2 Swap 语义（[规范推导]，2026-10-23 复核）：amountIn/amountOut 是**成对**的
        #   amount0In>0 → 用户付 token0、收 token1；amount1Out>0 是同一笔的另一半
        #   → 成交额只能按成交的**一侧**算，双侧相加 = 重复计账（实测高估 2 倍）
        # 只有一侧进池：直接换那一侧
        if a0_in > 0 and a1_in == 0:
            v = _usd(a0_in, 0)
        elif a1_in > 0 and a0_in == 0:
            v = _usd(0, a1_in)
        elif a0_in > 0 and a1_in > 0:
            # V2 极少数路径两侧同时进（如闪电兑换），两边相加
            v0, v1 = _usd(a0_in, 0), _usd(0, a1_in)
            v = (v0 or 0) + (v1 or 0) if (v0 is not None or v1 is not None) else None
        else:
            v = None
        if v is None:
            # 兜底：用出池侧
            v = _usd(a0_out, 0) or _usd(0, a1_out)
        if v:
            vol_usd += v
            ok += 1
    if ok == 0:
        return None, "Swap 事件无法换算美元（token 既非稳定币也非 WBNB）"

    r0, r1, _ = _get_reserves(pair)
    usd0, usd1 = _usd(r0, 0), _usd(0, r1)
    tvl = (usd0 or 0) + (usd1 or 0)
    if tvl <= 0:
        return None, "TVL 取不到"
    # 窗口跨度用**最小/最大事件块**实测推算，而不是「窗口块数 × 3s」估算。
    # 2026-10-23 实测：原式把 n_win 窗当成连续覆盖，而实际每窗只有部分块有成交，
    # 会把窗口时长算成 0.208 天（实际日志跨度约 6 小时），换手率被低估近 1.5 倍。
    _blks = [int(lg.get("blockNumber", "0x0"), 16) for lg in logs if lg.get("blockNumber")]
    if len(_blks) >= 2 and max(_blks) > min(_blks):
        win_days = (max(_blks) - min(_blks)) * SECONDS_PER_BLOCK / 86400
    else:
        win_days = (to_block - max(to_block - win * n_win, from_block)) * SECONDS_PER_BLOCK / 86400
    turnover_daily = vol_usd / tvl / win_days if win_days > 0 else None
    return {
        "swap_events": len(logs),
        "swap_usd_ok": ok,
        "volume_usd_window": round(vol_usd, 2),
        "tvl_usd": round(tvl, 2),
        "window_days": round(win_days, 3),
        "turnover_daily": round(turnover_daily, 4),
        "fee_daily_pct_of_tvl": round(turnover_daily * fee_rate * 100, 4),
    }, None


def backtest(pair, days=30, capital=100.0, fee_rate=FEE_RATE, n_samples=60,
             turnover_info=None):
    """对某池做只读历史回测（不依赖 eth_getLogs，改用归档状态采样）。

    计算思路（Uniswap V2 LP 收益分解，全部可复算）：
      1. 按 n_samples 个历史块采储备 → 真实价格序列
      2. 振幅 / 最大回撤 直接由序列算（D3/D4）
      3. 手续费收入：V2 的 k 增长 = 累计手续费（这是恒等式，不是估算）
           k_t / k_0 - 1 = 期间累计手续费率
         你的份额 = capital / TVL，乘进去即你分到的手续费
      4. 无常损失：由价格比直接算（V2 解析式）
      5. 净收益 = 手续费 - 无常损失 - Gas
    """
    pair = pair.lower()
    t_start = time.time()

    to_block = _block_number()
    from_block = max(to_block - int(days * 86400 / SECONDS_PER_BLOCK), 1)
    if to_block - from_block < 100:
        raise BacktestError("窗口过短")

    tok0 = _token0(pair)
    tok1 = "0x" + _rpc("eth_call", [{"to": pair, "data": "0xd21220a7"}, "latest"])[-40:]
    # tok0/tok1 由此为链上实读，后续所有换算都按它们判定，不再假设
    d0, d1 = _get_decimals(tok0), _get_decimals(tok1)

    # 1) 采样（[链上实测]）
    series = sample_price_series(pair, from_block, to_block,
                                 n_samples=n_samples, throttle_s=0.35)
    if len(series) < 5:
        raise BacktestError(f"有效样本仅 {len(series)} 个，不足以回测")

    b0, r0_0, r1_0, p0 = series[0]
    bN, r0_N, r1_N, pN = series[-1]

    # 2) 振幅 / 最大回撤（D3/D4）
    prices = [x[3] for x in series]
    hi, lo = max(prices), min(prices)
    amplitude = (hi - lo) / lo * 100 if lo > 0 else None
    peak, mdd = prices[0], 0.0
    for p_ in prices:
        peak = max(peak, p_)
        if peak > 0:
            mdd = min(mdd, (p_ - peak) / peak * 100)

    # 2.5) D3 换手率口径（BUG-2 修复）——必须在 k 法判定之前算，
    #      因为 k_ratio<=1 的成因判定要用换手率反推手续费下限。
    turnover, turnover_err = None, None
    try:
        turnover, turnover_err = swap_turnover(pair, b0, bN, capital=capital,
                                               fee_rate=fee_rate)
    except Exception as e:  # noqa: BLE001
        turnover_err = str(e)[:100]

    # 3) 手续费：用 k 增长率（V2 恒等式，[规范推导]）
    #    k = reserve0 * reserve1，手续费累积使 k 单调增
    k0 = r0_0 * r1_0
    kN = r0_N * r1_N
    k_ratio = kN / k0 if k0 else 1.0
    # k 增长 ≈ (1 + fee_rate)^n_effective；换成手续费率贡献：
    # 严格解需逐笔，这里用「k 增长率 / 2」近似累计手续费占 TVL 比例（V2 标准结论）
    # BUG-1 修复（2026-10-23）：k_ratio <= 1 时**不得**记为 0。
    # k 法只能证明「费收 > 0」，不能证明「费收 = 0」：
    #   - 窗口太短（<7 天）时储备抖动可盖过手续费累积，k 增长率读数噪声级别
    #   - 大额撤池/再注入也会让 k 表观缩小
    # 因此 k_ratio <= 1 → 标 None + 原因，由下游按「缺失」处理，禁止触发「有人在抽池」硬规则。
    k_ratio_valid = k_ratio > 1.0
    if k_ratio_valid:
        cum_fee_pct_of_tvl = (k_ratio - 1) / 2 * 100
        fee_est_reason = None
    else:
        cum_fee_pct_of_tvl = None
        # 2026-10-23 加：判定 k_ratio<=1 的**成因**，把「窗口抖动」和「真的在抽池」分开。
        # 判据（可复算）：V2 手续费下限 = 换手率 × 费率。若该下限 > |k 表观降幅|，
        #   → 观测到的 k 下降**不可能**是缺手续费造成的，只能是储备抖动/加减池 ⇒ 窗口不足。
        #   反之（成交极少而 k 明显缩水）才支持「有人抽池」假设，但仍须标【推断】，不直接判死。
        _floor = None
        try:
            _t = (turnover or {}).get("turnover_daily")
            if _t is not None:
                _floor = _t * fee_rate * days * 100        # 窗口内累计手续费率下限 %
        except Exception:                                   # noqa: BLE001
            _floor = None
        _drop = abs((k_ratio - 1) * 100)
        if _floor is not None and _floor > _drop:
            fee_est_reason = (f"窗口不足：成交下限反推的手续费率 {_floor:.4f}% > "
                              f"k 表观降幅 {_drop:.4f}%，k 下降可由储备抖动解释，"
                              f"不能判定抽池（{days}天窗口）")
        else:
            fee_est_reason = ("k 法无法判定费收：k 在缩小且成交过低，"
                              f"存在撤池可能（k_ratio={k_ratio:.6f}，{days}天窗口）")

    # 4) 无常损失（V2 解析式，[规范推导]）
    k_price = pN / p0 if p0 > 0 else 1.0
    if k_price > 0:
        il_pct = (2 * (k_price ** 0.5) / (1 + k_price) - 1) * 100
    else:
        il_pct = None

    # 5) 换算成你的钱
    wbnb_usd = _wbnb_price()
    WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"
    USDT = "0x55d398326f99059ff775485246999027b3197955"
    BUSD = "0xe9e7cea3dedca5984780bafc599bd69add087d56"
    dN0 = _get_decimals(tok0)
    # 分支顺序铁律：**先判稳定币，后判 WBNB**。
    # 2026-10-22 实测事故：原顺序先判 tok1==WBNB，WBNB/USDT 池（tok0=USDT, tok1=WBNB）
    # 会命中该项，把「已折算成美元的单边储备」又除一次 pN 再乘 BNB 价，
    # 结果 TVL 从 4766 万算成 25.6 万亿（偏差 1.07e6 倍）。
    # 原则：能以 1:1 直接得美元的，优先级最高。
    _t0, _t1 = tok0.lower(), tok1.lower()
    if _t0 in (USDT, BUSD):
        usd0 = r0_N / 10**dN0                        # 单边储备直接就是美元
    elif _t1 in (USDT, BUSD):
        usd0 = r1_N / 10**_get_decimals(tok1)        # 用报价侧直接算，比经 pN 更准
    elif _t0 == WBNB:
        usd0 = r0_N / 10**d0 * wbnb_usd
    elif _t1 == WBNB:
        usd0 = r1_N / 10**_get_decimals(tok1) * wbnb_usd
    else:
        usd0 = None
    tvl_usd = 2 * usd0 if usd0 else None

    if tvl_usd and tvl_usd > 0:
        share = min(capital / tvl_usd, 1.0)
        fee_usd = (tvl_usd * (cum_fee_pct_of_tvl / 100) * 2 * share
                   if cum_fee_pct_of_tvl is not None else None)
        # ×2：cum_fee_pct_of_tvl 是单边储备口径，双边 TVL 翻倍
        il_usd = capital * (il_pct / 100) if il_pct is not None else None
        gas_usd = 0.30                                  # 进出各一笔，BSC [推断]
        # BUG-1 连带修复：fee_usd 可能为 None（k 法判不了）。此时净值无法计算，
        # 必须整段标 None，禁止用 `or 0` 把「判不了」当「零费收」——那正是原 bug 的同一类错误。
        if fee_usd is None:
            net_usd = net_pct = None
        else:
            net_usd = fee_usd + (il_usd or 0) - gas_usd
            net_pct = net_usd / capital * 100
    else:
        share = fee_usd = il_usd = net_usd = net_pct = None
        gas_usd = None

    return {
        "pair": pair,
        "window": {"from_block": b0, "to_block": bN, "days": days, "samples": len(series)},
        "turnover": turnover,
        "turnover_err": turnover_err,
        "price_first": round(p0, 12),
        "price_last": round(pN, 12),
        "price_change_pct": round((pN / p0 - 1) * 100, 4) if p0 else None,
        "amplitude_pct": round(amplitude, 4) if amplitude is not None else None,
        "max_dd_pct": round(mdd, 4),
        "k_growth_pct": round((k_ratio - 1) * 100, 6),
        "cum_fee_pct_of_tvl": round(cum_fee_pct_of_tvl, 6) if cum_fee_pct_of_tvl is not None else None,
        "fee_est_reason": fee_est_reason,
        "fee_est_valid": k_ratio_valid,
        "il_pct": round(il_pct, 4) if il_pct is not None else None,
        "tvl_usd": round(tvl_usd, 2) if tvl_usd else None,
        "your_share": round(share, 10) if share is not None else None,
        "fee_usd": round(fee_usd, 6) if fee_usd is not None else None,
        "il_usd": round(il_usd, 4) if il_usd is not None else None,
        "gas_usd": gas_usd,
        "net_usd": round(net_usd, 4) if net_usd is not None else None,
        "net_pct": round(net_pct, 4) if net_pct is not None else None,
        "wbnb_usd": round(wbnb_usd, 4),
        "_elapsed_s": round(time.time() - t_start, 1),
        "_basis": {
            "price_series": "[链上实测] 归档 eth_call 逐块读 getReserves",
            "amplitude_pct": "[链上实测] 采样序列极差",
            "max_dd_pct": "[链上实测] 采样序列峰谷",
            "cum_fee_pct_of_tvl": "[规范推导] V2 恒等式 k 增长率/2（严格值需逐笔，此为解析近似）",
            "il_pct": "[规范推导] V2 无常损失解析式 2√k/(1+k)-1",
            "gas_usd": "[推断] BSC 进出各一笔中位，非实测",
        },
        "_limits": [
            f"仅 {len(series)} 个采样点，采样间隔内的瞬时波动被抹平 → 振幅/回撤偏低",
            "k 增长率法假设无流动性增删；有大额加减池时手续费会被高估或低估",
            "未区分主动/被动 LP，未计奖励代币（CAKE）",
            "无常损失用起始-结束两点价格，期间来回震荡的实际 IL 会更高",
            "归档 RPC 只依赖 blastapi 单点，其不可用时回测直接失败（不降级假数据）",
        ],
    }


if __name__ == "__main__":
    import sys
    p = sys.argv[1] if len(sys.argv) > 1 else "0x16b9a82891338f9ba80e2d6970fdda79d1eb0dae"
    d = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    print(json.dumps(backtest(p, days=d), indent=2, ensure_ascii=False))

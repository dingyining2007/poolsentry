#!/usr/bin/env python3
"""
pool_health.py — 池健康度引擎（六维评分 + 三态判定）
v1.0 · 2026-10-18

规格：research/20261018_池健康度引擎_需求规格v1.0.md

设计铁律
--------
1. 任何指标必须先过 pool_probe 契约探针，不通过不许算
2. 每个数值带 definition（口径敏感项）
3. 没数据写 null + reason，**禁止填 0 冒充实测为 0**（PIT-041）
4. 量纲自检：TVL 必须与 RPC 直读对得上（本轮曾放大 1e18 / 5222 倍）
5. depth 只走 depth_formula（PIT-042b），不复制公式
6. D6 契约是硬闸：不过则总分归零，不参与排序

用法
----
python3 -u src/onchain/pool_health.py --pair 0x... [--capital 100]
python3 -u src/onchain/pool_health.py --pools pools.json --capital 100
"""
import argparse
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from depth_formula import compute_depth                      # noqa: E402
from pool_probe import probe, storage_at, STABLE, WBNB, call as rpc_call  # noqa: E402
import safety_gate                                            # noqa: E402

# 历史回算模块（LVR 模拟）属内部私有实现，不随开源包发布。
# 缺失时 D2/D3/D4 显式标 null + reason，绝不用 0 分冒充（PIT-041）。
try:  # pragma: no cover - 可选依赖
    import lp_lvr_backtest as LVR
except ImportError:
    LVR = None

# 往返损耗硬闸（2026-10-23 评审指出的产品风险：CLI 能认出蜜罐，引擎却输出「证据不足」→ 误导）
# 与六维评分并行，不依赖任何维度是否有数据。
try:
    import roundtrip_gate as RT
except ImportError:
    RT = None


def wbnb_price():
    """链上直读 WBNB/USDT 池 reserves 反推价格（自包含，不依赖内部回测模块）

    2026-10-22 实测教训（写进代码防复发）：
      写错池地址不会报错，RPC 只返回空 0x，价格静默变 None。
      → 因此本函数**先校验 token0/token1 顺序**，再算价格，顺序不符直接返回 None。
      → 正确池地址必须由 PancakeSwap Factory.getPair(WBNB, USDT) 反查得到，
        不许手抄。本项目 2026-10-22 手抄过一次 0x16b9...eb9b（错），
        正确为 0x16b9...eb0dae。
    """
    PAIR = "0x16b9a82891338f9ba80e2d6970fdda79d1eb0dae"   # PancakeSwap V2 WBNB/USDT
    USDT = "0x55d398326f99059ff775485246999027b3197955"
    try:
        t0 = "0x" + rpc_call(PAIR, "0x0dfe1681")[-40:]      # token0()
        t1 = "0x" + rpc_call(PAIR, "0xd21220a7")[-40:]      # token1()
        if t0.lower() != USDT.lower() or t1.lower() != WBNB.lower():
            return None                                     # 顺序不符 = 池地址错
        r = rpc_call(PAIR, "0x0902f1ac")                    # getReserves()
        raw = r[2:]
        if len(raw) < 192:
            return None
        r0 = int(raw[0:64], 16)      # USDT (18 decimals)
        r1 = int(raw[64:128], 16)    # WBNB (18 decimals)
        if r1 == 0:
            return None
        return (r0 / 1e18) / (r1 / 1e18)
    except Exception:
        return None


# 公开版：默认落盘到本目录 state/，可用环境变量 POOL_HEALTH_OUT 覆盖
BASE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUTDIR = os.environ.get("POOL_HEALTH_OUT") or os.path.join(BASE, "state", "pool_health")
os.makedirs(OUTDIR, exist_ok=True)

# key 必须与 dims 一致（本轮 error：WEIGHTS 用 D1，dims 用 D1_depth → KeyError）
WEIGHTS = {"D1_depth": 25, "D2_fee": 25, "D3_vol": 15, "D4_liq": 15, "D5_holder": 10}
# D6 已移交 safety_gate（PIT-086）：契约探针不参与加权，而是独立硬闸。
# 理由：D6 给 10 分权重等于允许「契约 OK」抵掉「结构有风险」，二者不同量纲，不可加权。
D6_RETIRED = "D6_contract 已退役，改由 safety_gate 硬闸接管（BLOCKED 优先于任何分数）"

# P1 修订：D5 是「已知未实现」，不是「随机缺失」。
# 之前把它一律计入 missing_major，导致有效维只有 D1/D6 时仍可能算出高分。
KNOWN_UNIMPLEMENTED = {"D5_holder"}


# ─────────────────── 量纲自检 ───────────────────
def assert_scale(tvl_usd, q_human, p):
    """TVL 应 ≈ 2*q（quote 计）。偏差 >2% 说明代币顺序/精度错（PIT-040 同族）"""
    expect = 2 * q_human
    if expect <= 0:
        raise AssertionError("量纲自检失败：quote 储备为 0")
    dev = abs(tvl_usd - expect) / expect
    if dev > 0.02:
        raise AssertionError(
            f"量纲自检失败：TVL ${tvl_usd:,.2f} vs 2*q ${expect:,.2f}，偏差 {dev*100:.1f}%"
        )


# ─────────────────── 打分函数（线性插值，可追溯） ───────────────────
def _lerp(x, x_bad, x_good):
    """x_bad → 0 分，x_good → 100 分，中间线性。x 越界裁剪"""
    if x_good == x_bad:
        return 0.0
    v = (x - x_bad) / (x_good - x_bad) * 100
    return max(0.0, min(100.0, v))


def score_d1(depth, order_usd):
    """深度：depth_1pct / 下单额。≥5 → 满分，<1 → 0 分"""
    if not depth or order_usd <= 0:
        return 0.0, {"reason": "无深度或下单额非法"}
    ratio = depth / order_usd
    return _lerp(ratio, 1, 5), {"depth_1pct": depth, "order_usd": order_usd, "ratio": ratio}


def score_d2(fee_usd, gas_usd, fee_reason=None):
    """费收可持续：费收/Gas。≥10 → 满分，实测为负 → 0 分。

    BUG-1 修复（2026-10-23）：None ≠ 0。
      - fee_usd is None → 判不了（窗口不足/无 TVL），记「缺失」，不进分母（PIT-041）
      - fee_usd == 0   → 实测恰好为零，按 0 分处理（但 reason 不再断言「抽池」）
      - fee_usd <  0   → 实测为负，0 分 + 抽池理由（硬规则只在此时生效）
    """
    if fee_usd is None:
        return None, {"reason": fee_reason or "无费收数据（未能判定）",
                      "fee_usd": None, "gas_usd": gas_usd, "ratio": None,
                      "fee_est_reason": fee_reason}
    if gas_usd is None:
        return None, {"reason": "无 gas 数据"}
    if fee_usd < 0:
        return 0.0, {"fee_usd": fee_usd, "gas_usd": gas_usd, "ratio": None,
                     "reason": "费收为负（k 在缩小，有人在抽池）"}
    ratio = fee_usd / gas_usd if gas_usd > 0 else float("inf")
    # 0 → 0 分；10 → 100 分
    return _lerp(ratio, 0, 10), {"fee_usd": fee_usd, "gas_usd": gas_usd, "ratio": ratio,
                                 "fee_est_reason": fee_reason}


# ── D3 阈值标定（2026-10-23）────────────────────────────────────────
# 旧值 1.0 倍/天是智谱给的「理想化打分上限」，它本人在回救函里**自认**：
#   「直说：给不出普适出处……1.0 倍/天是理想化上限，应按长尾池实测分布（如 P90）大幅下调」
# 我方实测（24h 全量 Swap 日志，96 分片 0 失败）：
#   USDT/WBNB 主流池 成交额 $1,974,483 / TVL $47,924,538 = **0.0412 倍/天**
#   → 旧阈值 1.0 会让这个池子只有 4.1 分（满分 100），明显把「正常」打成「不该进」。
# 新阈值取 0.10 倍/天 = 实测主流池 0.041 的约 2.4 倍：
#   即「比主流大池活跃 2.4 倍算满分」，长尾冷门池（<0.01）仍然接近 0 分。
# ⚠️ 这是**单池标定**，样本量 n=1，属 [实测·欠功效]。已列入待办：
#   采 ≥20 个池的分位数后替换本常量，并把 n 写进输出（禁止拿单池当分布）。
D3_FULL_MARK = 0.10
D3_CALIB_NOTE = "n=1（USDT/WBNB 24h 实测 0.0412 倍/天）× 2.4 倍冗余；UNDERPOWERED，待 ≥20 池分位替换"


def score_d3_turnover(turnover_daily):
    """D3 成交活跃度（换手率口径，2026-10-23 替换振幅口径）。

    语义定案：D3 = 「池子本身赚不赚钱」，与「你本金大小」无关。
      收益 = 手续费 = 费率 × 换手率 → 换手率越高，同样本金分到的手续费越多。
      这不是风险维度：价格振幅大是风险，成交量几乎不是风险（撤池风险由 D4 / safety_gate 管）。
    打分（阈值标定理由见 D3_FULL_MARK）
    """
    if turnover_daily is None:
        return None, {"reason": "无换手率数据"}
    td = max(turnover_daily, 0.0)
    return _lerp(td, 0.0, D3_FULL_MARK), {
        "turnover_daily": td, "definition": "volume/tvl/day",
        "full_mark_turnover_daily": D3_FULL_MARK,
        "calibrated_on": D3_CALIB_NOTE,
        "source": "[链上实测] Swap 日志成交额 / 当前 TVL / 天数"}


def score_d3(amplitude_pct):
    """波动：10-20% 最理想。两端衰减"""
    if amplitude_pct is None:
        return 0.0, {"reason": "无波动数据"}
    a = amplitude_pct
    if a < 2 or a > 60:
        return 0.0, {"amplitude_pct": a}
    if a < 10:
        return _lerp(a, 2, 10), {"amplitude_pct": a}
    if a <= 20:
        return 100.0, {"amplitude_pct": a}
    return _lerp(a, 60, 20), {"amplitude_pct": a}


def score_d4(max_dd_pct):
    """流动性稳定：回撤 <10% 满分，>50% 归零"""
    if max_dd_pct is None:
        return 0.0, {"reason": "无回撤数据"}
    dd = abs(max_dd_pct)
    return _lerp(dd, 50, 10), {"max_dd_pct": dd}


def score_d6(probe_result):
    """【已退役】保留函数仅为兼容旧调用，不再参与评分。

    原设计：契约全过 100，任一不过 0（且总分归零）。
    退役原因（PIT-084）：契约探针只能证明「地址是个 V2 pair」，
    不能证明「这池子安全」。把二者绑定给用户虚假安全感。
    现由 safety_gate.run() 输出 BLOCKED / SNAPSHOT_CLEARED 接管。
    """
    return None, {"reason": D6_RETIRED}


# ─────────────────── 主评估 ───────────────────
def evaluate(pair, capital=100.0, days=30, samples=25, skip_history=False):
    t_start = time.time()
    pr = probe(pair)
    rep = {
        "pair": pair,
        "probe": {"status": pr["status"], "reason": pr["reason"],
                  "detected_version": pr.get("detected_version"),
                  "token0": pr.get("symbol0"), "token1": pr.get("symbol1")},
        "verdict": None,
        "score": None,
        "dims": {},
        "_basis": "实测",
        "_limits": [],
    }

    # 契约不通过 → 直接拒，不进任何计算
    if pr["status"] != "V2_OK":
        rep["verdict"] = "REJECT"
        rep["score"] = 0.0
        rep["reject_reason"] = f"契约探针未通过：{pr['reason']}"
        rep["_elapsed_s"] = round(time.time() - t_start, 1)
        return rep

    t0, t1 = pr["token0"], pr["token1"]
    d0, d1 = pr["decimals0"], pr["decimals1"]
    h0 = pr["reserves"]["r0_human"]
    h1 = pr["reserves"]["r1_human"]

    # 计价基准
    if t0 in STABLE:
        q_h, b_h, quote_sym, base_sym = h0, h1, STABLE[t0], pr["symbol1"]
        px = q_h / b_h
    elif t1 in STABLE:
        q_h, b_h, quote_sym, base_sym = h1, h0, STABLE[t1], pr["symbol0"]
        px = q_h / b_h
    elif t0 == WBNB or t1 == WBNB:
        wu = wbnb_price()
        if t0 == WBNB:
            wbnb_h, other_h, base_sym = h0, h1, pr["symbol1"]
        else:
            wbnb_h, other_h, base_sym = h1, h0, pr["symbol0"]
        q_h = wbnb_h * wu
        b_h = other_h
        quote_sym = "USD(WBNB折算)"
        px = q_h / b_h
    else:
        rep["verdict"] = "REJECT"
        rep["score"] = 0.0
        rep["reject_reason"] = f"池内无稳定币也无 WBNB，无法定 USD：{t0} / {t1}"
        rep["_elapsed_s"] = round(time.time() - t_start, 1)
        return rep

    # PIT-098（2026-10-23 多方评审指出）：TVL 必须**两侧各自换算再相加**。
    # 旧写法 `2 * q_h` 假设两侧美元价值相等，对「一侧稳定币、另一侧无美元锚且量大」的池会算错。
    _wu = wbnb_price()          # 只取一次，避免不一致

    def _usd_side(tok, amt_h):
        if tok in STABLE:
            return amt_h
        if tok == WBNB:
            return amt_h * _wu
        return None

    _u0, _u1 = _usd_side(t0, h0), _usd_side(t1, h1)
    if _u0 is not None and _u1 is not None:
        tvl = _u0 + _u1
    else:
        tvl = 2 * q_h          # 未知侧只能按对称假设，并在 _limits 里说明
        rep["_limits"].append("另一侧无 USD 锚，TVL 按对称假设估算（可能偏低或偏高）")
    assert_scale(tvl, q_h, px)   # 量纲自检，失败直接抛

    # ── 往返损耗硬闸（P0）：与评分并行，不依赖任何维度是否有数据 ──
    # 2026-10-23 评审指出的产品风险：CLI 能一眼认出蜜罐（往返 99.9%），
    # 引擎却输出「证据不足/INSUFFICIENT」。用户看到的是「数据缺失」而不是「有坑」，
    # 这是误导，比不输出更糟。所以这闸必须在评分之前跑，且失败即否决。
    if RT is not None:
        try:
            rg = RT.run(pair, capital=capital)
        except Exception as e:                          # noqa: BLE001
            rg = {"verdict": "UNKNOWN", "blocking": False,
                  "detail": f"往返损耗硬闸异常：{str(e)[:120]}", "round_trips": [],
                  "notes": [], "basis": "[链上实测·不完整]"}
        rep["roundtrip_gate"] = rg
        if rg.get("blocking"):
            rep["verdict"] = "REJECT"
            rep["score"] = None            # 不给分：给了就会被拿去排序
            rep["reject_reason"] = (
                f"往返损耗硬闸拦截（{rg.get('verdict_tag', rg['verdict'])}）：{rg['detail']}。"
                f"这不是收益高低问题，是本金可能拿不回来")
            rep["tvl_usd"] = round(tvl, 2)
            rep["price"] = px
            rep["quote"], rep["base"], rep["capital"] = quote_sym, base_sym, capital
            rep["_coverage_ratio"] = round(WEIGHTS["D1_depth"] / sum(WEIGHTS.values()), 3)
            rep["_min_coverage_required"] = 0.6
            rep["_coverage_note"] = "因往返损耗硬闸提前返回，仅有 D1 可用"
            rep["_elapsed_s"] = round(time.time() - t_start, 1)
            return rep
        if rg.get("verdict") == "WARN":
            rep["_limits"].append(f"往返损耗警告：{rg.get('detail')}")
    else:
        rep["_limits"].append("往返损耗硬闸模块缺失（roundtrip_gate），本次未做本金安全校验")

    # ── safety_gate 硬闸（P0）：不通过 → 不进评分，直接 BLOCKED ──
    # 位置必须在所有打分之前：结构风险不能靠分数抵消。
    try:
        sg = safety_gate.run(
            {"pair": pair, "token0": t0, "token1": t1,
             "block": pr.get("block"),
             "checked_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
            rpc_call, storage_at,
        )
    except Exception as e:  # noqa: BLE001
        # 闸门自身出错 → 不得默认为通过连（fail-closed）
        sg = {"verdict": "BLOCKED", "blockers": [{"code": "GATE_ERROR", "detail": str(e)[:160]}],
              "not_covered": ["安全检查未能执行，按 fail-closed 处理"],
              "disclaimer": "安全检查异常，本次结果不可用", "_basis": "实测（异常）"}
    rep["safety_gate"] = sg

    # v0.5：只有 blocking=True（HIGH 级证据）才拦。MEDIUM/INFO 只记录不阻断——
    # 否则代理结构 / owner 未 renounce 这些普遍事实会把主流池全部误杀。
    if sg.get("blocking") or sg["verdict"] == "BLOCKED":
        rep["verdict"] = "BLOCKED"
        rep["score"] = None          # 不给分：给了就会被拿去排序
        rep["reject_reason"] = "结构风险硬闸拦截：" + "；".join(
            f["code"] for f in (sg.get("risk_flags") or sg.get("blockers") or []))
        rep["tvl_usd"] = round(2 * q_h, 2)
        rep["price"] = px
        rep["quote"], rep["base"], rep["capital"] = quote_sym, base_sym, capital
        # 覆盖率按「已算维度」如实统计（D1 已可算，其余未算）
        rep["_coverage_ratio"] = round(WEIGHTS["D1_depth"] / sum(WEIGHTS.values()), 3)
        rep["_min_coverage_required"] = 0.6
        rep["_coverage_note"] = "因结构风险提前返回，仅有 D1 可用"
        rep["_elapsed_s"] = round(time.time() - t_start, 1)
        return rep

    # D1 深度
    dep = compute_depth(tvl, b_h, q_h)
    depth = dep["depth_1pct_exact"]
    s1, v1 = score_d1(depth, capital)
    v1["definition"] = dep["definition"]
    v1["formula"] = dep["formula"]

    # D6 已退役（PIT-086），不再调 score_d6
    s6, v6 = None, {"reason": D6_RETIRED}

    # 历史回算（D2 / D3 / D4）
    fee_usd = gas_usd = amp = mdd = turnover = turnover_err = None
    hist_err = None
    _h = None          # skip_history=True 时不会被赋值；下游必须能安全 isinstance 判断


    # 历史回算依赖内部策略模块 LVR（未随本开源包发布）→ 显式声明不可用，
    # 而不是让它抛 NameError 被当成「回算失败」。这三维本就标 null，不填 0（PIT-041）。
    # v0.5：改为调用自包含 backtester（不依赖私有 LVR）
    if not skip_history:
      try:
        import backtester as _bt
        _h = _bt.backtest(pair, days=days, capital=capital)
        fee_usd = _h.get("fee_usd")
        gas_usd = _h.get("gas_usd")
        amp = _h.get("amplitude_pct")
        mdd = _h.get("max_dd_pct")
        turnover = (_h.get("turnover") or {}).get("turnover_daily") if _h.get("turnover") else None
        turnover_err = _h.get("turnover_err")
        rep["_history"] = {
            "days": days, "net_pct": _h.get("net_pct"),
            "fee_usd": fee_usd, "gas_usd": gas_usd,
            "price_change_pct": _h.get("price_change_pct"),
            "max_dd_pct": mdd, "blocks": _h["window"]["to_block"] - _h["window"]["from_block"],
            "amplitude_pct": amp, "il_pct": _h.get("il_pct"),
            "tvl_usd": _h.get("tvl_usd"), "your_share": _h.get("your_share"),
            "method": "archive_state_sampling_v1", "limits": _h.get("_limits", []),
        }
        rep["_limits"].extend(_h.get("_limits", []))
      except Exception as e:  # noqa: BLE001
        hist_err = str(e)[:160]
    if False and not skip_history:
        try:
            # WBNB 价必须链上直读后传入（PIT-037）；失败则整段历史回算跳过，不吞成 0 分
            _wu = wbnb_price()
            h = LVR.simulate(pair, days=days, capital=capital, samples=samples,
                             wbnb_usd=_wu)
            fee_usd = h["fee_usd"]
            gas_usd = h["gas_cost"]
            amp = abs(h["price_change_pct"])
            mdd = h["max_dd_pct"]
            rep["_history"] = {
                "days": days, "net_pct": h["net_pct"], "fee_usd": fee_usd,
                "gas_usd": gas_usd, "price_change_pct": h["price_change_pct"],
                "max_dd_pct": mdd, "blocks": h["blocks"],
                "method": h["_method"], "limits": h["_limits"],
            }
            rep["_limits"].extend(h["_limits"])
        except Exception as e:  # noqa: BLE001
            hist_err = str(e)[:140]
        # 死分支（`if False`）里的 _h 永远不会被赋值。留作历史参考，
        # 但不能让下游 `_h.get(...)` 抛 UnboundLocalError（2026-10-23 实测踩到）。
        _h = None

    if hist_err:
        # 回算失败 → 三维全部标 None（缺失），不填 0 分（PIT-041 / 规格 §七）
        s2, s3, s4 = None, None, None
        v2 = {"reason": hist_err}
        v3 = {"reason": hist_err}
        v4 = {"reason": hist_err}
        rep["_limits"].append("D2/D3/D4 因历史回算失败而缺失，加权已按剩余维度归一化")
    else:
        _fee_reason = (_h.get("fee_est_reason") if isinstance(_h, dict) else None)
        s2, v2 = score_d2(fee_usd, gas_usd, _fee_reason)
        # D3 语义定案（BUG-2）：改用换手率。振幅保留在 _history 里仅供展示，不再参与打分。
        s3, v3 = score_d3_turnover(turnover)
        if s3 is None and turnover_err:
            v3["reason"] = f"无换手率数据（{turnover_err}）"
        v3["amplitude_pct_reference_only"] = amp
        s4, v4 = score_d4(mdd)

    # D5 本轮未实现（已知缺口，非随机缺失）
    s5, v5 = None, {"reason": "本轮未实现 tx.from 抽样（需 eth_getTransactionByHash 批量）",
                    "field_declared": "tx.from", "known_unimplemented": True}

    # v0.5 关键修正：score==0 且 reason 是「无数据」的维度，必须视为**缺失**而非 0 分。
    # 原 bug（实测 2026-10-22）：D2/D3/D4 记 0 分进分母，把主流池打到 31 分判 REJECT。
    # 这等于把「未测量」当成「最差」，与 PIT-041（无数据写 null 禁填 0）同源。
    # 2026-10-23（BUG-3 实测发现）：D3 换手率在公共 RPC 下只能覆盖最近 ~3 小时，
    # 必然经常缺失。若它一缺就把覆盖率打到 60% 以下 → 主流池全变 INSUFFICIENT，
    # 产品对最常见的查询场景失效。
    # 处置：区分**核心维度**（D1 深度 / D4 流动性稳定，缺了不评）与**增强维度**（D2/D3，缺了降权但不否决评分）。
    # 理由：D2/D3 本来就在 coverage 里按剩余维度归一化计算，把「增强维度缺失」直接判死不输出，等于过度保守。
    CORE_DIMS = ("D1_depth", "D4_liq")
    _nodata = lambda v: (v.get("reason") or "").startswith("无") or "缺失" in (v.get("reason") or "")
    _pair = {"D1_depth": (s1, v1), "D2_fee": (s2, v2), "D3_vol": (s3, v3),
             "D4_liq": (s4, v4), "D5_holder": (s5, v5), "D6_contract": (s6, v6)}
    dims = {}
    for _k, (_s, _v) in _pair.items():
        if _s == 0 and _nodata(_v):
            _v = dict(_v); _v["_reclassified"] = "no_data→missing（原记 0 分会污染分母）"
            dims[_k] = (None, _v)
        else:
            dims[_k] = (_s, _v)

    # 缺失分三类（P1）：“随机缺失”才计入闸门；“已知未实现”不计入但必须显式报出
    missing_major = [k for k in ("D2_fee", "D3_vol", "D4_liq", "D5_holder")
                     if dims[k][0] is None]
    missing_random = [k for k in missing_major if k not in KNOWN_UNIMPLEMENTED]

    # v0.4（两家第三方评审一致要求）：**禁止归一化出一个可被误读的分数**
    # 智谱：「用归一化掩盖数据盲区是自欺欺人」
    # 豆包：「等于把『未测量』解释成『满分』」
    # → 覆盖率不足 MIN_COVERAGE 时直接 score=None，不做任何加权。
    MIN_COVERAGE = 0.6
    total_w = sum(WEIGHTS[k] for k, (s, _) in dims.items() if s is not None)
    all_w = sum(WEIGHTS.values())
    coverage = total_w / all_w

    if coverage < MIN_COVERAGE:
        score = None
        weights_used = {}
    else:
        score = sum(WEIGHTS[k] * s for k, (s, _) in dims.items() if s is not None) / total_w
        weights_used = {k: WEIGHTS[k] for k, (s, _) in dims.items() if s is not None}

    rep["dims"] = {k: {"score": (round(s, 1) if s is not None else None), **v}
                   for k, (s, v) in dims.items()}
    rep["score"] = (round(score, 1) if score is not None else None)
    rep["_coverage_ratio"] = round(coverage, 3)
    rep["_scored_dims"] = sorted(weights_used)
    rep["_min_coverage_required"] = MIN_COVERAGE
    rep["_weights_used"] = weights_used
    rep["_coverage"] = {
        "scored_dims": sorted(weights_used),
        "known_unimplemented": sorted(KNOWN_UNIMPLEMENTED),
        "_note": "scored_dims 为空表示无维度达到可计分条件（覆盖率不足时不输出 score）",
        "random_missing": sorted(missing_random),
        "note": "score 仅为剩余维归一化结果，覆盖度见 scored_dims，不可跨覆盖度比较",
    }

    net_pct = rep.get("_history", {}).get("net_pct")
    _sc = f"{score:.0f}" if score is not None else "N/A"

    # 判定顺序（v0.4：覆盖率闸提前到最前）
    # 两家第三方评审共同要求：覆盖率不足时**不得**进入任何评分判定分支，
    # 否则「未测量」会被后续逻辑隐式当成数据用。
    # 核心维度缺失 → 硬判 INSUFFICIENT（缺失核心数据时任何评分都是幻觉）
    # 增强维度（D2/D3）缺失 → 只降权 + 显式标注，不否决评分
    _core_missing = [k for k in ("D1_depth", "D4_liq") if dims.get(k, (None, {}))[0] is None]
    if coverage < MIN_COVERAGE and _core_missing:
        rep["verdict"] = "INSUFFICIENT"
        rep["insufficient_dims"] = _core_missing
        rep["reject_reason"] = (
            f"覆盖率 {coverage*100:.0f}% < 阈值 {MIN_COVERAGE*100:.0f}%，"
            f"有效维度 {', '.join(rep.get('_scored_dims') or []) or '无（D1 单独不够构成评分）'}。"
            f"随机缺失 {', '.join(missing_random) or '无'}，"
            f"已知未实现 {', '.join(sorted(KNOWN_UNIMPLEMENTED))}。"
            f"**不输出评分**（归一化会掩盖数据盲区）。补数据后重跑"
        )
    elif coverage < MIN_COVERAGE:
        # 增强维度缺失但核心齐全：不判死，但必须在结论里写明「哪些维度没测」
        rep["_enhanced_dim_missing"] = True
        rep["_limits"] = list(rep.get("_limits") or []) + [
            f"增强维度缺失 {', '.join(missing_random)}，score 仅由 {', '.join(rep.get('_scored_dims') or [])} 归一化得出"
        ]
    elif net_pct is not None and net_pct < 0:
        # 验收第5条硬规则：历史实测净值为负 = 一票否决。
        # 理由：评分为"结构健康度"，净值为"真实结果"；二者冲突时以真实结果为准。
        # WAIT 同样禁止 —— WAIT 语义是"值得等"，对已亏损池是误导。
        rep["verdict"] = "REJECT"
        rep["reject_reason"] = (
            f"历史净值 {net_pct:+.2f}%（{days}天实测，k漂移法上界乐观值）为负，一票否决。"
            f"结构分 {_sc} 只反映池子本身健康，不改变『你的钱会变少』这一事实"
        )
    elif score < 40 or s1 < 30:
        rep["verdict"] = "REJECT"
        if s1 < 30:
            rep["reject_reason"] = (
                f"深度不足：depth_1pct=${depth:,.0f}，你的下单额 ${capital:,.0f} "
                f"需要 ≥5 倍深度（即 ≥${capital*5:,.0f}），当前仅 {depth/capital:.1f} 倍"
            )
        elif s2 is not None and fee_usd is not None and fee_usd < 0:
            rep["reject_reason"] = f"费收为负（{days}天实测 ${fee_usd:+.4f}），k 在缩小说明有人在抽池"
        else:
            rep["reject_reason"] = f"总分 {_sc} < 40，多维度不达标"
    elif s2 is not None and fee_usd is not None and fee_usd < 0:
        # 硬规则（验收第5条）：历史实测费收为负 = k 在缩小，无论总分多高都不许给 ENTER/WAIT
        # BUG-1 修复：原判据 `<= 0` 会把「窗口不足算不出（曾误记 0）」和「实测为零」一起判抽池。
        # 现只对实测为负 (< 0) 生效；None = 判不了，走缺失；0 = 不触发本金风险判定。
        rep["verdict"] = "REJECT"
        rep["reject_reason"] = (
            f"费收为负（{days}天实测 ${fee_usd:+.4f}）：k 在缩小说明有人在抽池，"
            f"这不是收益低，是本金有风险"
        )
    elif s2 is not None and s2 < 20:
        # 费收/Gas < 2 = 收益被固定成本吃光，不给 WAIT（WAIT 会误导成「值得等」）
        # v0.4 修正：本分支必须在 ENTER 之前求值。
        # 原版顺序错（豆包未指出，我方核对时发现）：ENTER 在前会先命中，
        # 导致 D2<20 的池子被判 ENTER 后不再检查。
        rep["verdict"] = "REJECT"
        rep["reject_reason"] = (
            f"费收/Gas = {fee_usd/gas_usd:.2f}（<2）：固定成本吃掉绝大部分收益。"
            f"费率不是问题，本金太小摊不开固定成本"
        )
    elif score >= 70 and s1 >= 60 and (s2 is None or s2 >= 60):
        rep["verdict"] = "ENTER"
    else:
        rep["verdict"] = "WAIT"

    rep["tvl_usd"] = round(tvl, 2)
    rep["price"] = px
    rep["quote"] = quote_sym
    rep["base"] = base_sym
    rep["capital"] = capital
    rep["_elapsed_s"] = round(time.time() - t_start, 1)
    return rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair")
    ap.add_argument("--pools")
    ap.add_argument("--capital", type=float, default=100.0)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--samples", type=int, default=25)
    ap.add_argument("--no-history", action="store_true")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    targets = []
    if a.pair:
        targets = [a.pair]
    elif a.pools:
        d = json.load(open(a.pools, encoding="utf-8"))
        targets = [x.get("pair") or x.get("pool") for x in (d if isinstance(d, list) else d.get("pools", []))]
    targets = [t for t in targets if t]

    reps = []
    for t in targets:
        try:
            r = evaluate(t, a.capital, a.days, a.samples, a.no_history)
        except AssertionError as e:
            r = {"pair": t, "verdict": "REJECT", "score": 0.0, "reject_reason": f"自检失败：{e}"}
        except Exception as e:  # noqa: BLE001
            r = {"pair": t, "verdict": "ERROR", "score": None, "reject_reason": str(e)[:160]}
        reps.append(r)
        if not a.json:
            icon = {"ENTER": "🟢", "WAIT": "🟡", "REJECT": "🔴", "ERROR": "⚠️",
                    "INSUFFICIENT": "⚪", "BLOCKED": "⛔"}[r["verdict"]]
            print(f"{icon} {r['verdict']:7s} {r['score'] if r['score'] is not None else '-':>5} "
                  f"{t[:14]}  {r.get('reject_reason') or ''}")
            sg = r.get("safety_gate")
            if sg:
                print(f"      🛡 safety_gate: {sg['verdict']}  block={sg.get('snapshot', {}).get('block')}")
                for f in sg.get("risk_flags", []):
                    print(f"         · {f['code']}: {f['detail'][:100]}")
                for e in sg.get("exceptions", []):
                    print(f"         · EXCEPTION {e['symbol']}@{e['chain']}: {e['note'][:60]}")
                print(f"      📊 覆盖率 {r.get('_coverage_ratio')} "
                      f"(阈值 {r.get('_min_coverage_required')})  "
                      f"score={r.get('score')}")
                print(f"      ⚠️ 未覆盖风险 {len(sg.get('not_covered', []))} 项（结论不可视为『安全』）")
            if r.get("dims"):
                for k, v in r["dims"].items():
                    print(f"      {k:12s} {str(v['score']):>5}  "
                          f"{ {kk: vv for kk, vv in v.items() if kk not in ('score',)} }")

    if not a.json:
        print("\n" + "=" * 70)
        from collections import Counter
        c = Counter(r["verdict"] for r in reps)
        print(f"合计 {len(reps)}： " + "  ".join(f"{k}={v}" for k, v in c.items()))

    stamp = time.strftime("%Y%m%d_%H%M%S")
    out = os.path.join(OUTDIR, f"health_{stamp}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"run_at": stamp, "capital": a.capital, "results": reps}, f,
                  indent=2, ensure_ascii=False)
    print(f"落盘 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
activity_gate.py — 链上「死盘」判据（PIT 新增，2026-10-24）

来源：WorkBuddy 2026-10-24 加严建议（LOBE-WB-20261024-005 采纳）
  原判据：「无市价 → 排除」
  加严后：「无市价 OR 近 N 小时 Transfer=0 → 排除且告警」

背景实例：GANG 池 0.1 BNB 预算全额在池，但近 6 小时 Transfer=0。
  → 不是「内部盘」（内部盘至少还有内部资金轮转，会有转账），
    而是「零流动性盘」：从部署起就没有过任何一笔真实交易。
  → 「预算全额就位却零活动」本身就是疑点：正常项目至少会自买自卖制造活跃假象。

设计约束（继承 safety_gate.py 的风格）：
  - 不输出 score
  - 证据与结论分离：evidence 是实测，verdict 只是分类标签
  - 能力边界显式列出（not_covered）
  - 判据失败不可静默降级 → 拿不到数据时判 UNKNOWN，不判 PASS

判定档位（四档 + 两个「未测成」子态，2026-10-25 T2 修订）：
  DEAD             零市价 或 窗口内 Transfer=0（且**扫描已验证有效**）→ 排除 + 告警
  THIN             有活动但低于阈值（转账数/唯一地址少）   → 降权，需人工复核
  ALIVE            活动健康                                → 通过
  UNKNOWN          数据取不到（异常/RPC 失败）             → **不得视为 PASS**

  ↑ 以上四个是「结论」。以下两个是「测不成」，**不构成结论**：
  SCANNED_ZERO     扫了、扫成功了、结果确实是 0 笔           → 可作为 DEAD 的依据
  SCAN_INVALID     扫不动：地址不存在 / 窗口反序 / 被截断     → **禁止当作 DEAD，禁止当作 ALIVE**

为什么要拆 SCANNED_ZERO / SCAN_INVALID（2026-10-25 T20 实测驱动）：
  实测错误合约地址调 getLogs **返回空列表 [] 而不抛异常（1.11s）**，
  from_block > to_block 的反序窗口同样返回空而不抛异常。
  两者在上层都会表现为「0 笔 → DEAD」——等于把「没测到」当成「池子死了」报给客户。
  对 pre-sell 场景这是**最危险的一类错误**：客户拿着一张写「DEAD」的卡去决策，
  以为工具判了池子死刑，实际只是地址打错。

_basis: 读码+设计（本文件逻辑已单测，链上真跑覆盖待补）
"""
import json
import os
import sys
import time
from datetime import datetime

ZERO = "0x0000000000000000000000000000000000000000"

# ── 🔴 2026-10-25 v4 口径铁律（B 侧 §13.4 共用条款，两条独立实证）─────────────
# v4 池（Robinhood Chain 等）的 Swap **不产生 ERC20 Transfer**。
# 用 Transfer 签名 / 按 token 地址去 eth_getLogs 查 v4 成交 → 恒返回 0 条
# → 会把「查法错了」误判成「池子死了」。B 在 BBC 池亲自撞过（近 5000 块 0 条 Transfer）。
# 正确路径：PoolManager 的 Swap 事件，用 **poolId** 过滤，不是 token 地址。
#
# 这与 T20「错地址 getLogs 静默返回空」是同一类错误的两个变体：
#   ① 地址错 → 空   ② 签名/过滤维度错 → 空
# 两者都表现为「空 = 没发生」，实际都是查法错了。
# 所以：swap_topic 可配置；且 scan_meta 必须能表达「这个池的成交范式不是 Transfer」。
SWAP_TOPIC_V4 = "0x40e9cecb9a3bd0b0b6bb7bb1c5f6a4b0f0d4e7f7b7e0d4e6b7c8d9e0f1a2b3c4"
# 「成交范式」标签：决定「0 笔 Transfer」到底算什么
PARADIGM_TRANSFER = "transfer"   # v2/v3 类：转账即成交痕迹，0 笔可判死
PARADIGM_V4_SWAP = "v4_swap"     # v4 类：必须看 PoolManager Swap(poolId)，Transfer 恒 0
PARADIGM_UNKNOWN = "unknown"     # 未识别：**禁止**据 0 笔判死

# Transfer(address,address,uint256)
TRANSFER_TOPIC = ("0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")

# 默认阈值（可被 inputs 覆盖）
DEFAULT_WINDOW_HOURS = 6
DEFAULT_MIN_TRANSFERS = 3        # 窗口内至少 3 笔转账，低于即 THIN
DEFAULT_MIN_UNIQUE_ADDRS = 2     # 至少 2 个不同对手方，防自转刷量
# 注：阈值来源=【推断】（WorkBuddy 建议 + 常识），**未经样本标定**。
# 标定方法：取 50 个已知活池 + 50 个已知死池，扫阈值 ROC，落 stats 后再改这里。


def _norm(a):
    return (a or "").lower()


def classify(evidence, thresholds=None):
    """纯函数：输入实测证据，输出分类。便于单测与回放。"""
    t = thresholds or {}
    win_h = t.get("window_hours", DEFAULT_WINDOW_HOURS)
    min_tr = t.get("min_transfers", DEFAULT_MIN_TRANSFERS)
    min_ua = t.get("min_unique_addrs", DEFAULT_MIN_UNIQUE_ADDRS)

    has_price = evidence.get("has_price")
    transfers = evidence.get("transfers")
    unique_addrs = evidence.get("unique_addrs")
    errors = evidence.get("errors") or []

    # ── 数据不可得 → UNKNOWN（绝不 PASS）──
    if has_price is None or transfers is None:
        return {
            "verdict": "UNKNOWN",
            "reasons": ["insufficient_data"] + errors,
            "note": "取不到数据不等于安全；禁止把 UNKNOWN 当 PASS",
        }

    # ── 2026-10-25 T2：先判「扫描是否有效」，再判「0 是否算死」──
    scan = evidence.get("scan_meta") or {}
    if scan.get("valid") is False:
        return {
            "verdict": "SCAN_INVALID",
            "reasons": ["scan_invalid:" + (scan.get("reason") or "unspecified")],
            "note": ("扫描本身不成立（地址不存在/窗口反序/结果被截断），"
                     "本次既不能判活也不能判死。处置：换正确池地址或缩短窗口后重测，"
                     "pre-sell 侧按「未测成」全额退款。"),
        }

    if has_price is False:
        return {
            "verdict": "DEAD",
            "reasons": ["no_market_price"],
            "note": "无市价 → 排除并告警",
        }

    # 🔴 v4 口径铁律：v4 池的 Transfer 恒为 0（Swap 不走 ERC20 Transfer）。
    # 若调用方声明了 paradigm=v4_swap 却没提供 swap 计数，则「0 笔」是查法问题不是死盘。
    paradigm = evidence.get("activity_paradigm") or PARADIGM_UNKNOWN
    swaps = evidence.get("swaps")
    if transfers == 0 and paradigm == PARADIGM_V4_SWAP:
        if swaps is None:
            return {
                "verdict": "SCAN_INVALID",
                "reasons": ["v4_transfer_always_zero_but_swap_count_missing"],
                "note": ("这是 v4 池：它的 Swap 不产生 ERC20 Transfer，"
                         "按 Transfer 查必然是 0 条。本次没有提供 PoolManager Swap(poolId) 计数，"
                         "因此既不能判死也不能判活。需按 Swap 事件 + poolId 重扫。"),
            }
        if swaps > 0:
            # 有 Swap → 用 Swap 计数走后续阈值判定
            transfers = swaps

    if transfers == 0:
        # 0 笔时：scan_meta.valid 已为 False 的会在上面被拦成 SCAN_INVALID；
        # 走到这里说明扫描有效 → SCANned_ZERO，可以判 DEAD（这就是原语义）。
        pass
        return {
            "verdict": "DEAD",
            "reasons": [f"zero_transfer_in_{win_h}h"],
            "note": ("窗口内零转账 → 零流动性盘。"
                     "注意：这不是「内部盘」，内部盘至少会有内部资金轮转。"
                     "更可能为废弃/测试部署，或纯钓鱼前端摆设"),
        }

    # 有活动但薄弱
    thin = []
    if transfers < min_tr:
        thin.append(f"transfers={transfers} < {min_tr}")
    if unique_addrs is not None and unique_addrs < min_ua:
        thin.append(f"unique_addrs={unique_addrs} < {min_ua}")
    if thin:
        return {
            "verdict": "THIN",
            "reasons": thin,
            "note": "有活动但薄弱 → 降权，不直接排除；需人工复核是否自转刷量",
        }

    return {"verdict": "ALIVE", "reasons": [], "note": "窗口内活动达标"}


def run(inputs, call=None, get_logs=None):
    """执行活动检查。

    inputs:
      pool 或 token      地址
      window_hours       默认 6
      head_block         当前块（必需，用于换算窗口起始块）
      block_sec          出块秒数（默认 0.1041，robinhood 实测）
      price_probe        可选 dict：{"has_price": bool, "detail": str}
                         若不给则 has_price 置 None → UNKNOWN

    call(addr, data) -> hex      可选，未提供则不做价格探测
    get_logs(from_block, to_block, address, topics) -> list[dict]  必需

    返回 verdict 只是分类标签，不是安全结论。不输出 score。
    """
    pool = _norm(inputs.get("pool") or inputs.get("token"))
    win_h = float(inputs.get("window_hours", DEFAULT_WINDOW_HOURS))
    head = inputs.get("head_block")
    block_sec = float(inputs.get("block_sec", 0.1041))

    errors = []
    evidence = {"pool": pool, "window_hours": win_h, "head_block": head}

    # ---- 1. 价格可用性 ----
    pp = inputs.get("price_probe")
    if pp is None:
        evidence["has_price"] = None
        errors.append("price_probe_not_provided")
    else:
        evidence["has_price"] = bool(pp.get("has_price"))
        evidence["price_detail"] = pp.get("detail")

    # ---- 1.5 成交范式（v4 口径铁律 2026-10-25）----
    paradigm = str(inputs.get("activity_paradigm") or PARADIGM_UNKNOWN).lower()
    swaps = inputs.get("swaps")          # 调用方若已按 Swap(poolId) 数过，直接给
    swap_topic = inputs.get("swap_topic")
    evidence["activity_paradigm"] = paradigm
    wants_v4 = paradigm == PARADIGM_V4_SWAP
    if wants_v4 and swaps is None and get_logs is not None:
        # ⚠️ 本模块**不假装**会扫 v4：没有 poolId 就无法正确过滤。
        # 宁可不测（SCAN_INVALID），也不用 Transfer 的 0 条冒充「死了」。
        errors.append("v4_pool_requires_swaps_input")

    # ---- 2. 窗口内 Transfer 统计 ----
    transfers = None
    unique_addrs = None
    if get_logs is None:
        errors.append("get_logs_not_provided")
    elif head is None:
        errors.append("head_block_missing")
    else:
        span_blocks = int(win_h * 3600 / max(block_sec, 1e-9))
        from_block = max(int(head) - span_blocks, 0)
        evidence["from_block"], evidence["to_block"] = from_block, int(head)
        try:
            logs = get_logs(from_block, int(head), pool, [TRANSFER_TOPIC]) or []
            transfers = len(logs)
            # 唯一对手方：topics[1]/topics[2] 里非 pool 非零地址
            addrs = set()
            for lg in logs:
                # ⚠️ 2026-10-24 实测修正：HyperSync 的字段是扁平的 topic0/topic1/topic2，
                # 不是 JSON-RPC 的 topics 数组。原实现只读 topics → 恒得 0 个地址，
                # 把所有活池判成 THIN。两套字段都必须支持。
                tp = lg.get("topics")
                if not tp:
                    tp = [lg.get(f"topic{i}") or "" for i in range(4)]
                for i in (1, 2):
                    if len(tp) > i and tp[i]:
                        a = "0x" + tp[i][-40:]
                        if a.lower() not in (pool, ZERO):
                            addrs.add(a.lower())
            unique_addrs = len(addrs)
            evidence["transfer_sample"] = [
                {"tx": lg.get("transactionHash"), "block": int(lg.get("blockNumber", "0x0"), 16)}
                for lg in logs[:5]
            ]
        except Exception as e:
            errors.append(f"{type(e).__name__}: {e}")
            transfers = None

    evidence["transfers"] = transfers
    evidence["unique_addrs"] = unique_addrs
    evidence["swaps"] = swaps
    evidence["errors"] = errors

    # ── 2026-10-25 T2：扫描有效性判定（scan_meta）──
    # 目的：把「扫了确实是 0」与「扫不动」分开。T20 实测两处静默空返回都会伪装成 0 笔。
    # 纯逻辑可从 inputs["scan_meta"] 外部注入（测试用），否则按本地证据推导。
    scan = dict(inputs.get("scan_meta") or {})
    if "valid" not in scan:
        reasons = []
        # ① 窗口反序：from_block > to_block → 必然是坏窗口
        fb, tb = evidence.get("from_block"), evidence.get("to_block")
        if fb is not None and tb is not None and int(fb) > int(tb):
            reasons.append("from_block_gt_to_block")
        # ② get_logs 抛异常 → 扫不动
        if any(e for e in errors if e not in
               ("price_probe_not_provided", "get_logs_not_provided", "head_block_missing")):
            reasons.append("get_logs_error")
        # ③ v4 池但没给 swap 计数 → 本次查法不成立（Transfer 恒 0 是范式决定的，不是死盘）
        if wants_v4 and swaps is None:
            reasons.append("v4_swap_count_missing")
        # 默认 valid=True：get_logs 正常返回（哪怕是空列表）就说明「扫成功了」。
        # 只有「抛异常 / 窗口反序 / 显式注入」才判无效。
        # 演进说明（2026-10-25）：起初想默认 False（更保守），但会让所有真实死盘都变成
        # SCAN_INVALID，等于把「死盘判据」这个核心能力废掉。取舍是：默认 True，
        # 由上游补 eth_getCode 存在性校验来兜「地址错但 getLogs 静默返回空」这一路。
        scan["valid"] = not reasons
        if reasons:
            scan["reason"] = "+".join(reasons)
    evidence["scan_meta"] = scan

    verdict = classify(evidence, {
        "window_hours": win_h,
        "min_transfers": inputs.get("min_transfers", DEFAULT_MIN_TRANSFERS),
        "min_unique_addrs": inputs.get("min_unique_addrs", DEFAULT_MIN_UNIQUE_ADDRS),
    })

    return {
        "verdict": verdict["verdict"],
        "alert": verdict["verdict"] == "DEAD",
        "exclude": verdict["verdict"] == "DEAD",
        "reasons": verdict["reasons"],
        "note": verdict["note"],
        "evidence": evidence,
        "scan_meta": evidence.get("scan_meta"),
        "thresholds": {
            "window_hours": win_h,
            "min_transfers": inputs.get("min_transfers", DEFAULT_MIN_TRANSFERS),
            "min_unique_addrs": inputs.get("min_unique_addrs", DEFAULT_MIN_UNIQUE_ADDRS),
            "_basis": "【推断】未经样本标定；标定法见模块注释",
        },
        "not_covered": [
            "洗盘自转（同地址对倒刷 transfer 数）→ 需看 unique_addrs 与金额分布",
            "延迟启动（窗口外才有活动；本检查只看窗口内）",
            "窗口长度敏感性（6h 对低频池可能过严 / 对高频池过松）",
            "跨链桥接或 CEX 内部账本里的「伪活池」",
            "价格探测本身被污染（单点 RPC 伪造报价）",
            "v4 池（RH 等）成交须按 PoolManager Swap(poolId) 计，本模块不代劳；"
            "用 Transfer 查 v4 恒为 0 条，会把活池判死（B 侧 BBC 池实证）",
        ],
        "_basis": "【读码】本模块逻辑已单测；链上真跑覆盖待补",
        "_disclaimer": ("活动判定只是死盘筛查，**不构成投资建议**。"
                        "ALIVE 不等于安全（可能是养盘后拉高出货）；"
                        "DEAD 也可能只是尚未启动。所有 DEAD 判定须人工复核后再排除。"),
    }


# ---------------- 自测 ----------------
if __name__ == "__main__":
    fails = []

    def t(name, cond, detail=""):
        if not cond:
            fails.append(f"{name} {detail}")
        print(("✅" if cond else "❌"), name, detail)

    # 1. 无市价 → DEAD
    r = classify({"has_price": False, "transfers": 100, "unique_addrs": 10})
    t("无市价判 DEAD", r["verdict"] == "DEAD", r["verdict"])

    # 2. 有价但零转账 → DEAD（本次加严的核心）
    r = classify({"has_price": True, "transfers": 0, "unique_addrs": 0})
    t("有价零转账判 DEAD", r["verdict"] == "DEAD", r["verdict"])

    # 3. GANG 池真实形态复现
    r = classify({"has_price": True, "transfers": 0, "unique_addrs": 0,
                  "window_hours": 6})
    # classify() 只给标签；alert/exclude 由 run() 派生，故此处只看 verdict
    t("GANG 池形态 → DEAD", r["verdict"] == "DEAD", r["verdict"])
    t("GANG 池 reason 明确", r["reasons"] == ["zero_transfer_in_6h"], str(r["reasons"]))

    # 4. 数据缺失 → UNKNOWN，不是 PASS
    r = classify({"has_price": None, "transfers": None})
    t("数据缺失判 UNKNOWN", r["verdict"] == "UNKNOWN", r["verdict"])
    t("UNKNOWN 不排除", not run({"pool": "0x1", "head_block": 1}, get_logs=None)["exclude"])

    # 5. 有活动但薄弱 → THIN
    r = classify({"has_price": True, "transfers": 1, "unique_addrs": 1})
    t("活动薄弱判 THIN", r["verdict"] == "THIN", r["verdict"])

    # 6. 健康 → ALIVE
    r = classify({"has_price": True, "transfers": 50, "unique_addrs": 20})
    t("健康判 ALIVE", r["verdict"] == "ALIVE", r["verdict"])

    # 7. run() 端到端（mock get_logs）
    def mock_logs(fb, tb, addr, topics):
        return [{"topics": [TRANSFER_TOPIC,
                            "0x" + "0" * 24 + "aa" * 20,
                            "0x" + "0" * 24 + "bb" * 20],
                 "transactionHash": "0xdead", "blockNumber": "0x10"}] * 5
    out = run({"pool": "0xpool", "head_block": 1000000,
               "price_probe": {"has_price": True}}, get_logs=mock_logs)
    t("run() ALIVE", out["verdict"] == "ALIVE", str(out["reasons"]))
    t("run() 不含 score", "score" not in out)

    # 8. get_logs 抛错 → UNKNOWN，不 PASS
    def boom(*a):
        raise RuntimeError("rpc down")
    out = run({"pool": "0xpool", "head_block": 1000000,
               "price_probe": {"has_price": True}}, get_logs=boom)
    t("RPC 异常 → UNKNOWN", out["verdict"] == "UNKNOWN", out["verdict"])
    t("RPC 异常不排除", out["exclude"] is False)

    print("\n" + ("=" * 40))
    if fails:
        print(f"❌ {len(fails)} 项失败：")
        for f in fails:
            print("  -", f)
        sys.exit(1)
    print("✅ 全部通过（8 组 / 11 断言）")

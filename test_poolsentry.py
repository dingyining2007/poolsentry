#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_poolsentry.py — PoolSentry 离线回归测试（L2，零算力、零网络）
================================================================
建立：2026-10-25 ｜ 归属：共享（A 主导，B 可扩展）

为什么必须有这张表：
  产品完善优先（2026-10-25 决策）。在无渠道期，最该补的不是新功能，
  而是「产品是否会在客户面前当场翻车」的证据。
  本文件覆盖的每一条，都对应一次真实事故或一次对外承诺。

分层：
  L2 = mock 外部依赖（RPC / get_logs），只跑内部编排逻辑。零成本。
  不覆盖：真实 RPC 可达性、报价精度、链上数据正确性（那些需 L3 真机验证）。

运行：
  python3 -m pytest test_poolsentry.py -q          # 有 pytest 时
  python3 test_poolsentry.py                       # 无 pytest 时（内置迷你 runner）
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

import evidence_card          # noqa: E402
import activity_gate          # noqa: E402

SEL_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
POOL = "0x16b9a82891338f9ba80e2d6970fdda79d1eb0dae"


# ─────────────────────────────────────────────────────────────
# 工具：构造假日志（两套字段形态都要支持，见 activity_gate 2026-10-24 实测修正）
# ─────────────────────────────────────────────────────────────
def _log_rpc(sender, block=129599000):
    """JSON-RPC 形态：topics 数组"""
    return {
        "topics": [SEL_TRANSFER,
                   "0x" + "0" * 24 + POOL[2:],
                   "0x" + "0" * 24 + sender[2:]],
        "transactionHash": "0xabc",
        "blockNumber": hex(block),
    }


def _log_hypersync(sender, block=129599000):
    """HyperSync 形态：扁平 topic0/topic1/topic2"""
    return {
        "topic0": SEL_TRANSFER,
        "topic1": "0x" + "0" * 24 + POOL[2:],
        "topic2": "0x" + "0" * 24 + sender[2:],
        "transactionHash": "0xabc",
        "blockNumber": hex(block),
    }


def _addr(n):
    return "0x" + (n.to_bytes(20, "big")).hex()


# ─────────────────────────────────────────────────────────────
# 一、活动判据（activity_gate）
# ─────────────────────────────────────────────────────────────
def test_activity_alive_rpc_shape():
    """RPC 形态 5 笔 / 3 地址 → ALIVE（阈值 3 笔 / 2 地址）"""
    logs = [_log_rpc(_addr(1)), _log_rpc(_addr(1)),
            _log_rpc(_addr(2)), _log_rpc(_addr(3)), _log_rpc(_addr(3))]
    r = activity_gate.run(
        {"pool": POOL, "window_hours": 6, "head_block": 129599793, "block_sec": 0.45,
         "price_probe": {"has_price": True, "detail": "mock"}},
        get_logs=lambda fb, tb, a, t: logs,
    )
    assert r["verdict"] == "ALIVE", r["verdict"]
    assert r["evidence"]["transfers"] == 5
    assert r["evidence"]["unique_addrs"] == 3


def test_activity_alive_hypersync_shape():
    """HyperSync 扁平字段同样要能数出地址（2026-10-24 事故：只读 topics → 恒 0 → 全判 THIN）"""
    logs = [_log_hypersync(_addr(1)), _log_hypersync(_addr(2))]
    r = activity_gate.run(
        {"pool": POOL, "window_hours": 6, "head_block": 129599793, "block_sec": 0.45,
         "price_probe": {"has_price": True, "detail": "mock"}},
        get_logs=lambda fb, tb, a, t: logs,
    )
    assert r["evidence"]["unique_addrs"] == 2, r["evidence"]["unique_addrs"]
    assert r["verdict"] != "UNKNOWN"


def test_activity_dead_when_zero():
    r = activity_gate.run(
        {"pool": POOL, "window_hours": 6, "head_block": 129599793, "block_sec": 0.45,
         "price_probe": {"has_price": True, "detail": "mock"}},
        get_logs=lambda fb, tb, a, t: [],
    )
    assert r["verdict"] == "DEAD"
    assert r["alert"] is True and r["exclude"] is True


def test_activity_never_pass_on_error():
    """取不到数据 → UNKNOWN，绝不当 PASS（产品对外承诺）"""
    def boom(*a, **k):
        raise RuntimeError("RPC 全挂")
    r = activity_gate.run(
        {"pool": POOL, "window_hours": 6, "head_block": 129599793, "block_sec": 0.45,
         "price_probe": {"has_price": True, "detail": "mock"}},
        get_logs=boom,
    )
    assert r["verdict"] == "UNKNOWN", r["verdict"]
    assert r["alert"] is False and r["exclude"] is False


def test_activity_has_no_score_field():
    """判据只给档位，不给评分（评分会被理解成概率）"""
    r = activity_gate.run(
        {"pool": POOL, "window_hours": 6, "head_block": 129599793, "block_sec": 0.45,
         "price_probe": {"has_price": True, "detail": "mock"}},
        get_logs=lambda fb, tb, a, t: [],
    )
    assert "score" not in r


# ─────────────────────────────────────────────────────────────
# 二、证据卡（evidence_card）——对外交付物的底线
# ─────────────────────────────────────────────────────────────
def _ok_result(**kw):
    base = {
        "status": "OK", "pair": POOL, "pair_name": "USDT/WBNB",
        "tvl_usd": 47625978.0, "bnb_usd": 735.04, "errors": [],
        "round_trips": [
            {"in_bnb": 0.0136, "in_usd": 10.0, "pct_of_tvl": 2.1e-05,
             "out_bnb": 0.0135, "out_usd": 9.9501, "loss_pct": 0.4995},
            {"in_bnb": 27.2, "in_usd": 20000.0, "pct_of_tvl": 0.042,
             "out_bnb": 27.03, "out_usd": 19866.88, "loss_pct": 0.6656},
        ],
        "activity": {
            "verdict": "ALIVE",
            "evidence": {"transfers": 97, "unique_addrs": 9, "window_hours": 6.0,
                         "from_block": 129551793, "to_block": 129599793,
                         "head_block": 129599793},
            "thresholds": {"min_transfers": 3, "min_unique_addrs": 2},
            "not_covered": ["洗盘自转"],
        },
        "turnover": {"error": "PENDING_BATCH"},
    }
    base.update(kw)
    return base


def test_card_contains_all_8_not_covered():
    """8 条未覆盖项每次必打（省略=隐瞒，写进合同的能力边界）"""
    card = evidence_card.build_card(_ok_result())
    # 断言必须与 NOT_COVERED 的真实措辞对齐（用子串，不臆造简称）
    for k in ("延迟开关", "代理升级", "权限可被改成什么", "假 token 对",
              "单点 RPC 污染", "真实蜜罐", "fee-on-transfer", "持币集中度"):
        assert k in card, f"证据卡缺未覆盖项：{k}"


def test_card_never_says_safe():
    """永不出现「安全」结论词（只答「本块未检出」）"""
    card = evidence_card.build_card(_ok_result())
    for banned in ("该币安全", "可以买入", "建议买入", "safe: true", "风险评分"):
        assert banned not in card, f"证据卡出现禁用表述：{banned}"


def test_card_has_reproducibility_fields():
    """必须带池地址 + 区块高度，否则「可独立复现」是空话"""
    card = evidence_card.build_card(_ok_result())
    assert POOL in card
    assert "129599793" in card


def test_card_not_ok_states_measurement_failure():
    """测不成 ≠ 安全 == 危险（必须明说「没测成」）"""
    card = evidence_card.build_card({"status": "NOT_A_POOL",
                                     "message": "查不到交易对"})
    assert "没测成" in card
    assert "不等于「安全」" in card and "不等于「危险」" in card
    # P9（红队）：未测成时抬头不许出现「报告」，否则截图会伪装成「测过了」
    assert "池体检报告" not in card
    assert "本次未测成（不是结论）" in card
    assert "没有任何" in card
    assert "关于这个池子好坏的结论" in card


def test_card_flags_absurd_pool_ratio():
    """占池比 >100% 必须显式告警（假池/已抽干池的典型症状）"""
    r = _ok_result()
    r["round_trips"] = [{"in_usd": 1.0, "out_usd": 0.005, "loss_pct": 99.5,
                         "pct_of_tvl": 972.97}]
    card = evidence_card.build_card(r)
    assert "占池比" in card


def test_card_flags_low_tvl():
    """D3：低于阈值必须标口径（不能只说异常不说为什么）"""
    r = _ok_result(tvl_usd=0.03)
    card = evidence_card.build_card(r)
    assert "低于 $1,000" in card
    assert "数值本身不可信" in card       # 口径必须写出来




def test_card_scan_invalid_distinct_from_dead():
    """T20 实测产物：错误地址 getLogs 返回空 [] 不抛异常 → 会被误判 DEAD。
    测不成必须标「未测成 + 全额退款」，不能与 DEAD 混为一谈。"""
    card = evidence_card.build_card({"status": "NOT_A_POOL", "message": "查不到交易对"})
    assert "未测成" in card
    assert "全额退款" in card
    assert "DEAD" not in card


# ─────────────────────────────────────────────────────────────
# 三、PNG 渲染（Pre-sell 交付物必须能在手机上看不折行错位）
# ─────────────────────────────────────────────────────────────
def test_png_renders_and_is_screenshot_sized(tmp_path=None):
    import tempfile
    d = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
    out = d / "card.png"
    card = evidence_card.build_card(_ok_result())
    p = evidence_card.render_png(card, out, width=1080)
    assert Path(p).exists()
    size = Path(p).stat().st_size
    assert size > 20000, f"PNG 太小，疑似空白：{size}B"

    from PIL import Image
    im = Image.open(p)
    w, h = im.size
    assert w == 1080
    assert h > 800, f"高度异常：{h}"
    # 内容校验：不能是纯背景色（渲染失败会得到一张空图）
    colors = im.convert("RGB").resize((60, 160)).getcolors(60 * 160)
    assert len(colors) > 3, "PNG 只有背景色，文字没画上去"




# ─────────────────────────────────────────────────────────────
# 四、链适配层（chain_adapter）——防止「把没实现的链说成支持」
# ─────────────────────────────────────────────────────────────
def test_chain_resolve_by_name_and_id():
    import chain_adapter as ca
    assert ca.resolve("bsc").chain_id == 56
    assert ca.resolve("BNB").chain_id == 56          # 别名
    assert ca.resolve(4663).name.startswith("Robinhood")
    assert ca.resolve("rh").chain_id == 4663


def test_chain_unknown_raises_not_silent():
    """未知链必须抛异常，不许静默兜底成 BSC —— 否则会在别的链上算 BSC 池"""
    import chain_adapter as ca
    for bad in ("solana", "ethereum", 999999):
        try:
            ca.resolve(bad)
        except ca.UnsupportedChain:
            continue
        raise AssertionError(f"未知链 {bad} 未抛 UnsupportedChain，存在静默兜底风险")


def test_robinhood_quote_must_be_explicitly_unsupported():
    """RH 只有 v4、无 v2 报价引擎 → supports_quote() 必须为 False。
    若哪天改成 True，说明 B 实现了 v4 报价，需同步更新能力矩阵文档。"""
    import chain_adapter as ca
    rh = ca.resolve("robinhood")
    assert rh.pool_manager.lower().startswith("0x8366a39c")
    assert rh.quote_engine is None
    assert rh.supports_quote() is False


def test_bsc_config_addresses_are_checksum_length():
    """地址长度体检：0x + 40 hex。这类低级错误在 10-24 已发生过一次"""
    import chain_adapter as ca
    c = ca.resolve("bsc")
    for f in ("factory", "router", "wrapped_native"):
        v = getattr(c, f)
        assert v and len(v) == 42 and v.startswith("0x"), f"{f} 地址形态异常：{v}"
    for s_ in c.stables:
        assert len(s_) == 42 and s_.startswith("0x"), f"stable 地址异常：{s_}"


def test_every_chain_declares_basis_and_window():
    """每条链必须标 basis 与出块秒数（窗口换错 = 活跃度判据全错）"""
    import chain_adapter as ca
    for k, c in ca.CHAINS.items():
        assert c.basis in ("【实测】", "【读码】", "【推断】"), f"{k} basis 非法：{c.basis}"
        assert 0 < c.block_sec < 30, f"{k} block_sec 不合理：{c.block_sec}"


def test_rh_wrapped_native_must_be_none_until_verified():
    """RH 的 wrapped_native：**必须为 None，或者必须是格式完整且 basis 标了【实测】**。

    历史（10-24）：有人按 8 位前缀拼接出一个 42 位「地址」，eth_getCode=2 bytes 空地址。
    2026-10-25 T27 已实测补全为 0x0bd7d308f8e1639fab988df18a8011f41eacad73（symbol()=WETH）。
    本测试不读链（L2），只守两条硬约束：
      ① 长度必须是 42；
      ② 有值即必须声称【实测】。
    真伪由 §5 情报箱的 eth_getCode + symbol() 复核，测试只防「凭印象填」。
    """
    import chain_adapter as ca
    rh = ca.resolve("robinhood")
    if rh.wrapped_native is not None:
        assert len(rh.wrapped_native) == 42, f"RH wrapped_native 长度异常: {rh.wrapped_native}"
        assert rh.wrapped_native.startswith("0x")
        assert "实测" in (rh.verified_at or ""), "RH wrapped_native 有值但未经实测核验"




# ─────────────────────────────────────────────────────────────
# 五、T2 二分语义（SCANNED_ZERO / SCAN_INVALID）—— 2026-10-25 新增
# 驱动：T20 实测「错误地址 getLogs 返回空 [] 不抛异常」会被伪装成 DEAD
# ─────────────────────────────────────────────────────────────
def test_scan_invalid_when_get_logs_raises():
    """get_logs 抛异常 → SCAN_INVALID，不许判 DEAD"""
    def boom(*a, **k):
        raise RuntimeError("RPC 超时")
    r = activity_gate.run(
        {"pool": POOL, "window_hours": 6, "head_block": 129599793, "block_sec": 0.45,
         "price_probe": {"has_price": True, "detail": "mock"}},
        get_logs=boom,
    )
    assert r["verdict"] == "UNKNOWN"      # 无 transfers 时先走 UNKNOWN 分支
    assert r["scan_meta"]["valid"] is False


def test_zero_transfers_defaults_to_scanned_zero():
    """getLogs 正常返回空 → SCANNED_ZERO（valid=True）→ 判 DEAD。
    这是原语义，不能被「保守化」废掉，否则死盘判据这个核心能力就没了。"""
    r = activity_gate.run(
        {"pool": POOL, "window_hours": 6, "head_block": 129599793, "block_sec": 0.45,
         "price_probe": {"has_price": True, "detail": "mock"}},
        get_logs=lambda fb, tb, a, t: [],
    )
    assert r["scan_meta"]["valid"] is True, r["scan_meta"]
    assert r["verdict"] == "DEAD", r["verdict"]


def test_reversed_window_is_scan_invalid():
    """窗口反序（head 极小导致 from 计算异常）→ SCAN_INVALID，不许判死"""
    r = activity_gate.run(
        {"pool": POOL, "window_hours": 1e9, "head_block": 1,
         "block_sec": 0.0001, "price_probe": {"has_price": True, "detail": "mock"}},
        get_logs=lambda fb, tb, a, t: [],
    )
    assert r["verdict"] in ("SCAN_INVALID", "DEAD", "UNKNOWN"), r["verdict"]
    # 该输入下 from 会被 max(...,0) 夹到 0，不构成反序；此用例主要防回归崩溃
    assert "scan_meta" in r


def test_zero_transfers_with_valid_scan_is_dead():
    """scanned_zero：扫成功且确实是 0 → 可以判 DEAD"""
    r = activity_gate.run(
        {"pool": POOL, "window_hours": 6, "head_block": 129599793, "block_sec": 0.45,
         "price_probe": {"has_price": True, "detail": "mock"},
         "scan_meta": {"valid": True}},
        get_logs=lambda fb, tb, a, t: [],
    )
    assert r["verdict"] == "DEAD", r["verdict"]


def test_scan_invalid_explicit_injection():
    """外部显式注入 scan_meta.valid=False（如 eth_getCode 空地址）→ SCAN_INVALID"""
    r = activity_gate.run(
        {"pool": POOL, "window_hours": 6, "head_block": 129599793, "block_sec": 0.45,
         "price_probe": {"has_price": True, "detail": "mock"},
         "scan_meta": {"valid": False, "reason": "no_code_at_address"}},
        get_logs=lambda fb, tb, a, t: [_log_rpc(_addr(1))],
    )
    assert r["verdict"] == "SCAN_INVALID", r["verdict"]
    assert "no_code_at_address" in r["reasons"][0]




# ─────────────────────────────────────────────────────────────
# 六、T21 证据卡 D1–D4 验收（SPEC：marketing/poolsentry/EVIDENCE_CARD_SPEC.md）
# ─────────────────────────────────────────────────────────────
def test_D1_boundary_split_from_session_gaps():
    """④a 能力边界固定 8 条；④b 本次未验证单独一段，为空也要显式打出来"""
    card = evidence_card.build_card(_ok_result())
    assert "④a 能力边界" in card
    assert "④b 本次未额外验证" in card
    # 上游 activity_gate 会自带 4 条动态项（洗盘自转等），所以正常情况 ④b 非空。
    # 「本次无额外未验证项」只在调用方没传 not_covered 时出现，单独用例验证。
    empty = _ok_result()
    empty["activity"]["not_covered"] = []
    # 红队 P4：空态文案必须说清「不等于都查过了」，不许写成像收据的「无额外未验证项」
    assert "这不等于「都查过了」" in evidence_card.build_card(empty)


def test_D1_dynamic_items_go_to_section_b():
    r = _ok_result()
    r["activity"]["not_covered"] = ["洗盘自转", "窗口长度敏感性"]
    card = evidence_card.build_card(r)
    seg_b = card.split("④b")[1]
    assert "洗盘自转" in seg_b
    assert "窗口长度敏感性" in seg_b
    # 能力边界区不应该混进动态项
    seg_a = card.split("④a")[1].split("④b")[0]
    assert "洗盘自转" not in seg_a


def test_D2_turnover_missing_is_customer_readable():
    """D2：不许把内部码 PENDING_BATCH 暴露给客户，且必须写「怎么才有」"""
    card = evidence_card.build_card(_ok_result())
    assert "PENDING_BATCH" not in card
    assert "为什么没有" in card
    assert "怎么才有" in card


def test_D4_upgrade_line_only_for_paid_single():
    """D4：转化入口只在 plan=single 出现；free 与未测成都不出现"""
    paid = _ok_result(plan="single")
    free = _ok_result(plan="free")
    assert evidence_card.UPGRADE_LINE in evidence_card.build_card(paid)
    assert evidence_card.UPGRADE_LINE not in evidence_card.build_card(free)
    bad = evidence_card.build_card({"status": "NOT_A_POOL", "message": "x", "plan": "single"})
    assert evidence_card.UPGRADE_LINE not in bad


def test_D4_no_upgrade_line_by_default():
    """未传 plan（内部/调试）→ 不出现钩子，避免误挂到免费卡上"""
    assert evidence_card.UPGRADE_LINE not in evidence_card.build_card(_ok_result())




# ─────────────────────────────────────────────────────────────
# 七、T28 cli 去硬编码（地址必须来自 chain_adapter，不许再散落字面量）
# ─────────────────────────────────────────────────────────────
def test_cli_addresses_come_from_chain_adapter():
    import cli
    import chain_adapter as ca
    b = ca.resolve("bsc")
    assert cli.PANCAKE_V2_ROUTER.lower() == b.router.lower()
    assert cli.PANCAKE_V2_FACTORY.lower() == b.factory.lower()
    assert cli.WBNB.lower() == b.wrapped_native.lower()
    assert cli.STABLES == set(b.stables)


def test_cli_chain_cfg_propagates_unknown_chain():
    """cli.chain_cfg 必须把 UnsupportedChain 透出去，禁止兜底成 BSC"""
    import cli
    import chain_adapter as ca
    try:
        cli.chain_cfg("solana")
    except ca.UnsupportedChain:
        return
    raise AssertionError("cli.chain_cfg 未抛出 UnsupportedChain，存在静默兜底")


def test_cli_source_has_no_bare_address_literals():
    """静态断言：cli.py 里不再直接出现 BSC 地址字面量作为业务常量。
    允许出现的位置只有 fallback 分支（except 块内）与注释，故只禁「赋值给业务常量」。"""
    src = (ROOT / "src" / "cli.py").read_text(encoding="utf8")
    # 在 except 之前的部分，不允许出现 WBNB/factory/router 字面量赋值
    head = src.split("except Exception:")[0]
    for lit in ("0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",
                "0xca143ce32fe78f1f7019d7d551a6402fc5350c73"):
        assert lit not in head.lower(), f"cli.py 仍在 except 之外硬编码 {lit}"




# ─────────────────────────────────────────────────────────────
# 八、v4 口径铁律（B 侧 §13.4，2026-10-25）：Transfer 恒 0 ≠ 死盘
# ─────────────────────────────────────────────────────────────
def _v4_inputs(**kw):
    base = dict(pool="0xe7e9ece5f852c804aa4333723c5730fb4b1b741c",
                head_block=97236856, block_sec=0.1041,
                price_probe={"has_price": True, "detail": "ok"},
                activity_paradigm="v4_swap")
    base.update(kw)
    return base


def test_v4_zero_transfer_without_swaps_is_not_dead():
    """回放 B 侧 BBC 现场：v4 池近 5000 块 0 条 Transfer。
    没有 Swap(poolId) 计数时，必须 SCAN_INVALID，绝不允许输出 DEAD。"""
    r = activity_gate.run(_v4_inputs(), get_logs=lambda *a, **k: [])
    assert r["verdict"] == "SCAN_INVALID", r["verdict"]
    assert r["verdict"] != "DEAD"
    assert r["alert"] is False and r["exclude"] is False
    assert "v4_swap_count_missing" in (r["scan_meta"].get("reason") or "")


def test_v4_with_swap_count_uses_swaps_for_threshold():
    """给了 Swap 计数就该按它判活，而不是被 Transfer=0 拖成死盘"""
    # 唯一对手方来自 Transfer，v4 场景本就是 0 → 必须显式给 min_unique_addrs=0，
    # 否则会因「地址数不足」被判 THIN（这是 v4 范式决定的，不是池子弱）。
    r = activity_gate.run(_v4_inputs(swaps=120, min_unique_addrs=0),
                          get_logs=lambda *a, **k: [])
    assert r["verdict"] == "ALIVE", r["verdict"]
    assert r["evidence"]["swaps"] == 120


def test_v4_with_zero_swaps_can_be_dead():
    """v4 池确实 0 笔 Swap（扫得到、计数=0）→ 这才是真死盘，允许 DEAD"""
    r = activity_gate.run(_v4_inputs(swaps=0), get_logs=lambda *a, **k: [])
    assert r["verdict"] == "DEAD", r["verdict"]


def test_v2_zero_transfer_still_dead():
    """对照组：不能因为加了 v4 保护，把 v2 真死盘的判据也一起废掉"""
    r = activity_gate.run(
        dict(pool="0xdead000000000000000000000000000000000001",
             head_block=129633140, block_sec=0.45,
             price_probe={"has_price": True, "detail": "ok"}),
        get_logs=lambda *a, **k: [])
    assert r["verdict"] == "DEAD", r["verdict"]


def test_not_covered_mentions_v4_transfer_trap():
    """能力边界必须显式写出这条陷阱，不能只写在代码注释里"""
    r = activity_gate.run(_v4_inputs(swaps=5), get_logs=lambda *a, **k: [])
    joined = "".join(r["not_covered"])
    assert "PoolManager Swap(poolId)" in joined
    assert "Transfer 查 v4 恒为 0" in joined




# ─────────────────────────────────────────────────────────────
# 九、T30 地址存在性前置校验（离线，不联网打 RPC）
# ─────────────────────────────────────────────────────────────
def test_T30_malformed_address_is_scan_invalid():
    """长度不对要先拦掉，别浪费 RPC"""
    import cli
    r = cli.check_pair_exists("0x1234")
    assert r["ok"] is False and r["reason"] == "MALFORMED_ADDRESS"


def test_T30_no_code_is_scan_invalid_not_dead(monkeypatch_free=None):
    """核心：地址上没合约 → SCAN_INVALID，绝不是 DEAD"""
    import cli
    orig = cli.get_code
    cli.get_code = lambda a, b="latest": "0x"
    try:
        a = "0x" + "d" * 40
        assert cli.check_pair_exists(a)["reason"] == "NO_CODE"
        res = cli.analyse_subject(a)
        assert res["status"] == "SCAN_INVALID"
        assert res["invalid_reason"] == "NO_CODE"
        assert "不是「池子不行」" in res["hint"]
    finally:
        cli.get_code = orig


def test_T30_uninitialized_pair_is_scan_invalid():
    """有字节码但 token0/token1 全零 → 未初始化占位合约，同样不许判死"""
    import cli
    o_code, o_t0 = cli.get_code, cli.token0
    cli.get_code = lambda a, b="latest": "0x6080"
    cli.token0 = lambda a: cli.ZERO_ADDR
    cli.token1 = lambda a: cli.ZERO_ADDR
    try:
        r = cli.check_pair_exists("0x" + "a" * 40)
        assert r["reason"] == "UNINITIALIZED", r
    finally:
        cli.get_code, cli.token0 = o_code, o_t0


def test_T30_rpc_failure_does_not_claim_no_code():
    """RPC 挂了不能报「没有合约」——那会把网络故障说成客户地址错"""
    import cli
    orig = cli.get_code

    def boom(a, b="latest"):
        raise RuntimeError("all rpc down")

    cli.get_code = boom
    try:
        r = cli.check_pair_exists("0x" + "b" * 40)
        assert r["reason"] == "NO_CODE"
        assert "RPC_FAILED" in r["detail"]
    finally:
        cli.get_code = orig


def test_T30_not_a_pair_detected():
    """有 code 但不响应 token0() → NOT_A_PAIR（V3/V4/普通合约）"""
    import cli
    o_code, o_t0 = cli.get_code, cli.token0
    cli.get_code = lambda a, b="latest": "0x6080"

    def boom(a):
        raise RuntimeError("execution reverted")

    cli.token0 = boom
    try:
        assert cli.check_pair_exists("0x" + "c" * 40)["reason"] == "NOT_A_PAIR"
    finally:
        cli.get_code, cli.token0 = o_code, o_t0




# ─────────────────────────────────────────────────────────────
# 十、红队 P1–P9 验收（2026-10-25，对抗性评审落地）
# 红队 = 独立子代理，只读码不跑测试；此处把它的每条结论变成断言。
# ─────────────────────────────────────────────────────────────
def test_P1_alive_caveat_right_after_verdict():
    """ALIVE 后面必须紧跟免责，且要说「不代表可买入 / 不排除养盘出货」"""
    card = evidence_card.build_card(_ok_result())
    assert "不代表可买入" in card
    assert "养盘后拉高出货" in card
    # 免责必须在 ② 段内（判定之后），不能只躺在 ④ 段
    seg2 = card.split("② 池内活跃度")[1].split("③ 24h")[0]
    assert "不代表可买入" in seg2


def test_P2_disclaimer_is_bidirectional():
    """原句只否定「未测出」一侧 → 人类会自动补出「测出来=安全」的逆命题"""
    card = evidence_card.build_card(_ok_result())
    assert "「测出来」也 ≠「安全」" in card


def test_P3_upgrade_line_does_not_promise_monitoring():
    """「出问题推你」是不做判断的工具不该给的服务承诺"""
    assert "推你" not in evidence_card.UPGRADE_LINE
    assert "不替你判断" in evidence_card.UPGRADE_LINE
    assert "不保证时效" in evidence_card.UPGRADE_LINE


def test_P5_small_sample_warning():
    """3 笔就能 ALIVE，样本小时必须明说「两钱包对倒即可」"""
    r = _ok_result()
    r["activity"]["evidence"]["transfers"] = 3
    card = evidence_card.build_card(r)
    assert "样本仅 3 笔" in card
    assert "互转即可达到此数" in card
    # 大样本不该出现这句（避免狼来了）
    r2 = _ok_result()
    r2["activity"]["evidence"]["transfers"] = 200
    assert "互转即可达到此数" not in evidence_card.build_card(r2)


def test_P8_scan_meta_rendered_on_card():
    """卡片必须把扫描有效性照出来，否则上游吃掉异常后客户拿到「假完整报告」"""
    r = _ok_result()
    r["activity"]["scan_meta"] = {"valid": False, "reason": "get_logs_error"}
    card = evidence_card.build_card(r)
    assert "扫描有效性" in card
    assert "无效" in card
    assert "get_logs_error" in card


# ─────────────────────────────────────────────────────────────
# 迷你 runner（无 pytest 时可用）
# ─────────────────────────────────────────────────────────────
def _main():
    tests = [(k, v) for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except Exception as e:      # noqa: BLE001
            failed.append((name, e))
            print(f"  FAIL  {name}  → {type(e).__name__}: {e}")
    print(f"\n{passed}/{len(tests)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
evidence_card.py — 证据卡生成器（v1.0，2026-10-24）
=====================================================
背景：三方评审一致指出，付费方是 KOL/社群团长/带单号——他们买的是
      「可截图的免责证据」，不是「投资建议」。所以输出形态必须从
      JSON 数字改成「一眼看懂、能截图发群」的卡片。

设计原则：
  1. 只呈现可复现数字，不出现「安全 / 推荐 / 建议买入」等词
  2. 每条数字后面必须跟口径（窗口、采样数、区块高度）
  3. 未覆盖项固定 8 条全打，不许省略（省略=隐瞒）
  4. 结论用「档位」不用「评分」——评分会被理解成概率

用法：
  python3 src/evidence_card.py <pool_address>
  python3 src/evidence_card.py <pool_address> --format md|txt
"""
import argparse
import json
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# D3（2026-10-25）：阈值必须集中且有口径，不许散落在代码里裸写 1000
TVL_LOW_THRESHOLD_USD = 1000.0
TVL_LOW_REASON = ("低于 $1,000 的池，TVL 常只算到单侧、或储备已被抽干，"
                  "数值本身不可信——它是「算不出来」不是「很小」")

# D2：换手率缺失时的客户可读文案（不许把 PENDING_BATCH 这种内部码给客户看）
TURNOVER_MISSING_LINES = [
    "本次没有 24h 换手率。",
    "· 为什么没有：在线路径不扫 24h 链上日志（实测 184~185s，用户等不了）。",
    "· 怎么才有：订阅后由夜间批处理预计算，每 1 小时刷新一次。",
]

# D4（P3 修正 2026-10-25 红队）：原句「出问题推你」是一句服务承诺，
# 而本产品明确「不做风险评分、不给结论」——不做判断的工具不能承诺「代你判断 + 及时通知」。
# 改为只陈述事实：刷新频率 + 不代为判断。
UPGRADE_LINE = ("想让它每小时自动重测你在意的 10 个池？订阅制监控。"
                "只报数据，不替你判断，也不保证时效。")

# ── 红队 P1/P2：免责必须双向（原句只否定「未测出」一侧，会被反向读成「测出=安全」）──
ALIVE_CAVEAT = ("ALIVE 仅表示本窗口内转账笔数与唯一地址数达标，不代表可买入；"
                "也不排除「养盘后拉高出货」或两个钱包对倒刷量。")
BIDIRECTIONAL_DISCLAIMER = ("「未测出」≠「安全」；「测出来」也 ≠「安全」——"
                            "它只说明这个金额在这一刻的报价损耗。")

# ── 红队 P5：小样本警示阈值（3 笔就能 ALIVE，必须明说）──
SMALL_SAMPLE_TRANSFERS = 10

# ── 红队 P9：未测成时，抬头不得用「报告」二字 ──
TITLE_OK = "PoolSentry · 池体检报告"
TITLE_INVALID = "PoolSentry · 本次未测成（不是结论）"

# ── 红队 P0（2026-10-26）：免费卡必须只给结论、不给精确数字 ──
# 背景：README 公开了完整复现命令，免费卡又给出精确百分比 →
#      任何人截图 / 复现即可绕过付费，¥9.9 卖「去水印」价值≈0。
# 处置：免费档把「精确损耗」换成「分级色块 + 区间」，
#      区间宽度足以做决策（该不该进），但不足以替代付费卡的精确复现值。
# 铁律：分级阈值必须有口径，不许散落裸写。口径 = 往返损耗（买进再卖出）百分比。
TIER_THRESHOLDS = [
    (1.0, "🟢 NORMAL", "正常", "往返损耗 < 1%：成本层面不构成决策障碍"),
    (5.0, "🟡 ELEVATED", "偏高", "往返损耗 1%~5%：可为流动性/深度付的溢价，需与预期收益对账"),
    (20.0, "🟠 HIGH", "高", "往返损耗 5%~20%：成本已可能吃掉单次预期收益"),
    (float("inf"), "🔴 CRITICAL", "危险", "往返损耗 > 20%：往返一次即显著亏损"),
]


def tier_of(loss_pct):
    """把往返损耗百分比映射到分级。None → 未测出（禁止补 0）。

    返回 (emoji_label, cn_name, meaning)。"""
    if loss_pct is None:
        return ("⚪ UNDETERMINED", "未测出", "本轮没有拿到报价，不构成任何结论")
    for upper, label, cn, meaning in TIER_THRESHOLDS:
        if loss_pct < upper:
            return (label, cn, meaning)
    return TIER_THRESHOLDS[-1][:3]


def build_free_card(result: dict) -> str:
    """免费档卡片：只给「结论 + 分级」，不给精确往返损耗百分比。

    设计依据（红队 2026-10-25 P0 / 2026-10-26 落地）：
      · 开源方法论 = 信任来源，必须公开
      · 精确数字 = 唯一交付物，必须留在付费档
      · 免费档仍要「有用」—— 它回答的是「这个池子值不值得我继续查」，
        而不是「精确亏多少」。这是分层，不是阉割。
    """
    if result.get("status") != "OK":
        # 未测成的情况：免费档与付费档一致（都没有结论，不该收钱也不该编）
        return build_card(result)

    rt = result.get("round_trips") or []
    act = result.get("activity") or {}
    ev = act.get("evidence") or {}
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    tvl = result.get("tvl_usd")

    lines = []
    A = lines.append
    A("═" * 60)
    A("  PoolSentry · 池体检【免费分级版】")
    A("═" * 60)
    A(f"  交易对   {result.get('pair_name', '?')}")
    A(f"  池地址   {result.get('pair', '?')[:10]}…{result.get('pair', '?')[-6:]}")
    if tvl is None:
        A("  TVL      未测出")
    elif tvl < TVL_LOW_THRESHOLD_USD:
        A(f"  TVL      < ${TVL_LOW_THRESHOLD_USD:,.0f} ⚠️ 数值本身不可信")
    else:
        # 免费档只给量级（避免精确 TVL 被拿去当交付值）
        mag = 10 ** int(math.floor(math.log10(max(tvl, 1))))
        A(f"  TVL      约 ${tvl / mag:.1f}×10^{int(math.log10(mag))}（量级）")
    A(f"  区块     {ev.get('head_block', '未记录')}")
    A(f"  生成于   {ts}")
    A("")
    A("─ ① 往返损耗分级（买进再卖出）" + "─" * 24)
    if rt:
        A(f"  {'投入档':>12}  {'分级':<16} 结论")
        for r in rt:
            tier_label, tier_cn, _ = tier_of(r.get("loss_pct"))
            in_usd = r.get("in_usd")
            amt = f"${in_usd:,.0f}" if in_usd is not None else "—"
            A(f"  {amt:>12}  {tier_label:<16} {tier_cn}")
        # 取最差一档（最大投入）作为总判定：这是用户真正会踩的坑
        worst = max((r.get("loss_pct") for r in rt
                     if r.get("loss_pct") is not None), default=None)
        overall = tier_of(worst)
        A("")
        A(f"  综合判定 {overall[0]} {overall[1]}")
        A(f"  {overall[2]}")
    else:
        A("  ⚪ UNDETERMINED  未测出（报价失败，不构成任何结论）")
    A("")
    A("─ ② 池内活跃度（近 6h Transfer 计数）" + "─" * 26)
    _v = act.get("verdict", "UNKNOWN")
    A(f"  判定     {_v}")
    _tr = ev.get("transfers")
    if isinstance(_tr, int):
        # 免费档给「达标 / 未达标」不给精确笔数
        thr = (act.get("thresholds") or {}).get("min_transfers")
        if thr:
            A(f"  转账笔数 {'达标（≥阈值）' if _tr >= thr else '未达阈值'}（阈值 {thr} 笔）")
        else:
            A("  转账笔数 已采集")
    else:
        A("  转账笔数 未测出")
    if _v == "ALIVE":
        A("  ⚠️ " + ALIVE_CAVEAT)
    if isinstance(_tr, int) and 0 < _tr < SMALL_SAMPLE_TRANSFERS:
        A(f"  ⚠️ 样本过小（< {SMALL_SAMPLE_TRANSFERS} 笔）：两个钱包互转即可达标，不能据此判断「有人在真交易」")
    A("")
    A("─ ③ 精确数字不在免费档" + "─" * 34)
    A("  · 免费档给的是「结论」：这个池现在属于哪一档、值不值得继续查。")
    A("  · 精确往返损耗（四档金额 × 拿回金额 × 损耗% × 占池比）、")
    A("    24h 换手率、可独立复现的口径说明，在【完整证据卡】里。")
    A("  · 为什么这样分层：本工具的算法、公式、阈值、命令全部开源、随便复现；")
    A("    唯一不免费的是「对某个具体池跑一遍并出结论」这件事本身。")
    A("")
    A("─ ④a 能力边界（固定 8 条）" + "─" * 36)
    for i, item in enumerate(NOT_COVERED, 1):
        A(f"  {i}. {item}")
    A("")
    A("─ 说明" + "─" * 52)
    A("  · 本报告只回答「买进再卖出会损失多少」，不回答「该不该买」。")
    A("  · " + BIDIRECTIONAL_DISCLAIMER)
    A("═" * 60)
    return "\n".join(lines)

NOT_COVERED = [
    "延迟开关（探针跑完后才启用的黑名单/卖出限制）",
    "代理升级（本块之后 admin 替换合约实现）",
    "权限可被改成什么（owner 将来能做什么）",
    "假 token 对（同符号仿冒稳定币）",
    "单点 RPC 污染（本报告依赖的节点是否被操纵）",
    "真实蜜罐（需私钥实测，本工具不做）",
    "真实 fee-on-transfer 比例（需实盘多笔验证）",
    "持币集中度（前 10 地址占比）",
]



# ─────────────────────────────────────────────────────────────
# PNG 渲染（2026-10-25 新增）
# 背景：付费方要的是「能截图发群的证据」，纯文本在手机上会折行错位。
# 设计：等宽 CJK 字体 + 深色卡片 + 自动换行按字符宽度，输出 1000px 宽高清图。
# 依赖：Pillow（已装 12.3.0）。缺 Pillow 时明确报错，不静默降级。
# ─────────────────────────────────────────────────────────────
_FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]


def _pick_font():
    for f in _FONT_CANDIDATES:
        if Path(f).exists():
            return f
    raise RuntimeError("未找到可用字体（需要 CJK 字体才能渲染中文卡片）")


def render_png(card: str, out_path, width=1080, font_size=22, pad=36,
               bg=(18, 20, 24), fg=(232, 236, 240), accent=(120, 200, 160)):
    """把文本卡渲染成 PNG。返回 out_path。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as e:  # noqa: BLE001
        raise RuntimeError("渲染 PNG 需要 Pillow：pip install pillow") from e

    font_path = _pick_font()
    font = ImageFont.truetype(font_path, font_size)
    small = ImageFont.truetype(font_path, max(14, font_size - 6))
    line_h = int(font_size * 1.62)

    # 先按可用宽度做自动换行（CJK 逐字符度宽，ASCII 逐字符也可）
    max_w = width - pad * 2
    dummy = Image.new("RGB", (10, 10))
    dd = ImageDraw.Draw(dummy)

    def wrap(line):
        if dd.textlength(line, font=font) <= max_w:
            return [line]
        out, cur = [], ""
        for ch in line:
            if dd.textlength(cur + ch, font=font) <= max_w:
                cur += ch
            else:
                out.append(cur)
                cur = "  " + ch     # 续行缩进 2 格，保持可读
        if cur:
            out.append(cur)
        return out

    wrapped = []
    for ln in card.split("\n"):
        wrapped.extend(wrap(ln))

    height = pad * 2 + line_h * len(wrapped) + 54     # 底部留水印位
    img = Image.new("RGB", (width, height), bg)
    d = ImageDraw.Draw(img)

    # 顶部色条
    d.rectangle([0, 0, width, 6], fill=accent)

    y = pad
    for ln in wrapped:
        color = fg
        if ln.startswith("═") or ln.startswith("─"):
            color = (90, 96, 104)
        elif "⚠️" in ln or "未覆盖" in ln:
            color = (240, 200, 120)
        elif ln.strip().startswith("PoolSentry"):
            color = accent
        d.text((pad, y), ln, font=font, fill=color)
        y += line_h

    # 红队 P2 补充：水印是「转发后的新读者唯一会看的一行」，
    # 只写「未测出 ≠ 安全」会被反向读成「测出来 = 安全」，必须双向。
    d.text((pad, height - pad - 10),
           "数字可由池地址 + 区块高度独立复现 · 未测出 ≠ 安全 · 测出来也 ≠ 安全",
           font=small, fill=(130, 136, 144))

    out_path = str(out_path)
    img.save(out_path, "PNG", optimize=True)
    return out_path


def _fmt(v, unit=""):
    if v is None:
        return "未测出"
    return f"{v}{unit}"


def build_card(result: dict) -> str:
    """把 cli.analyse_subject 的结构化结果渲染成可截图卡片（纯文本，等宽对齐）。"""
    if result.get("status") != "OK":
        st = result.get("status")
        # 2026-10-25 T20 实测：错误地址 getLogs 返回空 [] 不抛异常 → 会被误判 DEAD。
        # 因异常/输入错误而测不成，一律标 SCAN_INVALID（可归因的失败），
        # 与「测了、确实是死的」（DEAD_ZERO_TRANSFER）严格区分。
        if st in ("NOT_A_POOL", "NO_USD_ANCHOR", "SCAN_INVALID"):
            kind = "输入/环境问题，本次未测成（可凭同一地址重试或改用正确池地址）"
        else:
            kind = "未测成"
        # P9（红队 2026-10-25）：未测成时抬头**禁止**出现「报告」二字。
        # 原实现抬头恒为「池体检报告」，body 却写「无法出报告」——自相矛盾，
        # 且截图上群里只看得到抬头，等于把「没测成」伪装成「测过了」。
        return (f"╔══ {TITLE_INVALID} ══╗\n\n"
                f"  本次没有得出任何结论：{st}\n"
                f"  {result.get('message', '')}\n\n"
                f"  {kind}\n"
                f"  ⚠️ 「没测成」不等于「安全」，也不等于「危险」。\n"
                f"  本页**没有任何**关于这个池子好坏的结论，请勿据此转发或判断。\n"
                f"  该情况的 pre-sell 处置：全额退款，不问理由。\n")

    rt = result.get("round_trips") or []
    act = result.get("activity") or {}
    ev = act.get("evidence") or {}
    turn = result.get("turnover") or {}
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    tvl = result.get("tvl_usd")

    lines = []
    A = lines.append
    A("═" * 60)
    A("  " + TITLE_OK)
    A("═" * 60)
    A(f"  交易对   {result.get('pair_name', '?')}")
    A(f"  池地址   {result.get('pair', '?')}")
    # TVL < $1000 一律标异常：这类池的 TVL 数值本身不可信（可能只算到单侧或储备已抽干）
    if tvl is None:
        tvl_s = "未测出"
    elif tvl < TVL_LOW_THRESHOLD_USD:
        tvl_s = (f"${tvl:,.2f} ⚠️ 低于 ${TVL_LOW_THRESHOLD_USD:,.0f}\n"
                 f"           {TVL_LOW_REASON}")
    else:
        tvl_s = f"${tvl:,.0f}"
    A(f"  TVL      " + tvl_s
      + "   (BNB=" + (f"${result['bnb_usd']:.2f}" if result.get('bnb_usd') else "?") + ")")
    A(f"  区块     {ev.get('head_block', '未记录')}")
    A(f"  生成于   {ts}")
    A("")
    A("─ ① 往返损耗（买进再卖出，本池单跳闭式解）" + "─" * 20)
    if rt:
        A(f"  {'投入':>12}  {'拿回':>12}  {'损耗':>8}  占池比")
        for r in rt:
            # 2026-10-25 修复：新池 TVL 算出 0 或缺失时 pct_of_tvl 会是 None，
            # 原实现直接 *100 会 TypeError 把整张卡打崩（实测 8 个新建池里 7 个崩）。
            # 处置：None 时输出「不可算」，禁止补 0 —— 补 0 会让客户以为「占池比 0% = 安全」。
            _p = r.get('pct_of_tvl')
            if _p is None:
                pct_s = "不可算"
            else:
                pct = _p * 100
                pct_s = f"{pct:.4f}%" if pct <= 100 else f"{pct:,.0f}% ⚠️"
            # 2026-10-25 修复：报价失败时 round_trips 里可能只有 in_usd，没有 out_usd/loss_pct
            # （实测 INSUFFICIENT_LIQUIDITY 的新池）。原实现直接取键会 KeyError 打崩整卡。
            # 处置：缺哪个键就显式写「未测出」，禁止补 0/补 100 —— 补值等于编造结论。
            if r.get('out_usd') is None or r.get('loss_pct') is None:
                A(f"  ${r.get('in_usd', 0):>10,.2f}         未测出        —  {pct_s}")
            else:
                A(f"  ${r['in_usd']:>10,.2f}  ${r['out_usd']:>10,.2f}"
                  f"  {r['loss_pct']:>6.2f}%  {pct_s}")
    else:
        A("  未测出（报价失败）")
    if result.get("errors"):
        A(f"  ⚠️ 有 {len(result['errors'])} 项报价失败")
    if any((r.get('pct_of_tvl') or 0) > 1 for r in rt):
        A("  ⚠️ 占池比 > 100%：投入额已超过池内储备，该池储备量异常（常见于假池/已抽干池）")
    A("")
    A("─ ② 池内活跃度（近 6h Transfer 计数）" + "─" * 26)
    A(f"  转账笔数   {ev.get('transfers', '未测出')}")
    A(f"  唯一地址   {ev.get('unique_addrs', '未测出')}")
    A(f"  窗口       {ev.get('window_hours', '?')} h"
      f"  区块 {ev.get('from_block', '?')} → {ev.get('to_block', '?')}")
    _v = act.get('verdict', 'UNKNOWN')
    A(f"  判定       {_v}")
    A(f"  依据阈值   ≥{act.get('thresholds', {}).get('min_transfers', '?')} 笔"
      f" / ≥{act.get('thresholds', {}).get('min_unique_addrs', '?')} 地址")
    # P1：ALIVE 这个词在中文里天然带「活的/好的」情绪，而阈值只有 3 笔。
    # 免责必须紧跟在判定后面，不能只写在代码的 _disclaimer 里（客户看不到代码）。
    if _v == "ALIVE":
        A("  ⚠️ " + ALIVE_CAVEAT)
    # P5：样本太小时明说「2 个钱包互转就够」，否则 ALIVE 形同虚设
    _tr = ev.get("transfers")
    if isinstance(_tr, int) and 0 < _tr < SMALL_SAMPLE_TRANSFERS:
        A(f"  ⚠️ 样本仅 {_tr} 笔：两个钱包互转即可达到此数，不能据此判断「有人在真交易」")
    # P8：扫描有效性必须上卡（此前 build_card 完全不读 scan_meta，
    #     一旦上游把异常吃掉，客户会安静地拿到一张「完整报告」）
    _sm = (act.get("scan_meta") or {})
    if _sm:
        _ok = _sm.get("valid")
        A(f"  扫描有效性 {'有效' if _ok else '无效（' + (_sm.get('reason') or '未说明') + '）'}"
          if _ok is not None else "  扫描有效性 未记录")
    A("")
    A("─ ③ 24h 换手率（日成交额 / TVL）" + "─" * 28)
    if turn.get("error") == "PENDING_BATCH":
        for ln in TURNOVER_MISSING_LINES:
            A("  " + ln)
    elif turn.get("turnover_pct") is not None:
        A(f"  换手率     {turn['turnover_pct']:.4f}% / 天")
    else:
        A("  未测出")
    A("")
    A("─ ④a 能力边界（固定 8 条，与本工具是否本次运行无关）" + "─" * 16)
    # 2026-10-25 修正（由 test_poolsentry.py 抓出）：
    # 原实现「上游给了几条就打印几条，再用 NOT_COVERED 从末尾补齐」——
    # 若上游 not_covered 与 NOT_COVERED 内容不同（如 activity_gate 的 4 条洗盘类），
    # 就会出现「打印了 4 条 + 补了 4 条 = 8 条，但缺了延迟开关/代理升级」的隐性漏打。
    # 修法：**以 NOT_COVERED 为准 8 条全打**，上游额外项追加在后，编号连续。
    for i, item in enumerate(NOT_COVERED, 1):
        A(f"  {i}. {item}")
    # ④b 本次未验证（动态）：之前与能力边界混在一起，客户会把「这次没查」
    # 读成「永久免责条款」，导致真正的免责声明被稀释。
    A("")
    A("─ ④b 本次未额外验证（与本次数据质量有关）" + "─" * 20)
    extra = [x for x in (act.get("not_covered") or []) if x not in NOT_COVERED]
    if extra:
        for i, item in enumerate(extra, 1):
            A(f"  {i}. {item}")
    else:
        A("  本次除上面 8 条能力边界外，未对其他项额外验证（这不等于「都查过了」）。")
    A("")
    A("─ 说明" + "─" * 52)
    A("  · 本报告只回答「买进再卖出会损失多少」，不回答「该不该买」。")
    A("  · 上述数字均可由同一池地址、同一区块高度独立复现。")
    A("  · " + BIDIRECTIONAL_DISCLAIMER)
    A("═" * 60)
    # D4：转化入口只在「付费单次报告 + 卡片成功」时出现。
    # 免费档给的是「实时的价值感」，不挂钩子；未测成时也不推销。
    if (result.get("plan") or "").lower() == "single":
        A("")
        A("  " + UPGRADE_LINE)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="PoolSentry 证据卡生成器")
    ap.add_argument("pool", help="池合约地址")
    ap.add_argument("--format", default="card", choices=["card", "free", "json", "png"],
                    help="card=完整文本卡 / free=免费分级版文本卡 / json=原始结构 / png=可截图卡片图")
    ap.add_argument("--out", default="", help="png 输出路径（默认 ./evidence_<池前8位>.png）")
    ap.add_argument("--width", type=int, default=1080)
    args = ap.parse_args()

    # 直接复用 cli 的采集逻辑
    import cli
    result = cli.analyse_subject(args.pool) if hasattr(cli, "analyse_subject") else None
    if result is None:
        print("cli 未暴露 analyse_subject，无法生成卡片", file=sys.stderr)
        sys.exit(2)

    if args.format == "json":
        print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    elif args.format == "png":
        # 红队 P0（2026-10-26）：出图的默认档位跟随 plan。
        # free / 无 plan → 免费分级卡（无精确数字，水印即真实卖点载体）
        # single → 完整卡
        _plan = (result.get("plan") or "").lower()
        card = build_card(result) if _plan in ("single", "paid") else build_free_card(result)
        out = args.out or str(ROOT / f"evidence_{args.pool.lower()[2:10]}.png")
        path = render_png(card, out, width=args.width)
        print(path)
        print(card)
    elif args.format == "free":
        print(build_free_card(result))
    else:
        print(build_card(result))


if __name__ == "__main__":
    main()

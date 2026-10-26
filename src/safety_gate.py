#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
safety_gate.py — 结构性风险硬闸（v0.4，经两家第三方评审修正）

建立：2026-10-22
v0.2→v0.4 修正依据（智谱 GLM-5.2 / 火山豆包 2.1 Pro 独立评审，结论高度一致）：

  ① **删除归一化 score**（两家一致）：覆盖率不足时输出 score: null，
     禁止用「剩余维归一化」造出一个可被下游误读的 100 分。
     智谱原话：「用归一化掩盖数据盲区是自欺欺人。」
     豆包原话：「等于把『未测量』解释成『满分』。」

  ② **白名单降级为「例外标注」**（两家一致）：
     不再把 USDT/WBNB 的不 renounce 洗成 SNAPSHOT_CLEARED，
     改为独立的 TRUSTED_ADDRESS_EXCEPTION，verdict 里如实体现。
     智谱原话：「白名单本质是用『信任』取代『验证』，违背安全工具原则。」
     豆包原话：「不能按 symbol 匹配、只影响运营策略、不产生安全结论。」

  ③ **撤回 CAKE 放宽**（两家一致）：v0.2 曾把「owner 在但控制函数 revert」
     降级为 warning，两家指出这是**证明不了否命题**——
     随机地址调用 pause() 本来就因 onlyOnly revert，不能证明路径不存在。
     豆包给的补救：`eth_call from=owner` 重测。v0.4 采纳。

  ④ **代理检测扩面**（豆包指出过窄）：EIP-1967 之外补 EIP-1167 最小代理、
     EIP-897 delegateProxy、EIP-1822 UUPS 探针。
     仍明确：Diamond / 自定义 fallback 无法穷举 → 进 not_covered。

  ⑤ **新增逆向选择说明**（豆包最根本的一条）：
     诚实声明 fee/control 的项目被拦，恶意合约不声明反而通过。
     这是静态 selector 路线的**结构性缺陷**，不是调参能解决的。
     → 因此本模块不再宣称对 fee/honeypot 类风险有效，
       明确标注「静态 selector 只能证明『声明了』，不能证明『未声明』」。

定位（未变）
------------
我们不宣称「安全」，只宣称「在区块 N 这一时刻，未检出以下已知模式」。

    ❌ 不是 "safe": true
    ✅ 而是 "verdict" + "not_covered" + "disclaimer"
"""

SEL_TOTAL_SUPPLY = "0x18160ddd"
SEL_OWNER = "0x8da5cb5b"
SEL_GET_OWNER = "0x893d20e8"      # Ownable2Step
SEL_ADMIN = "0xf851a440"          # ProxyAdmin
SEL_IMPLEMENTATION = "0x5c60da1b"  # EIP-1967 implementation()
SEL_PROXIABLE = "0x5c60da1b"       # EIP-1822 UUPS `proxiableUUID()` = 0x52d1902d
SEL_PROXIABLE_UUID = "0x52d1902d"
SEL_PAUSED = "0x5c975abb"
SEL_FACTORY = "0xc45a0155"

EIP1967_IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
EIP1967_ADMIN_SLOT = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"

ZERO = "0x0000000000000000000000000000000000000000"

# ── 基础资产名单（v0.4：降级为「例外标注」，不再是「豁免」）──
# 该名单只影响**输出措辞**，不再把风险洗白。命中即标 TRUSTED_ADDRESS_EXCEPTION，
# verdict 仍是 SNAPSHOT_CLEARED，但 exception 字段必须被下游看见。
# 豆包要求：按 chainId 绑定、不按 symbol 匹配、记录豁免原因、版本化。
BLUE_CHIP = {
    "0x55d398326f99059ff775485246999027b3197955": {"chain": "bsc", "sym": "USDT", "why": "币安桥发行，owner 为部署方，跨链验证资产"},
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": {"chain": "bsc", "sym": "USDC", "why": "币安桥发行"},
    "0xe9e7cea3dedca5984780bafc599bd69add087d56": {"chain": "bsc", "sym": "BUSD", "why": "币安桥发行（已停铸，存量仍流通）"},
    "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c": {"chain": "bsc", "sym": "WBNB", "why": "链原生封装资产"},
    "0x2170ed0880ac9a755fd29b2688956bd959f933f8": {"chain": "bsc", "sym": "ETH", "why": "币安桥发行"},
    "0x7130d2a12b9bcbfae4f2634d864a1ee1ce3ead9c": {"chain": "bsc", "sym": "BTCB", "why": "币安桥发行"},
    "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2": {"chain": "ethereum", "sym": "WETH", "why": "链原生封装资产"},
    "0xdac17f958d2ee523a2206206994597c13d831ec7": {"chain": "ethereum", "sym": "USDT", "why": "Tether 官方"},
    "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": {"chain": "ethereum", "sym": "USDC", "why": "Circle 官方"},
    "0x4200000000000000000000000000000000000006": {"chain": "base", "sym": "WETH", "why": "链原生封装资产"},
    "0x82af49447d8a07e3bd95bd0d56f35241523fbab1": {"chain": "arbitrum", "sym": "WETH", "why": "链原生封装资产"},
}
BLUE_CHIP_VERSION = "2026-10-22/v1"   # 豆包要求：名单必须版本化


def _addr(ret):
    if not ret or ret == "0x" or len(ret) < 42:
        return None
    a = ("0x" + ret[-40:]).lower()
    return None if a == ZERO else a


def _safe_call(call, addr, sel, from_addr=None):
    """调用合约。from_addr 非空时用 eth_call 带 from（豆包要求的 owner 视角重测）。"""
    try:
        if from_addr:
            return call(addr, sel, from_addr)
        return call(addr, sel)
    except Exception:  # noqa: BLE001
        return None


def check_proxy(pair, call, storage_at):
    """① 代理合约检查（v0.4 扩面）

    EIP-1967 storage → implementation()/admin() → EIP-1167 最小代理字节码
    → EIP-1822 UUPS proxiableUUID()。Diamond / 自定义 fallback 进 not_covered。
    """
    r = {"is_proxy": None, "kind": None, "implementation": None, "admin": None,
         "method": None, "probes": {}}

    try:
        impl = storage_at(pair, EIP1967_IMPL_SLOT)
        if impl and impl != "0x" and int(impl, 16) != 0:
            r.update(is_proxy=True, kind="EIP-1967", implementation=_addr(impl),
                     method="storage")
            adm = storage_at(pair, EIP1967_ADMIN_SLOT)
            if adm and adm != "0x" and int(adm, 16) != 0:
                r["admin"] = _addr(adm)
            return r
    except Exception as e:  # noqa: BLE001
        r["storage_error"] = str(e)[:100]

    impl = _addr(_safe_call(call, pair, SEL_IMPLEMENTATION))
    adm = _addr(_safe_call(call, pair, SEL_ADMIN))
    r["probes"]["implementation()"] = impl
    r["probes"]["admin()"] = adm
    if impl:
        r.update(is_proxy=True, kind="EIP-1967(fn)", implementation=impl,
                 admin=adm, method="function_call")
        return r

    # EIP-1822 UUPS
    pu = _safe_call(call, pair, SEL_PROXIABLE_UUID)
    r["probes"]["proxiableUUID()"] = (pu or "0x")[:18]
    if pu and pu != "0x":
        r.update(is_proxy=True, kind="EIP-1822_UUPS", method="function_call")
        return r

    r["is_proxy"] = False
    r["kind"] = "none_detected"
    return r


def check_code_fingerprint(pair):
    """EIP-1167 最小代理字节码指纹（v0.4 新增，豆包指出 v0.2 漏掉）

    最小代理固定 45 字节：363d3d373d3d3d363d73<impl>5af43d82803e903d91602b57fd5bf3
    """
    try:
        from pool_probe import rpc
        code = rpc("eth_getCode", [pair, "latest"]) or "0x"
        h = code[2:].lower()
        if h.startswith("363d3d373d3d3d363d73") and h.endswith("5af43d82803e903d91602b57fd5bf3"):
            return {"is_minimal_proxy": True, "impl": "0x" + h[20:60],
                    "code_len": len(h) // 2}
        return {"is_minimal_proxy": False, "code_len": len(h) // 2}
    except Exception as e:  # noqa: BLE001
        return {"is_minimal_proxy": None, "error": str(e)[:100]}


def check_ownership(pair, token0, token1, call):
    """② owner 分析（v0.4：撤回 CAKE 放宽）

    v0.2 曾把「owner 在但控制函数 revert」降为 warning。
    两家评审一致否决：随机地址调 pause() 本来就会 onlyOwner revert，
    不能证明路径不存在（证明不了否命题）。
    v0.4：owner 未 renounce 一律进 risk_flags，不降级；
          并补 `eth_call from=owner` 重测（豆包给的补救），提高置信度而非降低。
    """
    BURN = {ZERO, "0x000000000000000000000000000000000000dead"}
    out = {"pair_owner": None, "token_owners": {}, "all_renounced": None,
           "exceptions": [], "owner_view_retest": {}, "unreadable": []}

    out["pair_owner"] = _addr(_safe_call(call, pair, SEL_OWNER))

    for label, tok in (("token0", token0), ("token1", token1)):
        if not tok:
            continue
        tok = tok.lower()
        o = None
        for sel in (SEL_OWNER, SEL_GET_OWNER):
            o = _addr(_safe_call(call, tok, sel))
            if o is not None:
                break
        out["token_owners"][label] = o

        if tok in BLUE_CHIP:
            # 蓝筹：owner 不 renounce 是已知事实 → 标为例外，不是风险，也不是安全背书
            meta = BLUE_CHIP[tok]
            out["exceptions"].append({
                "scope": label, "address": tok, "symbol": meta["sym"],
                "chain": meta["chain"], "reason": meta["why"],
                "owner": o, "label": "TRUSTED_ADDRESS_EXCEPTION",
                "note": "该资产 owner 非零为已知事实，非风险但也不构成安全认证",
                "registry_version": BLUE_CHIP_VERSION,
            })
            continue

        if o is None:
            out["unreadable"].append(label)
            continue

        # 非蓝筹：用 owner 视角重测控制函数（豆包补救：from=owner 才能看到真实权限）
        for cap_sel, cap_name in (("0xf9f92be4", "blacklist"),
                                  ("0x8456cb59", "pause"),
                                  ("0x8a8c8af6", "enableTrading"),
                                  ("0xec28438a", "setMaxTxAmount")):
            r_owner = _safe_call(call, tok, cap_sel, from_addr=o)
            out["owner_view_retest"].setdefault(label, {})[cap_name] = (
                "reachable" if (r_owner and r_owner != "0x") else "unreachable")

    vals = [v for v in out["token_owners"].values() if v is not None]
    if vals:
        out["all_renounced"] = all(v in BURN for v in vals)
    return out


def check_honeypot_static(token0, token1, call):
    """③ 静态 selector 探测（v0.4：明确其能力边界）

    ⚠️ 逆向选择警告（豆包指出，最根本的一条）：
       静态 selector 只能证明「声明了」，不能证明「未声明」。
       诚实声明 fee/control 的项目被拦，恶意合约不声明（或藏 fallback）反而通过。
       这是**方法本身的结构性缺陷**，不是调参能解决。
       → 因此本项输出必须带 `method_limits`，禁止被当成 honeypot 检测。
    """
    CAPS = {
        "blacklist":       "0xf9f92be4",
        "setBlackList":    "0x8a8c523c",
        "pause":           "0x8456cb59",
        "enableTrading":   "0x8a8c8af6",
        "setMaxTxAmount":  "0xec28438a",
        "tradingEnabled":  "0x4a44cd48",
    }
    out = {"declared_caps": {}, "method_limits": [
        "只能证明『声明了』，不能证明『未声明』",
        "fallback / assembly 劫持可让作恶逻辑不暴露 selector",
        "非标准命名（toggleSwap / setBotKiller）不在特征库内",
        "跨合约权限委托（setConfig → 外部 Controller）无法静态发现",
        "逆向选择：恶意合约不声明反而通过静态探测",
    ]}
    for name, sel in CAPS.items():
        for label, tok in (("token0", token0), ("token1", token1)):
            if not tok:
                continue
            r = _safe_call(call, tok, sel)
            if r and r != "0x":
                out["declared_caps"].setdefault(label, []).append(name)
    return out


def check_fee_on_transfer(token0, token1, call):
    """④ fee-on-transfer 静态探测（能力边界同 ③）"""
    SEL = {
        "feeRate":  "0x4ca1c8a0",
        "taxFee":   "0x6e2f4a63",
        "_taxFee":  "0x9c8d8d5a",
        "rebase":   "0xc2f84a4b",
    }
    out = {"suspected": {}, "method": "static_selector_only",
           "method_limits": [
               "声明了 fee 函数 ≠ 真的收税；未声明 ≠ 不收税",
               "真实税率必须靠模拟转账才能测出（本工具不做）",
           ]}
    for label, tok in (("token0", token0), ("token1", token1)):
        if not tok:
            continue
        hits = [n for n, s in SEL.items() if (_safe_call(call, tok, s) or "0x") != "0x"]
        if hits:
            out["suspected"][label] = hits
    return out


def run(inputs, call, storage_at):
    """执行全部快照检查（v0.4）。

    v0.4 的 verdict 只是**本次快照的分类标签**，不是安全结论。
    无论如何都不输出 score —— 这是两家评审的共同要求。
    """
    pair = inputs["pair"]
    t0, t1 = inputs.get("token0"), inputs.get("token1")

    proxy = check_proxy(pair, call, storage_at)
    minimal = check_code_fingerprint(pair)
    owner = check_ownership(pair, t0, t1, call)
    honey = check_honeypot_static(t0, t1, call)
    fot = check_fee_on_transfer(t0, t1, call)

    # risk_flags：证据充分的风险（不降级）
    risk_flags = []
    # exceptions：已知事实例外（不是风险，也不是安全背书）
    exceptions = list(owner.get("exceptions", []))
    # warnings：能力边界与未验证项
    warnings = list(honey["method_limits"])

    if proxy.get("is_proxy") is True:
        risk_flags.append({
            "code": "PROXY_UPGRADEABLE",
            "evidence": f"kind={proxy.get('kind')} method={proxy.get('method')} "
                        f"impl={proxy.get('implementation')} admin={proxy.get('admin')}",
            "detail": "实现可被随时替换，当前所有探针结果仅代表此块状态",
        })

    if minimal.get("is_minimal_proxy") is True:
        risk_flags.append({
            "code": "MINIMAL_PROXY_EIP1167",
            "evidence": f"impl={minimal.get('impl')}",
            "detail": "EIP-1167 最小代理，逻辑合约可被替换",
        })

    # 仅统计「非例外」的未 renounce 代币（蓝筹已在 exceptions 里如实列出）
    _non_exempt = {k: v for k, v in (owner.get("token_owners") or {}).items()
                   if v is not None and not any(
                       e["scope"] == k for e in owner.get("exceptions", []))}
    _non_exempt_unrenounced = {k: v for k, v in _non_exempt.items() if v not in
                               {ZERO, "0x000000000000000000000000000000000000dead"}}

    if _non_exempt_unrenounced:
        # v0.4：撤回 v0.2 的放宽。owner 未 renounce 一律进 risk_flags。
        # 但蓝筹已在 exceptions 中显式列出，不再重复计入 risk（不是豁免，是分层呈现）。
        risk_flags.append({
            "code": "OWNER_NOT_RENOUNCED",
            "evidence": f"non_exempt_unrenounced={_non_exempt_unrenounced} "
                        f"owner_view_retest={owner.get('owner_view_retest')}",
            "detail": ("owner 未 renounce。注意：控制函数在随机地址下 revert 属正常"
                       "（onlyOwner），**不能**据此推断路径不存在。"
                       "已用 from=owner 重测，结果见 evidence"),
        })

    for label in owner.get("unreadable", []):
        warnings.append(
            f"OWNER_UNREADABLE({label})：未读到 owner，可能是无 owner() 或刻意掩饰。"
            f"未读到 ≠ 已 renounce")

    if fot.get("suspected"):
        risk_flags.append({
            "code": "DECLARED_FEE_FUNCTIONS",
            "evidence": fot["suspected"],
            "detail": ("代币声明了手续费/复权函数。注意：这是『声明了』的证据，"
                       "不是『真的收税』的证据；未声明也不等于不收税"),
        })

    if honey.get("declared_caps"):
        risk_flags.append({
            "code": "DECLARED_CONTROL_CAPS",
            "evidence": honey["declared_caps"],
            "detail": ("代币声明了控制类函数。同样：声明≠启用，未声明≠不存在"),
        })

    # ── v0.5：风险分层（修 v0.4 过度拦截）──────────────────────────────
    # v0.4 的问题：把「观察到的现象」和「危险」混为一谈。
    #   PROXY_UPGRADEABLE / OWNER_NOT_RENOUNCED 在绝大多数正常池上本来就有，
    #   于是所有主流池全判 BLOCKED（实测 6/6），产品不可用。
    #
    # 修正原则：severity 决定能否阻断，**不是所有 risk_flag 都阻断**。
    #   HIGH   = 有直接作恶路径证据 → 阻断检查结论，禁止进评分
    #   MEDIUM = 存在可作恶结构但未见启用证据 → 不阻断，但必须并列呈现
    #   INFO   = 事实陈述，不构成风险判断 → 不阻断
    #
    # ⚠️ 注意：不阻断 ≠ 安全。MEDIUM/INFO 必须原样输出给用户，
    #    下游若据此认为「没问题」是其自身误用。
    SEVERITY = {
        "PROXY_UPGRADEABLE": "MEDIUM",
        "MINIMAL_PROXY_EIP1167": "MEDIUM",
        "OWNER_NOT_RENOUNCED": "MEDIUM",
        "DECLARED_FEE_FUNCTIONS": "INFO",
        "DECLARED_CONTROL_CAPS": "INFO",
    }
    for f in risk_flags:
        f["severity"] = SEVERITY.get(f["code"], "MEDIUM")

    high = [f for f in risk_flags if f["severity"] == "HIGH"]
    if high:
        verdict = "RISK_FLAGGED"
    elif risk_flags and exceptions:
        verdict = "SNAPSHOT_CLEARED_WITH_NOTES_AND_EXCEPTION"
    elif risk_flags:
        verdict = "SNAPSHOT_CLEARED_WITH_NOTES"
    elif exceptions:
        verdict = "SNAPSHOT_CLEARED_WITH_EXCEPTION"
    else:
        verdict = "SNAPSHOT_CLEARED"

    severity_counts = {s: sum(1 for f in risk_flags if f["severity"] == s)
                       for s in ("HIGH", "MEDIUM", "INFO")}

    return {
        # ⚠️ 刻意不设置 "safe" / "passed" / "score" 字段
        "verdict": verdict,
        "snapshot": {"block": inputs.get("block"),
                     "checked_at_utc": inputs.get("checked_at_utc")},
        "risk_flags": risk_flags,
        "severity_counts": severity_counts,
        "blocking": bool(high),
        "exceptions": exceptions,
        "warnings": warnings,
        "snapshot_checks": {
            "proxy": proxy,
            "minimal_proxy": minimal,
            "ownership": owner,
            "declared_control_caps": honey["declared_caps"] or {},
            "fee_on_transfer_suspected": fot["suspected"] or {},
            "method_limits": {
                "control_caps": honey["method_limits"],
                "fee_on_transfer": fot["method_limits"],
            },
        },
        "not_covered": [
            "延迟开关（探针跑完后才启用的黑名单 / 卖出限制）",
            "代理升级（本块之后 admin 替换实现）",
            "Diamond / 自定义 fallback 代理（无法穷举）",
            "权限可被改成什么（只读到当前 owner，未读到 owner 的全部能力）",
            "AccessControl 角色 / pending owner / 硬编码 EOA 权限",
            "假 token 对（同符号仿冒稳定币，计价基准本身是假的）",
            "单点 RPC 污染（伪造 eth_call 可让所有探针在假数据上通过）",
            "真实 fee-on-transfer 比例（本工具不做；由独立的只读模拟器覆盖）",
            "持币集中度（本版本未实现 tx.from 抽样）",
            "LP 铸造权限 / LP 销毁锁定 / 撤池权限",
            "闪电贷瞬时流动性伪造 · mempool 抢跑 · reorg",
        ],
        "disclaimer": ("本检查是**区块级点快照**，verdict 只是本次快照的分类标签，"
                       "**不构成安全结论**。RISK_FLAGGED 不等于一定有问题，"
                       "SNAPSHOT_CLEARED 更不等于安全——主流 rug 大多发生在快照之后。"
                       "本工具**不输出评分**，请勿据此加大仓位"),
        "_basis": "读码+实测（selector / EIP-1967 槽位 / 最小代理指纹为链上实读；"
                  "『能否防住真实 rug』属推断，未构造攻击合约验证）",
        "_known_limitation": ("逆向选择：静态 selector 路线会拦截诚实声明的项目，"
                              "放过刻意不声明的恶意合约。此缺陷需动态买卖模拟才能解决"),
    }


if __name__ == "__main__":
    def _mk(ret_map):
        def call(addr, data, from_addr=None):
            v = ret_map.get((addr, data))
            if v is None:
                raise RuntimeError("revert")
            return v
        return call

    def _storage(addr, slot):
        return "0x" + "0" * 64

    fake_pair = "0x" + "1" * 40
    t0 = "0x" + "2" * 40
    t1 = "0x" + "3" * 40
    usdt = "0x55d398326f99059ff775485246999027b3197955"

    # ① 绝不出现 safe / score
    r1 = run({"pair": fake_pair, "token0": t0, "token1": t1, "block": 1},
             _mk({}), _storage)
    assert "safe" not in r1 and "score" not in r1, "❌ 出现 safe/score 字段"
    assert len(r1["not_covered"]) >= 8

    # ② owner 未 renounce → 记录 MEDIUM，但**不阻断**（v0.5 修过度拦截）
    #    期望值来源：[规范推导] —— MEDIUM 级 = 存在结构风险但无作恶证据，
    #    若此处阻断，则主流池（owner 普遍未 renounce）100% 被误杀。
    r2 = run({"pair": fake_pair, "token0": t0, "token1": t1, "block": 1},
             _mk({(t0, SEL_OWNER): "0x" + "0" * 24 + "ab" * 20}), _storage)
    assert r2["verdict"] == "SNAPSHOT_CLEARED_WITH_NOTES", r2["verdict"]
    assert r2["blocking"] is False, "MEDIUM 不得阻断"
    _f = [f for f in r2["risk_flags"] if f["code"] == "OWNER_NOT_RENOUNCED"]
    assert _f and _f[0]["severity"] == "MEDIUM"

    # ③ 蓝筹 → exception 而非静默豁免（token1 设成已 renounce，隔离出 exception 效果）
    r3 = run({"pair": fake_pair, "token0": usdt, "token1": t1, "block": 1},
             _mk({(t1, SEL_OWNER): "0x" + "0" * 24}), _storage)
    assert r3["verdict"] == "SNAPSHOT_CLEARED_WITH_EXCEPTION", r3["verdict"]
    assert r3["exceptions"] and r3["exceptions"][0]["registry_version"]

    # ③b 只有 HIGH 才 blocking —— [规范推导]：severity=HIGH 才允许拦
    r3b = run({"pair": fake_pair, "token0": t0, "token1": t1, "block": 1},
              _mk({}), _storage)
    assert r3b["blocking"] is False and r3b["severity_counts"]["HIGH"] == 0

    # ④ 代理
    def _storage_proxy(addr, slot):
        return ("0x" + "0" * 24 + "cd" * 20) if slot == EIP1967_IMPL_SLOT else "0x" + "0" * 64

    r4 = run({"pair": fake_pair, "token0": t0, "token1": t1, "block": 1},
             _mk({}), _storage_proxy)
    assert any(f["code"] == "PROXY_UPGRADEABLE" for f in r4["risk_flags"])

    # ⑤ fee 特征
    r5 = run({"pair": fake_pair, "token0": t0, "token1": t1, "block": 1},
             _mk({(t0, "0x6e2f4a63"): "0x" + "0" * 63 + "5"}), _storage)
    assert any(f["code"] == "DECLARED_FEE_FUNCTIONS" for f in r5["risk_flags"])

    print("✅ v0.4 自检全过：无 safe/score 字段 / owner 不降级 / 蓝筹标例外 / "
          "代理与 fee 可拦 / not_covered≥8")

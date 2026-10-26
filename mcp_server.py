#!/usr/bin/env python3
"""mcp_server.py — PoolSentry MCP Server（stdio，JSON-RPC 2.0，零第三方依赖）。

为什么要有这个文件：
  PoolSentry 的核心价值是「让 AI 在推荐用户进某个池之前，先算一遍进出成本」。
  这件事天然属于 MCP 场景 —— 用户在 Claude/Cursor 里问「我拿 5000 U 进 CAKE/WBNB 池划算吗」，
  模型应该能直接调用链上实算，而不是凭印象说「深度应该够」。

  Smithery / mcp.so / Glama 这些目录只收 MCP server。所以这是上架的第一道门。

协议实现范围（够用即可，不追规范全量）：
  - initialize / notifications/initialized
  - tools/list
  - tools/call
  - ping

工具：
  1. pool_roundtrip_loss  —— 给定池地址 + 金额，返回往返损耗（单跳闭式解）
  2. pool_health          —— 池体检：TVL / 活跃度 / 往返损耗四档
  3. explain_formula      —— 解释 depth_1pct 口径（防止模型自己瞎算）

运行：
  python3 mcp_server.py                # stdio，供 MCP 客户端拉起
  python3 mcp_server.py --selftest     # 无需客户端，自检协议与两个工具
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

SERVER_NAME = "poolsentry"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "pool_roundtrip_loss",
        "description": (
            "计算在 BSC（PancakeSwap V2）某个具体池子里买进再卖出的往返损耗。"
            "只读该池 getReserves() 做 x*y=k 单跳闭式解，含 0.25% 手续费，"
            "不是全站最优路由报价。用于回答「我拿 $X 进去再出来还剩多少」。"
            "注意：只回答交易成本，不回答该不该买（蜜罐/rug 不在范围内）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pair_address": {
                    "type": "string",
                    "description": "Pancake V2 交易对（池）地址，0x 开头 40 位十六进制",
                },
                "amount_usd": {
                    "type": "number",
                    "description": "投入金额（美元）。不给则返回默认四档（10 / 0.1%TVL / 1%TVL / 20000）",
                },
            },
            "required": ["pair_address"],
        },
    },
    {
        "name": "pool_health",
        "description": (
            "对 BSC 某个 Pancake V2 池做体检：TVL、近 6h 转账活跃度、"
            "四档金额的往返损耗与占池比。返回结构化 JSON，含 tvl_basis 标注"
            "（both_sides_usd 表示两侧都折算到了美元；one_side_times2 表示只有一侧有美元锚、"
            "按 V2 两侧等值口径 ×2）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "pair_address": {"type": "string", "description": "池地址"},
            },
            "required": ["pair_address"],
        },
    },
    {
        "name": "explain_formula",
        "description": (
            "解释 PoolSentry 的口径定义，避免模型自行估算。"
            "包括 depth_1pct（价格冲击 1% 的口径，不是吃进 TVL 的 1%）、"
            "往返损耗为何约等于 2×手续费、以及哪些结论本工具不产出。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _err(mid, code, msg):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": msg}}


def _ok(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _text(mid, text, is_error=False):
    return _ok(mid, {"content": [{"type": "text", "text": text}], "isError": is_error})


def tool_roundtrip_loss(args):
    import cli

    pair = (args.get("pair_address") or "").strip()
    if not pair.startswith("0x") or len(pair) != 42:
        return None, "pair_address 必须是 0x 开头的 42 位地址"
    amt = args.get("amount_usd")

    res = cli.analyse_subject(pair)
    if res.get("status") != "OK":
        return None, f"分析失败：{res.get('status')} {res.get('message','')}"

    rts = res.get("round_trips") or []
    if amt is None:
        rows = rts
    else:
        # 按用户金额单独算一次（走同一套单跳闭式解）。
        # ⚠️ round_trip 的 amount_in 是「基准代币原始单位」，不是美元，必须自己换算。
        # 基准币取「与 WBNB 相对的那一枚」（池本身的报价方向），用 BNB 价折美元。
        try:
            import cli as _c
            t0, t1 = _c.token0(pair), _c.token1(pair)
            bnb_usd = _c.wbnb_usd() or 0.0
            base = t1 if t0 == _c.WBNB else t0
            base_dec = _c.decimals(base)
            base_sym = _c.symbol(base).upper()
            if base == _c.WBNB:
                px = bnb_usd
            elif base_sym in getattr(_c, "USD_STABLE_SYMBOLS", set()):
                px = 1.0
            else:
                # 非 WBNB、非美元稳定币的基准币（CAKE 等）：用「池内 WBNB 侧美元值 ÷ 基准币数量」
                # 反推单价。⚠️这与我此前撤销的「用隐含价推 TVL」**不是同一件事**：
                # 那次是用原始储备比例去估**整个池的 TVL**，错误被 ×180 放大；
                # 这里只用来把「用户输入的美元金额」换成「基准币数量」，
                # 且只影响单笔金额档，不影响 TVL 与占池比（那两者仍走 tvl_basis 口径）。
                r0, r1 = _c._pair_reserves(pair)
                d0, d1 = _c.decimals(t0), _c.decimals(t1)
                if t0 == _c.WBNB:
                    side_usd = (r0 / 10 ** d0) * bnb_usd
                    base_amt = r1 / 10 ** d1
                else:
                    side_usd = (r1 / 10 ** d1) * bnb_usd
                    base_amt = r0 / 10 ** d0
                if base_amt <= 0 or side_usd <= 0:
                    return None, "池内储备异常，无法按美元金额单算"
                px = side_usd / base_amt
            if px <= 0:
                return None, "BNB 美元价取不到，无法按美元金额单算"
            amount_raw = float(amt) / px * (10 ** base_dec)
            out_amt, loss_pct = _c.round_trip(pair, amount_raw, base, _c.WBNB if base != _c.WBNB else t0)
            back_usd = out_amt / (10 ** base_dec) * px
            tvl = res.get("tvl_usd") or 0
            rows = [{
                "in_usd": float(amt),
                "out_usd": back_usd,
                "loss_pct": loss_pct,
                "pct_of_tvl": (float(amt) / tvl * 100) if tvl > 0 else None,
            }]
        except Exception as e:  # noqa: BLE001
            return None, f"单笔计算失败：{type(e).__name__}: {e}"

    out = {
        "pair": res.get("pair_name"),
        "tvl_usd": res.get("tvl_usd"),
        "tvl_basis": res.get("tvl_basis"),
        "block": (res.get("activity") or {}).get("to_block") or res.get("block"),
        "definition": "single_hop_closed_form_x_y_k",
        "fee": "0.25% per swap (Pancake V2)",
        "round_trips": [
            {
                "in_usd": r.get("in_usd"),
                "out_usd": r.get("out_usd"),
                "loss_pct": r.get("loss_pct"),
                "pct_of_tvl": (r.get("pct_of_tvl") / 100 if r.get("pct_of_tvl") is not None else None),
            }
            for r in rows
        ],
        "disclaimer": "只回答交易成本。未测出≠安全；测出来也≠安全（蜜罐/rug 不在范围内）。",
    }
    return json.dumps(out, ensure_ascii=False, indent=2), None


def tool_pool_health(args):
    import cli

    pair = (args.get("pair_address") or "").strip()
    if not pair.startswith("0x") or len(pair) != 42:
        return None, "pair_address 必须是 0x 开头的 42 位地址"
    res = cli.analyse_subject(pair)
    if res.get("status") != "OK":
        return None, f"分析失败：{res.get('status')} {res.get('message','')}"
    slim = {
        "pair": res.get("pair_name"),
        "pair_address": res.get("pair"),
        "tvl_usd": res.get("tvl_usd"),
        "tvl_basis": res.get("tvl_basis"),
        "bnb_usd": res.get("bnb_usd"),
        "block": (res.get("activity") or {}).get("to_block") or res.get("block"),
        "activity": res.get("activity"),
        "round_trips": res.get("round_trips"),
        "errors": res.get("errors"),
        "disclaimer": "未测出≠安全；测出来也≠安全。",
    }
    return json.dumps(slim, ensure_ascii=False, indent=2), None


def tool_explain_formula(_args):
    txt = """PoolSentry 口径说明（直接引用，勿自行估算）

1) depth_1pct —— 「价格冲击 1%」口径，不是「吃进 TVL 的 1%」
   正确：depth_1pct = TVL × 0.00251891
   常见错误：TVL × 0.005（= 2 倍高估，方向是乐观的）
   V2 恒定乘积推导：把价格推动 1%，需要吃进约 0.2519% 的储备。

2) 往返损耗（buy→sell）为什么约等于 2 × 手续费
   单跳 x*y=k 含 0.25% 手续费时，小额往返的理论下限 ≈ 1 - (0.9975)^2 ≈ 0.4994%。
   金额越大，滑点叠加越多，损耗单调上升。若观测值显著低于 0.4994%，说明口径错了
   （例如用了全站最优路由，绕开了本池的稀薄深度）。

3) 本工具不产出什么（这些需要别的机制）：
   - 蜜罐判定（需私钥实盘）
   - fee-on-transfer 真实比例（需多笔实盘）
   - 延迟开关 / 代理升级 / owner 后门
   - 持币集中度
   - 「该不该买」的任何结论

4) 缺值处理
   算不出来一律输出「未测出」，不补 0、不补 100。
   tvl_basis=one_side_times2 表示池内只有一侧有美元锚，按 V2 两侧等值口径 ×2 估算 ——
   这是保守口径，不是精确值。"""
    return txt, None


HANDLERS = {
    "pool_roundtrip_loss": tool_roundtrip_loss,
    "pool_health": tool_pool_health,
    "explain_formula": tool_explain_formula,
}


def handle(req):
    method = req.get("method")
    mid = req.get("id")
    params = req.get("params") or {}

    if method == "initialize":
        return _ok(mid, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _ok(mid, {})
    if method == "tools/list":
        return _ok(mid, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if not fn:
            return _err(mid, -32602, f"未知工具：{name}")
        try:
            text, err = fn(args)
        except Exception as e:  # noqa: BLE001
            return _text(mid, f"工具执行异常：{type(e).__name__}: {e}", is_error=True)
        if err:
            return _text(mid, err, is_error=True)
        return _text(mid, text)
    if method in ("resources/list", "prompts/list"):
        # 声明了 tools-only，这两个返回空表比报错更兼容
        key = "resources" if method.startswith("resources") else "prompts"
        return _ok(mid, {key: []})
    return _err(mid, -32601, f"未实现的方法：{method}")


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def selftest():
    """不依赖任何 MCP 客户端，直接喂协议帧。"""
    print("=== PoolSentry MCP selftest ===")
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "explain_formula", "arguments": {}}},
    ]
    okc = fail = 0
    for f in frames:
        r = handle(f)
        if r and "result" in r:
            print(f"  [{f['id']}] {f['method']} ✓")
            okc += 1
        else:
            print(f"  [{f['id']}] {f['method']} ✗ {r}")
            fail += 1

    tl = handle({"jsonrpc": "2.0", "id": 4, "method": "tools/list"})["result"]["tools"]
    print(f"  工具数：{len(tl)} → {[t['name'] for t in tl]}")
    print(f"  结果：{okc} PASS / {fail} FAIL")
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    serve()

#!/usr/bin/env python3
"""
pool_probe.py — 池契约探针（PIT-044 机制化）
v1.0 · 2026-10-18

职责
----
判断一个地址「是不是标准 Pancake V2 pair」，并给出可复现的理由。
这是所有池计算的前置门：不通过 → 不许进入储备/深度/评分。

为什么必须单独做
----------------
2026-10-18 实测：19 个候选池里 14 个调 getReserves() 直接 revert，
旧代码用 `except: continue` 把 revert 吞成「采样点没取到」，
最终只显示「有效快照 6 < 阈值」——把人误导成"数据稀疏"。

产出三态（互斥、分母闭合）
    V2_OK         五探针全过
    NOT_V2        契约不符（附 fail 项）
    UNAVAILABLE   RPC 层不可达（不判对错，只判取不到）

用法
----
python3 -u src/onchain/pool_probe.py --pools pools.json [--json]
python3 -u src/onchain/pool_probe.py --pair 0x...
"""
import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 端能力实测（PIT-039 / PIT-044）
ARCHIVE_RPC = "https://bsc-dataseed.bnbchain.org"
ENDPOINTS = [
    # 2026-10-22 实测：以下为当前可用公共端点（原 blastapi/publicnode 对 eth_call 返回空 0x）
    "https://bsc-dataseed.bnbchain.org",
    "https://bsc-dataseed1.bnbchain.org",
    "https://bsc-dataseed2.bnbchain.org",
]

SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_GET_RESERVES = "0x0902f1ac"
SEL_DECIMALS = "0x313ce567"
SEL_SYMBOL = "0x95d89b41"
# V3 特征选择器（PIT-044 升级：先识别版本，再判定可用性）
SEL_SLOT0 = "0x3850c7bd"
SEL_FEE = "0xddca3f43"
SEL_LIQUIDITY = "0x1a686502"

_HDR = {"Content-Type": "application/json", "User-Agent": "curl/8.5.0", "Accept": "*/*"}

STABLE = {
    "0x55d398326f99059ff775485246999027b3197955": "USDT",
    "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d": "USDC",
    "0xe9e7cea3dedca5984780bafc599bd69add087d56": "BUSD",
}
WBNB = "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c"


class RpcUnavailable(Exception):
    """端点层不可达，不代表地址有问题"""


def _post(url, method, params, timeout=15):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url, data=body, headers=_HDR)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def rpc(method, params, tries=3, timeout=15):
    last = None
    for i in range(min(max(tries, len(ENDPOINTS)), len(ENDPOINTS))):
        try:
            d = _post(ENDPOINTS[i], method, params, timeout)
            if "error" in d:
                msg = json.dumps(d["error"])
                # 契约级错误要原样上抛，不能当端点故障重试
                if "revert" in msg.lower():
                    raise ValueError(msg)
                last = RuntimeError(msg)
                continue
            res = d.get("result")
            # 空 0x = 端点伪可用（2026-10-22 PIT-079）：换下一个端点，不许当成真返回
            if _bad_result(res):
                last = RuntimeError(f"{ENDPOINTS[i]} 返回空 0x（端点伪可用）")
                continue
            return res
        except ValueError:
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.3)
    raise RpcUnavailable(f"{method}: {last}")


def _bad_result(res):
    """空 0x = 端点伪可用（不报错但拿不到数据）——2026-10-22 PIT-079"""
    return isinstance(res, str) and res == "0x"


def call(addr, data, block="latest"):
    return rpc("eth_call", [{"to": addr, "data": data}, block])


def storage_at(addr, slot, block="latest"):
    """eth_getStorageAt —— 读 EIP-1967 槽位（safety_gate 用）。

    为什么不只调 implementation()：函数返回值可被合约伪造，
    标准槽位是 EVM 层面的事实，更难撒谎（PIT-086 配套）。
    """
    return rpc("eth_getStorageAt", [addr, slot, block])


def _addr_from(ret):
    if not ret or len(ret) < 66:
        raise ValueError(f"返回值长度异常: {ret[:20] if ret else '0x'}")
    return ("0x" + ret[-40:]).lower()


def _uint_from(ret):
    if not ret or ret == "0x":
        raise ValueError("空返回")
    return int(ret, 16)


def _symbol(addr):
    """symbol() 可能是 string 也可能是 bytes32，两种都试"""
    try:
        r = call(addr, SEL_SYMBOL)
        if not r or r == "0x":
            return None
        h = r[2:]
        if len(h) >= 128:                     # string：offset+len+data
            ln = int(h[64:128], 16)
            raw = bytes.fromhex(h[128:128 + ln * 2])
            return raw.decode("utf-8", "ignore").strip("\x00") or None
        raw = bytes.fromhex(h[:64]).rstrip(b"\x00")
        return raw.decode("utf-8", "ignore") or None
    except Exception:  # noqa: BLE001
        return None


def _detect_version(pair):
    """识别 AMM 版本。PIT-044：不要把所有 getReserves 失败的都叫『非 V2』"""
    hits = []
    for name, sel in [("slot0", SEL_SLOT0), ("fee", SEL_FEE), ("liquidity", SEL_LIQUIDITY)]:
        try:
            r = call(pair, sel)
            if r and r != "0x":
                hits.append(name)
        except Exception:  # noqa: BLE001
            pass
    if len(hits) >= 2:
        return f"V3({'/'.join(hits)} 可读)"
    if hits:
        return f"SUSPECT_V3(仅 {'/'.join(hits)})"
    return "UNKNOWN"


# ── PIT-052 ｜ V2/V3 判别顺序（2026-10-19 三方裁定落地：WorkBuddy 顺序 + 豆包 Algebra 陷阱）──
# 顺序：① V2 工厂反查（PCS V2 getPair）→ ② V3 工厂反查（PCS V3 getPool × 5 fee 档）
#       → ③ 同族内 Algebra 区分（getReserves 长度 / fee() 固定值 / plugin()）
# 选择器均经 keccak 实算核对：getPair 0xe6a43905 / getPool 0x1698ee82
#   slot0 0x3850c7bd / fee 0xddca3f43 / liquidity 0x1a686502 / plugin 0xef01df52
FACTORY_PCS_V2 = "0xca143ce32fe78f1f7019d7d551a6402fc5350c73"
FACTORY_PCS_V3 = "0x0bfbcf9fa4f9c56b0f40a671ad40e0805a091865"
V3_FEE_TIERS = (100, 500, 2500, 3000, 10000)
SEL_GET_PAIR = "0xe6a43905"
SEL_GET_POOL = "0x1698ee82"
SEL_PLUGIN = "0xef01df52"


def _enc_addr(a):
    return a.lower()[2:].rjust(64, "0")


def _factory_lookup(factory, sel, t0, t1, fee=None):
    data = sel + _enc_addr(t0) + _enc_addr(t1)
    if fee is not None:
        data += hex(fee)[2:].rjust(64, "0")
    try:
        r = call(factory, data)
        if not r or r == "0x" or int(r, 16) == 0:
            return None
        return ("0x" + r[-40:]).lower()
    except Exception:  # noqa: BLE001
        return None


def classify_amm(pair, t0, t1):
    """返回 (kind, evidence)。kind ∈ {V2, V3, ALGEBRA, UNKNOWN}
    证据必须落盘（PIT-052：判别结论不得无证据）。"""
    ev = {}
    a = _factory_lookup(FACTORY_PCS_V2, SEL_GET_PAIR, t0, t1)
    ev["v2_factory"] = a
    if a == pair.lower():
        return "V2", ev
    for fee in V3_FEE_TIERS:
        b = _factory_lookup(FACTORY_PCS_V3, SEL_GET_POOL, t0, t1, fee)
        if b == pair.lower():
            ev["v3_fee"] = fee
            return "V3", ev
    # 工厂查不到（代理/非标准/分叉）→ 长度指纹 + 特征函数
    try:
        r = call(pair, SEL_GET_RESERVES)
        ev["getReserves_bytes"] = len(r[2:]) // 2
        if ev["getReserves_bytes"] >= 96:
            return "V2", ev
    except Exception:  # noqa: BLE001
        ev["getReserves"] = "revert"
    try:
        s0 = call(pair, SEL_SLOT0)
        ev["slot0_bytes"] = len(s0[2:]) // 2
    except Exception:  # noqa: BLE001
        ev["slot0"] = "revert"
    has_fee = has_liq = False
    try:
        has_fee = call(pair, SEL_FEE) not in (None, "0x")
    except Exception:  # noqa: BLE001
        pass
    try:
        has_liq = call(pair, SEL_LIQUIDITY) not in (None, "0x")
    except Exception:  # noqa: BLE001
        pass
    ev["fee_readable"], ev["liquidity_readable"] = has_fee, has_liq
    if ev.get("slot0_bytes", 0) >= 224:
        # 豆包陷阱：Algebra 也有 slot0；无 fee() 有 plugin() → Algebra
        try:
            has_plugin = call(pair, SEL_PLUGIN) not in (None, "0x")
        except Exception:  # noqa: BLE001
            has_plugin = False
        ev["plugin_readable"] = has_plugin
        return ("ALGEBRA" if (has_plugin or not has_fee) else "V3"), ev
    return "UNKNOWN", ev


def _block_number():
    """取当前块号，写进报告（快照必须可定位到块，否则不可复现）"""
    try:
        return int(rpc("eth_blockNumber", []), 16)
    except Exception:  # noqa: BLE001
        return None


def probe(pair):
    """五探针。返回 dict，status ∈ {V2_OK, NOT_V2, WRONG_VERSION, UNAVAILABLE}"""
    out = {
        "pair": pair,
        "status": None,
        "checks": {},
        "reason": None,
        "token0": None,
        "token1": None,
        "symbol0": None,
        "symbol1": None,
        "decimals0": None,
        "decimals1": None,
        "reserves": None,
        "quote_kind": None,
    }

    # 安全检查：地址长度（PIT-040）
    if not (isinstance(pair, str) and pair.startswith("0x") and len(pair) == 42):
        out.update(status="NOT_V2", reason=f"地址格式非法（len={len(str(pair))}）")
        return out

    try:
        # ① token0
        try:
            t0 = _addr_from(call(pair, SEL_TOKEN0))
            out["checks"]["token0"] = "pass"
        except ValueError as e:
            out.update(status="NOT_V2", reason=f"token0() 调用失败：{e}")
            out["checks"]["token0"] = "fail"
            return out

        # ② token1
        try:
            t1 = _addr_from(call(pair, SEL_TOKEN1))
            out["checks"]["token1"] = "pass"
        except ValueError as e:
            out.update(status="NOT_V2", reason=f"token1() 调用失败：{e}")
            out["checks"]["token1"] = "fail"
            return out

        out["token0"], out["token1"] = t0, t1
        out["symbol0"], out["symbol1"] = _symbol(t0), _symbol(t1)

        # ③ token0 != token1
        if t0 == t1:
            out.update(status="NOT_V2", reason="token0 == token1（畸形池）")
            out["checks"]["distinct"] = "fail"
            return out
        out["checks"]["distinct"] = "pass"

        # ④ getReserves
        try:
            r = call(pair, SEL_GET_RESERVES)
            h = r[2:]
            if len(h) < 128:
                raise ValueError(f"返回不足 128 hex（实际 {len(h)}）")
            r0, r1 = int(h[0:64], 16), int(h[64:128], 16)
        except ValueError as e:
            out["checks"]["getReserves"] = "fail"
            # getReserves 失败但 token0/token1 可读 → 大概率是 V3/v4
            ver = _detect_version(pair)
            out["detected_version"] = ver
            if ver.startswith("V3"):
                out.update(
                    status="WRONG_VERSION",
                    reason=f"这是 Uniswap/Pancake V3 池（slot0/fee/liquidity 可读，无 getReserves）。"
                           f"当前引擎只支持恒定乘积 V2，需另建 V3 定价与深度模型",
                )
            else:
                out.update(status="NOT_V2", reason=f"getReserves() 失败：{e}")
            return out
        out["checks"]["getReserves"] = "pass"
        # PIT-052：即便 getReserves 可读，也做工厂/指纹判别（防 Algebra 兼容对混入）
        _kind, _ev = classify_amm(pair, t0, t1)
        out["amm_kind"] = _kind
        out["amm_evidence"] = _ev

        if r0 == 0 or r1 == 0:
            # 空池：契约对，但无数据（PIT-041：不能当"深度 0"）
            out.update(status="NOT_V2", reason="储备为 0（空池/未初始化）")
            out["checks"]["non_empty"] = "fail"
            return out
        out["checks"]["non_empty"] = "pass"

        # ⑤ decimals
        try:
            d0 = _uint_from(call(t0, SEL_DECIMALS))
            d1 = _uint_from(call(t1, SEL_DECIMALS))
            if not (0 <= d0 <= 36 and 0 <= d1 <= 36):
                raise ValueError(f"decimals 越界 {d0}/{d1}")
        except ValueError as e:
            out.update(status="NOT_V2", reason=f"decimals() 失败：{e}")
            out["checks"]["decimals"] = "fail"
            return out
        out["decimals0"], out["decimals1"] = d0, d1
        out["checks"]["decimals"] = "pass"

        h0, h1 = r0 / 10 ** d0, r1 / 10 ** d1
        out["reserves"] = {"r0_human": h0, "r1_human": h1, "raw0": r0, "raw1": r1}

        # 计价基准
        if t0 in STABLE:
            out["quote_kind"] = f"stable:{STABLE[t0]}"
        elif t1 in STABLE:
            out["quote_kind"] = f"stable:{STABLE[t1]}"
        elif t0 == WBNB or t1 == WBNB:
            out["quote_kind"] = "wbnb"
        else:
            out["quote_kind"] = None   # 合法但仍无法直接定 USD

        out["status"] = "V2_OK"
        out["block"] = _block_number()
        return out

    except RpcUnavailable as e:
        out.update(status="UNAVAILABLE", reason=f"RPC 不可达：{e}")
        return out


def summarize(results):
    """分母闭合校验：三类相加必须等于总数"""
    n = len(results)
    ok = sum(1 for r in results if r["status"] == "V2_OK")
    notv2 = sum(1 for r in results if r["status"] == "NOT_V2")
    wrong = sum(1 for r in results if r["status"] == "WRONG_VERSION")
    unav = sum(1 for r in results if r["status"] == "UNAVAILABLE")
    closed = (ok + notv2 + wrong + unav) == n
    return {
        "total": n,
        "V2_OK": ok,
        "NOT_V2": notv2,
        "WRONG_VERSION": wrong,
        "UNAVAILABLE": unav,
        "denominator_closed": closed,
        "priced": sum(1 for r in results if r.get("quote_kind")),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair")
    ap.add_argument("--pools")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out")
    a = ap.parse_args()

    targets = []
    if a.pair:
        targets = [a.pair]
    elif a.pools:
        d = json.load(open(a.pools, encoding="utf-8"))
        if isinstance(d, dict):
            d = d.get("pools") or []
        targets = [x.get("pair") or x.get("pool") or x.get("addr") for x in d]
    targets = [t for t in targets if t]
    if not targets:
        print("无目标")
        return 2

    results = []
    for t in targets:
        r = probe(t)
        results.append(r)
        if not a.json:
            tag = {"V2_OK": "✅", "NOT_V2": "⛔", "WRONG_VERSION": "🔶", "UNAVAILABLE": "⚠️"}[r["status"]]
            sym = f"{r['symbol0']}/{r['symbol1']}" if r.get("symbol0") else ""
            print(f"{tag} {r['status']:12s} {t}  {sym:16s} {r['reason'] or ''}")

    s = summarize(results)
    if a.json:
        print(json.dumps({"summary": s, "results": results}, indent=2, ensure_ascii=False))
    else:
        print("\n" + "=" * 62)
        print(f"合计 {s['total']}  =  可算V2 {s['V2_OK']}  +  版本不符 {s['WRONG_VERSION']}  "
              f"+  非池合约 {s['NOT_V2']}  +  不可达 {s['UNAVAILABLE']}")
        print(f"分母闭合：{'✅' if s['denominator_closed'] else '🔴 不闭合'}")
        print(f"可直接定 USD：{s['priced']}")
        print("=" * 62)

    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"summary": s, "results": results}, f, indent=2, ensure_ascii=False)
        print(f"落盘 {a.out}")

    return 0 if s["denominator_closed"] else 1


if __name__ == "__main__":
    sys.exit(main())

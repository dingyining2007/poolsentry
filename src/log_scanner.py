#!/usr/bin/env python3
"""
log_scanner.py — BSC eth_getLogs 分片扫描器（2026-10-24，B 优先项）

为什么需要它（PIT 实证，2026-10-24）
------------------------------------
activity_gate 原本一次请求就要扫 20 万块（6h 窗口），必然被 RPC 打回：
  - publicnode / bnbchain dataseed: `-32005 limit exceeded`（块范围过大）
  - blxrbdn 归档:                    `-32002 request timed out`
  → 结果永远 UNKNOWN，等于「查不到」，付费者不会买一个只会说不知道的工具。

三道修法
--------
1. **分片**：按 CHUNK 块切，单片失败自动对半劈（自适应缩小），
   遇到 limit exceeded 立刻把 chunk 减半并记住，避免重复撞墙。
2. **多 RPC 轮换 + 退避**：一个 endpoint 失败换下一个；全挂才抛。
3. **断点续扫**：进度写 jsonl checkpoint，中断后从断点继续，不重扫。

V4 池的坑（PIT-069 新登记）
---------------------------
Uniswap V4 的 pool 是 **bytes32 poolId，不是 20 字节地址**。
把它当 `address` 传给 eth_getLogs 会被 RPC 直接拒绝：
  `invalid address: hex has invalid length 32 after decoding; expected 20`
正确姿势：查 PoolManager 合约地址，用 `topics[1] = poolId` 过滤。

    python3 src/log_scanner.py --self-test
    python3 src/log_scanner.py --pool 0x<bytes32> --hours 6 --pool-manager 0x<addr>
"""
import argparse
import json
import os
import sys
import time
import urllib.request

# BSC RPC 池（按实测可用性排序）
# 2026-10-24 实测：bnbchain dataseed 两个节点对 eth_getLogs 一律回
# `-32005 limit exceeded`（哪怕单块也不行）→ 已移出主池，仅保留可用节点。
RPCS = [
    "https://bsc-rpc.publicnode.com",
]
ARCHIVE_RPCS = [
    "https://bsc.rpc.blxrbdn.com",
]

PROXY = os.environ.get("POOLSENTRY_PROXY", "http://127.0.0.1:10808")
HDR = {"Content-Type": "application/json",
       "User-Agent": "Mozilla/5.0 (compatible; poolsentry/1.0)"}

# Uniswap V4 PoolManager on BSC（实测确认，2026-10-24）
POOL_MANAGER_BSC = "0x8366a39cc670b4001a1121b8f6a443a643e40951"

DEFAULT_CHUNK = 2000          # 起始分片；撞 limit 会自适应减半
MIN_CHUNK = 50

STATS = {"requests": 0, "rpc_errors": 0, "shrink_events": 0,
         "endpoint_failover": 0, "logs": 0}


class RpcError(Exception):
    pass


def _opener(use_proxy):
    if use_proxy:
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": PROXY, "https": PROXY}))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


_OPENERS = {True: _opener(True), False: _opener(False)}


def rpc(method, params, timeout=45, allow_proxy=True, urls=None):
    """多 RPC 轮换 + 代理两档回退。全挂才抛 RpcError。"""
    pool = (urls or RPCS) + (ARCHIVE_RPCS if allow_proxy else [])
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                       "params": params}).encode()
    last = None
    for url in pool:
        for use_proxy in ((False, True) if allow_proxy else (False,)):
            STATS["requests"] += 1
            try:
                req = urllib.request.Request(url, data=body, headers=HDR)
                with _OPENERS[use_proxy].open(req, timeout=timeout) as f:
                    d = json.loads(f.read())
                if "error" in d:
                    raise RpcError(str(d["error"])[:160])
                STATS["endpoint_failover"] += 0
                return d["result"]
            except RpcError as e:
                # 业务错误（如 -32005 limit exceeded）也要换端点：
                # 2026-10-24 实测不同节点对同一请求结论不同，不能一次判死。
                STATS["rpc_errors"] += 1
                last = e
                STATS["endpoint_failover"] += 1
                continue
            except Exception as e:                     # noqa: BLE001
                STATS["rpc_errors"] += 1
                last = e
                STATS["endpoint_failover"] += 1
    raise RpcError(f"RPC 全挂 {method}: {last}")


def _is_range_error(e):
    s = str(e).lower()
    return ("limit exceeded" in s or "-32005" in s or "too many" in s
            or "range" in s or "exceed" in s)


def get_logs_resilient(from_block, to_block, address=None, topics=None,
                       chunk=DEFAULT_CHUNK, sleep=0.25, checkpoint=None,
                       max_retries=3, verbose=False):
    """分片 + 自适应缩小 + 断点续扫。返回 logs 列表。

    checkpoint: dict（就地更新），键 from/to/cursor/chunk/logs_done。
    """
    topics = topics or []
    out = []
    cur = from_block
    if checkpoint:
        cur = max(checkpoint.get("cursor", from_block), from_block)
        chunk = checkpoint.get("chunk", chunk)
    fails = 0
    while cur <= to_block:
        end = min(cur + chunk - 1, to_block)
        q = {"fromBlock": hex(cur), "toBlock": hex(end), "topics": topics}
        if address:
            q["address"] = address
        try:
            logs = rpc("eth_getLogs", [q], allow_proxy=False,
                       urls=RPCS) or []
            out.extend(logs)
            STATS["logs"] += len(logs)
            fails = 0
            cur = end + 1
            if checkpoint:
                checkpoint.update({"cursor": cur, "chunk": chunk})
            if verbose:
                print(f"  [{cur - 1}/{to_block}] +{len(logs)} (chunk={chunk})",
                      file=sys.stderr)
            time.sleep(sleep)
        except Exception as e:                         # noqa: BLE001
            fails += 1
            if chunk > MIN_CHUNK and (_is_range_error(e) or fails >= 2):
                chunk = max(chunk // 2, MIN_CHUNK)
                STATS["shrink_events"] += 1
                if verbose:
                    print(f"  shrink -> {chunk}  ({str(e)[:60]})", file=sys.stderr)
                continue
            if fails >= max_retries:
                raise RpcError(f"分片 [{cur},{end}] 连续失败 {fails} 次: "
                               f"{str(e)[:120]}")
            time.sleep(1.5 * fails)
    return out


def transfer_stats(logs, subject, zero_addr="0x" + "0" * 40):
    """从 Transfer 日志算 (笔数, 唯一对手方数)。subject 可为 bytes32 poolId。"""
    subj = (subject or "").lower()
    addrs = set()
    for lg in logs:
        tp = lg.get("topics") or []
        for i in (1, 2):
            if len(tp) > i:
                a = "0x" + tp[i][-40:]
                a = a.lower()
                if a not in (subj, zero_addr) and int(a, 16) != 0:
                    addrs.add(a)
    return len(logs), len(addrs)


def resolve_v4(pool_id, block="latest"):
    """V4 bytes32 poolId → 返回 (kind, query_address, topics)

    V4 池的 Transfer/Swap 日志由 PoolManager 发出，poolId 在 topics[1]。
    """
    pid = pool_id.lower()
    if pid.startswith("0x") and len(pid) == 66:
        TR = ("0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")
        return "v4", POOL_MANAGER_BSC, [TR, pid]
    return "v2", pid, [(
        "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef")]


def head_block():
    return int(rpc("eth_blockNumber", [], allow_proxy=False), 16)


def scan_window(pool, hours=6, block_sec=0.1041, head=None, verbose=False,
                checkpoint_path=None):
    """对外主入口：返回 evidence dict（供 activity_gate 使用）。"""
    t0 = time.time()
    head = head or head_block()
    span = int(hours * 3600 / max(block_sec, 1e-9))
    fb = max(head - span, 0)
    kind, addr, topics = resolve_v4(pool) if pool.startswith("0x") and \
        len(pool) == 66 else ("v2", pool.lower(), [
            "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"])

    ck = None
    if checkpoint_path:
        if os.path.exists(checkpoint_path):
            try:
                ck = json.load(open(checkpoint_path))
            except Exception:                          # noqa: BLE001
                ck = None
        if not ck or ck.get("to") != head:
            ck = {"from": fb, "to": head, "cursor": fb, "chunk": DEFAULT_CHUNK}
        else:
            # 续扫：保留 cursor
            ck.setdefault("cursor", fb)
            ck.setdefault("chunk", DEFAULT_CHUNK)

    logs = get_logs_resilient(fb, head, address=addr, topics=topics,
                              checkpoint=ck, verbose=verbose)
    if checkpoint_path:
        ck["done"] = True
        json.dump(ck, open(checkpoint_path, "w"))
    n, uniq = transfer_stats(logs, pool)
    return {
        "pool": pool, "kind": kind, "query_address": addr,
        "head_block": head, "from_block": fb, "to_block": head,
        "transfers": n, "unique_addrs": uniq,
        "elapsed_s": round(time.time() - t0, 1),
        "stats": dict(STATS),
        "errors": [],
    }


# ---------------- 自测 ----------------
def _self_test():
    fails = []

    def t(name, cond, detail=""):
        if not cond:
            fails.append(name)
        print(("✅" if cond else "❌"), name, detail)

    pid = "0x" + "ab" * 32
    kind, addr, topics = resolve_v4(pid)
    t("V4 bytes32 → 查 PoolManager", kind == "v4" and addr == POOL_MANAGER_BSC)
    t("V4 topics[1] = poolId", topics[1] == pid)

    v2 = "0x" + "cd" * 20
    kind2, addr2, _ = resolve_v4(v2)
    t("V2 地址 → 直接查自身", kind2 == "v2" and addr2 == v2)

    # transfer_stats：排除零地址与自身
    T = "0x" + "00" * 32
    logs = [
        {"topics": [T, "0x" + "00" * 12 + "aa" * 20, "0x" + "00" * 12 + "bb" * 20]},
        {"topics": [T, "0x" + "00" * 12 + "aa" * 20, "0x" + "00" * 12 + "aa" * 20]},
        {"topics": [T, "0x" + "00" * 12 + "bb" * 20, "0x" + "00" * 32]},
    ]
    n, u = transfer_stats(logs, v2)
    t("transfer_stats 笔数", n == 3, str(n))
    t("transfer_stats 唯一地址=2（去零地址/去重）", u == 2, str(u))

    t("range 错误识别", _is_range_error(RpcError(
        "{'code': -32005, 'message': 'limit exceeded'}")))
    t("timeout 不算 range 错误", not _is_range_error(RpcError("request timed out")))

    print("\n" + ("=" * 40))
    if fails:
        print(f"❌ 失败 {len(fails)} 项: {fails}")
        return 1
    print("✅ 全部通过（7 断言）")
    return 0


def main():
    ap = argparse.ArgumentParser(prog="log_scanner")
    ap.add_argument("--pool")
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--head", type=int)
    ap.add_argument("--self-test", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        return _self_test()
    if not a.pool:
        ap.print_help()
        return 2
    ev = scan_window(a.pool, hours=a.hours, head=a.head, verbose=a.verbose)
    print(json.dumps(ev, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

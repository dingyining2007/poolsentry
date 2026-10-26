#!/usr/bin/env python3
"""hypersync_logs.py — 用 Envio HyperSync 秒级拉取池子日志（替代 RPC 分片）。

为什么需要它（2026-10-24 实测）：
  `turnover_24h.py` 走 blxrbdn RPC 分片 2000 块 + 3 次重试，拉 24h Swap：
    → 185 秒，16 片里成功 16、但 `partial=true`（预算耗尽提前收工），
      换手率被系统性低估，`basis` 只能标「不完整」。
  这个延迟在「客户查一个池子」的路径上不可接受，且数据不完整。

HyperSync 的定位（必须说清，别再吹错）：
  ✅ 解决「能拿到全窗口数据、没有块范围上限」
  ❌ **不是「秒级」**——实测 WBNB 20 万块 Transfer 为 632 秒 / 397 页。
     单页上限约 7000 条，页面往返次数就是瓶颈。
  所以本模块的正确用法是：**夜间批量预计算落库**，客户查询读本地库（毫秒）。
  在线路径用它时，必须限制窗口与返回量。

设计约束：
  1. 不引入第三方依赖（纯 stdlib + urllib），因为裸 HTTP 有分块传输兼容问题，
     改用「一次拿全、不用 chunked」的显式 Accept-Encoding: identity。
  2. token 只从环境变量 / .secrets 读，**禁止硬编码**。
  3. 取不到数据 → 返回 error，调用方必须给 `UNKNOWN`，**不许 PASS**。

用法：
  python hypersync_logs.py --pair 0x池 --hours 6
  python hypersync_logs.py --pair 0x池 --hours 6 --json
"""
import argparse
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HS_URL = "https://bsc.hypersync.xyz"
WS = Path(os.environ.get("OPENCLAW_WS", "/root/.openclaw/workspace"))
SECRET_ENV = WS / ".secrets/envio/envio.env"

# 单次 page 大小：HyperSync 内部上限约 7000 条，显式给 5000 更稳
PAGE_HINT = 5000
MAX_PAGES = 400          # 硬闸，防跑飞
RETRY = 3
RETRY_BACKOFF = [2, 6, 15]

SWAP_TOPIC = "0xd78ad95fa46c994b6551d0da85fc275fe613ce37657fb8d5e3d130840159d822"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def load_token():
    """token 来源优先级：环境变量 > .secrets 文件。读不到返回 None（不抛）。"""
    t = os.environ.get("ENVIO_API_TOKEN", "").strip()
    if t:
        return t
    if SECRET_ENV.exists():
        for line in SECRET_ENV.read_text(errors="ignore").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("ENVIO_API_TOKEN="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v and not v.endswith("…") and len(v) > 20:
                    return v
                return None      # 占位值（如 51d055…4ab8）不是真 token
    return None


def _raw_request(url, headers, body=None, timeout=120):
    """裸 http.client 请求：显式读全长，规避 urllib 对 chunked 响应的 IncompleteRead。

    2026-10-24 实测：urllib 读 HyperSync 响应在 ~683KB 处抛 IncompleteRead，
    响应是 chunked 编码且服务端提前关连接。http.client 的 getresponse().read()
    对已知 Content-Length / 读到 EOF 的容错更好。
    """
    import http.client
    from urllib.parse import urlsplit
    u = urlsplit(url)
    conn = http.client.HTTPSConnection(u.netloc, timeout=timeout)
    try:
        path = u.path + (("?" + u.query) if u.query else "")
        conn.request("POST" if body is not None else "GET", path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, raw
    finally:
        conn.close()


class TokenMissing(RuntimeError):
    pass


def _post(path, body, token, timeout=120):
    hdr = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        # identity：禁用 gzip，规避此前 urllib 分块读取 IncompleteRead
        "Accept-Encoding": "identity",
        "User-Agent": "poolsentry/1.0",
    }
    data = json.dumps(body).encode()
    last = ""
    for attempt in range(RETRY):
        try:
            code, raw = _raw_request(HS_URL + path, hdr, data, timeout)
            if code >= 400:
                raise urllib.error.HTTPError(HS_URL + path, code, "err", None, None)
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            return json.loads(raw)
        except urllib.error.HTTPError as e:
            body_txt = ""
            try:
                body_txt = e.read()[:300].decode(errors="ignore")
            except Exception:                            # noqa: BLE001
                pass
            if e.code == 401:
                raise TokenMissing(f"HTTP 401 token 无效：{body_txt[:160]}")
            last = f"HTTP {e.code} {body_txt[:120]}"
        except Exception as e:                           # noqa: BLE001
            last = f"{type(e).__name__}: {str(e)[:120]}"
        if attempt < RETRY - 1:
            time.sleep(RETRY_BACKOFF[attempt])
    raise RuntimeError(f"HyperSync 查询失败（重试{RETRY}次）：{last}")


def get_height(token=None, timeout=30):
    token = token or load_token()
    if not token:
        raise TokenMissing("缺少 ENVIO_API_TOKEN")
    hdr = {"Authorization": f"Bearer {token}", "Accept-Encoding": "identity",
           "User-Agent": "poolsentry/1.0"}
    code, raw = _raw_request(HS_URL + "/height", hdr, None, timeout)
    if code >= 400:
        raise RuntimeError(f"HTTP {code}")
    return json.loads(raw)["height"]


def fetch_logs(address, topics, from_block, to_block,
               token=None, max_pages=MAX_PAGES, field_selection=None):
    """拉 [from_block, to_block] 内匹配的日志。

    topics: [[topic0, ...]] 形式，直接传给 HyperSync 的 LogSelection。
    返回 (logs, meta)。logs 为原始 dict 列表；meta 含页数与耗时，供上层判「是否被截断」。
    """
    token = token or load_token()
    if not token:
        raise TokenMissing("缺少 ENVIO_API_TOKEN（.secrets/envio/envio.env 或环境变量）")

    if field_selection is None:
        field_selection = {"log": ["block_number", "log_index", "transaction_hash",
                                   "address", "data", "topic0", "topic1", "topic2", "topic3"]}

    q = {
        "from_block": int(from_block),
        "to_block": int(to_block) + 1,        # HyperSync 右开区间
        "logs": [{"address": [address], "topics": topics}],
        "field_selection": field_selection,
    }

    logs, pages, cur = [], 0, int(from_block)
    t0 = time.time()
    truncated = False
    while cur <= to_block:
        q["from_block"] = cur
        r = _post("/query", q, token)
        # ⚠️ 2026-10-24 实测修正：HyperSync 响应是 {"data": {"logs": [...], ...}}，
        # 不是 {"data": [...]}。按 list 取会永远得到 0 条 → 死盘判据全部误判 DEAD。
        # ⚠️ 2026-10-24 实测修正（连踩三次）：
        #   ① 根级是 {"data": {"logs": [...], "next_block": N}}，不是 {"data": [...]}
        #   ② data.logs 里每个元素仍是 {"logs":[{...}]} 双层包裹，必须再拆一层
        #   ③ next_block 在 data 里，不在根级
        # 任一层解析错，结果都是 0 条 → 死盘判据把活池判成 DEAD。
        data = r.get("data")
        if isinstance(data, dict):
            got = data.get("logs") or []
            nxt_block = data.get("next_block")
        else:
            got = data or []
            nxt_block = r.get("next_block")

        flat = []
        for item in got:
            if isinstance(item, dict) and "logs" in item:
                flat.extend(item["logs"] or [])
            elif isinstance(item, dict):
                flat.append(item)
        logs.extend(flat)
        pages += 1
        nxt = nxt_block
        if not flat:
            break
        if nxt is None or nxt <= cur:
            break
        cur = nxt
        if pages >= max_pages:
            truncated = cur <= to_block
            break

    return logs, {
        "pages": pages,
        "elapsed_s": round(time.time() - t0, 2),
        "truncated": truncated,
        "from_block": int(from_block),
        "to_block": int(to_block),
        "source": f"{HS_URL} HyperSync /query（token 认证）",
    }


def fetch_swaps(pair, hours=6, token=None, max_pages=MAX_PAGES):
    """拉池子最近 hours 小时的 Swap 日志。"""
    head = get_height(token)
    # BSC 实测平均出块 0.45s（PIT-102：不能按 0.75s 假设，会低估 40%）
    span = int(hours * 3600 / 0.45)
    from_blk = max(0, head - span)
    logs, meta = fetch_logs(pair, [[SWAP_TOPIC]], from_blk, head,
                            token=token, max_pages=max_pages)
    meta.update({"head_block": head, "hours": hours})
    return logs, meta


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pair", required=True)
    ap.add_argument("--hours", type=float, default=6)
    ap.add_argument("--kind", choices=["swap", "transfer"], default="swap")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    try:
        if a.kind == "swap":
            logs, meta = fetch_swaps(a.pair, a.hours)
        else:
            head = get_height()
            span = int(a.hours * 3600 / 0.45)
            logs, meta = fetch_logs(a.pair, [[TRANSFER_TOPIC]], head - span, head)
    except TokenMissing as e:
        print(json.dumps({"error": "TOKEN_MISSING", "message": str(e)}, ensure_ascii=False))
        return 3
    except Exception as e:                               # noqa: BLE001
        print(json.dumps({"error": "QUERY_FAILED", "message": str(e)[:240]}, ensure_ascii=False))
        return 1

    out = {"pair": a.pair, "kind": a.kind, "count": len(logs), **meta}
    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
    else:
        print(f"池 {a.pair}  {a.kind}  {a.hours}h")
        print(f"  {len(logs)} 条  {meta['pages']} 页  {meta['elapsed_s']}s"
              f"{'  ⚠️ 被 max_pages 截断' if meta['truncated'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

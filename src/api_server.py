#!/usr/bin/env python3
"""api_server.py — PoolSentry 订阅 API（stdlib http.server，零依赖）。

职责边界（刻意做窄）：
  ✅ 鉴权（API Key）→ 配额检查 → 读缓存 → 返回结果 → 记账
  ❌ **不在请求里扫链**。缓存没命中就返回 202 + `PENDING_BATCH`，
     由夜间批量任务填充。这样客户端等待时间永远是毫秒级，
     而不是「换数据源也救不了」的 184~269 秒。

启动：
  python3 src/api_server.py --port 8088
  python3 src/api_server.py --port 8088 --host 127.0.0.1

认证：
  请求头 `X-API-Key: ps_xxx`
  管理操作（建账号/发 key）需 `X-Admin-Token`，从 .secrets 或环境变量读。

接口：
  GET  /health                       无需认证
  GET  /v1/pool/<地址>               查池子（走缓存，计配额）
  GET  /v1/account                   我的账号/配额/keys
  POST /v1/watch/<地址>              加入监控
  GET  /v1/watch                     我的监控列表
  GET  /v1/plans                     套餐列表（公开）
  --- 管理 ---
  POST /admin/account                建账号 {"email":..., "plan":...}
  POST /admin/key                    发 key {"account_id":...}
"""
import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import cache           # noqa: E402
import subscriber as sub  # noqa: E402

ADMIN_TOKEN_FILE = ROOT.parent.parent / ".secrets/poolsentry/admin.env"
VERSION = "1.0.0"


def _admin_token():
    t = os.environ.get("POOLSENTRY_ADMIN_TOKEN", "").strip()
    if t:
        return t
    if ADMIN_TOKEN_FILE.exists():
        for line in ADMIN_TOKEN_FILE.read_text(errors="ignore").splitlines():
            line = line.strip()
            if line.startswith("POOLSENTRY_ADMIN_TOKEN="):
                v = line.split("=", 1)[1].strip().strip('"').strip("'")
                if v and len(v) > 12:
                    return v
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = f"PoolSentry/{VERSION}"

    # ── 工具 ──
    def _send(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False, indent=1).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        key = self.headers.get("X-API-Key", "").strip()
        acc, err = sub.verify_key(key)
        if acc is None:
            self._send(401, {"error": err,
                             "hint": "请求头需带 X-API-Key: ps_xxx",
                             "docs": "/v1/plans"})
            return None
        return acc

    def _auth_admin(self):
        want = _admin_token()
        if not want:
            self._send(503, {"error": "admin_token_not_configured",
                             "hint": f"设置 POOLSENTRY_ADMIN_TOKEN 环境变量或写入 {ADMIN_TOKEN_FILE}"})
            return False
        got = self.headers.get("X-Admin-Token", "").strip()
        if got != want:
            self._send(403, {"error": "admin_token_invalid"})
            return False
        return True

    def _body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n) or b"{}")
        except Exception:                                # noqa: BLE001
            return {}

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[api] {self.address_string()} {fmt % args}\n")

    # ── 路由 ──
    def do_GET(self):
        path = unquote(urlsplit(self.path).path)

        if path in ("/health", "/healthz"):
            return self._send(200, {"ok": True, "version": VERSION,
                                    "ts": int(time.time()),
                                    "cache": cache.stats()})

        if path == "/v1/plans":
            return self._send(200, {"plans": sub.PLANS,
                                    "_note": "月费为人民币；额度按「可监控池数 + 刷新频率」计，不按单次查询卖"})

        if path == "/v1/account":
            acc = self._auth()
            if not acc:
                return
            return self._send(200, sub.account_summary(acc["id"]))

        if path == "/v1/watch":
            acc = self._auth()
            if not acc:
                return
            return self._send(200, {"pools": sub.list_watch(acc["id"])})

        if path.startswith("/v1/pool/"):
            return self._get_pool(path[len("/v1/pool/"):])

        return self._send(404, {"error": "not_found", "path": path})

    def _get_pool(self, pool):
        acc = self._auth()
        if not acc:
            return
        pool = (pool or "").strip().lower()
        if not pool.startswith("0x") or len(pool) != 42:
            return self._send(400, {"error": "bad_pool_address",
                                    "hint": "需要 0x 开头的 42 位池地址"})

        allow, qinfo = sub.check_quota(acc, "query")
        if not allow:
            return self._send(429, {"error": "quota_exceeded", "quota": qinfo,
                                    "hint": "升级套餐或等次日重置"})

        data = cache.get(pool, max_age_s=cache.TTL_WARM)
        sub.record_usage(acc["id"], "query", {"pool": pool})

        if data is None:
            # 关键设计：**不在这里扫链**。184~269 秒的等待不能在客户请求里发生。
            return self._send(202, {
                "status": "PENDING_BATCH",
                "pool": pool,
                "message": ("该池尚未预计算。夜批任务会自动填充；"
                            "在线路径不扫链，因为实测 184~269 秒，用户等不了。"),
                "eta": "下一个夜批周期",
                "quota": qinfo,
                "_basis": "【实测】RPC 185s / HyperSync 184s，换数据源无效",
            })

        meta = data.pop("_cache", {})
        return self._send(200, {
            "status": "OK",
            "pool": pool,
            "result": data,
            "cache": meta,
            "quota": qinfo,
            "_disclaimer": ("本结果基于链上点快照 + 本池单跳闭式解，"
                            "不构成投资建议。ALIVE ≠ 安全，NORMAL ≠ 无风险。"),
        })

    def do_POST(self):
        path = unquote(urlsplit(self.path).path)

        if path == "/admin/account":
            if not self._auth_admin():
                return
            b = self._body()
            try:
                aid = sub.create_account(b.get("email"), b.get("plan", "free"),
                                         b.get("expires_at"), b.get("note"))
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(201, {"account_id": aid,
                                    "summary": sub.account_summary(aid)})

        if path == "/admin/key":
            if not self._auth_admin():
                return
            b = self._body()
            try:
                k = sub.issue_key(int(b.get("account_id") or 0))
            except (ValueError, TypeError) as e:
                return self._send(400, {"error": str(e)})
            return self._send(201, k)

        if path.startswith("/v1/watch/"):
            acc = self._auth()
            if not acc:
                return
            pool = path[len("/v1/watch/"):].strip().lower()
            allow, winfo = sub.check_quota(acc, "watch_add")
            if not allow:
                return self._send(429, {"error": "watch_limit_reached", "quota": winfo,
                                        "hint": "升级套餐可监控更多池"})
            try:
                added = sub.add_watch(acc["id"], pool)
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            return self._send(201 if added else 200,
                              {"added": added, "pool": pool, "quota": winfo})

        return self._send(404, {"error": "not_found", "path": path})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8088)
    a = ap.parse_args()
    if not _admin_token():
        print("⚠️  未配置 POOLSENTRY_ADMIN_TOKEN，管理接口不可用（查询接口正常）", flush=True)
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"PoolSentry API v{VERSION} → http://{a.host}:{a.port}", flush=True)
    print(f"  缓存库 {cache.DB_PATH}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n停止", flush=True)


if __name__ == "__main__":
    main()

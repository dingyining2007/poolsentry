#!/usr/bin/env python3
"""subscriber.py — 订阅后端数据层：客户 / API Key / 配额 / 用量（SQLite，零依赖）。

对应产品路线决策（2026-10-24）：**B 路线＝按月订阅 + 配额**。

为什么不用 FastAPI（先说清，免得日后被问）：
  这一层只做「认证 + 配额 + 记账」，全是 SQLite 读写，**用 stdlib http.server 就够**。
  引入 FastAPI 要多装 uvicorn/pydantic 两个依赖、多一套 venv，
  而本产品对外的卖点是「零依赖、可自托管」。
  → 先零依赖落地；若日后要横向扩容再迁 FastAPI，本文件的数据层不用改。

费率设计（可改，见 PLANS）：
  免费档用于引流 + 验证需求；付费档按「监控池数量 + 刷新频率」分层，
  因为夜批预计算的成本模型就是这两项，不按查询次数卖 —— 否则客户会为「刷新一下」付费，
  而我们实际不为单次查询付成本，定价与成本结构脱节。

安全约束：
  - API Key 只存 sha256 哈希，**明文只在创建时返回一次**（与 Envio/Stripe 同做法）
  - Key 前缀索引（前 8 位）用于快速定位，无敏感信息
  - 所有配额判断走单一函数 `check_quota()`，禁止散落各处
"""
import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path

WS = Path(__file__).resolve().parent.parent
DB_PATH = WS / "state" / "subscribers.db"

# ── 套餐定义（改这里就能改定价，无需动代码逻辑）──
PLANS = {
    # 2026-10-24 v1.1 三方评审修订：三 SKU 拆分，取消 pro 档（成本不可持续）
    # 关键设计：免费档【不延迟】，把"实时的价值感"免费给出，把"持续盯+告警+历史"收起来卖
    "free": {
        "name": "免费",
        "price_cny_month": 0,
        "max_watch_pools": 0,          # 不提供监控（监控即付费价值）
        "refresh_hours": 0,            # 0 = 实时单池快照，不做夜批
        "queries_per_day": 100,        # 不限额池数，限次数
        "history_days": 0,             # 不给历史
        "export": False,
        "alert_push": False,
    },
    "single": {
        "name": "单次报告",
        "price_cny_month": 0,          # 按次计费，见 price_cny_per_call
        "price_cny_per_call": 19.9,
        "max_watch_pools": 0,
        "refresh_hours": 0,
        "queries_per_day": 0,          # 0 = 不限（按次扣费，不设日限）
        "history_days": 7,             # 关键：带7天历史，像"能一直看着"不像"一次性答题"
        "export": True,
        "alert_push": False,
        "upgrade_hint": "订阅制监控，1 小时刷新 + 异常推送",
    },
    "monitor": {
        "name": "监控订阅",
        "price_cny_month": 199,
        "max_watch_pools": 20,         # 300 → 20：卖"保安"不卖"仓库"
        "refresh_hours": 1,
        "queries_per_day": 2000,
        "history_days": 90,
        "export": True,
        "alert_push": True,
    },
    "api": {
        "name": "API 包",
        "price_cny_month": 0,
        "price_cny_per_call": 0.5,     # 充值包 ¥99 起，按次扣
        "max_watch_pools": 0,
        "refresh_hours": 0,
        "queries_per_day": 0,
        "history_days": 30,
        "export": True,
        "alert_push": False,
    },
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS account (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT UNIQUE NOT NULL,
    plan         TEXT NOT NULL DEFAULT 'free',
    created_at   INTEGER NOT NULL,
    expires_at   INTEGER,                 -- 付费到期；NULL=永久有效(免费档)
    active       INTEGER NOT NULL DEFAULT 1,
    note         TEXT
);
CREATE TABLE IF NOT EXISTS api_key (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL,
    key_prefix   TEXT NOT NULL,           -- 前 8 位明文，仅用于定位
    key_hash     TEXT UNIQUE NOT NULL,    -- sha256(完整key)
    created_at   INTEGER NOT NULL,
    revoked      INTEGER NOT NULL DEFAULT 0,
    last_used_at INTEGER,
    FOREIGN KEY(account_id) REFERENCES account(id)
);
CREATE TABLE IF NOT EXISTS usage (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL,
    ts           INTEGER NOT NULL,
    action       TEXT NOT NULL,           -- 'query' | 'watch_add' | ...
    detail       TEXT,
    cost_units   INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS watch (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   INTEGER NOT NULL,
    pool         TEXT NOT NULL,
    added_at     INTEGER NOT NULL,
    UNIQUE(account_id, pool)
);
CREATE INDEX IF NOT EXISTS idx_usage_acc_ts ON usage(account_id, ts);
CREATE INDEX IF NOT EXISTS idx_key_hash ON api_key(key_hash);
"""


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    return c


def _hash_key(key):
    return hashlib.sha256(key.encode()).hexdigest()


# ── 账号 ──
def create_account(email, plan="free", expires_at=None, note=None):
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise ValueError("email 无效")
    if plan not in PLANS:
        raise ValueError(f"未知套餐：{plan}")
    with _conn() as c:
        try:
            cur = c.execute(
                "INSERT INTO account(email, plan, created_at, expires_at, note) VALUES(?,?,?,?,?)",
                (email, plan, int(time.time()), expires_at, note))
        except sqlite3.IntegrityError:
            raise ValueError(f"账号已存在：{email}")
        return cur.lastrowid


def get_account(account_id):
    with _conn() as c:
        r = c.execute("SELECT * FROM account WHERE id=?", (account_id,)).fetchone()
    return dict(r) if r else None


def plan_of(account):
    """返回生效套餐定义；付费过期自动降级为 free（不静默：返回值带 downgraded 标记）。"""
    acc_plan = (account or {}).get("plan", "free")
    exp = (account or {}).get("expires_at")
    if acc_plan != "free" and exp and int(exp) < int(time.time()):
        p = dict(PLANS["free"])
        p["_downgraded_from"] = acc_plan
        p["_downgraded_at"] = exp
        return p
    return dict(PLANS.get(acc_plan, PLANS["free"]))


# ── API Key ──
def issue_key(account_id, hint=""):
    """发新 key。明文只在这里返回一次，之后库里只有哈希。"""
    acc = get_account(account_id)
    if not acc:
        raise ValueError(f"账号不存在：{account_id}")
    key = "ps_" + secrets.token_urlsafe(32)
    prefix = key[:8]
    with _conn() as c:
        c.execute(
            "INSERT INTO api_key(account_id, key_prefix, key_hash, created_at) VALUES(?,?,?,?)",
            (account_id, prefix, _hash_key(key), int(time.time())))
    return {"api_key": key, "prefix": prefix, "account_id": account_id,
            "_note": "明文仅此一次，请立即保存。库内只存 sha256。"}


def verify_key(key):
    """校验 key → 返回 (account_dict, None) 或 (None, 错误原因)。"""
    if not key or not key.startswith("ps_"):
        return None, "invalid_format"
    h = _hash_key(key)
    with _conn() as c:
        r = c.execute(
            "SELECT * FROM api_key WHERE key_hash=? AND revoked=0", (h,)).fetchone()
        if not r:
            return None, "invalid_key"
        c.execute("UPDATE api_key SET last_used_at=? WHERE id=?", (int(time.time()), r["id"]))
        acc = c.execute("SELECT * FROM account WHERE id=?", (r["account_id"],)).fetchone()
    if not acc:
        return None, "account_missing"
    if not acc["active"]:
        return None, "account_inactive"
    return dict(acc), None


def revoke_key(key):
    with _conn() as c:
        cur = c.execute("UPDATE api_key SET revoked=1 WHERE key_hash=?", (_hash_key(key),))
    return cur.rowcount > 0


# ── 配额 ──
def check_quota(account, action="query"):
    """单一配额判定入口。返回 (allow: bool, info: dict)。

    任何配额判断都必须走这里，禁止各调用点自己数 —— 否则口径必然分叉。
    """
    plan = plan_of(account)
    today0 = int(time.time()) // 86400 * 86400
    with _conn() as c:
        if action == "query":
            used = c.execute(
                "SELECT COUNT(*) FROM usage WHERE account_id=? AND ts>=? AND action='query'",
                (account["id"], today0)).fetchone()[0]
            limit = plan["queries_per_day"]
            return used < limit, {
                "action": action, "used_today": used, "limit": limit,
                "remaining": max(0, limit - used), "plan": plan["name"],
                "reset_at": today0 + 86400,
                **({"downgraded_from": plan["_downgraded_from"]} if "_downgraded_from" in plan else {}),
            }
        if action == "watch_add":
            used = c.execute("SELECT COUNT(*) FROM watch WHERE account_id=?",
                             (account["id"],)).fetchone()[0]
            limit = plan["max_watch_pools"]
            return used < limit, {"action": action, "used": used, "limit": limit,
                                  "remaining": max(0, limit - used), "plan": plan["name"]}
    return False, {"action": action, "reason": "unknown_action"}


def record_usage(account_id, action="query", detail=None, cost_units=1):
    with _conn() as c:
        c.execute(
            "INSERT INTO usage(account_id, ts, action, detail, cost_units) VALUES(?,?,?,?,?)",
            (account_id, int(time.time()), action,
             json.dumps(detail, ensure_ascii=False) if detail else None, cost_units))


def add_watch(account_id, pool):
    pool = (pool or "").lower()
    if not pool.startswith("0x") or len(pool) != 42:
        raise ValueError("池地址格式无效")
    with _conn() as c:
        try:
            c.execute("INSERT INTO watch(account_id, pool, added_at) VALUES(?,?,?)",
                      (account_id, pool, int(time.time())))
        except sqlite3.IntegrityError:
            return False            # 已存在
    return True


def list_watch(account_id):
    with _conn() as c:
        rows = c.execute("SELECT pool, added_at FROM watch WHERE account_id=? ORDER BY added_at",
                         (account_id,)).fetchall()
    return [dict(r) for r in rows]


def account_summary(account_id):
    acc = get_account(account_id)
    if not acc:
        return None
    plan = plan_of(acc)
    allow_q, qinfo = check_quota(acc, "query")
    allow_w, winfo = check_quota(acc, "watch_add")
    with _conn() as c:
        keys = c.execute(
            "SELECT key_prefix, created_at, last_used_at, revoked FROM api_key "
            "WHERE account_id=? ORDER BY id DESC", (account_id,)).fetchall()
    return {
        "account": acc,
        "plan": plan,
        "plan_downgraded": "_downgraded_from" in plan,
        "quota_query": qinfo,
        "quota_watch": winfo,
        "keys": [dict(k) for k in keys],
    }


if __name__ == "__main__":
    # 自测：建账号 → 发 key → 校验 → 配额 → 用超
    import shutil
    bak = None
    if DB_PATH.exists():
        bak = DB_PATH.with_suffix(".db.selftest_bak")
        shutil.copy(DB_PATH, bak)
    try:
        fails = []

        def t(name, cond, detail=""):
            if not cond:
                fails.append(f"{name} {detail}")
            print(("✅" if cond else "❌"), name, detail)

        aid = create_account("selftest@example.com", "free")
        t("建账号", aid > 0, f"id={aid}")

        k = issue_key(aid)
        t("发 key", k["api_key"].startswith("ps_"), k["prefix"])

        acc, err = verify_key(k["api_key"])
        t("校验 key 通过", acc is not None and err is None, str(err))

        acc2, err2 = verify_key("ps_wrongkey123")
        t("错 key 被拒", acc2 is None and err2 == "invalid_key", str(err2))

        allow, info = check_quota(acc, "query")
        t("免费档配额 100", info["limit"] == 100, str(info))

        # 用满配额
        for _ in range(100):
            record_usage(aid, "query")
        allow, info = check_quota(acc, "query")
        t("用满后拒", allow is False and info["remaining"] == 0, str(info))

        # 付费升级
        with _conn() as c:
            c.execute("UPDATE account SET plan='monitor' WHERE id=?", (aid,))
        acc, _ = verify_key(k["api_key"])
        allow, info = check_quota(acc, "query")
        t("升级 monitor 后限额 2000", info["limit"] == 2000 and allow, str(info))

        # 付费过期降级
        with _conn() as c:
            c.execute("UPDATE account SET expires_at=? WHERE id=?", (int(time.time()) - 10, aid))
        acc, _ = verify_key(k["api_key"])
        p = plan_of(acc)
        t("过期降级 free", p["name"] == "免费" and "_downgraded_from" in p, str(p.get("_downgraded_from")))

        # watch 上限
        for i in range(3):
            add_watch(aid, "0x" + f"{i:040x}")
        allow, info = check_quota(acc, "watch_add")
        t("free 不提供监控(limit=0)", allow is False and info["used"] == 3, str(info))

        # 重复 key 拒绝
        t("重复 watch 返回 False", add_watch(aid, "0x" + f"{0:040x}") is False)

        print("\n" + ("全部通过 ✅" if not fails else f"失败 {len(fails)} 项 ❌"))
        raise SystemExit(0 if not fails else 1)
    finally:
        # 自测不污染正式库
        if bak and bak.exists():
            shutil.move(str(bak), str(DB_PATH))
        elif DB_PATH.exists() and not bak:
            DB_PATH.unlink()

#!/usr/bin/env python3
"""cache.py — PoolSentry 本地结果缓存（SQLite，零依赖）。

为什么需要它（2026-10-24 实测）：
  `poolsentry check` 在线路径 269 秒，其中 250 秒耗在「24h 换手率扫链」。
  HyperSync 与 RPC 分片都约 184 秒 —— **换哪家数据源都救不了**，
  因为把「扫 24 小时日志」放在客户点击路径上，本身就是错的架构。

  正解：**夜间批量预计算 → 落 SQLite → 客户查询读本地库（毫秒级）**。

分层策略：
  L1 热数据（< TTL_HOT）  ：直接返回，标 cached_at
  L2 温数据（< TTL_WARM） ：返回但标 STALE，后台刷新
  L3 冷/缺失              ：返回 PENDING_BATCH，不阻塞用户

设计约束：
  - 不静默降级：任何缓存命中都必须把 `cached_at` / `age_s` / `stale` 一起给调用方
  - 不把「算不出来」写成数据：失败只记 error，不写 0
"""
import json
import sqlite3
import time
from pathlib import Path

WS = Path(__file__).resolve().parent.parent
DB_PATH = WS / "state" / "cache.db"

TTL_HOT = 3600            # 1 小时内算新鲜
TTL_WARM = 6 * 3600       # 6 小时内算可用但标 stale

SCHEMA = """
CREATE TABLE IF NOT EXISTS pool_cache (
    pool        TEXT PRIMARY KEY,
    payload     TEXT NOT NULL,      -- JSON
    computed_at INTEGER NOT NULL,   -- unix ts
    source      TEXT,               -- 'nightly_batch' | 'online' | ...
    ok          INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS batch_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  INTEGER NOT NULL,
    ended_at    INTEGER,
    total       INTEGER DEFAULT 0,
    done        INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_computed ON pool_cache(computed_at);
"""


def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.executescript(SCHEMA)
    return c


def get(pool, max_age_s=None, allow_stale=False):
    """读缓存。返回 dict 或 None（无记录 / 超龄）。

    返回值里带 `_cache` 元信息，调用方必须把它透传给用户，
    不许把缓存结果伪装成刚算出来的。

    allow_stale（2026-10-25 新增，T19 实测驱动）：
      False = 严格模式，超 TTL_WARM 视为未命中（默认，行为不变）。
      True  = 降级模式，超龄也返回，但 `_cache.stale=True` + `age_s` 照实给，
              **供「出不了卡也不让客户空手」的兜底**。
      为什么加：10-25 实测命中缓存的卡只花 0.25s，而新鲜计算 16~19s；
      客户付了钱等 19s 是体验问题，等不到卡才是事故。超龄数据标清楚比不给好。
    """
    pool = (pool or "").lower()
    if not pool:
        return None
    max_age_s = TTL_WARM if max_age_s is None else max_age_s
    with _conn() as c:
        row = c.execute(
            "SELECT payload, computed_at, source, ok FROM pool_cache WHERE pool=?",
            (pool,)).fetchone()
    if not row:
        return None
    payload, computed_at, source, ok = row
    age = int(time.time()) - int(computed_at)
    if age > max_age_s and not allow_stale:
        return None
    try:
        data = json.loads(payload)
    except Exception:                                    # noqa: BLE001
        return None
    data["_cache"] = {
        "hit": True,
        "cached_at": computed_at,
        "age_s": age,
        "stale": age > TTL_HOT or age > max_age_s,
        "source": source,
        "ok": bool(ok),
    }
    return data


def put(pool, data, source="online", ok=True):
    """写缓存。data 里若含 `_cache` 元信息会被剥掉（那是读时加的）。"""
    pool = (pool or "").lower()
    if not pool:
        return False
    clean = {k: v for k, v in (data or {}).items() if k != "_cache"}
    try:
        payload = json.dumps(clean, ensure_ascii=False)
    except Exception:                                    # noqa: BLE001
        return False
    with _conn() as c:
        c.execute(
            "INSERT INTO pool_cache(pool, payload, computed_at, source, ok) "
            "VALUES(?,?,?,?,?) ON CONFLICT(pool) DO UPDATE SET "
            "payload=excluded.payload, computed_at=excluded.computed_at, "
            "source=excluded.source, ok=excluded.ok",
            (pool, payload, int(time.time()), source, 1 if ok else 0))
    return True


def stats():
    """缓存总体状态，供运维与订阅后台展示。"""
    now = int(time.time())
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) FROM pool_cache").fetchone()[0]
        hot = c.execute("SELECT COUNT(*) FROM pool_cache WHERE computed_at > ?",
                        (now - TTL_HOT,)).fetchone()[0]
        warm = c.execute("SELECT COUNT(*) FROM pool_cache WHERE computed_at > ?",
                         (now - TTL_WARM,)).fetchone()[0]
        bad = c.execute("SELECT COUNT(*) FROM pool_cache WHERE ok=0").fetchone()[0]
        last = c.execute(
            "SELECT id, started_at, ended_at, total, done, failed FROM batch_run "
            "ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "db": str(DB_PATH),
        "total_pools": total,
        "hot_1h": hot,
        "warm_6h": warm,
        "failed": bad,
        "last_batch": ({"id": last[0], "started_at": last[1], "ended_at": last[2],
                        "total": last[3], "done": last[4], "failed": last[5]}
                       if last else None),
    }


if __name__ == "__main__":
    print(json.dumps(stats(), indent=2, ensure_ascii=False))

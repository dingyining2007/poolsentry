#!/usr/bin/env python3
"""nightly_batch.py — 夜间批量预计算，把重指标算好落 SQLite。

为什么必须有它（2026-10-24 实测）：
  24h 换手率扫链：RPC 分片 185s / HyperSync 184s，**换哪家数据源都救不了**。
  把这件事放在客户点击路径上本身就是错的架构。
  → 夜间批量算好，客户查询读本地库（毫秒级）。

用法：
  python3 scripts/nightly_batch.py --pools pools.txt          # 从文件读池列表
  python3 scripts/nightly_batch.py --pools pools.txt --limit 50 --resume
  python3 scripts/nightly_batch.py --auto-top 100             # 自动取 TVL 前 100 池
  python3 scripts/nightly_batch.py --status                   # 只看缓存状态

设计约束：
  - 幂等 + 断点续传：已算过且未过期的池跳过（--resume 默认开）
  - 失败不写假数据：算不出就标 ok=0，留 error，不填 0
  - 单池超时不拖垮整批：每池独立 try/except + 时间预算
  - 可中断：Ctrl-C 后已完成的池已落库
"""
import argparse
import json
import signal
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import cache                       # noqa: E402

STOP = False


def _sigint(signum, frame):
    global STOP
    STOP = True
    print("\n⏸  收到中断信号，当前池跑完后退出（已完成的结果已落库）", flush=True)


signal.signal(signal.SIGINT, _sigint)


def compute_one(pool, hours=24):
    """算一个池的完整指标（含 24h 换手率）。返回 (data, source) 或抛异常。"""
    from turnover_24h import turnover
    import cli

    t0 = time.time()
    # ① 24h 换手率（重活，只在夜间跑）
    tv = turnover(pool, hours=hours, verbose=False, timeout_s=300)
    # ② 在线轻量指标（往返报价 + 死盘判据），直接复用 cli
    res = cli.analyse_subject(pool, opts={"turnover_cached": tv})
    res["_batch_elapsed_s"] = round(time.time() - t0, 1)
    return res


def load_pools(path):
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"池列表文件不存在：{path}")
    txt = p.read_text(encoding="utf-8", errors="ignore")
    if p.suffix.lower() == ".json":
        data = json.loads(txt)
        if isinstance(data, dict):
            data = data.get("pools") or list(data.keys())
        out = []
        for x in data:
            if isinstance(x, str):
                out.append(x)
            elif isinstance(x, dict):
                out.append(x.get("pair") or x.get("pool") or x.get("address"))
        return [x.lower() for x in out if x and x.startswith("0x")]
    out = []
    for line in txt.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and line.startswith("0x"):
            out.append(line.lower())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pools", help="池列表文件（txt 每行一个 / json 数组）")
    ap.add_argument("--limit", type=int, default=0, help="最多算几个（0=不限）")
    ap.add_argument("--hours", type=float, default=24, help="换手率窗口小时数")
    ap.add_argument("--max-age", type=int, default=cache.TTL_WARM,
                    help="已缓存且未超过此秒数的池跳过")
    ap.add_argument("--no-resume", action="store_true", help="不跳过已缓存（强制重算）")
    ap.add_argument("--status", action="store_true", help="只打印缓存状态")
    ap.add_argument("--dry", action="store_true", help="只列出要算哪些，不真算")
    a = ap.parse_args()

    if a.status:
        print(json.dumps(cache.stats(), indent=2, ensure_ascii=False))
        return 0

    if not a.pools:
        raise SystemExit("需要 --pools <文件> 或 --status")

    pools = load_pools(a.pools)
    print(f"池列表：{len(pools)} 个", flush=True)

    # 断点续传：跳过已缓存的
    todo = []
    for p in pools:
        if not a.no_resume and cache.get(p, max_age_s=a.max_age):
            continue
        todo.append(p)
    print(f"需计算：{len(todo)} 个（跳过已缓存 {len(pools)-len(todo)} 个）", flush=True)

    if a.limit:
        todo = todo[:a.limit]
        print(f"限流后：{len(todo)} 个", flush=True)

    if a.dry:
        for p in todo[:20]:
            print("  ", p)
        if len(todo) > 20:
            print(f"   ... 还有 {len(todo)-20} 个")
        return 0

    ok = fail = 0
    t_start = time.time()
    for i, pool in enumerate(todo, 1):
        if STOP:
            print("已中断", flush=True)
            break
        t0 = time.time()
        try:
            res = compute_one(pool, hours=a.hours)
            cache.put(pool, res, source="nightly_batch", ok=True)
            ok += 1
            tv = (res.get("turnover") or {})
            tv_s = (f"换手 {tv['turnover_daily']*100:.4f}%/天"
                    if tv.get("turnover_daily") is not None else f"换手 N/A({tv.get('error','?')[:20]})")
            ag = (res.get("activity") or {}).get("verdict", "?")
            print(f"[{i}/{len(todo)}] {pool[:12]}… {res.get('status')} "
                  f"{tv_s} 死盘={ag} {time.time()-t0:.0f}s", flush=True)
        except Exception as e:                          # noqa: BLE001
            fail += 1
            cache.put(pool, {"status": "ERROR", "error": str(e)[:200]}, source="nightly_batch", ok=False)
            print(f"[{i}/{len(todo)}] {pool[:12]}… ❌ {type(e).__name__}: {str(e)[:100]}", flush=True)
            traceback.print_exc(limit=1)

    dur = time.time() - t_start
    print(f"\n完成：成功 {ok} / 失败 {fail}，耗时 {dur/60:.1f} 分钟", flush=True)
    print(json.dumps(cache.stats(), indent=2, ensure_ascii=False), flush=True)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

"""两线程并发健康检查（共享同一 SQLite 连接）：验证 SAVEPOINT 交错导致误报。

lan_server 使用 ThreadingHTTPServer，adapter.handle_request 无全局锁；
mcp_server._live_status 每次工具调用都调用 service._refresh_health()
→ ContextService._quick_check_diagnostics()，而连接是
check_same_thread=False 的单条共享连接。

对照组：同一路径加锁串行；以及“自带短连接 + 单事务”的候选修复。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

REPO = Path("/home/jiangli/hermes-memory-plugin")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from evolvmem.config import Config  # noqa: E402
from evolvmem.context_models import ContextMode  # noqa: E402
from evolvmem.context_service import ContextService  # noqa: E402
from evolvmem.context_store import ContextStore  # noqa: E402
from evolvmem.memory_store import MemoryStore  # noqa: E402
from scale_quick_check import DDL, INS  # noqa: E402

BASE = Path(__file__).resolve().parent / "threads"
N = 2000
CALLS = 150
WORKERS = 2


def prepare() -> Config:
    if BASE.exists():
        shutil.rmtree(BASE)
    data_dir = BASE / "data"
    data_dir.mkdir(parents=True)
    config = Config(data_dir=data_dir, apply_environment=False)
    MemoryStore(config).initialize()
    conn = sqlite3.connect(config.db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    conn.executemany(INS, [(i, "l0", f"content row {i} alpha zebra", "h", "upload",
                            "2026-09-21T00:00:00Z", "2026-09-21T00:00:00Z")
                           for i in range(1, N + 1)])
    conn.commit()
    conn.close()
    return config


def writer(stop: threading.Event, config: Config, stats: dict) -> threading.Thread:
    def loop() -> None:
        b = sqlite3.connect(config.db_path, timeout=10)
        b.execute("PRAGMA busy_timeout=5000")
        i = 0
        while not stop.is_set():
            i += 1
            try:
                b.execute("UPDATE context_layers SET content=content||' x' WHERE id=?",
                          (1 + (i * 7) % (N - 1),))
                b.commit()
                stats["writes"] = stats.get("writes", 0) + 1
            except Exception as exc:
                stats["errors"] = stats.get("errors", 0) + 1
                stats["last"] = f"{type(exc).__name__}: {exc}"
            time.sleep(0.001)
        b.close()

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def run_round(label: str, fn, *, lock: threading.Lock | None) -> int:
    fails = 0
    errs = 0
    barrier = threading.Barrier(WORKERS)

    def worker() -> None:
        nonlocal fails, errs
        barrier.wait()
        for _ in range(CALLS):
            try:
                if lock is None:
                    out = fn()
                else:
                    with lock:
                        out = fn()
            except Exception as exc:
                errs += 1
                continue
            if out:
                fails += 1
            time.sleep(0.002)

    threads = [threading.Thread(target=worker) for _ in range(WORKERS)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"{label:34} calls={WORKERS * CALLS:4} fails={fails:4} errors={errs:3} "
          f"({time.time() - t0:.1f}s)")
    return fails


def main() -> int:
    config = prepare()
    store = ContextStore(config)
    store.initialize(create_schema=False)
    service = ContextService(config, store=store)
    service.initialize(mode=ContextMode.PRIMARY, adapter="codex")
    for table in ("context_layers_fts", "context_layers_fts_trigram"):
        store._connection().execute(f"SELECT rowid FROM {table} LIMIT 1").fetchall()

    stats: dict = {}
    stop = threading.Event()
    w = writer(stop, config, stats)
    time.sleep(0.2)

    # A) 线上形状：两线程共用 service.store 的连接，无锁
    run_round("A shared-conn no-lock (shipped)", service._quick_check_diagnostics, lock=None)
    print("   final shipped check =", service._quick_check_diagnostics())

    # B) 同一路径但串行化
    run_round("B shared-conn lock (control)", service._quick_check_diagnostics,
              lock=threading.Lock())

    # C) 候选修复：每次健康检查自带短连接 + 单事务；线程间互不共享
    def own_conn() -> tuple[str, ...]:
        conn = sqlite3.connect(config.db_path, timeout=10)
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("BEGIN")
            conn.execute("SELECT rowid FROM context_layers_fts LIMIT 1").fetchall()
            conn.execute("SELECT rowid FROM context_layers_fts_trigram LIMIT 1").fetchall()
            rows = conn.execute("PRAGMA quick_check").fetchall()
            conn.commit()
        finally:
            conn.close()
        return () if rows and all(str(r[0]).lower() == "ok" for r in rows) else ("quick_check_failed",)

    run_round("C own-conn single-txn (candidate)", own_conn, lock=None)

    stop.set()
    w.join(timeout=5)
    print("writer:", stats)
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

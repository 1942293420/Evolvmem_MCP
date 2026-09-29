"""同进程写路径与健康检查共享一条连接：事务被结束导致 FTS 快照误报。

lan_server 是 ThreadingHTTPServer，adapter.handle_request 没有全局锁；
ContextStore 只有一条 check_same_thread=False 的连接。T2 走 store.transaction()
（BEGIN IMMEDIATE/rollback）时会把 T1 健康检查 SAVEPOINT 里的读事务结束掉，
T1 的 quick_check 于是在“新快照 + 旧 FTS 结构缓存”下执行 → checksum mismatch。

对照组：T2 用独立连接写（互不干扰）。
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

BASE = Path(__file__).resolve().parent / "mixed"
N = 2000
CALLS = 250


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


def round(label: str, config: Config, service: ContextService, *, same_conn: bool) -> None:
    shared = service.store._connection()
    stats = {"writes": 0, "write_errors": 0, "last": ""}
    stop = threading.Event()

    def write_loop() -> None:
        i = 0
        while not stop.is_set():
            i += 1
            try:
                if same_conn:
                    with service.store.transaction():
                        shared.execute(
                            "UPDATE context_layers SET content=content||' w' WHERE id=?",
                            (1 + (i * 7) % (N - 1),))
                else:
                    b = sqlite3.connect(config.db_path, timeout=10)
                    b.execute("PRAGMA busy_timeout=5000")
                    b.execute("BEGIN IMMEDIATE")
                    b.execute("UPDATE context_layers SET content=content||' w' WHERE id=?",
                              (1 + (i * 7) % (N - 1),))
                    b.commit()
                    b.close()
                stats["writes"] += 1
            except Exception as exc:
                stats["write_errors"] += 1
                stats["last"] = f"{type(exc).__name__}: {exc}"
            time.sleep(0.001)

    t = threading.Thread(target=write_loop, daemon=True)
    t.start()
    time.sleep(0.3)
    fails = 0
    first = None
    for _ in range(CALLS):
        out = service._quick_check_diagnostics()
        if out:
            fails += 1
            first = first or out
        time.sleep(0.002)
    stop.set()
    t.join(timeout=5)
    print(f"{label:38} health_fails={fails:3}/{CALLS} writes={stats['writes']:4} "
          f"write_errors={stats['write_errors']:3} first={first} last_writer_err={stats['last'][:60]}")
    # 现场收尾：清理可能残留的读事务
    try:
        if shared.in_transaction:
            shared.rollback()
    except Exception:
        pass


def main() -> int:
    config = prepare()
    store = ContextStore(config)
    store.initialize(create_schema=False)
    service = ContextService(config, store=store)
    service.initialize(mode=ContextMode.PRIMARY, adapter="codex")
    for table in ("context_layers_fts", "context_layers_fts_trigram"):
        store._connection().execute(f"SELECT rowid FROM {table} LIMIT 1").fetchall()

    print("A) writer uses the SAME shared connection (in-process write path)")
    round("same-conn writer", config, service, same_conn=True)
    print("   after: shipped check =", service._quick_check_diagnostics())

    print("B) writer uses its own connection (external process / another handle)")
    round("separate-conn writer", config, service, same_conn=False)
    print("   after: shipped check =", service._quick_check_diagnostics())

    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

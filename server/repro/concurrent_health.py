"""并发写入下跑真实健康门禁：验证刷新与 quick_check 之间的竞态误报。

- 连接 A：真实 ContextStore/ContextService（常驻进程视角），只从主线程使用。
- 连接 B：独立线程/独立连接，模拟外部写入者（上传/另一进程/legacy 写路径）。
- 对照组：候选修复（新建短连接、失败重试、count(*) 全扫刷新）。
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

BASE = Path(__file__).resolve().parent / "conc"
N = 2000
CHECKS = 80


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


def start_writer(config: Config, stop: threading.Event, stats: dict) -> threading.Thread:
    def loop() -> None:
        b = sqlite3.connect(config.db_path)
        b.execute("PRAGMA journal_mode=WAL")
        i = 0
        while not stop.is_set():
            i += 1
            row = 1 + (i * 7) % (N - 1)
            try:
                b.execute("UPDATE context_layers SET content=content||' x' WHERE id=?", (row,))
                b.commit()
                stats["writes"] = stats.get("writes", 0) + 1
            except Exception as exc:  # pragma: no cover
                stats["write_errors"] = stats.get("write_errors", 0) + 1
                stats["last_error"] = f"{type(exc).__name__}: {exc}"
            time.sleep(0.002)
        b.close()

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


def shipped_check(service: ContextService) -> tuple[str, ...]:
    return service._quick_check_diagnostics()


def retry_check(service: ContextService) -> tuple[str, ...]:
    out = service._quick_check_diagnostics()
    if out:
        out = service._quick_check_diagnostics()
    return out


def fresh_conn_check(config: Config) -> tuple[str, ...]:
    conn = sqlite3.connect(config.db_path)
    try:
        for table in ("context_layers_fts", "context_layers_fts_trigram",
                      "memories_fts", "memories_fts_trigram"):
            try:
                conn.execute(f"SELECT rowid FROM {table} LIMIT 1").fetchall()
            except sqlite3.OperationalError:
                pass
        rows = conn.execute("PRAGMA quick_check").fetchall()
    finally:
        conn.close()
    return () if rows and all(str(r[0]).lower() == "ok" for r in rows) else ("quick_check_failed",)


def fullscan_check(service: ContextService) -> tuple[str, ...]:
    conn = service.store._connection()
    conn.execute("SAVEPOINT sp")
    try:
        conn.execute("SELECT count(*) FROM context_layers_fts").fetchall()
        conn.execute("SELECT count(*) FROM context_layers_fts_trigram").fetchall()
        rows = conn.execute("PRAGMA quick_check").fetchall()
    finally:
        conn.execute("RELEASE sp")
    return () if rows and all(str(r[0]).lower() == "ok" for r in rows) else ("quick_check_failed",)


def main() -> int:
    config = prepare()
    store = ContextStore(config)
    store.initialize(create_schema=False)
    service = ContextService(config, store=store)
    service.initialize(mode=ContextMode.PRIMARY, adapter="codex")
    # 预热：常驻连接先读过两张 context FTS（等价于上一次健康检查）
    for table in ("context_layers_fts", "context_layers_fts_trigram"):
        service.store._connection().execute(f"SELECT rowid FROM {table} LIMIT 1").fetchall()

    stats: dict = {}
    stop = threading.Event()
    writer = start_writer(config, stop, stats)
    time.sleep(0.2)
    fails = {"shipped": 0, "retry": 0, "fresh": 0, "fullscan": 0}
    first: dict = {}
    for i in range(CHECKS):
        for name, fn in (("shipped", lambda: shipped_check(service)),
                         ("retry", lambda: retry_check(service)),
                         ("fresh", lambda: fresh_conn_check(config)),
                         ("fullscan", lambda: fullscan_check(service))):
            out = fn()
            if out:
                fails[name] += 1
                first.setdefault(name, out)
        time.sleep(0.01)
    stop.set()
    writer.join(timeout=3)
    print(f"N={N} checks={CHECKS} writes={stats.get('writes')} "
          f"write_errors={stats.get('write_errors', 0)} {stats.get('last_error', '')}")
    for name in fails:
        print(f"{name:9} failures={fails[name]:3}/{CHECKS} first={first.get(name)}")
    print("final shipped =", shipped_check(service))
    store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

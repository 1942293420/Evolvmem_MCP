"""确定性交错：证明同一连接上“写路径结束健康检查的读事务”即产生 FTS 快照误报。

无需线程，按线上代码的语句顺序手工重放：
  1) A 读 FTS（缓存结构快照 V_a）
  2) A 自己的写事务提交（FTS 变为 V_b，A 的缓存仍是 V_a）
  3) A 健康检查：SAVEPOINT → 刷新 FTS（缓存更新为 V_b，快照 S_b）
  4) 并发的写请求在同一连接上 BEGIN IMMEDIATE 失败 → rollback()
     （ContextStore.transaction 的异常分支，见 context_store.py:432-435）
     → 健康检查的读事务被提前结束
  5) 外部/另一条连接的写提交（FTS 变为 V_c）
  6) A 继续 PRAGMA quick_check：新快照 S_c + 缓存 V_b → checksum mismatch
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

REPO = Path("/home/jiangli/hermes-memory-plugin")
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from scale_quick_check import DDL, INS  # noqa: E402

BASE = Path(__file__).resolve().parent / "deterministic"
N = 300


def main() -> int:
    if BASE.exists():
        shutil.rmtree(BASE)
    BASE.mkdir(parents=True)
    db = BASE / "memory.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    conn.executemany(INS, [(i, "l0", f"content row {i} alpha zebra", "h", "upload",
                            "2026-09-21T00:00:00Z", "2026-09-21T00:00:00Z")
                           for i in range(1, N + 1)])
    conn.commit()

    a = conn  # 常驻连接 A（ContextStore 的唯一连接）
    print("step0 quick_check                        =",
          [str(r[0])[:70] for r in a.execute("PRAGMA quick_check").fetchall()][:1])
    a.execute("SELECT rowid FROM context_layers_fts LIMIT 1").fetchall()  # 缓存 V_a
    a.execute("UPDATE context_layers SET content=content||' self' WHERE id=1")  # 进程内写
    a.commit()                                                             # FTS 变 V_b

    a.execute("SAVEPOINT evolvmem_health_snapshot")
    a.execute("SELECT rowid FROM context_layers_fts LIMIT 1").fetchall()   # 刷新为 V_b
    a.execute("SELECT rowid FROM context_layers_fts_trigram LIMIT 1").fetchall()

    try:
        a.execute("BEGIN IMMEDIATE")   # 并发写请求走 ContextStore.transaction()
    except sqlite3.OperationalError as exc:
        print("step3 concurrent BEGIN IMMEDIATE refused =", exc)
        a.rollback()                   # 该分支 rollback() 结束了健康检查的读事务

    b = sqlite3.connect(db)
    b.execute("UPDATE context_layers SET content=content||' ext' WHERE id=2")  # FTS 变 V_c
    b.commit()
    b.close()

    rows = a.execute("PRAGMA quick_check").fetchall()
    print("step4 quick_check rows                   =", [str(r[0])[:90] for r in rows][:2])
    try:
        a.execute("RELEASE evolvmem_health_snapshot")
    except sqlite3.OperationalError as exc:
        print("step5 RELEASE (finally 分支)             =", exc)
    print("step6 == 线上 _quick_check_diagnostics 返回 quick_check_failed =",
          not (rows and all(str(r[0]).lower() == "ok" for r in rows)))
    print("step7 新连接（只读视角）quick_check       =",
          [str(r[0])[:70] for r in sqlite3.connect(db).execute("PRAGMA quick_check").fetchall()][:1])
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

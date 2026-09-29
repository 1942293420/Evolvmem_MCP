"""规模/并发条件矩阵：为什么 5 行合成库 OK，而线上 8336 行会误报。

核心问题：`SELECT rowid FROM <fts> LIMIT 1`（线上修复）在数据量大、
写入落在其他 segment 时是否仍能刷新 FTS5 校验快照。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

WORK = Path(__file__).resolve().parent / "scale"
N = 8000

DDL = """
CREATE TABLE context_layers (
  id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL, layer TEXT NOT NULL,
  content TEXT NOT NULL, content_hash TEXT NOT NULL, generator TEXT NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(item_id, layer));
CREATE VIRTUAL TABLE context_layers_fts USING fts5(content, tokenize='unicode61');
CREATE VIRTUAL TABLE context_layers_fts_trigram USING fts5(content, tokenize='trigram');
CREATE TRIGGER context_layers_fts_ai AFTER INSERT ON context_layers
  WHEN new.layer IN ('l0','l1') BEGIN
  INSERT INTO context_layers_fts(rowid,content) VALUES(new.id,new.content); END;
CREATE TRIGGER context_layers_fts_au AFTER UPDATE ON context_layers
  WHEN old.layer IN ('l0','l1') OR new.layer IN ('l0','l1') BEGIN
  DELETE FROM context_layers_fts WHERE rowid=old.id AND old.layer IN ('l0','l1');
  INSERT INTO context_layers_fts(rowid,content) SELECT new.id,new.content
    WHERE new.layer IN ('l0','l1'); END;
CREATE TRIGGER context_layers_fts_ad AFTER DELETE ON context_layers
  WHEN old.layer IN ('l0','l1') BEGIN
  DELETE FROM context_layers_fts WHERE rowid=old.id; END;
CREATE TRIGGER context_layers_fts_trigram_ai AFTER INSERT ON context_layers
  WHEN new.layer IN ('l0','l1') BEGIN
  INSERT INTO context_layers_fts_trigram(rowid,content) VALUES(new.id,new.content); END;
CREATE TRIGGER context_layers_fts_trigram_au AFTER UPDATE ON context_layers
  WHEN old.layer IN ('l0','l1') OR new.layer IN ('l0','l1') BEGIN
  DELETE FROM context_layers_fts_trigram WHERE rowid=old.id AND old.layer IN ('l0','l1');
  INSERT INTO context_layers_fts_trigram(rowid,content) SELECT new.id,new.content
    WHERE new.layer IN ('l0','l1'); END;
CREATE TRIGGER context_layers_fts_trigram_ad AFTER DELETE ON context_layers
  WHEN old.layer IN ('l0','l1') BEGIN
  DELETE FROM context_layers_fts_trigram WHERE rowid=old.id; END;
"""

UP = "UPDATE context_layers SET content=content||' changed' WHERE id=?"
INS = ("INSERT INTO context_layers(item_id,layer,content,content_hash,generator,"
       "created_at,updated_at) VALUES(?,?,?,?,?,?,?)")
DEL = "DELETE FROM context_layers WHERE id=?"

SHIPPED = ("SELECT rowid FROM context_layers_fts LIMIT 1;"
           "SELECT rowid FROM context_layers_fts_trigram LIMIT 1")
FULLCNT = ("SELECT count(*) FROM context_layers_fts;"
           "SELECT count(*) FROM context_layers_fts_trigram")


def build(tag: str, rows: int = N) -> Path:
    WORK.mkdir(parents=True, exist_ok=True)
    db = WORK / f"{tag}.db"
    if db.exists():
        db.unlink()
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    conn.executemany(INS, [(i, "l0", f"content row {i} alpha zebra", "h", "repro",
                            "2026-09-21T00:00:00Z", "2026-09-21T00:00:00Z")
                           for i in range(1, rows + 1)])
    conn.commit()
    conn.close()
    return db


def run(label: str, *, prior: str | None, write, refresh: str, rows: int = N,
        pre_refresh: bool = False) -> None:
    db = build(label, rows)
    a = sqlite3.connect(db)
    a.execute("PRAGMA journal_mode=WAL")
    if prior:
        a.execute(prior).fetchall()
    b = sqlite3.connect(db)
    write(b)
    b.commit()
    b.close()
    if refresh:
        for stmt in refresh.split(";"):
            if stmt.strip():
                a.execute(stmt).fetchall()
    rows_out = a.execute("PRAGMA quick_check").fetchall()
    verdict = "OK  " if rows_out and all(str(r[0]).lower() == "ok" for r in rows_out) else "FAIL"
    print(f"{verdict} | {label:42} write={write.__name__:12} refresh={refresh or 'none':16} "
          f"rows={[str(r[0])[:80] for r in rows_out][:2]}")
    a.close()


def w_update_last(b):
    b.execute(UP, (N - 3,))


def w_update_first(b):
    b.execute(UP, (2,))


def w_update_many(b):
    for i in range(1, 501):
        b.execute(UP, (i,))


def w_insert_100(b):
    b.executemany(INS, [(N + i, "l0", f"uploaded row {i} beta", "h", "upload",
                         "2026-09-21T00:03:00Z", "2026-09-21T00:03:00Z")
                        for i in range(1, 101)])


def w_delete_100(b):
    for i in range(1, 101):
        b.execute(DEL, (i,))


def main() -> int:
    print(f"rows={N} fts5 file={sqlite3.sqlite_version}")
    prior_read = "SELECT rowid FROM context_layers_fts LIMIT 1"
    print("== 无历史读（新连接，协调者 mode=ro 视角） ==")
    run("fresh_no_prior", prior=None, write=w_update_last, refresh=SHIPPED)
    print("== 有历史读：写入位置/写入量 × 刷新方式 ==")
    run("prior_update_last_lim1", prior=prior_read, write=w_update_last, refresh=SHIPPED)
    run("prior_update_first_lim1", prior=prior_read, write=w_update_first, refresh=SHIPPED)
    run("prior_update_last_nonrefresh", prior=prior_read, write=w_update_last, refresh="")
    run("prior_update_last_fullcount", prior=prior_read, write=w_update_last, refresh=FULLCNT)
    run("prior_update500_lim1", prior=prior_read, write=w_update_many, refresh=SHIPPED)
    run("prior_insert100_lim1", prior=prior_read, write=w_insert_100, refresh=SHIPPED)
    run("prior_delete100_lim1", prior=prior_read, write=w_delete_100, refresh=SHIPPED)
    print("== 写后先刷新一次、再写入（TOCTOU 窗口） ==")
    db = build("toctou", N)
    a = sqlite3.connect(db)
    a.execute(prior_read).fetchall()
    for stmt in SHIPPED.split(";"):
        if stmt.strip():
            a.execute(stmt).fetchall()
    b = sqlite3.connect(db)
    w_update_last(b)
    b.commit()
    b.close()
    rows_out = a.execute("PRAGMA quick_check").fetchall()
    print(("OK  " if all(str(r[0]).lower() == "ok" for r in rows_out) else "FAIL"),
          "| refresh-then-concurrent-write      rows=", [str(r[0])[:80] for r in rows_out][:2])
    a.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

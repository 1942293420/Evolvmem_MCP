"""条件矩阵：quick_check 在何种外部写入 + 何种连接历史下误报。

raw sqlite3，合成库，无业务数据。目的：确定“刷新 FTS 快照”到底针对哪类
FTS 表/哪类写入，从而判断只刷 context_layers_fts 两张表是否漏了
memories_fts / memories_fts_trigram（同一 memory.db 内的 legacy 外部内容 FTS）。
"""
from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

WORK = Path(__file__).resolve().parent / "matrix"

DDL = """
CREATE TABLE memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, value TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active', attribute TEXT DEFAULT '', tags TEXT DEFAULT '',
  source_session TEXT DEFAULT '', access_count INTEGER DEFAULT 0,
  last_accessed TEXT DEFAULT NULL, supersedes INTEGER, superseded_by INTEGER,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE VIRTUAL TABLE memories_fts USING fts5(value, tags, content=memories,
  content_rowid=id, tokenize='unicode61');
CREATE VIRTUAL TABLE memories_fts_trigram USING fts5(value, tags, content=memories,
  content_rowid=id, tokenize='trigram');
CREATE TRIGGER memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid,value,tags) VALUES(new.id,new.value,new.tags); END;
CREATE TRIGGER memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts,rowid,value,tags) VALUES('delete',old.id,old.value,old.tags); END;
CREATE TRIGGER memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts,rowid,value,tags) VALUES('delete',old.id,old.value,old.tags);
  INSERT INTO memories_fts(rowid,value,tags) VALUES(new.id,new.value,new.tags); END;
CREATE TRIGGER memories_ai_trigram AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts_trigram(rowid,value,tags) VALUES(new.id,new.value,new.tags); END;
CREATE TRIGGER memories_ad_trigram AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts_trigram(memories_fts_trigram,rowid,value,tags) VALUES('delete',old.id,old.value,old.tags); END;
CREATE TRIGGER memories_au_trigram AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts_trigram(memories_fts_trigram,rowid,value,tags) VALUES('delete',old.id,old.value,old.tags);
  INSERT INTO memories_fts_trigram(rowid,value,tags) VALUES(new.id,new.value,new.tags); END;

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


def build(name: str) -> Path:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    db = WORK / f"{name}.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(DDL)
    for i in range(1, 6):
        conn.execute("INSERT INTO memories(key,value,tags,created_at,updated_at)"
                     " VALUES(?,?,?,?,?)", (f"k{i}", f"value number {i}", "alpha",
                                            "2026-09-21T00:00:00Z", "2026-09-21T00:00:00Z"))
        conn.execute("INSERT INTO context_layers(item_id,layer,content,content_hash,"
                     "generator,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                     (i, "l0", f"context content {i}", "h", "repro",
                      "2026-09-21T00:00:00Z", "2026-09-21T00:00:00Z"))
    conn.commit()
    conn.close()
    return db


def probe(label: str, prior_read: str | None, external_write: str, refresh: str) -> None:
    db = build(label.replace(" ", "_")[:60])
    a = sqlite3.connect(db)
    a.execute("PRAGMA journal_mode=WAL")
    if prior_read:
        a.execute(prior_read).fetchall()
    b = sqlite3.connect(db)
    b.execute(external_write)
    b.commit()
    b.close()
    if refresh:
        for stmt in refresh.split(";"):
            if stmt.strip():
                a.execute(stmt).fetchall()
    rows = a.execute("PRAGMA quick_check").fetchall()
    verdict = "OK" if rows and all(str(r[0]).lower() == "ok" for r in rows) else "FAIL"
    print(f"{verdict} | prior_read={prior_read!r:38} write={external_write[:58]!r:60} "
          f"refresh={refresh!r:55} rows={[str(r[0])[:90] for r in rows][:2]}")
    a.close()


def main() -> None:
    ctx_refresh = ("SELECT rowid FROM context_layers_fts LIMIT 1;"
                   "SELECT rowid FROM context_layers_fts_trigram LIMIT 1")
    mem_refresh = ("SELECT rowid FROM memories_fts LIMIT 1;"
                   "SELECT rowid FROM memories_fts_trigram LIMIT 1")
    write_ctx_update = "UPDATE context_layers SET content='x changed' WHERE id=1"
    write_mem_update = "UPDATE memories SET value='x changed' WHERE id=1"
    write_mem_delete = "DELETE FROM memories WHERE id=1"
    write_mem_insert = ("INSERT INTO memories(key,value,tags,created_at,updated_at)"
                        " VALUES('new','x new value','beta','2026-09-21T00:02:00Z',"
                        "'2026-09-21T00:02:00Z')")

    print("== 机制校准：context_layers_fts（已修复/已测试的表） ==")
    probe("cal_ctx_fail", "SELECT rowid FROM context_layers_fts LIMIT 1",
          write_ctx_update, "")                      # 预期 FAIL：不刷新
    probe("cal_ctx_ok", "SELECT rowid FROM context_layers_fts LIMIT 1",
          write_ctx_update, ctx_refresh)             # 预期 OK：刷新后
    probe("cal_ctx_noread", None, write_ctx_update, "")  # 预期 OK：无历史读

    print("== 缺口候选：memories_fts（生产修复未刷新的表） ==")
    probe("mem_upd_norefresh", "SELECT rowid FROM memories_fts LIMIT 1",
          write_mem_update, "")
    probe("mem_del_norefresh", "SELECT rowid FROM memories_fts LIMIT 1",
          write_mem_delete, "")
    probe("mem_ins_norefresh", "SELECT rowid FROM memories_fts LIMIT 1",
          write_mem_insert, "")
    probe("mem_upd_noread", None, write_mem_update, "")
    probe("mem_upd_noread_norefresh_ctxonly", None, write_mem_update, ctx_refresh)
    probe("mem_ins_ctxonly_refresh", "SELECT rowid FROM memories_fts LIMIT 1",
          write_mem_insert, ctx_refresh)             # 与线上代码等价
    probe("mem_upd_ctxonly_refresh", "SELECT rowid FROM memories_fts LIMIT 1",
          write_mem_update, ctx_refresh)             # 与线上代码等价
    probe("mem_upd_mem_refresh", "SELECT rowid FROM memories_fts LIMIT 1",
          write_mem_update, mem_refresh)
    probe("mem_upd_single_fts_refresh", "SELECT rowid FROM memories_fts LIMIT 1",
          write_mem_update, "SELECT rowid FROM memories_fts LIMIT 1")

    print("== 两条连接都读/写同一 FTS（多客户端） ==")
    probe("mem_upd_a_wrote", "SELECT rowid FROM memories_fts LIMIT 1",
          write_mem_update, "SELECT rowid FROM memories_fts LIMIT 1")


if __name__ == "__main__":
    main()

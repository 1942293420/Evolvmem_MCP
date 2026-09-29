"""合成库最小复现：degraded_legacy 的 quick_check 跨连接误判。

场景：常驻进程的 ContextStore 连接（A）既跑健康门禁 PRAGMA quick_check，
又通过 legacy facade 读 memories_fts / memories_fts_trigram。外部进程（B）
写入 memories 后，A 的 FTS5 校验快照过期；而
ContextService._quick_check_diagnostics() 只刷新 context_layers_fts 两张表。

只使用合成数据；使用生产源码类，但不调用 LanRuntime.initialize。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

REPO = Path("/home/jiangli/hermes-memory-plugin")
sys.path.insert(0, str(REPO))

from evolvmem.config import Config  # noqa: E402
from evolvmem.context_models import (  # noqa: E402
    ContextContentType,
    ContextItemDraft,
    ContextLayers,
    ContextMode,
    ContextScope,
    ContextStatus,
    ContextTier,
)
from evolvmem.context_service import ContextService  # noqa: E402
from evolvmem.context_store import ContextStore  # noqa: E402
from evolvmem.memory_store import MemoryStore  # noqa: E402

WORK = Path(__file__).resolve().parent / "work"


def fresh_data_dir(name: str) -> Config:
    if WORK.exists():
        shutil.rmtree(WORK)
    data_dir = WORK / name
    data_dir.mkdir(parents=True)
    return Config(data_dir=data_dir, apply_environment=False)


def seed(config: Config) -> Path:
    """建立 memories（外部内容 FTS5）与 context 两套 schema + 种子行。"""
    db = config.db_path
    memory = MemoryStore(config)
    memory.initialize()
    memory.close()

    store = ContextStore(config)
    store.initialize()
    store.close()

    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    for i in range(1, 6):
        conn.execute(
            "INSERT INTO memories(key, value, tags, attribute, status, created_at,"
            " updated_at) VALUES(?,?,?,?,?,?,?)",
            (f"seed:key:{i}", f"seed memory value {i}", "seed,alpha", "fact",
             "active", "2026-09-21T00:00:00Z", "2026-09-21T00:00:00Z"),
        )
    conn.commit()
    conn.close()

    # context 侧种子行走真实 API（触发器同步两张 context FTS）
    store = ContextStore(config)
    store.initialize(create_schema=False)
    for i in range(1, 4):
        store.create_item(
            ContextItemDraft(
                identity_key=f"seed:ctx:{i}",
                content_type=ContextContentType.FACT,
                layers=ContextLayers(
                    l0=f"seed context layer {i} alpha",
                    l1=f"detail for seed context layer {i}",
                    l2=f"source for seed context layer {i}",
                    generator="repro",
                ),
                project="proj",
                scope=ContextScope.PROJECT,
                status=ContextStatus.ACTIVE,
                tier=ContextTier.NORMAL,
                importance=5.0,
                confidence=0.9,
                expires_at=None,
            )
        )
    store.close()
    return db


def open_service(config: Config):
    """常驻进程视角：A 连接由 ContextStore 持有；服务只拿它做健康检查。"""
    store = ContextStore(config)
    store.initialize(create_schema=False)
    service = ContextService(config, store=store)
    service.initialize(mode=ContextMode.PRIMARY, adapter="codex")
    return store, service


def quick_rows(service: ContextService):
    conn = service.store._connection()
    try:
        rows = conn.execute("PRAGMA quick_check").fetchall()
    except Exception as exc:  # pragma: no cover
        return f"<raise {type(exc).__name__}: {exc}>"
    return [str(r[0])[:200] for r in rows][:4]


def external_memory_write(config: Config, tag: str) -> None:
    """模拟外部进程/legacy 写路径：只写 memories，由触发器维护两张 FTS。"""
    with sqlite3.connect(config.db_path) as b:
        b.execute(
            "INSERT INTO memories(key, value, tags, attribute, status, created_at,"
            " updated_at) VALUES(?,?,?,?,?,?,?)",
            (f"external:key:{tag}", f"external write {tag}", "beta", "fact",
             "active", "2026-09-21T00:02:00Z", "2026-09-21T00:02:00Z"),
        )


def main() -> int:
    config = fresh_data_dir("s1")
    db = seed(config)
    store, service = open_service(config)
    conn_a = store._connection()
    print("db_path =", db)
    print("S0 baseline _quick_check_diagnostics   =", service._quick_check_diagnostics())

    # 1) 已覆盖路径：外部写 context_layers（现有测试场景）
    with sqlite3.connect(db) as b:
        b.execute("UPDATE context_layers SET content='changed by client B' WHERE layer='l0'")
    print("S1 external context_layers write       =", service._quick_check_diagnostics())

    # 2) 常驻连接 A 先读 memories_fts（PRIMARY 模式 _legacy_reader 就是
    #    store.legacy_projection()，共用 A 连接）
    reader = service._legacy_reader()
    hits = reader.search_fts("alpha", 5)
    print("S2 legacy search on A hits =", len(hits),
          "| _quick_check_diagnostics =", service._quick_check_diagnostics())

    # 3) 外部写入 memories
    external_memory_write(config, "1")
    d1 = service._quick_check_diagnostics()
    print("S3 external memories write             =", d1)
    print("S3b repeat (idempotence)               =", service._quick_check_diagnostics())
    print("S3c raw PRAGMA quick_check rows        =", quick_rows(service))

    # 4) 只补刷 memories_fts（不刷 trigram）
    conn_a.execute("SELECT rowid FROM memories_fts LIMIT 1").fetchall()
    d2 = service._quick_check_diagnostics()
    print("S4 refresh memories_fts only           =", d2)

    # 5) 最小修复验证：两张 memories_* FTS 都补刷
    conn_a.execute("SELECT rowid FROM memories_fts_trigram LIMIT 1").fetchall()
    print("S5 refresh memories_fts + trigram      =", service._quick_check_diagnostics())

    # 6) 对照：新连接（协调者 mode=ro 只读连接视角）没有过期快照
    store2 = ContextStore(config)
    store2.initialize(create_schema=False)
    svc2 = ContextService(config, store=store2)
    svc2.initialize(mode=ContextMode.PRIMARY, adapter="codex")
    print("S6 fresh connection                    =", svc2._quick_check_diagnostics())
    print("S6b fresh raw quick_check rows         =", quick_rows(svc2))

    # 7) 反向对照：外部写入 memories，但 A 从未读过 memories_fts
    config7 = fresh_data_dir("s7")
    seed(config7)
    store_c, service_c = open_service(config7)
    print("S7 baseline untouched A                =", service_c._quick_check_diagnostics())
    external_memory_write(config7, "2")
    print("S7b external write, no prior A read    =", service_c._quick_check_diagnostics())
    print("S7c raw rows                           =", quick_rows(service_c))

    # 8) 读事务快照变体：A 开着读事务时外部写入
    config8 = fresh_data_dir("s8")
    seed(config8)
    store_d, service_d = open_service(config8)
    conn_d = store_d._connection()
    conn_d.execute("SELECT rowid FROM memories_fts LIMIT 1").fetchall()
    conn_d.execute("BEGIN")
    conn_d.execute("SELECT count(*) FROM memories").fetchone()
    external_memory_write(config8, "3")
    print("S8 quick_check during A read txn       =", quick_rows(service_d))
    conn_d.rollback()
    print("S8b after rollback (context ftes only) =", service_d._quick_check_diagnostics())

    # 9) 真实门禁视角：写入后服务整体健康（_refresh_health）
    service._refresh_health()
    status = service.status()
    print("S9 service.status() after writes       = ready:", status.ready,
          "reasons:", status.reason_codes,
          "diagnostics:", [d[:60] for d in status.diagnostics])

    store.close()
    store2.close()
    store_c.close()
    store_d.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

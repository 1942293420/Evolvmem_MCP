"""Migrate claude-mem session summaries from Chroma into EvolvMem.

Usage:
  python migrate_claude_mem.py
  python migrate_claude_mem.py --source-db /path/to/chroma.sqlite3
"""

import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

from evolvmem.config import Config
from evolvmem.context_models import ContextMode
from evolvmem.context_service import ContextService
from evolvmem.memory_store import MemoryStore
from evolvmem.vector_index import VectorIndex
from evolvmem.embedding import EmbeddingEngine
import numpy as np

CHROMA_DB = Path.home() / ".claude-mem" / "chroma" / "chroma.sqlite3"


def _ensure_legacy_schema(config: Config) -> None:
    """Create the legacy projection schema if missing.

    Migration utilities are explicitly allowed to use MemoryStore (计划约束:
    MemoryStore 仍可用于迁移); production adapters never instantiate it.
    """
    with MemoryStore(config):
        pass


def extract_summaries(source_db: str | Path | None = None) -> list[dict]:
    """Read session summaries without creating or modifying the source DB."""
    source = Path(source_db if source_db is not None else CHROMA_DB).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Source Chroma database not found: {source}")
    with closing(sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute('''
        SELECT e.id, m.string_value as doc,
               (SELECT m2.int_value FROM embedding_metadata m2
                WHERE m2.id = e.id AND m2.key = 'created_at_epoch') as created_at,
               (SELECT m2.string_value FROM embedding_metadata m2
                WHERE m2.id = e.id AND m2.key = 'project') as project
        FROM embedding_metadata m
        JOIN embeddings e ON e.id = m.id
        WHERE m.key = 'chroma:document'
        AND EXISTS (
            SELECT 1 FROM embedding_metadata m3
            WHERE m3.id = m.id AND m3.key = 'doc_type'
            AND m3.string_value = 'session_summary'
        )
        ORDER BY e.id
        ''').fetchall()

    return [{
            "chroma_id": row["id"],
            "doc": row["doc"],
            "created_at_epoch": row["created_at"],
            "project": row["project"] or "unknown",
        } for row in rows]


def import_summaries(config: Config, summaries: list[dict]) -> dict:
    """Import summaries through the mode-selected compatibility boundary.

    Constructs a ContextService with the configured context_mode: pre-cutover
    `legacy` keeps the old job (plain memories rows, no Core claims);
    post-cutover `compat` creates the mapping and three layers for each row
    instead of unmapped rows. The returned ids stay legacy IDs so the old
    vector handling below keeps working unchanged.
    """
    _ensure_legacy_schema(config)
    service = ContextService(config)
    service.initialize(
        mode=ContextMode(config.context_mode), adapter="migration"
    )
    try:
        facade = service.legacy_facade()

        # Check existing keys to avoid duplicates
        existing_keys = {m["key"] for m in facade.get_active()}

        new_ids = []
        skipped = 0
        for s in summaries:
            key = f"claude-mem:summary:{s['chroma_id']}"
            if key in existing_keys:
                skipped += 1
                continue

            # Truncate very long docs for memory efficiency
            value = s["doc"]
            if len(value) > 2000:
                value = value[:2000] + "\n\n[truncated from claude-mem migration]"

            tags = ["migrated", "claude-mem", "session-summary",
                    f"project:{s['project']}"]

            new_ids.append(facade.add(
                key=key,
                value=value,
                attribute="claude-mem-migration",
                tags=tags,
                source_session="claude-mem-migration",
            ))

        return {
            "imported": len(new_ids),
            "skipped": skipped,
            "new_ids": new_ids,
            "new_memories": facade.get_by_ids(new_ids),
        }
    finally:
        service.close()


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source-db", type=Path,
        help="Source Chroma SQLite database (default: ~/.claude-mem/chroma/chroma.sqlite3)",
    )
    args = parser.parse_args(argv)
    print("=" * 60)
    print("claude-mem → EvolvMem 数据迁移")
    print("=" * 60)

    # Step 1: Extract
    print("\n[1/4] 从 Chroma 提取 session summaries...")
    try:
        summaries = extract_summaries(args.source_db)
    except (FileNotFoundError, sqlite3.Error) as exc:
        parser.error(str(exc))
    print(f"  提取到 {len(summaries)} 条 session summary")

    if not summaries:
        print("  无数据可迁移，退出")
        return

    # Show distribution by project
    projects = {}
    for s in summaries:
        p = s["project"]
        projects[p] = projects.get(p, 0) + 1
    print("  按项目分布:")
    for p, c in sorted(projects.items()):
        print(f"    {p}: {c} 条")

    # Step 2: Import to EvolvMem SQLite (经 ContextService 兼容门面)
    print("\n[2/4] 导入 EvolvMem SQLite...")
    config = Config()
    # 门面写后同步可能在 legacy 向量索引留下 dirty 标记；记下迁移前的
    # 既有漂移，步骤 4 全量补齐新向量后只清除本次自己造成的标记。
    preexisting_vector_dirty = VectorIndex(config).is_dirty()
    result = import_summaries(config, summaries)
    new_ids = result["new_ids"]
    print(f"  新增: {result['imported']} 条, 跳过(已存在): {result['skipped']} 条")

    # Step 3: Generate embeddings for new records
    print("\n[3/4] 生成向量嵌入...")
    try:
        engine = EmbeddingEngine(config)
        engine.initialize()

        texts = [m["value"] for m in result["new_memories"]]

        print(f"  正在为 {len(texts)} 条记录生成嵌入向量(nomic-embed-text-v1.5)...")
        embeddings = engine.encode_batch(texts)
        print(f"  生成完成: {len(embeddings)} 个向量")

        engine.close()

        # Step 4: Update vector index
        print("\n[4/4] 更新 USearch 向量索引...")
        vi = VectorIndex(config)
        vi.initialize(dim=config.embedding_dim)

        current_count = vi.count()
        print(f"  当前向量索引: {current_count} 条")

        for mem_id, emb in zip(new_ids, embeddings):
            vi.add(mem_id, np.array(emb, dtype=np.float32))

        vi.save()
        if not preexisting_vector_dirty:
            vi.clear_dirty()
        print(f"  更新后向量索引: {vi.count()} 条")
        vi.close()

    except FileNotFoundError as e:
        print(f"  ⚠ 嵌入模型不可用: {e}")
        print(f"  数据已导入 SQLite (FTS5 全文搜索可用)，向量索引将在下次模型可用时重建")
    except Exception as e:
        print(f"  ⚠ 嵌入生成失败: {e}")
        print(f"  数据已导入 SQLite，向量索引待重建")

    print("\n" + "=" * 60)
    print(f"迁移完成: 导入 {result['imported']} 条, 跳过 {result['skipped']} 条")
    print("=" * 60)


if __name__ == "__main__":
    main()

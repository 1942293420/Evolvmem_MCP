"""Migrate claude-mem session summaries from Chroma into EvolvMem.

Usage:
  uv run python /home/jiangli/hermes-memory-plugin/migrate_claude_mem.py
"""

import sqlite3
import sys
import os
from datetime import datetime, timezone

# Add evolvmem to path
sys.path.insert(0, "/home/jiangli/hermes-memory-plugin")

from evolvmem.config import Config
from evolvmem.memory_store import MemoryStore
from evolvmem.vector_index import VectorIndex
from evolvmem.embedding import EmbeddingEngine
import numpy as np

CHROMA_DB = "/home/jiangli/.claude-mem/chroma/chroma.sqlite3"


def extract_summaries() -> list[dict]:
    """Extract all session summaries from claude-mem Chroma DB."""
    conn = sqlite3.connect(CHROMA_DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute('''
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
    ''')

    results = []
    for row in cur.fetchall():
        results.append({
            "chroma_id": row["id"],
            "doc": row["doc"],
            "created_at_epoch": row["created_at"],
            "project": row["project"] or "unknown",
        })
    conn.close()
    return results


def main():
    print("=" * 60)
    print("claude-mem → EvolvMem 数据迁移")
    print("=" * 60)

    # Step 1: Extract
    print("\n[1/4] 从 Chroma 提取 session summaries...")
    summaries = extract_summaries()
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

    # Step 2: Import to EvolvMem SQLite
    print("\n[2/4] 导入 EvolvMem SQLite...")
    config = Config()
    store = MemoryStore(config)
    store.initialize()

    # Check existing keys to avoid duplicates
    existing_keys = set()
    for mem in store.get_active():
        existing_keys.add(mem["key"])

    imported = 0
    skipped = 0
    new_ids = []

    for s in summaries:
        key = f"claude-mem:summary:{s['chroma_id']}"
        if key in existing_keys:
            skipped += 1
            continue

        # Truncate very long docs for memory efficiency
        value = s["doc"]
        if len(value) > 2000:
            value = value[:2000] + "\n\n[truncated from claude-mem migration]"

        tags = ["migrated", "claude-mem", "session-summary", f"project:{s['project']}"]

        new_id = store.add(
            key=key,
            value=value,
            attribute="claude-mem-migration",
            tags=tags,
            source_session="claude-mem-migration",
        )
        new_ids.append(new_id)
        imported += 1

    print(f"  新增: {imported} 条, 跳过(已存在): {skipped} 条")

    # Step 3: Generate embeddings for new records
    print("\n[3/4] 生成向量嵌入...")
    try:
        engine = EmbeddingEngine(config)
        engine.initialize()

        new_memories = store.get_by_ids(new_ids)
        texts = [m["value"] for m in new_memories]

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
        print(f"  更新后向量索引: {vi.count()} 条")
        vi.close()

    except FileNotFoundError as e:
        print(f"  ⚠ 嵌入模型不可用: {e}")
        print(f"  数据已导入 SQLite (FTS5 全文搜索可用)，向量索引将在下次模型可用时重建")
    except Exception as e:
        print(f"  ⚠ 嵌入生成失败: {e}")
        print(f"  数据已导入 SQLite，向量索引待重建")

    store.close()

    print("\n" + "=" * 60)
    print(f"迁移完成: 导入 {imported} 条, 跳过 {skipped} 条")
    print("=" * 60)


if __name__ == "__main__":
    main()

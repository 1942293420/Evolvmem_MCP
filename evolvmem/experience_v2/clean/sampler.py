"""Task 4.2: Seeded 50-item random delete sampling, scrubbing, and zero-write verification."""
from __future__ import annotations

from pathlib import Path
import random
import sqlite3
from typing import Any, Sequence

from evolvmem.experience_v2.pipeline.pilot import scrub_pilot_text


def sample_delete_manifest(
    delete_entries: Sequence[dict[str, Any]],
    *,
    sample_size: int = 50,
    seed: int = 20261114,
    extra_scrub_terms: Sequence[str] = (),
) -> list[dict[str, Any]]:
    """Deterministically sample up to `sample_size` delete candidates and scrub PII/secrets."""
    items = list(delete_entries)
    if not items:
        return []
    k = min(max(1, int(sample_size)), len(items))
    rng = random.Random(int(seed))
    picked = rng.sample(items, k) if k < len(items) else items
    terms = tuple(str(x) for x in extra_scrub_terms if x)

    out: list[dict[str, Any]] = []
    for idx, entry in enumerate(picked, 1):
        out.append(
            {
                'sample_index': idx,
                'legacy_id': str(entry.get('legacy_id') or ''),
                'source_table': str(entry.get('source_table') or ''),
                'legacy_status': str(entry.get('legacy_status') or ''),
                'legacy_category': str(entry.get('legacy_category') or ''),
                'problem': scrub_pilot_text(entry.get('problem') or '', extra_terms=terms),
                'solution': scrub_pilot_text(entry.get('solution') or '', extra_terms=terms),
                'raw_preview': scrub_pilot_text(entry.get('raw_preview') or '', extra_terms=terms),
                'gate_layer': str(entry.get('gate_layer') or ''),
                'reason_code': str(entry.get('reason_code') or ''),
                'detail': scrub_pilot_text(entry.get('detail') or '', extra_terms=terms),
            }
        )
    return out


def read_legacy_table_counts(sqlite_path: Path | str) -> dict[str, int]:
    """Read exact legacy table row counts via strict read-only SQLite URI mode."""
    path = Path(sqlite_path).resolve()
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        conn.execute('PRAGMA query_only = ON')
        ci = int(conn.execute("SELECT COUNT(*) FROM context_items WHERE content_type = 'experience'").fetchone()[0])
        cm = int(conn.execute('SELECT COUNT(*) FROM capability_memories').fetchone()[0])
        lm = int(conn.execute('SELECT COUNT(*) FROM learning_memories').fetchone()[0])
        return {
            'context_items_experience': ci,
            'capability_memories': cm,
            'learning_memories': lm,
        }
    finally:
        conn.close()


def assert_zero_sqlite_writes(sqlite_path: Path | str, pre_counts: dict[str, int]) -> dict[str, int]:
    """Verify legacy experience table row counts match pre-run counts identically."""
    post = read_legacy_table_counts(sqlite_path)
    if post != pre_counts:
        raise RuntimeError(f'SQLITE_COUNT_CHANGED: before={pre_counts} after={post}')
    return post

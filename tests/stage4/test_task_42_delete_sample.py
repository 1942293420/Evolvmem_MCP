"""TDD tests for Stage 4 Task 4.2: seeded 50-item random delete sample & scrubbing."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from evolvmem.experience_v2.clean.sampler import (
    assert_zero_sqlite_writes,
    sample_delete_manifest,
)


def _fake_delete_entries(n: int = 80) -> list[dict]:
    out = []
    layers = ('L0', 'L1', 'L2')
    reasons = (
        'legacy_inactive_status',
        'duplicate_conversation_snapshot',
        'l2_no_value_known',
    )
    for i in range(n):
        idx = i % 3
        out.append(
            {
                'legacy_id': f'capability_memories:{1000 + i}',
                'source_table': 'capability_memories',
                'legacy_status': 'active',
                'legacy_category': 'delivery',
                'problem': f'Item {i} on /home/alice/work host 192.168.1.99',
                'solution': f'Check Bearer sk-secretkey99999999 for item {i}',
                'raw_preview': f'Raw preview {i} from C:\\Users\\alice\\repo',
                'gate_layer': layers[idx],
                'reason_code': reasons[idx],
                'detail': 'filtered by stage4 clean',
            }
        )
    return out


def test_sample_delete_manifest_seeded_50_and_scrubbed():
    entries = _fake_delete_entries(80)
    s1 = sample_delete_manifest(entries, sample_size=50, seed=20261114, extra_scrub_terms=('alice',))
    s2 = sample_delete_manifest(entries, sample_size=50, seed=20261114, extra_scrub_terms=('alice',))

    assert len(s1) == 50
    assert s1 == s2  # reproducible seed
    assert len({x['legacy_id'] for x in s1}) == 50

    required_keys = {
        'sample_index', 'legacy_id', 'source_table', 'legacy_status', 'legacy_category',
        'problem', 'solution', 'raw_preview', 'gate_layer', 'reason_code', 'detail',
    }
    for row in s1:
        assert required_keys.issubset(row.keys())
    blob = json.dumps(s1, ensure_ascii=False)
    assert '192.168.1.99' not in blob
    assert 'sk-secretkey' not in blob
    assert '/home/alice' not in blob
    assert 'Users\\alice' not in blob


def test_sample_delete_manifest_handles_fewer_than_50():
    entries = _fake_delete_entries(12)
    s = sample_delete_manifest(entries, sample_size=50, seed=42)
    assert len(s) == 12


def test_assert_zero_sqlite_writes_detects_or_confirms_counts(tmp_path: Path):
    db_path = tmp_path / 's.db'
    c = sqlite3.connect(db_path)
    c.executescript(
        """
        CREATE TABLE context_items (id INTEGER PRIMARY KEY, content_type TEXT);
        CREATE TABLE capability_memories (memory_id INTEGER PRIMARY KEY);
        CREATE TABLE learning_memories (item_id INTEGER PRIMARY KEY);
        INSERT INTO context_items VALUES (1, 'experience');
        INSERT INTO capability_memories VALUES (10);
        INSERT INTO learning_memories VALUES (1);
        """
    )
    c.commit()
    c.close()

    pre_counts = {'context_items_experience': 1, 'capability_memories': 1, 'learning_memories': 1}
    assert_zero_sqlite_writes(db_path, pre_counts)

    c = sqlite3.connect(db_path)
    c.execute('DELETE FROM capability_memories WHERE memory_id = 10')
    c.commit()
    c.close()
    with pytest.raises(RuntimeError, match='SQLITE_COUNT_CHANGED'):
        assert_zero_sqlite_writes(db_path, pre_counts)

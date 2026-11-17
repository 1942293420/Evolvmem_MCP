"""TDD tests for Stage 4 Tasks 4.3-4.5: backup export, FK/FTS/USearch delete, PG legacy migration, registry."""
from __future__ import annotations

from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import sqlite3

import numpy as np
import pytest
from usearch.index import Index, MetricKind, ScalarKind

from evolvmem.experience_v2.changes_sync import recall_pg_experiences
from evolvmem.experience_v2.clean.executor import (
    apply_rescue_overrides,
    delete_approved_legacy_sqlite_and_vectors,
    export_deleted_legacy_backup,
)
from evolvmem.experience_v2.clean.legacy_reader import read_legacy_experience_candidates
from evolvmem.experience_v2.clean.migrator import (
    _RESCUE_16405_RULE,
    legacy_pg_note_id,
    migrate_kept_legacy_to_pg,
    prepare_legacy_keep_units,
    register_legacy_cleanup_manifest,
)
from evolvmem.experience_v2.pg_experience_store import PgExperienceSettings, PgExperienceStore
from evolvmem.experience_v2.writer import ExperienceWriter, Op, TicketIssuer


class _DetEmbedder:
    def encode_document(self, text: str) -> list[float]:
        v = [0.0] * 768
        v[abs(hash(text)) % 64] = 1.0
        return v


def _build_realistic_sqlite_and_vectors(tmp_path: Path) -> Path:
    db_path = tmp_path / 'memory.db'
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE context_items (
            id INTEGER PRIMARY KEY,
            identity_key TEXT NOT NULL,
            content_type TEXT NOT NULL,
            project TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT 'project',
            status TEXT NOT NULL DEFAULT 'active',
            tier TEXT NOT NULL DEFAULT 'normal',
            tags TEXT NOT NULL DEFAULT '',
            importance REAL NOT NULL DEFAULT 5.0,
            confidence REAL NOT NULL DEFAULT 0.5,
            source_state TEXT NOT NULL DEFAULT 'none',
            source_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            access_count INTEGER NOT NULL DEFAULT 0,
            last_accessed TEXT,
            last_verified_at TEXT,
            expires_at TEXT,
            supersedes INTEGER REFERENCES context_items(id),
            superseded_by INTEGER REFERENCES context_items(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            experience_payload TEXT NOT NULL DEFAULT '',
            personal_general INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE context_layers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES context_items(id) ON DELETE CASCADE,
            layer TEXT NOT NULL CHECK (layer IN ('l0', 'l1', 'l2')),
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            generator TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(item_id, layer)
        );
        CREATE VIRTUAL TABLE context_layers_fts USING fts5(content, content='context_layers', content_rowid='id');
        CREATE VIRTUAL TABLE context_layers_fts_trigram USING fts5(content, content='context_layers', content_rowid='id', tokenize='trigram');
        CREATE TRIGGER context_layers_fts_ad AFTER DELETE ON context_layers BEGIN
            INSERT INTO context_layers_fts(context_layers_fts, rowid, content) VALUES('delete', old.id, old.content);
            INSERT INTO context_layers_fts_trigram(context_layers_fts_trigram, rowid, content) VALUES('delete', old.id, old.content);
        END;
        CREATE TABLE context_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES context_items(id) ON DELETE CASCADE,
            source_ref TEXT NOT NULL
        );
        CREATE TABLE context_project_resolutions (
            item_id INTEGER PRIMARY KEY REFERENCES context_items(id),
            resolution_state TEXT NOT NULL
        );
        CREATE TABLE history_experience_ingest_materials (
            card_key TEXT PRIMARY KEY,
            item_id INTEGER REFERENCES context_items(id),
            raw_card_json TEXT NOT NULL
        );
        CREATE TABLE capability_memories (
            memory_id INTEGER PRIMARY KEY,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            source_project TEXT NOT NULL DEFAULT '',
            source_ref TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE capability_memories_fts USING fts5(title, content, content='capability_memories', content_rowid='memory_id');
        CREATE TABLE capability_sources (
            source_id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL REFERENCES capability_memories(memory_id),
            context_item_id INTEGER
        );
        CREATE TABLE capability_assessments (
            assessment_id INTEGER PRIMARY KEY AUTOINCREMENT,
            memory_id INTEGER NOT NULL REFERENCES capability_memories(memory_id),
            score INTEGER NOT NULL
        );
        CREATE TABLE learning_memories (
            item_id INTEGER PRIMARY KEY REFERENCES context_items(id) ON DELETE CASCADE,
            category TEXT NOT NULL,
            payload TEXT NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE continuity_workstreams (
            id INTEGER PRIMARY KEY,
            current_context_id INTEGER REFERENCES context_items(id)
        );
        CREATE TABLE session_archives (id INTEGER PRIMARY KEY, session_id TEXT NOT NULL);
        CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT NOT NULL);
        """
    )

    now = '2026-10-10T10:00:00Z'
    ep_keep = json.dumps({
        'problem': '经验子集按 id 归属导出前未排序导致分页游标漏项',
        'rationale': '分页游标依赖稳定主键有序输出',
        'steps': ['使用 id ASC 与 created_at DESC 联合排序'],
        'transferable': True,
        'project': 'evolvmem',
    }, ensure_ascii=False)
    ep_del = json.dumps({
        'problem': '本轮已关闭旧窗口',
        'rationale': '临时操作记录',
        'steps': ['关闭窗口'],
        'transferable': False,
        'project': 'evolvmem',
    }, ensure_ascii=False)

    # Kept experience context_item + shared-PK fk-inherit learning child (id=5938)
    conn.execute(
        "INSERT INTO context_items(id, identity_key, content_type, project, status, created_at, updated_at, experience_payload) "
        "VALUES (5938, 'ci-keep-5938', 'experience', 'evolvmem', 'active', ?, ?, ?)",
        (now, now, ep_keep),
    )
    conn.execute("INSERT INTO context_layers(item_id, layer, content, content_hash, generator, created_at, updated_at) VALUES (5938, 'l0', 'experience: test keep', 'h1', 'g', ?, ?)", (now, now))
    conn.execute("INSERT INTO learning_memories(item_id, category, payload) VALUES (5938, 'experience', ?)", (json.dumps({'source_session': 'session_keep_5938', 'l0': '共用父子经验复用规则'}),))

    # Deleted experience context_item (id=101) with NO ACTION FK rows in context_project_resolutions & history_experience_ingest_materials
    conn.execute(
        "INSERT INTO context_items(id, identity_key, content_type, project, status, created_at, updated_at, experience_payload) "
        "VALUES (101, 'ci-del-101', 'experience', 'evolvmem', 'active', ?, ?, ?)",
        (now, now, ep_del),
    )
    conn.execute("INSERT INTO context_layers(id, item_id, layer, content, content_hash, generator, created_at, updated_at) VALUES (101, 101, 'l0', 'deleted experience layer content', 'h2', 'g', ?, ?)", (now, now))
    conn.execute("INSERT INTO context_layers_fts(rowid, content) VALUES (101, 'deleted experience layer content')")
    conn.execute("INSERT INTO context_layers_fts_trigram(rowid, content) VALUES (101, 'deleted experience layer content')")
    conn.execute("INSERT INTO context_sources(item_id, source_ref) VALUES (101, 'conv:del-101')")
    conn.execute("INSERT INTO context_project_resolutions(item_id, resolution_state) VALUES (101, 'resolved')")
    conn.execute("INSERT INTO history_experience_ingest_materials(card_key, item_id, raw_card_json) VALUES ('card-101', 101, '{\"k\":1}')")

    # Untouched non-experience fact context_item (id=202)
    conn.execute(
        "INSERT INTO context_items(id, identity_key, content_type, project, status, created_at, updated_at) "
        "VALUES (202, 'ci-fact-202', 'fact', 'evolvmem', 'active', ?, ?)",
        (now, now),
    )
    conn.execute("INSERT INTO context_layers(id, item_id, layer, content, content_hash, generator, created_at, updated_at) VALUES (202, 202, 'l0', 'fact layer intact', 'h3', 'g', ?, ?)", (now, now))
    conn.execute("INSERT INTO context_layers_fts(rowid, content) VALUES (202, 'fact layer intact')")
    conn.execute("INSERT INTO context_layers_fts_trigram(rowid, content) VALUES (202, 'fact layer intact')")

    # Standalone learning_memories: id=16405 (rescued to keep!) and id=20001 (deleted, keeping parent context_item fact intact!)
    conn.execute("INSERT INTO context_items(id, identity_key, content_type, project, status, created_at, updated_at) VALUES (16405, 'lm-16405', 'fact', 'pricing', 'active', ?, ?)", (now, now))
    conn.execute("INSERT INTO context_layers(item_id, layer, content, content_hash, generator, created_at, updated_at) VALUES (16405, 'l0', 'fact: 物流费兜底字段口径', 'h4', 'g', ?, ?)", (now, now))
    conn.execute("INSERT INTO learning_memories(item_id, category, payload) VALUES (16405, 'reference', ?)", (json.dumps({'source_session': 'conv:16405', 'content': '产品物流费兜底沿用原脚本的参考-墨西哥物流费字段，当前其他国家也会用到。', 'rationale': '助手说明兜底字段沿用情况，未经用户确认。'}),))

    conn.execute("INSERT INTO context_items(id, identity_key, content_type, project, status, created_at, updated_at) VALUES (20001, 'lm-20001', 'fact', 'pricing', 'active', ?, ?)", (now, now))
    conn.execute("INSERT INTO learning_memories(item_id, category, payload) VALUES (20001, 'reference', ?)", (json.dumps({'source_session': 'conv:20001', 'content': '可删除的临时静态参考记录'}),))

    # capability_memories: id=3076 (rescued!) and id=4001 (deleted with NO ACTION assessments & sources!)
    conn.execute("INSERT INTO capability_memories(memory_id, category, title, content, source_project, source_ref, status, created_at, updated_at) VALUES (3076, 'architecture', '经验记分设计：零召回不减分', '只对有真实纠偏事件的召回条目减分，从未召回条目保持未知权重。', 'evolvmem', 'conv:3076', 'active', ?, ?)", (now, now))
    conn.execute("INSERT INTO capability_memories(memory_id, category, title, content, source_project, source_ref, status, created_at, updated_at) VALUES (4001, 'delivery', '已上线进度汇报', 'Stage 2 已交付并重启服务', 'evolvmem', 'conv:4001', 'active', ?, ?)", (now, now))
    conn.execute("INSERT INTO capability_memories_fts(capability_memories_fts) VALUES('rebuild')")
    conn.execute("INSERT INTO capability_sources(memory_id, context_item_id) VALUES (4001, 101)")
    conn.execute("INSERT INTO capability_sources(memory_id, context_item_id) VALUES (3076, 101)")
    conn.execute("INSERT INTO capability_assessments(memory_id, score) VALUES (4001, 20)")

    conn.execute("INSERT INTO session_archives VALUES (1, 'sess-raw-1')")
    conn.execute("INSERT INTO memories VALUES (1, 'legacy-mem-raw-1')")
    conn.commit()
    conn.close()

    # Build .usearch indexes + .provenance.json + untouched vectors.usearch
    vec1 = np.array([1.0] + [0.0] * 767, dtype=np.float32)
    vec2 = np.array([0.0, 1.0] + [0.0] * 766, dtype=np.float32)

    cap_idx = Index(ndim=768, metric=MetricKind.Cos, dtype=ScalarKind.F32)
    cap_idx.add(3076, vec1)
    cap_idx.add(4001, vec2)
    cap_idx.save(str(tmp_path / 'capability_vectors.usearch'))
    (tmp_path / 'capability_vectors.provenance.json').write_text(
        json.dumps({'version': 1, 'contract': ['bge', 768, '', 512], 'generation': 5, 'cursor': 10, 'items': {'3076': {'text': 'a', 'vector': 'b'}, '4001': {'text': 'c', 'vector': 'd'}}}),
        encoding='utf-8',
    )

    ctx_idx = Index(ndim=768, metric=MetricKind.Cos, dtype=ScalarKind.F32)
    ctx_idx.add(101, vec1)
    ctx_idx.add(202, vec2)
    ctx_idx.save(str(tmp_path / 'context_vectors.usearch'))
    (tmp_path / 'context_vectors.provenance.json').write_text(
        json.dumps({'version': 1, 'contract': ['bge', 768, '', 512], 'items': {'101': {'text': 'x', 'vector': 'y'}, '202': {'text': 'z', 'vector': 'w'}}}),
        encoding='utf-8',
    )

    mem_idx = Index(ndim=768, metric=MetricKind.Cos, dtype=ScalarKind.F32)
    mem_idx.add(1, vec1)
    mem_idx.save(str(tmp_path / 'vectors.usearch'))
    return db_path


def test_rescue_backup_delete_migrate_and_registry_end_to_end(pg_s4_migrated, tmp_path):
    conn, schema, dsn = pg_s4_migrated
    secret = b'stage4-exec-test-secret'

    # Check migration 004 created legacy_cleanup_registry
    assert conn.execute("SELECT to_regclass('legacy_cleanup_registry')").fetchone()[0] == 'legacy_cleanup_registry'

    # Seed one pre-existing manual active note in PG (must stay intact and remain manual-only in PgExperienceStore)
    writer = ExperienceWriter(secret)
    issuer = TicketIssuer(secret, gate_id='manual')
    m_ticket = issuer.issue(proposal_uid='manual:req-manual-1', decision_id='dec-manual-1', lane='user_fast')
    writer.apply(
        conn,
        m_ticket,
        Op(
            action='create',
            owner_id='owner-1',
            actor_id='owner-1',
            request_id='req-manual-1',
            category='系统环境经验',
            problem='现役手工经验用于校验不受清理影响',
            solution='保留现役手工经验并可与迁入历史经验一同被检索召回',
            reusable_experience='手工经验与 legacy 经验可在 PG 中按 origin 隔离',
            origin='manual',
            embedding=_DetEmbedder().encode_document('manual-1'),
        ),
    )
    conn.commit()

    db_path = _build_realistic_sqlite_and_vectors(tmp_path)
    vec_mem_sha_before = hashlib.sha256((tmp_path / 'vectors.usearch').read_bytes()).hexdigest()

    keep_raw = [
        {'legacy_id': 'context_items:5938', 'source_table': 'context_items', 'raw_pk': 5938, 'gate_layer': 'L2', 'reason_code': 'valuable_l2'},
        {'legacy_id': 'learning_memories:5938', 'source_table': 'learning_memories', 'raw_pk': 5938, 'gate_layer': 'L2', 'reason_code': 'fk_inherit_keep'},
    ]
    del_raw = [
        {'legacy_id': 'context_items:101', 'source_table': 'context_items', 'raw_pk': 101, 'gate_layer': 'L0', 'reason_code': 'legacy_non_transferable'},
        {'legacy_id': 'learning_memories:16405', 'source_table': 'learning_memories', 'raw_pk': 16405, 'gate_layer': 'L2', 'reason_code': 'common_knowledge'},
        {'legacy_id': 'learning_memories:20001', 'source_table': 'learning_memories', 'raw_pk': 20001, 'gate_layer': 'L0', 'reason_code': 'legacy_static_fact_not_experience'},
        {'legacy_id': 'capability_memories:3076', 'source_table': 'capability_memories', 'raw_pk': 3076, 'gate_layer': 'L2', 'reason_code': 'common_knowledge'},
        {'legacy_id': 'capability_memories:4001', 'source_table': 'capability_memories', 'raw_pk': 4001, 'gate_layer': 'L0', 'reason_code': 'legacy_delivery_or_ops_log'},
    ]

    keep_final, del_final = apply_rescue_overrides(keep_raw, del_raw)
    assert {x['legacy_id'] for x in keep_final} == {'context_items:5938', 'learning_memories:5938', 'capability_memories:3076', 'learning_memories:16405'}
    assert {x['legacy_id'] for x in del_final} == {'context_items:101', 'learning_memories:20001', 'capability_memories:4001'}

    # Read raw SQLite records BEFORE deletion and prepare 3 distinct units (5938 child merges into parent)
    raw_by_id = {r.legacy_id: r for r in read_legacy_experience_candidates(db_path)}
    units = prepare_legacy_keep_units(raw_by_id, keep_final)
    assert len(units) == 3
    by_lid = {u['legacy_id']: u for u in units}
    assert any(ev.get('merged_child_legacy_id') == 'learning_memories:5938' for ev in by_lid['context_items:5938']['evidence'])
    assert by_lid['learning_memories:16405']['reusable_experience'] == _RESCUE_16405_RULE

    # Refuse SQLite delete without valid backup receipt
    with pytest.raises(ValueError, match='backup'):
        delete_approved_legacy_sqlite_and_vectors(
            db_path, tmp_path, [x['legacy_id'] for x in del_final],
            backup_receipt={'path': str(tmp_path / 'missing.jsonl.gz'), 'sha256': '0', 'exported_count': 3},
        )

    # Reject non-experience context_items before mutating child tables
    bad_gz = tmp_path / 'bad-non-exp.jsonl.gz'
    bad_receipt = export_deleted_legacy_backup(db_path, ['context_items:202'], bad_gz, retain_days=30)
    with pytest.raises(ValueError, match='non_experience_context_item_in_delete_ids'):
        delete_approved_legacy_sqlite_and_vectors(db_path, tmp_path, ['context_items:202'], backup_receipt=bad_receipt)

    # Task 4.3a: Export .jsonl.gz backup first
    gz_path = tmp_path / 'deleted-backup.jsonl.gz'
    receipt = export_deleted_legacy_backup(db_path, [x['legacy_id'] for x in del_final], gz_path, retain_days=30)
    assert receipt['exported_count'] == 3
    with gzip.open(gz_path, 'rt', encoding='utf-8') as fh:
        lines = [json.loads(line) for line in fh if line.strip()]
    assert lines[0]['record_type'] == 'backup_header'
    assert {row['legacy_id'] for row in lines[1:]} == {'context_items:101', 'learning_memories:20001', 'capability_memories:4001'}

    # Task 4.4: Migrate kept legacy items to PG via AdmissionTicket + ExperienceWriter
    mig_res = migrate_kept_legacy_to_pg(
        conn, units=units, owner_id='owner-1', ticket_secret=secret, embedder=_DetEmbedder(),
    )
    assert mig_res['migrated'] == 3
    assert mig_res['pg_legacy_active_total'] == 3
    assert conn.execute("SELECT COUNT(*) FROM experience_notes WHERE owner_id='owner-1' AND origin='manual' AND deleted_at IS NULL").fetchone()[0] == 1

    # Idempotent rerun creates 0 duplicates
    mig_again = migrate_kept_legacy_to_pg(
        conn, units=units, owner_id='owner-1', ticket_secret=secret, embedder=_DetEmbedder(),
    )
    assert mig_again['already_present'] == 3
    assert mig_again['pg_legacy_active_total'] == 3

    # PgExperienceStore only shows manual notes (total=1), while recall_pg_experiences searches both!
    store = PgExperienceStore(PgExperienceSettings(dsn=dsn, schema=schema, ticket_secret=secret))
    manual_listing = store.read({'user_id': 'owner-1'}, {}, category='', query='', page=1, page_size=20, deleted=0)
    assert manual_listing['summary']['total'] == 1
    rec = recall_pg_experiences(conn, owner_id='owner-1', query='经验记分设计 零召回', max_chars=2400)
    assert any(r['id'] == legacy_pg_note_id('owner-1', 'capability_memories:3076') for r in rec['results'])

    # Task 4.3b: Execute FK/FTS/USearch physical deletion
    del_res = delete_approved_legacy_sqlite_and_vectors(
        db_path, tmp_path, [x['legacy_id'] for x in del_final], backup_receipt=receipt,
    )
    assert del_res['deleted']['context_items'] == 1
    assert del_res['deleted']['capability_memories'] == 1
    assert del_res['deleted']['learning_memories'] == 1

    sconn = sqlite3.connect(db_path)
    assert sconn.execute('PRAGMA foreign_key_check').fetchall() == []
    assert sconn.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
    assert sconn.execute("SELECT COUNT(*) FROM context_items WHERE content_type != 'experience'").fetchone()[0] == 3
    assert sconn.execute("SELECT COUNT(*) FROM history_experience_ingest_materials WHERE card_key='card-101' AND item_id IS NULL").fetchone()[0] == 1
    assert sconn.execute('SELECT COUNT(*) FROM session_archives').fetchone()[0] == 1
    assert sconn.execute('SELECT COUNT(*) FROM memories').fetchone()[0] == 1
    assert sconn.execute("SELECT COUNT(*) FROM capability_sources WHERE memory_id=3076 AND context_item_id IS NULL").fetchone()[0] == 1
    assert [r[0] for r in sconn.execute("SELECT rowid FROM capability_memories_fts WHERE capability_memories_fts MATCH '零召回*'").fetchall()] == [3076]
    assert sconn.execute("SELECT rowid FROM capability_memories_fts WHERE capability_memories_fts MATCH '已上线进度汇报'").fetchall() == []
    sconn.close()

    # Check vector indexes & vectors.usearch
    assert hashlib.sha256((tmp_path / 'vectors.usearch').read_bytes()).hexdigest() == vec_mem_sha_before
    cap_after = Index.restore(str(tmp_path / 'capability_vectors.usearch'), view=True)
    assert [int(k) for k in cap_after.keys] == [3076]
    cap_prov = json.loads((tmp_path / 'capability_vectors.provenance.json').read_text(encoding='utf-8'))
    assert set(cap_prov['items']) == {'3076'}
    ctx_after = Index.restore(str(tmp_path / 'context_vectors.usearch'), view=True)
    assert [int(k) for k in ctx_after.keys] == [202]
    ctx_prov = json.loads((tmp_path / 'context_vectors.provenance.json').read_text(encoding='utf-8'))
    assert set(ctx_prov['items']) == {'202'}

    # Task 4.5: Register cleanup & manifest in PG
    manifest_path = tmp_path / 'pg-migration-manifest.json'
    reg = register_legacy_cleanup_manifest(
        conn,
        cleanup_id='stage4-clean-20261114',
        owner_id='owner-1',
        approved_by='owner',
        approved_at=datetime(2026, 11, 14, 13, 48, 30, tzinfo=timezone.utc),
        backup_receipt=receipt,
        delete_summary=del_res,
        migrate_summary=mig_res,
        rescued_ids=['capability_memories:3076', 'learning_memories:16405'],
        scrubbed_backup_label='clean/legacy-deleted-20261114.jsonl.gz',
        manifest_out_path=manifest_path,
    )
    assert '用户授权删除' in reg['authorization_note']
    assert manifest_path.is_file()
    row_cnt = conn.execute("SELECT COUNT(*) FROM legacy_cleanup_registry WHERE cleanup_id = 'stage4-clean-20261114'").fetchone()[0]
    assert row_cnt == 1

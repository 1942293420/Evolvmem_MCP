"""Task 2.4: product_policy experience v2 flags, PG storage adapter parity, and strict 503 fail-closed."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import uuid

import pytest

from evolvmem import manual_experience_mcp, manual_experiences, product_policy
from evolvmem.experience_v2.schema import apply_experience_migrations
from evolvmem.experience_v2.writer import scan_unauthorized_experience_notes_writes
from evolvmem.manual_experience_search import ManualExperienceSearch

REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_policy(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return path


def _seed_sqlite_note(db_path: Path, owner_id: str = 'owner-alpha') -> None:
    conn = sqlite3.connect(db_path)
    try:
        for sql in manual_experiences.DDL:
            conn.execute(sql)
        conn.execute(
            'INSERT INTO manual_experience_notes VALUES (?,?,?,?,?,?,1,NULL,?,?)',
            (
                'manual-' + 'a' * 32,
                owner_id,
                '前端经验',
                'SQLite decoy problem',
                'SQLite decoy solution',
                'SQLite decoy rule',
                '2026-11-13T00:00:00+00:00',
                '2026-11-13T00:00:00+00:00',
            ),
        )
        conn.commit()
    finally:
        conn.close()


class _FakeEmbedder:
    def initialize(self):
        return None

    def encode(self, text: str, kind: str) -> list[float]:
        if kind == 'query' or any(tok in text for tok in ('布局', '排版', '对齐', '样式', '按钮', '窄屏')):
            return [1.0, 0.0, 0.0]
        return [0.0, 1.0, 0.0]


def test_policy_experience_v2_flags_and_fail_closed(tmp_path: Path, monkeypatch):
    monkeypatch.delenv('EVOLVMEM_PRODUCT_POLICY_FILE', raising=False)
    monkeypatch.setenv('HOME', str(tmp_path))
    default_policy = product_policy.load_policy()
    assert default_policy.valid is True
    assert default_policy.experience_v2_enabled is False
    assert default_policy.experience_gate_mode == 'review'
    assert default_policy.experience_pipeline_enabled is False
    assert default_policy.experience_backfill_enabled is False

    good = _write_policy(
        tmp_path / 'good.json',
        {
            'console_enabled': True,
            'ai_processing_enabled': False,
            'experience_v2_enabled': True,
            'experience_gate_mode': {'前端经验': 'partial', '系统环境经验': 'review'},
            'experience_pipeline_enabled': True,
            'experience_backfill_enabled': False,
        },
    )
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(good))
    loaded = product_policy.load_policy()
    assert loaded.valid is True
    assert loaded.experience_v2_enabled is True
    assert loaded.experience_gate_mode == {'前端经验': 'partial', '系统环境经验': 'review'}
    assert loaded.experience_pipeline_enabled is True
    assert loaded.experience_backfill_enabled is False
    assert loaded.as_dict()['experience_v2_enabled'] is True

    bad = _write_policy(
        tmp_path / 'bad.json',
        {'experience_v2_enabled': True, 'experience_gate_mode': 'yolo'},
    )
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(bad))
    failed = product_policy.load_policy()
    assert failed.valid is False
    assert failed.experience_v2_enabled is False
    assert failed.console_enabled is False
    assert failed.ai_processing_enabled is False


def test_pg_storage_switch_crud_idempotency_and_search_filtering(pg_exp_schema, tmp_path: Path, monkeypatch):
    conn, schema, dsn = pg_exp_schema
    apply_experience_migrations(conn)
    conn.commit()

    policy_path = _write_policy(
        tmp_path / 'policy.json',
        {'console_enabled': True, 'experience_v2_enabled': True},
    )
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(policy_path))
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_DSN', str(dsn))
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_SCHEMA', schema)

    data_dir = tmp_path / 'personal'
    data_dir.mkdir()
    decoy_db = data_dir / 'memory.db'
    _seed_sqlite_note(decoy_db, 'u1')
    source = {'user_id': 'u1', 'data_dir': str(data_dir)}

    # PG search consumes stored attested projections. The old supplied searcher
    # must never cold-encode the candidate documents on a read.
    from types import SimpleNamespace
    from evolvmem.experience_v2.embedding_client import EmbeddingClient, note_content_sha256
    class WarmProjection:
        retrieval_contract_id = 'test-task24-warm-1024'
        legacy_contract_id = 'test-task24-warm-768'
        def encode_query(self, text, *, deadline): return [1.0]+[0.0]*1023
        def encode_note(self, note, *, deadline=None):
            return SimpleNamespace(legacy_embedding=[1.0]+[0.0]*767,
                retrieval_embedding=[1.0]+[0.0]*1023, contract_id=self.retrieval_contract_id,
                content_sha256=note_content_sha256(note))
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_EMBEDDING_CONFIG','isolated-warm-service')
    monkeypatch.setattr(EmbeddingClient,'from_environment',classmethod(lambda cls: WarmProjection()))

    req1 = 'req-t24-' + uuid.uuid4().hex[:12]
    status, created = manual_experiences.write(
        source,
        'u1',
        {
            'action': 'create',
            'request_id': req1,
            'category': '前端经验',
            'problem': '移动端弹窗按钮错位溢出屏幕',
            'solution': '给弹窗容器补充 flex-wrap 与最小宽度约束',
            'reusable_experience': '固定宽度按钮组必须允许换行并在窄屏验证',
        },
    )
    assert status == 201
    assert created['replayed'] is False
    note = created['experience']
    assert note['id'].startswith('manual-')
    assert note['revision'] == 1

    # Replay returns same note once without creating another event row
    status_rep, replayed = manual_experiences.write(
        source,
        'u1',
        {
            'action': 'create',
            'request_id': req1,
            'category': '前端经验',
            'problem': '移动端弹窗按钮错位溢出屏幕',
            'solution': '给弹窗容器补充 flex-wrap 与最小宽度约束',
            'reusable_experience': '固定宽度按钮组必须允许换行并在窄屏验证',
        },
    )
    assert status_rep == 201
    assert replayed['replayed'] is True
    assert replayed['experience']['id'] == note['id']
    ev_count = conn.execute('SELECT count(*) FROM experience_events WHERE owner_id = %s', ('u1',)).fetchone()[0]
    assert ev_count == 1

    # Seed dormant and retired notes directly in PG; must stay hidden in search/list
    conn.execute(
        """
        INSERT INTO experience_notes(
            id, owner_id, category, problem, solution, reusable_experience, origin, status
        ) VALUES
        (%s, 'u1', '前端经验', '休眠布局笔记不应出现', '休眠方案', '休眠规则', 'manual', 'dormant'),
        (%s, 'u1', '前端经验', '退役布局笔记不应出现', '退役方案', '退役规则', 'manual', 'retired')
        """,
        ('manual-' + 'b' * 32, 'manual-' + 'c' * 32),
    )
    conn.commit()

    searcher = ManualExperienceSearch(_FakeEmbedder())
    listed = manual_experiences.read(source, {'q': '布局'}, search=searcher)
    assert listed['connected'] is True
    assert listed['total'] == 1
    assert listed['search_mode'] == 'hybrid'
    assert [r['id'] for r in listed['rows']] == [note['id']]
    assert all('SQLite decoy' not in r['problem'] for r in listed['rows'])

    # Update -> delete -> restore parity
    st_upd, updated = manual_experiences.write(
        source,
        'u1',
        {
            'action': 'update',
            'id': note['id'],
            'revision': 1,
            'request_id': 'req-upd-' + uuid.uuid4().hex[:12],
            'category': '前端经验',
            'problem': '移动端弹窗按钮错位溢出屏幕（已修）',
            'solution': '改用 CSS grid 自适应列宽',
            'reusable_experience': '固定宽度按钮组必须允许换行并在窄屏验证',
        },
    )
    assert st_upd == 200
    assert updated['experience']['revision'] == 2

    st_del, _ = manual_experiences.write(
        source,
        'u1',
        {'action': 'delete', 'id': note['id'], 'revision': 2, 'request_id': 'req-del-' + uuid.uuid4().hex[:12]},
    )
    assert st_del == 200
    assert manual_experiences.read(source, {'q': '按钮'}, search=searcher)['total'] == 0
    assert manual_experiences.read(source, {'deleted': 1})['total'] == 1

    st_res, restored = manual_experiences.write(
        source,
        'u1',
        {'action': 'restore', 'id': note['id'], 'revision': 3, 'request_id': 'req-res-' + uuid.uuid4().hex[:12]},
    )
    assert st_res == 200
    assert restored['experience']['revision'] == 4
    assert restored['experience']['deleted_at'] is None


def test_pg_v2_enabled_fails_closed_without_sqlite_fallback(tmp_path: Path, monkeypatch):
    policy_path = _write_policy(
        tmp_path / 'policy.json',
        {'console_enabled': True, 'experience_v2_enabled': True},
    )
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(policy_path))
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_DSN', 'postgresql://invalid:invalid@127.0.0.1:1/nope?connect_timeout=1')
    monkeypatch.delenv('EVOLVMEM_EXPERIENCE_PG_SCHEMA', raising=False)
    monkeypatch.delenv('EVOLVMEM_CONSOLE_PG_CONFIG', raising=False)

    data_dir = tmp_path / 'personal'
    data_dir.mkdir()
    _seed_sqlite_note(data_dir / 'memory.db', 'u1')
    source = {'user_id': 'u1', 'data_dir': str(data_dir)}

    with pytest.raises(manual_experiences.ManualExperienceError) as read_exc:
        manual_experiences.read(source, {})
    assert read_exc.value.status == 503
    assert read_exc.value.code == 'pg_experience_unavailable'

    with pytest.raises(manual_experiences.ManualExperienceError) as write_exc:
        manual_experiences.write(
            source,
            'u1',
            {
                'action': 'create',
                'request_id': 'req-fail-' + uuid.uuid4().hex[:10],
                'category': '前端经验',
                'problem': 'P',
                'solution': 'S',
                'reusable_experience': 'R',
            },
        )
    assert write_exc.value.status == 503
    assert write_exc.value.code == 'pg_experience_unavailable'


def test_mcp_calls_work_against_pg_without_memory_db_and_ast_clean(pg_exp_schema, tmp_path: Path, monkeypatch):
    conn, schema, dsn = pg_exp_schema
    apply_experience_migrations(conn)
    conn.commit()

    policy_path = _write_policy(
        tmp_path / 'policy.json',
        {'console_enabled': True, 'experience_v2_enabled': True},
    )
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(policy_path))
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_DSN', str(dsn))
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_SCHEMA', schema)

    # No memory.db on disk when PG v2 is active!
    data_dir = tmp_path / 'no-sqlite-here'
    data_dir.mkdir()
    owner_dir = data_dir
    lan_dir = tmp_path / 'lan'
    lan_dir.mkdir()

    fake_console = type(
        'ConsoleStub',
        (),
        {'owner_id': 'owner1', 'manual_experience_source': lambda self, uid: {'user_id': uid, 'data_dir': str(data_dir)}},
    )()
    monkeypatch.setattr(manual_experience_mcp, 'configured_console', lambda **kw: fake_console)

    src = manual_experience_mcp.registered_source(
        data_dir, owner_data_dir=owner_dir, lan_data_dir=lan_dir, user='owner1'
    )
    saved = manual_experience_mcp.call(
        'manual_experience_save',
        {
            'category': '系统环境经验',
            'problem': '容器 DNS 解析超时',
            'solution': '在 daemon config 固定 dns 地址并重启服务',
            'reusable_experience': '排查内网容器超时先核对 resolv 配置',
            'request_id': 'mcp-pg-' + uuid.uuid4().hex[:10],
        },
        src,
    )
    assert saved['ok'] is True
    assert saved['experience']['id'].startswith('manual-')

    found = manual_experience_mcp.call('manual_experience_search', {'q': 'DNS'}, src)
    assert found['ok'] is True
    assert len(found['rows']) == 1
    assert found['rows'][0]['id'] == saved['experience']['id']
    assert scan_unauthorized_experience_notes_writes(REPO_ROOT) == []

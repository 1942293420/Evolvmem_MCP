"""Stage 5 regressions for live em_history sync and R9 circuit breaker denominator/reset."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import secrets
import uuid

import psycopg
import pytest

from evolvmem.experience_v2.pipeline.scheduler import evaluate_circuit_breaker
from evolvmem.archive_storage import ArchiveStorage
from evolvmem.config import Config
from evolvmem.experience_v2.schema import apply_experience_migrations
from evolvmem.history_pg.candidate_reader import PgHistoryCandidateReader
from evolvmem.history_pg.config import PgHistoryConfig
from evolvmem.history_pg.live_history_sync import sync_session_archive_conn
from evolvmem.transcript_formats import CODEX_FORMAT
from evolvmem.history_pg.schema import apply_history_schemas, ensure_databases_exist
from evolvmem.web_v2.api_pipeline_history_and_control import post_control_action
from tests.stage5.conftest import STAGE5_PG_CONTAINER, STAGE5_PG_PORT, STAGE5_PW_FILE


def _dsn() -> str:
    pw = STAGE5_PW_FILE.read_text(encoding='utf-8').strip()
    return f'host=127.0.0.1 port={STAGE5_PG_PORT} dbname=postgres user=postgres password={pw}'


class _FakeConsole:
    def dispatch(self, method: str, path: str, payload: dict, session: dict):
        if path == '/me':
            return 200, {}, {
                'current_user': {'id': 'owner-1', 'is_admin': True},
                'libraries': [{'id': 'owner-1', 'can_read_experience': True, 'can_read_history': True}],
            }
        raise AssertionError(f'unexpected dispatch {method} {path}')


def test_circuit_breaker_counts_review_proposals_and_reset_action_clears(monkeypatch: pytest.MonkeyPatch) -> None:
    base_dsn = _dsn()
    schema = f's5cb_{uuid.uuid4().hex[:10]}'
    with psycopg.connect(base_dsn, autocommit=True) as admin:
        admin.execute('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public')
        admin.execute(f'CREATE SCHEMA "{schema}"')
    try:
        with psycopg.connect(base_dsn, options=f'-c search_path={schema},public') as conn:
            apply_experience_migrations(conn)
            for idx in range(52):
                uid = f'prop-rev-{idx}'
                conn.execute(
                    """
                    INSERT INTO experience_proposals(
                        uid, owner_id, op, category, kind, problem, solution, reusable_experience,
                        scope, payload_json, evidence_json, source_kind, source_track,
                        agent_client, model, actor_id, request_id, state
                    ) VALUES (%s, 'owner-1', 'add', '系统环境经验', 'pitfall', 'p', 's', 'r',
                              '{}'::jsonb, '{}'::jsonb, '[]'::jsonb, 'pipeline', 'live',
                              'codex', 'm', 'owner-1', %s, 'review')
                    """,
                    (uid, f'req-rev-{idx}'),
                )
            for d_idx in range(3):
                conn.execute(
                    """
                    INSERT INTO llm_calls(track, stage, model, session_id, input_tokens, output_tokens, cost_estimate, outcome, is_shadow, created_at)
                    VALUES ('live', 'extract', 'm', %s, 400000, 126000, 1.5, 'ok', false, now())
                    """,
                    (f'sess-cb-{d_idx}',),
                )
            res = evaluate_circuit_breaker(conn, owner_id='owner-1', track='live')
            assert res['paused'] is False and res['consecutive_bad_days'] == 0

            conn.execute(
                """
                INSERT INTO llm_calls(track, stage, model, session_id, input_tokens, output_tokens, cost_estimate, outcome, is_shadow, created_at)
                VALUES ('live', 'extract', 'm', 'sess-huge', 4000000, 1000000, 5.0, 'ok', false, now() - interval '2 days'),
                       ('live', 'extract', 'm', 'sess-huge2', 4000000, 1000000, 5.0, 'ok', false, now() - interval '1 day'),
                       ('live', 'extract', 'm', 'sess-huge3', 4000000, 1000000, 5.0, 'ok', false, now() - interval '5 minutes')
                """
            )
            tripped = evaluate_circuit_breaker(conn, owner_id='owner-1', track='live')
            assert tripped['paused'] is True
            conn.commit()

        monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_DSN', base_dsn)
        monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_SCHEMA', schema)
        out = post_control_action(_FakeConsole(), {'open_id': 'ou_1'}, {'action': 'reset_circuit', 'track': 'live'})
        assert out['http_status'] == 200 and out['ok'] is True and out['paused'] is False

        with psycopg.connect(base_dsn, options=f'-c search_path={schema},public') as conn:
            after = evaluate_circuit_breaker(conn, owner_id='owner-1', track='live')
            assert after['paused'] is False and after['consecutive_bad_days'] == 0
    finally:
        with psycopg.connect(base_dsn, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _codex_jsonl(session_id: str, cwd: str, lines: list[tuple[str, str, str]], *, subagent: bool = False) -> bytes:
    meta_pl: dict = {'id': session_id, 'cwd': cwd}
    if subagent:
        meta_pl['source'] = {'subagent': {'thread_id': 'parent-1'}}
    recs = [{'timestamp': lines[0][0], 'type': 'session_meta', 'payload': meta_pl}]
    for ts, role, text in lines:
        recs.append({
            'timestamp': ts,
            'type': 'response_item',
            'payload': {'type': 'message', 'role': role, 'content': [{'type': 'input_text' if role == 'user' else 'output_text', 'text': text}]},
        })
    return ('\n'.join(json.dumps(r, ensure_ascii=False) for r in recs) + '\n').encode('utf-8')


def test_live_history_sync_materializes_and_updates_idempotently(tmp_path: Path) -> None:
    pw = STAGE5_PW_FILE.read_text(encoding='utf-8').strip()
    cas_dir = tmp_path / 'cas'
    cas_dir.mkdir(parents=True, exist_ok=True)
    cfg = PgHistoryConfig(
        host='127.0.0.1', port=STAGE5_PG_PORT, admin_user='postgres', admin_password=pw,
        control_coordinator_password=pw, user_history_dbs={'jiangli': 'evolvmem_history_migration'},
        allow_loopback_disable_ssl=True, container_name=STAGE5_PG_CONTAINER, cas_root=cas_dir,
    )
    ensure_databases_exist(cfg, include_control=True)
    apply_history_schemas(cfg, include_control=True)

    sid = f'0199live-{uuid.uuid4().hex[:12]}'
    sub_sid = f'0199sub-{uuid.uuid4().hex[:12]}'
    cwd = r'C:\Users\ASUS\Documents\ChatGPT\领星下载'
    raw_v1 = _codex_jsonl(sid, cwd, [
        ('2026-11-16T10:00:00Z', 'user', '# AGENTS.md instructions\nignored preamble'),
        ('2026-11-16T10:00:01Z', 'user', '为什么今天订单利润报表漏了一列？'),
        ('2026-11-16T10:00:05Z', 'assistant', '排查发现导出字段映射漏了 tax_fee'),
    ])
    raw_v2 = _codex_jsonl(sid, cwd, [
        ('2026-11-16T10:00:00Z', 'user', '# AGENTS.md instructions\nignored preamble'),
        ('2026-11-16T10:00:01Z', 'user', '为什么今天订单利润报表漏了一列？'),
        ('2026-11-16T10:00:05Z', 'assistant', '排查发现导出字段映射漏了 tax_fee'),
        ('2026-11-16T10:02:00Z', 'user', '加上后再帮我重跑校验'),
        ('2026-11-16T10:02:10Z', 'assistant', '已重跑校验通过'),
    ])
    raw_sub = _codex_jsonl(sub_sid, cwd, [('2026-11-16T10:01:00Z', 'user', 'subagent turn')], subagent=True)

    p1 = cas_dir / 's1.bin'
    p2 = cas_dir / 's2.bin'
    psub = cas_dir / 'sub.bin'
    p1.write_bytes(raw_v1)
    p2.write_bytes(raw_v2)
    psub.write_bytes(raw_sub)

    with cfg.connect(cfg.history_migration_db) as conn:
        sub_res = sync_session_archive_conn(
            conn, device_id='win-dev', session_id=sub_sid, sha256=hashlib.sha256(raw_sub).hexdigest(),
            archive_id=910001, payload_path=str(psub), source_format_str=CODEX_FORMAT,
        )
        assert sub_res == {'synced': False, 'reason': 'subagent_session'}

        r1 = sync_session_archive_conn(
            conn, device_id='win-dev', session_id=sid, sha256=hashlib.sha256(raw_v1).hexdigest(),
            archive_id=910002, payload_path=str(p1), source_format_str=CODEX_FORMAT,
        )
        assert r1['synced'] is True and r1['project'] == '领星下载' and r1['messages'] == 2

        r2 = sync_session_archive_conn(
            conn, device_id='win-dev', session_id=sid, sha256=hashlib.sha256(raw_v2).hexdigest(),
            archive_id=910003, payload_path=str(p2), source_format_str=CODEX_FORMAT,
        )
        assert r2['synced'] is True and r2['messages'] == 4
        cnt = conn.execute('SELECT COUNT(*) FROM em_history.blocks WHERE native_session_id = %s', (sid,)).fetchone()[0]
        assert cnt == 1

    with PgHistoryCandidateReader(cfg, dbname=cfg.history_migration_db) as reader:
        projs = reader.projects()['projects']
        ling = next(p for p in projs if p['project'] == '领星下载')
        assert ling['block_count'] >= 1
        ov = reader.project_overview('领星下载')
        blk = next(b for b in ov['blocks'] if b['block_id'] == f'live-{sid}')
        assert blk['title'].startswith('为什么今天订单利润报表漏了一列')
        page = reader.block_page(f'live-{sid}')
        assert len(page['messages']) == 4 and page['messages'][-1]['content'] == '已重跑校验通过'

    data_dir = tmp_path / 'data'
    (data_dir / 'session_archives').mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    (data_dir / 'archive.key').write_bytes(key)
    enc_sid = f'0199enc-{uuid.uuid4().hex[:12]}'
    raw_enc = _codex_jsonl(enc_sid, r'C:\Users\ASUS\Documents\ChatGPT\总控台', [
        ('2026-11-10T08:00:00Z', 'user', '核查总控台历史同步问题'),
        ('2026-11-10T08:00:05Z', 'assistant', '已确认 .bin 归档解包正常'),
    ])
    packed, _manifest = ArchiveStorage(Config(data_dir=data_dir, apply_environment=False), key).pack(
        json.dumps({'transcript': raw_enc.decode('utf-8'), 'conversation': []}, ensure_ascii=False),
    )
    (data_dir / 'session_archives' / 'enc1.bin').write_bytes(packed)
    with cfg.connect(cfg.history_migration_db) as conn:
        enc_res = sync_session_archive_conn(
            conn, device_id='win-dev', session_id=enc_sid, sha256=hashlib.sha256(raw_enc).hexdigest(),
            archive_id=910004, payload_path='session_archives/enc1.bin', source_format_str=CODEX_FORMAT,
            data_dir=data_dir,
        )
        assert enc_res['synced'] is True and enc_res['project'] == '总控台' and enc_res['messages'] == 2
    with PgHistoryCandidateReader(cfg, dbname=cfg.history_migration_db) as reader2:
        projs2 = reader2.projects()['projects']
        assert any(pr['project'] == '总控台' and pr['block_count'] >= 1 for pr in projs2)

"""TDD tests for Backfill Track Task B.3: newest-to-oldest order, single-scope & cursor resume."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from evolvmem.experience_v2.backfill.cursor import (
    BackfillCursorStore,
    BackfillScopeError,
    execute_scoped_backfill,
    plan_scoped_sessions,
    validate_backfill_scope,
)
from evolvmem.experience_v2.backfill.runner import BackfillRunner
from evolvmem.experience_v2.pipeline.history_reader import (
    HistoryRawRecord,
    HistorySession,
    HistorySessionSummary,
    InMemoryHistoryReader,
)
from evolvmem.product_policy import ProductPolicy


def _sess(sid: str, *, project: str, created_at: str) -> HistorySession:
    return HistorySession(
        summary=HistorySessionSummary(
            session_id=sid, adapter='codex', source_format='codex-jsonl',
            project=project, source_ref=sid, library='dev',
            external_session_id=sid, created_at=created_at,
        ),
        records=(HistoryRawRecord(event_index=1, role='user', content='Normal greeting only.'),),
    )


def test_validate_single_scope_rejects_mixed_empty_or_bad_month():
    assert validate_backfill_scope(project='proj-a') == ('project', 'proj-a')
    assert validate_backfill_scope(month='2026-10') == ('month', '2026-10')
    with pytest.raises(BackfillScopeError):
        validate_backfill_scope(project='proj-a', month='2026-10')
    with pytest.raises(BackfillScopeError):
        validate_backfill_scope()
    with pytest.raises(BackfillScopeError):
        validate_backfill_scope(month='2026-13')
    with pytest.raises(BackfillScopeError):
        validate_backfill_scope(project='   ')


def test_scoped_newest_to_oldest_and_cursor_resume(pg_s4_migrated):
    conn, _schema, _dsn = pg_s4_migrated
    sessions = [
        _sess('oct-01', project='alpha', created_at='2026-10-01T08:00:00Z'),
        _sess('oct-05', project='alpha', created_at='2026-10-05T08:00:00Z'),
        _sess('oct-03', project='beta', created_at='2026-10-03T08:00:00Z'),
        _sess('oct-07', project='alpha', created_at='2026-10-07T08:00:00Z'),
        _sess('sep-28', project='alpha', created_at='2026-09-28T08:00:00Z'),
        _sess('oct-02', project='alpha', created_at='2026-10-02T08:00:00Z'),
    ]
    reader = InMemoryHistoryReader(sessions)

    planned_month = plan_scoped_sessions(
        reader, scope_kind='month', scope_value='2026-10', exclude_source_refs={'oct-07'},
    )
    assert [s.source_ref for s in planned_month] == ['oct-05', 'oct-03', 'oct-02', 'oct-01']

    planned_proj = plan_scoped_sessions(reader, scope_kind='project', scope_value='alpha')
    assert [s.source_ref for s in planned_proj] == ['oct-07', 'oct-05', 'oct-02', 'oct-01', 'sep-28']

    seen_order: list[str] = []

    class _TrackingReader(InMemoryHistoryReader):
        def get_session(self, source_ref: str) -> HistorySession | None:
            seen_order.append(source_ref)
            return super().get_session(source_ref)

    t_reader = _TrackingReader(sessions)
    runner = BackfillRunner(
        history_reader=t_reader,
        policy=ProductPolicy(experience_backfill_enabled=True),
        owner_id='owner-cursor',
        flash_model='f', strong_model='s', judge_model='j',
        flash_transport=lambda *a, **kw: {'text': '{}', 'input_tokens': 1, 'output_tokens': 1},
    )
    night = datetime(2026, 11, 14, 3, 0, tzinfo=ZoneInfo('Asia/Shanghai'))

    # Batch 1: process first 2 sessions in project='alpha'
    b1 = execute_scoped_backfill(
        conn, runner=runner, project='alpha', max_sessions=2, now=night,
    )
    assert b1['processed'] == 2
    assert b1['ordered_source_refs'] == ['oct-07', 'oct-05']
    c1 = BackfillCursorStore(owner_id='owner-cursor').get_cursor(conn, scope_kind='project', scope_value='alpha')
    assert c1 is not None and c1['last_source_ref'] == 'oct-05' and c1['processed_count'] == 2 and c1['completed'] is False

    # Batch 2: resume from cursor for remaining sessions (must NOT re-run oct-07 or oct-05)
    b2 = execute_scoped_backfill(
        conn, runner=runner, project='alpha', max_sessions=10, now=night,
    )
    assert b2['processed'] == 3
    assert b2['ordered_source_refs'] == ['oct-02', 'oct-01', 'sep-28']
    c2 = BackfillCursorStore(owner_id='owner-cursor').get_cursor(conn, scope_kind='project', scope_value='alpha')
    assert c2 is not None and c2['last_source_ref'] == 'sep-28' and c2['processed_count'] == 5 and c2['completed'] is True
    assert seen_order == ['oct-07', 'oct-05', 'oct-02', 'oct-01', 'sep-28']

    # Batch 3: cursor completed -> 0 sessions processed
    b3 = execute_scoped_backfill(conn, runner=runner, project='alpha', max_sessions=10, now=night)
    assert b3['processed'] == 0 and b3['status'] == 'cursor_completed'


def test_mixed_timestamp_formats_chronological_and_pagination(pg_s4_migrated, tmp_path):
    import sqlite3
    from evolvmem.experience_v2.backfill.cursor import plan_scoped_sessions
    from evolvmem.experience_v2.backfill.queue import BackfillQueue
    from evolvmem.experience_v2.pipeline.history_reader import SqliteHistoryReader

    conn, _schema, _dsn = pg_s4_migrated
    mixed = [
        _sess('archive:1', project='mix', created_at='2026-10-30 23:59:59'),
        _sess('block:b2', project='mix', created_at='2026-10-30T12:00:00Z'),
        _sess('archive:2', project='mix', created_at='2026-10-30 06:00:00'),
        _sess('block:b1', project='mix', created_at='2026-10-30T01:00:00Z'),
        _sess('archive:3', project='mix', created_at='2026-10-29 20:00:00'),
    ]
    inmem = InMemoryHistoryReader(mixed)
    assert [s.source_ref for s in inmem.list_sessions(limit=10)] == [
        'archive:1', 'block:b2', 'archive:2', 'block:b1', 'archive:3',
    ]

    # Paginate across page boundaries
    planned = plan_scoped_sessions(inmem, scope_kind='project', scope_value='mix', page_size=2)
    assert [s.source_ref for s in planned] == [
        'archive:1', 'block:b2', 'archive:2', 'block:b1', 'archive:3',
    ]

    # Verify BackfillQueue claim order uses real UTC chronology, not ASCII 'T' > ' '
    q = BackfillQueue(owner_id='owner-mix')
    q.enqueue_sessions(conn, scope_kind='project', scope_value='mix', sessions=planned)
    claimed = q.claim_next_jobs(conn, scope_kind='project', scope_value='mix', limit=10)
    assert [c['source_ref'] for c in claimed] == [
        'archive:1', 'block:b2', 'archive:2', 'block:b1', 'archive:3',
    ]

    # Verify SqliteHistoryReader interleaves archive + block chronologically across offset
    mem_db = tmp_path / 'memory.db'
    cand_db = tmp_path / 'candidate.db'
    mc = sqlite3.connect(mem_db)
    mc.executescript(
        """
        CREATE TABLE session_archives (
            id INTEGER PRIMARY KEY, adapter TEXT, external_session_id TEXT,
            project TEXT, state TEXT, payload_path TEXT, created_at TEXT
        );
        CREATE TABLE conversation_history (id INTEGER PRIMARY KEY, archive_id INTEGER, messages TEXT);
        INSERT INTO session_archives VALUES (10, 'codex', 'e1', 'mix', 'available', '', '2026-10-30 23:59:59');
        INSERT INTO session_archives VALUES (20, 'codex', 'e2', 'mix', 'available', '', '2026-10-30 06:00:00');
        INSERT INTO conversation_history VALUES (1, 10, '[{"role":"user","content":"hi"}]');
        INSERT INTO conversation_history VALUES (2, 20, '[{"role":"user","content":"hi"}]');
        """
    )
    mc.commit()
    mc.close()

    cc = sqlite3.connect(cand_db)
    cc.executescript(
        """
        CREATE TABLE blocks (
            block_id TEXT PRIMARY KEY, adapter TEXT, native_session_id TEXT,
            observed_first_at TEXT, projects TEXT, message_count INTEGER
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY, block_id TEXT, ordinal INTEGER,
            role TEXT, content TEXT, recorded_at TEXT, archive_id INTEGER, message_index INTEGER
        );
        INSERT INTO blocks VALUES ('b2', 'dsh', 'n2', '2026-10-30T12:00:00Z', '["mix"]', 1);
        INSERT INTO blocks VALUES ('b1', 'dsh', 'n1', '2026-10-30T01:00:00Z', '["mix"]', 1);
        INSERT INTO messages VALUES (1, 'b2', 1, 'user', 'hi', '2026-10-30T12:00:00Z', NULL, 1);
        INSERT INTO messages VALUES (2, 'b1', 1, 'user', 'hi', '2026-10-30T01:00:00Z', NULL, 1);
        """
    )
    cc.commit()
    cc.close()

    sr = SqliteHistoryReader(memory_db=mem_db, candidate_db=cand_db)
    all_sql = sr.list_sessions(limit=10, offset=0)
    assert [s.source_ref for s in all_sql] == ['archive:10', 'block:b2', 'archive:20', 'block:b1']
    page2 = sr.list_sessions(limit=2, offset=2)
    assert [s.source_ref for s in page2] == ['archive:20', 'block:b1']


def test_execute_scoped_backfill_stops_cursor_on_failed_or_not_found(pg_s4_migrated):
    conn, _schema, _dsn = pg_s4_migrated
    sessions = [
        _sess('f-3', project='failproj', created_at='2026-10-03T10:00:00Z'),
        _sess('f-2', project='failproj', created_at='2026-10-02 09:00:00'),
        _sess('f-1', project='failproj', created_at='2026-10-01T08:00:00Z'),
    ]
    reader = InMemoryHistoryReader(sessions)
    runner = BackfillRunner(
        history_reader=reader,
        policy=ProductPolicy(experience_backfill_enabled=True),
        owner_id='owner-fail-cur',
        flash_model='f', strong_model='s', judge_model='j',
        flash_transport=lambda *a, **kw: {'text': '{}', 'input_tokens': 1, 'output_tokens': 1},
    )
    orig = runner.scheduler.process_session_if_ready
    fail_f2 = {'flag': True}
    def _maybe_fail(c, source_ref, **kw):
        if source_ref == 'f-2' and fail_f2['flag']:
            return {'status': 'not_found', 'llm_calls': 0}
        return orig(c, source_ref, **kw)
    runner.scheduler.process_session_if_ready = _maybe_fail
    night = datetime(2026, 11, 14, 3, 0, tzinfo=ZoneInfo('Asia/Shanghai'))

    res1 = execute_scoped_backfill(conn, runner=runner, project='failproj', max_sessions=3, now=night)
    cur1 = BackfillCursorStore(owner_id='owner-fail-cur').get_cursor(conn, scope_kind='project', scope_value='failproj')
    assert cur1 is not None
    assert cur1['last_source_ref'] == 'f-3'
    assert cur1['completed'] is False
    assert res1['cursor']['processed_count'] == 1

    fail_f2['flag'] = False
    res2 = execute_scoped_backfill(conn, runner=runner, project='failproj', max_sessions=3, now=night)
    cur2 = BackfillCursorStore(owner_id='owner-fail-cur').get_cursor(conn, scope_kind='project', scope_value='failproj')
    assert cur2 is not None
    assert cur2['last_source_ref'] == 'f-1'
    assert cur2['completed'] is True
    assert res2['ordered_source_refs'] == ['f-2']
    assert cur2['processed_count'] == 3

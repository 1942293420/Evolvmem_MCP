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

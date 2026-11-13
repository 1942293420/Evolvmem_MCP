"""Stage 2 Task 2.9 tests: Per-model rollback with mandatory signed dry-run."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from evolvmem.experience_v2.schema import apply_experience_migrations
from evolvmem.experience_v2.writer import (
    ExperienceWriter,
    Op,
    TicketIssuer,
    scan_unauthorized_experience_notes_writes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _issue_gated_ticket(conn, secret: bytes, req_tag: str, *, owner_id: str = 'jiangli', agent_client: str = 'codex', model: str = 'gpt-5'):
    puid = f'prop-{req_tag}'
    did = f'dec-{req_tag}'
    conn.execute(
        """
        INSERT INTO experience_proposals (
            uid, owner_id, op, category, kind, problem, solution, reusable_experience,
            misbelief, scope, payload_json, evidence_json, source_kind, source_track,
            agent_client, model, device_id, actor_id, session_id, request_id, state
        ) VALUES (
            %s, %s, 'add', '系统环境经验', 'pitfall', 'p', 's', 'r',
            '', '{}'::jsonb, '{}'::jsonb, '[]'::jsonb, 'agent', 'live',
            %s, %s, 'dev', %s, 'sess', %s, 'accepted'
        )
        """,
        (puid, owner_id, agent_client, model, owner_id, f'seed-req-{req_tag}'),
    )
    conn.execute(
        """
        INSERT INTO proposal_decisions (decision_id, proposal_uid, layer, verdict, reason_code, confidence)
        VALUES (%s, %s, 'L4', 'pass', NULL, 1.0)
        """,
        (did, puid),
    )
    return TicketIssuer(secret, gate_id='test_seed').issue(
        proposal_uid=puid,
        decision_id=did,
        lane='gated',
    )


def _dump_pg_state(conn) -> dict:
    notes = conn.execute(
        """
        SELECT id, owner_id, category, problem, solution, reusable_experience,
               evidence, occurrence_count, status, deleted_at, revision
        FROM experience_notes ORDER BY id ASC
        """
    ).fetchall()
    events = conn.execute(
        """
        SELECT seq, event_id, action, note_id, agent_client, model, rolled_back_by_seq
        FROM experience_events ORDER BY seq ASC
        """
    ).fetchall()
    reqs = conn.execute(
        'SELECT owner_id, request_id, payload_sha256, http_status FROM experience_requests ORDER BY request_id ASC'
    ).fetchall()
    return {'notes': notes, 'events': events, 'reqs': reqs}


def test_model_rollback_requires_dry_run_preserves_state_and_reverses_only_target_model(pg_exp_schema):
    from evolvmem.experience_v2.gate.rollback import (
        ModelRollbackService,
        RollbackPlanRequiredError,
    )

    conn, _schema, _dsn = pg_exp_schema
    apply_experience_migrations(conn)
    secret = b'stage2-task29-rollback-secret'
    writer = ExperienceWriter(secret)

    # 1. Control model creates N2 (existing valid note) and N4 (independent note)
    n2_res = writer.apply(
        conn,
        _issue_gated_ticket(conn, secret, 'n2', agent_client='claude-code', model='claude-sonnet-4-5'),
        Op(
            action='create',
            owner_id='jiangli',
            actor_id='jiangli',
            request_id='seed-n2',
            category='系统环境经验',
            problem='Valid note owned by healthy model',
            solution='Healthy solution step',
            reusable_experience='Keep healthy model records intact',
            origin='agent',
            evidence=[{'ref': 'sess:healthy', 'quote': 'ok'}],
            agent_client='claude-code',
            model='claude-sonnet-4-5',
        ),
    )
    n2_id = n2_res['experience']['id']

    n4_res = writer.apply(
        conn,
        _issue_gated_ticket(conn, secret, 'n4', agent_client='claude-code', model='claude-sonnet-4-5'),
        Op(
            action='create',
            owner_id='jiangli',
            actor_id='jiangli',
            request_id='seed-n4',
            category='业务经验',
            problem='Independent healthy model insight',
            solution='Unaffected workflow',
            reusable_experience='Control note must never roll back',
            origin='agent',
            evidence=[{'ref': 'sess:ctrl', 'quote': 'ok'}],
            agent_client='claude-code',
            model='claude-sonnet-4-5',
        ),
    )
    n4_id = n4_res['experience']['id']

    # N3 initially created by healthy model, then erroneously updated by bad-model-v1
    n3_res = writer.apply(
        conn,
        _issue_gated_ticket(conn, secret, 'n3', agent_client='claude-code', model='claude-sonnet-4-5'),
        Op(
            action='create',
            owner_id='jiangli',
            actor_id='jiangli',
            request_id='seed-n3',
            category='前端经验',
            problem='Original N3 problem',
            solution='Original N3 solution',
            reusable_experience='Original N3 reusable rule',
            origin='agent',
            evidence=[{'ref': 'sess:n3', 'quote': 'original'}],
            agent_client='claude-code',
            model='claude-sonnet-4-5',
        ),
    )
    n3_id = n3_res['experience']['id']

    # 2. Target model ('codex', 'bad-model-v1') performs create(N1), merge_evidence(N2), update(N3)
    n1_res = writer.apply(
        conn,
        _issue_gated_ticket(conn, secret, 'n1', agent_client='codex', model='bad-model-v1'),
        Op(
            action='create',
            owner_id='jiangli',
            actor_id='jiangli',
            request_id='bad-create-n1',
            category='系统环境经验',
            problem='Hallucinated create by bad-model-v1',
            solution='Bogus command',
            reusable_experience='Should be retired by rollback',
            origin='agent',
            evidence=[{'ref': 'sess:bad1', 'quote': 'bad'}],
            agent_client='codex',
            model='bad-model-v1',
        ),
    )
    n1_id = n1_res['experience']['id']

    writer.apply(
        conn,
        _issue_gated_ticket(conn, secret, 'n2-merge', agent_client='codex', model='bad-model-v1'),
        Op(
            action='merge_evidence',
            owner_id='jiangli',
            actor_id='jiangli',
            request_id='bad-merge-n2',
            note_id=n2_id,
            evidence=[{'ref': 'sess:bad-merge', 'quote': 'noisy'}],
            agent_client='codex',
            model='bad-model-v1',
        ),
    )

    writer.apply(
        conn,
        _issue_gated_ticket(conn, secret, 'n3-upd', agent_client='codex', model='bad-model-v1'),
        Op(
            action='update',
            owner_id='jiangli',
            actor_id='jiangli',
            request_id='bad-update-n3',
            note_id=n3_id,
            category='前端经验',
            problem='Corrupted N3 problem',
            solution='Corrupted N3 solution',
            reusable_experience='Corrupted N3 reusable rule',
            agent_client='codex',
            model='bad-model-v1',
        ),
    )
    conn.commit()

    svc = ModelRollbackService(ticket_secret=secret)
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=1)
    until = now + timedelta(hours=1)

    # Cannot execute without dry-run RollbackPlan
    with pytest.raises(RollbackPlanRequiredError) as no_plan:
        svc.execute_model_rollback(conn, plan=None, actor_id='jiangli', request_id='rb-noplan')  # type: ignore[arg-type]
    assert no_plan.value.code == 'dry_run_required'

    # Dry-run plan must leave all tables bit-for-bit identical
    before_dry = _dump_pg_state(conn)
    plan = svc.plan_model_rollback(
        conn,
        owner_id='jiangli',
        agent_client='codex',
        model='bad-model-v1',
        since=since,
        until=until,
    )
    after_dry = _dump_pg_state(conn)
    assert after_dry == before_dry
    assert len(plan.operations) == 3

    # Tampered plan signature must fail
    tampered = replace(plan, signature='0' * 64)
    with pytest.raises(RollbackPlanRequiredError) as bad_sig:
        svc.execute_model_rollback(conn, plan=tampered, actor_id='jiangli', request_id='rb-tampered')
    assert bad_sig.value.code == 'invalid_plan_signature'

    # Execute approved dry-run plan
    res = svc.execute_model_rollback(conn, plan=plan, actor_id='jiangli', request_id='rb-exec-1')
    assert res['ok'] is True
    assert res['reverted_events'] == 3

    # N1 (created by bad model) -> retired & soft-deleted
    n1_row = conn.execute('SELECT status, deleted_at FROM experience_notes WHERE id = %s', (n1_id,)).fetchone()
    assert n1_row[0] == 'retired' and n1_row[1] is not None

    # N2 (merge_evidence by bad model) -> restored to occurrence_count=1 & original evidence
    n2_row = conn.execute('SELECT occurrence_count, evidence FROM experience_notes WHERE id = %s', (n2_id,)).fetchone()
    assert n2_row[0] == 1 and n2_row[1] == [{'ref': 'sess:healthy', 'quote': 'ok'}]

    # N3 (update by bad model) -> restored to original text
    n3_row = conn.execute('SELECT problem, solution, reusable_experience FROM experience_notes WHERE id = %s', (n3_id,)).fetchone()
    assert n3_row == ('Original N3 problem', 'Original N3 solution', 'Original N3 reusable rule')

    # N4 (healthy model) -> completely untouched (revision still 1, active)
    n4_row = conn.execute('SELECT status, revision, deleted_at FROM experience_notes WHERE id = %s', (n4_id,)).fetchone()
    assert n4_row == ('active', 1, None)

    # Double-rollback prevention: rolled_back_by_seq set -> second dry-run returns 0 ops
    plan_again = svc.plan_model_rollback(
        conn,
        owner_id='jiangli',
        agent_client='codex',
        model='bad-model-v1',
        since=since,
        until=until,
    )
    assert len(plan_again.operations) == 0


def test_task29_ast_and_file_limits():
    assert scan_unauthorized_experience_notes_writes(REPO_ROOT) == []
    for rel in (
        'evolvmem/experience_v2/writer.py',
        'evolvmem/experience_v2/gate/rollback.py',
    ):
        p = REPO_ROOT / rel
        if p.exists():
            assert len(p.read_text(encoding='utf-8').splitlines()) <= 600
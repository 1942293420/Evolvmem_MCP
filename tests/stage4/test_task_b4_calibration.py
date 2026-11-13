"""TDD tests for Backfill Track Task B.4: separate track calibration & precedent isolation."""
from __future__ import annotations

import json
import uuid

from evolvmem.experience_v2.backfill.calibration import (
    compute_track_calibration,
    get_track_precedents,
)


def _seed_review(
    conn,
    *,
    owner_id: str,
    source_track: str,
    category: str,
    gate_verdict: str,
    user_verdict: str,
    problem: str,
) -> None:
    uid = f'prop-{uuid.uuid4().hex[:14]}'
    rev_id = f'rev-{uuid.uuid4().hex[:14]}'
    conn.execute(
        """
        INSERT INTO experience_proposals(
            uid, owner_id, op, category, kind, problem, solution, reusable_experience,
            misbelief, scope, payload_json, evidence_json, source_kind, source_track,
            agent_client, model, actor_id, request_id, state
        ) VALUES (
            %s, %s, 'add', %s, 'pitfall', %s, '标准做法', '可复用经验',
            '', '{}'::jsonb, '{}'::jsonb, '[{"library":"dev","source":"archive:1","event":1}]'::jsonb,
            'pipeline', %s, 'codex', 'deepseek-v4-flash', %s, %s, 'review'
        )
        """,
        (uid, owner_id, category, problem, source_track, owner_id, f'req-{uid}'),
    )
    conn.execute(
        """
        INSERT INTO review_decisions(
            review_id, owner_id, reviewer_id, proposal_uid, category, source_track,
            gate_verdict, gate_confidence, gate_reason, user_verdict, user_reason
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, 0.9, NULL, %s, %s)
        """,
        (
            rev_id, owner_id, owner_id, uid, category, source_track,
            gate_verdict, user_verdict, ('task_log' if user_verdict == 'reject' else None),
        ),
    )


def test_backfill_and_live_calibrations_and_precedents_never_cross(pg_s4_migrated):
    conn, _schema, _dsn = pg_s4_migrated

    # Live track: 55 agreed reviews -> auto
    for i in range(55):
        _seed_review(
            conn, owner_id='owner-cal', source_track='live', category='业务经验',
            gate_verdict='pass', user_verdict='accept', problem=f'LIVE_ONLY_RULE_{i}',
        )

    # Backfill track: only 6 reviews (3 agreed, 3 disagreed = 50%) -> review
    for i in range(3):
        _seed_review(
            conn, owner_id='owner-cal', source_track='backfill', category='业务经验',
            gate_verdict='pass', user_verdict='accept', problem=f'BACKFILL_GOOD_{i}',
        )
        _seed_review(
            conn, owner_id='owner-cal', source_track='backfill', category='业务经验',
            gate_verdict='pass', user_verdict='reject', problem=f'BACKFILL_BAD_{i}',
        )

    live_cal = compute_track_calibration(conn, owner_id='owner-cal', source_track='live', category='业务经验')
    bf_cal = compute_track_calibration(conn, owner_id='owner-cal', source_track='backfill', category='业务经验')

    assert live_cal['reviewed_total'] == 55
    assert live_cal['agreement_rate'] == 1.0
    assert live_cal['recommended_mode'] == 'auto'

    assert bf_cal['reviewed_total'] == 6
    assert bf_cal['agreement_rate'] == 0.5
    assert bf_cal['recommended_mode'] == 'review'

    live_precs = get_track_precedents(conn, owner_id='owner-cal', source_track='live', category='业务经验')
    bf_precs = get_track_precedents(conn, owner_id='owner-cal', source_track='backfill', category='业务经验')
    assert all(p['problem'].startswith('LIVE_ONLY_RULE_') for p in live_precs)
    assert all(p['problem'].startswith('BACKFILL_') for p in bf_precs)

    snap_tracks = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT source_track, recommended_mode FROM track_calibration_snapshots WHERE owner_id = 'owner-cal'"
        ).fetchall()
    }
    assert snap_tracks == {'live': 'auto', 'backfill': 'review'}

"""Task B.4: Track-isolated calibration (precedents, agreement rate & auto-switch readiness)."""
from __future__ import annotations

import json
import os
from typing import Any, Literal

from evolvmem.experience_v2.gate.review_inbox import ADMITTING_VERDICTS, VALID_TRACKS
from evolvmem.experience_v2.pipeline.pilot import scrub_pilot_text
from evolvmem.experience_v2.writer import CATEGORIES


def _is_gate_user_agreed(gate_verdict: str | None, user_verdict: str) -> bool | None:
    if gate_verdict is None:
        return None
    if gate_verdict in ('pass', 'review', 'merge'):
        return user_verdict in ('accept', 'edit_accept')
    if gate_verdict == 'reject':
        return user_verdict == 'reject'
    return None


def get_track_precedents(
    conn,
    *,
    owner_id: str,
    source_track: Literal['live', 'backfill'],
    category: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return strictly track- and category-isolated scrubbed review precedents."""
    if source_track not in VALID_TRACKS or category not in CATEGORIES:
        raise ValueError('invalid_track_or_category')
    safe_limit = max(1, min(int(limit), 100))
    rows = conn.execute(
        """
        SELECT r.review_id, r.gate_verdict, r.user_verdict, r.user_reason,
               p.kind, p.problem, p.solution, p.reusable_experience, r.created_at
        FROM review_decisions AS r
        JOIN experience_proposals AS p ON p.uid = r.proposal_uid
        WHERE r.owner_id = %s
          AND r.source_track = %s
          AND r.category = %s
        ORDER BY r.created_at DESC, r.review_id DESC
        LIMIT %s
        """,
        (owner_id, source_track, category, safe_limit),
    ).fetchall()
    extra = [os.environ.get('USER', '')]
    return [
        {
            'review_id': row[0],
            'gate_verdict': row[1],
            'user_verdict': row[2],
            'user_reason': row[3],
            'admitted': row[2] in ADMITTING_VERDICTS,
            'kind': row[4],
            'problem': scrub_pilot_text(row[5], extra_terms=extra),
            'solution': scrub_pilot_text(row[6], extra_terms=extra),
            'reusable_experience': scrub_pilot_text(row[7], extra_terms=extra),
        }
        for row in rows
    ]


def compute_track_calibration(
    conn,
    *,
    owner_id: str,
    source_track: Literal['live', 'backfill'],
    category: str,
    precedent_limit: int = 10,
    persist_snapshot: bool = True,
) -> dict[str, Any]:
    """Compute track-isolated agreement rate, rolling-20 rate, and recommended gate mode."""
    if source_track not in VALID_TRACKS or category not in CATEGORIES:
        raise ValueError('invalid_track_or_category')

    rows = conn.execute(
        """
        SELECT r.gate_verdict, r.user_verdict
        FROM review_decisions AS r
        WHERE r.owner_id = %s
          AND r.source_track = %s
          AND r.category = %s
        ORDER BY r.created_at DESC, r.review_id DESC
        """,
        (owner_id, source_track, category),
    ).fetchall()

    reviewed_total = len(rows)
    evaluable: list[bool] = []
    for gv, uv in rows:
        agreed = _is_gate_user_agreed(gv, uv)
        if agreed is not None:
            evaluable.append(agreed)

    evaluable_total = len(evaluable)
    agreed_total = sum(1 for x in evaluable if x)
    agreement_rate = round(agreed_total / evaluable_total, 4) if evaluable_total else 0.0
    rolling20 = evaluable[:20]
    rolling20_rate = round(sum(1 for x in rolling20 if x) / len(rolling20), 4) if rolling20 else 0.0

    if reviewed_total >= 50 and agreement_rate >= 0.90 and rolling20_rate >= 0.85:
        recommended_mode: Literal['review', 'partial', 'auto'] = 'auto'
    elif reviewed_total >= 20 and agreement_rate >= 0.80 and rolling20_rate >= 0.75:
        recommended_mode = 'partial'
    else:
        recommended_mode = 'review'

    precedents = get_track_precedents(
        conn, owner_id=owner_id, source_track=source_track, category=category, limit=precedent_limit,
    )

    if persist_snapshot:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO track_calibration_snapshots(
                    owner_id, source_track, category, reviewed_total, agreed_total,
                    agreement_rate, precedents_json, recommended_mode
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                """,
                (
                    owner_id, source_track, category, reviewed_total, agreed_total,
                    agreement_rate, json.dumps(precedents, ensure_ascii=False), recommended_mode,
                ),
            )

    return {
        'owner_id': owner_id,
        'source_track': source_track,
        'category': category,
        'reviewed_total': reviewed_total,
        'evaluable_total': evaluable_total,
        'agreed_total': agreed_total,
        'agreement_rate': agreement_rate,
        'rolling20_rate': rolling20_rate,
        'recommended_mode': recommended_mode,
        'precedents': precedents,
    }

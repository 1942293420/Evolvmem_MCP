"""Ticketed rescue workflows for intercepted/rejected experience items (Migration 005)."""
from __future__ import annotations

import json
from typing import Any

from evolvmem.experience_v2.control.agent_identity import AgentAttribution
from evolvmem.experience_v2.control.reputation_policy import TrustedChannel
from evolvmem.experience_v2.gate.propose import ProposalService
from evolvmem.experience_v2.gate.review_inbox import ReviewInboxError, ReviewInboxService
from evolvmem.experience_v2.writer import CATEGORIES


def _jval(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return default
    return raw


def rescue_intercepted_item(
    conn,
    *,
    owner_id: str,
    reviewer_id: str,
    item_id: str,
    edited_payload: dict[str, Any] | None,
    note: str,
    ticket_secret: bytes | str,
) -> dict[str, Any]:
    """Admit an intercepted item strictly via ProposalService + ReviewInboxService + ExperienceWriter."""
    if not item_id:
        raise ReviewInboxError('item_id_required', 400)
    if not item_id.startswith(('int-', 'prop-')):
        raise ReviewInboxError('interception_not_rescuable', 400)

    svc = ReviewInboxService(ticket_secret=ticket_secret, shadow_mode=False)
    edits = dict(edited_payload) if isinstance(edited_payload, dict) else None

    if item_id.startswith('int-'):
        row = conn.execute(
            """
            SELECT track, session_id, agent_client, model, candidate_json, evidence_json, rescued_note_id
            FROM pipeline_interceptions WHERE interception_id = %s AND owner_id = %s FOR UPDATE
            """,
            (item_id, owner_id),
        ).fetchone()
        if not row:
            raise ReviewInboxError('interception_not_found', 404)
        if row[6]:
            raise ReviewInboxError('interception_already_rescued', 409)
        cand = _jval(row[4], {})
        ev = _jval(row[5], [])
        valid_kinds = ('env_fact', 'pitfall', 'procedure', 'preference', 'decision', 'habit')
        merged_body = {
            'op': 'add',
            'request_id': f'rescue-from:{item_id[:48]}',
            'category': (edits or {}).get('category') or cand.get('category') or '系统环境经验',
            'kind': cand.get('kind') if cand.get('kind') in valid_kinds else 'pitfall',
            'problem': str((edits or {}).get('problem') or cand.get('problem') or '').strip(),
            'solution': str((edits or {}).get('solution') or cand.get('solution') or '').strip(),
            'reusable_experience': str(
                (edits or {}).get('reusable_experience') or cand.get('reusable_experience') or ''
            ).strip(),
            'misbelief': str((edits or {}).get('misbelief') or cand.get('misbelief') or '').strip(),
            'scope': cand.get('scope') if isinstance(cand.get('scope'), dict) else {},
            'evidence': ev if isinstance(ev, list) else [],
            'source_track': row[0] if row[0] in ('live', 'backfill') else 'live',
            'session_id': row[1] or None,
        }
        if merged_body['category'] not in CATEGORIES:
            raise ReviewInboxError('invalid_category', 400)
        if not (merged_body['problem'] and merged_body['solution'] and merged_body['reusable_experience']):
            raise ReviewInboxError('empty_experience_fields', 400)
        attr = AgentAttribution(
            actor=reviewer_id, agent_id='web-console-rescue',
            client='web-console', model=row[3] or 'human-rescue',
        )
        prop = ProposalService().propose(
            conn, owner_id=owner_id, actor_id=reviewer_id, channel=TrustedChannel.WEB_HUMAN,
            attribution=attr, body=merged_body, source_kind='user',
        )
        res = svc.decide(
            conn, owner_id=owner_id, reviewer_id=reviewer_id, proposal_uid=prop['uid'],
            user_verdict='rescue', note=note or f'从前置拦截 {item_id} 人工救回', edited_payload=edits,
        )
        note_id = (res.get('note') or {}).get('id')
        conn.execute(
            """
            UPDATE pipeline_interceptions
            SET rescued_proposal_uid = %s, rescued_note_id = %s, rescued_by = %s, rescued_at = now()
            WHERE interception_id = %s AND owner_id = %s
            """,
            (prop['uid'], note_id, reviewer_id, item_id, owner_id),
        )
        conn.commit()
        return {**res, 'rescued_from': item_id}

    existing_rev = conn.execute(
        'SELECT review_id FROM review_decisions WHERE proposal_uid = %s AND owner_id = %s LIMIT 1',
        (item_id, owner_id),
    ).fetchone()
    if existing_rev is None:
        st_row = conn.execute(
            'SELECT state FROM experience_proposals WHERE uid = %s AND owner_id = %s',
            (item_id, owner_id),
        ).fetchone()
        if not st_row:
            raise ReviewInboxError('proposal_not_found', 404)
        if st_row[0] == 'accepted':
            raise ReviewInboxError('proposal_already_accepted', 409)
        return {
            **svc.decide(
                conn, owner_id=owner_id, reviewer_id=reviewer_id, proposal_uid=item_id,
                user_verdict='rescue', note=note or '从门控拦截人工救回入库', edited_payload=edits,
            ),
            'rescued_from': item_id,
        }

    p_row = conn.execute(
        """
        SELECT category, kind, problem, solution, reusable_experience, misbelief,
               scope, evidence_json, source_track, model, session_id, importance, state
        FROM experience_proposals WHERE uid = %s AND owner_id = %s FOR UPDATE
        """,
        (item_id, owner_id),
    ).fetchone()
    if not p_row:
        raise ReviewInboxError('proposal_not_found', 404)
    if p_row[12] == 'accepted':
        raise ReviewInboxError('proposal_already_accepted', 409)
    attr = AgentAttribution(
        actor=reviewer_id, agent_id='web-console-rescue',
        client='web-console', model=p_row[9] or 'human-rescue',
    )
    prop = ProposalService().propose(
        conn, owner_id=owner_id, actor_id=reviewer_id, channel=TrustedChannel.WEB_HUMAN,
        attribution=attr,
        body={
            'op': 'add',
            'request_id': f'rescue-from:{item_id[:48]}',
            'category': (edits or {}).get('category') or p_row[0],
            'kind': p_row[1],
            'problem': str((edits or {}).get('problem') or p_row[2]).strip(),
            'solution': str((edits or {}).get('solution') or p_row[3]).strip(),
            'reusable_experience': str((edits or {}).get('reusable_experience') or p_row[4]).strip(),
            'misbelief': str((edits or {}).get('misbelief') or p_row[5]).strip(),
            'scope': _jval(p_row[6], {}),
            'evidence': _jval(p_row[7], []),
            'source_track': p_row[8],
            'session_id': p_row[10],
            'importance': int((edits or {}).get('importance') or p_row[11]),
        },
        source_kind='user',
    )
    res = svc.decide(
        conn, owner_id=owner_id, reviewer_id=reviewer_id, proposal_uid=prop['uid'],
        user_verdict='rescue', note=note or f'从 L4 驳回 {item_id} 人工救回', edited_payload=edits,
    )
    return {**res, 'rescued_from': item_id}

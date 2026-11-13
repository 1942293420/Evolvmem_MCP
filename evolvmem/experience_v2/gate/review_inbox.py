"""Stage 2 Task 2.8: Web Review Inbox, live/backfill track isolation & shadow mode."""
from __future__ import annotations

from datetime import datetime
import json
from typing import Any
import uuid

from evolvmem.experience_v2.gate.rules import REASON_CODES
from evolvmem.experience_v2.writer import (
    CATEGORIES,
    ExperienceWriteError,
    ExperienceWriter,
    Op,
    TicketIssuer,
)

VALID_TRACKS = ('live', 'backfill')
VALID_USER_VERDICTS = ('accept', 'edit_accept', 'reject', 'rescue')
ADMITTING_VERDICTS = ('accept', 'edit_accept', 'rescue')
_SOURCE_KIND_TO_ORIGIN = {
    'user': 'manual',
    'agent': 'agent',
    'pipeline': 'pipeline',
    'legacy': 'legacy',
}


class ReviewInboxError(ValueError):
    """Domain validation or authorization error for Review Inbox operations."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def _iso(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        return val.isoformat(timespec='seconds')
    return str(val)


def _build_evidence_preview(evidence_json: Any) -> list[dict[str, str]]:
    items = evidence_json if isinstance(evidence_json, list) else []
    preview: list[dict[str, str]] = []
    for entry in items[:8]:
        if not isinstance(entry, dict):
            continue
        preview.append({
            'kind': str(entry.get('kind') or 'session'),
            'ref': str(entry.get('ref') or entry.get('session_id') or ''),
            'quote': str(entry.get('quote') or entry.get('snippet') or '')[:400],
        })
    return preview


def _latest_gate_decision(conn, proposal_uid: str) -> tuple[str | None, float | None, str | None]:
    row = conn.execute(
        """
        SELECT verdict, confidence, reason_code
        FROM proposal_decisions
        WHERE proposal_uid = %s AND layer <> 'L4'
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (proposal_uid,),
    ).fetchone()
    if not row:
        return None, None, None
    return row[0], (float(row[1]) if row[1] is not None else None), row[2]


class ReviewInboxService:
    """Owner-isolated review queue separated by source_track with optional shadow masking."""

    def __init__(self, *, ticket_secret: bytes | str, shadow_mode: bool = True):
        self._secret = ticket_secret
        self._issuer = TicketIssuer(ticket_secret, gate_id='L4_review')
        self._writer = ExperienceWriter(ticket_secret)
        self.shadow_mode = bool(shadow_mode)

    def _counts_by_track(self, conn, owner_id: str) -> dict[str, int]:
        counts = {'live': 0, 'backfill': 0}
        rows = conn.execute(
            """
            SELECT p.source_track, COUNT(*)
            FROM experience_proposals AS p
            WHERE p.owner_id = %s
              AND p.state IN ('pending', 'review', 'rejected', 'quarantined')
              AND NOT EXISTS (
                  SELECT 1 FROM review_decisions AS r
                  WHERE r.proposal_uid = p.uid AND r.owner_id = p.owner_id
              )
            GROUP BY p.source_track
            """,
            (owner_id,),
        ).fetchall()
        for track, total in rows:
            if track in counts:
                counts[track] = int(total)
        return counts

    def list_inbox(
        self,
        conn,
        *,
        owner_id: str,
        source_track: str = 'live',
        include_decided: bool = False,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not owner_id:
            raise ReviewInboxError('owner_required', 400)
        if source_track not in VALID_TRACKS:
            raise ReviewInboxError('invalid_source_track', 400)
        safe_limit = max(1, min(int(limit), 200))
        decided_clause = (
            ''
            if include_decided
            else (
                "AND p.state IN ('pending', 'review', 'rejected', 'quarantined') "
                "AND NOT EXISTS (SELECT 1 FROM review_decisions AS r "
                "WHERE r.proposal_uid = p.uid AND r.owner_id = p.owner_id)"
            )
        )
        rows = conn.execute(
            f"""
            SELECT p.uid, p.op, p.target_uid, p.category, p.kind,
                   p.problem, p.solution, p.reusable_experience, p.misbelief,
                   p.scope, p.evidence_json, p.source_kind, p.source_track,
                   p.agent_client, p.model, p.state, p.importance, p.created_at
            FROM experience_proposals AS p
            WHERE p.owner_id = %s
              AND p.source_track = %s
              {decided_clause}
            ORDER BY p.created_at DESC, p.uid ASC
            LIMIT %s
            """,
            (owner_id, source_track, safe_limit),
        ).fetchall()

        items: list[dict[str, Any]] = []
        for row in rows:
            uid = row[0]
            gate_verdict, gate_conf, gate_reason = (
                (None, None, None) if self.shadow_mode else _latest_gate_decision(conn, uid)
            )
            items.append({
                'uid': uid,
                'op': row[1],
                'target_uid': row[2],
                'category': row[3],
                'kind': row[4],
                'problem': row[5],
                'solution': row[6],
                'reusable_experience': row[7],
                'misbelief': row[8],
                'scope': row[9] or {},
                'evidence_preview': _build_evidence_preview(row[10]),
                'source_kind': row[11],
                'source_track': row[12],
                'agent_client': row[13],
                'model': row[14],
                'state': row[15],
                'importance': int(row[16]),
                'created_at': _iso(row[17]),
                'gate_verdict': gate_verdict,
                'gate_confidence': gate_conf,
                'gate_reason': gate_reason,
            })

        return {
            'ok': True,
            'source_track': source_track,
            'shadow_mode': self.shadow_mode,
            'counts': self._counts_by_track(conn, owner_id),
            'reason_codes': [{'code': code, 'label': label} for code, label in REASON_CODES.items()],
            'items': items,
        }

    def _validate_decision_inputs(
        self,
        user_verdict: str,
        user_reason: str | None,
        edited_payload: dict[str, Any] | None,
    ) -> None:
        if user_verdict not in VALID_USER_VERDICTS:
            raise ReviewInboxError('invalid_user_verdict', 400)
        if user_verdict == 'reject':
            if not user_reason:
                raise ReviewInboxError('reason_required', 400)
            if user_reason not in REASON_CODES:
                raise ReviewInboxError('invalid_reason_code', 400)
        elif user_reason and user_reason not in REASON_CODES:
            raise ReviewInboxError('invalid_reason_code', 400)
        if edited_payload is not None and not isinstance(edited_payload, dict):
            raise ReviewInboxError('invalid_edited_payload', 400)

    def _apply_one(
        self,
        conn,
        *,
        owner_id: str,
        reviewer_id: str,
        proposal_uid: str,
        user_verdict: str,
        user_reason: str | None = None,
        note: str = '',
        edited_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._validate_decision_inputs(user_verdict, user_reason, edited_payload)
        row = conn.execute(
            """
            SELECT uid, category, kind, problem, solution, reusable_experience,
                   misbelief, scope, evidence_json, source_kind, source_track,
                   agent_client, model, device_id, session_id, state, importance
            FROM experience_proposals
            WHERE uid = %s AND owner_id = %s
            FOR UPDATE
            """,
            (proposal_uid, owner_id),
        ).fetchone()
        if not row:
            raise ReviewInboxError('proposal_not_found', 404)

        existing = conn.execute(
            'SELECT review_id FROM review_decisions WHERE proposal_uid = %s AND owner_id = %s LIMIT 1',
            (proposal_uid, owner_id),
        ).fetchone()
        if existing:
            raise ReviewInboxError('proposal_already_reviewed', 409)

        (
            _uid, category, kind, problem, solution, reusable_experience,
            misbelief, scope, evidence_json, source_kind, source_track,
            agent_client, model, device_id, session_id, _state, importance,
        ) = row

        gate_verdict, gate_conf, gate_reason = _latest_gate_decision(conn, proposal_uid)
        review_id = f'rev-{uuid.uuid4().hex[:16]}'
        decision_id: str | None = None
        applied_note: dict[str, Any] | None = None

        if user_verdict in ADMITTING_VERDICTS:
            edits = edited_payload or {}
            final_cat = str(edits.get('category') or category)
            if final_cat not in CATEGORIES:
                raise ReviewInboxError('invalid_category', 400)
            final_problem = str(edits.get('problem') if 'problem' in edits else problem).strip()
            final_solution = str(edits.get('solution') if 'solution' in edits else solution).strip()
            final_reusable = str(
                edits.get('reusable_experience') if 'reusable_experience' in edits else reusable_experience
            ).strip()
            if not (final_problem and final_solution and final_reusable):
                raise ReviewInboxError('empty_experience_fields', 400)
            final_importance = int(edits.get('importance') if 'importance' in edits else importance)
            final_importance = max(1, min(10, final_importance))

            decision_id = f'dec-{uuid.uuid4().hex[:16]}'
            conn.execute(
                """
                INSERT INTO proposal_decisions (
                    decision_id, proposal_uid, layer, verdict, reason_code,
                    confidence, judge_model, detail
                ) VALUES (%s, %s, 'L4', 'pass', %s, 1.0, 'human_review', %s)
                """,
                (decision_id, proposal_uid, user_reason, f'user_verdict={user_verdict}'),
            )
            ticket = self._issuer.issue(
                proposal_uid=proposal_uid,
                decision_id=decision_id,
                lane='gated',
            )
            origin = _SOURCE_KIND_TO_ORIGIN.get(source_kind, 'agent')
            try:
                write_res = self._writer.apply(
                    conn,
                    ticket,
                    Op(
                        action='create',
                        owner_id=owner_id,
                        actor_id=reviewer_id,
                        request_id=f'review:{review_id}',
                        category=final_cat,
                        problem=final_problem,
                        solution=final_solution,
                        reusable_experience=final_reusable,
                        origin=origin,
                        kind=kind,
                        misbelief=misbelief or '',
                        scope=scope if isinstance(scope, dict) else {},
                        evidence=evidence_json if isinstance(evidence_json, list) else [],
                        importance=final_importance,
                        source_device_id=device_id or '',
                        agent_client=agent_client or '',
                        model=model or '',
                    ),
                )
                applied_note = write_res['experience']
            except ExperienceWriteError as exc:
                raise ReviewInboxError(exc.code, exc.status) from None
            new_state = 'accepted'
        else:
            new_state = 'rejected'

        conn.execute(
            """
            INSERT INTO review_decisions (
                review_id, owner_id, reviewer_id, proposal_uid, category, source_track,
                gate_verdict, gate_confidence, gate_reason,
                user_verdict, user_reason, note, edited_payload, decision_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                review_id, owner_id, reviewer_id, proposal_uid, category, source_track,
                gate_verdict, gate_conf, gate_reason,
                user_verdict, user_reason, (note or '')[:1000],
                json.dumps(edited_payload, ensure_ascii=False) if edited_payload is not None else None,
                decision_id,
            ),
        )
        conn.execute(
            'UPDATE experience_proposals SET state = %s, updated_at = now() WHERE uid = %s',
            (new_state, proposal_uid),
        )
        return {
            'ok': True,
            'review_id': review_id,
            'proposal_uid': proposal_uid,
            'state': new_state,
            'note': applied_note,
            'gate_agreement': (
                None if gate_verdict is None
                else (
                    (gate_verdict == 'pass' and user_verdict in ADMITTING_VERDICTS)
                    or (gate_verdict == 'reject' and user_verdict == 'reject')
                )
            ),
        }

    def decide(
        self,
        conn,
        *,
        owner_id: str,
        reviewer_id: str,
        proposal_uid: str,
        user_verdict: str,
        user_reason: str | None = None,
        note: str = '',
        edited_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = self._apply_one(
            conn,
            owner_id=owner_id,
            reviewer_id=reviewer_id,
            proposal_uid=proposal_uid,
            user_verdict=user_verdict,
            user_reason=user_reason,
            note=note,
            edited_payload=edited_payload,
        )
        conn.commit()
        return result

    def batch_decide(
        self,
        conn,
        *,
        owner_id: str,
        reviewer_id: str,
        proposal_uids: list[str],
        user_verdict: str,
        user_reason: str | None = None,
        note: str = '',
    ) -> dict[str, Any]:
        if not isinstance(proposal_uids, list) or not proposal_uids or len(proposal_uids) > 100:
            raise ReviewInboxError('invalid_proposal_uids', 400)
        if user_verdict == 'edit_accept':
            raise ReviewInboxError('batch_edit_not_supported', 400)
        results = [
            self._apply_one(
                conn,
                owner_id=owner_id,
                reviewer_id=reviewer_id,
                proposal_uid=str(uid),
                user_verdict=user_verdict,
                user_reason=user_reason,
                note=note,
            )
            for uid in proposal_uids
        ]
        conn.commit()
        return {'ok': True, 'processed': len(results), 'results': results}
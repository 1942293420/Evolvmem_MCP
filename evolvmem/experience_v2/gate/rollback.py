"""Stage 2 Task 2.9: Per-model rollback with mandatory signed dry-run."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import hmac
import json
from typing import Any
import uuid

from evolvmem.experience_v2.writer import (
    ExperienceWriteError,
    ExperienceWriter,
    Op,
    TicketIssuer,
    _secret_bytes,
)


class RollbackPlanRequiredError(ValueError):
    """Raised when rollback execution lacks a valid, fresh, signed dry-run plan."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class RollbackPlan:
    plan_id: str
    owner_id: str
    agent_client: str
    model: str
    since_iso: str | None
    until_iso: str | None
    event_seqs: tuple[int, ...]
    operations: tuple[dict[str, Any], ...]
    pre_state_digest: str
    signature: str


def _iso_or_none(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    norm = dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    return norm.astimezone(timezone.utc).isoformat()


def _canon(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _sign_plan(
    secret: bytes,
    *,
    plan_id: str,
    owner_id: str,
    agent_client: str,
    model: str,
    since_iso: str | None,
    until_iso: str | None,
    event_seqs: tuple[int, ...],
    operations: tuple[dict[str, Any], ...],
    pre_state_digest: str,
) -> str:
    msg = _canon({
        'plan_id': plan_id,
        'owner_id': owner_id,
        'agent_client': agent_client,
        'model': model,
        'since_iso': since_iso,
        'until_iso': until_iso,
        'event_seqs': list(event_seqs),
        'operations': list(operations),
        'pre_state_digest': pre_state_digest,
    }).encode('utf-8')
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def _compute_pre_state_digest(
    conn,
    *,
    owner_id: str,
    note_ids: list[str],
    event_seqs: tuple[int, ...],
    for_update: bool = False,
) -> str:
    if not note_ids:
        return hashlib.sha256(_canon({'notes': [], 'event_seqs': list(event_seqs)}).encode('utf-8')).hexdigest()
    lock_sql = ' FOR UPDATE' if for_update else ''
    rows = conn.execute(
        f"""
        SELECT id, revision
        FROM experience_notes
        WHERE owner_id = %s AND id = ANY(%s)
        ORDER BY id ASC{lock_sql}
        """,
        (owner_id, note_ids),
    ).fetchall()
    return hashlib.sha256(
        _canon({
            'notes': [[r[0], int(r[1])] for r in rows],
            'event_seqs': list(event_seqs),
        }).encode('utf-8')
    ).hexdigest()


class ModelRollbackService:
    """Builds signed dry-run rollback plans per (agent_client, model) and applies via ExperienceWriter."""

    def __init__(self, *, ticket_secret: bytes | str):
        self._secret = _secret_bytes(ticket_secret)
        self._issuer = TicketIssuer(self._secret, gate_id='model_rollback')
        self._writer = ExperienceWriter(self._secret)

    def plan_model_rollback(
        self,
        conn,
        *,
        owner_id: str,
        agent_client: str,
        model: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> RollbackPlan:
        """Read-only dry-run: computes reverse operations in LIFO order without modifying DB."""
        if not owner_id or not agent_client or not model:
            raise RollbackPlanRequiredError('invalid_rollback_scope', 400)

        clauses = [
            'owner_id = %s',
            'agent_client = %s',
            'model = %s',
            "action <> 'rollback'",
            'rolled_back_by_seq IS NULL',
        ]
        params: list[Any] = [owner_id, agent_client, model]
        if since is not None:
            clauses.append('created_at >= %s')
            params.append(since)
        if until is not None:
            clauses.append('created_at <= %s')
            params.append(until)

        rows = conn.execute(
            f"""
            SELECT seq, action, note_id, before_json, after_json, created_at
            FROM experience_events
            WHERE {' AND '.join(clauses)}
            ORDER BY seq DESC
            """,
            params,
        ).fetchall()

        event_seqs: list[int] = []
        operations: list[dict[str, Any]] = []
        note_ids: list[str] = []
        for seq, action, note_id, before_json, _after_json, _created_at in rows:
            seq_i = int(seq)
            event_seqs.append(seq_i)
            if note_id not in note_ids:
                note_ids.append(note_id)
            snap = before_json if action not in ('create', 'import_manual') else None
            operations.append({
                'target_event_seq': seq_i,
                'source_action': action,
                'note_id': note_id,
                'restore_snapshot': snap,
                'reverse_mode': 'retire' if snap is None else 'restore_before_json',
            })

        seq_tuple = tuple(event_seqs)
        op_tuple = tuple(operations)
        digest = _compute_pre_state_digest(
            conn,
            owner_id=owner_id,
            note_ids=note_ids,
            event_seqs=seq_tuple,
            for_update=False,
        )
        plan_id = f'rbplan-{uuid.uuid4().hex[:16]}'
        since_iso = _iso_or_none(since)
        until_iso = _iso_or_none(until)
        sig = _sign_plan(
            self._secret,
            plan_id=plan_id,
            owner_id=owner_id,
            agent_client=agent_client,
            model=model,
            since_iso=since_iso,
            until_iso=until_iso,
            event_seqs=seq_tuple,
            operations=op_tuple,
            pre_state_digest=digest,
        )
        return RollbackPlan(
            plan_id=plan_id,
            owner_id=owner_id,
            agent_client=agent_client,
            model=model,
            since_iso=since_iso,
            until_iso=until_iso,
            event_seqs=seq_tuple,
            operations=op_tuple,
            pre_state_digest=digest,
            signature=sig,
        )

    def execute_model_rollback(
        self,
        conn,
        *,
        plan: RollbackPlan,
        actor_id: str,
        request_id: str,
    ) -> dict[str, Any]:
        if not isinstance(plan, RollbackPlan):
            raise RollbackPlanRequiredError('dry_run_required', 400)
        if not actor_id or not request_id:
            raise RollbackPlanRequiredError('invalid_rollback_actor_or_request', 400)

        expected_sig = _sign_plan(
            self._secret,
            plan_id=plan.plan_id,
            owner_id=plan.owner_id,
            agent_client=plan.agent_client,
            model=plan.model,
            since_iso=plan.since_iso,
            until_iso=plan.until_iso,
            event_seqs=plan.event_seqs,
            operations=plan.operations,
            pre_state_digest=plan.pre_state_digest,
        )
        if not hmac.compare_digest(plan.signature, expected_sig):
            raise RollbackPlanRequiredError('invalid_plan_signature', 403)

        if not plan.operations:
            return {'ok': True, 'plan_id': plan.plan_id, 'reverted_events': 0, 'notes': []}

        note_ids = list(dict.fromkeys(op['note_id'] for op in plan.operations))
        unreverted = conn.execute(
            """
            SELECT seq
            FROM experience_events
            WHERE owner_id = %s AND seq = ANY(%s) AND rolled_back_by_seq IS NULL
            FOR UPDATE
            """,
            (plan.owner_id, list(plan.event_seqs)),
        ).fetchall()
        if len(unreverted) != len(plan.event_seqs):
            raise RollbackPlanRequiredError('stale_rollback_plan', 409)

        live_digest = _compute_pre_state_digest(
            conn,
            owner_id=plan.owner_id,
            note_ids=note_ids,
            event_seqs=plan.event_seqs,
            for_update=True,
        )
        if live_digest != plan.pre_state_digest:
            raise RollbackPlanRequiredError('stale_rollback_plan', 409)

        applied: list[dict[str, Any]] = []
        for entry in plan.operations:
            seq_i = int(entry['target_event_seq'])
            ticket = self._issuer.issue(
                proposal_uid=f'rollback:{plan.plan_id}:{seq_i}',
                decision_id=f'rollback:{plan.plan_id}:{seq_i}',
                lane='user_fast',
            )
            try:
                res = self._writer.apply(
                    conn,
                    ticket,
                    Op(
                        action='rollback',
                        owner_id=plan.owner_id,
                        actor_id=actor_id,
                        request_id=f'{request_id}:seq:{seq_i}',
                        note_id=entry['note_id'],
                        target_event_seq=seq_i,
                        restore_snapshot=entry['restore_snapshot'],
                        agent_client=plan.agent_client,
                        model=plan.model,
                    ),
                )
            except ExperienceWriteError as exc:
                raise RollbackPlanRequiredError(exc.code, exc.status) from None
            applied.append(res['experience'])

        conn.commit()
        return {
            'ok': True,
            'plan_id': plan.plan_id,
            'reverted_events': len(applied),
            'notes': applied,
        }
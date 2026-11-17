"""Task 2.5: Proposal Inbox admission, reputation tier & session quota enforcement."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import re
import secrets
from typing import Any, Callable

from evolvmem.experience_v2.control.agent_identity import AgentAttribution
from evolvmem.experience_v2.control.reputation_policy import (
    ProposalOperation,
    ReputationCoordinator,
    ReputationPolicyConfig,
    TrustedChannel,
)
from evolvmem.experience_v2.writer import CATEGORIES

VALID_OPS = frozenset({'add', 'update', 'retire'})
VALID_KINDS = frozenset({'env_fact', 'pitfall', 'procedure', 'preference', 'decision', 'habit'})
VALID_SOURCE_KINDS = frozenset({'user', 'agent', 'pipeline', 'legacy'})
VALID_TRACKS = frozenset({'live', 'backfill'})
_REQUEST_ID_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z', re.ASCII)
_ALPHABET = '0123456789abcdefghjkmnpqrstvwxyz'


class ProposalPolicyError(ValueError):
    """Domain, quota, or tier-authorization failure when submitting an experience proposal."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


def _json_canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _new_proposal_uid() -> str:
    ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    chars: list[str] = []
    for _ in range(10):
        chars.append(_ALPHABET[ms % 32])
        ms //= 32
    chars.reverse()
    rand = ''.join(secrets.choice(_ALPHABET) for _ in range(16))
    return 'prop-' + ''.join(chars) + rand


def _normalize_text(val: Any, *, allow_empty: bool = False) -> str:
    if val is None:
        if allow_empty:
            return ''
        raise ProposalPolicyError('invalid_proposal_fields', 400)
    if not isinstance(val, str):
        raise ProposalPolicyError('invalid_proposal_fields', 400)
    cleaned = val.strip()
    if not cleaned and not allow_empty:
        raise ProposalPolicyError('invalid_proposal_fields', 400)
    return cleaned


class _ConfigOnlyReputationStore:
    """Fallback read-only store when no live em_control connection is bound."""

    def query(self, sql, params=()):
        return []

    def mutate(self, sql, params=()):
        return []


class ProposalService:
    """Validates agent proposals, enforces tier/quota/sensitivity, and stores in experience_proposals."""

    def __init__(self, *, reputation_engine: ReputationCoordinator | None = None):
        self._engine = reputation_engine or ReputationCoordinator(
            _ConfigOnlyReputationStore(), config=ReputationPolicyConfig()
        )

    def propose(
        self,
        conn,
        *,
        owner_id: str,
        actor_id: str,
        channel: TrustedChannel,
        attribution: AgentAttribution,
        body: dict[str, Any],
        source_kind: str = 'agent',
        gate_hook: Callable[[Any, str], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        if not owner_id or not actor_id or not isinstance(body, dict):
            raise ProposalPolicyError('invalid_proposal_fields', 400)

        op = body.get('op', 'add')
        if op not in VALID_OPS:
            raise ProposalPolicyError('invalid_proposal_op', 400)
        request_id = body.get('request_id')
        if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
            raise ProposalPolicyError('invalid_request_id', 400)

        category = body.get('category')
        kind = body.get('kind', 'pitfall')
        if category not in CATEGORIES:
            raise ProposalPolicyError('invalid_experience_category', 400)
        if kind not in VALID_KINDS:
            raise ProposalPolicyError('invalid_experience_kind', 400)

        track = body.get('source_track', 'live')
        if track not in VALID_TRACKS or source_kind not in VALID_SOURCE_KINDS:
            raise ProposalPolicyError('invalid_proposal_source', 400)

        target_uid = body.get('target_uid')
        if op in ('update', 'retire') and (not isinstance(target_uid, str) or not target_uid.strip()):
            raise ProposalPolicyError('missing_target_uid', 400)
        target_uid = target_uid.strip() if isinstance(target_uid, str) and target_uid.strip() else None

        problem = _normalize_text(body.get('problem'))
        solution = _normalize_text(body.get('solution'))
        reusable = _normalize_text(body.get('reusable_experience'))
        misbelief = _normalize_text(body.get('misbelief', ''), allow_empty=True)

        scope = body.get('scope') if body.get('scope') is not None else {}
        if not isinstance(scope, dict):
            raise ProposalPolicyError('invalid_proposal_scope', 400)
        evidence = body.get('evidence') if body.get('evidence') is not None else []
        if not isinstance(evidence, list) or not all(isinstance(item, dict) for item in evidence):
            raise ProposalPolicyError('invalid_proposal_evidence', 400)

        importance = body.get('importance', 5)
        if type(importance) is not int or not 1 <= importance <= 10:
            raise ProposalPolicyError('invalid_proposal_importance', 400)
        pinned = body.get('pinned', False)
        if type(pinned) is not bool:
            raise ProposalPolicyError('invalid_proposal_pinned', 400)

        session_id = body.get('session_id')
        if session_id is not None and (not isinstance(session_id, str) or not session_id.strip()):
            raise ProposalPolicyError('invalid_session_id', 400)
        session_id = session_id.strip() if isinstance(session_id, str) else None

        canonical_payload = {
            'op': op,
            'target_uid': target_uid,
            'category': category,
            'kind': kind,
            'problem': problem,
            'solution': solution,
            'reusable_experience': reusable,
            'misbelief': misbelief,
            'scope': scope,
            'evidence': evidence,
            'importance': importance,
            'pinned': pinned,
            'source_track': track,
        }
        canonical_str = _json_canon(canonical_payload)

        with conn.transaction():
            prior = conn.execute(
                """
                SELECT uid, state, payload_json, actor_id
                FROM experience_proposals
                WHERE owner_id = %s AND request_id = %s
                FOR UPDATE
                """,
                (owner_id, request_id),
            ).fetchone()
            if prior is not None:
                prior_uid, prior_state, prior_payload, prior_actor = prior
                if prior_actor != actor_id or _json_canon(prior_payload) != canonical_str:
                    raise ProposalPolicyError('proposal_request_id_conflict', 409)
                return {
                    'ok': True,
                    'uid': prior_uid,
                    'state': prior_state,
                    'replayed': True,
                }

            if session_id:
                session_count = conn.execute(
                    """
                    SELECT count(*) FROM experience_proposals
                    WHERE owner_id = %s AND session_id = %s AND agent_client = %s AND model = %s
                    """,
                    (owner_id, session_id, attribution.client, attribution.model),
                ).fetchone()[0]
            else:
                session_count = conn.execute(
                    """
                    SELECT count(*) FROM experience_proposals
                    WHERE owner_id = %s AND agent_client = %s AND model = %s
                      AND created_at >= now() - interval '8 hours'
                    """,
                    (owner_id, attribution.client, attribution.model),
                ).fetchone()[0]

            decision = self._engine.authorize_operation(
                attribution,
                ProposalOperation(op),
                channel=channel,
                session_proposed_count=int(session_count),
                target_pinned=pinned,
                target_importance=float(importance) / 10.0,
            )
            if not decision.allowed:
                status = 429 if decision.reason == 'session_quota_exceeded' else 403
                raise ProposalPolicyError(decision.reason, status)

            uid = _new_proposal_uid()
            conn.execute(
                """
                INSERT INTO experience_proposals(
                    uid, owner_id, op, target_uid, category, kind,
                    problem, solution, reusable_experience, misbelief,
                    scope, payload_json, evidence_json,
                    source_kind, source_track, agent_client, model, device_id,
                    actor_id, session_id, request_id, state, importance, pinned
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, %s::jsonb,
                    %s, %s, %s, %s, %s,
                    %s, %s, %s, 'pending', %s, %s
                )
                """,
                (
                    uid, owner_id, op, target_uid, category, kind,
                    problem, solution, reusable, misbelief,
                    _json_canon(scope), canonical_str, _json_canon(evidence),
                    source_kind, track, attribution.client, attribution.model, attribution.device_id or '',
                    actor_id, session_id, request_id, importance, pinned,
                ),
            )
            final_state = 'pending'
            gate_result = None
            if gate_hook is not None:
                gate_result = gate_hook(conn, uid)
                row_state = conn.execute(
                    'SELECT state FROM experience_proposals WHERE uid = %s',
                    (uid,),
                ).fetchone()
                if row_state:
                    final_state = row_state[0]

        result = {
            'ok': True,
            'uid': uid,
            'state': final_state,
            'tier': decision.tier,
            'requires_user_review': bool(decision.requires_user_review),
            'replayed': False,
        }
        if gate_result is not None:
            result['gate'] = gate_result
        return result

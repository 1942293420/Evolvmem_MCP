"""Task 2.5/2.10: MCP specs and handlers for experience_v2 tools."""
from __future__ import annotations

from pathlib import Path
import math
import time
from typing import Any, Sequence

from evolvmem.experience_v2.changes_sync import changes_since, recall_pg_experiences
from evolvmem.experience_v2.gate.dedupe import _encode_text, _resolve_gate_mode, run_l1_dedupe
from evolvmem.experience_v2.gate.rules import SqliteHistoryEvidenceVerifier, run_l0_gate
from evolvmem.experience_v2.writer import ExperienceWriter, TicketIssuer
from evolvmem.experience_v2.control.agent_identity import AgentAttribution
from evolvmem.experience_v2.control.reputation_policy import (
    ReputationCoordinator,
    ReputationPolicyConfig,
    TrustedChannel,
)
from evolvmem.experience_v2.gate.propose import (
    CATEGORIES,
    ProposalPolicyError,
    ProposalService,
    VALID_KINDS,
    VALID_OPS,
)
from evolvmem.experience_v2.pg_experience_store import (
    PgExperienceUnavailableError,
    load_pg_experience_settings,
    open_experience_conn,
)
from evolvmem.experience_v2.deadline_pg import open_experience_deadline
from evolvmem.manual_experience_mcp import ManualMcpError
from evolvmem.product_policy import load_policy

EXPERIENCE_V2_MCP_SPECS: dict[str, dict[str, Any]] = {
    'experience_propose': {
        'name': 'experience_propose',
        'description': (
            'Submit a reusable engineering experience proposal into the gated PG inbox '
            'under model tier and session quota enforcement. Returns proposal uid and state.'
        ),
        'annotations': {'readOnlyHint': False, 'openWorldHint': False},
        'inputSchema': {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'op': {'type': 'string', 'enum': sorted(VALID_OPS)},
                'target_uid': {'type': 'string', 'minLength': 1, 'maxLength': 80},
                'category': {'type': 'string', 'enum': list(CATEGORIES)},
                'kind': {'type': 'string', 'enum': sorted(VALID_KINDS)},
                'problem': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
                'solution': {'type': 'string', 'minLength': 1, 'maxLength': 8000},
                'reusable_experience': {'type': 'string', 'minLength': 1, 'maxLength': 8000},
                'misbelief': {'type': 'string', 'maxLength': 400},
                'scope': {'type': 'object'},
                'evidence': {'type': 'array', 'items': {'type': 'object'}},
                'importance': {'type': 'integer', 'minimum': 1, 'maximum': 10},
                'pinned': {'type': 'boolean'},
                'session_id': {'type': 'string', 'minLength': 1, 'maxLength': 128},
                'request_id': {'type': 'string', 'minLength': 8, 'maxLength': 128},
                'source_track': {'type': 'string', 'enum': ['live', 'backfill']},
            },
            'required': ['category', 'problem', 'solution', 'reusable_experience', 'request_id'],
        },
    },
    'experience_changes_since': {
        'name': 'experience_changes_since',
        'description': (
            'Return incremental PG experience changes since the given sequence cursor '
            '(or bootstrap the latest cursor without payload).'
        ),
        'annotations': {'readOnlyHint': True, 'openWorldHint': False},
        'inputSchema': {
            'type': 'object',
            'additionalProperties': False,
            'properties': {
                'cursor': {'type': 'integer', 'minimum': 0},
                'limit': {'type': 'integer', 'minimum': 1, 'maximum': 200},
                'bootstrap_only': {'type': 'boolean'},
            },
            'required': ['cursor'],
        },
    },
}


EXPERIENCE_RECALL_SPEC = {
    "name": "experience_recall",
    "description": "Recall relevant experiences from the authenticated user's authorized owners.",
    "annotations": {"readOnlyHint": True, "openWorldHint": False},
    "inputSchema": {
        "type": "object", "additionalProperties": False, "required": ["query"],
        "properties": {
            "query": {"type": "string", "minLength": 1, "maxLength": 4000},
            "project": {"type": "string", "maxLength": 300},
            "constraints": {"type": "object", "maxProperties": 20,
                "propertyNames": {"minLength": 1, "maxLength": 100},
                "additionalProperties": {"type": "string", "maxLength": 1000}},
            "mode": {"type": "string", "enum": ["hook", "full"]},
            "timeout_ms": {"type": "integer", "minimum": 1, "maximum": 1800},
            "session_id": {"type": "string", "minLength": 1, "maxLength": 128},
            "workstream_id": {"type": "string", "maxLength": 128,
                "description": "Deprecated continuity identifier; never used as a session identifier."},
        },
    },
}


def valid_experience_recall_args(args):
    schema = EXPERIENCE_RECALL_SPEC["inputSchema"]
    if (not isinstance(args, dict) or set(args)-set(schema["properties"])
            or "query" not in args):
        return False
    query = args["query"]
    if (not isinstance(query, str) or not query.strip() or len(query) > 4000
            or any(ord(c) < 32 and c not in "\n\r\t" for c in query)):
        return False
    if "project" in args and (not isinstance(args["project"], str) or len(args["project"]) > 300):
        return False
    constraints = args.get("constraints", {})
    if (not isinstance(constraints, dict) or len(constraints) > 20
            or any(not isinstance(k, str) or not 1 <= len(k) <= 100
                or not isinstance(v, str) or len(v) > 1000 for k, v in constraints.items())):
        return False
    if args.get("mode", "full") not in ("hook", "full"):
        return False
    if type(args.get("timeout_ms", 1800)) is not int or not 1 <= args.get("timeout_ms", 1800) <= 1800:
        return False
    return all(key not in args or isinstance(args[key], str)
        and (1 if key == "session_id" else 0) <= len(args[key]) <= 128
        for key in ("session_id", "workstream_id"))


class _PrecomputedEmbedder:
    def __init__(self, vec: list[float]):
        self._vec = vec

    def encode(self, _text: str, _kind: str = 'document') -> list[float]:
        return self._vec


def _precompute_vector(embedder: Any, args: dict[str, Any]) -> list[float] | None:
    if embedder is None or not getattr(embedder, 'is_loaded', True):
        return None
    doc_text = f"{args.get('problem', '')}\n{args.get('solution', '')}\n{args.get('reusable_experience', '')}"
    try:
        return _encode_text(embedder, doc_text)
    except Exception:
        return None


def _build_live_gate_hook(
    *,
    owner_id: str = '',
    history_db_paths: Sequence[Path | str],
    vec: list[float] | None,
    ticket_secret: bytes,
    gate_mode: str | dict[str, str],
    category: str,
):
    from evolvmem.product_policy import load_policy
    if load_policy().history_pg_enabled:
        import os
        from evolvmem.experience_v2.pipeline.pg_history_reader import PgHistoryEvidenceVerifier, PgHistoryReader
        uid = str(owner_id or os.environ.get('EVOLVMEM_CONSOLE_OWNER_ID') or '').strip()
        verifier = PgHistoryEvidenceVerifier(PgHistoryReader(user_id=uid))
    else:
        existing = [Path(p) for p in history_db_paths if p and str(p).strip() and Path(p).is_file()]
        verifier = SqliteHistoryEvidenceVerifier(existing) if existing else None

    def _hook(conn, uid: str) -> dict[str, Any]:
        l0 = run_l0_gate(conn, uid, evidence_verifier=verifier, require_evidence=True)
        if l0['verdict'] != 'pass':
            return l0
        if vec is not None:
            return run_l1_dedupe(
                conn,
                uid,
                embedder=_PrecomputedEmbedder(vec),
                ticket_issuer=TicketIssuer(ticket_secret, gate_id='L1_dedupe'),
                writer=ExperienceWriter(ticket_secret),
                gate_mode=gate_mode,
            )
        mode = _resolve_gate_mode(gate_mode, category)
        if mode in ('review', 'partial', 'auto'):
            conn.execute("UPDATE experience_proposals SET state = 'review', updated_at = now() WHERE uid = %s", (uid,))
        return l0

    return _hook


def call_experience_v2_tool(
    tool_name: str,
    args: dict[str, Any],
    *,
    owner_id: str,
    actor_id: str,
    attribution: AgentAttribution,
    channel: TrustedChannel = TrustedChannel.MCP_CLIENT,
    reputation_config: ReputationPolicyConfig | None = None,
    gate_hook=None,
    history_db_paths: Sequence[Path | str] | None = None,
    embedder: Any = None,
    settings: Any = None,
) -> dict[str, Any]:
    if not isinstance(args, dict):
        raise ManualMcpError('invalid_arguments')
    policy = load_policy()
    if not policy.valid or not policy.experience_v2_enabled:
        raise ManualMcpError('experience_v2_disabled')
    if tool_name not in EXPERIENCE_V2_MCP_SPECS:
        raise ManualMcpError('unknown_tool')

    spec = EXPERIENCE_V2_MCP_SPECS[tool_name]['inputSchema']
    if set(args) - set(spec['properties']) or set(spec.get('required', [])) - set(args):
        raise ManualMcpError('invalid_arguments')

    pre_vec = _precompute_vector(embedder, args) if (tool_name == 'experience_propose' and gate_hook is None and history_db_paths is not None) else None
    try:
        settings = settings if settings is not None else load_pg_experience_settings()
        if tool_name == 'experience_propose' and gate_hook is None and history_db_paths is not None:
            gate_hook = _build_live_gate_hook(
                owner_id=owner_id,
                history_db_paths=history_db_paths,
                vec=pre_vec,
                ticket_secret=settings.ticket_secret,
                gate_mode=policy.experience_gate_mode,
                category=str(args.get('category') or ''),
            )
        with open_experience_conn(settings, role='coordinator') as conn:
            if tool_name == 'experience_changes_since':
                return changes_since(
                    conn,
                    owner_id=owner_id,
                    cursor=int(args.get('cursor', 0)),
                    limit=int(args.get('limit', 50)),
                    bootstrap_only=bool(args.get('bootstrap_only', False)),
                )
            from evolvmem.experience_v2.gate.propose import _ConfigOnlyReputationStore
            engine = (
                ReputationCoordinator(_ConfigOnlyReputationStore(), config=reputation_config)
                if reputation_config is not None else None
            )
            svc = ProposalService(reputation_engine=engine)
            return svc.propose(
                conn,
                owner_id=owner_id,
                actor_id=actor_id,
                channel=channel,
                attribution=attribution,
                body=args,
                source_kind='agent',
                gate_hook=gate_hook,
            )
    except PgExperienceUnavailableError:
        raise ManualMcpError('pg_experience_unavailable') from None
    except ProposalPolicyError as exc:
        raise ManualMcpError(exc.code) from None


def call_pg_experience_recall(
    args: dict[str, Any],
    *,
    owner_id: str,
    attribution: AgentAttribution | None = None,
    channel: str = 'mcp',
    settings: Any = None,
    deadline: float | None = None,
    authorized_owner_ids: list[str] | None = None,
) -> dict[str, Any]:
    if not valid_experience_recall_args(args):
        return {'error': 'invalid_arguments'}
    requested = args.get('timeout_ms', 1800)
    if type(requested) is not int or requested <= 0:
        return {'error': 'invalid_arguments'}
    started = time.monotonic()
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        return {'error': 'invalid_arguments'}
    until = min(started + min(1800, requested)/1000, deadline) if deadline is not None else started + min(1800, requested)/1000
    session_id = args.get('session_id')
    if session_id is not None and (not isinstance(session_id, str) or not 1 <= len(session_id) <= 128):
        return {'error': 'invalid_arguments'}
    try:
        with open_experience_deadline(settings, role='coordinator', deadline=until) as conn:
            return recall_pg_experiences(
                conn, owner_id=owner_id, query=args.get('query'),
                project=str(args.get('project') or ''), constraints=args.get('constraints'),
                mode=str(args.get('mode') or 'full'), channel='hook' if args.get('mode') == 'hook' else channel, session_id=session_id,
                agent_client=attribution.client if attribution is not None else '',
                model=attribution.model if attribution is not None else '',
                deadline=until, authorized_owner_ids=authorized_owner_ids,
            )
    except TimeoutError:
        return {'error': 'request_timeout'}
    except ValueError:
        return {'error': 'invalid_arguments'}
    except PgExperienceUnavailableError:
        return {'error': 'pg_experience_unavailable'}

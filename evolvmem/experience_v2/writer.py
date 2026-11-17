"""Ticket-enforced single write gate for PostgreSQL experience_notes."""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
from typing import Any, Literal
import uuid

CATEGORIES = ('前端经验', '业务经验', '系统环境经验')
_WRITE_SQL_RE = re.compile(
    r'(?is)\b(?:insert\s+into|update|delete\s+from)\s+(?:[a-zA-Z_][a-zA-Z0-9_]*\.)?experience_notes\b'
)
_SELECT_COLS = (
    'id, owner_id, category, problem, solution, reusable_experience, origin, kind, '
    'misbelief, scope, evidence, occurrence_count, importance, pinned, status, '
    'confidence_a, confidence_b, stability, last_used_at, use_count, valid_from, valid_until, '
    'proposal_uid, source_actor, source_device_id, source_agent, source_model, '
    'revision, deleted_at, created_at, updated_at, retrieval_embedding_contract, '
    'retrieval_embedding_content_sha256, retrieval_embedding_updated_at'
)


class TicketVerificationError(PermissionError):
    """Raised when an AdmissionTicket is missing, forged, tampered, or unverified."""


class ExperienceWriteError(ValueError):
    """Domain/concurrency write error matching manual_experiences semantics."""

    def __init__(self, code: str, status: int = 400):
        super().__init__(code)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class AdmissionTicket:
    proposal_uid: str
    decision_id: str
    lane: Literal['user_fast', 'gated']
    issued_by: str
    signature: str


@dataclass(frozen=True)
class Op:
    action: Literal['create', 'update', 'delete', 'retire', 'restore', 'merge_evidence', 'import_manual', 'rollback', 'reconcile_state', 'reembed']
    owner_id: str
    actor_id: str
    request_id: str
    note_id: str | None = None
    category: str | None = None
    problem: str | None = None
    solution: str | None = None
    reusable_experience: str | None = None
    expected_revision: int | None = None
    expected_content_sha256: str | None = None
    origin: str = 'manual'
    kind: str | None = None
    misbelief: str = ''
    scope: dict[str, Any] = field(default_factory=dict)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    importance: int = 5
    pinned: bool = False
    status: str = 'active'
    embedding: list[float] | None = None
    source_device_id: str = ''
    agent_client: str = ''
    model: str = ''
    deleted_at: Any = None
    created_at: Any = None
    updated_at: Any = None
    revision: int | None = None
    target_event_seq: int | None = None
    restore_snapshot: dict[str, Any] | None = None
    valid_from: Any = None
    valid_until: Any = None


def _secret_bytes(secret: bytes | str) -> bytes:
    if isinstance(secret, bytes):
        out = secret
    elif isinstance(secret, str):
        out = secret.encode('utf-8')
    else:
        raise TicketVerificationError('invalid_ticket_secret')
    if not out:
        raise TicketVerificationError('empty_ticket_secret')
    return out


def _canonical_ticket_msg(proposal_uid: str, decision_id: str, lane: str, issued_by: str) -> bytes:
    return f'{proposal_uid}\n{decision_id}\n{lane}\n{issued_by}'.encode('utf-8')


class TicketIssuer:
    def __init__(self, secret: bytes | str, *, gate_id: str = 'gate-v2'):
        self._secret = _secret_bytes(secret)
        self.gate_id = str(gate_id).strip() or 'gate-v2'

    def issue(self, *, proposal_uid: str, decision_id: str, lane: Literal['user_fast', 'gated']) -> AdmissionTicket:
        if lane not in ('user_fast', 'gated'):
            raise TicketVerificationError('invalid_ticket_lane')
        p_uid = str(proposal_uid or '').strip()
        d_id = str(decision_id or '').strip()
        if not p_uid or not d_id:
            raise TicketVerificationError('invalid_ticket_fields')
        sig = hmac.new(self._secret, _canonical_ticket_msg(p_uid, d_id, lane, self.gate_id), hashlib.sha256).hexdigest()
        return AdmissionTicket(proposal_uid=p_uid, decision_id=d_id, lane=lane, issued_by=self.gate_id, signature=sig)


def _json_canon(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)


def _new_exp_id() -> str:
    alphabet = '0123456789abcdefghjkmnpqrstvwxyz'
    ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    chars: list[str] = []
    for _ in range(10):
        chars.append(alphabet[ms % 32])
        ms //= 32
    chars.reverse()
    rand = ''.join(secrets.choice(alphabet) for _ in range(16))
    return 'exp-' + ''.join(chars) + rand


def _iso(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        dt = val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return str(val)


def _vec_literal(embedding: list[float] | None) -> str | None:
    if embedding is None:
        return None
    return '[' + ','.join(f'{float(x):.8g}' for x in embedding) + ']'


def _projection_update(projection, clear, legacy, now):
    if projection is None and not clear:
        return '', ()
    sql = (', embedding = %s::vector, retrieval_embedding = %s::vector, '
           'retrieval_embedding_contract = %s, retrieval_embedding_content_sha256 = %s, '
           'retrieval_embedding_updated_at = %s')
    values = (legacy, _vec_literal(projection.retrieval_embedding) if projection else None,
              projection.contract_id if projection else None, projection.content_sha256 if projection else None,
              now if projection else None)
    return sql, values


def _row_to_note(row: tuple[Any, ...]) -> dict[str, Any]:
    cols = [c.strip() for c in _SELECT_COLS.split(',')]
    data = dict(zip(cols, row))
    for k in ('last_used_at', 'valid_from', 'valid_until', 'deleted_at', 'created_at', 'updated_at', 'retrieval_embedding_updated_at'):
        data[k] = _iso(data[k])
    return data


class ExperienceWriter:
    """Single authorized write entrypoint for PG experience_notes."""

    def __init__(self, secret: bytes | str, *, embedding_client=None):
        self._secret = _secret_bytes(secret)
        self.embedding_client = embedding_client

    def verify_ticket(self, conn, ticket: AdmissionTicket | None, op: Op) -> str | None:
        if not isinstance(ticket, AdmissionTicket):
            raise TicketVerificationError('missing_admission_ticket')
        if ticket.lane not in ('user_fast', 'gated'):
            raise TicketVerificationError('invalid_ticket_lane')
        expected_sig = hmac.new(
            self._secret,
            _canonical_ticket_msg(ticket.proposal_uid, ticket.decision_id, ticket.lane, ticket.issued_by),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(ticket.signature, expected_sig):
            raise TicketVerificationError('invalid_ticket_signature')
        if op.action in ('create', 'update', 'delete', 'retire', 'merge_evidence', 'reconcile_state') and op.origin in ('agent', 'pipeline') and ticket.lane != 'gated':
            raise TicketVerificationError('automated_write_requires_gated_ticket')
        if ticket.lane == 'gated':
            row = conn.execute(
                """
                SELECT d.verdict, p.owner_id
                FROM proposal_decisions AS d
                JOIN experience_proposals AS p ON p.uid = d.proposal_uid
                WHERE d.decision_id = %s AND d.proposal_uid = %s
                """,
                (ticket.decision_id, ticket.proposal_uid),
            ).fetchone()
            if not row or row[0] not in ('pass', 'merge') or row[1] != op.owner_id:
                raise TicketVerificationError('ticket_decision_unverified')
            return ticket.proposal_uid
        return None

    def _payload_sha(self, op: Op) -> str:
        canonical = {
            'action': op.action,
            'owner_id': op.owner_id,
            'actor_id': op.actor_id,
            'note_id': op.note_id,
            'category': op.category,
            'problem': op.problem,
            'solution': op.solution,
            'reusable_experience': op.reusable_experience,
            'expected_revision': op.expected_revision,
            'origin': op.origin,
            'kind': op.kind,
            'misbelief': op.misbelief,
            'scope': op.scope,
            'evidence': op.evidence,
            'importance': op.importance,
            'pinned': op.pinned,
            'status': op.status,
            'target_event_seq': op.target_event_seq,
        }
        if op.action == 'reconcile_state' or op.valid_from is not None or op.valid_until is not None:
            canonical['valid_from'] = op.valid_from
            canonical['valid_until'] = op.valid_until
        if op.expected_content_sha256 is not None:
            canonical['expected_content_sha256'] = op.expected_content_sha256
        return hashlib.sha256(_json_canon(canonical).encode('utf-8')).hexdigest()

    def apply(self, conn, ticket: AdmissionTicket | None, op: Op) -> dict[str, Any]:
        proposal_uid = self.verify_ticket(conn, ticket, op)
        if not op.owner_id or not op.actor_id or not op.request_id:
            raise ExperienceWriteError('invalid_experience_actor_or_request', 400)
        payload_sha = self._payload_sha(op)
        with conn.transaction():
            # Lock even a first request, whose absent receipt cannot be row-locked.
            conn.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))',
                         (_json_canon([op.owner_id, op.request_id]),))
            if op.action == 'import_manual':
                if not op.note_id:
                    raise ExperienceWriteError('invalid_experience_id', 400)
                existing_import = conn.execute(
                    f'SELECT {_SELECT_COLS} FROM experience_notes WHERE owner_id = %s AND id = %s FOR UPDATE',
                    (op.owner_id, op.note_id),
                ).fetchone()
                if existing_import is not None:
                    note_obj = _row_to_note(existing_import)
                    return {'ok': True, 'experience': note_obj, 'replayed': True, 'http_status': 200}
            prior = conn.execute(
                """
                SELECT actor_id, payload_sha256, response_json, http_status
                FROM experience_requests
                WHERE owner_id = %s AND request_id = %s
                FOR UPDATE
                """,
                (op.owner_id, op.request_id),
            ).fetchone()
            if prior is not None and op.action != 'import_manual':
                if prior[0] != op.actor_id or prior[1] != payload_sha:
                    raise ExperienceWriteError('experience_request_id_conflict', 409)
                resp = dict(prior[2])
                resp['replayed'] = True
                resp['http_status'] = prior[3]
                return resp

            now = datetime.now(timezone.utc)
            before_note: dict[str, Any] | None = None
            target_id = op.note_id
            vec = _vec_literal(op.embedding)
            projection = None
            clear_projection = False
            from evolvmem.experience_v2.writer_embeddings import prepare_projection, projection_changed, content_sha256

            if op.action in ('create', 'import_manual'):
                if op.category not in CATEGORIES:
                    raise ExperienceWriteError('invalid_experience_category', 400)
                if not op.problem or not op.solution or not op.reusable_experience:
                    raise ExperienceWriteError('invalid_experience_text', 400)
                if op.action == 'create':
                    target_id = target_id or ('manual-' + uuid.uuid4().hex if op.origin == 'manual' else _new_exp_id())
                elif not target_id:
                    raise ExperienceWriteError('invalid_experience_id', 400)
                projection = prepare_projection(conn, op, None, self.embedding_client)
                if projection is not None:
                    vec = _vec_literal(projection.legacy_embedding)
                rev = int(op.revision or 1)
                c_at = op.created_at or now
                u_at = op.updated_at or now
                conn.execute(
                    """
                    INSERT INTO experience_notes(
                        id, owner_id, category, problem, solution, reusable_experience,
                        origin, kind, misbelief, scope, evidence, occurrence_count,
                        importance, pinned, status, valid_from, valid_until, embedding, proposal_uid,
                        source_actor, source_device_id, source_agent, source_model,
                        revision, deleted_at, created_at, updated_at, retrieval_embedding,
                        retrieval_embedding_contract, retrieval_embedding_content_sha256, retrieval_embedding_updated_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s::jsonb, %s::jsonb, 1,
                        %s, %s, %s, %s, %s, %s::vector, %s,
                        %s, %s, %s, %s,
                        %s, %s, %s, %s, %s::vector, %s, %s, %s
                    )
                    """,
                    (
                        target_id, op.owner_id, op.category, op.problem.strip(),
                        op.solution.strip(), op.reusable_experience.strip(),
                        op.origin, op.kind, op.misbelief,
                        _json_canon(op.scope), _json_canon(op.evidence),
                        op.importance, op.pinned, op.status, op.valid_from, op.valid_until, vec, proposal_uid,
                        op.actor_id, op.source_device_id, op.agent_client, op.model,
                        rev, op.deleted_at, c_at, u_at,
                        _vec_literal(projection.retrieval_embedding) if projection else None,
                        projection.contract_id if projection else None, projection.content_sha256 if projection else None,
                        now if projection else None,
                    ),
                )
            else:
                if not target_id:
                    raise ExperienceWriteError('invalid_experience_id', 400)
                existing = conn.execute(
                    f'SELECT {_SELECT_COLS} FROM experience_notes WHERE owner_id = %s AND id = %s FOR UPDATE',
                    (op.owner_id, target_id),
                ).fetchone()
                if existing is None:
                    raise ExperienceWriteError('experience_not_found', 404)
                before_note = _row_to_note(existing)
                if op.action != 'rollback' and op.expected_revision is not None and before_note['revision'] != op.expected_revision:
                    raise ExperienceWriteError('experience_revision_conflict', 409)
                if op.action in ('update', 'delete', 'retire') and before_note['deleted_at'] is not None:
                    raise ExperienceWriteError('experience_state_conflict', 409)
                if op.action == 'restore' and before_note['deleted_at'] is None:
                    raise ExperienceWriteError('experience_state_conflict', 409)

                if op.action == 'reembed' and (op.expected_revision is None or not op.expected_content_sha256):
                    raise ExperienceWriteError('experience_reembed_precondition_required', 400)
                if op.expected_content_sha256 is not None and content_sha256(before_note) != op.expected_content_sha256:
                    raise ExperienceWriteError('experience_content_conflict', 409)
                projection = prepare_projection(conn, op, before_note, self.embedding_client)
                clear_projection = projection is None and (projection_changed(op, before_note) or op.embedding is not None)
                if projection is not None:
                    vec = _vec_literal(projection.legacy_embedding)

                projection_sql, projection_values = _projection_update(projection, clear_projection, vec, now)
                if op.action == 'update':
                    if op.category not in CATEGORIES:
                        raise ExperienceWriteError('invalid_experience_category', 400)
                    conn.execute(
                        f"""
                        UPDATE experience_notes
                        SET category = %s, problem = %s, solution = %s, reusable_experience = %s,
                            revision = revision + 1, updated_at = %s{projection_sql}
                        WHERE owner_id = %s AND id = %s
                        """,
                        (
                            op.category, (op.problem or '').strip(), (op.solution or '').strip(),
                            (op.reusable_experience or '').strip(), now, *projection_values, op.owner_id, target_id,
                        ),
                    )
                elif op.action in ('delete', 'restore'):
                    conn.execute(
                        f"""
                        UPDATE experience_notes
                        SET deleted_at = %s, revision = revision + 1, updated_at = %s{projection_sql}
                        WHERE owner_id = %s AND id = %s
                        """,
                        (now if op.action == 'delete' else None, now, *projection_values, op.owner_id, target_id),
                    )
                elif op.action == 'retire':
                    conn.execute(
                        """
                        UPDATE experience_notes
                        SET status = 'retired', deleted_at = %s, revision = revision + 1, updated_at = %s
                        WHERE owner_id = %s AND id = %s
                        """,
                        (now, now, op.owner_id, target_id),
                    )
                elif op.action == 'merge_evidence':
                    merged_ev = list(before_note['evidence']) + list(op.evidence)
                    conn.execute(
                        """
                        UPDATE experience_notes
                        SET evidence = %s::jsonb, occurrence_count = occurrence_count + 1,
                            revision = revision + 1, updated_at = %s
                        WHERE owner_id = %s AND id = %s
                        """,
                        (_json_canon(merged_ev), now, op.owner_id, target_id),
                    )
                elif op.action == 'reconcile_state':
                    conn.execute(
                        f"""
                        UPDATE experience_notes
                        SET status = COALESCE(%s, status),
                            scope = CASE WHEN %s THEN %s::jsonb ELSE scope END,
                            valid_from = COALESCE(%s, valid_from),
                            valid_until = COALESCE(%s, valid_until),
                            revision = revision + 1,
                            updated_at = %s{projection_sql}
                        WHERE owner_id = %s AND id = %s
                        """,
                        (
                            op.status,
                            bool(op.scope),
                            _json_canon(op.scope),
                            op.valid_from,
                            op.valid_until,
                            now,
                            *projection_values,
                            op.owner_id,
                            target_id,
                        ),
                    )
                elif op.action == 'reembed':
                    # Vector-only repair preserves the authored revision and timestamps.
                    conn.execute(f'UPDATE experience_notes SET id = id{projection_sql} WHERE owner_id = %s AND id = %s',
                                 (*projection_values, op.owner_id, target_id))
                elif op.action == 'rollback':
                    snap = op.restore_snapshot
                    if snap is None:
                        conn.execute(
                            """
                            UPDATE experience_notes
                            SET status = 'retired', deleted_at = COALESCE(deleted_at, %s),
                                revision = revision + 1, updated_at = %s
                            WHERE owner_id = %s AND id = %s
                            """,
                            (now, now, op.owner_id, target_id),
                        )
                    else:
                        conn.execute(
                            f"""
                            UPDATE experience_notes
                            SET category = %s, problem = %s, solution = %s, reusable_experience = %s,
                                evidence = %s::jsonb, occurrence_count = %s, status = %s,
                                deleted_at = %s, revision = revision + 1, updated_at = %s{projection_sql}
                            WHERE owner_id = %s AND id = %s
                            """,
                            (
                                snap['category'], snap['problem'], snap['solution'], snap['reusable_experience'],
                                _json_canon(snap.get('evidence', [])), int(snap.get('occurrence_count', 1)),
                                snap.get('status', 'active'), snap.get('deleted_at'), now, *projection_values, op.owner_id, target_id,
                            ),
                        )
                else:
                    raise ExperienceWriteError('invalid_experience_action', 400)

            after_row = conn.execute(
                f'SELECT {_SELECT_COLS} FROM experience_notes WHERE owner_id = %s AND id = %s',
                (op.owner_id, target_id),
            ).fetchone()
            after_note = _row_to_note(after_row)
            status_code = 201 if op.action == 'create' else 200
            response = {'ok': True, 'experience': after_note, 'replayed': False}

            if op.action == 'import_manual' and isinstance(op.restore_snapshot, dict):
                hist_events = op.restore_snapshot.get('historical_events') or []
                hist_reqs = op.restore_snapshot.get('historical_requests') or []
                if hist_events:
                    for ev in hist_events:
                        conn.execute(
                            """
                            INSERT INTO experience_events(
                                event_id, owner_id, actor_id, request_id, action, note_id,
                                before_json, after_json, agent_client, model, created_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, '', '', %s)
                            ON CONFLICT (event_id) DO NOTHING
                            """,
                            (
                                ev['event_id'], ev['owner_id'], ev['actor_id'], ev['request_id'],
                                ev['action'], ev['note_id'], ev.get('before_json'), ev['after_json'],
                                ev['created_at'],
                            ),
                        )
                else:
                    conn.execute(
                        """
                        INSERT INTO experience_events(
                            event_id, owner_id, actor_id, request_id, action, note_id,
                            before_json, after_json, proposal_uid, agent_client, model, created_at
                        ) VALUES (%s, %s, %s, %s, 'import_manual', %s, NULL, %s::jsonb, NULL, '', '', %s)
                        """,
                        (uuid.uuid4().hex, op.owner_id, op.actor_id, op.request_id, target_id, _json_canon(after_note), op.updated_at or now),
                    )
                for req in hist_reqs:
                    conn.execute(
                        """
                        INSERT INTO experience_requests(
                            owner_id, request_id, actor_id, payload_sha256, response_json, http_status, created_at
                        ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                        ON CONFLICT (owner_id, request_id) DO NOTHING
                        """,
                        (
                            req['owner_id'], req['request_id'], req['actor_id'],
                            req['payload_sha256'], req['response_json'], int(req['http_status']), req['created_at'],
                        ),
                    )
                return {**response, 'http_status': status_code}

            ev_row = conn.execute(
                """
                INSERT INTO experience_events(
                    event_id, owner_id, actor_id, request_id, action, note_id,
                    before_json, after_json, proposal_uid, agent_client, model, rolled_back_by_seq, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, NULL, %s)
                RETURNING seq
                """,
                (
                    uuid.uuid4().hex, op.owner_id, op.actor_id, op.request_id, op.action, target_id,
                    _json_canon(before_note) if before_note is not None else None,
                    _json_canon(after_note), proposal_uid, op.agent_client, op.model,
                    op.updated_at or now,
                ),
            ).fetchone()
            if op.action == 'rollback' and op.target_event_seq is not None and ev_row:
                conn.execute(
                    'UPDATE experience_events SET rolled_back_by_seq = %s WHERE owner_id = %s AND seq = %s',
                    (int(ev_row[0]), op.owner_id, int(op.target_event_seq)),
                )
            conn.execute(
                """
                INSERT INTO experience_requests(
                    owner_id, request_id, actor_id, payload_sha256, response_json, http_status, created_at
                ) VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)
                """,
                (op.owner_id, op.request_id, op.actor_id, payload_sha, _json_canon(response), status_code, now),
            )
            return {**response, 'http_status': status_code}


def _extract_ast_strings(node: ast.AST) -> list[str]:
    strings: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            strings.append(child.value)
        elif isinstance(child, ast.JoinedStr):
            parts = [
                v.value if isinstance(v, ast.Constant) and isinstance(v.value, str) else '{expr}'
                for v in child.values
            ]
            strings.append(''.join(parts))
    return strings


def scan_unauthorized_experience_notes_writes(repo_root: Path) -> list[str]:
    """AST-scan evolvmem/ and scripts/ to guarantee only writer.py writes experience_notes."""
    root = Path(repo_root).resolve()
    allowed_writer = (root / 'evolvmem' / 'experience_v2' / 'writer.py').resolve()
    migrations_dir = (root / 'evolvmem' / 'experience_v2' / 'migrations').resolve()
    violations: list[str] = []

    for sub in ('evolvmem', 'scripts'):
        base = root / sub
        if not base.exists():
            continue
        for path in sorted(base.rglob('*')):
            if not path.is_file() or any(part in ('__pycache__', '.venv') for part in path.parts):
                continue
            resolved = path.resolve()
            if resolved == allowed_writer or (resolved.suffix == '.sql' and resolved.parent == migrations_dir):
                continue
            if resolved.suffix == '.py':
                tree = ast.parse(resolved.read_text(encoding='utf-8'), filename=str(resolved))
                for text in _extract_ast_strings(tree):
                    if _WRITE_SQL_RE.search(text):
                        violations.append(str(resolved.relative_to(root)))
                        break
            elif resolved.suffix == '.sql':
                if _WRITE_SQL_RE.search(resolved.read_text(encoding='utf-8')):
                    violations.append(str(resolved.relative_to(root)))
    return violations

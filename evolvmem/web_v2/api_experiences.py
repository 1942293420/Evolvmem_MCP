"""Web Console v2 Experience Library (M3) & Model Rollback endpoints."""
from __future__ import annotations

from typing import Any
import uuid

from evolvmem.experience_v2.gate.propose import VALID_KINDS
from evolvmem.experience_v2.gate.rollback import (
    ModelRollbackService,
    RollbackPlan,
    RollbackPlanRequiredError,
)
from evolvmem.experience_v2.interception_store import (
    fetch_proposal_gate_chain,
    query_interception_funnel,
)
from evolvmem.experience_v2.writer import (
    CATEGORIES,
    ExperienceWriteError,
    ExperienceWriter,
    Op,
    TicketIssuer,
)
from evolvmem.web_v2.common import (
    V2ApiError,
    clamp_int,
    iso,
    jval,
    open_exp_pg,
    parse_iso_dt,
    resolve_actor_and_library,
)


def _format_note_row(row: tuple[Any, ...]) -> dict[str, Any]:
    (
        nid, category, kind, problem, solution, reusable, misbelief,
        scope, evidence, status, origin, src_agent, src_model,
        occ, imp, pinned, rev, valid_from, valid_until,
        last_used, created_at, updated_at,
    ) = row
    ev_list = jval(evidence, [])
    return {
        'id': nid,
        'category': category,
        'kind': kind or 'pitfall',
        'problem': problem,
        'solution': solution,
        'reusable_experience': reusable,
        'misbelief': misbelief or '',
        'scope': jval(scope, {}),
        'evidence': ev_list,
        'evidence_count': len(ev_list) if isinstance(ev_list, list) else 0,
        'status': status,
        'origin': origin,
        'source_agent': src_agent or '',
        'source_model': src_model or '',
        'occurrence_count': int(occ or 0),
        'importance': int(imp or 5),
        'pinned': bool(pinned),
        'revision': int(rev or 1),
        'valid_from': iso(valid_from),
        'valid_until': iso(valid_until),
        'last_used_at': iso(last_used),
        'created_at': iso(created_at),
        'updated_at': iso(updated_at),
    }


def _note_counts(conn, owner_id: str, *, is_owner: bool) -> tuple[dict[str, int], dict[str, int]]:
    status_counts = {'active': 0, 'challenger': 0, 'dormant': 0, 'contested': 0, 'retired': 0}
    cat_counts: dict[str, int] = {cat: 0 for cat in CATEGORIES}
    if is_owner:
        for st, cnt in conn.execute(
            """
            SELECT status, COUNT(*) FROM experience_notes
            WHERE owner_id = %s AND (deleted_at IS NULL OR status = 'retired')
            GROUP BY status
            """,
            (owner_id,),
        ).fetchall():
            status_counts[str(st)] = int(cnt)
        cat_rows = conn.execute(
            """
            SELECT category, COUNT(*) FROM experience_notes
            WHERE owner_id = %s AND deleted_at IS NULL
            GROUP BY category
            """,
            (owner_id,),
        ).fetchall()
    else:
        act_row = conn.execute(
            """
            SELECT COUNT(*) FROM experience_notes
            WHERE owner_id = %s AND deleted_at IS NULL AND status = 'active'
            """,
            (owner_id,),
        ).fetchone()
        status_counts['active'] = int(act_row[0]) if act_row else 0
        cat_rows = conn.execute(
            """
            SELECT category, COUNT(*) FROM experience_notes
            WHERE owner_id = %s AND deleted_at IS NULL AND status = 'active'
            GROUP BY category
            """,
            (owner_id,),
        ).fetchall()
    for cat, cnt in cat_rows:
        cat_counts[str(cat)] = int(cnt)
    return status_counts, cat_counts


def list_experiences(console: Any, session: Any, params: dict[str, Any]) -> dict[str, Any]:
    target = params.get('owner_id')
    inspect = str(params.get('inspect') or '') == '1'
    _actor, owner_id, is_owner, _lib, _me = resolve_actor_and_library(
        console, session, target, need_experience=True, inspect=inspect,
    )
    raw_status = str(params.get('status') or 'active').strip().lower()
    if not is_owner:
        if raw_status not in ('active', ''):
            raise V2ApiError('peer_status_forbidden', 403)
        raw_status = 'active'

    category = str(params.get('category') or '').strip()
    kind = str(params.get('kind') or '').strip()
    origin = str(params.get('origin') or '').strip()
    if not isinstance(params.get('q', ''), str):
        raise V2ApiError('invalid_recall_query', 400)
    query = params.get('q', '').strip()
    limit = clamp_int(params.get('limit'), 60, 1, 200)
    offset = clamp_int(params.get('offset'), 0, 0, 5000)

    clauses = ['owner_id = %s']
    args: list[Any] = [owner_id]
    if not is_owner:
        clauses.append("deleted_at IS NULL AND status = 'active'")
    elif raw_status == 'retired':
        clauses.append("status = 'retired'")
    elif raw_status and raw_status != 'all':
        clauses.append('deleted_at IS NULL AND status = %s')
        args.append(raw_status)
    else:
        clauses.append("(deleted_at IS NULL OR status = 'retired')")
    if category and category != 'all':
        clauses.append('category = %s')
        args.append(category)
    if kind and kind != 'all':
        clauses.append('kind = %s')
        args.append(kind)
    if origin and origin != 'all':
        clauses.append('origin = %s')
        args.append(origin)

    where_sql = ' AND '.join(clauses)
    role = 'coordinator' if is_owner else 'reader'
    with open_exp_pg(role=role) as (_settings, conn):
        searched = None
        if query:
            from evolvmem.web_v2.experience_search import search_notes
            searched = search_notes(conn, owner_id=owner_id, params=params,
                status=raw_status or 'all', category=category if category != 'all' else '',
                origin=origin if origin != 'all' else '', kind=kind if kind != 'all' else '',
                limit=limit, offset=offset)
            columns = ('id category kind problem solution reusable_experience misbelief scope evidence '
                       'status origin source_agent source_model occurrence_count importance pinned revision '
                       'valid_from valid_until last_used_at created_at updated_at').split()
            items = []
            for note in searched['items']:
                item = _format_note_row(tuple(note.get(k) for k in columns))
                item.update({k: note[k] for k in ('match_types', 'cosine', 'keyword_score', 'score')})
                items.append(item)
            total = searched['total']
        else:
            total_row = conn.execute(f'SELECT COUNT(*) FROM experience_notes WHERE {where_sql}', args).fetchone()
            rows = conn.execute(
                f"""
                SELECT id, category, kind, problem, solution, reusable_experience, misbelief,
                       scope, evidence, status, origin, source_agent, source_model,
                       occurrence_count, importance, pinned, revision, valid_from, valid_until,
                       last_used_at, created_at, updated_at
                FROM experience_notes
                WHERE {where_sql}
                ORDER BY pinned DESC, updated_at DESC, id ASC
                LIMIT %s OFFSET %s
                """,
                [*args, limit, offset],
            ).fetchall()
            total = int(total_row[0]) if total_row else 0
            items = [_format_note_row(r) for r in rows]
        status_counts, cat_counts = _note_counts(conn, owner_id, is_owner=is_owner)
        funnel = query_interception_funnel(conn, owner_id=owner_id) if is_owner else None

    return {
        'ok': True,
        'owner_id': owner_id,
        'is_owner': is_owner,
        'total': total,
        'status_counts': status_counts,
        'category_counts': cat_counts,
        'interception_funnel': funnel,
        'items': items,
        **({'diagnostics': searched['diagnostics']} if searched else {}),
    }


def get_experience_trace(console: Any, session: Any, note_id: str, params: dict[str, Any]) -> dict[str, Any]:
    target = params.get('owner_id')
    inspect = str(params.get('inspect') or '') == '1'
    _actor, owner_id, is_owner, _lib, _me = resolve_actor_and_library(
        console, session, target, need_experience=True, inspect=inspect,
    )
    role = 'coordinator' if is_owner else 'reader'
    extra_filter = " AND status = 'active' AND deleted_at IS NULL" if not is_owner else ''
    with open_exp_pg(role=role) as (_settings, conn):
        row = conn.execute(
            f"""
            SELECT id, category, kind, problem, solution, reusable_experience, misbelief,
                   scope, evidence, status, origin, source_agent, source_model,
                   occurrence_count, importance, pinned, revision, valid_from, valid_until,
                   last_used_at, created_at, updated_at
            FROM experience_notes
            WHERE owner_id = %s AND id = %s{extra_filter}
            """,
            (owner_id, note_id),
        ).fetchone()
        if not row:
            raise V2ApiError('experience_not_found', 404)
        note = _format_note_row(row)

        rel_rows = conn.execute(
            """
            SELECT relation_id, from_uid, to_uid, relation, proposal_uid, reason, created_at
            FROM experience_relations
            WHERE owner_id = %s AND (from_uid = %s OR to_uid = %s)
            ORDER BY relation_id DESC LIMIT 30
            """,
            (owner_id, note_id, note_id),
        ).fetchall()
        relations = [
            {
                'relation_id': int(r[0]), 'from_uid': r[1], 'to_uid': r[2],
                'relation': r[3], 'proposal_uid': r[4],
                'confidence': None,
                'reason': r[5] or '', 'created_at': iso(r[6]),
            }
            for r in rel_rows
        ]
        if not is_owner:
            return {
                'ok': True, 'owner_id': owner_id, 'is_owner': False,
                'note': note, 'relations': relations,
                'events': [], 'trials': [], 'gate_chain': None,
            }

        ev_rows = conn.execute(
            """
            SELECT seq, event_id, action, actor_id, agent_client, model,
                   proposal_uid, request_id, rolled_back_by_seq, created_at
            FROM experience_events
            WHERE owner_id = %s AND note_id = %s
            ORDER BY seq DESC LIMIT 50
            """,
            (owner_id, note_id),
        ).fetchall()
        events = [
            {
                'seq': int(e[0]), 'event_id': e[1], 'action': e[2], 'actor_id': e[3],
                'agent_client': e[4] or '', 'model': e[5] or '',
                'proposal_uid': e[6], 'decision_id': None, 'request_id': e[7],
                'rolled_back_by_seq': e[8], 'created_at': iso(e[9]),
            }
            for e in ev_rows
        ]
        trial_rows = conn.execute(
            """
            SELECT id, challenger_note_id, incumbent_note_id, relation, proposal_uid,
                   required_successes, success_count, failure_count, status, created_at, updated_at
            FROM challenger_trials
            WHERE owner_id = %s AND (challenger_note_id = %s OR incumbent_note_id = %s)
            ORDER BY id DESC LIMIT 10
            """,
            (owner_id, note_id, note_id),
        ).fetchall()
        trials = [
            {
                'trial_id': int(t[0]), 'challenger_note_id': t[1], 'incumbent_note_id': t[2],
                'relation': t[3], 'proposal_uid': t[4], 'required_successes': int(t[5]),
                'success_count': int(t[6]), 'failure_count': int(t[7]),
                'status': t[8], 'created_at': iso(t[9]), 'updated_at': iso(t[10]),
            }
            for t in trial_rows
        ]
        prop_uid = next((e['proposal_uid'] for e in reversed(events) if e['proposal_uid']), None)
        if not prop_uid and trials:
            prop_uid = trials[0].get('proposal_uid')
        gate_chain = (
            fetch_proposal_gate_chain(conn, prop_uid, problem=note['problem'], scope=note['scope'])
            if prop_uid else None
        )

    return {
        'ok': True, 'owner_id': owner_id, 'is_owner': True,
        'note': note, 'relations': relations,
        'events': events, 'trials': trials, 'gate_chain': gate_chain,
    }


def write_experience(console: Any, session: Any, body: dict[str, Any]) -> dict[str, Any]:
    actor, owner_id, _is_owner, _lib, _me = resolve_actor_and_library(
        console, session, body.get('owner_id'), need_experience=True, require_write=True,
    )
    action = str(body.get('action') or '').strip().lower()
    if action not in ('create', 'update', 'retire', 'delete'):
        raise V2ApiError('invalid_experience_action', 400)
    req_id = str(body.get('request_id') or f'v2-manual-{uuid.uuid4().hex[:16]}').strip()
    note_id = str(body.get('note_id') or body.get('id') or '').strip() or None
    if action != 'create' and not note_id:
        raise V2ApiError('note_id_required', 400)
    expected_revision = body.get('expected_revision')
    if action == 'update' and expected_revision is None:
        raise V2ApiError('expected_revision_required', 400)
    if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 1):
        raise V2ApiError('invalid_expected_revision', 400)

    cat = str(body.get('category') or '系统环境经验')
    kind = str(body.get('kind') or 'pitfall')
    if action in ('create', 'update'):
        if cat not in CATEGORIES:
            raise V2ApiError('invalid_category', 400)
        if kind not in VALID_KINDS:
            raise V2ApiError('invalid_kind', 400)
        if not (str(body.get('problem') or '').strip() and str(body.get('solution') or '').strip()
                and str(body.get('reusable_experience') or '').strip()):
            raise V2ApiError('empty_experience_fields', 400)

    with open_exp_pg(role='coordinator') as (settings, conn):
        issuer = TicketIssuer(settings.ticket_secret, gate_id='manual_console_v2')
        writer = ExperienceWriter(settings.ticket_secret)
        ticket = issuer.issue(
            proposal_uid=f'manual:{req_id}',
            decision_id=f'user_fast:{uuid.uuid4().hex[:16]}',
            lane='user_fast',
        )
        op = Op(
            action=action,
            owner_id=owner_id,
            actor_id=str(actor['id']),
            request_id=req_id,
            note_id=note_id,
            category=cat if action in ('create', 'update') else None,
            problem=str(body.get('problem') or '').strip() if action in ('create', 'update') else None,
            solution=str(body.get('solution') or '').strip() if action in ('create', 'update') else None,
            reusable_experience=str(body.get('reusable_experience') or '').strip() if action in ('create', 'update') else None,
            expected_revision=expected_revision,
            origin='manual',
            kind=kind if action in ('create', 'update') else None,
            misbelief=str(body.get('misbelief') or '').strip(),
            scope=body.get('scope') if isinstance(body.get('scope'), dict) else {},
            evidence=body.get('evidence') if isinstance(body.get('evidence'), list) else [],
            importance=clamp_int(body.get('importance'), 5, 1, 10),
            pinned=bool(body.get('pinned', False)),
        )
        try:
            applied = writer.apply(conn, ticket, op)
        except ExperienceWriteError as exc:
            raise V2ApiError(exc.code, exc.status) from None

    return {
        'ok': True,
        'note': applied.get('experience'),
        'replayed': bool(applied.get('replayed', False)),
        'http_status': int(applied.get('http_status') or (201 if action == 'create' else 200)),
    }


def handle_model_rollback(console: Any, session: Any, body: dict[str, Any]) -> dict[str, Any]:
    actor, owner_id, _is_owner, _lib, _me = resolve_actor_and_library(
        console, session, body.get('owner_id'), need_experience=True, require_write=True,
    )
    mode = str(body.get('mode') or 'dry_run').strip().lower()
    if mode not in ('dry_run', 'execute'):
        raise V2ApiError('invalid_rollback_mode', 400)

    with open_exp_pg(role='coordinator') as (settings, conn):
        svc = ModelRollbackService(ticket_secret=settings.ticket_secret)
        try:
            if mode == 'dry_run':
                plan = svc.plan_model_rollback(
                    conn,
                    owner_id=owner_id,
                    agent_client=str(body.get('agent_client') or '').strip(),
                    model=str(body.get('model') or '').strip(),
                    since=parse_iso_dt(body.get('since')),
                    until=parse_iso_dt(body.get('until')),
                )
                return {
                    'ok': True,
                    'mode': 'dry_run',
                    'plan': {
                        'plan_id': plan.plan_id,
                        'owner_id': plan.owner_id,
                        'agent_client': plan.agent_client,
                        'model': plan.model,
                        'since_iso': plan.since_iso,
                        'until_iso': plan.until_iso,
                        'event_seqs': list(plan.event_seqs),
                        'operations': list(plan.operations),
                        'pre_state_digest': plan.pre_state_digest,
                        'signature': plan.signature,
                    },
                }

            raw_plan = body.get('plan')
            if not isinstance(raw_plan, dict):
                raise V2ApiError('dry_run_plan_required', 400)
            req_id = str(body.get('request_id') or f'rb-{uuid.uuid4().hex[:16]}').strip()
            plan = RollbackPlan(
                plan_id=str(raw_plan.get('plan_id') or ''),
                owner_id=owner_id,
                agent_client=str(raw_plan.get('agent_client') or ''),
                model=str(raw_plan.get('model') or ''),
                since_iso=raw_plan.get('since_iso'),
                until_iso=raw_plan.get('until_iso'),
                event_seqs=tuple(int(s) for s in (raw_plan.get('event_seqs') or ())),
                operations=tuple(dict(op) for op in (raw_plan.get('operations') or ())),
                pre_state_digest=str(raw_plan.get('pre_state_digest') or ''),
                signature=str(raw_plan.get('signature') or ''),
            )
            res = svc.execute_model_rollback(conn, plan=plan, actor_id=str(actor['id']), request_id=req_id)
            return {**res, 'mode': 'execute'}
        except RollbackPlanRequiredError as exc:
            raise V2ApiError(exc.code, exc.status) from None
        except ExperienceWriteError as exc:
            raise V2ApiError(exc.code, exc.status) from None

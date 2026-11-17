"""Web Console v2 Interception Trace, Rescue, Review Inbox (L4), and Evidence Context endpoints."""
from __future__ import annotations

import re
from typing import Any

from evolvmem.experience_v2.gate.review_inbox import (
    ReviewInboxError,
    ReviewInboxService,
    VALID_TRACKS,
)
from evolvmem.experience_v2.interception_rescue import rescue_intercepted_item
from evolvmem.experience_v2.interception_store import (
    fetch_proposal_gate_chain,
    get_interception_trace,
    list_intercepted_items,
)
from evolvmem.history_pg.history_web_pg import dispatch_pg_history_api
from evolvmem.web_v2.common import (
    V2ApiError,
    clamp_int,
    iso,
    jval,
    open_exp_pg,
    parse_bool_flag,
    resolve_actor_and_library,
    resolve_shadow_mode,
)

_BLOCK_REF_RE = re.compile(r'(?:archive|block|blk)[:/#]?([A-Za-z0-9._-]+)')


def _require_owner_interceptions(is_owner: bool, code: str) -> None:
    if not is_owner:
        raise V2ApiError(code, 403)


def _track_agreement_stats(conn, owner_id: str) -> dict[str, dict[str, Any]]:
    stats: dict[str, dict[str, Any]] = {}
    for tr in ('live', 'backfill'):
        rows = conn.execute(
            """
            SELECT gate_verdict, user_verdict
            FROM review_decisions
            WHERE owner_id = %s AND source_track = %s
            ORDER BY created_at DESC LIMIT 200
            """,
            (owner_id, tr),
        ).fetchall()
        reviewed = len(rows)
        evaluable = 0
        agreed = 0
        for g_verdict, u_verdict in rows:
            if not g_verdict:
                continue
            evaluable += 1
            gate_admits = g_verdict in ('pass', 'merge')
            human_admits = u_verdict in ('accept', 'rescue', 'edit_accept')
            if gate_admits == human_admits:
                agreed += 1
        stats[tr] = {
            'reviewed_total': reviewed,
            'evaluable_total': evaluable,
            'agreed_total': agreed,
            'agreement_rate': round(agreed / evaluable, 4) if evaluable else 0.0,
        }
    return stats


def _review_map(conn, owner_id: str, uids: list[str]) -> dict[str, dict[str, Any]]:
    if not uids:
        return {}
    rows = conn.execute(
        """
        SELECT proposal_uid, review_id, reviewer_id, gate_verdict, gate_confidence,
               gate_reason, user_verdict, user_reason, note, edited_payload, created_at
        FROM review_decisions
        WHERE owner_id = %s AND proposal_uid = ANY(%s)
        """,
        (owner_id, uids),
    ).fetchall()
    return {
        r[0]: {
            'review_id': r[1],
            'reviewer_id': r[2],
            'gate_verdict': r[3],
            'gate_confidence': float(r[4]) if r[4] is not None else None,
            'gate_reason': r[5],
            'user_verdict': r[6],
            'user_reason': r[7],
            'note': r[8],
            'edited_payload': jval(r[9], None),
            'created_at': iso(r[10]),
        }
        for r in rows
    }


def list_interceptions(console: Any, session: Any, params: dict[str, Any]) -> dict[str, Any]:
    inspect = str(params.get('inspect') or '') == '1'
    _actor, owner_id, is_owner, _lib, _me = resolve_actor_and_library(
        console, session, params.get('owner_id'), need_experience=True, inspect=inspect,
    )
    _require_owner_interceptions(is_owner, 'owner_only_interceptions')

    layer = str(params.get('layer') or 'all').strip() or 'all'
    track = str(params.get('track') or 'all').strip() or 'all'
    category = str(params.get('category') or 'all').strip() or 'all'
    query = str(params.get('q') or '').strip()
    limit = clamp_int(params.get('limit'), 80, 1, 200)

    with open_exp_pg(role='coordinator') as (_settings, conn):
        res = list_intercepted_items(
            conn, owner_id=owner_id, layer=layer, track=track,
            category=category, q=query, limit=limit,
        )
    return {**res, 'owner_id': owner_id}


def trace_interception(console: Any, session: Any, item_id: str, params: dict[str, Any]) -> dict[str, Any]:
    inspect = str(params.get('inspect') or '') == '1'
    _actor, owner_id, is_owner, _lib, _me = resolve_actor_and_library(
        console, session, params.get('owner_id'), need_experience=True, inspect=inspect,
    )
    _require_owner_interceptions(is_owner, 'owner_only_interceptions')

    with open_exp_pg(role='coordinator') as (_settings, conn):
        item = get_interception_trace(conn, owner_id=owner_id, item_id=item_id)
    if item is None:
        raise V2ApiError('interception_not_found', 404)
    return {'ok': True, 'owner_id': owner_id, 'item': item}


def rescue_interception(console: Any, session: Any, body: dict[str, Any]) -> dict[str, Any]:
    actor, owner_id, _is_owner, _lib, _me = resolve_actor_and_library(
        console, session, body.get('owner_id'), need_experience=True, require_write=True,
    )
    item_id = str(body.get('item_id') or '').strip()
    note = str(body.get('note') or '').strip()
    edited_payload = body.get('edited_payload')
    if edited_payload is not None and not isinstance(edited_payload, dict):
        raise V2ApiError('invalid_edited_payload', 400)

    with open_exp_pg(role='coordinator') as (settings, conn):
        try:
            res = rescue_intercepted_item(
                conn,
                owner_id=owner_id,
                reviewer_id=str(actor['id']),
                item_id=item_id,
                edited_payload=edited_payload,
                note=note,
                ticket_secret=settings.ticket_secret,
            )
        except ReviewInboxError as exc:
            raise V2ApiError(exc.code, exc.status) from None
    return res


def get_review_inbox(console: Any, policy: Any, session: Any, params: dict[str, Any]) -> dict[str, Any]:
    inspect = str(params.get('inspect') or '') == '1'
    _actor, owner_id, is_owner, _lib, _me = resolve_actor_and_library(
        console, session, params.get('owner_id'), need_experience=True, inspect=inspect,
    )
    _require_owner_interceptions(is_owner, 'owner_only_review_inbox')

    source_track = str(params.get('source_track') or 'live').strip() or 'live'
    if source_track not in VALID_TRACKS:
        raise V2ApiError('invalid_source_track', 400)
    include_decided = parse_bool_flag(params.get('include_decided'), False)
    limit = clamp_int(params.get('limit'), 50, 1, 200)
    shadow = resolve_shadow_mode(policy)

    with open_exp_pg(role='coordinator') as (settings, conn):
        svc = ReviewInboxService(ticket_secret=settings.ticket_secret, shadow_mode=shadow)
        try:
            res = svc.list_inbox(
                conn,
                owner_id=owner_id,
                source_track=source_track,
                include_decided=include_decided,
                limit=limit,
            )
        except ReviewInboxError as exc:
            raise V2ApiError(exc.code, exc.status) from None

        uids = [str(it['uid']) for it in res.get('items', [])]
        reviews = _review_map(conn, owner_id, uids)
        for item in res.get('items', []):
            rev = reviews.get(item['uid'])
            item['review'] = rev
            if shadow and rev is None:
                item['gate_verdict'] = None
                item['gate_confidence'] = None
                item['gate_reason'] = None
                item['gate_chain'] = None
            else:
                chain = fetch_proposal_gate_chain(
                    conn,
                    item['uid'],
                    problem=str(item.get('problem') or ''),
                    scope=(item.get('scope') if isinstance(item.get('scope'), dict) else {}),
                )
                item['gate_chain'] = chain['decisions']
                item['llm_calls'] = chain['llm_calls']
                if rev is not None:
                    item['gate_verdict'] = rev['gate_verdict']
                    item['gate_confidence'] = rev['gate_confidence']
                    item['gate_reason'] = rev['gate_reason']
        res['track_agreement'] = _track_agreement_stats(conn, owner_id)
        res['owner_id'] = owner_id
    return res


def post_review_decide(console: Any, policy: Any, session: Any, body: dict[str, Any], *, batch: bool = False) -> dict[str, Any]:
    actor, owner_id, _is_owner, _lib, _me = resolve_actor_and_library(
        console, session, body.get('owner_id'), need_experience=True, require_write=True,
    )
    shadow = resolve_shadow_mode(policy)
    with open_exp_pg(role='coordinator') as (settings, conn):
        svc = ReviewInboxService(ticket_secret=settings.ticket_secret, shadow_mode=shadow)
        try:
            if batch:
                return svc.batch_decide(
                    conn,
                    owner_id=owner_id,
                    reviewer_id=str(actor['id']),
                    proposal_uids=body.get('proposal_uids') or [],
                    user_verdict=str(body.get('user_verdict') or ''),
                    user_reason=(str(body['user_reason']) if body.get('user_reason') else None),
                    note=str(body.get('note') or ''),
                )
            return svc.decide(
                conn,
                owner_id=owner_id,
                reviewer_id=str(actor['id']),
                proposal_uid=str(body.get('proposal_uid') or ''),
                user_verdict=str(body.get('user_verdict') or ''),
                user_reason=(str(body['user_reason']) if body.get('user_reason') else None),
                note=str(body.get('note') or ''),
                edited_payload=body.get('edited_payload'),
            )
        except ReviewInboxError as exc:
            raise V2ApiError(exc.code, exc.status) from None


def get_evidence_context(console: Any, session: Any, params: dict[str, Any]) -> dict[str, Any]:
    _actor, owner_id, is_owner, _lib, _me = resolve_actor_and_library(
        console, session, params.get('owner_id'), need_history=True,
    )
    ref = str(params.get('ref') or '').strip()
    block_id = str(params.get('block_id') or '').strip()
    if not block_id and ref:
        m = _BLOCK_REF_RE.search(ref)
        if m:
            block_id = m.group(1)

    if not block_id or not block_id.isdigit():
        return {
            'ok': True, 'owner_id': owner_id, 'ref': ref, 'block_id': block_id or None,
            'resolved': False, 'turns': [], 'detail': '非数值历史块引用，已展示证据原文摘录',
        }

    try:
        status, payload = dispatch_pg_history_api(
            'GET',
            f'/api/history-candidate/block/{block_id}/turns',
            {'limit': '30', 'offset': '0'},
            {},
            environ={'EVOLVMEM_CONSOLE_OWNER_ID': owner_id, 'EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE': owner_id},
            owner_verified=is_owner,
        )
        if status == 200 and isinstance(payload, dict):
            return {
                'ok': True, 'owner_id': owner_id, 'ref': ref, 'block_id': block_id,
                'resolved': True, 'turns': payload.get('turns') or payload.get('items') or [],
                'block': payload.get('block'),
            }
        return {
            'ok': True, 'owner_id': owner_id, 'ref': ref, 'block_id': block_id,
            'resolved': False, 'turns': [], 'detail': str(payload.get('error') or 'history_block_unavailable'),
        }
    except Exception:
        return {
            'ok': True, 'owner_id': owner_id, 'ref': ref, 'block_id': block_id,
            'resolved': False, 'turns': [], 'detail': 'history_pg_unconfigured',
        }

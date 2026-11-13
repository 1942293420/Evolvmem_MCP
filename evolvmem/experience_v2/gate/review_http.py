"""HTTP adapter mounting Review Inbox endpoints onto Knowledge Console API."""
from __future__ import annotations

from typing import Any

from evolvmem.experience_v2.gate.review_inbox import ReviewInboxError, ReviewInboxService
from evolvmem.experience_v2.pg_experience_store import (
    PgExperienceUnavailableError,
    load_pg_experience_settings,
    open_experience_conn,
)


def _err(code: str, status: int) -> tuple[int, str, dict[str, Any]]:
    return status, 'application/json', {'ok': False, 'error': code}


def dispatch_review_inbox(
    console: Any,
    policy: Any,
    method: str,
    rel_path: str,
    params_or_body: dict[str, Any],
    session: Any,
) -> tuple[int, str, dict[str, Any]]:
    """Handle /api/knowledge-console/review-inbox* routes with fail-closed PG enforcement."""
    status, _, me = console.dispatch('GET', '/me', {}, session)
    if status != 200 or not isinstance(me, dict) or not me.get('current_user'):
        code = me.get('error', 'login_required') if isinstance(me, dict) else 'login_required'
        return _err(code, status if status >= 400 else 401)
    actor = me['current_user']
    owner_id = str(actor['id'])

    if not getattr(policy, 'experience_v2_enabled', False):
        return _err('experience_v2_disabled', 404)

    try:
        settings = load_pg_experience_settings()
        svc = ReviewInboxService(
            ticket_secret=settings.ticket_secret,
            shadow_mode=bool(getattr(policy, 'shadow_mode', True)),
        )
        with open_experience_conn(settings, role='coordinator') as conn:
            path = rel_path.rstrip('/') or '/'
            if method == 'GET' and path == '/review-inbox':
                track = str(params_or_body.get('source_track') or 'live')
                include_decided = str(params_or_body.get('include_decided') or '0') in ('1', 'true', 'True')
                raw_limit = params_or_body.get('limit') or 50
                res = svc.list_inbox(
                    conn,
                    owner_id=owner_id,
                    source_track=track,
                    include_decided=include_decided,
                    limit=int(raw_limit),
                )
                return 200, 'application/json', res

            if method == 'POST' and path == '/review-inbox/decide':
                res = svc.decide(
                    conn,
                    owner_id=owner_id,
                    reviewer_id=owner_id,
                    proposal_uid=str(params_or_body.get('proposal_uid') or ''),
                    user_verdict=str(params_or_body.get('user_verdict') or ''),
                    user_reason=(str(params_or_body['user_reason']) if params_or_body.get('user_reason') else None),
                    note=str(params_or_body.get('note') or ''),
                    edited_payload=params_or_body.get('edited_payload'),
                )
                return 200, 'application/json', res

            if method == 'POST' and path == '/review-inbox/batch':
                res = svc.batch_decide(
                    conn,
                    owner_id=owner_id,
                    reviewer_id=owner_id,
                    proposal_uids=params_or_body.get('proposal_uids') or [],
                    user_verdict=str(params_or_body.get('user_verdict') or ''),
                    user_reason=(str(params_or_body['user_reason']) if params_or_body.get('user_reason') else None),
                    note=str(params_or_body.get('note') or ''),
                )
                return 200, 'application/json', res

            return _err('not_found', 404)
    except PgExperienceUnavailableError as exc:
        return _err(exc.code, exc.status)
    except ReviewInboxError as exc:
        return _err(exc.code, exc.status)
    except (TypeError, ValueError):
        return _err('invalid_request', 400)
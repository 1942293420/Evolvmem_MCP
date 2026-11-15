"""Web Console v2 Overview (M1), Pipeline/Backfill (M4), History (M5), Evolution (M6-M8), and Control (M9)."""
from __future__ import annotations

import re
from typing import Any

from evolvmem.experience_v2.control.reputation_policy import ReputationPolicyConfig
from evolvmem.experience_v2.interception_store import query_interception_funnel
from evolvmem.history_pg.history_web_pg import dispatch_pg_history_api
from evolvmem.web_v2.common import (
    V2ApiError,
    iso,
    jval,
    open_exp_pg,
    resolve_actor_and_library,
    resolve_shadow_mode,
)

_HISTORY_BLOCK_ID_RE = re.compile(r'^[A-Za-z0-9._-]+$')


def _valid_project_key(proj: str) -> bool:
    return bool(proj) and len(proj) <= 256 and '/' not in proj and '..' not in proj and '\x00' not in proj


def _safe_policy_dict(policy: Any) -> dict[str, Any]:
    if hasattr(policy, 'as_dict'):
        return dict(policy.as_dict())
    return {
        'console_enabled': bool(getattr(policy, 'console_enabled', True)),
        'experience_v2_enabled': bool(getattr(policy, 'experience_v2_enabled', False)),
        'experience_backfill_enabled': bool(getattr(policy, 'experience_backfill_enabled', False)),
        'ai_processing_enabled': bool(getattr(policy, 'ai_processing_enabled', False)),
        'shadow_mode': resolve_shadow_mode(policy),
    }


def _model_reputation_rollup(conn, owner_id: str) -> list[dict[str, Any]]:
    cfg = ReputationPolicyConfig()
    rows = conn.execute(
        """
        SELECT COALESCE(p.agent_client, 'unknown'),
               COALESCE(p.model, 'unknown'),
               COUNT(*)::int,
               COUNT(*) FILTER (WHERE p.state = 'accepted')::int,
               COUNT(*) FILTER (WHERE p.state = 'rejected')::int,
               COUNT(*) FILTER (WHERE p.state = 'merged')::int,
               COUNT(*) FILTER (WHERE p.state = 'quarantined')::int,
               COUNT(*) FILTER (WHERE r.user_verdict IN ('accept', 'rescue', 'edit_accept'))::int,
               COUNT(*) FILTER (WHERE r.user_verdict = 'reject')::int
        FROM experience_proposals p
        LEFT JOIN review_decisions r ON r.proposal_uid = p.uid AND r.owner_id = p.owner_id
        WHERE p.owner_id = %s
        GROUP BY 1, 2
        ORDER BY 3 DESC, 1 ASC, 2 ASC
        LIMIT 30
        """,
        (owner_id,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for client, model, total, acc, rej, mrg, quar, u_acc, u_rej in rows:
        tier, base_quota = cfg.model_tiers.get((str(client).lower(), str(model).lower()), (cfg.default_tier, cfg.default_quota_per_session))
        evaluated = int(acc) + int(rej)
        rate = round(int(acc) / evaluated, 4) if evaluated else None
        alert = None
        eff_quota = base_quota
        if evaluated >= cfg.min_evaluated_samples and rate is not None:
            if rate < cfg.low_acceptance_threshold:
                alert = 'low_acceptance_rate'
                eff_quota = 0
            elif rate > cfg.high_acceptance_threshold:
                eff_quota = base_quota + cfg.boost_bonus_quota
        out.append({
            'agent_client': client,
            'model': model,
            'tier': tier,
            'base_quota': base_quota,
            'effective_quota': eff_quota,
            'proposed': int(total),
            'accepted': int(acc),
            'rejected': int(rej),
            'merged': int(mrg),
            'quarantined': int(quar),
            'human_approved': int(u_acc),
            'human_rejected': int(u_rej),
            'acceptance_rate': rate,
            'alert': alert,
        })
    return out


def get_overview(console: Any, policy: Any, session: Any, params: dict[str, Any]) -> dict[str, Any]:
    inspect = str(params.get('inspect') or '') == '1'
    actor, owner_id, is_owner, _lib, me = resolve_actor_and_library(
        console, session, params.get('owner_id'), need_experience=True, inspect=inspect,
    )
    with open_exp_pg(role='coordinator' if is_owner else 'reader') as (_settings, conn):
        notes_row = conn.execute(
            """
            SELECT COUNT(*) FILTER (WHERE status = 'active' AND deleted_at IS NULL)::int,
                   COUNT(*) FILTER (WHERE status = 'challenger' AND deleted_at IS NULL)::int,
                   COUNT(*) FILTER (WHERE status = 'retired')::int
            FROM experience_notes
            WHERE owner_id = %s
            """,
            (owner_id,),
        ).fetchone()
        active_cnt = int(notes_row[0]) if notes_row else 0
        probation_cnt = int(notes_row[1]) if (notes_row and is_owner) else 0
        retired_cnt = int(notes_row[2]) if (notes_row and is_owner) else 0

        pending_live = 0
        pending_backfill = 0
        circuits: list[dict[str, Any]] = []
        recent_events: list[dict[str, Any]] = []
        funnel = None
        if is_owner:
            for tr, cnt in conn.execute(
                """
                SELECT p.source_track, COUNT(*)::int
                FROM experience_proposals p
                WHERE p.owner_id = %s
                  AND p.state IN ('pending', 'review', 'rejected', 'quarantined')
                  AND NOT EXISTS (
                      SELECT 1 FROM review_decisions r
                      WHERE r.proposal_uid = p.uid AND r.owner_id = p.owner_id
                  )
                GROUP BY p.source_track
                """,
                (owner_id,),
            ).fetchall():
                if tr == 'live':
                    pending_live = int(cnt)
                elif tr == 'backfill':
                    pending_backfill = int(cnt)
            circuits = [
                {'track': c[0], 'paused': bool(c[1]), 'pause_reason': c[2] or '', 'updated_at': iso(c[3])}
                for c in conn.execute(
                    """
                    SELECT track, paused, pause_reason, updated_at
                    FROM pipeline_circuit_state
                    WHERE owner_id = %s
                    ORDER BY track ASC
                    """,
                    (owner_id,),
                ).fetchall()
            ]
            recent_events = [
                {
                    'seq': int(e[0]), 'action': e[1], 'note_id': e[2],
                    'agent_client': e[3] or '', 'model': e[4] or '', 'created_at': iso(e[5]),
                }
                for e in conn.execute(
                    """
                    SELECT seq, action, note_id, agent_client, model, created_at
                    FROM experience_events
                    WHERE owner_id = %s
                    ORDER BY seq DESC LIMIT 12
                    """,
                    (owner_id,),
                ).fetchall()
            ]
            funnel = query_interception_funnel(conn, owner_id=owner_id)

    return {
        'ok': True,
        'current_user': actor,
        'owner_id': owner_id,
        'is_owner': is_owner,
        'libraries': me.get('libraries') or [],
        'policy': _safe_policy_dict(policy),
        'metrics': {
            'active_notes': active_cnt,
            'probation_notes': probation_cnt,
            'retired_notes': retired_cnt,
            'pending_live': pending_live,
            'pending_backfill': pending_backfill,
            'intercepted_total': (funnel or {}).get('total_intercepted', 0),
            'rescued_total': (funnel or {}).get('rescued_total', 0),
        },
        'interception_funnel': funnel,
        'circuits': circuits,
        'recent_events': recent_events,
    }


def get_pipeline(console: Any, policy: Any, session: Any, params: dict[str, Any]) -> dict[str, Any]:
    inspect = str(params.get('inspect') or '') == '1'
    _actor, owner_id, is_owner, _lib, _me = resolve_actor_and_library(
        console, session, params.get('owner_id'), need_experience=True, inspect=inspect,
    )
    if not is_owner:
        raise V2ApiError('owner_only_pipeline', 403)

    with open_exp_pg(role='coordinator') as (_settings, conn):
        circuits = [
            {'track': r[0], 'paused': bool(r[1]), 'pause_reason': r[2] or '', 'updated_at': iso(r[3])}
            for r in conn.execute(
                'SELECT track, paused, pause_reason, updated_at FROM pipeline_circuit_state WHERE owner_id = %s ORDER BY track',
                (owner_id,),
            ).fetchall()
        ]
        segments = [
            {
                'track': r[0], 'session_id': r[1], 'segment_hash': r[2],
                'pipeline_version': r[3], 'status': r[4], 'moments_count': int(r[5]),
                'error_code': r[6] or '', 'last_event_index': int(r[7]), 'updated_at': iso(r[8]),
            }
            for r in conn.execute(
                """
                SELECT track, session_id, segment_hash, pipeline_version, status,
                       moments_count, error_code, last_event_index, updated_at
                FROM pipeline_segment_runs
                WHERE owner_id = %s
                ORDER BY updated_at DESC LIMIT 60
                """,
                (owner_id,),
            ).fetchall()
        ]
        llm_usage = [
            {
                'track': r[0], 'stage': r[1], 'model': r[2], 'calls': int(r[3]),
                'input_tokens': int(r[4]), 'output_tokens': int(r[5]), 'cost_estimate': round(float(r[6]), 6),
            }
            for r in conn.execute(
                """
                SELECT c.track, c.stage, c.model, COUNT(*)::int,
                       COALESCE(SUM(c.input_tokens), 0)::int,
                       COALESCE(SUM(c.output_tokens), 0)::int,
                       COALESCE(SUM(c.cost_estimate), 0)::float
                FROM llm_calls c
                WHERE c.is_shadow = false AND (
                    EXISTS (SELECT 1 FROM experience_proposals p WHERE p.owner_id = %s AND p.uid = c.proposal_uid)
                    OR EXISTS (SELECT 1 FROM pipeline_segment_runs s WHERE s.owner_id = %s AND s.session_id = c.session_id)
                )
                GROUP BY c.track, c.stage, c.model
                ORDER BY c.track ASC, c.stage ASC
                """,
                (owner_id, owner_id),
            ).fetchall()
        ]
        cursors = [
            {
                'scope_kind': r[0], 'scope_value': r[1], 'is_shadow': bool(r[2]),
                'last_created_at': iso(r[3]), 'last_source_ref': r[4],
                'processed_count': int(r[5]), 'completed': bool(r[6]), 'updated_at': iso(r[7]),
            }
            for r in conn.execute(
                """
                SELECT scope_kind, scope_value, is_shadow, last_created_at,
                       last_source_ref, processed_count, completed, updated_at
                FROM backfill_cursors WHERE owner_id = %s ORDER BY updated_at DESC LIMIT 30
                """,
                (owner_id,),
            ).fetchall()
        ]
        job_stats = [
            {'status': r[0], 'is_shadow': bool(r[1]), 'count': int(r[2])}
            for r in conn.execute(
                """
                SELECT status, is_shadow, COUNT(*)::int
                FROM backfill_jobs WHERE owner_id = %s
                GROUP BY status, is_shadow
                """,
                (owner_id,),
            ).fetchall()
        ]
        pilots = [
            {
                'run_id': r[0], 'track': r[1], 'pipeline_version': r[2],
                'sessions_total': int(r[3]), 'sessions_zero_moments': int(r[4]),
                'moments_detected': int(r[5]), 'proposals_generated': int(r[6]),
                'proposals_admitted_candidate': int(r[7]), 'tokens_total': int(r[8]),
                'llm_calls_total': int(r[9]), 'report': jval(r[10], {}), 'created_at': iso(r[11]),
            }
            for r in conn.execute(
                """
                SELECT run_id, track, pipeline_version, sessions_total, sessions_zero_moments,
                       moments_detected, proposals_generated, proposals_admitted_candidate,
                       tokens_total, llm_calls_total, report_json, created_at
                FROM shadow_pilot_runs WHERE owner_id = %s
                ORDER BY created_at DESC LIMIT 10
                """,
                (owner_id,),
            ).fetchall()
        ]
        calibrations = [
            {
                'source_track': r[0], 'category': r[1], 'reviewed_total': int(r[2]),
                'agreed_total': int(r[3]), 'agreement_rate': float(r[4]),
                'recommended_mode': r[5], 'created_at': iso(r[6]),
            }
            for r in conn.execute(
                """
                SELECT source_track, category, reviewed_total, agreed_total,
                       agreement_rate, recommended_mode, created_at
                FROM track_calibration_snapshots WHERE owner_id = %s
                ORDER BY created_at DESC LIMIT 20
                """,
                (owner_id,),
            ).fetchall()
        ]
        cleanups = [
            {
                'cleanup_id': r[0], 'approved_by': r[1], 'approved_at': iso(r[2]),
                'authorization_note': r[3], 'deleted_total': int(r[4]),
                'migrated_keep_total': int(r[5]), 'rescued_ids': jval(r[6], []),
                'table_dispositions': jval(r[7], {}), 'backup_sha256': r[8],
            }
            for r in conn.execute(
                """
                SELECT cleanup_id, approved_by, approved_at, authorization_note, deleted_total,
                       migrated_keep_total, rescued_ids_json, table_dispositions_json, backup_sha256
                FROM legacy_cleanup_registry WHERE owner_id = %s
                ORDER BY approved_at DESC LIMIT 10
                """,
                (owner_id,),
            ).fetchall()
        ]

    return {
        'ok': True,
        'owner_id': owner_id,
        'policy': _safe_policy_dict(policy),
        'circuits': circuits,
        'segments': segments,
        'llm_usage': llm_usage,
        'backfill_cursors': cursors,
        'backfill_job_stats': job_stats,
        'shadow_pilots': pilots,
        'calibrations': calibrations,
        'legacy_cleanups': cleanups,
    }


def handle_history(console: Any, session: Any, method: str, params_or_body: dict[str, Any]) -> dict[str, Any]:
    target = params_or_body.get('owner_id')
    is_manage = (method == 'POST')
    _actor, owner_id, is_owner, _lib, _me = resolve_actor_and_library(
        console, session, target, need_history=True, require_write=is_manage,
    )
    env = {'EVOLVMEM_CONSOLE_OWNER_ID': owner_id, 'EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE': owner_id}

    if is_manage:
        action = str(params_or_body.get('action') or '').strip()
        if action not in ('delete', 'restore'):
            raise V2ApiError('invalid_history_manage_action', 400)
        status, payload = dispatch_pg_history_api(
            'POST', f'/api/history-candidate/management/{action}', params_or_body,
            {'x-evolvmem-action': 'history-manage'}, environ=env, owner_verified=is_owner,
        )
        return {'http_status': status, **payload}

    view = str(params_or_body.get('view') or 'projects').strip()
    if view == 'projects':
        path = '/api/history-candidate/projects'
    elif view == 'project':
        proj = str(params_or_body.get('project_id') or '').strip()
        if not _valid_project_key(proj):
            raise V2ApiError('invalid_project_id', 400)
        path = f'/api/history-candidate/project/{proj}'
    elif view == 'search':
        path = '/api/history-candidate/search'
    elif view == 'unassigned':
        path = '/api/history-candidate/unassigned'
    elif view == 'trash':
        if not is_owner:
            raise V2ApiError('owner_only_trash', 403)
        path = '/api/history-candidate/management/trash'
    elif view == 'coverage':
        proj = str(params_or_body.get('project_id') or '').strip()
        if not _valid_project_key(proj):
            raise V2ApiError('invalid_project_id', 400)
        path = f'/api/history-candidate/coverage/{proj}'
    elif view == 'turns':
        blk = str(params_or_body.get('block_id') or '').strip()
        if not blk or not _HISTORY_BLOCK_ID_RE.fullmatch(blk):
            raise V2ApiError('invalid_block_id', 400)
        path = f'/api/history-candidate/block/{blk}/turns'
    else:
        raise V2ApiError('invalid_history_view', 400)

    try:
        # ACL already enforced by resolve_actor_and_library(need_history=True)
        status, payload = dispatch_pg_history_api('GET', path, params_or_body, {}, environ=env, owner_verified=True)
        err = str((payload or {}).get('error') or '')
        if status == 400 and err.startswith(('unmapped_user_history_db', 'missing_history_pg_target', 'missing_console_owner_id')):
            return {
                'http_status': 200, 'ok': True, 'owner_id': owner_id, 'is_owner': is_owner,
                'pg_connected': False, 'projects': [], 'blocks': [], 'items': [], 'messages': [], 'detail': err,
            }
        return {'http_status': status, 'owner_id': owner_id, 'is_owner': is_owner, **payload}
    except Exception:
        return {
            'http_status': 200, 'ok': True, 'owner_id': owner_id, 'is_owner': is_owner,
            'pg_connected': False, 'projects': [], 'blocks': [], 'items': [], 'messages': [], 'detail': 'history_pg_unconfigured',
        }


def get_evolution_and_skills(console: Any, session: Any, params: dict[str, Any]) -> dict[str, Any]:
    actor, owner_id, is_owner, _lib, _me = resolve_actor_and_library(
        console, session, params.get('owner_id'), need_experience=True,
    )
    with open_exp_pg(role='coordinator' if is_owner else 'reader') as (_settings, conn):
        trials = []
        if is_owner:
            trials = [
                {
                    'trial_id': int(r[0]), 'challenger_note_id': r[1], 'incumbent_note_id': r[2],
                    'relation': r[3], 'required_successes': int(r[4]),
                    'success_count': int(r[5]), 'failure_count': int(r[6]),
                    'status': r[7], 'updated_at': iso(r[8]),
                }
                for r in conn.execute(
                    """
                    SELECT id, challenger_note_id, incumbent_note_id, relation, required_successes,
                           success_count, failure_count, status, updated_at
                    FROM challenger_trials WHERE owner_id = %s
                    ORDER BY updated_at DESC LIMIT 30
                    """,
                    (owner_id,),
                ).fetchall()
            ]
        relations = [
            {
                'relation_id': int(r[0]), 'from_uid': r[1], 'to_uid': r[2],
                'relation': r[3], 'confidence': None,
                'reason': r[4] or '', 'proposal_uid': r[5], 'created_at': iso(r[6]),
            }
            for r in conn.execute(
                """
                SELECT relation_id, from_uid, to_uid, relation, reason, proposal_uid, created_at
                FROM experience_relations WHERE owner_id = %s
                ORDER BY relation_id DESC LIMIT 40
                """,
                (owner_id,),
            ).fetchall()
        ]
        impressions = [
            {'item_uid': r[0], 'channel': r[1], 'agent_client': r[2] or '', 'model': r[3] or '', 'hits': int(r[4])}
            for r in conn.execute(
                """
                SELECT item_uid, channel, agent_client, model, COUNT(*)::int
                FROM retrieval_impressions WHERE owner_id = %s
                GROUP BY item_uid, channel, agent_client, model
                ORDER BY 5 DESC LIMIT 30
                """,
                (owner_id,),
            ).fetchall()
        ]
        candidates = [
            {
                'id': r[0], 'category': r[1], 'kind': r[2], 'problem': r[3],
                'reusable_experience': r[4], 'occurrence_count': int(r[5]), 'importance': int(r[6]),
            }
            for r in conn.execute(
                """
                SELECT id, category, kind, problem, reusable_experience, occurrence_count, importance
                FROM experience_notes
                WHERE owner_id = %s AND deleted_at IS NULL AND status = 'active'
                  AND (occurrence_count >= 2 OR kind = 'habit' OR pinned = true)
                ORDER BY occurrence_count DESC, importance DESC LIMIT 30
                """,
                (owner_id,),
            ).fetchall()
        ]

    skills_payload: dict[str, Any] = {'skills': [], 'restricted': not bool(actor.get('is_admin'))}
    if actor.get('is_admin'):
        st, _, sk = console.dispatch('GET', '/skills', {}, session)
        if st == 200 and isinstance(sk, dict):
            skills_payload = sk

    return {
        'ok': True,
        'owner_id': owner_id,
        'is_owner': is_owner,
        'trials': trials,
        'relations': relations,
        'impressions': impressions,
        'habit_and_skill_candidates': candidates,
        'skills_catalog': skills_payload,
    }


def get_control(console: Any, policy: Any, session: Any, params: dict[str, Any]) -> dict[str, Any]:
    actor, owner_id, is_owner, _lib, me = resolve_actor_and_library(
        console, session, params.get('owner_id'), need_experience=True,
    )
    users_data: list[dict[str, Any]] = []
    pending_data: list[dict[str, Any]] = []
    grants_data: list[dict[str, Any]] = []
    if actor.get('is_admin'):
        st_u, _, u_res = console.dispatch('GET', '/users', {}, session)
        if st_u == 200 and isinstance(u_res, dict):
            users_data = u_res.get('users') or []
            pending_data = u_res.get('pending') or []
        st_g, _, g_res = console.dispatch('GET', '/grants', {}, session)
        if st_g == 200 and isinstance(g_res, dict):
            grants_data = g_res.get('grants') or g_res.get('rows') or []

    reputation: list[dict[str, Any]] = []
    if is_owner:
        with open_exp_pg(role='coordinator') as (_settings, conn):
            reputation = _model_reputation_rollup(conn, owner_id)

    return {
        'ok': True,
        'current_user': actor,
        'owner_id': owner_id,
        'is_owner': is_owner,
        'policy': _safe_policy_dict(policy),
        'libraries': me.get('libraries') or [],
        'users': users_data,
        'pending': pending_data,
        'grants': grants_data,
        'model_reputation': reputation,
    }


def post_control_action(console: Any, session: Any, body: dict[str, Any]) -> dict[str, Any]:
    actor, _owner_id, _is_owner, _lib, _me = resolve_actor_and_library(console, session, None)
    if not actor.get('is_admin'):
        raise V2ApiError('admin_required', 403)
    action = str(body.get('action') or '').strip()
    if action == 'set_grant':
        payload = {
            'viewer_id': body.get('viewer_id'),
            'owner_id': body.get('owner_id'),
            'history_read': body.get('history_read'),
            'experience_read': body.get('experience_read'),
            'revision': body.get('revision'),
        }
        status, _, res = console.dispatch('POST', '/grants', payload, session)
        return {'http_status': status, **(res if isinstance(res, dict) else {'ok': False})}
    if action in ('create_user', 'update_user'):
        uid = str(body.get('id') or '').strip()
        path = '/users' if action == 'create_user' else f'/users/{uid}'
        payload = {k: v for k, v in body.items() if k in ('id', 'display_name', 'open_id', 'enabled', 'revision')}
        status, _, res = console.dispatch('POST', path, payload, session)
        return {'http_status': status, **(res if isinstance(res, dict) else {'ok': False})}
    raise V2ApiError('invalid_control_action', 400)

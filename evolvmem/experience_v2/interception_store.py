"""Unified interception ledger & gate-chain traceability store (Migration 005)."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from typing import Any
import uuid

from evolvmem.experience_v2.gate.review_inbox import _build_evidence_preview
from evolvmem.experience_v2.gate.value import _build_blind_problem_prompt
from evolvmem.experience_v2.interception_rescue import rescue_intercepted_item  # noqa: F401


def _iso(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, datetime):
        dt = val if val.tzinfo is not None else val.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec='seconds')
    return str(val)


def _jval(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return default
    return raw


def parse_skip_reason(raw: str) -> tuple[str, str]:
    text = str(raw or '').strip()
    if not text:
        return 'other', ''
    if ':' in text:
        head, tail = text.split(':', 1)
        code = head.strip().lower()[:64] or 'other'
        return code, (tail.strip() or text)[:240]
    return text[:64], text[:240]


def record_pipeline_interception(
    conn,
    *,
    owner_id: str,
    track: str,
    session_id: str,
    intercept_layer: str,
    reason_code: str,
    detail: str = '',
    source_ref: str = '',
    segment_hash: str = '',
    moment: Any = None,
    agent_client: str = '',
    model: str = '',
    candidate_json: dict[str, Any] | None = None,
    evidence_json: list[dict[str, Any]] | None = None,
    llm_call_id: int | None = None,
    tokens_used: int = 0,
    is_shadow: bool = False,
) -> str | None:
    """Persist Extract/Quota/Moments pre-proposal drops into pipeline_interceptions."""
    if conn is None or not owner_id:
        return None
    reg = conn.execute("SELECT to_regclass('pipeline_interceptions')").fetchone()
    if not reg or reg[0] is None:
        return None
    code, parsed_detail = parse_skip_reason(reason_code)
    final_detail = (detail or parsed_detail or code)[:240]
    m_id = str(getattr(moment, 'moment_id', '') or '')[:128]
    m_kind = str(getattr(moment, 'kind', '') or '')[:128]
    m_title = str(getattr(moment, 'title', '') or '')
    m_snip = str(getattr(moment, 'snippet_text', '') or '')
    m_summary = (' '.join(f'{m_title}: {m_snip}'.split()) if m_title else ' '.join(m_snip.split()))[:240]
    ev = evidence_json
    if ev is None and moment is not None:
        ptrs = getattr(moment, 'evidence_pointers', ()) or ()
        ev = [dict(p) for p in ptrs if isinstance(p, dict)]
    if ev is None and isinstance(candidate_json, dict) and isinstance(candidate_json.get('evidence'), list):
        ev = candidate_json.get('evidence')
    int_id = f'int-{uuid.uuid4().hex[:20]}'
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO pipeline_interceptions(
              interception_id, owner_id, track, session_id, source_ref, segment_hash,
              moment_id, moment_kind, moment_summary, intercept_layer, reason_code, detail,
              agent_client, model, candidate_json, evidence_json, llm_call_id, tokens_used, is_shadow
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
            """,
            (
                int_id, owner_id, track if track in ('live', 'backfill') else 'live',
                str(session_id or ''), str(source_ref or ''), str(segment_hash or ''),
                m_id, m_kind, m_summary, intercept_layer, code, final_detail,
                str(agent_client or ''), str(model or ''),
                json.dumps(candidate_json or {}, ensure_ascii=False),
                json.dumps(ev or [], ensure_ascii=False),
                llm_call_id, max(0, int(tokens_used or 0)), bool(is_shadow),
            ),
        )
    return int_id


def update_decision_meta(conn, decision_id: str, meta: dict[str, Any]) -> None:
    """Attach structured diagnostic metadata onto an existing proposal_decisions row."""
    if not decision_id:
        return
    conn.execute(
        'UPDATE proposal_decisions SET meta_json = %s::jsonb WHERE decision_id = %s',
        (json.dumps(meta or {}, ensure_ascii=False), decision_id),
    )


def fetch_proposal_gate_chain(
    conn,
    proposal_uid: str,
    *,
    problem: str = '',
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Load ordered L0..L4 decisions (with meta_json), review row, and metered llm_calls."""
    if not proposal_uid:
        return {'decisions': [], 'review': None, 'llm_calls': []}
    decs = conn.execute(
        """
        SELECT decision_id, layer, verdict, reason_code, confidence,
               judge_model, detail, target_note_id, meta_json, created_at
        FROM proposal_decisions
        WHERE proposal_uid = %s
        ORDER BY created_at ASC, decision_id ASC
        """,
        (proposal_uid,),
    ).fetchall()
    decisions = [
        {
            'decision_id': r[0],
            'layer': r[1],
            'verdict': r[2],
            'reason_code': r[3],
            'confidence': float(r[4]) if r[4] is not None else None,
            'judge_model': r[5],
            'detail': r[6],
            'target_note_id': r[7],
            'meta': _jval(r[8], {}),
            'created_at': _iso(r[9]),
        }
        for r in decs
    ]
    rev_row = conn.execute(
        """
        SELECT review_id, reviewer_id, gate_verdict, gate_confidence, gate_reason,
               user_verdict, user_reason, note, edited_payload, decision_id, created_at
        FROM review_decisions
        WHERE proposal_uid = %s
        ORDER BY created_at DESC LIMIT 1
        """,
        (proposal_uid,),
    ).fetchone()
    review = None
    if rev_row:
        review = {
            'review_id': rev_row[0],
            'reviewer_id': rev_row[1],
            'gate_verdict': rev_row[2],
            'gate_confidence': float(rev_row[3]) if rev_row[3] is not None else None,
            'gate_reason': rev_row[4],
            'user_verdict': rev_row[5],
            'user_reason': rev_row[6],
            'note': rev_row[7],
            'edited_payload': _jval(rev_row[8], None),
            'decision_id': rev_row[9],
            'created_at': _iso(rev_row[10]),
        }
    call_rows = conn.execute(
        """
        SELECT id, track, stage, model, input_tokens, output_tokens, cost_estimate, outcome, created_at
        FROM llm_calls WHERE proposal_uid = %s ORDER BY id ASC
        """,
        (proposal_uid,),
    ).fetchall()
    llm_calls = [
        {
            'id': int(c[0]),
            'track': c[1],
            'stage': c[2],
            'model': c[3],
            'input_tokens': int(c[4]),
            'output_tokens': int(c[5]),
            'cost_estimate': float(c[6]),
            'outcome': c[7],
            'created_at': _iso(c[8]),
        }
        for c in call_rows
    ]
    last_l2a_pred = ''
    for dec in decisions:
        lyr = dec.get('layer')
        if lyr in ('L2a', 'L2b'):
            meta = dec.get('meta') if isinstance(dec.get('meta'), dict) else {}
            if problem and not meta.get('blind_prompt'):
                meta['blind_prompt'] = _build_blind_problem_prompt(problem, scope)
            if lyr == 'L2a' and meta.get('model_prediction'):
                last_l2a_pred = str(meta['model_prediction'])
            elif lyr == 'L2b' and last_l2a_pred and not meta.get('flash_prediction'):
                meta['flash_prediction'] = last_l2a_pred
            dec['meta'] = meta
    return {'decisions': decisions, 'review': review, 'llm_calls': llm_calls}


def query_interception_funnel(conn, *, owner_id: str, track: str = 'all') -> dict[str, Any]:
    """Compute exact non-write / intercepted / held counts across all pipeline layers."""
    has_track = track in ('live', 'backfill')
    tr_cond = 'AND track = %s' if has_track else ''
    ptr_cond = 'AND p.source_track = %s' if has_track else ''
    rtr_cond = 'AND source_track = %s' if has_track else ''
    tr_args = (owner_id, track) if has_track else (owner_id,)

    moments_cnt = int(
        conn.execute(
            f"""
            SELECT COUNT(*) FROM pipeline_segment_runs
            WHERE owner_id = %s AND is_shadow = false {tr_cond}
              AND status IN ('skipped_zero_moments', 'deferred', 'failed')
            """,
            tr_args,
        ).fetchone()[0]
    )
    pi_rows = conn.execute(
        f"""
        SELECT intercept_layer, COUNT(*), COUNT(rescued_note_id)
        FROM pipeline_interceptions
        WHERE owner_id = %s AND is_shadow = false {tr_cond}
        GROUP BY intercept_layer
        """,
        tr_args,
    ).fetchall()
    pi_map = {r[0]: int(r[1]) for r in pi_rows}
    rescued_pre = sum(int(r[2]) for r in pi_rows)

    dec_rows = conn.execute(
        f"""
        SELECT d.layer, d.verdict, COUNT(DISTINCT p.uid)
        FROM experience_proposals p
        JOIN proposal_decisions d ON d.proposal_uid = p.uid
        WHERE p.owner_id = %s {ptr_cond}
          AND d.verdict IN ('reject', 'merge')
        GROUP BY d.layer, d.verdict
        """,
        tr_args,
    ).fetchall()
    dec_map = {(r[0], r[1]): int(r[2]) for r in dec_rows}

    trial_cnt = int(
        conn.execute(
            "SELECT COUNT(*) FROM challenger_trials WHERE owner_id = %s AND status IN ('trial', 'rolled_back')",
            (owner_id,),
        ).fetchone()[0]
    )
    l4_cnt = int(
        conn.execute(
            f"SELECT COUNT(*) FROM review_decisions WHERE owner_id = %s AND user_verdict = 'reject' {rtr_cond}",
            tr_args,
        ).fetchone()[0]
    )
    rescued_rev = int(
        conn.execute(
            f"SELECT COUNT(*) FROM review_decisions WHERE owner_id = %s AND user_verdict = 'rescue' {rtr_cond}",
            tr_args,
        ).fetchone()[0]
    )
    stage4_row = conn.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(deleted_total), 0), COALESCE(SUM(migrated_keep_total), 0)
        FROM legacy_cleanup_registry WHERE owner_id = %s
        """,
        (owner_id,),
    ).fetchone()
    layers = {
        'Moments': moments_cnt + pi_map.get('Moments', 0),
        'Extract': pi_map.get('Extract', 0),
        'Quota': pi_map.get('Quota', 0),
        'L0': dec_map.get(('L0', 'reject'), 0),
        'L1': dec_map.get(('L1', 'merge'), 0),
        'L2a': dec_map.get(('L2a', 'reject'), 0),
        'L2b': dec_map.get(('L2b', 'reject'), 0),
        'L3': dec_map.get(('L3', 'merge'), 0) + trial_cnt,
        'L4': l4_cnt,
        'Stage4': int(stage4_row[1]) if stage4_row else 0,
    }
    return {
        'layers': layers,
        'total_intercepted': sum(v for k, v in layers.items() if k != 'Stage4'),
        'rescued_total': max(rescued_pre, rescued_rev),
        'stage4_batches': int(stage4_row[0]) if stage4_row else 0,
        'stage4_kept_total': int(stage4_row[2]) if stage4_row else 0,
    }


def _classify_proposal_interception(decisions: list[dict[str, Any]], review: dict[str, Any] | None, state: str) -> tuple[str, str, str, str]:
    if review and review.get('user_verdict') == 'reject':
        return 'L4', str(review.get('user_reason') or 'rejected'), str(review.get('note') or '人工审阅驳回'), ''
    for d in reversed(decisions):
        if d['verdict'] in ('reject', 'merge'):
            return d['layer'], str(d.get('reason_code') or d['verdict']), str(d.get('detail') or ''), str(d.get('target_note_id') or '')
    return ('Quota' if state == 'quarantined' else 'L0'), state, '', ''


def _note_preview_map(conn, owner_id: str, note_ids: list[str]) -> dict[str, dict[str, Any]]:
    clean_ids = [nid for nid in dict.fromkeys(note_ids) if nid]
    if not clean_ids:
        return {}
    rows = conn.execute(
        """
        SELECT id, category, problem, solution, reusable_experience, status, occurrence_count, scope
        FROM experience_notes
        WHERE owner_id = %s AND id = ANY(%s)
        """,
        (owner_id, clean_ids),
    ).fetchall()
    return {
        r[0]: {
            'id': r[0], 'category': r[1], 'problem': r[2], 'solution': r[3],
            'reusable_experience': r[4], 'status': r[5], 'occurrence_count': int(r[6]),
            'scope': _jval(r[7], {}),
        }
        for r in rows
    }


def _append_q(parts: list[str], args: list[Any], ql: str, cols: tuple[str, ...], extra: str = '') -> None:
    """Append case-insensitive ILIKE search predicates across target SQL columns."""
    if not ql:
        return
    clauses = [f'{col} ILIKE %s' for col in cols] + ([extra] if extra else [])
    args.extend([f'%{ql}%'] * (len(cols) + extra.count('%s')))
    parts.append('(' + ' OR '.join(clauses) + ')')


def list_intercepted_items(
    conn,
    *,
    owner_id: str,
    layer: str = 'all',
    track: str = 'all',
    category: str = 'all',
    q: str = '',
    limit: int = 80,
) -> dict[str, Any]:
    """List unified intercepted/non-written items with L1/L2/L3 detail structures."""
    cap = max(1, min(int(limit), 200))
    ql = q.strip().lower()
    items: list[dict[str, Any]] = []

    if layer in ('all', 'Extract', 'Quota', 'Moments'):
        pi_parts, pi_args = ['owner_id = %s', 'is_shadow = false'], [owner_id]
        if layer in ('Extract', 'Quota', 'Moments'):
            pi_parts.append('intercept_layer = %s')
            pi_args.append(layer)
        if track in ('live', 'backfill'):
            pi_parts.append('track = %s')
            pi_args.append(track)
        _append_q(pi_parts, pi_args, ql, ('interception_id', "COALESCE(moment_summary,'')", "COALESCE(reason_code,'')", "COALESCE(detail,'')", "COALESCE(candidate_json::text,'')", "COALESCE(model,'')"))
        pi_rows = conn.execute(
            f"""SELECT interception_id, track, session_id, source_ref, moment_id, moment_kind,
                   moment_summary, intercept_layer, reason_code, detail, agent_client, model,
                   candidate_json, evidence_json, llm_call_id, tokens_used,
                   rescued_proposal_uid, rescued_note_id, created_at
            FROM pipeline_interceptions WHERE {' AND '.join(pi_parts)} ORDER BY created_at DESC LIMIT %s""",
            (*pi_args, cap),
        ).fetchall()
        for r in pi_rows:
            cand = _jval(r[12], {})
            ev = _jval(r[13], [])
            cat = str(cand.get('category') or '系统环境经验')
            title = str(cand.get('problem') or r[6] or r[9] or f'{r[7]} 拦截 ({r[8]})')
            items.append({
                'id': r[0], 'source_table': 'pipeline_interceptions',
                'interception_id': r[0], 'proposal_uid': r[16], 'note_id': r[17],
                'layer': r[7], 'sub_status': ('rescued' if r[17] else 'dropped'),
                'reason_code': r[8], 'detail': r[9], 'title': title[:120],
                'category': cat, 'kind': str(cand.get('kind') or r[5] or 'pitfall'),
                'track': r[1], 'session_id': r[2], 'source_ref': r[3],
                'agent_client': r[10], 'model': r[11],
                'problem': str(cand.get('problem') or ''),
                'solution': str(cand.get('solution') or ''),
                'reusable_experience': str(cand.get('reusable_experience') or ''),
                'misbelief': str(cand.get('misbelief') or ''),
                'scope': cand.get('scope') if isinstance(cand.get('scope'), dict) else {},
                'evidence_preview': _build_evidence_preview(ev), 'evidence': ev,
                'rescuable': not bool(r[17]), 'rescued_note_id': r[17],
                'tokens_used': int(r[15]), 'created_at': _iso(r[18]),
                'diagnostic': {
                    'moment_id': r[4], 'moment_kind': r[5], 'moment_summary': r[6],
                    'llm_call_id': r[14], 'candidate': cand,
                },
                'gate_chain': [{
                    'decision_id': r[0], 'layer': r[7], 'verdict': 'reject',
                    'reason_code': r[8], 'confidence': None, 'judge_model': r[11],
                    'detail': r[9], 'meta': {'moment_id': r[4], 'moment_kind': r[5]}, 'created_at': _iso(r[18]),
                }],
            })

    if layer in ('all', 'L0', 'L1', 'L2a', 'L2b', 'L3', 'L4', 'Quota'):
        sql_parts = ["owner_id = %s", "state IN ('rejected', 'merged', 'quarantined')"]
        sql_args: list[Any] = [owner_id]
        if layer in ('L0', 'L1', 'L2a', 'L2b', 'L3'):
            sql_parts.append("EXISTS (SELECT 1 FROM proposal_decisions d WHERE d.proposal_uid = experience_proposals.uid AND d.layer = %s AND d.verdict IN ('reject', 'merge'))")
            sql_args.append(layer)
        elif layer == 'L4':
            sql_parts.append("EXISTS (SELECT 1 FROM review_decisions r WHERE r.proposal_uid = experience_proposals.uid AND r.user_verdict = 'reject')")
        if track in ('live', 'backfill'):
            sql_parts.append('source_track = %s')
            sql_args.append(track)
        if category != 'all':
            sql_parts.append('category = %s')
            sql_args.append(category)
        _append_q(sql_parts, sql_args, ql, ('uid', 'problem', 'solution', 'reusable_experience', "COALESCE(scope::text,'')", "COALESCE(model,'')"), "EXISTS (SELECT 1 FROM proposal_decisions dq WHERE dq.proposal_uid = experience_proposals.uid AND (COALESCE(dq.reason_code,'') ILIKE %s OR COALESCE(dq.detail,'') ILIKE %s))")
        p_rows = conn.execute(
            f"""SELECT uid, category, kind, problem, solution, reusable_experience, misbelief,
                   scope, evidence_json, source_kind, source_track, agent_client, model,
                   session_id, state, importance, merged_into_uid, COALESCE(updated_at, created_at) AS sort_ts
            FROM experience_proposals WHERE {' AND '.join(sql_parts)} ORDER BY sort_ts DESC, uid ASC LIMIT %s""",
            (*sql_args, cap),
        ).fetchall()
        target_ids = [r[16] for r in p_rows if r[16]]
        note_map = _note_preview_map(conn, owner_id, target_ids)
        for r in p_rows:
            chain = fetch_proposal_gate_chain(conn, r[0], problem=r[3], scope=_jval(r[7], {}))
            lyr, rcode, det, dec_target = _classify_proposal_interception(chain['decisions'], chain['review'], r[14])
            merged_id = r[16] or dec_target
            ev = _jval(r[8], [])
            rescuable = (r[14] in ('rejected', 'quarantined'))
            sub_st = 'merged' if r[14] == 'merged' else ('human_rejected' if lyr == 'L4' else 'gate_rejected')
            items.append({
                'id': r[0], 'source_table': 'experience_proposals',
                'interception_id': None, 'proposal_uid': r[0], 'note_id': merged_id or None,
                'layer': lyr, 'sub_status': sub_st,
                'reason_code': rcode, 'detail': det, 'title': str(r[3])[:120],
                'category': r[1], 'kind': r[2],
                'track': r[10], 'session_id': r[13] or '', 'source_ref': '',
                'agent_client': r[11], 'model': r[12],
                'problem': r[3], 'solution': r[4], 'reusable_experience': r[5],
                'misbelief': r[6], 'scope': _jval(r[7], {}),
                'evidence_preview': _build_evidence_preview(ev), 'evidence': ev,
                'rescuable': rescuable, 'rescued_note_id': None,
                'tokens_used': sum(c['input_tokens'] + c['output_tokens'] for c in chain['llm_calls']),
                'created_at': _iso(r[17]),
                'diagnostic': {
                    'target_note': note_map.get(merged_id),
                    'review': chain['review'],
                },
                'gate_chain': chain['decisions'],
                'llm_calls': chain['llm_calls'],
            })

    if layer in ('all', 'L3'):
        t_parts, t_args = ['ct.owner_id = %s', "ct.status IN ('trial', 'rolled_back')"], [owner_id]
        if category != 'all':
            t_parts.append('n.category = %s')
            t_args.append(category)
        _append_q(t_parts, t_args, ql, ("('trial-' || ct.id::text)", 'n.problem', 'n.solution', "COALESCE(n.scope::text,'')"))
        t_rows = conn.execute(
            f"""SELECT ct.id, ct.challenger_note_id, ct.incumbent_note_id, ct.relation, ct.proposal_uid,
                   ct.required_successes, ct.success_count, ct.failure_count, ct.status, ct.created_at,
                   n.category, n.kind, n.problem, n.solution, n.reusable_experience, n.misbelief,
                   n.scope, n.evidence, n.source_agent, n.source_model
            FROM challenger_trials ct JOIN experience_notes n ON n.id = ct.challenger_note_id
            WHERE {' AND '.join(t_parts)} ORDER BY ct.updated_at DESC LIMIT %s""",
            (*t_args, cap),
        ).fetchall()
        inc_map = _note_preview_map(conn, owner_id, [r[2] for r in t_rows])
        for r in t_rows:
            chain = fetch_proposal_gate_chain(conn, r[4] or '', problem=r[12], scope=_jval(r[16], {}))
            ev = _jval(r[17], [])
            items.append({
                'id': f'trial-{r[0]}', 'source_table': 'challenger_trials',
                'interception_id': None, 'proposal_uid': r[4], 'note_id': r[1],
                'layer': 'L3', 'sub_status': ('probation_hold' if r[8] == 'trial' else 'trial_rolled_back'),
                'reason_code': r[3],
                'detail': f'关系={r[3]}，试用期 {r[6]}/{r[5]} 次成功（失败 {r[7]} 次）',
                'title': str(r[12])[:120], 'category': r[10], 'kind': r[11] or 'pitfall',
                'track': 'live', 'session_id': '', 'source_ref': '',
                'agent_client': r[18] or '', 'model': r[19] or '',
                'problem': r[12], 'solution': r[13], 'reusable_experience': r[14],
                'misbelief': r[15] or '', 'scope': _jval(r[16], {}),
                'evidence_preview': _build_evidence_preview(ev), 'evidence': ev,
                'rescuable': False, 'rescued_note_id': None,
                'tokens_used': sum(c['input_tokens'] + c['output_tokens'] for c in chain['llm_calls']),
                'created_at': _iso(r[9]),
                'diagnostic': {
                    'trial': {
                        'trial_id': int(r[0]), 'relation': r[3], 'status': r[8],
                        'challenger_note_id': r[1], 'incumbent_note_id': r[2],
                        'required_successes': int(r[5]), 'success_count': int(r[6]), 'failure_count': int(r[7]),
                    },
                    'target_note': inc_map.get(r[2]),
                },
                'gate_chain': chain['decisions'],
                'llm_calls': chain['llm_calls'],
            })

    if layer in ('all', 'Moments'):
        s_parts = ['owner_id = %s', 'is_shadow = false', "status IN ('skipped_zero_moments', 'deferred', 'failed')"]
        s_args: list[Any] = [owner_id]
        _append_q(s_parts, s_args, ql, ("('seg-' || left(session_id, 24) || '-' || left(segment_hash, 8))", 'session_id', "COALESCE(error_code,'')"))
        s_rows = conn.execute(
            f"""SELECT track, session_id, segment_hash, pipeline_version, status,
                   moments_count, error_code, last_event_index, updated_at
            FROM pipeline_segment_runs WHERE {' AND '.join(s_parts)} ORDER BY updated_at DESC LIMIT 40""",
            tuple(s_args),
        ).fetchall()
        for r in s_rows:
            items.append({
                'id': f'seg-{r[1][:24]}-{r[2][:8]}', 'source_table': 'pipeline_segment_runs',
                'interception_id': None, 'proposal_uid': None, 'note_id': None,
                'layer': 'Moments', 'sub_status': r[4],
                'reason_code': r[6] or r[4],
                'detail': f'会话分段 {r[2]} ({r[4]}，检测到 {r[5]} 个片段)',
                'title': f'会话 {r[1]} 零 LLM 前置截断 ({r[6] or r[4]})',
                'category': '系统环境经验', 'kind': 'procedure',
                'track': r[0], 'session_id': r[1], 'source_ref': '',
                'agent_client': 'pipeline', 'model': r[3],
                'problem': '', 'solution': '', 'reusable_experience': '',
                'misbelief': '', 'scope': {'last_event_index': int(r[7])},
                'evidence_preview': [], 'evidence': [],
                'rescuable': False, 'rescued_note_id': None,
                'tokens_used': 0, 'created_at': _iso(r[8]),
                'diagnostic': {'segment_hash': r[2], 'status': r[4], 'moments_count': int(r[5])},
                'gate_chain': [], 'llm_calls': [],
            })

    if layer in ('all', 'Stage4'):
        c_rows = conn.execute(
            """
            SELECT cleanup_id, approved_by, approved_at, authorization_note, deleted_total,
                   migrated_keep_total, rescued_ids_json, table_dispositions_json, backup_sha256
            FROM legacy_cleanup_registry WHERE owner_id = %s
            ORDER BY approved_at DESC LIMIT 10
            """,
            (owner_id,),
        ).fetchall()
        for r in c_rows:
            items.append({
                'id': r[0], 'source_table': 'legacy_cleanup_registry',
                'interception_id': None, 'proposal_uid': None, 'note_id': None,
                'layer': 'Stage4', 'sub_status': 'legacy_cleaned',
                'reason_code': 'stage4_cleanup',
                'detail': f'Stage 4 旧库清洗：清理 {r[4]} 条垃圾/冗余，迁移保留 {r[5]} 条 legacy 经验',
                'title': f'Stage 4 旧库清洗批次 {r[0]} (删除 {r[4]} / 保留 {r[5]})',
                'category': '系统环境经验', 'kind': 'procedure',
                'track': 'backfill', 'session_id': '', 'source_ref': '',
                'agent_client': 'stage4-cleaner', 'model': 'deterministic',
                'problem': str(r[3]), 'solution': '', 'reusable_experience': '',
                'misbelief': '', 'scope': {'approved_by': r[1], 'backup_sha256': r[8]},
                'evidence_preview': [], 'evidence': [],
                'rescuable': False, 'rescued_note_id': None,
                'tokens_used': 0, 'created_at': _iso(r[2]),
                'diagnostic': {
                    'deleted_total': int(r[4]), 'migrated_keep_total': int(r[5]),
                    'rescued_ids': _jval(r[6], []), 'table_dispositions': _jval(r[7], {}),
                },
                'gate_chain': [], 'llm_calls': [],
            })

    filtered = []
    for it in items:
        if layer != 'all' and it['layer'] != layer:
            continue
        if track in ('live', 'backfill') and it['track'] != track:
            continue
        if category != 'all' and it['category'] != category:
            continue
        if ql:
            blob = ' '.join([
                it['id'], str(it.get('proposal_uid') or ''), json.dumps(it.get('scope') or {}, ensure_ascii=False),
                it['title'], it['reason_code'], it['detail'], it['problem'], it['solution'], it['reusable_experience'], it['model'],
            ]).lower()
            if ql not in blob:
                continue
        filtered.append(it)
    filtered.sort(key=lambda x: str(x.get('created_at') or ''), reverse=True)
    return {
        'ok': True,
        'funnel': query_interception_funnel(conn, owner_id=owner_id, track=track),
        'total': len(filtered),
        'items': filtered[:cap],
    }


def get_interception_trace(conn, *, owner_id: str, item_id: str) -> dict[str, Any] | None:
    for kw in ({'q': item_id}, {'layer': 'Stage4'}):
        for item in list_intercepted_items(conn, owner_id=owner_id, limit=50, **kw)['items']:
            if item['id'] == item_id:
                return item
    return None

"""Metered historical backfill for proposal_decisions.meta_json (L0/L1/L2a/L2b/L3)."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from typing import Any, Callable

from evolvmem.experience_v2.gate.value import (
    L2_PREDICT_SYSTEM_PROMPT,
    _build_blind_problem_prompt,
)
from evolvmem.experience_v2.llm_meter import BudgetConfig, LlmTransportError, load_llm_endpoints_from_file, metered_llm
from evolvmem.experience_v2.pipeline.live_worker import _resolve_transports


def _jval(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return default
    return raw


def _backfill_deterministic_layers(conn_factory: Callable[[], Any]) -> int:
    """Populate meta_json for pre-005 L0, L1, and L3 decisions with 0 LLM calls."""
    updated = 0
    with conn_factory() as conn:
        rows = conn.execute(
            """
            SELECT d.decision_id, d.layer, d.verdict, d.reason_code, d.confidence,
                   d.target_note_id, p.problem, p.solution, p.reusable_experience, p.evidence_json
            FROM proposal_decisions d
            JOIN experience_proposals p ON p.uid = d.proposal_uid
            WHERE d.layer IN ('L0', 'L1', 'L3')
              AND (d.meta_json IS NULL OR d.meta_json = '{}'::jsonb)
            """
        ).fetchall()
        for dec_id, layer, verdict, reason_code, conf, target_id, prob, sol, reus, ev_raw in rows:
            ev_list = _jval(ev_raw, [])
            if layer == 'L0':
                meta = {
                    'field_lengths': {
                        'problem': len(str(prob or '')),
                        'solution': len(str(sol or '')),
                        'reusable_experience': len(str(reus or '')),
                    },
                    'limits': {'problem': 120, 'solution': 240, 'reusable_experience': 120},
                    'require_evidence': True,
                    'evidence_count': len(ev_list) if isinstance(ev_list, list) else 0,
                    'backfilled': True,
                }
            elif layer == 'L1':
                cval = float(conf) if conf is not None else 0.0
                sim = round(cval, 4) if verdict == 'merge' else round(max(0.0, 1.0 - cval), 4)
                meta = {
                    'cosine_similarity': sim,
                    'threshold': 0.88,
                    'nearest_note_id': target_id or '',
                    'backfilled': True,
                }
            else:
                meta = {
                    'relation': reason_code or 'novel',
                    'target_note_id': target_id or '',
                    'backfilled': True,
                }
            conn.execute(
                'UPDATE proposal_decisions SET meta_json = %s::jsonb WHERE decision_id = %s',
                (json.dumps(meta, ensure_ascii=False), dec_id),
            )
            updated += 1
        conn.commit()
    return updated


def _process_l2_proposal(
    item: dict[str, Any],
    *,
    conn_factory: Callable[[], Any],
    flash_model: str,
    strong_model: str,
    f_tr: Any,
    s_tr: Any,
    budget: BudgetConfig,
) -> int:
    """Run metered blind predictions for missing L2a/L2b meta_json on one proposal."""
    uid = item['uid']
    owner_id = item['owner_id']
    track = item['track'] if item['track'] in ('live', 'backfill') else 'live'
    session_id = item['session_id'] or None
    blind_prompt = _build_blind_problem_prompt(item['problem'], item['scope'])

    updated = 0
    flash_pred = ''
    with conn_factory() as conn:
        for dec in item['decisions']:
            lyr = dec['layer']
            meta = dict(dec['meta'])
            existing_pred = str(meta.get('model_prediction') or '').strip()
            if existing_pred:
                if lyr == 'L2a':
                    flash_pred = existing_pred
                continue
            is_l2a = (lyr == 'L2a')
            model = flash_model if is_l2a else strong_model
            tr = f_tr if is_l2a else s_tr
            stage = 'l2a_predict_backfill' if is_l2a else 'l2b_predict_backfill'
            msgs = [
                {'role': 'system', 'content': L2_PREDICT_SYSTEM_PROMPT},
                {'role': 'user', 'content': blind_prompt},
            ]

            def _call_l2(m: str, t: Any) -> Any:
                return metered_llm(
                    track=track, stage=stage, model=m, messages=msgs,
                    session_id=session_id, max_output_tokens=220, conn=conn,
                    owner_id=owner_id, proposal_uid=uid, is_shadow=False,
                    budget=budget, transport=t,
                )

            try:
                res = _call_l2(model, tr)
            except LlmTransportError:
                if is_l2a or f_tr is tr:
                    raise
                model = flash_model
                res = _call_l2(flash_model, f_tr)
            pred_text = res.text.strip()
            toks = int(res.input_tokens) + int(res.output_tokens)
            new_meta: dict[str, Any] = {
                **meta,
                'blind_prompt': blind_prompt,
                'model_prediction': pred_text,
                'tokens_used': toks,
                'backfill_model': model,
                'backfilled': True,
            }
            if is_l2a:
                flash_pred = pred_text
            elif flash_pred and not new_meta.get('flash_prediction'):
                new_meta['flash_prediction'] = flash_pred
            conn.execute(
                'UPDATE proposal_decisions SET meta_json = %s::jsonb WHERE decision_id = %s',
                (json.dumps(new_meta, ensure_ascii=False), dec['decision_id']),
            )
            conn.commit()
            updated += 1
    return updated


def backfill_historical_decision_meta(
    conn_factory: Callable[[], Any],
    *,
    flash_model: str = 'deepseek-v4-flash',
    strong_model: str = 'deepseek-chat',
    flash_transport: Any = None,
    strong_transport: Any = None,
    max_workers: int = 6,
    budget: BudgetConfig | None = None,
) -> dict[str, Any]:
    """Backfill deterministic L0/L1/L3 metadata and metered L2a/L2b blind predictions."""
    l0_l1_l3_updated = _backfill_deterministic_layers(conn_factory)
    use_flash = flash_model
    use_strong = strong_model
    if flash_transport is None or strong_transport is None:
        endpoints = load_llm_endpoints_from_file()
        if flash_transport is None and 'flash' in endpoints:
            use_flash = endpoints['flash'].model
        if strong_transport is None:
            prefix = use_strong.split('-', 1)[0].casefold()
            if not any(ep.provider.casefold() == prefix for ep in endpoints.values()):
                s_ep = endpoints.get('strong') or endpoints.get('fallback') or endpoints.get('flash')
                if s_ep is not None:
                    use_strong = s_ep.model
    f_tr, s_tr, _j_tr = _resolve_transports(
        use_flash,
        use_strong,
        use_strong,
        flash_transport,
        strong_transport,
        strong_transport,
    )
    active_budget = budget or BudgetConfig(
        live_daily_tokens=5_000_000,
        backfill_daily_tokens=5_000_000,
        session_max_tokens=1_000_000,
    )

    with conn_factory() as conn:
        rows = conn.execute(
            """
            SELECT p.uid, p.owner_id, p.problem, p.scope, p.session_id, p.source_track,
                   d.decision_id, d.layer, d.judge_model, d.meta_json
            FROM experience_proposals p
            JOIN proposal_decisions d ON d.proposal_uid = p.uid
            WHERE d.layer IN ('L2a', 'L2b')
            ORDER BY p.created_at DESC, p.uid ASC, d.created_at ASC, d.decision_id ASC
            """
        ).fetchall()

    grouped: dict[str, dict[str, Any]] = {}
    for uid, owner_id, prob, scope_raw, sess_id, track, dec_id, lyr, jmodel, meta_raw in rows:
        entry = grouped.setdefault(
            uid,
            {
                'uid': uid,
                'owner_id': owner_id,
                'problem': str(prob or ''),
                'scope': _jval(scope_raw, {}) if isinstance(_jval(scope_raw, {}), dict) else {},
                'session_id': str(sess_id or ''),
                'track': str(track or 'live'),
                'decisions': [],
            },
        )
        meta_obj = _jval(meta_raw, {})
        entry['decisions'].append(
            {
                'decision_id': dec_id,
                'layer': lyr,
                'judge_model': str(jmodel or ''),
                'meta': meta_obj if isinstance(meta_obj, dict) else {},
            }
        )

    pending = [
        item
        for item in grouped.values()
        if any(not str((d['meta'] or {}).get('model_prediction') or '').strip() for d in item['decisions'])
    ]

    l2_decisions_updated = 0
    l2_errors = 0
    if pending:
        with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as pool:
            futs = [
                pool.submit(
                    _process_l2_proposal,
                    item,
                    conn_factory=conn_factory,
                    flash_model=use_flash,
                    strong_model=use_strong,
                    f_tr=f_tr,
                    s_tr=s_tr,
                    budget=active_budget,
                )
                for item in pending
            ]
            for fut in as_completed(futs):
                try:
                    l2_decisions_updated += int(fut.result())
                except Exception:
                    l2_errors += 1

    return {
        'l0_l1_l3_updated': l0_l1_l3_updated,
        'l2_proposals_processed': len(pending),
        'l2_decisions_updated': l2_decisions_updated,
        'l2_errors': l2_errors,
    }

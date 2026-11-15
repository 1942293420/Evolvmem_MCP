"""Stage 4 active legacy note full-text audit, re-distillation & ticketed remediation."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Callable

from evolvmem.experience_v2.gate.dedupe import _encode_text, normalize_vec768
from evolvmem.experience_v2.gate.rules import (
    CATEGORIES,
    _INCOMPLETE_BODY_RE,
    _SPECULATIVE_HEAD_RE,
    _TRUNCATED_TAIL_RE,
    _WS_NORM_RE,
    evaluate_l0_fields,
)
from evolvmem.experience_v2.gate.value import L2_PREDICT_SYSTEM_PROMPT, _build_blind_problem_prompt
from evolvmem.experience_v2.llm_meter import (
    BudgetConfig,
    LlmTransportError,
    load_llm_endpoints_from_file,
    metered_llm,
)
from evolvmem.experience_v2.pipeline.live_worker import _resolve_transports
from evolvmem.experience_v2.writer import ExperienceWriter, Op, TicketIssuer

_VALID_KINDS = frozenset({'env_fact', 'pitfall', 'procedure', 'preference', 'decision', 'habit'})
_REJECT_REASONS = frozenset({'weak_evidence', 'task_log', 'common_knowledge', 'one_off_bug'})
_FENCE_RE = re.compile(r'^\s*```(?:json)?\s*|\s*```\s*\Z', re.IGNORECASE)
_CANDIDATE_UNVERIFIED_RE = re.compile(r'(?:未验证优化效果|优化未实施|尚未实施优化)')

_REDISTILL_SYSTEM_PROMPT = (
    '你是资深工程知识审核与提炼专家。你将拿到一条旧库迁入记录的完整未截断原文，请完成「准入终审 + 完整三字段重提炼」。\n'
    '以下任一情况必须严格淘汰（keep=false）：\n'
    '1) reason_code="weak_evidence"：缺少最终定位的具体根因或已验证落地的具体解法/业务口径'
    '（例如仅列“需逐项分解排查范围/索引/重排开销”等空泛思路、仅记录耗时现象/对比报告、含“推测/只做核查未改代码/尚未实施/待排查/尚未安装或试跑”等半成品）；\n'
    '2) reason_code="task_log"：单次开发/部署/调UI/跑测试流水账（如“修改了X个文件，Y项测试通过，已完成部署”），无跨会话具体踩坑根因与通用工程规则；\n'
    '3) reason_code="common_knowledge"：任何有经验工程师无需看记录都知道的通用编程/数据库/前端常识；\n'
    '4) reason_code="one_off_bug"：仅一次手滑或拼写错误，无复用价值。\n'
    '仅当原文明确包含「已定位的具体根因 + 已实证有效的具体解法/私有核算口径/内部系统边界或精确参数」时，才设 keep=true 并提炼单行三字段：\n'
    '- problem（10~110字）：具体触发条件与问题现象，严禁省略号；\n'
    '- solution（16~225字）：必须完整概括原文前后半段的核心根因与已验证落地解法/精确口径/参数，严禁省略号或空话；\n'
    '- reusable_experience（12~110字）：提炼成一条独立可执行的判断铁律（触发某条件时必须怎么做/禁止怎么做），绝对禁止与 problem 相同或复读标题。\n'
    '仅输出严格单个 JSON 对象：'
    '{"keep": true或false, "reason_code": "weak_evidence|task_log|common_knowledge|one_off_bug", "detail": "<=100字判定理由", '
    '"problem": "...", "solution": "...", "reusable_experience": "..."}'
)


@dataclass(frozen=True)
class _AuditPlan:
    nid: str
    owner_id: str
    prop_uid: str
    legacy_id: str
    category: str
    kind: str
    misbelief: str
    scope: dict[str, Any]
    evidence: list[dict[str, Any]]
    revision: int
    old_problem: str
    old_solution: str
    old_reusable: str
    keep: bool
    layer: str
    reason_code: str
    detail: str
    judge_model: str
    new_problem: str = ''
    new_solution: str = ''
    new_reusable: str = ''
    meta_json: dict[str, Any] | None = None


def _jval(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return default
    return raw


def _parse_json_obj(raw_text: str) -> dict[str, Any]:
    cleaned = _FENCE_RE.sub('', raw_text or '').strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except ValueError:
        pass
    s, e = cleaned.find('{'), cleaned.rfind('}')
    if s != -1 and e > s:
        try:
            parsed = json.loads(cleaned[s : e + 1])
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    return {}


def _single_line(text: Any) -> str:
    return _WS_NORM_RE.sub(' ', str(text or '')).strip()


def _extract_legacy_id(scope: dict[str, Any], evidence: list[dict[str, Any]], nid: str) -> str:
    lid = str(scope.get('legacy_id') or '').strip()
    if lid:
        return lid
    for item in evidence:
        if isinstance(item, dict) and item.get('legacy_id'):
            return str(item['legacy_id']).strip()
    return nid


def _det_id(prefix: str, owner_id: str, seed: str) -> str:
    return f'{prefix}-{hashlib.sha256(f"{prefix}:{owner_id}:{seed}".encode("utf-8")).hexdigest()[:26]}'


def _pre_reject_reason(row: dict[str, Any], src: dict[str, Any]) -> tuple[str, str] | None:
    lstatus = str(src.get('legacy_status') or '').strip().lower()
    if lstatus and lstatus != 'active':
        return 'weak_evidence', f'旧库原始状态为 {lstatus}（未验证候选），缺少已验证结论'
    full_text = str(src.get('full_text') or '')
    blob = ' '.join(x for x in (row['problem'], row['solution'], row['reusable_experience'], full_text) if x)
    if _CANDIDATE_UNVERIFIED_RE.search(blob) or _INCOMPLETE_BODY_RE.search(blob):
        return 'weak_evidence', '原文含半程核查或优化未实施标记，缺少最终根因与已验证解法'
    if _SPECULATIVE_HEAD_RE.search(row['solution']) or _SPECULATIVE_HEAD_RE.search(row['reusable_experience']):
        return 'weak_evidence', '仅为未验证推测分析，缺少实证结论'
    if not src and (_TRUNCATED_TAIL_RE.search(row['solution']) or _TRUNCATED_TAIL_RE.search(row['reusable_experience'])):
        return 'weak_evidence', '文本在截断点残缺且无未截断原文可重提炼'
    return None


def _check_l0_pair(row: dict[str, Any], p: str, s: str, r: str) -> tuple[str, str | None, str]:
    return evaluate_l0_fields(
        {'category': row['category'], 'kind': row['kind'], 'problem': p, 'solution': s, 'reusable_experience': r, 'misbelief': row['misbelief']},
        require_evidence=False,
    )


def _llm_audit_one(
    row: dict[str, Any],
    src: dict[str, Any],
    *,
    conn_factory: Callable[[], Any],
    flash_model: str,
    flash_transport: Any,
    budget: BudgetConfig,
) -> _AuditPlan:
    nid, owner_id, legacy_id = row['nid'], row['owner_id'], row['legacy_id']
    title = str(src.get('title') or row['problem']).strip()
    full_text = str(src.get('full_text') or row['solution']).strip()
    src_table = str(src.get('source_table') or row['scope'].get('source_table') or 'legacy')
    src_cat = str(src.get('legacy_category') or row['category'])
    src_proj = str(src.get('project') or row['scope'].get('project') or '')

    user_msg = (
        f'legacy_id: {legacy_id} | 来源表: {src_table} | 原分类: {src_cat} | 项目: {src_proj}\n'
        f'原标题: {title}\n'
        f'当前截断/旧版记录:\n- problem: {row["problem"]}\n- solution: {row["solution"]}\n- reusable_experience: {row["reusable_experience"]}\n'
        f'未截断完整原文:\n{full_text[:2800]}'
    )

    def _call(stage: str, messages: list[dict[str, Any]], max_toks: int = 420) -> str:
        last_exc: Exception | None = None
        for _attempt in range(2):
            try:
                with conn_factory() as conn:
                    res = metered_llm(
                        track='backfill', stage=stage, model=flash_model, messages=messages,
                        session_id=f's4rem2:{legacy_id[:48]}', max_output_tokens=max_toks, conn=conn,
                        owner_id=owner_id, proposal_uid=row['prop_uid'] or None, is_shadow=False,
                        budget=budget, transport=flash_transport,
                    )
                    conn.commit()
                    return res.text
            except LlmTransportError as exc:
                last_exc = exc
        raise last_exc  # type: ignore[misc]

    raw_reply = _call('stage4_redistill', [{'role': 'system', 'content': _REDISTILL_SYSTEM_PROMPT}, {'role': 'user', 'content': user_msg}])
    obj = _parse_json_obj(raw_reply)
    keep = bool(obj.get('keep', False))
    rc = str(obj.get('reason_code') or 'weak_evidence').strip()
    rc = rc if rc in _REJECT_REASONS else 'weak_evidence'
    detail = _single_line(obj.get('detail') or '')[:200]

    if keep:
        cand_p, cand_s, cand_r = _single_line(obj.get('problem')), _single_line(obj.get('solution')), _single_line(obj.get('reusable_experience'))
        ok, err_rc, err_det = _check_l0_pair(row, cand_p, cand_s, cand_r)
        if ok != 'pass':
            retry_reply = _call(
                'stage4_redistill_retry',
                [
                    {'role': 'system', 'content': _REDISTILL_SYSTEM_PROMPT},
                    {'role': 'user', 'content': user_msg},
                    {'role': 'assistant', 'content': raw_reply},
                    {'role': 'user', 'content': f'你上次输出未通过 L0 规则（{err_rc}: {err_det}）。请压缩或改写为严格合规的 JSON，或若无具体根因/解法请改判 keep=false。'},
                ],
            )
            obj2 = _parse_json_obj(retry_reply)
            keep = bool(obj2.get('keep', False))
            rc2 = str(obj2.get('reason_code') or rc).strip()
            rc = rc2 if rc2 in _REJECT_REASONS else 'weak_evidence'
            detail = _single_line(obj2.get('detail') or detail)[:200]
            if keep:
                cand_p, cand_s, cand_r = _single_line(obj2.get('problem')), _single_line(obj2.get('solution')), _single_line(obj2.get('reusable_experience'))
                ok2, err_rc2, err_det2 = _check_l0_pair(row, cand_p, cand_s, cand_r)
                if ok2 != 'pass':
                    keep, rc, detail = False, 'weak_evidence', f'L0复核仍不合规({err_rc2}: {err_det2})'[:200]
        if keep:
            return _AuditPlan(
                nid=nid, owner_id=owner_id, prop_uid=row['prop_uid'], legacy_id=legacy_id,
                category=row['category'], kind=row['kind'], misbelief=row['misbelief'],
                scope=row['scope'], evidence=row['evidence'], revision=row['revision'],
                old_problem=row['problem'], old_solution=row['solution'], old_reusable=row['reusable_experience'],
                keep=True, layer='L2b', reason_code='', detail=detail or '完整原文重提炼并经 L0/L2 复核保留',
                judge_model=flash_model, new_problem=cand_p, new_solution=cand_s, new_reusable=cand_r,
            )

    layer = 'L2a' if rc == 'common_knowledge' else 'L0'
    meta: dict[str, Any] = {'remediated_from': 'stage4_legacy_v1', 'legacy_id': legacy_id}
    if layer == 'L2a':
        blind_prompt = _build_blind_problem_prompt(row['problem'], row['scope'])
        pred_text = _call(
            'l2a_predict_backfill',
            [{'role': 'system', 'content': L2_PREDICT_SYSTEM_PROMPT}, {'role': 'user', 'content': blind_prompt}],
            max_toks=220,
        )
        meta.update({'blind_prompt': blind_prompt, 'model_prediction': pred_text.strip(), 'backfill_model': flash_model})

    return _AuditPlan(
        nid=nid, owner_id=owner_id, prop_uid=row['prop_uid'], legacy_id=legacy_id,
        category=row['category'], kind=row['kind'], misbelief=row['misbelief'],
        scope=row['scope'], evidence=row['evidence'], revision=row['revision'],
        old_problem=row['problem'], old_solution=row['solution'], old_reusable=row['reusable_experience'],
        keep=False, layer=layer, reason_code=rc, detail=detail or f'Stage4 全文重审淘汰 ({rc})',
        judge_model=flash_model, meta_json=meta,
    )


def _ensure_proposal_ticket(conn, *, plan: _AuditPlan, issuer: TicketIssuer) -> tuple[str, Any]:
    owner_id, prop_uid = plan.owner_id, plan.prop_uid
    if prop_uid:
        row = conn.execute('SELECT uid FROM experience_proposals WHERE uid = %s AND owner_id = %s', (prop_uid, owner_id)).fetchone()
        if row is None:
            prop_uid = ''
    if not prop_uid:
        prop_uid = _det_id('prop', owner_id, f's4rem2:{plan.legacy_id}:{plan.nid}')
        payload_obj = {
            'category': plan.category, 'kind': plan.kind, 'problem': plan.old_problem,
            'solution': plan.old_solution, 'reusable_experience': plan.old_reusable,
            'misbelief': plan.misbelief, 'scope': plan.scope,
        }
        prop_uid = str(
            conn.execute(
                """
                INSERT INTO experience_proposals(
                    uid, owner_id, op, category, kind, problem, solution, reusable_experience,
                    misbelief, scope, payload_json, evidence_json, source_kind, source_track,
                    agent_client, model, actor_id, request_id, state
                ) VALUES (%s, %s, 'add', %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                          'legacy', 'backfill', 'stage4-remediate', 'stage4-remediate-v2', %s, %s, 'accepted')
                ON CONFLICT (owner_id, request_id) DO UPDATE SET updated_at = now()
                RETURNING uid
                """,
                (
                    prop_uid, owner_id, plan.category, plan.kind, plan.old_problem, plan.old_solution, plan.old_reusable,
                    plan.misbelief, json.dumps(plan.scope, ensure_ascii=False),
                    json.dumps(payload_obj, ensure_ascii=False), json.dumps(plan.evidence, ensure_ascii=False),
                    owner_id, f's4rem2-prop:{plan.nid}',
                ),
            ).fetchone()[0]
        )

    dec_row = conn.execute(
        "SELECT decision_id FROM proposal_decisions WHERE proposal_uid = %s AND verdict IN ('pass', 'merge') ORDER BY created_at DESC LIMIT 1",
        (prop_uid,),
    ).fetchone()
    pass_dec_id = str(dec_row[0]) if dec_row is not None else _det_id('dec', owner_id, f's4rem2-pass:{plan.nid}')
    if dec_row is None:
        conn.execute(
            """
            INSERT INTO proposal_decisions(decision_id, proposal_uid, layer, verdict, reason_code, confidence, judge_model, detail, target_note_id)
            VALUES (%s, %s, 'L4', 'pass', 'stage4_seed_pass', 1.0, 'user-gate', 'Stage4 seed ticket', %s)
            ON CONFLICT (decision_id) DO NOTHING
            """,
            (pass_dec_id, prop_uid, plan.nid),
        )
    return prop_uid, issuer.issue(proposal_uid=prop_uid, decision_id=pass_dec_id, lane='gated')


def _apply_plan(
    conn_factory: Callable[[], Any], *, plan: _AuditPlan, vec: list[float] | None, writer: ExperienceWriter, issuer: TicketIssuer,
) -> None:
    with conn_factory() as conn:
        prop_uid, ticket = _ensure_proposal_ticket(conn, plan=plan, issuer=issuer)
        if not plan.keep:
            writer.apply(
                conn, ticket,
                Op(action='retire', owner_id=plan.owner_id, actor_id=plan.owner_id,
                   request_id=f's4rem2-retire:{plan.nid}:r{plan.revision}', note_id=plan.nid,
                   expected_revision=plan.revision, origin='legacy'),
            )
            rej_dec_id = _det_id('dec', plan.owner_id, f's4rem2-rej:{plan.nid}')
            conn.execute(
                """
                INSERT INTO proposal_decisions(decision_id, proposal_uid, layer, verdict, reason_code, confidence, judge_model, detail, target_note_id, meta_json)
                VALUES (%s, %s, %s, 'reject', %s, 0.92, %s, %s, %s, %s::jsonb)
                ON CONFLICT (decision_id) DO UPDATE SET layer = EXCLUDED.layer, reason_code = EXCLUDED.reason_code,
                    judge_model = EXCLUDED.judge_model, detail = EXCLUDED.detail, meta_json = EXCLUDED.meta_json
                """,
                (rej_dec_id, prop_uid, plan.layer, plan.reason_code, plan.judge_model, plan.detail[:200], plan.nid, json.dumps(plan.meta_json or {}, ensure_ascii=False)),
            )
            conn.execute("UPDATE experience_proposals SET state = 'rejected', updated_at = now() WHERE uid = %s", (prop_uid,))
            conn.commit()
            return

        upd = writer.apply(
            conn, ticket,
            Op(action='update', owner_id=plan.owner_id, actor_id=plan.owner_id,
               request_id=f's4rem2-upd:{plan.nid}:r{plan.revision}', note_id=plan.nid,
               category=plan.category, problem=plan.new_problem, solution=plan.new_solution,
               reusable_experience=plan.new_reusable, expected_revision=plan.revision, embedding=vec, origin='legacy'),
        )
        next_rev = int(upd['experience']['revision'])
        new_scope = {**plan.scope, 'stage4_remediated_v2': 'true'}
        writer.apply(
            conn, ticket,
            Op(action='reconcile_state', owner_id=plan.owner_id, actor_id=plan.owner_id,
               request_id=f's4rem2-scope:{plan.nid}:r{next_rev}', note_id=plan.nid,
               expected_revision=next_rev, status='active', scope=new_scope, origin='legacy'),
        )
        payload_obj = {
            'category': plan.category, 'kind': plan.kind, 'problem': plan.new_problem, 'solution': plan.new_solution,
            'reusable_experience': plan.new_reusable, 'misbelief': plan.misbelief, 'scope': new_scope,
        }
        conn.execute(
            """
            UPDATE experience_proposals
            SET problem = %s, solution = %s, reusable_experience = %s, scope = %s::jsonb, payload_json = %s::jsonb, updated_at = now()
            WHERE uid = %s
            """,
            (plan.new_problem, plan.new_solution, plan.new_reusable, json.dumps(new_scope, ensure_ascii=False), json.dumps(payload_obj, ensure_ascii=False), prop_uid),
        )
        conn.commit()


def remediate_legacy_notes(
    conn_factory: Callable[[], Any],
    *,
    ticket_secret: bytes | str,
    full_sources: dict[str, dict[str, Any]],
    embedder: Any = None,
    batch_encode_fn: Callable[[list[str]], list[list[float]]] | None = None,
    flash_model: str = 'deepseek-v4-flash',
    strong_model: str = '',
    flash_transport: Any = None,
    strong_transport: Any = None,
    max_workers: int = 10,
    budget: BudgetConfig | None = None,
) -> dict[str, Any]:
    """Audit & re-distill active Stage 4 legacy notes against full un-truncated sources."""
    use_flash = flash_model or 'deepseek-v4-flash'
    if flash_transport is None:
        endpoints = load_llm_endpoints_from_file()
        if 'flash' in endpoints:
            use_flash = endpoints['flash'].model
    use_strong = strong_model or use_flash
    f_tr, _s_tr, _j_tr = _resolve_transports(use_flash, use_strong, use_strong, flash_transport, strong_transport, strong_transport)
    active_budget = budget or BudgetConfig(live_daily_tokens=10_000_000, backfill_daily_tokens=15_000_000, session_max_tokens=1_000_000)

    with conn_factory() as conn:
        raw_rows = conn.execute(
            """
            SELECT id, owner_id, proposal_uid, category, kind, problem, solution, reusable_experience, misbelief, scope, evidence, revision
            FROM experience_notes
            WHERE origin = 'legacy' AND deleted_at IS NULL AND status = 'active'
            ORDER BY updated_at DESC, id ASC
            """
        ).fetchall()

    already_remediated = 0
    pre_plans: list[_AuditPlan] = []
    llm_queue: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for r in raw_rows:
        scope_obj = _jval(r[9], {})
        scope_dict = scope_obj if isinstance(scope_obj, dict) else {}
        if str(scope_dict.get('stage4_remediated_v2') or '').lower() == 'true':
            already_remediated += 1
            continue
        ev_obj = _jval(r[10], [])
        ev_list = ev_obj if isinstance(ev_obj, list) else []
        legacy_id = _extract_legacy_id(scope_dict, ev_list, str(r[0]))
        cat = str(r[3]) if r[3] in CATEGORIES else '业务经验'
        kind = str(r[4]) if r[4] in _VALID_KINDS else 'procedure'
        row_dict = {
            'nid': str(r[0]), 'owner_id': str(r[1]), 'prop_uid': str(r[2] or ''), 'legacy_id': legacy_id,
            'category': cat, 'kind': kind, 'problem': str(r[5] or ''), 'solution': str(r[6] or ''),
            'reusable_experience': str(r[7] or ''), 'misbelief': str(r[8] or ''),
            'scope': scope_dict, 'evidence': ev_list, 'revision': int(r[11]),
        }
        src = full_sources.get(legacy_id, {})
        pre = _pre_reject_reason(row_dict, src)
        if pre is not None:
            rc, det = pre
            pre_plans.append(_AuditPlan(
                nid=row_dict['nid'], owner_id=row_dict['owner_id'], prop_uid=row_dict['prop_uid'], legacy_id=legacy_id,
                category=cat, kind=kind, misbelief=row_dict['misbelief'], scope=scope_dict, evidence=ev_list, revision=row_dict['revision'],
                old_problem=row_dict['problem'], old_solution=row_dict['solution'], old_reusable=row_dict['reusable_experience'],
                keep=False, layer='L0', reason_code=rc, detail=det, judge_model='rule-l0-remediate',
                meta_json={'remediated_from': 'stage4_legacy_v1', 'legacy_id': legacy_id, 'legacy_status': str(src.get('legacy_status') or '')},
            ))
        else:
            llm_queue.append((row_dict, src))

    plans: list[_AuditPlan] = list(pre_plans)
    errors = 0
    if llm_queue:
        with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as pool:
            futs = [
                pool.submit(_llm_audit_one, rd, sc, conn_factory=conn_factory, flash_model=use_flash, flash_transport=f_tr, budget=active_budget)
                for rd, sc in llm_queue
            ]
            for fut in as_completed(futs):
                try:
                    plans.append(fut.result())
                except Exception:
                    errors += 1

    keep_plans = [p for p in plans if p.keep]
    vecs_by_nid: dict[str, list[float] | None] = {p.nid: None for p in keep_plans}
    if keep_plans and (batch_encode_fn is not None or embedder is not None):
        texts = [f'{p.new_problem}\n{p.new_solution}\n{p.new_reusable}' for p in keep_plans]
        raw_vecs = batch_encode_fn(texts) if batch_encode_fn is not None else [_encode_text(embedder, t) for t in texts]
        for p, raw_v in zip(keep_plans, raw_vecs):
            vecs_by_nid[p.nid] = normalize_vec768(raw_v)

    writer = ExperienceWriter(ticket_secret)
    issuer = TicketIssuer(ticket_secret, gate_id='stage4-remediate-v2')
    retired = updated = 0
    for p in plans:
        try:
            _apply_plan(conn_factory, plan=p, vec=vecs_by_nid.get(p.nid), writer=writer, issuer=issuer)
            if p.keep:
                updated += 1
            else:
                retired += 1
        except Exception:
            errors += 1

    return {
        'scanned': len(raw_rows), 'already_remediated': already_remediated, 'pre_rejected': len(pre_plans),
        'llm_audited': len(llm_queue), 'retired': retired, 'updated': updated, 'errors': errors,
    }

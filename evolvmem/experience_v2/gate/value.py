"""Task 3.6: L2 Value Gate cascade (flash counterfactual test -> strong model blind check)."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable, Literal
import uuid

from evolvmem.experience_v2.llm_meter import BudgetConfig, estimate_tokens, metered_llm

STATIC_PROMPT_MAX_TOKENS = 600
BLIND_INPUT_MAX_TOKENS = 300
_FENCE_RE = re.compile(r'^\s*```(?:json)?\s*|\s*```\s*\Z', re.IGNORECASE)

L2_PREDICT_SYSTEM_PROMPT = (
    '你是资深工程师，正在参加盲测验证。下面只提供触发范围与问题现象，'
    '请直接给出你推断的核心根因与标准解决办法，要求简洁准确（<=160字），不要提问。'
)

L2_JUDGE_SYSTEM_PROMPT = (
    '你是客观盲测评审员，执行「结果完备性审查 + 只做对比参考答案与候选答案（模型盲测答案）的一致性判定」。\n'
    '- consistent 为 true（拦截不入库）涵盖三类情况：\n'
    '  0) 无结论/未验证半成品（reason_code="weak_evidence"）：参考解法未给出最终定位的具体根因与已验证落地的具体修复或明确口径（例如只列“逐项分解排查范围/索引/重排开销”等空泛清单、含“推测/尚未实施/只做核查未改代码/待排查”、或仅有耗时现象与施工汇报而无复用答案），即便夹带内部模块名或耗时数字也必须判 true 且 reason_code 填 "weak_evidence"！\n'
    '  1) 盲测已准确说出参考解法核心机制（忽略算sha256校验等顺带兜底细节，reason_code="common_knowledge"）；\n'
    '  2) 仅属通用编程/系统常识或Agent调工具语法参数手滑（reason_code="common_knowledge"）。\n'
    '- consistent 为 false（放行入库）：必须同时满足参考解法已给出完整具体的最终根因/解法，且包含盲测未猜出的具体事实：私有业务核算口径、内部系统真实边界、精确协议偏移或协作CLI规程。\n'
    '仅输出严格单个 JSON 对象：{"consistent": true或false, "reason_code": "common_knowledge或weak_evidence", "confidence": 0.0~1.0, "detail": "<=120字判定说明"}。'
)


@dataclass(frozen=True, slots=True)
class L2ValueOutcome:
    layer: Literal['L2a', 'L2b']
    verdict: Literal['pass', 'reject']
    reason_code: str | None
    confidence: float
    judge_model: str
    detail: str
    flash_prediction: str
    strong_prediction: str
    calls_used: int
    tokens_used: int
    decisions: tuple[dict[str, Any], ...]


def _clip_tokens(text: str, max_tokens: int) -> str:
    cur = str(text or '').strip()
    while estimate_tokens(cur) > max_tokens and len(cur) > 32:
        cur = cur[: int(len(cur) * 0.8)]
    return cur


def _build_blind_problem_prompt(problem: str, scope: dict[str, Any] | None) -> str:
    """Build blind R5 prompt containing ONLY scope + problem (never solution/misbelief), <=300 tokens."""
    scope_txt = json.dumps(scope or {}, ensure_ascii=False, sort_keys=True)
    if estimate_tokens(scope_txt) > 120:
        scope_txt = _clip_tokens(scope_txt, 100)
    raw = f'触发范围: {scope_txt}\n问题现象: {problem.strip()}'
    return _clip_tokens(raw, BLIND_INPUT_MAX_TOKENS)


def _parse_judge_json(raw_text: str) -> tuple[bool, float, str]:
    cleaned = _FENCE_RE.sub('', raw_text or '').strip()
    obj: dict[str, Any] = {}
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            obj = parsed
    except ValueError:
        s, e = cleaned.find('{'), cleaned.rfind('}')
        if s != -1 and e > s:
            try:
                parsed = json.loads(cleaned[s : e + 1])
                if isinstance(parsed, dict):
                    obj = parsed
            except ValueError:
                pass
    raw_c = obj.get('consistent', False)
    if isinstance(raw_c, bool):
        consistent = raw_c
    elif isinstance(raw_c, str):
        consistent = raw_c.strip().lower() in ('true', '1', 'yes')
    else:
        consistent = bool(raw_c)
    try:
        conf = max(0.0, min(1.0, float(obj.get('confidence', 0.5))))
    except (TypeError, ValueError):
        conf = 0.5
    detail = str(obj.get('detail') or '')[:200]
    return consistent, conf, detail


def _parse_judge_reason(raw_text: str) -> str:
    cleaned = _FENCE_RE.sub('', raw_text or '').strip()
    obj: dict[str, Any] = {}
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            obj = parsed
    except ValueError:
        s, e = cleaned.find('{'), cleaned.rfind('}')
        if s != -1 and e > s:
            try:
                parsed = json.loads(cleaned[s : e + 1])
                if isinstance(parsed, dict):
                    obj = parsed
            except ValueError:
                pass
    rc = str(obj.get('reason_code') or '').strip()
    return rc if rc in ('weak_evidence', 'task_log', 'common_knowledge', 'one_off_bug') else 'common_knowledge'


def _run_single_tier(
    conn,
    *,
    stage_prefix: Literal['l2a', 'l2b'],
    model: str,
    transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]],
    blind_prompt: str,
    solution: str,
    track: Literal['live', 'backfill'],
    session_id: str | None,
    owner_id: str | None,
    proposal_uid: str | None,
    is_shadow: bool,
    budget: BudgetConfig | None,
) -> tuple[str, bool, float, str, int]:
    pred_res = metered_llm(
        track=track,
        stage=f'{stage_prefix}_predict',
        model=model,
        messages=[
            {'role': 'system', 'content': L2_PREDICT_SYSTEM_PROMPT},
            {'role': 'user', 'content': blind_prompt},
        ],
        session_id=session_id,
        max_output_tokens=320,
        conn=conn,
        owner_id=owner_id,
        proposal_uid=proposal_uid,
        is_shadow=is_shadow,
        budget=budget,
        transport=transport,
    )
    judge_user = _clip_tokens(
        f'{blind_prompt}\n参考解法: {solution.strip()}\n模型盲测答案: {pred_res.text.strip()}',
        750,
    )
    judge_res = metered_llm(
        track=track,
        stage=f'{stage_prefix}_judge',
        model=model,
        messages=[
            {'role': 'system', 'content': L2_JUDGE_SYSTEM_PROMPT},
            {'role': 'user', 'content': judge_user},
        ],
        session_id=session_id,
        max_output_tokens=240,
        conn=conn,
        owner_id=owner_id,
        proposal_uid=proposal_uid,
        is_shadow=is_shadow,
        budget=budget,
        transport=transport,
    )
    consistent, conf, detail = _parse_judge_json(judge_res.text)
    reason = _parse_judge_reason(judge_res.text)
    if consistent and reason == 'weak_evidence' and not detail.startswith('[incomplete]'):
        detail = f'[incomplete] {detail}'[:200]
    return pred_res.text.strip(), consistent, conf, detail, (pred_res.total_tokens + judge_res.total_tokens)


def assess_value_cascade(
    conn,
    *,
    problem: str,
    solution: str,
    scope: dict[str, Any] | None = None,
    flash_model: str,
    strong_model: str,
    flash_transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]],
    strong_transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]] | None = None,
    track: Literal['live', 'backfill'] = 'live',
    session_id: str | None = None,
    owner_id: str | None = None,
    proposal_uid: str | None = None,
    is_shadow: bool = False,
    budget: BudgetConfig | None = None,
) -> L2ValueOutcome:
    """Cascade counterfactual value test: L2a (flash) -> L2b (strong model only on L2a miss)."""
    if estimate_tokens(L2_PREDICT_SYSTEM_PROMPT) > STATIC_PROMPT_MAX_TOKENS or estimate_tokens(L2_JUDGE_SYSTEM_PROMPT) > STATIC_PROMPT_MAX_TOKENS:
        raise ValueError('static_prompt_exceeds_600_tokens')

    blind_prompt = _build_blind_problem_prompt(problem, scope)
    flash_pred, flash_hit, flash_conf, flash_detail, flash_toks = _run_single_tier(
        conn,
        stage_prefix='l2a',
        model=flash_model,
        transport=flash_transport,
        blind_prompt=blind_prompt,
        solution=solution,
        track=track,
        session_id=session_id,
        owner_id=owner_id,
        proposal_uid=proposal_uid,
        is_shadow=is_shadow,
        budget=budget,
    )
    if flash_hit:
        f_reason = 'weak_evidence' if flash_detail.startswith('[incomplete]') else 'common_knowledge'
        f_clean_det = flash_detail[len('[incomplete] '):] if flash_detail.startswith('[incomplete] ') else flash_detail
        dec_l2a = {
            'layer': 'L2a',
            'verdict': 'reject',
            'reason_code': f_reason,
            'confidence': flash_conf,
            'judge_model': flash_model,
            'detail': (f_clean_det or ('缺少完整根因与已验证解法' if f_reason == 'weak_evidence' else 'flash 盲测直接命中，判为通用常识'))[:200],
            'meta': {'blind_prompt': blind_prompt, 'model_prediction': flash_pred, 'tokens_used': flash_toks},
        }
        return L2ValueOutcome(
            layer='L2a',
            verdict='reject',
            reason_code=f_reason,
            confidence=flash_conf,
            judge_model=flash_model,
            detail=dec_l2a['detail'],
            flash_prediction=flash_pred,
            strong_prediction='',
            calls_used=2,
            tokens_used=flash_toks,
            decisions=(dec_l2a,),
        )

    dec_l2a_pass = {
        'layer': 'L2a',
        'verdict': 'pass',
        'reason_code': None,
        'confidence': flash_conf,
        'judge_model': flash_model,
        'detail': (flash_detail or 'flash 盲测未命中，移交强模型复核')[:200],
        'meta': {'blind_prompt': blind_prompt, 'model_prediction': flash_pred, 'tokens_used': flash_toks},
    }
    strong_pred, strong_hit, strong_conf, strong_detail, strong_toks = _run_single_tier(
        conn,
        stage_prefix='l2b',
        model=strong_model,
        transport=strong_transport or flash_transport,
        blind_prompt=blind_prompt,
        solution=solution,
        track=track,
        session_id=session_id,
        owner_id=owner_id,
        proposal_uid=proposal_uid,
        is_shadow=is_shadow,
        budget=budget,
    )
    if strong_hit:
        s_reason = 'weak_evidence' if strong_detail.startswith('[incomplete]') else 'common_knowledge'
        s_clean_det = strong_detail[len('[incomplete] '):] if strong_detail.startswith('[incomplete] ') else strong_detail
        dec_l2b = {
            'layer': 'L2b',
            'verdict': 'reject',
            'reason_code': s_reason,
            'confidence': strong_conf,
            'judge_model': strong_model,
            'detail': (s_clean_det or ('缺少完整根因与已验证解法' if s_reason == 'weak_evidence' else '强模型盲测命中，模型本身可推导出解法'))[:200],
            'meta': {'blind_prompt': blind_prompt, 'model_prediction': strong_pred, 'flash_prediction': flash_pred, 'tokens_used': strong_toks},
        }
        return L2ValueOutcome(
            layer='L2b',
            verdict='reject',
            reason_code=s_reason,
            confidence=strong_conf,
            judge_model=strong_model,
            detail=dec_l2b['detail'],
            flash_prediction=flash_pred,
            strong_prediction=strong_pred,
            calls_used=4,
            tokens_used=flash_toks + strong_toks,
            decisions=(dec_l2a_pass, dec_l2b),
        )

    dec_l2b_pass = {
        'layer': 'L2b',
        'verdict': 'pass',
        'reason_code': None,
        'confidence': strong_conf,
        'judge_model': strong_model,
        'detail': (strong_detail or '强模型盲测未命中，确认为高价值私有/经验知识')[:200],
        'meta': {'blind_prompt': blind_prompt, 'model_prediction': strong_pred, 'flash_prediction': flash_pred, 'tokens_used': strong_toks},
    }
    return L2ValueOutcome(
        layer='L2b',
        verdict='pass',
        reason_code=None,
        confidence=strong_conf,
        judge_model=strong_model,
        detail=dec_l2b_pass['detail'],
        flash_prediction=flash_pred,
        strong_prediction=strong_pred,
        calls_used=4,
        tokens_used=flash_toks + strong_toks,
        decisions=(dec_l2a_pass, dec_l2b_pass),
    )


def run_l2_value_gate(
    conn,
    proposal_uid: str,
    *,
    flash_model: str,
    strong_model: str,
    flash_transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]],
    strong_transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]] | None = None,
    gate_mode: str | dict[str, str] = 'review',
    budget: BudgetConfig | None = None,
) -> dict[str, Any]:
    """Run L2a->L2b value gate on an existing proposal and record proposal_decisions."""
    row = conn.execute(
        """
        SELECT owner_id, category, problem, solution, scope, session_id, source_track
        FROM experience_proposals
        WHERE uid = %s
        FOR UPDATE
        """,
        (proposal_uid,),
    ).fetchone()
    if row is None:
        raise ValueError(f'proposal_not_found: {proposal_uid}')

    owner_id, _category, problem, solution, scope_raw, session_id, track = row
    scope = json.loads(scope_raw) if isinstance(scope_raw, str) else (scope_raw or {})
    outcome = assess_value_cascade(
        conn,
        problem=problem,
        solution=solution,
        scope=scope,
        flash_model=flash_model,
        strong_model=strong_model,
        flash_transport=flash_transport,
        strong_transport=strong_transport,
        track=track,
        session_id=session_id,
        owner_id=owner_id,
        proposal_uid=proposal_uid,
        is_shadow=False,
        budget=budget,
    )

    last_decision_id = ''
    with conn.transaction():
        for dec in outcome.decisions:
            last_decision_id = f"dec-{dec['layer'].lower()}-{uuid.uuid4().hex[:18]}"
            conn.execute(
                """
                INSERT INTO proposal_decisions(
                    decision_id, proposal_uid, layer, verdict, reason_code, confidence, judge_model, detail, meta_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    last_decision_id,
                    proposal_uid,
                    dec['layer'],
                    dec['verdict'],
                    dec['reason_code'],
                    dec['confidence'],
                    dec['judge_model'],
                    dec['detail'][:200],
                    json.dumps(dec.get('meta') or {}, ensure_ascii=False),
                ),
            )
        new_state = 'rejected' if outcome.verdict == 'reject' else 'review'
        conn.execute(
            'UPDATE experience_proposals SET state = %s, updated_at = now() WHERE uid = %s',
            (new_state, proposal_uid),
        )

    return {
        'decision_id': last_decision_id,
        'layer': outcome.layer,
        'verdict': outcome.verdict,
        'reason_code': outcome.reason_code,
        'confidence': outcome.confidence,
        'judge_model': outcome.judge_model,
        'detail': outcome.detail,
        'calls_used': outcome.calls_used,
        'tokens_used': outcome.tokens_used,
    }

"""Metered experience extractor converting compressed KeyMoments to §5.3 JSON (Task 3.5)."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable, Literal, Sequence

from evolvmem.experience_v2.llm_meter import BudgetConfig, estimate_tokens, metered_llm
from evolvmem.experience_v2.pipeline.history_reader import HistoryReader
from evolvmem.experience_v2.pipeline.moments import KeyMoment
from evolvmem.experience_v2.writer import CATEGORIES

STATIC_PROMPT_MAX_TOKENS = 600
MOMENT_INPUT_MAX_TOKENS = 3000
VALID_EXTRACT_KINDS = frozenset({'env_fact', 'pitfall', 'procedure', 'preference', 'decision'})
_FENCE_RE = re.compile(r'^\s*```(?:json)?\s*|\s*```\s*\Z', re.IGNORECASE)
_WS_RE = re.compile(r'\s+')

EXTRACT_SYSTEM_PROMPT = (
    '你是经验提炼器，只从单个关键时刻片段提炼可复用工程教训，禁止输出任务流水账。\n'
    '【值得记】：私有环境知识（内部系统/平台行为、拓扑、边界）、走过的死路（尝试失败并放弃的方案及原因）、'
    '用户的品味与偏好、决策理由（为什么选A不选B）、跨会话反复出现的问题与误区。\n'
    '【不值得记】：通用编程常识（common_knowledge）、单次会话任务日志（task_log，严禁用「用户要求」「用户询问」「已完成」开头）、'
    '已修复并提交到 git 的一次性局部行号 bug（one_off_bug）。\n'
    '不值得记时必须输出 skip_reason 说明原因并将前面字段留空，允许并鼓励输出 skip_reason。\n'
    '仅输出严格单个 JSON 对象（UTF-8）：'
    '{"category":"前端经验|业务经验|系统环境经验","problem":"<=40字触发情境与现象",'
    '"solution":"<=90字真相与做法","reusable_experience":"<=50字可迁移教训",'
    '"kind":"env_fact|pitfall|procedure|preference|decision","misbelief":"<=80字当时误以为何，无则空串",'
    '"scope":{"system":"...","os":"..."},"skip_reason":"不值得记时填原因，否则空串"}。'
)


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    moment_id: str
    skipped: bool
    skip_reason: str
    payload: dict[str, Any]
    tokens_used: int
    call_id: int | None = None


class HistoryReaderEvidenceVerifier:
    """HistoryReader-backed EvidenceVerifier compatible with L0 gate without SQLite coupling."""

    def __init__(self, reader: HistoryReader) -> None:
        self._reader = reader

    def verify_all(self, evidence: list[dict[str, Any]]) -> tuple[bool, str]:
        if not isinstance(evidence, list) or not evidence:
            return False, '缺少历史会话证据指针'
        for item in evidence:
            if not self._reader.verify_evidence_pointer(item):
                src = item.get('source') if isinstance(item, dict) else ''
                return False, f'历史证据指针不可核验: {str(src)[:64]}'
        return True, 'ok'


def _clean_line(text: Any, limit: int) -> str:
    single = _WS_RE.sub(' ', str(text or '')).strip()
    return single[:limit]


def _clip_tokens(text: str, max_tokens: int) -> str:
    cur = text.strip()
    while estimate_tokens(cur) > max_tokens and len(cur) > 64:
        cur = cur[: int(len(cur) * 0.8)]
    return cur


def _parse_json_object(raw_text: str) -> dict[str, Any] | None:
    stripped = _FENCE_RE.sub('', raw_text or '').strip()
    if not stripped:
        return None
    try:
        obj = json.loads(stripped)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        start = stripped.find('{')
        end = stripped.rfind('}')
        if start != -1 and end > start:
            try:
                obj = json.loads(stripped[start : end + 1])
                return obj if isinstance(obj, dict) else None
            except ValueError:
                return None
    return None


def _resolve_verified_evidence(
    moment: KeyMoment,
    *,
    library: str,
    valid_record_indices: Sequence[int],
) -> list[dict[str, Any]]:
    src = moment.session_id
    ev_num = max(1, int(moment.event_range[0]) if moment.event_range else 1)
    if moment.evidence_pointers:
        ptr = moment.evidence_pointers[0]
        src = str(ptr.get('source_ref') or ptr.get('source') or src)
        raw_range = ptr.get('event_range')
        if isinstance(raw_range, (list, tuple)) and raw_range:
            ev_num = max(1, int(raw_range[0]) or 1)
    if valid_record_indices:
        ordered = sorted({int(i) for i in valid_record_indices if int(i) >= 1})
        if ordered and ev_num not in ordered:
            ev_num = ordered[0]
    return [{'library': library or 'default', 'source': src, 'event': int(ev_num)}]


def extract_moment(
    conn,
    moment: KeyMoment,
    *,
    model: str,
    transport: Callable[[str, list[dict[str, Any]], int], dict[str, Any]],
    track: Literal['live', 'backfill'] = 'live',
    owner_id: str | None = None,
    library: str = 'default',
    valid_record_indices: Sequence[int] = (),
    is_shadow: bool = False,
    budget: BudgetConfig | None = None,
    max_output_tokens: int = 600,
) -> ExtractionResult:
    """Extract at most one structured experience (§5.3) from a bounded KeyMoment snippet."""
    if estimate_tokens(EXTRACT_SYSTEM_PROMPT) > STATIC_PROMPT_MAX_TOKENS:
        raise ValueError('static_prompt_exceeds_600_tokens')

    user_content = _clip_tokens(
        f'关键时刻类型: {moment.kind}\n标题: {moment.title}\n片段:\n{moment.snippet_text}',
        MOMENT_INPUT_MAX_TOKENS,
    )
    messages = [
        {'role': 'system', 'content': EXTRACT_SYSTEM_PROMPT},
        {'role': 'user', 'content': user_content},
    ]
    llm_res = metered_llm(
        track=track,
        stage='extract',
        model=model,
        messages=messages,
        session_id=moment.session_id,
        max_output_tokens=max_output_tokens,
        conn=conn,
        owner_id=owner_id,
        is_shadow=is_shadow,
        budget=budget,
        transport=transport,
    )
    obj = _parse_json_object(llm_res.text)
    if obj is None:
        return ExtractionResult(
            moment_id=moment.moment_id,
            skipped=True,
            skip_reason='invalid_json_output',
            payload={},
            tokens_used=llm_res.total_tokens,
            call_id=llm_res.call_id,
        )

    skip_reason = _clean_line(obj.get('skip_reason'), 120)
    problem = _clean_line(obj.get('problem'), 40)
    solution = _clean_line(obj.get('solution'), 90)
    reusable = _clean_line(obj.get('reusable_experience'), 50)
    if skip_reason or not (problem and solution and reusable):
        return ExtractionResult(
            moment_id=moment.moment_id,
            skipped=True,
            skip_reason=skip_reason or 'empty_core_fields',
            payload={},
            tokens_used=llm_res.total_tokens,
            call_id=llm_res.call_id,
        )

    category = str(obj.get('category') or '').strip()
    if category not in CATEGORIES:
        category = '系统环境经验'
    kind = str(obj.get('kind') or '').strip()
    if kind not in VALID_EXTRACT_KINDS:
        kind = 'pitfall'
    misbelief = _clean_line(obj.get('misbelief'), 80)
    scope = obj.get('scope') if isinstance(obj.get('scope'), dict) else {}
    evidence = _resolve_verified_evidence(moment, library=library, valid_record_indices=valid_record_indices)

    payload = {
        'category': category,
        'problem': problem,
        'solution': solution,
        'reusable_experience': reusable,
        'kind': kind,
        'misbelief': misbelief,
        'scope': scope,
        'evidence': evidence,
        'skip_reason': '',
    }
    return ExtractionResult(
        moment_id=moment.moment_id,
        skipped=False,
        skip_reason='',
        payload=payload,
        tokens_used=llm_res.total_tokens,
        call_id=llm_res.call_id,
    )


def extract_session_moments(
    conn,
    moments: Sequence[KeyMoment],
    **kwargs: Any,
) -> list[ExtractionResult]:
    """R2 fast path: 0-moment sessions make 0 LLM calls; others extract per KeyMoment."""
    if not moments:
        return []
    return [extract_moment(conn, m, **kwargs) for m in moments]

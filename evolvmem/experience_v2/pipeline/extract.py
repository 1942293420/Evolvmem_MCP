"""Metered experience extractor converting compressed KeyMoments to §5.3 JSON (Task 3.5)."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Callable, Literal, Sequence

from evolvmem.experience_v2.gate.rules import L0_LIMITS
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
    '你是高标准经验提炼器，严禁流水账或牵强拔高。先判是否不值得记（若是，skip_reason填「类型:原因」，其余字段填""）：\n'
    '【不值得记】：\n'
    '- tool_fumble：Agent调工具/写临时脚本的手滑与语法参数失误（apply_patch行不匹配或同路径Delete+Add、'
    '内联命令超长os error 206、引号/String.raw截断SyntaxError、直接JSON.parse报错文本、'
    '复合命令退出码与路径不一致、GUI自动化getAXState/坐标点击找开关）；\n'
    '- task_log：单次盘点/核查统计汇报（如核查40个项目、23找到/15未找到，或禁「用户要求/询问/已完成」开头）；\n'
    '- common_knowledge：通用编程/系统常识；\n'
    '- one_off_bug：已修局部行号笔误。\n'
    '【值得记】：1) 私有业务口径与分摊归因规则；2) 内部系统真实生产状态（如仍连旧库）与切换边界；'
    '3) 二进制包精确字节偏移与检索规程；4) 动态协作数据源（多维表）配内部CLI的版本diff规程；5) 用户明确纠偏或偏好。\n'
    '【防剧透铁律】：problem只写「任务场景+受阻表象或待决策问题」（<=100字），勿剧透事后根因、用户纠正结论或具体解法！\n'
    '仅输出单个UTF-8 JSON（完整成句）：'
    '{"skip_reason":"不值得记填类型:原因，值得记填\"\"","category":"前端经验|业务经验|系统环境经验",'
    '"problem":"<=100字场景与表象(勿剧透根因结论解法)","solution":"<=200字真实根因与具体做法",'
    '"reusable_experience":"<=100字带适用前提的规则","kind":"env_fact|pitfall|procedure|preference|decision",'
    '"misbelief":"<=140字当时误以为何，无则\"\"","scope":{"system":"...","os":"..."}}。'
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
    if len(single) <= limit:
        return single
    cut = single[:limit]
    floor = max(1, int(limit * 0.65))
    for sep in ('。', '；', '，', ';', ',', ' '):
        pos = cut.rfind(sep)
        if pos >= floor:
            return cut[:pos].rstrip(' ，,;；。. ')
    if cut[-1].isascii() and cut[-1].isalnum() and len(single) > limit and single[limit].isascii() and single[limit].isalnum():
        trimmed = re.sub(r'[A-Za-z0-9_]+$', '', cut).rstrip(' ，,;；.-_')
        if len(trimmed) >= floor:
            return trimmed
    return cut.rstrip(' ，,;；。. ')


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
            ev_num = min(ordered, key=lambda x: abs(x - ev_num))
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
    max_output_tokens: int = 800,
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

    skip_reason = _clean_line(obj.get('skip_reason'), 160)
    problem = _clean_line(obj.get('problem'), L0_LIMITS['problem'])
    solution = _clean_line(obj.get('solution'), L0_LIMITS['solution'])
    reusable = _clean_line(obj.get('reusable_experience'), L0_LIMITS['reusable_experience'])
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
    misbelief = _clean_line(obj.get('misbelief'), L0_LIMITS['misbelief'])
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

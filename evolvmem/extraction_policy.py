"""Pure policies for redacting extraction inputs and summaries."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass

from evolvmem.auto_extractor import CandidateMemory


_REDACTION_MARKER_RE = re.compile(
    r"\[已脱敏:(?:private_key|url凭据|token|api_key|password|凭据位置)\]"
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?P<kind>(?:[A-Z0-9 ]+ )?PRIVATE KEY)-----.*?"
    r"(?:-----END (?P=kind)-----|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_URL_CREDENTIAL_RE = re.compile(
    r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\bBearer\s+[^\s,，。；;]{2,}", re.IGNORECASE)
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
)

_NAMESPACE_RE = r"(?:[A-Za-z][A-Za-z0-9]*_)*"
_API_KEY_LABEL_RE = (
    rf"(?:{_NAMESPACE_RE}(?:API_KEY|SECRET_KEY)|"
    r"api[_ -]?key|secret[_ -]?key|[\u3400-\u9fff]{0,4}(?:密钥|秘钥))"
)
_TOKEN_LABEL_RE = (
    rf"(?:{_NAMESPACE_RE}(?:ACCESS_TOKEN|REFRESH_TOKEN|TOKEN)|"
    r"access[_ -]?token|refresh[_ -]?token|token|令牌)"
)
_PASSWORD_LABEL_RE = (
    rf"(?:{_NAMESPACE_RE}(?:PASSWORD|PASSWD|PWD)|"
    r"password|passwd|pwd|[\u3400-\u9fff]{0,4}(?:密码|口令))"
)
_LOCATION_LABEL_RE = (
    r"(?:api[_ -]?key|secret[_ -]?key|password|passwd|pwd|"
    r"tokens?|credentials?|密码|口令|密钥|秘钥|令牌)"
)


def _compile_assignment(label: str) -> re.Pattern[str]:
    return re.compile(
        rf"(?<![\w])(?P<key_quote>[\"'])?(?P<label>{label})"
        rf"(?(key_quote)(?P=key_quote))\s*"
        rf"(?P<operator>set\s+to|=|:|is|是|为)\s*"
        rf"(?:"
        rf"(?P<value_quote>[\"'])(?P<quoted_value>[^\r\n]*?)"
        rf"(?P=value_quote)|"
        rf"(?P<bare_value>[^\s,，。；;}}\]\"']+)"
        rf")",
        re.IGNORECASE,
    )


_CREDENTIAL_LOCATION_RE = re.compile(
    rf"(?<![\w])(?P<label>{_LOCATION_LABEL_RE})\s*"
    r"(?:(?:is|are)\s+)?"
    r"(?:stored?|saved?|located?|written?|kept|存放|保存|位于|写入)\s*"
    r"(?:在|于|to|at|in)?\s*"
    r"(?:"
    r"(?P<value_quote>[\"'])(?P<quoted_value>[^\r\n]*?)"
    r"(?P=value_quote)|"
    r"(?P<bare_value>[^\s,，。；;}}\]\"']+)"
    r")",
    re.IGNORECASE,
)

_POLICY_PREDICATES = frozenset({
    "required",
    "requirement",
    "enabled",
    "disabled",
    "recommended",
    "optional",
    "mandatory",
    "needed",
    "enforced",
    "configured",
    "managed",
    "rotated",
    "必填",
    "必填项",
    "必需",
    "必须",
    "必要",
    "启用",
    "禁用",
    "受控",
    "轮换",
})

_Replacement = str | Callable[[re.Match[str]], str | None]


def _assignment_replacement(marker: str) -> Callable[[re.Match[str]], str | None]:
    def replace(match: re.Match[str]) -> str | None:
        operator = match.group("operator").casefold()
        bare_value = match.group("bare_value")
        if "\ue000" in match.group(0):
            return None
        if (
            bare_value is not None
            and bare_value.casefold() == "bearer"
            and match.string[match.end():].startswith("\ue000")
        ):
            return None
        if operator in {"is", "是", "为"} and bare_value is not None:
            predicate = bare_value.casefold().rstrip(".!?")
            if predicate in _POLICY_PREDICATES:
                return None
        return marker

    return replace


_SENSITIVE_RULES: tuple[tuple[re.Pattern[str], _Replacement], ...] = (
    (_PRIVATE_KEY_RE, "[已脱敏:private_key]"),
    (
        _URL_CREDENTIAL_RE,
        lambda match: f"{match.group('scheme')}[已脱敏:url凭据]@",
    ),
    (_BEARER_RE, "Bearer [已脱敏:token]"),
    (_JWT_RE, "[已脱敏:token]"),
    (_CREDENTIAL_LOCATION_RE, "凭据[已脱敏:凭据位置]"),
    (
        _compile_assignment(_API_KEY_LABEL_RE),
        _assignment_replacement("api_key=[已脱敏:api_key]"),
    ),
    (
        _compile_assignment(_TOKEN_LABEL_RE),
        _assignment_replacement("token=[已脱敏:token]"),
    ),
    (
        _compile_assignment(_PASSWORD_LABEL_RE),
        _assignment_replacement("password=[已脱敏:password]"),
    ),
)

_EPHEMERAL_PATTERNS = (
    re.compile(r"(?:等待|稍后|之后).{0,12}(?:用户|输入|确认)"),
    re.compile(r"(?:临时|暂时|一次性).{0,12}(?:密码|设置|测试|任务)"),
    re.compile(r"(?:本次|此次|刚刚)?.{0,12}(?:测试|用例).{0,12}(?:通过|成功|完成|\d+\s*个)"),
    re.compile(r"(?:本次|此次|刚刚)?.{0,12}(?:部署|发布|上线).{0,8}(?:通过|成功|完成|结束)"),
    re.compile(r"(?:commit|提交)\s*[0-9a-f]{7,40}", re.IGNORECASE),
)
_CLAUSE_SPLIT_RE = re.compile(r"[，,。；;！？!?\n]+|(?:但是|并且|而且|同时|但)")
_ONGOING_SIGNAL_RE = re.compile(
    r"后续|未来|长期|持续|永久|始终|每次|必须|禁止|不得|应当|"
    r"需要|只能|决定|采用|保留|因为|原因|防止|避免|以免"
)
_DURABLE_BOILERPLATE_RE = re.compile(
    r"这是|一项|长期|持续|永久|有效|重要|规则|决定|原因|约束|因为|防止|避免"
)
_LOW_INFORMATION_PREFIXES = (
    "等待用户",
    "会话继续",
    "等待下一步",
    "等待用户确认",
    "等待用户后续",
    "no action required",
)

_ALLOWED_ATTRIBUTES = frozenset({
    "decision",
    "preference",
    "fact",
    "constraint",
    "user_profile",
})
_ALLOWED_TIERS = frozenset({"pinned", "normal", "reference"})
_STABLE_KEY_RE = re.compile(r"[\w\u3400-\u9fff-]+(?::[\w\u3400-\u9fff-]+){3}")
_TAG_RE = re.compile(r"[^,\r\n\x00-\x1f\x7f]{1,64}")
_MAX_KEY_CHARS = 200
_MAX_TAGS = 8


@dataclass(frozen=True)
class PolicyDecision:
    accepted: bool
    reason: str = ""


def contains_cjk(text: str) -> bool:
    """Return whether *text* includes a CJK ideograph."""
    return re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text) is not None


def contains_sensitive_text(text: str) -> bool:
    """Return whether *text* contains a value that should be redacted."""
    if not isinstance(text, str):
        return False
    _, count = _redact_text(text)
    return count > 0


def _has_substantive_ongoing_clause(value: str) -> bool:
    for clause in _CLAUSE_SPLIT_RE.split(value):
        clause = clause.strip()
        if not clause or any(pattern.search(clause) for pattern in _EPHEMERAL_PATTERNS):
            continue
        if not _ONGOING_SIGNAL_RE.search(clause):
            continue
        substance = _DURABLE_BOILERPLATE_RE.sub("", clause)
        substance = re.sub(r"[^\w\u3400-\u9fff]", "", substance)
        if len(substance) >= 6:
            return True
    return False


def _metadata_decision(candidate: CandidateMemory) -> PolicyDecision | None:
    if not isinstance(candidate.key, str) or not isinstance(candidate.value, str):
        return PolicyDecision(False, "metadata")
    if not isinstance(candidate.attribute, str) or not isinstance(candidate.tier, str):
        return PolicyDecision(False, "metadata")
    if not isinstance(candidate.tags, list) or any(
        not isinstance(tag, str) for tag in candidate.tags
    ):
        return PolicyDecision(False, "metadata")

    metadata_strings = [
        candidate.key,
        candidate.attribute,
        candidate.tier,
        *candidate.tags,
    ]
    if any(contains_sensitive_text(text) for text in metadata_strings):
        return PolicyDecision(False, "sensitive")
    if not (5 <= len(candidate.key) <= _MAX_KEY_CHARS):
        return PolicyDecision(False, "metadata")
    if _STABLE_KEY_RE.fullmatch(candidate.key) is None:
        return PolicyDecision(False, "metadata")
    if candidate.attribute not in _ALLOWED_ATTRIBUTES:
        return PolicyDecision(False, "metadata")
    if candidate.tier not in _ALLOWED_TIERS:
        return PolicyDecision(False, "metadata")
    if len(candidate.tags) > _MAX_TAGS or any(
        tag != tag.strip() or _TAG_RE.fullmatch(tag) is None
        for tag in candidate.tags
    ):
        return PolicyDecision(False, "metadata")
    try:
        importance = float(candidate.importance)
    except (TypeError, ValueError):
        return PolicyDecision(False, "metadata")
    if not math.isfinite(importance) or not 1.0 <= importance <= 10.0:
        return PolicyDecision(False, "metadata")
    return None


def evaluate_candidate(
    candidate: CandidateMemory,
    *,
    value_min_chars: int = 10,
    value_max_chars: int = 500,
) -> PolicyDecision:
    """Apply every deterministic pre-persistence gate to one candidate."""
    metadata_rejection = _metadata_decision(candidate)
    if metadata_rejection is not None:
        return metadata_rejection

    value = candidate.value.strip()
    if contains_sensitive_text(value):
        return PolicyDecision(False, "sensitive")
    if not contains_cjk(value):
        return PolicyDecision(False, "language")
    is_ephemeral = any(pattern.search(value) for pattern in _EPHEMERAL_PATTERNS)
    if is_ephemeral and not _has_substantive_ongoing_clause(value):
        return PolicyDecision(False, "ephemeral")
    try:
        confidence = float(candidate.confidence)
    except (TypeError, ValueError):
        return PolicyDecision(False, "confidence")
    if not math.isfinite(confidence) or not 0.3 <= confidence <= 1.0:
        return PolicyDecision(False, "confidence")
    if not value_min_chars <= len(value) <= value_max_chars:
        return PolicyDecision(False, "length")
    if any(
        value.casefold().startswith(prefix.casefold())
        for prefix in _LOW_INFORMATION_PREFIXES
    ):
        return PolicyDecision(False, "low_information")
    return PolicyDecision(True)


def _quality_tuple(
    candidate: CandidateMemory, original_index: int
) -> tuple[bool, float, float, int]:
    return (
        candidate.tier == "pinned",
        candidate.importance,
        candidate.confidence,
        -original_index,
    )


def rank_candidates(
    candidates: list[CandidateMemory], limit: int | None = 8
) -> list[CandidateMemory]:
    """Deduplicate candidates by key, rank by quality, and apply *limit*."""
    best_by_key: dict[str, tuple[CandidateMemory, int]] = {}
    for index, candidate in enumerate(candidates):
        normalized_key = candidate.key.strip().casefold()
        current = best_by_key.get(normalized_key)
        if current is None or _quality_tuple(candidate, index) > _quality_tuple(*current):
            best_by_key[normalized_key] = (candidate, index)

    ranked = sorted(
        best_by_key.values(),
        key=lambda item: _quality_tuple(*item),
        reverse=True,
    )
    return [candidate for candidate, _ in ranked[:limit]]


def _substitute(
    value: str,
    pattern: re.Pattern[str],
    replacement: _Replacement,
) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        result = replacement(match) if callable(replacement) else replacement
        if result is None:
            return match.group(0)
        count += 1
        return result

    return pattern.sub(replace, value), count


def _redact_text(value: str) -> tuple[str, int]:
    """Replace sensitive values in *value* without retaining their originals."""
    markers: list[str] = []

    def protect_markers(text: str) -> str:
        def protect(match: re.Match[str]) -> str:
            marker_index = len(markers)
            markers.append(match.group(0))
            return f"\ue000{marker_index}\ue001"

        return _REDACTION_MARKER_RE.sub(protect, text)

    protected = protect_markers(value)
    count = 0
    for pattern, replacement in _SENSITIVE_RULES:
        protected, replacements = _substitute(protected, pattern, replacement)
        count += replacements
        protected = protect_markers(protected)
    for index, marker in enumerate(markers):
        protected = protected.replace(f"\ue000{index}\ue001", marker)
    return protected, count


def redact_messages(
    messages: list[dict[str, str]],
) -> tuple[list[dict[str, str]], int]:
    """Return independently copied messages with sensitive content redacted."""
    redacted_messages: list[dict[str, str]] = []
    count = 0
    for message in messages:
        copied = dict(message)
        copied["content"], replacements = _redact_text(
            str(message.get("content", ""))
        )
        redacted_messages.append(copied)
        count += replacements
    return redacted_messages, count


def sanitize_summary(value: str) -> tuple[str | None, int]:
    """Redact a summary and reject empty, low-information, non-Chinese output."""
    sanitized, count = _redact_text(value.strip())
    informative = _REDACTION_MARKER_RE.sub("", sanitized)
    informative = re.sub(r"[\s，。；：、.!?]", "", informative)
    if not informative or not contains_cjk(informative):
        return None, count
    return sanitized, count

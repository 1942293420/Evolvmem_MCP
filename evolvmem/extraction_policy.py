"""Pure policies for redacting extraction inputs and summaries."""

from __future__ import annotations

import re
from dataclasses import dataclass

from evolvmem.auto_extractor import CandidateMemory


_SENSITIVE_RULES: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----.*?"
            r"-----END (?:[A-Z0-9 ]+ )?PRIVATE KEY-----",
            re.IGNORECASE | re.DOTALL,
        ),
        "[已脱敏:private_key]",
    ),
    (
        "url_credentials",
        re.compile(
            r"(?P<scheme>[a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@",
            re.IGNORECASE,
        ),
        r"\g<scheme>[已脱敏:url凭据]@",
    ),
    (
        "bearer",
        re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
        "Bearer [已脱敏:token]",
    ),
    (
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
        "[已脱敏:token]",
    ),
    (
        "api_key",
        re.compile(
            r"(?i)\b(?:api[_ -]?key|secret[_ -]?key)\s*(?:=|:|is|是|为)\s*"
            r"[^\s，。；,]{8,}"
        ),
        "api_key=[已脱敏:api_key]",
    ),
    (
        "token",
        re.compile(
            r"(?i)\b(?:access[_ -]?token|refresh[_ -]?token|token)\s*"
            r"(?:=|:|is|是|为)\s*[^\s，。；,]{8,}"
        ),
        "token=[已脱敏:token]",
    ),
    (
        "password",
        re.compile(
            r"(?i)(?:\bpassword\b|\bpasswd\b|\bpwd\b|密码)\s*"
            r"(?:=|:|set\s+to|is|是|为)\s*[^\s，。；,]{4,}"
        ),
        "password=[已脱敏:password]",
    ),
    (
        "credential_location",
        re.compile(
            r"(?i)(?:密码|口令|密钥|token|credential)s?\s*"
            r"(?:存放|保存|位于|写入|stored?|saved?|located?)\s*"
            r"(?:在|于|to|at|in)?\s*[^\s，。；,]+"
        ),
        "凭据[已脱敏:凭据位置]",
    ),
)

_EPHEMERAL_PATTERNS = (
    re.compile(r"(?:等待|稍后|之后).{0,12}(?:用户|输入|确认)"),
    re.compile(r"(?:临时|暂时|一次性).{0,12}(?:密码|设置|测试|任务)"),
    re.compile(r"(?:本次|此次|刚刚)?.{0,12}(?:测试|用例).{0,12}(?:通过|成功|完成|\d+\s*个)"),
    re.compile(r"(?:本次|此次|刚刚)?.{0,12}(?:部署|发布|上线).{0,8}(?:通过|成功|完成|结束)"),
    re.compile(r"(?:commit|提交)\s*[0-9a-f]{7,40}", re.IGNORECASE),
)
_DURABLE_CONTEXT_RE = re.compile(
    r"长期|持续|永久|约束|规则|决定|原因|因为|防止|禁止|必须"
)


@dataclass(frozen=True)
class PolicyDecision:
    accepted: bool
    reason: str = ""


def contains_cjk(text: str) -> bool:
    """Return whether *text* includes a CJK ideograph."""
    return re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text) is not None


def contains_sensitive_text(text: str) -> bool:
    """Return whether *text* contains a value that should be redacted."""
    return any(pattern.search(text) for _, pattern, _ in _SENSITIVE_RULES)


def evaluate_candidate(candidate: CandidateMemory) -> PolicyDecision:
    """Apply safety, language, and durability gates to one candidate."""
    value = candidate.value.strip()
    if contains_sensitive_text(value):
        return PolicyDecision(False, "sensitive")
    if not contains_cjk(value):
        return PolicyDecision(False, "language")
    is_ephemeral = any(pattern.search(value) for pattern in _EPHEMERAL_PATTERNS)
    if is_ephemeral and not _DURABLE_CONTEXT_RE.search(value):
        return PolicyDecision(False, "ephemeral")
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
    candidates: list[CandidateMemory], limit: int = 8
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


def _redact_text(value: str) -> tuple[str, int]:
    """Replace sensitive values in *value* without retaining their originals."""
    count = 0
    for _, pattern, replacement in _SENSITIVE_RULES:
        value, replacements = pattern.subn(replacement, value)
        count += replacements
    return value, count


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
    informative = re.sub(r"\[已脱敏:[^\]]+\]|[\s，。；：、.!?]", "", sanitized)
    if not informative or not contains_cjk(sanitized):
        return None, count
    return sanitized, count

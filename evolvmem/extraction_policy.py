"""Pure policies for redacting extraction inputs and summaries."""

from __future__ import annotations

import re

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


def contains_cjk(text: str) -> bool:
    """Return whether *text* includes a CJK ideograph."""
    return re.search(r"[\u3400-\u4dbf\u4e00-\u9fff]", text) is not None


def contains_sensitive_text(text: str) -> bool:
    """Return whether *text* contains a value that should be redacted."""
    return any(pattern.search(text) for _, pattern, _ in _SENSITIVE_RULES)


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

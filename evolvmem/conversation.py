"""Readable user/assistant dialogue, separate from raw verification evidence."""
from __future__ import annotations
import json
import re

_WRAPPERS = re.compile(r'<(environment_context|INSTRUCTIONS|system-reminder|permissions_instructions|turn_aborted)\b[^>]*>.*?</\1>', re.I | re.S)
_MEMORY_BLOCK = re.compile(r'\[BEGIN EVOLVMEM[^\]]*\].*?\[END EVOLVMEM[^\]]*\]', re.S)
# Known client-side injections that arrive as their own wrapped record. Only a
# complete structural wrapper is removed, so a normal message that merely names
# these features keeps its text.
_INJECTED_NAMES = ('recommended_plugins', 'external_codex_apps_open_page', 'in-app-browser-context')
_INJECTED_WRAPPERS = re.compile(r'<(' + '|'.join(_INJECTED_NAMES) + r')\b[^>]*>.*?</\1\s*>', re.I | re.S)
_AGENTS_HEADER = re.compile(r'^\s*# AGENTS\.md instructions\b[^\n]*(?:\n|$)')
# Fenced blocks and inline spans quote real text: an injection name inside them
# is a business reference, not transport noise, so it is never removed.
_CODE_QUOTE = re.compile(r'```.*?```|~~~.*?~~~|`[^`\n]*`', re.S)
_INJECTION_PATTERNS = (_MEMORY_BLOCK, _WRAPPERS, _INJECTED_WRAPPERS)


def _strip_injections(content):
    """Drop known injections only outside code quotes; keep the payload unrewritten.

    A wrapper is a business reference exactly when it *starts* inside a code
    quote (fenced block or inline span) and is then left untouched. Code quoted
    inside a real wrapper does not shield that wrapper: the transport record is
    still one record and is removed whole.
    """
    quoted = [(match.start(), match.end()) for match in _CODE_QUOTE.finditer(content)]

    def in_quote(position):
        return any(start <= position < end for start, end in quoted)

    spans = sorted((match.start(), match.end())
                   for pattern in _INJECTION_PATTERNS
                   for match in pattern.finditer(content) if not in_quote(match.start()))
    parts, cursor = [], 0
    for start, end in spans:
        if start < cursor:
            cursor = max(cursor, end)
            continue
        parts.append(content[cursor:start])
        cursor = end
    parts.append(content[cursor:])
    return _AGENTS_HEADER.sub('', ''.join(parts)).strip()


def clean_messages(messages, *, policy=None):
    """Remove transport/injected records, preserving actual dialogue and order.

    ``unknown`` keeps the real role of an unstructured record (for example an
    unassigned material item): its author cannot be verified, so it is kept as
    unknown rather than being dropped or re-labelled as the assistant's words.
    Tool records, system injections and non-dialogue channels are still removed.
    """
    result = []
    settings = (policy or {}).get('settings', {})
    drop = set(settings.get('cleaning_drop_lines', []))
    for message in messages:
        if not isinstance(message, dict) or message.get('role') not in ('user', 'assistant', 'unknown'):
            continue
        if message.get('channel') in ('analysis', 'summary') or message.get('recipient') not in (None, '', 'all'):
            continue
        content = message.get('content', '')
        if not isinstance(content, str):
            continue
        content = _strip_injections(content)
        if drop:
            content = '\n'.join(line for line in content.splitlines() if line.strip() not in drop).strip()
        if content:
            entry = {'role': message['role'], 'content': content}
            if settings.get('cleaning_collapse_duplicates') and result and result[-1] == entry:
                continue
            result.append(entry)
    return result


def from_payload(payload, *, policy=None):
    """Old archives are cleaned on read; new archives carry the same view."""
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get('conversation'), list):
        return clean_messages(payload['conversation'], policy=policy)
    if isinstance(payload.get('messages'), list):
        return clean_messages(payload['messages'], policy=policy)
    if isinstance(payload.get('transcript'), str):
        from evolvmem.codex_transcript import parse_transcript
        _, messages = parse_transcript(payload['transcript'].encode(), payload['session_id'])
        return clean_messages(messages, policy=policy)
    return []


def render(messages):
    return '\n\n'.join(('用户：' if m['role']=='user' else 'AI：')+m['content'] for m in messages)

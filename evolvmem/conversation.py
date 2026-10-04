"""Readable user/assistant dialogue, separate from raw verification evidence."""
from __future__ import annotations
import json
import re

_WRAPPERS = re.compile(r'<(environment_context|INSTRUCTIONS|system-reminder|permissions_instructions|turn_aborted)\b[^>]*>.*?</\1>', re.I | re.S)
_MEMORY_BLOCK = re.compile(r'\[BEGIN EVOLVMEM[^\]]*\].*?\[END EVOLVMEM[^\]]*\]', re.S)


def clean_messages(messages, *, policy=None):
    """Remove transport/injected records, preserving actual dialogue and order."""
    result = []
    settings = (policy or {}).get('settings', {})
    drop = set(settings.get('cleaning_drop_lines', []))
    for message in messages:
        if not isinstance(message, dict) or message.get('role') not in ('user', 'assistant'):
            continue
        if message.get('channel') in ('analysis', 'summary') or message.get('recipient') not in (None, '', 'all'):
            continue
        content = message.get('content', '')
        if not isinstance(content, str):
            continue
        content = _MEMORY_BLOCK.sub('', _WRAPPERS.sub('', content))
        content = re.sub(r'^# AGENTS\.md instructions for [^\n]*(?:\n|$)', '', content).strip()
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

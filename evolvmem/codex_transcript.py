"""Parse persisted Codex events without treating assistant claims as evidence."""
from __future__ import annotations

import json
import ntpath
import posixpath

from evolvmem.lan_sharing import LanError

# Attribution reason for a native system sub-session (``source={"subagent": ...}``).
# It is metadata-only; body keyword matching is never used.
SUBAGENT_SESSION_REASON = 'subagent_session'


def same_client_workspace(root: str, child: str) -> bool:
    """Compare client paths lexically; never probe a Windows path on Linux."""
    path = ntpath if ntpath.splitdrive(root)[0] else posixpath
    if not path.isabs(root) or not path.isabs(child):
        return False
    root, child = path.normcase(path.normpath(root)), path.normcase(path.normpath(child))
    try:
        return path.commonpath([root, child]) == root
    except ValueError:
        return False


def workspace_details(rows: list[dict]) -> dict:
    cwd, reason, parent, subagent, native = '', '', '', False, False
    for row in rows:
        payload = row.get('payload')
        if not isinstance(payload, dict):
            continue
        if row.get('type') == 'session_meta':
            parent = str(payload.get('parent_thread_id') or parent)
            source = payload.get('source')
            # ``source={"subagent": {...}}`` is the native system sub-session
            # marker; ``parent_thread_id`` marks a real child thread. Both are the
            # same predicate the local collector uses (``subagent_session``),
            # while a plain ``forked_from_id`` user fork keeps neither marker and
            # stays an ordinary session.
            native = native or isinstance(source, dict) and 'subagent' in source
            subagent = subagent or bool(parent) or native
        if row.get('type') in ('session_meta', 'turn_context'):
            current = payload.get('cwd')
            if isinstance(current, str) and current:
                if cwd and not same_client_workspace(cwd, current):
                    reason = 'mixed_workspace'
                elif not cwd:
                    cwd = current
    if not cwd or not same_client_workspace(cwd, cwd):
        reason = 'workspace_unknown'
    if subagent:
        reason = SUBAGENT_SESSION_REASON
    return dict(cwd=cwd, attribution_reason=reason, parent_session_id=parent,
                subagent=subagent, native_subagent=native)


def dialogue_messages(rows: list[dict]) -> list[dict]:
    """Extractor messages for already-parsed rows, in their original order.

    Shared with the incremental local capture so a batch range produces exactly
    the same messages the whole-transcript parser would: visible user/assistant
    messages, tool records tagged ``tool``, analysis/summary channels marked,
    and the native response_item/event_msg mirror collapsed once.
    """
    messages = []
    previous = None
    for row in rows:
        payload = row.get('payload', {})
        if not isinstance(payload, dict):
            continue
        role, content = '', ''
        kind = payload.get('type')
        if row.get('type') == 'response_item':
            if kind == 'message' and payload.get('role') in ('user', 'assistant'):
                role = payload['role']
                parts = payload.get('content', [])
                content = parts if isinstance(parts, str) else '\n'.join(
                    p['text'] for p in parts if isinstance(p, dict) and isinstance(p.get('text'), str)
                    and p.get('type') in ('input_text', 'output_text', 'text')) if isinstance(parts, list) else ''
            elif kind in ('function_call_output', 'custom_tool_call_output'):
                role = 'tool'
                output = payload.get('output', '')
                content = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
        elif row.get('type') == 'event_msg' and kind in ('user_message', 'agent_message'):
            role = 'user' if kind == 'user_message' else 'assistant'
            content = payload.get('message', '')
        if not role or not isinstance(content, str) or not content.strip():
            continue
        marker = (role, content)
        # Native streams often mirror a message in response_item and event_msg.
        if previous is not None and previous[:2] == marker and previous[2] != row.get('type'):
            previous = None
            continue
        message = {'role': role, 'content': content}
        if payload.get('channel') in ('analysis', 'summary'):
            message['channel'] = payload['channel']
        if payload.get('recipient') not in (None, '', 'all'):
            message['recipient'] = payload['recipient']
        messages.append(message)
        previous = (*marker, row.get('type'))
    return messages


def parse_transcript(raw: bytes, session_id: str) -> tuple[list[dict], list[dict]]:
    """Keep original rows; produce extractor messages from complete JSONL only."""
    if not raw or not raw.endswith(b'\n'):
        raise LanError('invalid_transcript')
    try:
        # JSONL records end at LF. Unicode separators inside JSON strings are
        # valid content; str.splitlines() would split and corrupt those records.
        rows = [json.loads(line) for line in raw.decode('utf-8').split('\n') if line.strip()]
    except (UnicodeError, ValueError):
        raise LanError('invalid_transcript') from None
    if not rows or any(not isinstance(row, dict) for row in rows):
        raise LanError('invalid_transcript')
    # Only the first session_meta identifies the transcript. Codex Desktop
    # fork/subagent rollouts start with the session's own meta and then repeat
    # the thread it was forked from (`forked_from_id`/`parent_thread_id`) as
    # lineage metadata, so requiring every meta to match would reject every
    # fork. A parent token registering its child's file still fails here
    # because that file leads with the child's id.
    metadata = [row.get('payload') for row in rows if row.get('type') == 'session_meta']
    if not metadata or not isinstance(metadata[0], dict) or metadata[0].get('id') != session_id:
        raise LanError('session_id_mismatch')
    return rows, dialogue_messages(rows)

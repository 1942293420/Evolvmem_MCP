"""Deterministic zero-LLM event parser for Codex, DSH, Claude, and Kimi transcripts (搂3.3)."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from evolvmem import transcript_formats
from evolvmem.experience_v2.pipeline.history_reader import HistorySession
from evolvmem.lan_sharing import LanError

_CODE_FENCE_RE = re.compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]*`)")
_EXIT_CODE_RE = re.compile(r"(?:exit[_\s]?code\s*[=:]\s*|Exit\s+code\s+)(\d+)")
_ERROR_SIG_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*(?:Error|Exception)|ECONNREFUSED|EACCES|ENOENT|TS\d{4}|error\[[A-Z0-9]+\]|Operation not permitted)"
)
_ROLLBACK_CMD_RE = re.compile(r"\b(git\s+(?:checkout|restore|reset|revert)|roll\s*back)\b", re.IGNORECASE)

_INTENT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("preference", re.compile(r"\b(?:always\s+(?:prefer|use|keep)|never\s+use|please\s+remember|prefer\s+\S+\s+over)\b", re.IGNORECASE)),
    ("correction", re.compile(r"\b(?:actually\s+use|instead\s+of|don'?t\s+(?:kill|use|do|retry)|wrong\b|not\s+that)\b", re.IGNORECASE)),
    ("abandon", re.compile(r"\b(?:abandon|drop\s+the|discard|give\s+up|switch\s+to\s+podman|roll\s*back\s+to)\b", re.IGNORECASE)),
    ("confirmation", re.compile(r"\b(?:confirmed|that\s+worked|that\s+fixed|looks\s+good|go\s+ahead|yes,\s*proceed)\b", re.IGNORECASE)),
)


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    event_index: int
    tool_name: str
    command_or_target: str
    exit_code: int | None
    is_error: bool
    error_signature: str
    output_excerpt: str


@dataclass(frozen=True, slots=True)
class FileEditEvent:
    event_index: int
    path: str
    action: str


@dataclass(frozen=True, slots=True)
class RetrySequence:
    command_or_target: str
    count: int
    event_indices: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class UserIntentSignal:
    event_index: int
    kind: str
    excerpt: str


@dataclass(frozen=True, slots=True)
class ParsedSessionEvents:
    session_id: str
    adapter: str
    source_format: str
    project: str
    source_ref: str
    events: tuple[dict[str, Any], ...]
    tool_outcomes: tuple[ToolOutcome, ...]
    file_edits: tuple[FileEditEvent, ...]
    rollbacks: tuple[str, ...]
    retries: tuple[RetrySequence, ...]
    user_intents: tuple[UserIntentSignal, ...]
    evidence_pointers: tuple[dict[str, Any], ...]
    llm_calls_used: int = 0


def strip_code_blocks(text: str) -> str:
    """Remove fenced/inline code spans so quoted snippets never trigger user intents."""
    return _CODE_FENCE_RE.sub(" ", text)


def classify_user_intent(text: str, event_index: int = 0) -> tuple[UserIntentSignal, ...]:
    cleaned = strip_code_blocks(text).strip()
    if not cleaned:
        return ()
    found: list[UserIntentSignal] = []
    for kind, pattern in _INTENT_PATTERNS:
        if pattern.search(cleaned):
            found.append(UserIntentSignal(event_index=event_index, kind=kind, excerpt=cleaned[:200]))
    return tuple(found)


def _extract_error_signature(text: str) -> str:
    match = _ERROR_SIG_RE.search(text)
    return match.group(1) if match else ""


def _extract_exit_code(text: str) -> int | None:
    match = _EXIT_CODE_RE.search(text)
    if match:
        return int(match.group(1))
    try:
        obj = json.loads(text)
        if isinstance(obj, dict) and isinstance(obj.get("exit_code"), int):
            return obj["exit_code"]
    except (ValueError, TypeError):
        pass
    return None


def _parse_json_lines(raw: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s:
            continue
        obj = json.loads(s)
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _json_arg_dict(raw_args: Any) -> dict[str, Any]:
    if isinstance(raw_args, dict):
        return raw_args
    if isinstance(raw_args, str):
        try:
            obj = json.loads(raw_args)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            return {"raw": raw_args}
    return {}


def _extract_from_codex(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    norm: list[dict[str, Any]] = []
    for idx, row in enumerate(rows):
        rtype = row.get("type")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        kind = payload.get("type")
        if rtype == "response_item":
            if kind == "message" and payload.get("role") in ("user", "assistant"):
                parts = payload.get("content", [])
                text = parts if isinstance(parts, str) else "\n".join(
                    p.get("text", "") for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)
                )
                norm.append({"index": idx, "kind": "message", "role": payload["role"], "text": text})
            elif kind == "function_call":
                args = _json_arg_dict(payload.get("arguments"))
                norm.append({
                    "index": idx, "kind": "tool_call", "call_id": str(payload.get("call_id") or ""),
                    "tool": str(payload.get("name") or ""), "command": str(args.get("cmd") or args.get("command") or ""),
                    "path": str(args.get("path") or ""), "action": str(args.get("action") or ""),
                })
            elif kind in ("function_call_output", "custom_tool_call_output"):
                out = payload.get("output", "")
                text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
                norm.append({
                    "index": idx, "kind": "tool_result", "call_id": str(payload.get("call_id") or ""),
                    "exit_code": _extract_exit_code(text), "text": text,
                })
        elif rtype == "event_msg" and kind in ("user_message", "agent_message"):
            norm.append({
                "index": idx, "kind": "message",
                "role": "user" if kind == "user_message" else "assistant",
                "text": str(payload.get("message") or ""),
            })
    return norm


def _extract_from_dsh(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    norm: list[dict[str, Any]] = []
    for idx, row in enumerate(rows[1:] if rows and rows[0].get("type") == "session" else rows):
        rtype = row.get("type", "")
        data = row.get("data") if isinstance(row.get("data"), dict) else {}
        if rtype in ("user/message", "assistant/message"):
            msg = data if rtype == "user/message" else data.get("message", {})
            parts = msg.get("content", []) if isinstance(msg, dict) else []
            text = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str))
            norm.append({"index": idx, "kind": "message", "role": "user" if rtype == "user/message" else "assistant", "text": text})
        elif rtype == "tool/call":
            inp = _json_arg_dict(data.get("input"))
            norm.append({
                "index": idx, "kind": "tool_call", "call_id": str(data.get("callId") or ""),
                "tool": str(data.get("name") or ""), "command": str(inp.get("command") or inp.get("cmd") or ""),
                "path": str(inp.get("path") or ""), "action": str(inp.get("action") or ""),
            })
        elif rtype == "tool/result":
            msg = data.get("message") if isinstance(data.get("message"), dict) else {}
            parts = msg.get("content", []) if isinstance(msg, dict) else []
            text = "\n".join(p.get("text", "") for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str))
            rc = msg.get("exitCode") if isinstance(msg.get("exitCode"), int) else _extract_exit_code(text)
            norm.append({"index": idx, "kind": "tool_result", "call_id": str(msg.get("callId") or ""), "exit_code": rc, "text": text})
    return norm


def _extract_from_claude(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    norm: list[dict[str, Any]] = []
    for idx, row in enumerate(rows):
        msg = row.get("message") if isinstance(row.get("message"), dict) else {}
        role = str(msg.get("role") or row.get("type") or "")
        content = msg.get("content")
        if isinstance(content, str):
            norm.append({"index": idx, "kind": "message", "role": role, "text": content})
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text":
                    norm.append({"index": idx, "kind": "message", "role": role, "text": str(part.get("text") or "")})
                elif ptype == "tool_use":
                    inp = _json_arg_dict(part.get("input"))
                    tname = str(part.get("name") or "")
                    action = "edit" if tname == "Edit" else ("create" if tname == "Write" else "")
                    norm.append({
                        "index": idx, "kind": "tool_call", "call_id": str(part.get("id") or ""),
                        "tool": tname, "command": str(inp.get("command") or ""),
                        "path": str(inp.get("file_path") or inp.get("path") or ""), "action": action,
                    })
                elif ptype == "tool_result":
                    text = part.get("content", "")
                    text_s = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
                    rc = _extract_exit_code(text_s)
                    if rc is None and part.get("is_error"):
                        rc = 1
                    norm.append({
                        "index": idx, "kind": "tool_result", "call_id": str(part.get("tool_use_id") or ""),
                        "exit_code": rc, "text": text_s,
                    })
    return norm


def _extract_from_kimi(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    norm: list[dict[str, Any]] = []
    for idx, row in enumerate(rows):
        rtype = str(row.get("type") or "")
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if rtype == "turn.prompt":
            norm.append({"index": idx, "kind": "message", "role": "user", "text": str(payload.get("text") or "")})
        elif rtype == "context.append_loop_event":
            kind = str(payload.get("kind") or "")
            if kind == "tool_call":
                args = _json_arg_dict(payload.get("args"))
                norm.append({
                    "index": idx, "kind": "tool_call", "call_id": str(payload.get("call_id") or ""),
                    "tool": str(payload.get("name") or ""), "command": str(args.get("command") or ""),
                    "path": str(args.get("path") or ""), "action": str(args.get("action") or ""),
                })
            elif kind == "tool_result":
                text = str(payload.get("output") or "")
                rc = payload.get("exit_code") if isinstance(payload.get("exit_code"), int) else _extract_exit_code(text)
                norm.append({"index": idx, "kind": "tool_result", "call_id": str(payload.get("call_id") or ""), "exit_code": rc, "text": text})
            elif kind in ("assistant_message", "user_message"):
                norm.append({
                    "index": idx, "kind": "message",
                    "role": "user" if kind == "user_message" else "assistant",
                    "text": str(payload.get("content") or ""),
                })
    return norm


def _normalize_events(session: HistorySession) -> list[dict[str, Any]]:
    s = session.summary
    if session.raw_bytes:
        fmt = s.source_format
        if fmt in transcript_formats.SOURCE_FORMATS:
            try:
                rows, _ = transcript_formats.parse_transcript(session.raw_bytes, s.session_id, format=fmt)
                return _extract_from_dsh(rows) if fmt == transcript_formats.DSH_FORMAT else _extract_from_codex(rows)
            except LanError:
                pass
        rows = _parse_json_lines(session.raw_bytes)
        if s.adapter == "claude" or fmt.startswith("claude"):
            return _extract_from_claude(rows)
        if s.adapter == "kimi" or fmt.startswith("kimi"):
            return _extract_from_kimi(rows)
        if fmt == transcript_formats.DSH_FORMAT or s.adapter == "dsh":
            return _extract_from_dsh(rows)
        return _extract_from_codex(rows)

    norm: list[dict[str, Any]] = []
    for rec in session.records:
        if rec.role == "tool":
            norm.append({
                "index": rec.event_index, "kind": "tool_result", "call_id": "",
                "exit_code": _extract_exit_code(rec.content), "text": rec.content,
            })
        else:
            norm.append({"index": rec.event_index, "kind": "message", "role": rec.role, "text": rec.content})
    return norm


def parse_session_events(session: HistorySession) -> ParsedSessionEvents:
    """Deterministically parse multi-client HistorySession into typed events with 0 LLM calls."""
    norm = _normalize_events(session)
    calls_by_id: dict[str, dict[str, Any]] = {}
    outcomes: list[ToolOutcome] = []
    edits: list[FileEditEvent] = []
    rollbacks: list[str] = []
    cmd_hits: dict[str, list[int]] = {}
    intents: list[UserIntentSignal] = []

    for ev in norm:
        idx = int(ev.get("index", 0))
        kind = ev.get("kind")
        if kind == "tool_call":
            cid = str(ev.get("call_id") or f"idx_{idx}")
            calls_by_id[cid] = ev
            cmd = str(ev.get("command") or "").strip()
            path = str(ev.get("path") or "").strip()
            action = str(ev.get("action") or "").strip().lower()
            if cmd:
                cmd_hits.setdefault(cmd, []).append(idx)
                if _ROLLBACK_CMD_RE.search(cmd):
                    rollbacks.append(cmd)
                if cmd.startswith("rm ") and path == "":
                    edits.append(FileEditEvent(event_index=idx, path=cmd.split()[-1], action="delete"))
            if path and action in ("create", "edit", "delete", "rollback"):
                edits.append(FileEditEvent(event_index=idx, path=path, action=action))
                if action == "rollback":
                    rollbacks.append(path)
        elif kind == "tool_result":
            cid = str(ev.get("call_id") or "")
            call = calls_by_id.get(cid, {})
            text = str(ev.get("text") or "")
            rc = ev.get("exit_code")
            sig = _extract_error_signature(text)
            is_err = (rc is not None and rc != 0) or bool(sig)
            outcomes.append(
                ToolOutcome(
                    event_index=idx,
                    tool_name=str(call.get("tool") or "tool"),
                    command_or_target=str(call.get("command") or call.get("path") or ""),
                    exit_code=rc,
                    is_error=is_err,
                    error_signature=sig,
                    output_excerpt=text[:240],
                )
            )
        elif kind == "message" and ev.get("role") == "user":
            sigs = classify_user_intent(str(ev.get("text") or ""), event_index=idx)
            intents.extend(sigs)
            for s in sigs:
                if s.kind == "abandon" and "roll" in s.excerpt.lower():
                    rollbacks.append(s.excerpt[:120])

    retries = tuple(
        RetrySequence(command_or_target=cmd, count=len(idxs), event_indices=tuple(idxs))
        for cmd, idxs in cmd_hits.items()
        if len(idxs) >= 2
    )
    digest = hashlib.sha256(
        session.raw_bytes if session.raw_bytes else "\n".join(r.content for r in session.records).encode("utf-8")
    ).hexdigest()
    max_idx = max((int(e.get("index", 0)) for e in norm), default=0)
    pointers = (
        {
            "session_id": session.summary.session_id,
            "source_ref": session.summary.source_ref,
            "event_range": [0, max_idx],
            "content_hash": f"sha256:{digest}",
        },
    )
    return ParsedSessionEvents(
        session_id=session.summary.session_id,
        adapter=session.summary.adapter,
        source_format=session.summary.source_format,
        project=session.summary.project,
        source_ref=session.summary.source_ref,
        events=tuple(norm),
        tool_outcomes=tuple(outcomes),
        file_edits=tuple(edits),
        rollbacks=tuple(dict.fromkeys(rollbacks)),
        retries=retries,
        user_intents=tuple(intents),
        evidence_pointers=pointers,
        llm_calls_used=0,
    )

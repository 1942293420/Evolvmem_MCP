"""Deterministic KeyMoment detector and context window compressor (§3.4)."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from evolvmem.experience_v2.llm_meter import estimate_tokens
from evolvmem.experience_v2.pipeline.events import ParsedSessionEvents, strip_code_blocks

MOMENT_KINDS: tuple[str, ...] = (
    "fail_to_success",
    "user_correction",
    "abandoned_approach",
    "high_cost_segment",
    "external_surprise",
    "preference_expression",
)


@dataclass(frozen=True, slots=True)
class KeyMoment:
    moment_id: str
    session_id: str
    kind: str
    title: str
    event_range: tuple[int, int]
    snippet_text: str
    snippet_tokens: int
    evidence_pointers: tuple[dict[str, Any], ...]
    score: float
    llm_calls_used: int = 0


def _clip_line(text: str, limit: int = 260) -> str:
    clean = " ".join(text.split())
    return clean if len(clean) <= limit else clean[: limit - 3] + "..."


def _build_window_snippet(
    parsed: ParsedSessionEvents,
    start_idx: int,
    end_idx: int,
    max_snippet_tokens: int,
) -> str:
    lines: list[str] = [f"session={parsed.session_id} adapter={parsed.adapter} project={parsed.project}"]
    lo = max(0, start_idx - 2)
    hi = end_idx + 2
    for ev in parsed.events:
        idx = int(ev.get("index", 0))
        if idx < lo or idx > hi:
            continue
        k = ev.get("kind")
        if k == "message":
            txt = strip_code_blocks(str(ev.get("text") or "")) if ev.get("role") == "user" else str(ev.get("text") or "")
            lines.append(f"[{idx}] {ev.get('role')}: {_clip_line(txt, 280)}")
        elif k == "tool_call":
            target = ev.get("command") or ev.get("path") or ev.get("tool")
            lines.append(f"[{idx}] call {ev.get('tool')}: {_clip_line(str(target), 180)}")
        elif k == "tool_result":
            rc = ev.get("exit_code")
            lines.append(f"[{idx}] result exit={rc}: {_clip_line(str(ev.get('text') or ''), 240)}")

    text = "\n".join(lines)
    while estimate_tokens(text) > max_snippet_tokens and len(lines) > 2:
        lines.pop(1)
        text = "\n".join(lines)
    if estimate_tokens(text) > max_snippet_tokens:
        # Hard byte-safe char trim strictly respecting max_snippet_tokens.
        char_cap = max(200, max_snippet_tokens * 2)
        text = text[:char_cap]
        while estimate_tokens(text) > max_snippet_tokens and len(text) > 64:
            text = text[: int(len(text) * 0.8)]
    return text


def _make_moment(
    parsed: ParsedSessionEvents,
    kind: str,
    title: str,
    start_idx: int,
    end_idx: int,
    score: float,
    max_snippet_tokens: int,
) -> KeyMoment:
    snippet = _build_window_snippet(parsed, start_idx, end_idx, max_snippet_tokens)
    toks = estimate_tokens(snippet)
    mid = hashlib.sha256(f"{parsed.session_id}:{kind}:{start_idx}:{end_idx}".encode("utf-8")).hexdigest()[:16]
    base_hash = parsed.evidence_pointers[0]["content_hash"] if parsed.evidence_pointers else "sha256:none"
    ptrs = (
        {
            "session_id": parsed.session_id,
            "source_ref": parsed.source_ref,
            "event_range": [start_idx, end_idx],
            "content_hash": base_hash,
        },
    )
    return KeyMoment(
        moment_id=f"km_{mid}",
        session_id=parsed.session_id,
        kind=kind,
        title=title[:120],
        event_range=(start_idx, end_idx),
        snippet_text=snippet,
        snippet_tokens=toks,
        evidence_pointers=ptrs,
        score=score,
        llm_calls_used=0,
    )


def detect_key_moments(parsed: ParsedSessionEvents, max_snippet_tokens: int = 3000) -> tuple[KeyMoment, ...]:
    """Identify high-value session moments and compress snippets under max_snippet_tokens."""
    moments: list[KeyMoment] = []
    seen_ranges: set[tuple[str, int, int]] = set()

    def add(kind: str, title: str, lo: int, hi: int, score: float) -> None:
        key = (kind, lo, hi)
        if key in seen_ranges:
            return
        seen_ranges.add(key)
        moments.append(_make_moment(parsed, kind, title, lo, hi, score, max_snippet_tokens))

    outcomes = parsed.tool_outcomes
    first_err = next((o for o in outcomes if o.is_error), None)
    last_ok = next((o for o in reversed(outcomes) if not o.is_error and o.exit_code == 0), None)

    # 1. fail_to_success: error occurred followed by later edit or successful command
    has_f2s = False
    if first_err is not None and last_ok is not None and last_ok.event_index > first_err.event_index:
        add(
            "fail_to_success",
            f"Recovered from {first_err.error_signature or first_err.command_or_target}",
            first_err.event_index,
            last_ok.event_index,
            0.9,
        )
        has_f2s = True

    # 2. abandoned_approach: rollback command/edit or abandon user intent
    has_abandon = False
    for rb in parsed.rollbacks:
        idxs = [int(e.get("index", 0)) for e in parsed.events]
        lo, hi = (min(idxs), max(idxs)) if idxs else (0, 0)
        add("abandoned_approach", f"Abandon/rollback: {_clip_line(rb, 60)}", lo, hi, 0.85)
        has_abandon = True
        break
    if not has_abandon:
        for u in parsed.user_intents:
            if u.kind == "abandon":
                add("abandoned_approach", f"User abandoned approach: {_clip_line(u.excerpt, 60)}", u.event_index, u.event_index, 0.85)
                has_abandon = True
                break

    # 3. user_correction & 4. preference_expression
    for u in parsed.user_intents:
        if u.kind == "correction":
            add("user_correction", f"User correction: {_clip_line(u.excerpt, 60)}", max(0, u.event_index - 2), u.event_index, 0.9)
        elif u.kind == "preference":
            add("preference_expression", f"User preference: {_clip_line(u.excerpt, 60)}", u.event_index, u.event_index, 0.85)

    # 5. high_cost_segment: repeated tool retry loops
    for r in parsed.retries:
        lo, hi = min(r.event_indices), max(r.event_indices)
        add("high_cost_segment", f"Repeated command ({r.count}x): {_clip_line(r.command_or_target, 50)}", lo, hi, 0.8)

    # 6. external_surprise: unresolved external error signature
    if not has_f2s:
        for o in outcomes:
            if o.is_error and o.error_signature:
                add("external_surprise", f"External failure: {o.error_signature}", o.event_index, o.event_index, 0.78)
                break

    return tuple(moments)

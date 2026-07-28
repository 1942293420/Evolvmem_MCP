"""SessionStart and Stop Hook integration — memory formatting and extraction triggering."""

import sys
import time
from pathlib import Path

from evolvmem.config import Config
from evolvmem.memory_store import MemoryStore
from evolvmem.auto_extractor import AutoExtractor
from evolvmem.forgetting import ForgettingEngine
from evolvmem.scoring import compute_score


def _last_forget_path(config: Config) -> Path:
    return config.data_dir / ".last_forget"


def _maybe_run_forgetting(config: Config, store: MemoryStore) -> None:
    """Run the forgetting engine at most once per forget_auto_run_hours.

    Failures are swallowed — memory maintenance must never block session start.
    """
    try:
        marker = _last_forget_path(config)
        interval_s = config.forget_auto_run_hours * 3600
        if marker.exists():
            last_run = marker.stat().st_mtime
            if time.time() - last_run < interval_s:
                return
        archived = ForgettingEngine(config, store).run()
        marker.touch()
        if archived:
            print(f"[evolvmem] auto-forgetting archived {archived} memories",
                  file=sys.stderr, flush=True)
    except Exception:
        pass


def _format_full_line(m: dict) -> str:
    tags = m.get("tags", "")
    if tags:
        return f"- **{m['key']}** [{tags}]: {m['value']}"
    return f"- **{m['key']}**: {m['value']}"


def _format_index_line(m: dict) -> str:
    tags = m.get("tags", "")
    suffix = f" [{tags}]" if tags else ""
    return f"- {m['key']}{suffix} ({len(m['value'])}字)"


def _take_budget(items: list[dict], max_count: int, max_chars: int,
                 formatter=_format_full_line) -> tuple[list[dict], list[dict]]:
    """Take items within count/char budget. First item is always kept.

    Returns (selected, omitted).
    """
    selected: list[dict] = []
    used = 0
    for i, m in enumerate(items):
        if len(selected) >= max_count:
            return selected, items[i:]
        line_len = len(formatter(m))
        if selected and used + line_len > max_chars:
            return selected, items[i:]
        selected.append(m)
        used += line_len
    return selected, []


def _key_prefix(key: str) -> str:
    """First two segments of a dotted key, e.g. 'project:purchase:fact:x' → 'project:purchase'."""
    parts = key.split(":")
    return ":".join(parts[:2]) if len(parts) >= 2 else key


def _apply_prefix_quota(items: list[dict], quota: int) -> tuple[list[dict], list[dict]]:
    """Cap items per key prefix. Returns (kept, overflow), both order-preserving."""
    counts: dict[str, int] = {}
    kept: list[dict] = []
    overflow: list[dict] = []
    for m in items:
        prefix = _key_prefix(m["key"])
        if counts.get(prefix, 0) < quota:
            kept.append(m)
            counts[prefix] = counts.get(prefix, 0) + 1
        else:
            overflow.append(m)
    return kept, overflow


def get_session_start_block(config: Config | None = None) -> str:
    """Build the SessionStart injection block with three layers:

    1. Pinned layer — tier='pinned' memories always injected (own budget),
       sorted by importance desc.
    2. Scored layer — normal memories ranked by compute_score()
       (importance + recency + frequency), competing for the remaining
       inject_max_chars budget, with a per-key-prefix quota.
    3. Index layer — omitted memories listed as one-line indexes so the
       agent knows they exist and can fetch them via memory_search.

    Args:
        config: Configuration object, uses defaults when None.

    Returns:
        Formatted system prompt string; empty string if no active memories.
    """
    if config is None:
        config = Config.from_file()

    with MemoryStore(config) as store:
        _maybe_run_forgetting(config, store)
        memories = store.get_active()

    if not memories:
        return ""

    pinned = [m for m in memories if m.get("tier") == "pinned"]
    normal = [m for m in memories if m.get("tier") != "pinned"]

    # Layer 1: pinned, own budget, importance desc
    pinned.sort(key=lambda m: m.get("importance") or 5.0, reverse=True)
    pinned_sel, pinned_omit = _take_budget(
        pinned, config.inject_pinned_max_count, config.inject_pinned_max_chars)

    # Layer 2: normal, scored, prefix quota, remaining budget
    normal.sort(key=lambda m: compute_score(m, config), reverse=True)
    normal_quota, quota_overflow = _apply_prefix_quota(
        normal, config.inject_key_prefix_quota)
    pinned_chars = sum(len(_format_full_line(m)) for m in pinned_sel)
    normal_sel, normal_omit = _take_budget(
        normal_quota,
        max(0, config.inject_max_count - len(pinned_sel)),
        max(0, config.inject_max_chars - pinned_chars))

    # Layer 3: index lines for everything omitted
    omitted = pinned_omit + normal_omit + quota_overflow
    omitted.sort(key=lambda m: compute_score(m, config), reverse=True)
    index_sel, index_omit = ([], omitted)
    if config.inject_index_max_chars > 0 and omitted:
        index_sel, index_omit = _take_budget(
            omitted, len(omitted), config.inject_index_max_chars,
            formatter=_format_index_line)

    # Compose
    lines = [
        "## Persistent Memory (from EvolvMem plugin)",
        "",
        "The following are preferences, decisions, and constraints extracted from previous conversations "
        "that are still valid.",
        "These are persisted facts, not context from the current conversation.",
        "Trust priority: user's current instructions > current code and tests > the memories below > history.",
        "If any memory contradicts what the user is currently saying, follow the user's current statement.",
        "Only the most relevant memories are injected here; use the memory_search tool to recall the rest on demand.",
        "",
    ]

    if pinned_sel:
        lines.append("### 常驻记忆 (pinned)")
        lines.extend(_format_full_line(m) for m in pinned_sel)
        lines.append("")

    if normal_sel:
        lines.append("### 记忆精选 (by importance)")
        lines.extend(_format_full_line(m) for m in normal_sel)
        lines.append("")

    if index_sel:
        lines.append("### 记忆索引 (use memory_search to fetch full content)")
        lines.extend(_format_index_line(m) for m in index_sel)

    if index_omit:
        lines.append("")
        lines.append(
            f"({len(index_omit)} more memories not injected; "
            f"use the memory_search tool to retrieve them when needed.)"
        )

    return "\n".join(lines)


def get_stop_prompt(messages_summary: str) -> str:
    """Build the extraction prompt for the Stop Hook.

    Args:
        messages_summary: Conversation summary string (from the hook system).

    Returns:
        Extraction prompt string, to be passed to Claude for analysis.
    """
    extractor = AutoExtractor()
    return extractor.build_extraction_prompt(
        messages=[{"role": "user", "content": messages_summary}],
    )

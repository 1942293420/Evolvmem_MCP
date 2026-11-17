#!/usr/bin/env python3
"""Isolated two-session Codex behavior gate for the EvolvMem context cutover.

Plan Task 11: prove cross-process Codex recall against a sentinel-guarded,
script-owned temporary EvolvMem library — never the real data directory or
the real Codex TOML config.

Session A (``codex exec --ephemeral --approve-for-me --json``) stores a
unique high-entropy canary through the MCP write tool; session B (a fresh
``codex exec --ephemeral --json``) is asked to recall the decision and its
details without naming any tool. The event streams must show:

1. ``context_session_start`` exactly once before the first substantive answer;
2. a ``context_search`` that hits the exact canary context ID;
3. a ``context_read(layer=l2)`` for the same ID;
4. an automatic block within the configured L1 budget that never contains
   the L2-only sentinel suffix;
5. no unrelated neighbor padding the search ``top_k``.

Privacy contract: the public report carries only process/session labels,
ordered whitelisted tool names, integer IDs, counts, durations, and
pass/fail reason codes — never tool arguments, prompts/queries, canary
text, memory bodies, or filesystem paths.

Isolation mechanics:

- the library lives in ``tempfile.mkdtemp(prefix=TEMP_PREFIX)`` with a
  sentinel file this script owns; cleanup deletes only that exact
  sentinel-verified directory;
- with no ``--model-file``, the engine-less library cannot satisfy the
  primary vector invariant after a write (every write durably marks the
  context vector index dirty). An explicit isolated FTS-only allowance —
  refused for any non-temp or sentinel-less directory — reconciles the
  disposable vector cache with deterministic placeholder vectors so the
  count/dirty invariant holds while retrieval relevance stays purely
  lexical (the MCP subprocess has no embedding engine, so the vector
  channel is never queried);
- MCP env overrides (``EVOLVMEM_DATA_DIR``, adapter ``codex``, mode
  ``primary``) reach Codex through ``-c`` CLI overrides only; the user's
  TOML is never edited, and any pre-existing ``evolvmem`` registration is
  disabled for the spawned sessions;
- child processes run under enforced timeouts; only the process groups this
  script started are terminated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

TEMP_PREFIX = "evolvmem-codex-acceptance."
SENTINEL_FILENAME = ".evolvmem-codex-acceptance.sentinel"
_SENTINEL_MAGIC = "evolvmem-codex-acceptance"
_SENTINEL_RE = re.compile(rf"^{_SENTINEL_MAGIC}\n[0-9a-f]{{32}}\n$")

# Tool names safe to publish; anything else is reported as "<unrelated>".
KNOWN_TOOL_NAMES = frozenset({
    "memory_search", "memory_status", "memory_add", "memory_replace",
    "memory_remove", "memory_consolidate",
    "context_session_start", "context_search", "context_read",
    "context_status",
})
UNRELATED_TOOL_LABEL = "<unrelated>"

# Stable pass/fail reason codes (the only diagnostic strings in reports).
REASON_CODES = frozenset({
    "timeout",
    "malformed_event_stream",
    "turn_failed",
    "usage_limited",
    "tool_error",
    "missing_session_start",
    "duplicate_session_start",
    "session_start_after_first_answer",
    "missing_context_search",
    "search_missed_canary",
    "unrelated_search_neighbor",
    "missing_context_read_l2",
    "read_id_mismatch",
    "auto_block_over_budget",
    "l2_leak_in_auto_block",
    "missing_memory_add",
    "missing_returned_ids",
    "isolated_library_not_ready",
    "session_not_run",
    "harness_error",
    "privacy_breach",
})

_READ_LAYERS = frozenset({"l1", "l2"})


class HarnessError(Exception):
    """A harness invariant broke; messages never carry content or paths."""


class AllowanceRejected(HarnessError):
    """The isolated FTS-only allowance was requested for a foreign path."""


class PrivacyBreach(HarnessError):
    """A forbidden token would have entered the public report."""


# ---- event-stream parsing (codex exec --json, CLI 0.147 schema) ----


@dataclass(frozen=True, slots=True)
class ToolCallRecord:
    """One completed MCP tool call, reduced to safe fields only."""

    ordinal: int
    tool: str  # whitelisted name or "<unrelated>"
    status: str  # "completed" | "failed"
    own_server: bool
    legacy_id: int | None = None
    context_id: int | None = None
    result_ids: tuple[int, ...] = ()
    read_layer: str | None = None
    used_chars: int | None = None
    l2_leak: bool = False


@dataclass(frozen=True, slots=True)
class SessionTrace:
    """Content-free reduction of one Codex session event stream."""

    label: str
    event_count: int
    malformed_count: int
    tool_calls: tuple[ToolCallRecord, ...]
    first_answer_ordinal: int | None
    turn_failed: bool
    usage_limited: bool
    exit_code: int | None
    timed_out: bool
    duration_ms: int

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(call.tool for call in self.tool_calls)


def _as_positive_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _result_payload(item: dict) -> dict | None:
    """Parse the MCP text payload; the parsed body stays process-local."""
    result = item.get("result")
    if not isinstance(result, dict):
        return None
    chunks = []
    for block in result.get("content") or ():
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                chunks.append(text)
    if not chunks:
        structured = result.get("structured_content")
        return structured if isinstance(structured, dict) else None
    try:
        payload = json.loads("".join(chunks))
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _reduce_tool_call(
    ordinal: int, item: dict, *, expected_server: str | None, l2_sentinel: str
) -> ToolCallRecord:
    raw_tool = item.get("tool")
    own_server = expected_server is None or item.get("server") == expected_server
    whitelisted = isinstance(raw_tool, str) and raw_tool in KNOWN_TOOL_NAMES
    tool = raw_tool if (own_server and whitelisted) else UNRELATED_TOOL_LABEL
    failed = (
        item.get("error") is not None or item.get("status") != "completed"
    )
    record = dict(
        ordinal=ordinal,
        tool=tool,
        status="failed" if failed else "completed",
        own_server=own_server,
    )
    if failed or not whitelisted or not own_server:
        return ToolCallRecord(**record)
    payload = _result_payload(item)
    if payload is None:
        return ToolCallRecord(**record)
    if tool == "memory_add":
        record["legacy_id"] = _as_positive_int(payload.get("id"))
        record["context_id"] = _as_positive_int(payload.get("context_id"))
    elif tool == "context_session_start":
        used = payload.get("used_chars")
        record["used_chars"] = used if isinstance(used, int) else None
        block = payload.get("block")
        # The block is inspected for the sentinel and discarded immediately.
        record["l2_leak"] = (
            isinstance(block, str) and bool(l2_sentinel)
            and l2_sentinel in block
        )
        selected = payload.get("selected_ids")
        if isinstance(selected, list):
            record["result_ids"] = tuple(
                i for i in (_as_positive_int(v) for v in selected)
                if i is not None
            )
    elif tool == "context_search":
        results = payload.get("results")
        if isinstance(results, list):
            record["result_ids"] = tuple(
                i
                for i in (
                    _as_positive_int(r.get("id"))
                    for r in results
                    if isinstance(r, dict)
                )
                if i is not None
            )
    elif tool == "context_read":
        record["context_id"] = _as_positive_int(payload.get("id"))
        layer = payload.get("layer")
        record["read_layer"] = layer if layer in _READ_LAYERS else None
    return ToolCallRecord(**record)


def _is_usage_limit(message: object) -> bool:
    return isinstance(message, str) and "usage limit" in message.lower()


def parse_session_events(
    text: str,
    *,
    label: str,
    l2_sentinel: str,
    expected_server: str | None = None,
    exit_code: int | None = None,
    timed_out: bool = False,
    duration_ms: int = 0,
) -> SessionTrace:
    """Reduce one ``codex exec --json`` stream to a content-free trace.

    Malformed lines are counted and skipped; ``item.started`` events are
    ignored so each call is counted once at completion; error messages are
    classified (usage limit) but never retained.
    """
    tool_calls: list[ToolCallRecord] = []
    event_count = 0
    malformed = 0
    first_answer_ordinal: int | None = None
    turn_failed = False
    usage_limited = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed += 1
            continue
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            malformed += 1
            continue
        ordinal = event_count
        event_count += 1
        event_type = event["type"]
        if event_type == "item.completed":
            item = event.get("item")
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "mcp_tool_call":
                tool_calls.append(
                    _reduce_tool_call(
                        ordinal, item,
                        expected_server=expected_server,
                        l2_sentinel=l2_sentinel,
                    )
                )
            elif item_type == "agent_message" and first_answer_ordinal is None:
                first_answer_ordinal = ordinal
        elif event_type == "error":
            turn_failed = True
            usage_limited = usage_limited or _is_usage_limit(event.get("message"))
        elif event_type == "turn.failed":
            turn_failed = True
            error = event.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            usage_limited = usage_limited or _is_usage_limit(message)
    return SessionTrace(
        label=label,
        event_count=event_count,
        malformed_count=malformed,
        tool_calls=tuple(tool_calls),
        first_answer_ordinal=first_answer_ordinal,
        turn_failed=turn_failed,
        usage_limited=usage_limited,
        exit_code=exit_code,
        timed_out=timed_out,
        duration_ms=duration_ms,
    )


# ---- pass/fail evaluation (reason codes only) ----


def _shared_reasons(trace: SessionTrace) -> list[str]:
    reasons = []
    if trace.timed_out:
        reasons.append("timeout")
    if trace.malformed_count:
        reasons.append("malformed_event_stream")
    if trace.usage_limited:
        reasons.append("usage_limited")
    if trace.turn_failed:
        reasons.append("turn_failed")
    return reasons


def evaluate_writer_session(trace: SessionTrace) -> tuple[str, ...]:
    """Session A: one completed memory_add returning both integer IDs.

    Tool errors after a successful write are tolerated here: under the
    FTS-only allowance the freshly written library is known-degraded until
    the harness reconciles the disposable vector cache.
    """
    reasons = _shared_reasons(trace)
    adds = [
        call for call in trace.tool_calls
        if call.own_server and call.tool == "memory_add"
        and call.status == "completed"
    ]
    if not adds:
        reasons.append("missing_memory_add")
    elif not any(
        call.legacy_id is not None and call.context_id is not None
        for call in adds
    ):
        reasons.append("missing_returned_ids")
    return tuple(dict.fromkeys(reasons))


def evaluate_recall_session(
    trace: SessionTrace, *, canary_context_id: int, l1_budget_chars: int
) -> tuple[str, ...]:
    """Session B: the five behavioral requirements of plan Task 11."""
    reasons = _shared_reasons(trace)
    own_calls = [call for call in trace.tool_calls if call.own_server]
    if any(call.status == "failed" for call in own_calls):
        reasons.append("tool_error")

    starts = [
        call for call in own_calls
        if call.tool == "context_session_start" and call.status == "completed"
    ]
    if not starts:
        reasons.append("missing_session_start")
    else:
        if len(starts) > 1:
            reasons.append("duplicate_session_start")
        first = starts[0]
        if (
            trace.first_answer_ordinal is not None
            and first.ordinal > trace.first_answer_ordinal
        ):
            reasons.append("session_start_after_first_answer")
        for call in starts:
            if (
                call.used_chars is not None
                and call.used_chars > l1_budget_chars
            ):
                reasons.append("auto_block_over_budget")
            if call.l2_leak:
                reasons.append("l2_leak_in_auto_block")

    searches = [
        call for call in own_calls
        if call.tool == "context_search" and call.status == "completed"
    ]
    if not searches:
        reasons.append("missing_context_search")
    else:
        if not any(canary_context_id in call.result_ids for call in searches):
            reasons.append("search_missed_canary")
        if any(
            any(hit != canary_context_id for hit in call.result_ids)
            for call in searches
        ):
            reasons.append("unrelated_search_neighbor")

    l2_reads = [
        call for call in own_calls
        if call.tool == "context_read" and call.status == "completed"
        and call.read_layer == "l2"
    ]
    if not l2_reads:
        reasons.append("missing_context_read_l2")
    elif not any(call.context_id == canary_context_id for call in l2_reads):
        reasons.append("read_id_mismatch")
    return tuple(dict.fromkeys(reasons))


# ---- privacy-safe public report ----


def public_session_summary(
    trace: SessionTrace, reason_codes: tuple[str, ...]
) -> dict:
    """Labels, ordered tool names, integer IDs, counts, durations, codes."""
    adds = [
        call for call in trace.tool_calls
        if call.own_server and call.tool == "memory_add"
        and call.status == "completed"
        and call.legacy_id is not None and call.context_id is not None
    ]
    starts = [
        call for call in trace.tool_calls
        if call.own_server and call.tool == "context_session_start"
        and call.status == "completed"
    ]
    reads = [
        call for call in trace.tool_calls
        if call.own_server and call.tool == "context_read"
        and call.status == "completed"
    ]
    return {
        "label": trace.label,
        "event_count": trace.event_count,
        "malformed_count": trace.malformed_count,
        "tool_call_count": len(trace.tool_calls),
        "tool_names": [
            call.tool if call.own_server else UNRELATED_TOOL_LABEL
            for call in trace.tool_calls
        ],
        "legacy_id": adds[0].legacy_id if adds else None,
        "added_context_id": adds[0].context_id if adds else None,
        "read_context_id": reads[0].context_id if reads else None,
        "used_chars": starts[0].used_chars if starts else None,
        "duration_ms": trace.duration_ms,
        "exit_code": trace.exit_code,
        "timed_out": trace.timed_out,
        "turn_failed": trace.turn_failed,
        "usage_limited": trace.usage_limited,
        "reason_codes": list(reason_codes),
    }


def build_report(
    *,
    passed: bool,
    sessions: dict[str, dict],
    cleanup: str,
    reason_codes: tuple[str, ...],
) -> dict:
    return {
        "passed": bool(passed),
        "reason_codes": list(reason_codes),
        "sessions": sessions,
        "cleanup": cleanup,
    }


def assert_privacy_safe(report: dict, forbidden_tokens) -> None:
    """Fail closed if any content/path token would enter the report."""
    blob = json.dumps(report, ensure_ascii=False)
    for token in forbidden_tokens:
        if token and token in blob:
            raise PrivacyBreach("forbidden token would enter the report")


# ---- guarded temporary library ----


def require_isolated_fts_only_allowance(path: Path) -> None:
    """The FTS-only vector reconciliation exists only for a library this
    script created: a direct child of the system temp dir with the harness
    prefix and a well-formed sentinel. Anything else is refused."""
    resolved = Path(path).resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    if resolved.parent != temp_root or not resolved.name.startswith(TEMP_PREFIX):
        raise AllowanceRejected("not a harness-owned temporary directory")
    sentinel = resolved / SENTINEL_FILENAME
    try:
        content = sentinel.read_text(encoding="utf-8")
    except OSError:
        raise AllowanceRejected("missing sentinel") from None
    if not _SENTINEL_RE.match(content):
        raise AllowanceRejected("sentinel is malformed or foreign")


def _placeholder_vector(item_id: int, dim: int):
    """Deterministic unit vector; relevance stays purely lexical because
    the spawned MCP servers have no embedding engine to query with."""
    import numpy as np

    seed = int.from_bytes(
        hashlib.sha256(f"{_SENTINEL_MAGIC}:{item_id}".encode()).digest()[:8],
        "big",
    )
    vector = np.random.default_rng(seed).standard_normal(dim)
    return (vector / np.linalg.norm(vector)).astype(np.float32)


class IsolatedLibrary:
    """A sentinel-guarded temporary EvolvMem data directory."""

    def __init__(self, path: Path, token: str) -> None:
        self.path = path
        self._token = token

    @classmethod
    def create(cls) -> "IsolatedLibrary":
        path = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
        token = secrets.token_hex(16)
        sentinel = path / SENTINEL_FILENAME
        fd = os.open(str(sentinel), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"{_SENTINEL_MAGIC}\n{token}\n")
        return cls(path, token)

    def make_config(self):
        from evolvmem.config import Config

        return Config(data_dir=self.path)

    def _verify_owned(self) -> None:
        require_isolated_fts_only_allowance(self.path)
        content = (self.path / SENTINEL_FILENAME).read_text(encoding="utf-8")
        if content != f"{_SENTINEL_MAGIC}\n{self._token}\n":
            raise AllowanceRejected("sentinel token mismatch")

    def _open_service(self):
        from evolvmem.context_models import ContextMode
        from evolvmem.context_service import ContextService

        config = self.make_config()
        service = ContextService(config)
        service.initialize(mode=ContextMode.PRIMARY, adapter="codex")
        return service

    def initialize_schema(self) -> None:
        """Create the Context and legacy projection schemas, nothing else."""
        service = self._open_service()
        try:
            # Same idempotent bootstrap the MCP server performs; it is the
            # single place allowed to instantiate the legacy backend.
            service._legacy_backend()
        finally:
            service.close()

    def seed_distractor(self, *, nonce: str) -> int:
        """Add one unrelated global-scope item; returns its context ID.

        The distractor is lexically disjoint from the recall query, so any
        appearance in session B's search results proves top_k padding.
        """
        from evolvmem.legacy_models import LegacyAddRequest

        service = self._open_service()
        try:
            result = service.legacy_add(
                LegacyAddRequest(
                    key=f"acceptance:context:preference:coffee-brewing-{nonce}",
                    value="手冲咖啡偏好：水温 92 摄氏度，中细研磨，粉水比 1:15。",
                    attribute="preference",
                    tags=("acceptance-distractor",),
                    importance=3.0,
                )
            )
        finally:
            service.close()
        if result.context_id is None:
            raise HarnessError("distractor write returned no context id")
        return result.context_id

    def append_l2_sentinel(self, context_id: int, sentinel: str) -> None:
        """Append the L2-only sentinel suffix to exactly one item's L2 layer.

        L0/L1 stay untouched (the projection-lag invariant compares L1), so
        the automatic block can never contain the suffix while the exact L2
        read must. FTS triggers keep the index consistent.
        """
        self._verify_owned()
        connection = sqlite3.connect(str(self.path / "memory.db"))
        try:
            cursor = connection.execute(
                "UPDATE context_layers SET content = content || ? "
                "WHERE item_id = ? AND layer = 'l2'",
                ("\n[[" + sentinel + "]]", context_id),
            )
            if cursor.rowcount != 1:
                raise HarnessError("canary L2 layer not found exactly once")
            connection.commit()
        finally:
            connection.close()

    def reconcile_vectors_fts_only(self) -> None:
        """Rebuild the disposable context vector cache with placeholders.

        Guarded by the isolated FTS-only allowance; refused for any
        non-temp/sentinel-less directory.
        """
        require_isolated_fts_only_allowance(self.path)
        from evolvmem.context_store import ContextStore
        from evolvmem.vector_index import VectorIndex

        config = self.make_config()
        store = ContextStore(config)
        store.initialize()
        try:
            documents = store.list_vector_documents()
        finally:
            store.close()
        index = VectorIndex(config, path=config.context_vector_path)
        index.initialize(dim=config.embedding_dim)
        try:
            # rebuild() creates a fresh index, saves it, and clears the
            # durable dirty marker left by the engine-less writes.
            index.rebuild(
                [document.item_id for document in documents],
                [
                    _placeholder_vector(document.item_id, config.embedding_dim)
                    for document in documents
                ],
            )
        finally:
            index.close()

    def reconcile_vectors_with_model(self) -> None:
        """Real-embedding reconciliation when --model-file was supplied."""
        require_isolated_fts_only_allowance(self.path)
        from evolvmem.context_store import ContextStore
        from evolvmem.context_vector_sync import ContextVectorSynchronizer
        from evolvmem.embedding import EmbeddingEngine
        from evolvmem.vector_index import VectorIndex

        config = self.make_config()
        engine = EmbeddingEngine(config)
        engine.initialize()
        if not engine.is_loaded:
            raise HarnessError("supplied test model did not load")
        try:
            store = ContextStore(config)
            store.initialize()
            try:
                synchronizer = ContextVectorSynchronizer(
                    config,
                    store,
                    VectorIndex(config, path=config.context_vector_path),
                    engine,
                )
                report = synchronizer.rebuild_active_l0()
            finally:
                store.close()
        finally:
            engine.close()
        if report.status != "synchronized":
            raise HarnessError("vector reconciliation failed")

    def primary_ready(self) -> bool:
        """Re-evaluate primary readiness the way the MCP server does."""
        service = self._open_service()
        try:
            try:
                service.vector_index.initialize(dim=service.config.embedding_dim)
            except Exception:
                pass  # unreadable cache is reported, not hidden
            service._refresh_health()  # the server's per-call evaluator
            return bool(service.status().ready)
        finally:
            service.close()

    def cleanup(self) -> bool:
        """Delete only the sentinel-verified exact temp directory."""
        try:
            self._verify_owned()
        except AllowanceRejected:
            return False
        shutil.rmtree(self.path)
        return True


# ---- codex invocation (CLI overrides only; user TOML never edited) ----


def _toml_basic_string(value: object) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    escaped = "".join(
        char if " " <= char < "\x7f" else f"\\u{ord(char):04X}"
        for char in escaped
    )
    return f'"{escaped}"'


def codex_config_overrides(
    *, server_name: str, data_dir: Path, python_bin: str, plugin_root: Path
) -> tuple[str, ...]:
    base = f"mcp_servers.{server_name}"
    return (
        f"{base}.command={_toml_basic_string(python_bin)}",
        f'{base}.args=["-m", "evolvmem.mcp_server"]',
        f"{base}.env.EVOLVMEM_DATA_DIR={_toml_basic_string(data_dir)}",
        f'{base}.env.EVOLVMEM_ADAPTER="codex"',
        f'{base}.env.EVOLVMEM_CONTEXT_MODE="primary"',
        f"{base}.env.PYTHONPATH={_toml_basic_string(plugin_root)}",
        # Read-only context tools run unimpeded; writes stay approval-gated.
        f'{base}.default_tools_approval_mode="writes"',
        f"{base}.startup_timeout_sec=30",
        f"{base}.tool_timeout_sec=60",
        # Never let a pre-existing real registration serve real memory.
        "mcp_servers.evolvmem.enabled=false",
    )


def build_codex_argv(
    *,
    codex_bin: str,
    workdir: Path,
    server_name: str,
    data_dir: Path,
    python_bin: str,
    plugin_root: Path,
    prompt: str,
    approve_for_me: bool,
) -> list[str]:
    argv = [
        codex_bin, "exec", "--ephemeral", "--json",
        "--skip-git-repo-check", "-C", str(workdir),
    ]
    if approve_for_me:
        argv.append("--approve-for-me")
    for override in codex_config_overrides(
        server_name=server_name,
        data_dir=data_dir,
        python_bin=python_bin,
        plugin_root=plugin_root,
    ):
        argv += ["-c", override]
    argv.append(prompt)
    return argv


@dataclass(frozen=True, slots=True)
class RawRun:
    jsonl: str
    exit_code: int | None
    timed_out: bool
    duration_ms: int
    stderr: str = field(default="", repr=False)  # process-local only


def run_codex_session(argv: list[str], *, timeout_s: float, workdir: Path) -> RawRun:
    """Run one session with an enforced timeout; kill only our own group."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("EVOLVMEM_")
    }
    started = time.monotonic()
    process = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(workdir),
        env=env,
        start_new_session=True,
        text=True,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGKILL)  # our own child's group only
        stdout, stderr = process.communicate()
    return RawRun(
        jsonl=stdout,
        exit_code=process.returncode,
        timed_out=timed_out,
        duration_ms=int((time.monotonic() - started) * 1000),
        stderr=stderr,
    )


# ---- prompts (session B never names a tool) ----


def session_a_prompt(*, key: str, value: str, tags: tuple[str, ...]) -> str:
    tag_list = ", ".join(json.dumps(tag, ensure_ascii=False) for tag in tags)
    return (
        "这是一次隔离环境下的验收写入。请调用 evolvmem 的 memory_add 工具，"
        "原样存储下面这条记录（所有字段逐字使用，不要改写、不要翻译）：\n"
        f"key: {json.dumps(key, ensure_ascii=False)}\n"
        f"value: {json.dumps(value, ensure_ascii=False)}\n"
        'attribute: "constraint"\n'
        f"tags: [{tag_list}]\n"
        "importance: 8\n"
        "存储成功后，回复返回结果里的 id 和 context_id 两个整数，然后结束。"
    )


def session_b_prompt() -> str:
    return (
        "这是我们的一次全新会话。请回忆一下我们之前关于 Codex 切换验收"
        "做出的决策，然后给出该决策的完整细节和原文。"
    )


def make_canary(nonce: str) -> dict:
    """High-entropy nonce in key, value, and tags so L0 FTS identifies it."""
    key = f"acceptance:context:constraint:codex-cutover-{nonce}"
    value = (
        f"决策约定 {nonce}（decision record）：Codex 切换验收必须使用隔离"
        "临时库，禁止触碰真实记忆库。所有 context 召回在临时库内验证，"
        "验收结束后物理删除该库。"
    )
    tags = ("acceptance", "codex-cutover", nonce)
    return {"key": key, "value": value, "tags": tags}


# ---- CLI ----


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument(
        "--workdir",
        default=str(Path(__file__).resolve().parent.parent),
        help="workspace directory for both Codex sessions",
    )
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter for the MCP server subprocess")
    parser.add_argument("--timeout-s", type=float, default=300.0,
                        help="per-session process timeout")
    parser.add_argument("--model-file", default=None,
                        help="optional test GGUF model copied into the "
                             "isolated library; omitted means the explicit "
                             "isolated FTS-only allowance is used")
    parser.add_argument("--json", action="store_true",
                        help="print the JSON report")
    parser.add_argument("--experience-suite", action="store_true",
                        help="run the isolated local experience fixture suite")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    if args.experience_suite:
        from scripts import experience_acceptance
        return experience_acceptance.main(["--json"] if args.json else [])
    workdir = Path(args.workdir).resolve()
    plugin_root = Path(__file__).resolve().parent.parent
    if not workdir.is_dir():
        print("workdir does not exist", file=sys.stderr)
        return 2

    nonce = secrets.token_hex(8)
    l2_sentinel = f"L2-ONLY-SENTINEL-{secrets.token_hex(8)}"
    server_name = f"evolvmem_acceptance_{secrets.token_hex(4)}"
    canary = make_canary(nonce)

    library = IsolatedLibrary.create()
    trace_a = trace_b = None
    reasons_a: tuple[str, ...] = ()
    reasons_b: tuple[str, ...] = ()
    harness_reasons: list[str] = []
    forbidden = [
        nonce, l2_sentinel, canary["key"], canary["value"],
        str(library.path), str(workdir),
    ]
    try:
        library.initialize_schema()
        if args.model_file:
            model = Path(args.model_file)
            if not model.is_file():
                raise HarnessError("supplied test model is not a file")
            target = library.path / "models" / library.make_config().embedding_model_filename
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(model, target)

        raw_a = run_codex_session(
            build_codex_argv(
                codex_bin=args.codex_bin, workdir=workdir,
                server_name=server_name, data_dir=library.path,
                python_bin=args.python, plugin_root=plugin_root,
                prompt=session_a_prompt(
                    key=canary["key"], value=canary["value"],
                    tags=canary["tags"],
                ),
                approve_for_me=True,
            ),
            timeout_s=args.timeout_s, workdir=workdir,
        )
        trace_a = parse_session_events(
            raw_a.jsonl, label="A", l2_sentinel=l2_sentinel,
            expected_server=server_name, exit_code=raw_a.exit_code,
            timed_out=raw_a.timed_out, duration_ms=raw_a.duration_ms,
        )
        reasons_a = evaluate_writer_session(trace_a)

        if reasons_a:
            reasons_b = ("session_not_run",)
        else:
            canary_context_id = next(
                call.context_id for call in trace_a.tool_calls
                if call.own_server and call.tool == "memory_add"
                and call.status == "completed" and call.context_id is not None
            )
            library.seed_distractor(nonce=nonce)
            library.append_l2_sentinel(canary_context_id, l2_sentinel)
            if args.model_file:
                library.reconcile_vectors_with_model()
            else:
                library.reconcile_vectors_fts_only()
            if not library.primary_ready():
                reasons_b = ("isolated_library_not_ready",)
            else:
                raw_b = run_codex_session(
                    build_codex_argv(
                        codex_bin=args.codex_bin, workdir=workdir,
                        server_name=server_name, data_dir=library.path,
                        python_bin=args.python, plugin_root=plugin_root,
                        prompt=session_b_prompt(),
                        approve_for_me=False,
                    ),
                    timeout_s=args.timeout_s, workdir=workdir,
                )
                trace_b = parse_session_events(
                    raw_b.jsonl, label="B", l2_sentinel=l2_sentinel,
                    expected_server=server_name, exit_code=raw_b.exit_code,
                    timed_out=raw_b.timed_out, duration_ms=raw_b.duration_ms,
                )
                reasons_b = evaluate_recall_session(
                    trace_b,
                    canary_context_id=canary_context_id,
                    l1_budget_chars=library.make_config().context_inject_max_chars,
                )
    except HarnessError as exc:
        print(f"harness error: {exc}", file=sys.stderr)
        harness_reasons.append("harness_error")
    except Exception:
        import traceback

        traceback.print_exc(file=sys.stderr)
        harness_reasons.append("harness_error")
    finally:
        # Children are already reaped; delete only the sentinel-verified dir.
        try:
            cleanup = "complete" if library.cleanup() else "failed"
        except Exception:
            cleanup = "failed"

    sessions = {}
    if trace_a is not None:
        sessions["A"] = public_session_summary(trace_a, reasons_a)
    if trace_b is not None:
        sessions["B"] = public_session_summary(trace_b, reasons_b)
    all_reasons = tuple(
        dict.fromkeys(harness_reasons + list(reasons_a) + list(reasons_b))
    )
    passed = not all_reasons
    report = build_report(
        passed=passed, sessions=sessions, cleanup=cleanup,
        reason_codes=all_reasons,
    )
    try:
        assert_privacy_safe(report, forbidden)
    except PrivacyBreach:
        report = build_report(
            passed=False, sessions={}, cleanup=cleanup,
            reason_codes=("privacy_breach",),
        )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"passed={report['passed']} cleanup={report['cleanup']}")
        if report["reason_codes"]:
            print("reason_codes: " + ", ".join(report["reason_codes"]))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

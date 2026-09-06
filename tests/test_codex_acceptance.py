"""Deterministic tests for the isolated two-session Codex cutover gate.

The real behavioral gate (``scripts/accept_codex_cutover.py``) is not a CI
test: it spawns the authenticated Codex CLI against a sentinel-guarded
temporary library. Everything else — the JSONL event parser, the pass/fail
evaluator, the privacy-safe report contract, and the temporary-library
guards — is deterministic and tested here offline.

Synthetic event streams mirror the codex-cli 0.147 ``exec --json`` schema
(``codex-rs/exec/src/exec_events.rs``): dotted envelope types
(``thread.started``/``item.completed``/``turn.failed``) and snake_case
``ThreadItem`` payloads (``mcp_tool_call``/``agent_message``).
"""

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from scripts import accept_codex_cutover as acc


# Content-bearing tokens that must never leak into the public report.
CANARY = "决策约定 CANARY9f8e7d6c5b4a：Codex 切换验收使用隔离临时库。"
SENTINEL = "L2-ONLY-SENTINEL-a1b2c3d4e5f6"
QUERY_TEXT = "回忆之前的决策"
FAKE_PATH = "/home/somebody/secret/memory.db"
CANARY_ID = 7
OTHER_ID = 8
L1_BUDGET = 6000


# ---- synthetic codex exec --json event fixtures ----


def _mcp_completed(tool, payload, *, server="evolvmem_acceptance_abc123",
                   arguments=None):
    return {
        "type": "item.completed",
        "item": {
            "id": f"item_{tool}",
            "type": "mcp_tool_call",
            "server": server,
            "tool": tool,
            "arguments": arguments if arguments is not None else {},
            "result": {
                "content": [
                    {"type": "text",
                     "text": json.dumps(payload, ensure_ascii=False)}
                ],
                "structured_content": None,
            },
            "error": None,
            "status": "completed",
        },
    }


def _mcp_failed(tool, message):
    return {
        "type": "item.completed",
        "item": {
            "id": f"item_{tool}",
            "type": "mcp_tool_call",
            "server": "evolvmem_acceptance_abc123",
            "tool": tool,
            "arguments": {"id": CANARY_ID, "query": QUERY_TEXT},
            "result": None,
            "error": {"message": message},
            "status": "failed",
        },
    }


def _agent_message(text):
    return {
        "type": "item.completed",
        "item": {"id": "msg", "type": "agent_message", "text": text},
    }


def _session_start_event(*, block, used_chars=420, selected=(CANARY_ID,)):
    return _mcp_completed(
        "context_session_start",
        {
            "block": block,
            "selected_ids": list(selected),
            "used_chars": used_chars,
            "excluded_counts": [],
        },
        arguments={"project": "proj", "query": QUERY_TEXT},
    )


def _search_event(ids=(CANARY_ID,)):
    return _mcp_completed(
        "context_search",
        {"results": [{"id": i} for i in ids], "count": len(ids)},
        arguments={"query": QUERY_TEXT},
    )


def _read_event(context_id=CANARY_ID, layer="l2"):
    return _mcp_completed(
        "context_read",
        {"id": context_id, "layer": layer,
         "content": CANARY + "\n[[" + SENTINEL + "]]"},
        arguments={"id": context_id, "layer": layer},
    )


def _memory_add_event():
    return _mcp_completed(
        "memory_add",
        {"status": "added", "id": 12, "context_id": CANARY_ID,
         "old_context_id": None, "available_layers": ["l0", "l1", "l2"]},
        arguments={"key": "k", "value": CANARY, "tags": ["acceptance"]},
    )


def _to_jsonl(events):
    return "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)


def _recall_events():
    block = f"[BEGIN EVOLVMEM CONTEXT HISTORY]\n### context #{CANARY_ID}\n{CANARY}\n[END EVOLVMEM CONTEXT HISTORY]"
    return [
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "turn.started"},
        _session_start_event(block=block),
        _search_event(),
        _read_event(),
        _agent_message(f"完整细节：{CANARY} [[{SENTINEL}]]"),
        {"type": "turn.completed", "usage": {"input_tokens": 1}},
    ]


def _parse_recall(events, **kwargs):
    params = dict(label="B", l2_sentinel=SENTINEL,
                  expected_server="evolvmem_acceptance_abc123",
                  exit_code=0, duration_ms=1234)
    params.update(kwargs)
    return acc.parse_session_events(_to_jsonl(events), **params)


def _evaluate_recall(trace):
    return acc.evaluate_recall_session(
        trace, canary_context_id=CANARY_ID, l1_budget_chars=L1_BUDGET
    )


# ---- parser: happy path and safe-field extraction ----


def test_parse_successful_recall_session():
    trace = _parse_recall(_recall_events())
    assert trace.label == "B"
    assert trace.event_count == 7
    assert trace.malformed_count == 0
    assert trace.tool_names == (
        "context_session_start", "context_search", "context_read",
    )
    start_call, search_call, read_call = trace.tool_calls
    assert start_call.used_chars == 420
    assert start_call.l2_leak is False
    assert search_call.result_ids == (CANARY_ID,)
    assert read_call.context_id == CANARY_ID
    assert read_call.read_layer == "l2"
    assert trace.first_answer_ordinal is not None
    assert _evaluate_recall(trace) == ()


def test_parse_successful_writer_session_captures_integer_ids():
    events = [
        {"type": "thread.started", "thread_id": "t-a"},
        {"type": "turn.started"},
        _memory_add_event(),
        _agent_message(f"已存储 {CANARY}"),
        {"type": "turn.completed", "usage": {}},
    ]
    trace = acc.parse_session_events(
        _to_jsonl(events), label="A", l2_sentinel=SENTINEL,
        exit_code=0, duration_ms=99,
    )
    assert trace.tool_names == ("memory_add",)
    call = trace.tool_calls[0]
    assert call.legacy_id == 12
    assert call.context_id == CANARY_ID
    assert acc.evaluate_writer_session(trace) == ()


# ---- evaluator failure scenarios (plan Task 11 Step 1) ----


def test_missing_session_start_fails():
    events = [e for e in _recall_events()
              if not (e["type"] == "item.completed"
                      and e["item"].get("tool") == "context_session_start")]
    reasons = _evaluate_recall(_parse_recall(events))
    assert "missing_session_start" in reasons


def test_answer_before_session_start_fails():
    events = [
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "turn.started"},
        _agent_message("先回答了实质内容"),
        _session_start_event(block="block"),
        _search_event(),
        _read_event(),
        _agent_message("最终回答"),
        {"type": "turn.completed", "usage": {}},
    ]
    reasons = _evaluate_recall(_parse_recall(events))
    assert "session_start_after_first_answer" in reasons


def test_duplicate_session_start_fails():
    events = _recall_events()
    events.insert(3, _session_start_event(block="again"))
    reasons = _evaluate_recall(_parse_recall(events))
    assert "duplicate_session_start" in reasons


def test_read_with_wrong_context_id_fails():
    events = [
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "turn.started"},
        _session_start_event(block="block"),
        _search_event(),
        _read_event(context_id=99),
        _agent_message("details"),
        {"type": "turn.completed", "usage": {}},
    ]
    reasons = _evaluate_recall(_parse_recall(events))
    assert "read_id_mismatch" in reasons
    assert "missing_context_read_l2" not in reasons


def test_read_on_l1_only_fails_the_l2_requirement():
    events = [
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "turn.started"},
        _session_start_event(block="block"),
        _search_event(),
        _read_event(layer="l1"),
        _agent_message("details"),
        {"type": "turn.completed", "usage": {}},
    ]
    reasons = _evaluate_recall(_parse_recall(events))
    assert "missing_context_read_l2" in reasons


def test_l2_sentinel_leaked_into_automatic_block_fails():
    events = _recall_events()
    events[2] = _session_start_event(block="prefix " + SENTINEL)
    trace = _parse_recall(events)
    assert trace.tool_calls[0].l2_leak is True
    assert "l2_leak_in_auto_block" in _evaluate_recall(trace)


def test_automatic_block_over_budget_fails():
    events = _recall_events()
    events[2] = _session_start_event(block="block", used_chars=L1_BUDGET + 1)
    assert "auto_block_over_budget" in _evaluate_recall(_parse_recall(events))


def test_missing_search_fails():
    events = [e for e in _recall_events()
              if not (e["type"] == "item.completed"
                      and e["item"].get("tool") == "context_search")]
    reasons = _evaluate_recall(_parse_recall(events))
    assert "missing_context_search" in reasons


def test_search_without_canary_hit_fails():
    events = _recall_events()
    events[3] = _search_event(ids=())
    reasons = _evaluate_recall(_parse_recall(events))
    assert "search_missed_canary" in reasons


def test_unrelated_neighbor_in_search_results_fails():
    events = _recall_events()
    events[3] = _search_event(ids=(CANARY_ID, OTHER_ID))
    reasons = _evaluate_recall(_parse_recall(events))
    assert "unrelated_search_neighbor" in reasons


def test_tool_error_fails_without_leaking_message():
    events = _recall_events()
    events[3] = _mcp_failed(
        "context_search", f"boom while reading {FAKE_PATH} for {CANARY}"
    )
    trace = _parse_recall(events)
    reasons = _evaluate_recall(trace)
    assert "tool_error" in reasons
    report = acc.public_session_summary(trace, reasons)
    blob = json.dumps(report, ensure_ascii=False)
    assert FAKE_PATH not in blob
    assert CANARY not in blob
    assert QUERY_TEXT not in blob


def test_timeout_fails():
    trace = _parse_recall(_recall_events(), timed_out=True, exit_code=None)
    assert "timeout" in _evaluate_recall(trace)


def test_malformed_events_are_counted_and_fail_closed():
    text = _to_jsonl(_recall_events())
    text += "this is not json\n"
    text += '{"type":"item.completed",'  # truncated
    text += "\n"
    trace = acc.parse_session_events(
        text, label="B", l2_sentinel=SENTINEL, exit_code=0, duration_ms=1
    )
    assert trace.malformed_count == 2
    assert "malformed_event_stream" in _evaluate_recall(trace)


def test_content_bearing_diagnostics_never_enter_report():
    events = _recall_events() + [
        {"type": "error",
         "message": f"failed at {FAKE_PATH} with {CANARY} and {QUERY_TEXT}"},
        {"type": "turn.failed",
         "error": {"message": f"path {FAKE_PATH} canary {CANARY}"}},
    ]
    trace = _parse_recall(events, exit_code=1)
    assert trace.turn_failed is True
    reasons = _evaluate_recall(trace)
    assert "turn_failed" in reasons
    report = acc.build_report(
        passed=False,
        sessions={"B": acc.public_session_summary(trace, reasons)},
        cleanup="complete",
        reason_codes=reasons,
    )
    blob = json.dumps(report, ensure_ascii=False)
    for forbidden in (CANARY, SENTINEL, QUERY_TEXT, FAKE_PATH):
        assert forbidden not in blob


def test_usage_limit_is_classified_without_message_text():
    events = [
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "turn.started"},
        {"type": "error", "message": "You've hit your usage limit. ..."},
        {"type": "turn.failed",
         "error": {"message": "You've hit your usage limit. ..."}},
    ]
    trace = _parse_recall(events, exit_code=1)
    assert trace.usage_limited is True
    assert trace.turn_failed is True
    reasons = _evaluate_recall(trace)
    assert "usage_limited" in reasons


def test_writer_session_missing_memory_add_fails():
    events = [
        {"type": "thread.started", "thread_id": "t-a"},
        {"type": "turn.started"},
        _agent_message("没有写入"),
        {"type": "turn.completed", "usage": {}},
    ]
    trace = acc.parse_session_events(
        _to_jsonl(events), label="A", l2_sentinel=SENTINEL,
        exit_code=0, duration_ms=5,
    )
    assert "missing_memory_add" in acc.evaluate_writer_session(trace)


def test_writer_session_without_returned_ids_fails():
    events = [
        {"type": "thread.started", "thread_id": "t-a"},
        {"type": "turn.started"},
        _mcp_completed("memory_add", {"status": "added"}),
        _agent_message("done"),
        {"type": "turn.completed", "usage": {}},
    ]
    trace = acc.parse_session_events(
        _to_jsonl(events), label="A", l2_sentinel=SENTINEL,
        exit_code=0, duration_ms=5,
    )
    assert "missing_returned_ids" in acc.evaluate_writer_session(trace)


def test_unrelated_server_tool_calls_cannot_satisfy_requirements():
    foreign = _mcp_completed(
        "context_search", {"results": [{"id": CANARY_ID}], "count": 1},
        server="someone_elses_server",
    )
    events = [
        {"type": "thread.started", "thread_id": "t-1"},
        {"type": "turn.started"},
        _session_start_event(block="block"),
        foreign,
        _read_event(),
        _agent_message("details"),
        {"type": "turn.completed", "usage": {}},
    ]
    trace = _parse_recall(events)
    assert "<unrelated>" in trace.tool_names
    assert "missing_context_search" in _evaluate_recall(trace)


def test_item_started_events_do_not_double_count_calls():
    started = {
        "type": "item.started",
        "item": {
            "id": "item_context_search", "type": "mcp_tool_call",
            "server": "evolvmem_acceptance_abc123", "tool": "context_search",
            "arguments": {"query": QUERY_TEXT}, "result": None,
            "error": None, "status": "in_progress",
        },
    }
    events = _recall_events()
    events.insert(3, started)
    trace = _parse_recall(events)
    assert trace.tool_names.count("context_search") == 1
    assert _evaluate_recall(trace) == ()


# ---- public report privacy contract ----


def test_public_report_strings_are_whitelisted():
    trace = _parse_recall(_recall_events())
    reasons = _evaluate_recall(trace)
    report = acc.build_report(
        passed=True,
        sessions={
            "A": acc.public_session_summary(
                acc.parse_session_events(
                    _to_jsonl([_memory_add_event()]), label="A",
                    l2_sentinel=SENTINEL, exit_code=0, duration_ms=3,
                ),
                (),
            ),
            "B": acc.public_session_summary(trace, reasons),
        },
        cleanup="complete",
        reason_codes=reasons,
    )
    allowed = (
        set(acc.KNOWN_TOOL_NAMES) | set(acc.REASON_CODES)
        | {"A", "B", "<unrelated>", "complete", "failed", "not_run"}
    )

    def strings(node):
        if isinstance(node, str):
            yield node
        elif isinstance(node, dict):
            for value in node.values():
                yield from strings(value)
        elif isinstance(node, (list, tuple)):
            for value in node:
                yield from strings(value)

    for value in strings(report):
        assert value in allowed, f"non-whitelisted string in report: {value!r}"


def test_privacy_guard_rejects_forbidden_tokens():
    with pytest.raises(acc.PrivacyBreach):
        acc.assert_privacy_safe(
            {"note": f"path {FAKE_PATH}"}, (FAKE_PATH,)
        )


# ---- temporary-library harness guards ----


def test_fts_only_allowance_rejects_non_temp_directory():
    with pytest.raises(acc.AllowanceRejected):
        acc.require_isolated_fts_only_allowance(Path.home())
    with pytest.raises(acc.AllowanceRejected):
        acc.require_isolated_fts_only_allowance(Path("relative/dir"))


def test_fts_only_allowance_rejects_temp_dir_without_sentinel():
    plain = Path(tempfile.mkdtemp())  # right parent, wrong prefix/sentinel
    try:
        with pytest.raises(acc.AllowanceRejected):
            acc.require_isolated_fts_only_allowance(plain)
    finally:
        import shutil
        shutil.rmtree(plain, ignore_errors=True)


def test_fts_only_allowance_rejects_tampered_sentinel():
    library = acc.IsolatedLibrary.create()
    try:
        (library.path / acc.SENTINEL_FILENAME).write_text("forged")
        with pytest.raises(acc.AllowanceRejected):
            acc.require_isolated_fts_only_allowance(library.path)
    finally:
        import shutil
        shutil.rmtree(library.path, ignore_errors=True)


def test_isolated_library_lifecycle_and_fts_only_readiness():
    """The guarded FTS-only allowance makes an engine-less primary ready."""
    library = acc.IsolatedLibrary.create()
    try:
        assert library.path.name.startswith(acc.TEMP_PREFIX)
        assert (library.path / acc.SENTINEL_FILENAME).is_file()
        library.initialize_schema()
        distractor_id = library.seed_distractor(nonce="deadbeefcafe")
        assert isinstance(distractor_id, int) and distractor_id >= 1
        # An unsynchronized FTS-only write leaves primary degraded...
        assert library.primary_ready() is False
        library.reconcile_vectors_fts_only()
        # ...and the explicit isolated allowance restores the invariant.
        assert library.primary_ready() is True
    finally:
        assert library.cleanup() is True
    assert not library.path.exists()


def test_cleanup_refuses_tampered_sentinel_and_keeps_directory():
    library = acc.IsolatedLibrary.create()
    (library.path / acc.SENTINEL_FILENAME).write_text("forged")
    try:
        assert library.cleanup() is False
        assert library.path.exists()
    finally:
        import shutil
        shutil.rmtree(library.path, ignore_errors=True)


def test_append_l2_sentinel_updates_only_the_l2_layer():
    library = acc.IsolatedLibrary.create()
    try:
        library.initialize_schema()
        context_id = library.seed_distractor(nonce="f00df00d")
        library.append_l2_sentinel(context_id, SENTINEL)
        db = sqlite3.connect(str(library.path / "memory.db"))
        try:
            rows = dict(
                db.execute(
                    "SELECT layer, content FROM context_layers WHERE item_id=?",
                    (context_id,),
                ).fetchall()
            )
        finally:
            db.close()
        assert SENTINEL in rows["l2"]
        assert SENTINEL not in rows["l0"]
        assert SENTINEL not in rows["l1"]
    finally:
        library.cleanup()


def test_append_l2_sentinel_rejects_unknown_id():
    library = acc.IsolatedLibrary.create()
    try:
        library.initialize_schema()
        with pytest.raises(acc.HarnessError):
            library.append_l2_sentinel(424242, SENTINEL)
    finally:
        library.cleanup()


# ---- codex invocation construction (never edits user TOML) ----


def test_codex_argv_passes_mcp_env_overrides_via_dash_c():
    argv = acc.build_codex_argv(
        codex_bin="codex",
        workdir=Path("/tmp/workdir"),
        server_name="evolvmem_acceptance_abc123",
        data_dir=Path("/tmp/evolvmem-codex-acceptance.x"),
        python_bin="/venv/bin/python",
        plugin_root=Path("/plugin"),
        prompt="store it",
        approve_for_me=True,
    )
    assert argv[0] == "codex"
    assert "exec" in argv and "--ephemeral" in argv and "--json" in argv
    assert "--approve-for-me" in argv
    overrides = [argv[i + 1] for i, a in enumerate(argv) if a == "-c"]
    joined = "\n".join(overrides)
    assert "mcp_servers.evolvmem_acceptance_abc123.env.EVOLVMEM_DATA_DIR" in joined
    assert 'mcp_servers.evolvmem_acceptance_abc123.env.EVOLVMEM_ADAPTER="codex"' in joined
    assert 'mcp_servers.evolvmem_acceptance_abc123.env.EVOLVMEM_CONTEXT_MODE="primary"' in joined
    assert "mcp_servers.evolvmem_acceptance_abc123.env.PYTHONPATH" in joined
    # A pre-existing real registration is disabled, never read or edited.
    assert "mcp_servers.evolvmem.enabled=false" in overrides
    # Write-capable tools stay behind approval; read-only context tools run.
    assert any(
        o == 'mcp_servers.evolvmem_acceptance_abc123.'
             'default_tools_approval_mode="writes"'
        for o in overrides
    )


def test_toml_basic_string_escapes_quotes_and_backslashes():
    assert acc._toml_basic_string('a"b\\c') == '"a\\"b\\\\c"'


def test_session_b_prompt_names_no_tool():
    prompt = acc.session_b_prompt()
    assert "context_" not in prompt
    assert "memory_" not in prompt
    assert "工具" not in prompt or "tool" not in prompt.lower()

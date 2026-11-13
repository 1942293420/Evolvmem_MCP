"""TDD tests for Stage 3 Task 3.4: Deterministic KeyMoment detector."""
from __future__ import annotations

import json
from pathlib import Path

from evolvmem.experience_v2.llm_meter import estimate_tokens, scan_unmetered_llm_calls
from evolvmem.experience_v2.pipeline.events import parse_session_events
from evolvmem.experience_v2.pipeline.history_reader import (
    HistorySession,
    HistorySessionSummary,
    scan_direct_sqlite_in_pipeline,
)

FIXTURE = Path(__file__).parent / "fixtures" / "labeled_moments_30.json"


def _make_session(sid: str, raw_lines: list[dict]) -> HistorySession:
    raw_bytes = ("\n".join(json.dumps(r, ensure_ascii=False) for r in raw_lines) + "\n").encode("utf-8")
    summary = HistorySessionSummary(
        session_id=sid,
        adapter="codex",
        source_format="codex-jsonl",
        project="demo-app",
        source_ref=f"archive:{sid}",
        library="stage3-fixture",
        external_session_id=sid,
        created_at="2026-11-13T10:00:00Z",
        has_raw_bytes=True,
    )
    return HistorySession(summary=summary, records=(), raw_bytes=raw_bytes)


def test_labeled_30_sessions_recall_and_precision_thresholds() -> None:
    from evolvmem.experience_v2.pipeline.moments import MOMENT_KINDS, detect_key_moments

    dataset = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(dataset) == 30

    tp = fp = fn = 0
    neg_triggered = []

    for item in dataset:
        parsed = parse_session_events(_make_session(item["session_id"], item["raw_lines"]))
        moments = detect_key_moments(parsed)
        got_kinds = {m.kind for m in moments}
        exp_kinds = set(item["expected_kinds"])

        for m in moments:
            assert m.kind in MOMENT_KINDS
            assert len(m.evidence_pointers) >= 1
            assert m.snippet_tokens <= 3000

        if not exp_kinds and got_kinds:
            neg_triggered.append((item["session_id"], sorted(got_kinds)))

        tp += len(got_kinds & exp_kinds)
        fp += len(got_kinds - exp_kinds)
        fn += len(exp_kinds - got_kinds)

    recall = tp / max(1, tp + fn)
    precision = tp / max(1, tp + fp)
    assert neg_triggered == [], f"False positives on routine sessions: {neg_triggered}"
    assert recall >= 0.80, f"Recall too low: {recall:.3f}"
    assert precision >= 0.80, f"Precision too low: {precision:.3f}"


def test_long_session_20k_tokens_is_compressed_under_3000_tokens() -> None:
    from evolvmem.experience_v2.pipeline.moments import detect_key_moments

    huge_log = ("INFO step heartbeat ok " * 2500).strip()  # > 10k chars / ~2.5k tokens per message
    rows = [{"type": "session_meta", "payload": {"id": "stress-20k", "cwd": "/workspace/demo-app"}}]
    for idx in range(8):
        rows.append({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"chunk {idx}: {huge_log}"}]}})
    rows.extend([
        {"type": "response_item", "payload": {"type": "function_call", "call_id": "c_fail", "name": "exec_command", "arguments": json.dumps({"cmd": "pytest tests/api.py"})}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c_fail", "output": json.dumps({"exit_code": 1, "stderr": "AssertionError: status 500\n" + huge_log})}},
        {"type": "response_item", "payload": {"type": "function_call", "call_id": "c_edit", "name": "apply_patch", "arguments": json.dumps({"path": "src/api.py", "action": "edit"})}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c_edit", "output": "ok"}},
        {"type": "response_item", "payload": {"type": "function_call", "call_id": "c_pass", "name": "exec_command", "arguments": json.dumps({"cmd": "pytest tests/api.py --retest"})}},
        {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c_pass", "output": json.dumps({"exit_code": 0, "stdout": "2 passed"})}},
    ])

    raw_bytes = ("\n".join(json.dumps(r) for r in rows) + "\n").encode("utf-8")
    assert estimate_tokens(raw_bytes.decode("utf-8")) > 20000

    parsed = parse_session_events(_make_session("stress-20k", rows))
    moments = detect_key_moments(parsed, max_snippet_tokens=3000)
    assert len(moments) >= 1
    for m in moments:
        assert 0 < m.snippet_tokens <= 3000
        assert estimate_tokens(m.snippet_text) <= 3000
        assert m.evidence_pointers[0]["session_id"] == "stress-20k"

    assert scan_direct_sqlite_in_pipeline(Path("evolvmem/experience_v2/pipeline")) == []
    assert scan_unmetered_llm_calls(Path("evolvmem/experience_v2")) == []

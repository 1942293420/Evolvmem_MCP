"""TDD tests for Stage 3 Task 3.3: Multi-client deterministic EventParser."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from evolvmem import transcript_formats
from evolvmem.experience_v2.llm_meter import scan_unmetered_llm_calls
from evolvmem.experience_v2.pipeline.history_reader import (
    HistoryRawRecord,
    HistorySession,
    HistorySessionSummary,
    scan_direct_sqlite_in_pipeline,
)

FIXTURE_DIR = Path(__file__).parent / "fixtures"
CLIENT_FILES = ("codex_10.json", "dsh_10.json", "claude_10.json", "kimi_10.json")


def _load_all_40() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for fname in CLIENT_FILES:
        items.extend(json.loads((FIXTURE_DIR / fname).read_text(encoding="utf-8")))
    return items


def _to_history_session(item: dict[str, Any], include_raw: bool = True) -> HistorySession:
    raw_bytes = ("\n".join(json.dumps(r, ensure_ascii=False) for r in item["raw_lines"]) + "\n").encode("utf-8")
    summary = HistorySessionSummary(
        session_id=item["session_id"],
        adapter=item["adapter"],
        source_format=item["source_format"],
        project=item["project"],
        source_ref=f"archive:{item['session_id']}",
        library="stage3-fixture",
        external_session_id=item["session_id"],
        created_at="2026-11-13T10:00:00Z",
        has_raw_bytes=include_raw,
    )
    records = (
        HistoryRawRecord(event_index=0, role="user", content="Investigate task"),
        HistoryRawRecord(event_index=1, role="assistant", content="Running check"),
    )
    return HistorySession(summary=summary, records=records, raw_bytes=raw_bytes if include_raw else None)


def test_parse_all_40_multi_client_fixtures_deterministic_zero_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    from evolvmem.experience_v2.pipeline.events import parse_session_events

    format_calls: list[str] = []
    orig_parse = transcript_formats.parse_transcript

    def spy_parse(raw: bytes, session_id: str, format: str = transcript_formats.CODEX_FORMAT):
        format_calls.append(format)
        return orig_parse(raw, session_id, format=format)

    monkeypatch.setattr(transcript_formats, "parse_transcript", spy_parse)

    items = _load_all_40()
    assert len(items) == 40

    for item in items:
        session = _to_history_session(item)
        parsed = parse_session_events(session)
        assert parsed.session_id == item["session_id"]
        assert parsed.adapter == item["adapter"]
        assert parsed.llm_calls_used == 0
        assert len(parsed.evidence_pointers) >= 1

        exp = item["expect"]
        assert any(o.exit_code is not None and o.exit_code != 0 for o in parsed.tool_outcomes) == exp["has_nonzero_exit"], item["session_id"]
        assert any(o.error_signature for o in parsed.tool_outcomes) == exp["has_error_sig"], item["session_id"]
        assert (len(parsed.retries) > 0) == exp["has_retry"], item["session_id"]
        assert (len(parsed.rollbacks) > 0) == exp["has_rollback"], item["session_id"]
        if exp["has_file_edit"]:
            assert len(parsed.file_edits) >= 1, item["session_id"]
        kinds = {u.kind for u in parsed.user_intents}
        if exp["user_intent"]:
            assert exp["user_intent"] in kinds, f"{item['session_id']} got {kinds}"
        if exp["fenced_trap"]:
            assert "preference" not in kinds and "correction" not in kinds and "abandon" not in kinds, f"{item['session_id']} trapped into {kinds}"

    assert format_calls.count("codex-jsonl") == 10
    assert format_calls.count("dsh-session-jsonl-v1") == 10


def test_fenced_code_blocks_and_inline_code_do_not_trigger_user_intents() -> None:
    from evolvmem.experience_v2.pipeline.events import classify_user_intent

    text_only_fence = (
        "Look at this example config:\n"
        "```bash\n"
        "# Always use pnpm and never use npm!\n"
        "# Actually replace localhost with 0.0.0.0, abandon old setup!\n"
        "```\n"
        "And avoid `never use npm` in inline snippets too. Does it compile?"
    )
    assert classify_user_intent(text_only_fence) == ()

    real_correction = "No, don't retry port 8080; actually switch to port 8090."
    assert "correction" in {s.kind for s in classify_user_intent(real_correction)}

    real_pref = "Always prefer uv over pip in our Python projects."
    assert "preference" in {s.kind for s in classify_user_intent(real_pref)}

    real_abandon = "Abandon the Redis lock approach and roll back to Postgres."
    assert "abandon" in {s.kind for s in classify_user_intent(real_abandon)}

    real_confirm = "Confirmed, that worked鈥攇o ahead with the migration."
    assert "confirmation" in {s.kind for s in classify_user_intent(real_confirm)}


def test_fallback_records_when_raw_bytes_missing_and_static_guards() -> None:
    from evolvmem.experience_v2.pipeline.events import parse_session_events

    session = HistorySession(
        summary=HistorySessionSummary(
            session_id="fallback-01",
            adapter="claude",
            source_format="claude-jsonl",
            project="demo-app",
            source_ref="conv:1",
            library="sqlite",
            external_session_id="ext-1",
            created_at="2026-11-13T10:00:00Z",
            has_raw_bytes=False,
        ),
        records=(
            HistoryRawRecord(event_index=0, role="user", content="Always keep migrations reversible."),
            HistoryRawRecord(event_index=1, role="tool", content="exit_code=1\nAssertionError: mismatch"),
            HistoryRawRecord(event_index=2, role="assistant", content="Fixed migration script."),
        ),
        raw_bytes=None,
    )
    parsed = parse_session_events(session)
    assert parsed.llm_calls_used == 0
    assert any(o.exit_code == 1 for o in parsed.tool_outcomes)
    assert any(u.kind == "preference" for u in parsed.user_intents)
    assert scan_direct_sqlite_in_pipeline(Path("evolvmem/experience_v2/pipeline")) == []
    assert scan_unmetered_llm_calls(Path("evolvmem/experience_v2")) == []

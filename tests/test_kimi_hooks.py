"""kimi_hooks module tests (pure helpers only; no LLM/IO)."""

from evolvmem.auto_extractor import CandidateMemory
from evolvmem.kimi_hooks import _project_from_wire, _split_summary_candidate


class TestProjectFromWire:
    def test_alias_mapping(self):
        path = ("/home/u/.kimi-code/sessions/wd_automation-control_1a32444e9dae"
                "/session_x/agents/main/wire.jsonl")
        aliases = {"automation-control": "automation_control"}
        assert _project_from_wire(path, aliases) == "automation_control"

    def test_no_alias_uses_dirname(self):
        path = ("/home/u/.kimi-code/sessions/wd_eva_1a32444e9dae"
                "/session_x/agents/main/wire.jsonl")
        assert _project_from_wire(path, {}) == "eva"

    def test_unrecognized_path_falls_back_to_general(self):
        assert _project_from_wire("/tmp/wire.jsonl", {}) == "general"

    def test_sanitizes_dirname(self):
        path = ("/home/u/.kimi-code/sessions/wd_My Shop_1a32444e9dae"
                "/session_x/agents/main/wire.jsonl")
        assert _project_from_wire(path, {}) == "my_shop"


class TestSplitSummaryCandidate:
    def test_pulls_out_summary(self):
        summary = CandidateMemory(key="SESSION_SUMMARY", value="本次做了 X")
        other = CandidateMemory(key="p:t:fact:x", value="某事实")
        s, rest = _split_summary_candidate([other, summary])
        assert s is summary
        assert rest == [other]

    def test_no_summary_returns_all(self):
        other = CandidateMemory(key="p:t:fact:x", value="某事实")
        s, rest = _split_summary_candidate([other])
        assert s is None
        assert rest == [other]

    def test_case_insensitive_key_match(self):
        summary = CandidateMemory(key="session_summary", value="本次做了 X")
        s, rest = _split_summary_candidate([summary])
        assert s is summary
        assert rest == []

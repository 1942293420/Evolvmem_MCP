"""The restructure runbook documents safe operation and rollback."""
import json
from pathlib import Path


def test_restructure_runbook_contains_copy_first_and_rollback():
    text = Path("docs/context-core.md").read_text(encoding="utf-8")
    for phrase in (
        "capability_memories", "project_archives", "project_events",
        "knowledge_structure_mode", "dry-run", "set-mode --data-dir ~/.claude/evolvmem legacy",
    ):
        assert phrase in text
    assert "不得删除原始会话" in text
    assert "项目摘要不得进入开发能力检索" in text


def test_example_config_defaults_to_legacy():
    data = json.loads(Path("examples/config.example.json").read_text(encoding="utf-8"))
    assert data["knowledge_structure_mode"] == "legacy"


def test_frontend_refresh_is_documented():
    text = Path("docs/context-core.md").read_text(encoding="utf-8")
    for phrase in ("阅读优先", "开发能力", "项目存档", "每页 20 条", "390、768、1024、1440"):
        assert phrase in text

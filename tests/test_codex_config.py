"""Tests for the comment-preserving, compare-and-swap Codex config editor.

All tests use synthetic TOML in temporary directories. They must never read
the real ``~/.codex/config.toml``.
"""

import copy
import dataclasses
import hashlib
import json
import os
import stat

import pytest
import tomlkit

from evolvmem.codex_config import (
    REQUIRED_CONTEXT_TOOLS,
    CodexCliParseError,
    CodexConfigApplyResult,
    CodexConfigEditor,
    CodexConfigError,
    CodexConfigWriteError,
    CodexSnapshotError,
    CodexStanzaDriftError,
    CodexToolPolicyError,
    parse_mcp_get_json,
    verify_cli_matches_stanza,
)

SYNTHETIC_CONFIG = '''# Codex CLI configuration — operator comments must survive edits.
model = "gpt-5"
approval_policy = "on-request"

[profiles.work]
model = "gpt-5-codex"

# EvolvMem memory server stanza.
[mcp_servers.evolvmem]
command = "/opt/evolvmem/bin/python3"  # interpreter inside the plugin venv
args = ["-m", "evolvmem.mcp_server"]
cwd = "/opt/evolvmem"
enabled = true
startup_timeout_sec = 20
tool_timeout_sec = 120
enabled_tools = [
    "memory_add",
    "memory_search",
    "memory_status",
    "context_session_start",
    "context_search",
    "context_read",
    "context_status",
]
disabled_tools = ["memory_consolidate"]
experimental_option = { future = true }  # unknown fields must survive

[mcp_servers.evolvmem.env]
PYTHONPATH = "/opt/evolvmem"
EVOLVMEM_ADAPTER = "claude"
EVOLVMEM_CONTEXT_MODE = "compat"
FAKE_API_TOKEN = "synthetic-token-not-real"  # fake secret; never a real credential

# Another server the user installed; nothing here may change.
[mcp_servers.docs]
command = "docs-mcp"
args = ["serve", "--port", "8642"]

[mcp_servers.docs.env]
DOCS_API_KEY = "another-synthetic-secret"

[some_unknown_table]
values = [1, 2, 3]
'''

MINIMAL_CONFIG = '''[mcp_servers.evolvmem]
command = "/opt/evolvmem/bin/python3"
'''

ENABLED_TOOLS_INCOMPLETE_CONFIG = SYNTHETIC_CONFIG.replace(
    '    "context_read",\n', ""
)
DISABLED_TOOLS_BLOCKING_CONFIG = SYNTHETIC_CONFIG.replace(
    'disabled_tools = ["memory_consolidate"]',
    'disabled_tools = ["memory_consolidate", "context_search"]',
)


def write_config(tmp_path, text=SYNTHETIC_CONFIG):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def stanza_of(path, server="evolvmem"):
    doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    return doc["mcp_servers"][server]


def test_constructor_requires_an_explicit_config_path():
    """The editor must never guess user/project config precedence."""
    with pytest.raises(TypeError):
        CodexConfigEditor()


def test_required_context_tools_contract():
    assert set(REQUIRED_CONTEXT_TOOLS) == {
        "context_session_start",
        "context_search",
        "context_read",
        "context_status",
    }


def test_snapshot_returns_plain_stanza_and_hashes(tmp_path):
    path = write_config(tmp_path)
    editor = CodexConfigEditor(path)

    snapshot = editor.snapshot()

    assert isinstance(snapshot.stanza, dict)
    json.dumps(snapshot.stanza)  # structured snapshot must be serializable
    assert snapshot.stanza["command"] == "/opt/evolvmem/bin/python3"
    assert snapshot.stanza["env"]["EVOLVMEM_CONTEXT_MODE"] == "compat"
    assert snapshot.stanza["env"]["FAKE_API_TOKEN"] == "synthetic-token-not-real"
    assert len(snapshot.stanza_sha256) == 64
    assert snapshot.source_file_sha256 == hashlib.sha256(
        path.read_bytes()
    ).hexdigest()


def test_snapshot_missing_file_raises_config_error(tmp_path):
    editor = CodexConfigEditor(tmp_path / "missing.toml")
    with pytest.raises(CodexConfigError):
        editor.snapshot()


def test_snapshot_missing_stanza_raises_config_error(tmp_path):
    path = write_config(tmp_path, 'model = "gpt-5"\n')
    editor = CodexConfigEditor(path)
    with pytest.raises(CodexConfigError):
        editor.snapshot()


def test_unparseable_config_raises_config_error_without_writing(tmp_path):
    path = write_config(tmp_path, "[broken\n")
    original = path.read_bytes()
    editor = CodexConfigEditor(path)

    with pytest.raises(CodexConfigError):
        editor.snapshot()
    with pytest.raises(CodexConfigError):
        editor.apply_primary(_detached_snapshot(tmp_path))
    assert path.read_bytes() == original


def _detached_snapshot(tmp_path):
    """A valid snapshot taken from a separate synthetic config."""
    other_dir = tmp_path / "other"
    other_dir.mkdir(exist_ok=True)
    other = write_config(other_dir)
    return CodexConfigEditor(other).snapshot()


def test_apply_primary_changes_only_the_three_cutover_settings(tmp_path):
    path = write_config(tmp_path)
    before = tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
    editor = CodexConfigEditor(path)

    editor.apply_primary(editor.snapshot())

    after = tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
    expected = copy.deepcopy(before)
    stanza = expected["mcp_servers"]["evolvmem"]
    stanza["env"]["EVOLVMEM_ADAPTER"] = "codex"
    stanza["env"]["EVOLVMEM_CONTEXT_MODE"] = "primary"
    stanza["default_tools_approval_mode"] = "writes"
    assert after == expected


def test_apply_primary_preserves_comments_and_fake_secrets(tmp_path):
    path = write_config(tmp_path)
    editor = CodexConfigEditor(path)

    editor.apply_primary(editor.snapshot())

    text = path.read_text(encoding="utf-8")
    assert "# Codex CLI configuration — operator comments must survive edits." in text
    assert "# interpreter inside the plugin venv" in text
    assert "# unknown fields must survive" in text
    assert "# Another server the user installed; nothing here may change." in text
    assert "synthetic-token-not-real" in text
    assert "another-synthetic-secret" in text


def test_apply_primary_preserves_file_mode(tmp_path):
    path = write_config(tmp_path)
    path.chmod(0o640)
    editor = CodexConfigEditor(path)

    editor.apply_primary(editor.snapshot())

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_apply_primary_creates_env_table_when_missing(tmp_path):
    path = write_config(tmp_path, MINIMAL_CONFIG)
    editor = CodexConfigEditor(path)

    editor.apply_primary(editor.snapshot())

    stanza = stanza_of(path)
    assert stanza["command"] == "/opt/evolvmem/bin/python3"
    assert stanza["env"]["EVOLVMEM_ADAPTER"] == "codex"
    assert stanza["env"]["EVOLVMEM_CONTEXT_MODE"] == "primary"
    assert stanza["default_tools_approval_mode"] == "writes"


def test_apply_primary_result_exposes_hashes_only(tmp_path):
    path = write_config(tmp_path)
    editor = CodexConfigEditor(path)
    snapshot = editor.snapshot()

    result = editor.apply_primary(snapshot)

    assert {f.name for f in dataclasses.fields(CodexConfigApplyResult)} == {
        "before_stanza_sha256",
        "after_stanza_sha256",
    }
    assert result.before_stanza_sha256 == snapshot.stanza_sha256
    assert result.after_stanza_sha256 == editor.snapshot().stanza_sha256
    assert result.before_stanza_sha256 != result.after_stanza_sha256
    assert "synthetic-token-not-real" not in repr(result)
    assert str(tmp_path) not in repr(result)


def test_apply_primary_detects_target_stanza_drift_and_writes_nothing(tmp_path):
    path = write_config(tmp_path)
    editor = CodexConfigEditor(path)
    snapshot = editor.snapshot()

    doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    doc["mcp_servers"]["evolvmem"]["env"]["PYTHONPATH"] = "/elsewhere"
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    original = path.read_bytes()

    with pytest.raises(CodexStanzaDriftError):
        editor.apply_primary(snapshot)
    assert path.read_bytes() == original


def test_apply_primary_preserves_concurrent_unrelated_changes(tmp_path):
    """CAS compares only the target stanza: other edits survive the apply."""
    path = write_config(tmp_path)
    editor = CodexConfigEditor(path)
    snapshot = editor.snapshot()

    doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    doc["model"] = "gpt-5-codex-mini"
    doc["mcp_servers"]["docs"]["args"] = ["serve", "--port", "9000"]
    path.write_text(tomlkit.dumps(doc), encoding="utf-8")

    editor.apply_primary(snapshot)

    after = tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
    assert after["model"] == "gpt-5-codex-mini"
    assert after["mcp_servers"]["docs"]["args"] == ["serve", "--port", "9000"]
    stanza = after["mcp_servers"]["evolvmem"]
    assert stanza["env"]["EVOLVMEM_ADAPTER"] == "codex"
    assert stanza["env"]["EVOLVMEM_CONTEXT_MODE"] == "primary"
    assert stanza["default_tools_approval_mode"] == "writes"


def test_apply_primary_rejects_enabled_tools_missing_a_context_tool(tmp_path):
    path = write_config(tmp_path, ENABLED_TOOLS_INCOMPLETE_CONFIG)
    original = path.read_bytes()
    editor = CodexConfigEditor(path)

    with pytest.raises(CodexToolPolicyError):
        editor.apply_primary(editor.snapshot())
    assert path.read_bytes() == original


def test_apply_primary_rejects_disabled_tools_blocking_a_context_tool(tmp_path):
    path = write_config(tmp_path, DISABLED_TOOLS_BLOCKING_CONFIG)
    original = path.read_bytes()
    editor = CodexConfigEditor(path)

    with pytest.raises(CodexToolPolicyError):
        editor.apply_primary(editor.snapshot())
    assert path.read_bytes() == original


def test_apply_legacy_sets_legacy_mode_without_touching_other_settings(tmp_path):
    """Operational rollback sets the env mode to legacy; it is not a restore."""
    path = write_config(tmp_path)
    editor = CodexConfigEditor(path)
    applied = editor.apply_primary(editor.snapshot())

    rolled_back = editor.apply_legacy(
        expected_stanza_sha256=applied.after_stanza_sha256
    )

    assert rolled_back.before_stanza_sha256 == applied.after_stanza_sha256
    stanza = stanza_of(path)
    assert stanza["env"]["EVOLVMEM_CONTEXT_MODE"] == "legacy"
    assert stanza["env"]["EVOLVMEM_ADAPTER"] == "codex"
    assert stanza["default_tools_approval_mode"] == "writes"
    assert stanza["command"] == "/opt/evolvmem/bin/python3"


def test_apply_legacy_rejects_a_stale_expected_hash(tmp_path):
    path = write_config(tmp_path)
    original = path.read_bytes()
    editor = CodexConfigEditor(path)

    with pytest.raises(CodexStanzaDriftError):
        editor.apply_legacy(expected_stanza_sha256="0" * 64)
    assert path.read_bytes() == original


def test_restore_snapshot_restores_the_exact_installation_stanza(tmp_path):
    """Restore is the exact installation-stanza recovery, unlike legacy rollback."""
    path = write_config(tmp_path)
    editor = CodexConfigEditor(path)
    install_snapshot = editor.snapshot()
    applied = editor.apply_primary(install_snapshot)

    restored = editor.restore_snapshot(
        install_snapshot,
        expected_stanza_sha256=applied.after_stanza_sha256,
    )

    assert restored.before_stanza_sha256 == applied.after_stanza_sha256
    assert restored.after_stanza_sha256 == install_snapshot.stanza_sha256
    after = tomlkit.parse(path.read_text(encoding="utf-8")).unwrap()
    assert after["mcp_servers"]["evolvmem"] == install_snapshot.stanza
    stanza = after["mcp_servers"]["evolvmem"]
    assert stanza["env"]["EVOLVMEM_ADAPTER"] == "claude"
    assert stanza["env"]["EVOLVMEM_CONTEXT_MODE"] == "compat"
    assert "default_tools_approval_mode" not in stanza
    assert after["mcp_servers"]["docs"]["command"] == "docs-mcp"
    assert "# Codex CLI configuration" in path.read_text(encoding="utf-8")


def test_restore_snapshot_rejects_stanza_drift(tmp_path):
    path = write_config(tmp_path)
    editor = CodexConfigEditor(path)
    snapshot = editor.snapshot()
    original = path.read_bytes()

    with pytest.raises(CodexStanzaDriftError):
        editor.restore_snapshot(snapshot, expected_stanza_sha256="0" * 64)
    assert path.read_bytes() == original


@pytest.mark.parametrize("patch_target", ["os.fchmod", "os.fsync", "os.replace"])
def test_failed_write_leaves_original_parseable_and_removes_temp(
        tmp_path, monkeypatch, patch_target):
    path = write_config(tmp_path)
    editor = CodexConfigEditor(path)
    snapshot = editor.snapshot()
    original = path.read_bytes()

    def boom(*args, **kwargs):
        raise OSError("injected failure")

    monkeypatch.setattr(patch_target, boom)
    with pytest.raises(CodexConfigWriteError):
        editor.apply_primary(snapshot)

    assert path.read_bytes() == original
    tomlkit.parse(original.decode("utf-8"))  # original stays parseable
    assert {p.name for p in tmp_path.iterdir()} == {path.name}


def test_directory_fsync_failure_after_replace_keeps_parseable_new_config(
        tmp_path, monkeypatch):
    path = write_config(tmp_path)
    editor = CodexConfigEditor(path)
    snapshot = editor.snapshot()
    real_open = os.open

    def fake_open(target, flags, mode=0o777, **kwargs):
        if str(target) == str(tmp_path):
            raise OSError("injected directory fsync failure")
        return real_open(target, flags, mode, **kwargs)

    monkeypatch.setattr("os.open", fake_open)
    with pytest.raises(CodexConfigWriteError):
        editor.apply_primary(snapshot)

    stanza = stanza_of(path)  # the replaced file is valid TOML
    assert stanza["default_tools_approval_mode"] == "writes"
    assert {p.name for p in tmp_path.iterdir()} == {path.name}


def test_temp_files_are_unique_and_in_the_config_directory(tmp_path, monkeypatch):
    import tempfile

    calls = []
    real_mkstemp = tempfile.mkstemp

    def spy_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        calls.append((kwargs.get("dir"), name))
        return fd, name

    monkeypatch.setattr("tempfile.mkstemp", spy_mkstemp)
    path = write_config(tmp_path)
    editor = CodexConfigEditor(path)
    applied = editor.apply_primary(editor.snapshot())
    editor.apply_legacy(expected_stanza_sha256=applied.after_stanza_sha256)

    assert len(calls) == 2
    assert {call[0] for call in calls} == {str(tmp_path)}
    assert calls[0][1] != calls[1][1]


def test_snapshot_file_is_written_owner_only_and_round_trips(tmp_path):
    path = write_config(tmp_path)
    editor = CodexConfigEditor(path)
    snapshot = editor.snapshot()
    snapshot_path = tmp_path / "snapshot.json"

    editor.save_snapshot(snapshot, snapshot_path)

    assert stat.S_IMODE(snapshot_path.stat().st_mode) == 0o600
    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert set(payload) == {"stanza", "stanza_sha256", "source_file_sha256"}
    assert editor.load_snapshot(snapshot_path) == snapshot


def test_save_snapshot_makes_a_preexisting_file_owner_only(tmp_path):
    path = write_config(tmp_path)
    editor = CodexConfigEditor(path)
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text("old", encoding="utf-8")
    snapshot_path.chmod(0o644)

    editor.save_snapshot(editor.snapshot(), snapshot_path)

    assert stat.S_IMODE(snapshot_path.stat().st_mode) == 0o600
    assert json.loads(snapshot_path.read_text(encoding="utf-8"))["stanza"]


def test_load_snapshot_rejects_tampered_or_malformed_files(tmp_path):
    path = write_config(tmp_path)
    editor = CodexConfigEditor(path)
    snapshot_path = tmp_path / "snapshot.json"
    editor.save_snapshot(editor.snapshot(), snapshot_path)

    payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    payload["stanza"]["command"] = "/tampered"
    snapshot_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CodexSnapshotError):
        editor.load_snapshot(snapshot_path)

    snapshot_path.write_text("{not json", encoding="utf-8")
    with pytest.raises(CodexSnapshotError):
        editor.load_snapshot(snapshot_path)

    with pytest.raises(CodexSnapshotError):
        editor.load_snapshot(tmp_path / "missing.json")


def test_editor_operations_never_print_to_stdout_or_stderr(tmp_path, capsys):
    path = write_config(tmp_path)
    editor = CodexConfigEditor(path)
    snapshot = editor.snapshot()
    applied = editor.apply_primary(snapshot)
    editor.apply_legacy(expected_stanza_sha256=applied.after_stanza_sha256)
    snapshot_path = tmp_path / "snapshot.json"
    editor.save_snapshot(snapshot, snapshot_path)
    editor.load_snapshot(snapshot_path)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def cli_payload_from_stanza(stanza, server="evolvmem", **overrides):
    """Mocked ``codex mcp get <server> --json`` output (CLI 0.147 shape).

    The CLI never echoes ``default_tools_approval_mode``; unset lists,
    timeouts, env, and cwd come back as null.
    """
    payload = {
        "name": server,
        "enabled": stanza.get("enabled", True),
        "disabled_reason": None,
        "transport": {
            "type": "stdio",
            "command": stanza.get("command"),
            "args": list(stanza.get("args", [])),
            "env": dict(stanza["env"]) if stanza.get("env") else None,
            "env_vars": [],
            "cwd": stanza.get("cwd"),
        },
        "enabled_tools": (
            list(stanza["enabled_tools"]) if "enabled_tools" in stanza else None
        ),
        "disabled_tools": (
            list(stanza["disabled_tools"]) if "disabled_tools" in stanza else None
        ),
        "startup_timeout_sec": (
            float(stanza["startup_timeout_sec"])
            if "startup_timeout_sec" in stanza
            else None
        ),
        "tool_timeout_sec": (
            float(stanza["tool_timeout_sec"])
            if "tool_timeout_sec" in stanza
            else None
        ),
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_parse_mcp_get_json_reads_the_stdio_shape():
    stanza = tomlkit.parse(SYNTHETIC_CONFIG)["mcp_servers"]["evolvmem"].unwrap()

    result = parse_mcp_get_json(cli_payload_from_stanza(stanza))

    assert result.name == "evolvmem"
    assert result.enabled is True
    assert result.transport.type == "stdio"
    assert result.transport.command == "/opt/evolvmem/bin/python3"
    assert result.transport.args == ("-m", "evolvmem.mcp_server")
    assert result.transport.env["EVOLVMEM_CONTEXT_MODE"] == "compat"
    assert result.transport.env["FAKE_API_TOKEN"] == "synthetic-token-not-real"
    assert result.transport.cwd == "/opt/evolvmem"
    assert "context_session_start" in result.enabled_tools
    assert result.disabled_tools == ("memory_consolidate",)
    assert result.startup_timeout_sec == 20.0
    assert result.tool_timeout_sec == 120.0


def test_parse_mcp_get_json_rejects_malformed_payloads():
    for bad in (
        "{not json",
        '["not", "an", "object"]',
        json.dumps({"transport": "stdio"}),
        json.dumps({"transport": {"type": "stdio", "args": [1, 2]}}),
        json.dumps({"transport": {"type": "stdio", "env": {"A": 1}}}),
        json.dumps({"enabled": "yes"}),
        json.dumps({"startup_timeout_sec": "20"}),
    ):
        with pytest.raises(CodexCliParseError):
            parse_mcp_get_json(bad)


def test_verify_cli_matches_stanza_after_apply_primary(tmp_path):
    """Two-source verification: CLI echo vs TOML, approval mode TOML-only."""
    path = write_config(tmp_path)
    editor = CodexConfigEditor(path)
    editor.apply_primary(editor.snapshot())
    stanza = editor.snapshot().stanza

    verification = verify_cli_matches_stanza(
        parse_mcp_get_json(cli_payload_from_stanza(stanza)), stanza
    )

    assert verification.ok
    assert verification.mismatches == ()
    assert {
        "name",
        "enabled",
        "transport.type",
        "transport.command",
        "transport.args",
        "transport.env",
        "transport.cwd",
        "enabled_tools",
        "disabled_tools",
        "startup_timeout_sec",
        "tool_timeout_sec",
    } <= set(verification.verified_fields)
    # CLI 0.147 never echoes the approval mode; it must never be claimed
    # as CLI-verified. It is verified by reparsing the TOML instead.
    assert "default_tools_approval_mode" not in verification.verified_fields
    assert "default_tools_approval_mode" not in verification.skipped_fields
    reparsed = tomlkit.parse(path.read_text(encoding="utf-8"))
    assert (
        reparsed["mcp_servers"]["evolvmem"]["default_tools_approval_mode"]
        == "writes"
    )


def test_verify_cli_flags_env_drift_without_leaking_values(tmp_path):
    path = write_config(tmp_path)
    editor = CodexConfigEditor(path)
    editor.apply_primary(editor.snapshot())
    stanza = editor.snapshot().stanza

    drifted_env = dict(stanza["env"])
    drifted_env["EVOLVMEM_CONTEXT_MODE"] = "compat"
    transport = {
        "type": "stdio",
        "command": stanza["command"],
        "args": list(stanza["args"]),
        "env": drifted_env,
        "env_vars": [],
        "cwd": stanza["cwd"],
    }
    payload = cli_payload_from_stanza(stanza, transport=transport)

    verification = verify_cli_matches_stanza(parse_mcp_get_json(payload), stanza)

    assert not verification.ok
    assert "transport.env" in verification.mismatches
    rendered = repr(verification)
    assert "synthetic-token-not-real" not in rendered
    assert "compat" not in rendered


def test_verify_cli_skips_fields_the_cli_did_not_emit(tmp_path):
    path = write_config(tmp_path, MINIMAL_CONFIG)
    editor = CodexConfigEditor(path)
    editor.apply_primary(editor.snapshot())
    stanza = editor.snapshot().stanza

    verification = verify_cli_matches_stanza(
        parse_mcp_get_json(cli_payload_from_stanza(stanza)), stanza
    )

    assert verification.ok
    assert {
        "transport.cwd",
        "enabled_tools",
        "disabled_tools",
        "startup_timeout_sec",
        "tool_timeout_sec",
    } <= set(verification.skipped_fields)
    assert "transport.command" in verification.verified_fields
    assert "transport.env" in verification.verified_fields


def test_verify_cli_flags_stanza_fields_the_cli_loaded_as_unset(tmp_path):
    """A stanza field echoed as null means the CLI did not pick it up."""
    path = write_config(tmp_path)
    editor = CodexConfigEditor(path)
    editor.apply_primary(editor.snapshot())
    stanza = editor.snapshot().stanza

    payload = cli_payload_from_stanza(
        stanza, enabled_tools=None, startup_timeout_sec=None
    )
    verification = verify_cli_matches_stanza(parse_mcp_get_json(payload), stanza)

    assert not verification.ok
    assert "enabled_tools" in verification.mismatches
    assert "startup_timeout_sec" in verification.mismatches


def test_verify_cli_rejects_a_non_stdio_transport(tmp_path):
    path = write_config(tmp_path)
    editor = CodexConfigEditor(path)
    editor.apply_primary(editor.snapshot())
    stanza = editor.snapshot().stanza

    transport = {
        "type": "streamable-http",
        "url": "https://example.invalid/mcp",
    }
    payload = cli_payload_from_stanza(stanza, transport=transport)
    verification = verify_cli_matches_stanza(parse_mcp_get_json(payload), stanza)

    assert not verification.ok
    assert "transport.type" in verification.mismatches

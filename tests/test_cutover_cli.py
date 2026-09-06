"""Behavioral contracts for the cutover operator CLI.

Four subcommands — ``preflight``, ``backup``, ``cutover``, ``rollback`` —
each take explicit absolute paths only; omission is a usage error and the
program never guesses a default path. ``cutover`` without ``--apply`` is a
pure dry-run that writes only its own ``--output`` report. ``backup`` is
optional verification tooling; ``cutover`` always makes a fresh verified
backup under the lock. ``--allow-fts-only`` counts only beside an explicit
``--apply`` and is recorded as degraded, never vector-healthy. All fixtures
are synthetic and live in temporary directories.
"""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import stat

import pytest

from evolvmem import cutover_cli
from evolvmem.codex_config import CodexConfigEditor
from evolvmem.config import Config
from evolvmem.context_store import ContextStore
from evolvmem.cutover import (
    JOURNAL_FILENAME,
    CutoverJournal,
    load_preflight_envelope,
)
from evolvmem.memory_store import MemoryStore

from tests.test_cutover import (
    CODEX_CONFIG_TEXT,
    _cli_probe_for,
    _seed_library,
    _tree_snapshot,
    _write_codex_config,
)


def _preflight_args(tmp_path: Path, data_dir: Path, output: Path) -> list:
    return [
        "preflight",
        "--data-dir",
        str(data_dir),
        "--codex-config",
        str(tmp_path / "config.toml"),
        "--output",
        str(output),
        "--json",
    ]


def _run_preflight_cli(tmp_path: Path, data_dir: Path, capsys=None) -> Path:
    _write_codex_config(tmp_path)
    output = tmp_path / "preflight.json"
    assert cutover_cli.main(_preflight_args(tmp_path, data_dir, output)) == 0
    if capsys is not None:
        capsys.readouterr()  # drain the preflight report from captured stdout
    return output


def _cutover_args(envelope: Path, output: Path, *extra: str) -> list:
    return [
        "cutover",
        "--preflight-report",
        str(envelope),
        "--writers-restarted",
        "--output",
        str(output),
        *extra,
    ]


@pytest.fixture(autouse=True)
def _no_embedding_engine(monkeypatch):
    """The CLI never loads a real model inside the test suite."""
    monkeypatch.setattr(cutover_cli, "_load_embedding_engine", lambda config: None)


@pytest.fixture(autouse=True)
def _fake_cli_probe(monkeypatch):
    """Dual-source verification reads the real temp TOML through a fake CLI."""

    def factory(codex_config_path, codex_bin):
        return _cli_probe_for(Path(codex_config_path))

    monkeypatch.setattr(cutover_cli, "_build_cli_probe", factory)


# ---- explicit absolute paths: omission is an error, never a guess ----


def test_preflight_requires_every_path_argument(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        cutover_cli.main(["preflight", "--json"])
    assert excinfo.value.code == 2
    with pytest.raises(SystemExit):
        cutover_cli.main(
            [
                "preflight",
                "--data-dir",
                str(tmp_path),
                # --codex-config and --output omitted
                "--json",
            ]
        )


def test_relative_paths_are_rejected(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        cutover_cli.main(
            [
                "preflight",
                "--data-dir",
                "relative/data/dir",
                "--codex-config",
                str(tmp_path / "config.toml"),
                "--output",
                str(tmp_path / "out.json"),
                "--json",
            ]
        )
    assert excinfo.value.code == 2


def test_backup_cutover_and_rollback_require_their_arguments(tmp_path):
    with pytest.raises(SystemExit):
        cutover_cli.main(["backup", "--apply", "--json"])
    with pytest.raises(SystemExit):
        cutover_cli.main(["cutover", "--apply", "--json"])
    with pytest.raises(SystemExit):
        cutover_cli.main(
            [
                "cutover",
                "--preflight-report",
                str(tmp_path / "preflight.json"),
                "--apply",
                "--json",
                # --output omitted
            ]
        )
    with pytest.raises(SystemExit):
        cutover_cli.main(["rollback", "--apply", "--json"])


# ---- preflight ----


def test_preflight_writes_an_owner_only_envelope_without_leaking_paths(
    test_config, tmp_path, capsys
):
    _seed_library(test_config)
    _write_codex_config(tmp_path)
    output = tmp_path / "preflight.json"
    before = _tree_snapshot(tmp_path)
    assert (
        cutover_cli.main(_preflight_args(tmp_path, test_config.data_dir, output)) == 0
    )
    after = _tree_snapshot(tmp_path)
    delta = set(after) - set(before)
    assert delta == {str(output.relative_to(tmp_path))}  # only the report file

    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    envelope = load_preflight_envelope(output)
    assert envelope.data_dir == test_config.data_dir
    assert envelope.codex_config_path == tmp_path / "config.toml"

    printed = json.loads(capsys.readouterr().out)
    assert printed["report"]["ready"] is True
    assert printed["digest"] == envelope.digest
    # The public stdout projection never carries absolute paths.
    assert str(tmp_path) not in json.dumps(printed)


# ---- backup: optional verification tooling ----


def test_backup_dry_run_creates_nothing(test_config, tmp_path, capsys):
    _seed_library(test_config)
    envelope = _run_preflight_cli(tmp_path, test_config.data_dir, capsys)
    before = _tree_snapshot(tmp_path)
    assert (
        cutover_cli.main(
            ["backup", "--preflight-report", str(envelope), "--json"]
        )
        == 0
    )
    assert _tree_snapshot(tmp_path) == before
    printed = json.loads(capsys.readouterr().out)
    assert printed["applied"] is False


def test_backup_apply_creates_an_independently_verified_backup(
    test_config, tmp_path, capsys
):
    _seed_library(test_config)
    envelope = _run_preflight_cli(tmp_path, test_config.data_dir, capsys)
    assert (
        cutover_cli.main(
            ["backup", "--preflight-report", str(envelope), "--apply", "--json"]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["applied"] is True
    assert printed["manifest"]["complete"] is True
    assert printed["verification"]["verified"] is True
    backups = list((test_config.data_dir / "backups").iterdir())
    assert len(backups) == 1
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o700
    assert str(tmp_path) not in json.dumps(printed)


# ---- cutover: flags, dry-run, and the real apply path ----


def test_cutover_requires_writers_restarted_for_apply(test_config, tmp_path, capsys):
    _seed_library(test_config)
    envelope = _run_preflight_cli(tmp_path, test_config.data_dir, capsys)
    with pytest.raises(SystemExit) as excinfo:
        cutover_cli.main(
            [
                "cutover",
                "--preflight-report",
                str(envelope),
                "--output",
                str(tmp_path / "result.json"),
                "--apply",
                "--json",
            ]
        )
    assert excinfo.value.code == 2
    assert not (test_config.data_dir / "backups").exists()


def test_allow_fts_only_requires_the_second_explicit_apply_flag(
    test_config, tmp_path, capsys
):
    _seed_library(test_config)
    envelope = _run_preflight_cli(tmp_path, test_config.data_dir, capsys)
    with pytest.raises(SystemExit) as excinfo:
        cutover_cli.main(
            [
                "cutover",
                "--preflight-report",
                str(envelope),
                "--writers-restarted",
                "--output",
                str(tmp_path / "result.json"),
                "--allow-fts-only",
                "--json",
                # --apply omitted: the approval alone is meaningless
            ]
        )
    assert excinfo.value.code == 2


def test_cutover_dry_run_writes_only_its_output(test_config, tmp_path, capsys):
    _seed_library(test_config)
    envelope = _run_preflight_cli(tmp_path, test_config.data_dir, capsys)
    codex_path = tmp_path / "config.toml"
    codex_before = codex_path.read_bytes()
    output = tmp_path / "result.json"
    before = _tree_snapshot(tmp_path)

    assert (
        cutover_cli.main(
            _cutover_args(envelope, output, "--json")
        )
        == 0
    )
    after = _tree_snapshot(tmp_path)
    delta = set(after) - set(before)
    assert delta == {str(output.relative_to(tmp_path))}
    assert codex_path.read_bytes() == codex_before
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["state"] == "planned"
    assert result["applied"] is False
    printed = json.loads(capsys.readouterr().out)
    assert printed["state"] == "planned"


def test_cutover_apply_runs_the_full_real_path(test_config, tmp_path, capsys):
    _seed_library(test_config)
    envelope = _run_preflight_cli(tmp_path, test_config.data_dir, capsys)
    output = tmp_path / "result.json"

    exit_code = cutover_cli.main(
        _cutover_args(envelope, output, "--apply", "--allow-fts-only", "--json")
    )
    assert exit_code == 0

    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["state"] == "awaiting_post_cutover_canary"
    assert result["fts_only"] is True  # degraded on record, never vector-healthy
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert str(tmp_path) not in json.dumps(result)

    persisted = json.loads(test_config.config_path.read_text(encoding="utf-8"))
    assert persisted["context_mode"] == "compat"
    stanza = CodexConfigEditor(tmp_path / "config.toml").snapshot().stanza
    assert stanza["env"]["EVOLVMEM_CONTEXT_MODE"] == "primary"

    # A fresh verified backup was made under the lock even though no
    # standalone backup was requested beforehand.
    journals = list(test_config.data_dir.rglob(JOURNAL_FILENAME))
    assert len(journals) == 1
    journal = CutoverJournal.load(journals[0])
    assert journal.state == "awaiting_post_cutover_canary"

    printed = json.loads(capsys.readouterr().out)
    assert printed["state"] == "awaiting_post_cutover_canary"


def test_cutover_apply_fails_closed_when_the_target_config_disappears(
    test_config, tmp_path, capsys
):
    _seed_library(test_config)
    envelope = _run_preflight_cli(tmp_path, test_config.data_dir, capsys)
    (tmp_path / "config.toml").unlink()
    output = tmp_path / "result.json"
    exit_code = cutover_cli.main(
        _cutover_args(envelope, output, "--apply", "--allow-fts-only", "--json")
    )
    assert exit_code == 1
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["state"] == "failed"
    assert result["failed_step"] == "validate_inputs"
    assert not test_config.config_path.exists()


# ---- rollback: journal-driven operational legacy CAS ----


def _applied_cutover(test_config, tmp_path, capsys) -> Path:
    _seed_library(test_config)
    envelope = _run_preflight_cli(tmp_path, test_config.data_dir, capsys)
    output = tmp_path / "result.json"
    assert (
        cutover_cli.main(
            _cutover_args(envelope, output, "--apply", "--allow-fts-only", "--json")
        )
        == 0
    )
    capsys.readouterr()  # drain the cutover report from captured stdout
    journals = list(test_config.data_dir.rglob(JOURNAL_FILENAME))
    assert len(journals) == 1
    return journals[0]


def test_rollback_dry_run_changes_nothing(test_config, tmp_path, capsys):
    journal_path = _applied_cutover(test_config, tmp_path, capsys)
    codex_path = tmp_path / "config.toml"
    codex_before = codex_path.read_bytes()
    journal_before = journal_path.read_bytes()

    assert (
        cutover_cli.main(["rollback", "--journal", str(journal_path), "--json"]) == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["applied"] is False
    assert printed["state"] == "awaiting_post_cutover_canary"
    assert printed["would_roll_back"] is True
    assert codex_path.read_bytes() == codex_before
    assert journal_path.read_bytes() == journal_before


def test_rollback_apply_returns_codex_to_explicit_legacy(test_config, tmp_path, capsys):
    journal_path = _applied_cutover(test_config, tmp_path, capsys)
    codex_path = tmp_path / "config.toml"

    assert (
        cutover_cli.main(
            ["rollback", "--journal", str(journal_path), "--apply", "--json"]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["applied"] is True
    assert printed["state"] == "rolled_back"
    assert printed["action"] == "cas_legacy"

    stanza = CodexConfigEditor(codex_path).snapshot().stanza
    assert stanza["env"]["EVOLVMEM_CONTEXT_MODE"] == "legacy"
    journal = CutoverJournal.load(journal_path)
    assert journal.state == "rolled_back"
    assert journal.hashes["codex_rollback_stanza_sha256"] != ""

    # The persistent compat mode and the migrated Context data are retained;
    # rollback never restores or deletes the database.
    persisted = json.loads(test_config.config_path.read_text(encoding="utf-8"))
    assert persisted["context_mode"] == "compat"
    store = ContextStore(test_config)
    store.initialize(create_schema=False)
    try:
        assert store.count_by_status().get("active", 0) == 2
    finally:
        store.close()


def _compat_persisted_journal(tmp_path: Path, codex_path: Path) -> Path:
    """A durable journal parked at the persist/primary crash window."""
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    journal = CutoverJournal.begin(
        preflight_digest=hashlib.sha256(b"synthetic preflight").hexdigest(),
        private={
            "data_dir": str(tmp_path / "data"),
            "codex_config_path": str(codex_path),
        },
    )
    journal.bind(backup_dir)
    for state in (
        "locked",
        "backed_up",
        "migrated",
        "vector_ready_or_approved_fts",
        "shadow_passed",
        "compat_persisted",
    ):
        journal.advance(state)
    return backup_dir / JOURNAL_FILENAME


def test_rollback_apply_from_compat_persisted_returns_codex_to_legacy(
    tmp_path, capsys
):
    # The crash window after step 8: the journal sits at live
    # ``compat_persisted`` while Codex may already run as primary.
    codex_path = _write_codex_config(tmp_path)
    editor = CodexConfigEditor(codex_path)
    editor.apply_primary(editor.snapshot())
    journal_path = _compat_persisted_journal(tmp_path, codex_path)

    assert (
        cutover_cli.main(
            ["rollback", "--journal", str(journal_path), "--apply", "--json"]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is True
    assert printed["applied"] is True
    assert printed["state"] == "rolled_back"
    assert printed["action"] == "cas_legacy"

    stanza = CodexConfigEditor(codex_path).snapshot().stanza
    assert stanza["env"]["EVOLVMEM_CONTEXT_MODE"] == "legacy"
    journal = CutoverJournal.load(journal_path)
    assert journal.state == "rolled_back"
    assert journal.rolled_back_from == "compat_persisted"


def test_rollback_apply_from_compat_persisted_with_compat_stanza_is_a_noop(
    tmp_path, capsys
):
    codex_path = _write_codex_config(
        tmp_path,
        CODEX_CONFIG_TEXT.replace(
            'EVOLVMEM_CONTEXT_MODE = "legacy"', 'EVOLVMEM_CONTEXT_MODE = "compat"'
        ),
    )
    journal_path = _compat_persisted_journal(tmp_path, codex_path)
    codex_before = codex_path.read_bytes()

    assert (
        cutover_cli.main(
            ["rollback", "--journal", str(journal_path), "--apply", "--json"]
        )
        == 0
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["ok"] is True
    assert printed["applied"] is True
    assert printed["state"] == "rolled_back"
    assert printed["action"] == "not_needed"
    assert printed["reason_codes"] == ["codex_primary_not_applied"]

    assert codex_path.read_bytes() == codex_before
    journal = CutoverJournal.load(journal_path)
    assert journal.state == "rolled_back"
    assert journal.rolled_back_from == "compat_persisted"
    assert journal.hashes["codex_rollback_stanza_sha256"] == ""


def test_rollback_twice_is_a_terminal_error(test_config, tmp_path, capsys):
    journal_path = _applied_cutover(test_config, tmp_path, capsys)
    assert (
        cutover_cli.main(
            ["rollback", "--journal", str(journal_path), "--apply", "--json"]
        )
        == 0
    )
    capsys.readouterr()
    codex_before = (tmp_path / "config.toml").read_bytes()
    assert (
        cutover_cli.main(
            ["rollback", "--journal", str(journal_path), "--apply", "--json"]
        )
        == 1
    )
    printed = json.loads(capsys.readouterr().out)
    assert printed["reason_codes"] == ["journal_already_rolled_back"]
    assert (tmp_path / "config.toml").read_bytes() == codex_before


def test_rollback_requires_an_existing_journal_file(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        cutover_cli.main(
            ["rollback", "--journal", str(tmp_path / "missing.json"), "--apply"]
        )
    assert excinfo.value.code == 2

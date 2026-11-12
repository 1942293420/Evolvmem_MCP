"""Stage 0 Task 0.5 TDD tests for continuity_backfill quiet output, 0600 mode, error rotation, and shell fd rebinding."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
import uuid as uuidlib
from pathlib import Path

import pytest

from evolvmem import continuity_backfill
from evolvmem.config import Config
from evolvmem.context_store import ContextStore
from evolvmem.project_store import ProjectStore
from tests.test_continuity_mcp import _bootstrap_key, git_workspace  # noqa: F401


def _write_codex_rollout(
    sessions_root: Path, session_id: str, events: list[dict], *, mtime_offset_sec: float = -7200.0
) -> Path:
    day_dir = sessions_root / "2026" / "11" / "12"
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-2026-11-12T10-00-00-{session_id}.jsonl"
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
    ts = time.time() + mtime_offset_sec
    os.utime(path, (ts, ts))
    return path


def _actionable_events(session_id: str, workspace: Path) -> list[dict]:
    return [
        {"type": "session_meta", "payload": {"id": session_id, "cwd": str(workspace)}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "排查连续补录日志轮转与重定向问题"}],
            },
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "shell", "call_id": "call_1", "arguments": '{"command":["pytest"]}'},
        },
    ]


def _setup_bound_workspace(data_dir: Path, workspace: Path, project_name: str = "evolvmem") -> None:
    cfg = Config(data_dir=data_dir)
    provider = _bootstrap_key(cfg)
    store = ContextStore(cfg)
    try:
        store.initialize()
        fp = provider.resolve(str(workspace)).fingerprint
        with store.transaction():
            ps = ProjectStore(store._connection(), store._require_transaction, generic_names=())
            ps.register_project(project_name)
            ps.bind_workspace(fp, project_name, method="test", make_default=True)
    finally:
        store.close()


def test_backfill_cli_only_outputs_actionable_sessions_by_default(
    tmp_path: Path, git_workspace: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data_dir = tmp_path / "data"
    sessions_root = tmp_path / "sessions"
    _setup_bound_workspace(data_dir, git_workspace)

    actionable_sid = str(uuidlib.uuid4())
    active_sid = str(uuidlib.uuid4())
    empty_sid = str(uuidlib.uuid4())

    _write_codex_rollout(sessions_root, actionable_sid, _actionable_events(actionable_sid, git_workspace), mtime_offset_sec=-7200.0)
    _write_codex_rollout(sessions_root, active_sid, _actionable_events(active_sid, git_workspace), mtime_offset_sec=0.0)
    _write_codex_rollout(
        sessions_root,
        empty_sid,
        [{"type": "session_meta", "payload": {"id": empty_sid, "cwd": str(git_workspace)}}],
        mtime_offset_sec=-7200.0,
    )

    rc1 = continuity_backfill.main([
        "--data-dir", str(data_dir),
        "--sessions-root", str(sessions_root),
        "--auto",
        "--apply",
    ])
    assert rc1 == 0
    out1_lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines() if line.strip()]
    assert len(out1_lines) == 1
    assert out1_lines[0]["session"] == actionable_sid
    assert out1_lines[0]["action"] == "create"

    # Second run: already applied + active + empty -> 0 lines emitted
    rc2 = continuity_backfill.main([
        "--data-dir", str(data_dir),
        "--sessions-root", str(sessions_root),
        "--auto",
        "--apply",
    ])
    assert rc2 == 0
    assert capsys.readouterr().out == ""

    # --verbose preserves full diagnostic output on demand
    rc3 = continuity_backfill.main([
        "--data-dir", str(data_dir),
        "--sessions-root", str(sessions_root),
        "--auto",
        "--apply",
        "--verbose",
    ])
    assert rc3 == 0
    verbose_lines = [json.loads(line) for line in capsys.readouterr().out.strip().splitlines() if line.strip()]
    assert len(verbose_lines) == 3


def test_backfill_rotates_oversized_log_bounded_and_preserves_0600_mode(
    tmp_path: Path, git_workspace: Path
) -> None:
    data_dir = tmp_path / "data"
    sessions_root = tmp_path / "sessions"
    _setup_bound_workspace(data_dir, git_workspace)

    # 1. Explicit --log-file initial creation must enforce 0600 regardless of umask
    explicit_log = tmp_path / "explicit" / "backfill.log"
    sid = str(uuidlib.uuid4())
    _write_codex_rollout(sessions_root, sid, _actionable_events(sid, git_workspace), mtime_offset_sec=-7200.0)
    old_umask = os.umask(0o022)
    try:
        rc = continuity_backfill.main([
            "--data-dir", str(data_dir),
            "--sessions-root", str(sessions_root),
            "--auto",
            "--apply",
            "--log-file", str(explicit_log),
        ])
    finally:
        os.umask(old_umask)
    assert rc == 0
    assert explicit_log.exists()
    assert stat.S_IMODE(explicit_log.stat().st_mode) == 0o600
    assert json.loads(explicit_log.read_text(encoding="utf-8").strip())["session"] == sid

    # 2. Default data_dir/continuity_backfill.log rotates bounded (.1 .. .3, never .4)
    log_path = data_dir / "continuity_backfill.log"
    for cycle in range(5):
        log_path.write_text(f"cycle-{cycle}-" + ("X" * 400) + "\n", encoding="utf-8")
        rc_rot = continuity_backfill.main([
            "--data-dir", str(data_dir),
            "--sessions-root", str(sessions_root),
            "--auto",
            "--apply",
            "--log-max-bytes", "256",
            "--log-backups", "3",
        ])
        assert rc_rot == 0

    assert log_path.exists()
    assert log_path.stat().st_size == 0
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert "cycle-4-" in Path(f"{log_path}.1").read_text(encoding="utf-8")
    assert "cycle-3-" in Path(f"{log_path}.2").read_text(encoding="utf-8")
    assert "cycle-2-" in Path(f"{log_path}.3").read_text(encoding="utf-8")
    assert not Path(f"{log_path}.4").exists()


def test_backfill_shell_redirect_rotates_and_rebinds_stdout_stderr_fds(
    tmp_path: Path, git_workspace: Path
) -> None:
    """Simulate crontab `>> continuity_backfill.log 2>&1`: rotated output must land in NEW log, not .1."""
    data_dir = tmp_path / "data"
    sessions_root = tmp_path / "sessions"
    _setup_bound_workspace(data_dir, git_workspace)

    sid = str(uuidlib.uuid4())
    _write_codex_rollout(sessions_root, sid, _actionable_events(sid, git_workspace), mtime_offset_sec=-7200.0)

    log_path = data_dir / "continuity_backfill.log"
    old_marker = "OLD_OVERSIZED_LOG_MARKER_" + ("Z" * 350) + "\n"
    log_path.write_text(old_marker, encoding="utf-8")

    env = dict(os.environ)
    env["HOME"] = str(tmp_path)
    with log_path.open("ab") as redirected:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "evolvmem.continuity_backfill",
                "--data-dir", str(data_dir),
                "--sessions-root", str(sessions_root),
                "--auto",
                "--apply",
                "--log-max-bytes", "256",
            ],
            stdout=redirected,
            stderr=subprocess.STDOUT,
            env=env,
            check=False,
        )
    assert proc.returncode == 0
    rotated_1 = Path(f"{log_path}.1").read_text(encoding="utf-8")
    current_log = log_path.read_text(encoding="utf-8")
    assert rotated_1 == old_marker
    assert sid not in rotated_1
    parsed = [json.loads(line) for line in current_log.splitlines() if line.strip()]
    assert len(parsed) == 1
    assert parsed[0]["session"] == sid
    assert parsed[0]["action"] == "create"
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


def test_backfill_error_exit_still_rotates_oversized_log(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Early CLI/Continuity errors must not bypass rotation of an oversized log file."""
    data_dir = tmp_path / "err_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "continuity_backfill.log"
    old_content = "ERROR_FLOOD_" + ("E" * 400) + "\n"
    log_path.write_text(old_content, encoding="utf-8")

    # --apply without --auto or --project/--workspace-path returns 2 early
    rc = continuity_backfill.main([
        "--data-dir", str(data_dir),
        "--apply",
        "--log-max-bytes", "256",
    ])
    assert rc == 2
    err_payload = json.loads(capsys.readouterr().err.strip())
    assert err_payload["error"] == "apply_requires_auto_or_project_and_workspace"
    assert Path(f"{log_path}.1").read_text(encoding="utf-8") == old_content
    assert log_path.exists()
    assert log_path.stat().st_size == 0

"""Operator CLI for the reversible Context Core cutover.

Every subcommand takes explicit absolute paths only — an omitted or
relative path is a usage error and the program never guesses a default:

    python -m evolvmem.cutover_cli preflight \
      --data-dir /absolute/data/dir \
      --codex-config /absolute/config.toml \
      --output /absolute/preflight.json --json

    python -m evolvmem.cutover_cli backup \
      --preflight-report /absolute/preflight.json --apply --json

    python -m evolvmem.cutover_cli cutover \
      --preflight-report /absolute/preflight.json \
      --writers-restarted \
      --output /absolute/cutover-result.json --apply --json

    python -m evolvmem.cutover_cli rollback \
      --journal /absolute/cutover-journal.json --apply --json

``backup`` is optional verification tooling; ``cutover`` always makes a
fresh verified backup under the lock even when a standalone backup exists.
``--allow-fts-only`` counts only beside an explicit ``--apply`` and is
recorded as degraded, never vector-healthy. The process environment's
``EVOLVMEM_*`` overrides are stripped for the duration of the command so
the explicit arguments always win; they are restored before returning.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

from evolvmem.codex_config import CodexConfigEditor, CodexConfigError
from evolvmem.config import Config
from evolvmem.context_models import ContextValidationError
from evolvmem.cutover import (
    CutoverError,
    CutoverJournal,
    CutoverJournalError,
    CutoverRequest,
    _write_private_file,
    load_preflight_envelope,
    rollback_codex_to_legacy,
    run_cutover,
    write_preflight_envelope,
)
from evolvmem.cutover_backup import (
    CutoverBackupError,
    create_cutover_backup,
    verify_cutover_backup,
)
from evolvmem.cutover_checks import run_preflight
from evolvmem.cutover_models import validate_public_summary

_ENV_OVERRIDE_KEYS = ("EVOLVMEM_DATA_DIR", "EVOLVMEM_CONTEXT_MODE", "EVOLVMEM_ADAPTER")

# Rollback can start only from a post-persist journal state.
_ROLLBACKABLE_STATES = (
    "compat_persisted",
    "codex_primary",
    "awaiting_post_cutover_canary",
    "complete",
)
_ROLLBACKABLE_FAILED_STEPS = (
    "persist_compat",
    "codex_primary",
    "release_and_await_canary",
)


# ---- argument validation: explicit absolute paths, never guessed ----


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError("path must be absolute")
    return path


def _existing_file(value: str) -> Path:
    path = _absolute_path(value)
    if not path.is_file():
        raise argparse.ArgumentTypeError("file does not exist")
    return path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="evolvmem.cutover_cli",
        description="Reversible Context Core cutover tooling",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    preflight = sub.add_parser("preflight", help="side-effect-free readiness report")
    preflight.add_argument("--data-dir", required=True, type=_absolute_path)
    preflight.add_argument("--codex-config", required=True, type=_absolute_path)
    preflight.add_argument("--output", required=True, type=_absolute_path)
    preflight.add_argument("--json", action="store_true")

    backup = sub.add_parser("backup", help="optional standalone verified backup")
    backup.add_argument("--preflight-report", required=True, type=_existing_file)
    backup.add_argument("--apply", action="store_true")
    backup.add_argument("--json", action="store_true")

    cutover = sub.add_parser("cutover", help="the journaled ten-step cutover")
    cutover.add_argument("--preflight-report", required=True, type=_existing_file)
    cutover.add_argument("--writers-restarted", action="store_true")
    cutover.add_argument("--output", required=True, type=_absolute_path)
    cutover.add_argument("--apply", action="store_true")
    cutover.add_argument("--allow-fts-only", action="store_true")
    cutover.add_argument("--codex-bin", default="codex")
    cutover.add_argument("--json", action="store_true")

    rollback = sub.add_parser("rollback", help="operational Codex legacy rollback")
    rollback.add_argument("--journal", required=True, type=_existing_file)
    rollback.add_argument("--apply", action="store_true")
    rollback.add_argument("--json", action="store_true")
    return parser


class _scrubbed_environment:
    """Strip EVOLVMEM_* overrides so explicit CLI arguments always win."""

    def __enter__(self):
        self._saved = {}
        for key in _ENV_OVERRIDE_KEYS:
            if key in os.environ:
                self._saved[key] = os.environ.pop(key)
        return self

    def __exit__(self, *exc):
        os.environ.update(self._saved)
        return False


def _load_embedding_engine(config: Config):
    """Best-effort engine for vector staging; None means the FTS-only path."""
    try:
        from evolvmem.embedding import EmbeddingEngine
    except Exception:
        return None
    try:
        engine = EmbeddingEngine(config)
        engine.initialize()
    except Exception:
        return None
    return engine if getattr(engine, "is_loaded", False) else None


def _build_cli_probe(codex_config_path, codex_bin: str):
    from evolvmem.cutover import _default_cli_probe

    return lambda: _default_cli_probe(codex_config_path, codex_bin=codex_bin)


def _emit(payload: dict, use_json: bool, fallback: str) -> None:
    if use_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(fallback)


def _write_output(path: Path, payload: dict) -> None:
    _write_private_file(
        path,
        (json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n").encode(
            "utf-8"
        ),
    )


# ---- subcommands ----


def _cmd_preflight(args) -> int:
    config = Config(data_dir=args.data_dir)
    report = run_preflight(config, codex_config_path=args.codex_config)
    digest = write_preflight_envelope(
        args.output,
        report,
        data_dir=config.data_dir,
        codex_config_path=args.codex_config,
    )
    payload = {"digest": digest, "report": report.public_dict()}
    _emit(payload, args.json, f"ready={report.ready} digest={digest[:16]}")
    return 0 if report.ready else 1


def _cmd_backup(args) -> int:
    envelope = load_preflight_envelope(args.preflight_report)
    if not args.apply:
        payload = {"applied": False, "preflight_digest": envelope.digest}
        _emit(payload, args.json, "dry-run: no backup created")
        return 0
    config = Config(data_dir=envelope.data_dir)
    snapshot = CodexConfigEditor(envelope.codex_config_path).snapshot()
    manifest = create_cutover_backup(
        config,
        codex_snapshot=snapshot,
        preflight_digest=envelope.digest,
        timestamp=datetime.now(timezone.utc),
    )
    verification = verify_cutover_backup(
        config.data_dir / "backups" / manifest.directory_name
    )
    payload = {
        "applied": True,
        "manifest": manifest.public_dict(),
        "verification": verification.public_dict(),
    }
    _emit(payload, args.json, f"verified={verification.verified}")
    return 0 if verification.verified else 1


def _cmd_cutover(args) -> int:
    envelope = load_preflight_envelope(args.preflight_report)
    config = Config(data_dir=envelope.data_dir)
    request = CutoverRequest(
        config=config,
        codex_config_path=envelope.codex_config_path,
        preflight_report_path=args.preflight_report,
        writers_restarted=args.writers_restarted,
        apply=args.apply,
        allow_fts_only=args.allow_fts_only,
    )
    engine = None
    probe = None
    if args.apply:
        engine = _load_embedding_engine(config)
        probe = _build_cli_probe(envelope.codex_config_path, args.codex_bin)
    try:
        outcome = run_cutover(request, cli_probe=probe, embedding_engine=engine)
    finally:
        close = getattr(engine, "close", None)
        if callable(close):
            close()
    payload = outcome.public_dict()
    _write_output(args.output, payload)
    _emit(payload, args.json, f"state={outcome.state} failed_step={outcome.failed_step}")
    return 0 if outcome.ready else 1


def rollback_journal_file(journal_path, *, apply: bool, editor_factory=None) -> dict:
    """Journal-driven operational rollback: CAS Codex back to explicit legacy.

    The persistent compat mode and the migrated Context data are retained;
    the database is never restored or deleted. Dry-run (``apply=False``)
    performs no write.
    """
    journal = CutoverJournal.load(journal_path)
    state = journal.state
    base = {
        "schema": "evolvmem.cutover_rollback",
        "version": 1,
        "journal_digest": journal.digest(),
    }

    def result(ok, applied, result_state, action, would_roll_back, reason_codes):
        payload = {
            **base,
            "ok": ok,
            "applied": applied,
            "state": result_state,
            "action": action,
            "would_roll_back": would_roll_back,
            "reason_codes": list(reason_codes),
        }
        validate_public_summary(payload)
        return payload

    if state == "rolled_back":
        return result(
            False, bool(apply), state, "", False, ["journal_already_rolled_back"]
        )
    rollbackable = state in _ROLLBACKABLE_STATES or (
        state == "failed" and journal.failed_step in _ROLLBACKABLE_FAILED_STEPS
    )
    if not rollbackable:
        return result(
            False, bool(apply), state, "", False, ["journal_state_not_rollbackable"]
        )
    codex_config_path = journal.private.get("codex_config_path")
    if not codex_config_path:
        return result(
            False, bool(apply), state, "", False, ["journal_private_missing"]
        )
    editor = (editor_factory or CodexConfigEditor)(codex_config_path)
    snapshot = editor.snapshot()
    env = snapshot.stanza.get("env")
    mode = env.get("EVOLVMEM_CONTEXT_MODE") if isinstance(env, dict) else None
    if not apply:
        return result(True, False, state, "", mode == "primary", [])
    action, after_hash = rollback_codex_to_legacy(editor)
    codes = [] if action == "cas_legacy" else ["codex_primary_not_applied"]
    journal.mark_rolled_back(
        reason_codes=[*codes, "operational_rollback"],
        rollback_stanza_sha256=after_hash,
    )
    return result(True, True, "rolled_back", action, True, codes)


def _cmd_rollback(args) -> int:
    payload = rollback_journal_file(args.journal, apply=args.apply)
    _emit(payload, args.json, f"state={payload['state']} action={payload['action']}")
    return 0 if payload["ok"] else 1


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "cutover":
        # --allow-fts-only is meaningful only beside the second explicit
        # --apply flag; --apply itself requires --writers-restarted.
        if args.allow_fts_only and not args.apply:
            parser.error("--allow-fts-only requires an explicit --apply")
        if args.apply and not args.writers_restarted:
            parser.error("--apply requires --writers-restarted")
    handlers = {
        "preflight": _cmd_preflight,
        "backup": _cmd_backup,
        "cutover": _cmd_cutover,
        "rollback": _cmd_rollback,
    }
    with _scrubbed_environment():
        try:
            return handlers[args.command](args)
        except (CutoverError, CutoverJournalError, ContextValidationError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except (CodexConfigError, CutoverBackupError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())

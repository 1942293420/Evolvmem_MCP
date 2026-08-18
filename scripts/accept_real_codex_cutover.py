#!/usr/bin/env python3
"""Real-Codex post-switch acceptance runner for the Context cutover.

Plan Task 14 Step 5: after the formal cutover switched Codex to primary,
this runner proves one fresh ``codex exec --ephemeral --json`` session
recalls an explicitly authorized canary through the switched
configuration — no ``-c`` overrides, no harness-owned temporary library,
and never Task 11's isolated FTS-only allowance (that flag exists only
for script-owned temp libraries and is refused for real data).

Flow:

1. load the cutover journal (must be ``awaiting_post_cutover_canary``) and
   read the data directory / Codex config path from its private block;
2. prepare the exact canary through ContextService;
3. launch one fresh ephemeral Codex session whose prompt names no tool and
   require session-start before the first answer, a search that hits the
   canary, then a same-ID L2 read — reusing Task 11's privacy-safe event
   parser and evaluator;
4. in ``finally``, exact-clean the canary;
5. on behavioral failure, after cleanup, roll Codex back to explicit
   legacy by journal CAS; the database is never restored.

The public report carries labels, whitelisted tool names, integer IDs,
counts, durations, and reason codes only — never prompts, queries, canary
text, or filesystem paths.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time

from evolvmem.codex_config import CodexConfigEditor
from evolvmem.config import Config
from evolvmem.cutover import (
    CutoverJournal,
    CutoverJournalError,
    rollback_codex_to_legacy,
)
from evolvmem.cutover_canary import CutoverCanary, CutoverCanaryError
from scripts import accept_codex_cutover as acc

SCHEMA = "evolvmem.accept_real_codex_cutover"
VERSION = 1

# The post-switch canary journal sits beside the cutover journal under its
# own name so it can never collide with the shadow step's canary journal.
REAL_CANARY_JOURNAL_NAME = "post-cutover-canary.json"

_ENV_OVERRIDE_KEYS = ("EVOLVMEM_DATA_DIR", "EVOLVMEM_CONTEXT_MODE", "EVOLVMEM_ADAPTER")

_SAFE_REASON_CODES = frozenset(acc.REASON_CODES) | frozenset(
    {
        "journal_unreadable",
        "journal_state_invalid",
        "journal_private_missing",
        "journal_mark_failed",
        "canary_prepare_failed",
        "canary_cleanup_failed",
        "codex_rollback_failed",
        "harness_error",
    }
)


def build_session_argv(*, codex_bin: str, workdir: Path, prompt: str) -> list[str]:
    """One fresh ephemeral session against the switched config as-is."""
    return [
        codex_bin,
        "exec",
        "--ephemeral",
        "--json",
        "--skip-git-repo-check",
        "-C",
        str(workdir),
        prompt,
    ]


def recall_prompt() -> str:
    """The prompt never names a tool; recall must be automatic."""
    return (
        "这是我们的一次全新会话。请回忆一下我们之前记录的、供切换验收使用的"
        "那条全局偏好，给出它的完整细节和原文。"
    )


def _report(
    *,
    passed: bool,
    reason_codes,
    session: dict | None,
    canary: dict,
    rollback: str,
    journal_state: str,
    duration_ms: int,
    forbidden,
) -> dict:
    report = {
        "schema": SCHEMA,
        "version": VERSION,
        "passed": bool(passed),
        "reason_codes": list(dict.fromkeys(reason_codes)),
        "session": session,
        "canary": canary,
        "rollback": rollback,
        "journal_state": journal_state,
        "duration_ms": duration_ms,
    }
    try:
        acc.assert_privacy_safe(report, forbidden)
    except acc.PrivacyBreach:
        report = {
            "schema": SCHEMA,
            "version": VERSION,
            "passed": False,
            "reason_codes": ["privacy_breach"],
            "session": None,
            "canary": {"cleaned": canary.get("cleaned", False)},
            "rollback": rollback,
            "journal_state": journal_state,
            "duration_ms": duration_ms,
        }
    return report


def run_acceptance(
    *,
    journal_path,
    codex_bin: str,
    workdir,
    timeout_s: float = 300.0,
    session_runner=None,
    clock=None,
) -> dict:
    """Run the post-switch canary acceptance; see the module docstring."""
    started = time.monotonic()
    session_runner = session_runner or acc.run_codex_session
    journal_path = Path(journal_path)
    workdir = Path(workdir)
    duration = lambda: int((time.monotonic() - started) * 1000)  # noqa: E731
    canary_state = {"cleaned": False}

    try:
        journal = CutoverJournal.load(journal_path)
    except CutoverJournalError:
        return _report(
            passed=False,
            reason_codes=["journal_unreadable"],
            session=None,
            canary=canary_state,
            rollback="",
            journal_state="",
            duration_ms=duration(),
            forbidden=[],
        )
    if journal.state != "awaiting_post_cutover_canary":
        return _report(
            passed=False,
            reason_codes=["journal_state_invalid"],
            session=None,
            canary=canary_state,
            rollback="",
            journal_state=journal.state,
            duration_ms=duration(),
            forbidden=[],
        )
    private = journal.private
    data_dir = private.get("data_dir")
    codex_config_path = private.get("codex_config_path")
    if not data_dir or not codex_config_path:
        return _report(
            passed=False,
            reason_codes=["journal_private_missing"],
            session=None,
            canary=canary_state,
            rollback="",
            journal_state=journal.state,
            duration_ms=duration(),
            forbidden=[],
        )

    config = Config(data_dir=Path(data_dir))
    canary = CutoverCanary(
        config,
        journal_directory=journal_path.parent,
        journal_name=REAL_CANARY_JOURNAL_NAME,
        externally_locked=False,
        clock=clock,
    )
    handle = None
    trace = None
    reasons: list[str] = []
    forbidden: list[str] = [str(data_dir), str(codex_config_path), str(workdir)]
    try:
        handle = canary.prepare(authorized=True)
        forbidden += [
            handle.nonce,
            handle.key,
            handle.body,
            handle.exact_query,
            handle.cjk_query,
        ]
        canary_state = {
            "cleaned": False,
            "legacy_id": handle.legacy_id,
            "context_id": handle.context_id,
        }
        raw = session_runner(
            build_session_argv(
                codex_bin=codex_bin, workdir=workdir, prompt=recall_prompt()
            ),
            timeout_s=timeout_s,
            workdir=workdir,
        )
        trace = acc.parse_session_events(
            raw.jsonl,
            label="post-cutover",
            l2_sentinel="",
            expected_server="evolvmem",
            exit_code=raw.exit_code,
            timed_out=raw.timed_out,
            duration_ms=raw.duration_ms,
        )
        reasons.extend(
            acc.evaluate_recall_session(
                trace,
                canary_context_id=handle.context_id,
                l1_budget_chars=config.context_inject_max_chars,
            )
        )
    except CutoverCanaryError:
        reasons.append("canary_prepare_failed")
    except Exception:
        reasons.append("harness_error")
    finally:
        if handle is not None:
            try:
                canary.cleanup(handle)
                canary_state["cleaned"] = True
            except CutoverCanaryError:
                reasons.append("canary_cleanup_failed")
        canary.close()

    session_summary = None
    if trace is not None:
        session_summary = acc.public_session_summary(trace, tuple(reasons))

    passed = not reasons
    journal_state = journal.state
    if passed:
        try:
            journal.advance("complete", clock=clock)
            journal_state = journal.state
        except Exception:
            reasons.append("journal_mark_failed")
            passed = False

    rollback_action = ""
    if not passed and any(code != "journal_mark_failed" for code in reasons):
        # Behavioral failure: operational Codex legacy rollback by journal
        # CAS, after the canary cleanup; the database is never restored.
        try:
            rollback_action, after_hash = rollback_codex_to_legacy(
                CodexConfigEditor(codex_config_path)
            )
            journal.mark_rolled_back(
                reason_codes=tuple(
                    code for code in reasons if code in _SAFE_REASON_CODES
                ),
                rollback_stanza_sha256=after_hash,
            )
            journal_state = journal.state
        except Exception:
            rollback_action = "failed"
            reasons.append("codex_rollback_failed")

    return _report(
        passed=passed,
        reason_codes=reasons,
        session=session_summary,
        canary=canary_state,
        rollback=rollback_action,
        journal_state=journal_state,
        duration_ms=duration(),
        forbidden=forbidden,
    )


# ---- CLI ----


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


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--journal", required=True, type=_existing_file)
    parser.add_argument("--codex-bin", required=True, type=_absolute_path)
    parser.add_argument(
        "--workdir",
        type=_absolute_path,
        default=Path.cwd(),
        help="workspace directory for the Codex session",
    )
    parser.add_argument("--timeout-s", type=float, default=300.0)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    if not args.workdir.is_dir():
        print("workdir does not exist", file=sys.stderr)
        return 2
    saved = {}
    for key in _ENV_OVERRIDE_KEYS:
        if key in os.environ:
            saved[key] = os.environ.pop(key)
    try:
        report = run_acceptance(
            journal_path=args.journal,
            codex_bin=str(args.codex_bin),
            workdir=args.workdir,
            timeout_s=args.timeout_s,
        )
    finally:
        os.environ.update(saved)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"passed={report['passed']} journal_state={report['journal_state']}")
        if report["reason_codes"]:
            print("reason_codes: " + ", ".join(report["reason_codes"]))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

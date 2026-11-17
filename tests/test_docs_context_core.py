"""Lightweight documentation assertions for the Context Core Codex cutover.

These tests require README.md and docs/codex-context-core-runbook.md to keep
covering the operator-facing topics listed in the cutover plan (Task 15).
They match stable keywords/section markers only — never full prose — so
rewording the documentation does not break them.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
README_PATH = REPO_ROOT / "README.md"
RUNBOOK_PATH = REPO_ROOT / "docs" / "codex-context-core-runbook.md"

README = (README_PATH.read_text(encoding="utf-8") + "\n" +
          (REPO_ROOT / "docs" / "context-core.md").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def runbook() -> str:
    assert RUNBOOK_PATH.is_file(), "docs/codex-context-core-runbook.md is missing"
    return RUNBOOK_PATH.read_text(encoding="utf-8")


def _assert_mentions(text: str, needle: str, doc: str) -> None:
    assert needle in text, f"{doc} must mention {needle!r}"


# ---- README: Context Core routing state ----


def test_readme_context_source_of_truth() -> None:
    _assert_mentions(README, "source of truth", "README.md")
    _assert_mentions(README, "ContextItem", "README.md")


def test_readme_legacy_compatibility_projection() -> None:
    _assert_mentions(README, "legacy projection", "README.md")


def test_readme_adapter_matrix() -> None:
    # Public documentation covers every supported adapter and its actual mode boundary.
    for adapter in ("Codex", "Claude", "Kimi", "DSH", "Web"):
        _assert_mentions(README, adapter, "README.md")
    _assert_mentions(README, "primary", "README.md")
    _assert_mentions(README, "compat", "README.md")


def test_readme_four_modes() -> None:
    for mode in ("legacy", "compat", "shadow", "primary"):
        _assert_mentions(README, f"`{mode}`", "README.md")


def test_readme_four_context_tools() -> None:
    for tool in (
        "context_session_start",
        "context_search",
        "context_read",
        "context_status",
    ):
        _assert_mentions(README, f"`{tool}`", "README.md")


def test_readme_layered_disclosure() -> None:
    # L0 search hits, L1 session-start injection, exact-ID L2 disclosure.
    for layer in ("L0", "L1", "L2"):
        _assert_mentions(README, layer, "README.md")
    _assert_mentions(README, "exact context ID", "README.md")


def test_readme_default_budgets_and_thresholds() -> None:
    for config_key in (
        "context_inject_max_chars",
        "context_inject_max_items",
        "context_inject_pinned_max_chars",
        "context_inject_project_max_chars",
        "context_inject_related_max_chars",
        "context_min_confidence",
        "context_vector_min_similarity",
    ):
        _assert_mentions(README, f"`{config_key}`", "README.md")
    for default in ("6000", "12", "1500", "3000", "0.55", "0.80"):
        _assert_mentions(README, default, "README.md")


def test_readme_automatic_call_limitation() -> None:
    # Automatic recall relies on Codex following the server instructions and
    # fails open when a context tool is unavailable, errors, or times out.
    _assert_mentions(README, "instructions", "README.md")
    _assert_mentions(README, "fail-open", "README.md")


def test_readme_adapter_expansion_is_future_work() -> None:
    _assert_mentions(README, "future work", "README.md")


def test_readme_points_to_runbook() -> None:
    _assert_mentions(README, "docs/codex-context-core-runbook.md", "README.md")


# ---- README: Phase 3 archives, candidates, and lifecycle ----


def test_readme_phase3_encrypted_session_archives() -> None:
    for needle in ("AES-GCM", "session_archives/", "archive.key", "0600"):
        _assert_mentions(README, needle, "README.md")
    _assert_mentions(README, "never falls back to plaintext", "README.md")
    _assert_mentions(README, "`context_archive_ttl_days`", "README.md")
    _assert_mentions(README, "irreversible", "README.md")


def test_readme_phase3_candidate_isolation_and_promotion() -> None:
    _assert_mentions(README, "candidate", "README.md")
    _assert_mentions(README, "list_candidates", "README.md")
    _assert_mentions(README, "distinct session archives", "README.md")
    _assert_mentions(README, "`context_promotion_min_successes`", "README.md")


def test_readme_phase3_playbook_generation_and_degradation() -> None:
    _assert_mentions(README, "`context_playbook_min_experiences`", "README.md")
    _assert_mentions(
        README, "`context_promotion_similarity_threshold`", "README.md"
    )
    _assert_mentions(README, "0.95", "README.md")
    _assert_mentions(README, "degraded", "README.md")


def test_readme_phase3_lifecycle_write_tools() -> None:
    for tool in (
        "context_confirm",
        "context_record_outcome",
        "context_archive_project",
        "context_sweep",
    ):
        _assert_mentions(README, f"`{tool}`", "README.md")
    # Write tools carry no readOnlyHint and therefore require write approval.
    _assert_mentions(README, "readOnlyHint", "README.md")
    _assert_mentions(README, "write approval", "README.md")


# ---- Runbook: operator procedure and safety ----


def test_runbook_side_effect_free_preflight(runbook: str) -> None:
    _assert_mentions(runbook, "preflight", "runbook")
    _assert_mentions(runbook, "side-effect-free", "runbook")
    _assert_mentions(runbook, "dry-run", "runbook")


def test_runbook_backup_and_retention(runbook: str) -> None:
    _assert_mentions(runbook, "backups/context-core-cutover-", "runbook")
    _assert_mentions(runbook, "never deleted automatically", "runbook")


def test_runbook_writers_restart(runbook: str) -> None:
    _assert_mentions(runbook, "--writers-restarted", "runbook")


def test_runbook_config_scope_and_cas(runbook: str) -> None:
    _assert_mentions(runbook, "mcp_servers.evolvmem", "runbook")
    _assert_mentions(runbook, "compare-and-swap", "runbook")


def test_runbook_operational_rollback_and_destructive_restore(runbook: str) -> None:
    _assert_mentions(runbook, "rollback", "runbook")
    _assert_mentions(runbook, "legacy", "runbook")
    _assert_mentions(runbook, "destructive", "runbook")


def test_runbook_fts_only_degraded_meaning(runbook: str) -> None:
    _assert_mentions(runbook, "--allow-fts-only", "runbook")
    _assert_mentions(runbook, "degraded", "runbook")


def test_runbook_journal_and_status(runbook: str) -> None:
    _assert_mentions(runbook, "cutover-journal.json", "runbook")
    _assert_mentions(runbook, "awaiting_post_cutover_canary", "runbook")
    _assert_mentions(runbook, "rolled_back", "runbook")
    _assert_mentions(runbook, "projection_lag", "runbook")


def test_runbook_env_precedence_and_fail_closed(runbook: str) -> None:
    _assert_mentions(runbook, "EVOLVMEM_CONTEXT_MODE", "runbook")
    _assert_mentions(runbook, "EVOLVMEM_ADAPTER", "runbook")
    _assert_mentions(runbook, "fail-closed", "runbook")


def test_runbook_approval_semantics(runbook: str) -> None:
    _assert_mentions(runbook, "default_tools_approval_mode", "runbook")
    _assert_mentions(runbook, "readOnlyHint", "runbook")


def test_runbook_cites_official_codex_mcp_docs(runbook: str) -> None:
    _assert_mentions(
        runbook, "https://developers.openai.com/codex/mcp", "runbook"
    )


def test_runbook_cli_0147_dual_verification(runbook: str) -> None:
    # CLI 0.147 does not echo the approval field; verification is dual-source.
    _assert_mentions(runbook, "0.147", "runbook")
    _assert_mentions(runbook, "codex mcp get", "runbook")


# ---- Runbook: Phase 3 operations ----


def test_runbook_phase3_key_file_permissions(runbook: str) -> None:
    _assert_mentions(runbook, "archive.key", "runbook")
    _assert_mentions(runbook, "0600", "runbook")
    _assert_mentions(runbook, "0700", "runbook")
    _assert_mentions(runbook, "never falls back to plaintext", "runbook")


def test_runbook_phase3_purge_troubleshooting(runbook: str) -> None:
    _assert_mentions(runbook, "failed_archive_ids", "runbook")
    _assert_mentions(runbook, "irreversible", "runbook")
    _assert_mentions(runbook, "available", "runbook")
    _assert_mentions(runbook, "purged", "runbook")
    _assert_mentions(runbook, "source_state", "runbook")


def test_runbook_phase3_candidate_review_api(runbook: str) -> None:
    _assert_mentions(runbook, "list_candidates", "runbook")
    _assert_mentions(runbook, "context_confirm", "runbook")
    _assert_mentions(runbook, "context_record_outcome", "runbook")
    _assert_mentions(runbook, "context_promotion_min_successes", "runbook")

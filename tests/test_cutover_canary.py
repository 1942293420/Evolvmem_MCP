"""Behavioral contracts for the cutover canary and the real-Codex runner.

The canary service creates exactly one explicitly authorized, high-entropy,
global pinned preference through ContextService (no semantic merge), writes
an owner-only journal that privately holds the body/query plus both IDs and
hashes, and returns only hashes/IDs publicly. The canary must be eligible
for session-start L1, exact FTS search, and exact-ID L2 reads. Cleanup
re-reads and verifies both IDs, the mapping, the source kind, and the
content hash before calling the dual ``legacy_hard_delete``; any mismatch
refuses deletion and retains the journal. Success removes both vector
entries, verifies absence by exact-ID reads (never a new similarity
search), and marks the journal cleaned.

The real-Codex post-switch runner prepares this same canary against the
cutover journal's data directory, launches one fresh
``codex exec --ephemeral --json`` whose prompt names no tool, requires
session-start before the first answer followed by a search and a same-ID
L2 read, exact-cleans the canary in ``finally``, and on behavioral failure
rolls Codex back to explicit legacy by journal CAS — never restoring the
database. Tests here are deterministic: the Codex process is faked with
canned event streams and every path is temporary.
"""

import hashlib
import json
from pathlib import Path
import stat

import pytest

from evolvmem.codex_config import CodexConfigEditor
from evolvmem.config import Config
from evolvmem.context_migration import LegacyMemoryMigrator
from evolvmem.context_models import (
    ContextLayer,
    ContextMode,
    ContextReadRequest,
    ContextScope,
    ContextSearchRequest,
    ContextSessionStartRequest,
    ContextStatus,
    ContextTier,
    ContextContentType,
)
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.cutover import CutoverJournal, JOURNAL_FILENAME, run_cutover
from evolvmem.cutover_canary import (
    CANARY_JOURNAL_FILENAME,
    CANARY_SOURCE_KIND,
    CanaryVerificationError,
    CutoverCanary,
    CutoverCanaryError,
)
from evolvmem.cutover_models import validate_public_summary
from evolvmem.memory_store import MemoryStore
from evolvmem.vector_index import VectorIndex

from scripts import accept_codex_cutover as acc
from scripts import accept_real_codex_cutover as real_accept

from tests.test_cutover import (
    _cli_probe_for,
    _real_request,
    _seed_library,
    _write_codex_config,
)


def _migrated_library(config: Config) -> None:
    """A small legacy library migrated into the Context Core."""
    _seed_library(config)
    store = ContextStore(config)
    store.initialize()
    try:
        LegacyMemoryMigrator(store, config).migrate()
    finally:
        store.close()


def _canary(config: Config, journal_dir: Path, **kw) -> CutoverCanary:
    return CutoverCanary(config, journal_directory=journal_dir, **kw)


def _journal_file(journal_dir: Path) -> Path:
    return journal_dir / CANARY_JOURNAL_FILENAME


# ---- preparation: explicit authorization, dual write, privacy split ----


def test_prepare_requires_explicit_authorization(test_config, tmp_path):
    _migrated_library(test_config)
    canary = _canary(test_config, tmp_path)
    with pytest.raises(CutoverCanaryError):
        canary.prepare()
    with pytest.raises(CutoverCanaryError):
        canary.prepare(authorized=False)
    # Nothing was written: no canary rows, no journal file.
    store = ContextStore(test_config)
    store.initialize(create_schema=False)
    try:
        assert store.count_by_status().get("active", 0) == 2
    finally:
        store.close()
    assert not _journal_file(tmp_path).exists()


def test_prepare_writes_one_dual_pinned_global_canary(test_config, tmp_path):
    _migrated_library(test_config)
    canary = _canary(test_config, tmp_path)
    handle = canary.prepare(authorized=True)
    try:
        store = ContextStore(test_config)
        store.initialize(create_schema=False)
        try:
            row = store.legacy_projection().get_by_id(handle.legacy_id)
            assert row is not None
            assert row["status"] == "active"
            item = store.get_item(handle.context_id)
            assert item is not None
            assert item.status is ContextStatus.ACTIVE
            assert item.tier is ContextTier.PINNED
            assert item.scope is ContextScope.GLOBAL
            assert item.content_type is ContextContentType.PREFERENCE
            assert item.identity_key == handle.key
            # Exactly the three layers.
            for layer in (ContextLayer.L0, ContextLayer.L1, ContextLayer.L2):
                assert store.get_layer(handle.context_id, layer)
            assert (
                store.resolve_legacy_mapping(handle.legacy_id) == handle.context_id
            )
            # The provenance source-kind row marks the item as ours.
            sources = store._connection().execute(
                "SELECT source_kind, source_ref FROM context_sources "
                "WHERE item_id=?",
                (handle.context_id,),
            ).fetchall()
            assert [str(source["source_kind"]) for source in sources] == [
                CANARY_SOURCE_KIND
            ]
        finally:
            store.close()

        # The owner-only journal privately holds body and queries.
        journal_path = _journal_file(tmp_path)
        assert stat.S_IMODE(journal_path.stat().st_mode) == 0o600
        payload = json.loads(journal_path.read_text(encoding="utf-8"))
        assert payload["body"] == handle.body
        assert payload["exact_query"] in handle.body
        assert payload["cjk_query"] in handle.body
        assert payload["legacy_id"] == handle.legacy_id
        assert payload["context_id"] == handle.context_id

        # The public handle projection carries hashes and IDs only.
        public = handle.public_dict()
        validate_public_summary(public)
        blob = json.dumps(public, ensure_ascii=False)
        assert handle.body not in blob
        assert handle.key not in blob
        assert handle.exact_query not in blob
        assert handle.cjk_query not in blob
        assert public["legacy_id"] == handle.legacy_id
        assert public["context_id"] == handle.context_id
    finally:
        canary.cleanup(handle)


def test_prepare_refuses_to_stack_a_second_live_canary(test_config, tmp_path):
    _migrated_library(test_config)
    canary = _canary(test_config, tmp_path)
    handle = canary.prepare(authorized=True)
    try:
        with pytest.raises(CutoverCanaryError):
            _canary(test_config, tmp_path).prepare(authorized=True)
    finally:
        canary.cleanup(handle)


def test_canary_is_eligible_for_l1_exact_fts_and_exact_l2(test_config, tmp_path):
    _migrated_library(test_config)
    canary = _canary(test_config, tmp_path)
    handle = canary.prepare(authorized=True)
    try:
        service = ContextService(test_config)
        service.initialize(mode=ContextMode.SHADOW, adapter="codex")
        try:
            started = service.session_start(
                ContextSessionStartRequest(project="", query=handle.exact_query)
            )
            assert handle.context_id in started.selected_ids

            hits = service.search(
                ContextSearchRequest(query=handle.exact_query, top_k=5)
            )
            assert hits[0].id == handle.context_id

            cjk_hits = service.search(
                ContextSearchRequest(query=handle.cjk_query, top_k=5)
            )
            assert cjk_hits[0].id == handle.context_id

            read = service.read(
                ContextReadRequest(id=handle.context_id, layer=ContextLayer.L2)
            )
            assert read.error_code is None
            assert hashlib.sha256(read.content.encode("utf-8")).hexdigest() == (
                handle.l2_sha256
            )
        finally:
            service.close()
    finally:
        canary.cleanup(handle)


# ---- cleanup: verify everything, delete exactly, verify absence ----


def test_cleanup_removes_both_sides_and_marks_the_journal(test_config, tmp_path):
    _migrated_library(test_config)
    canary = _canary(test_config, tmp_path)
    handle = canary.prepare(authorized=True)

    summary = canary.cleanup(handle)
    validate_public_summary(summary)
    assert summary["cleaned"] is True
    assert summary["legacy_id"] == handle.legacy_id

    store = ContextStore(test_config)
    store.initialize(create_schema=False)
    try:
        assert store.legacy_projection().get_by_id(handle.legacy_id) is None
        assert store.get_item(handle.context_id) is None
        assert store.resolve_legacy_mapping(handle.legacy_id) is None
        assert store.count_by_status().get("active", 0) == 2  # seeded rows stay
    finally:
        store.close()

    # Absence is verified by exact ID reads, never a new similarity search.
    service = ContextService(test_config)
    service.initialize(mode=ContextMode.SHADOW, adapter="codex")
    try:
        read = service.read(
            ContextReadRequest(id=handle.context_id, layer=ContextLayer.L2)
        )
        assert read.error_code == "not_found"
    finally:
        service.close()

    for path, removed_id in (
        (test_config.vector_path, handle.legacy_id),
        (test_config.context_vector_path, handle.context_id),
    ):
        if path.is_file():
            index = VectorIndex(test_config, path=path)
            index.initialize(dim=test_config.embedding_dim)
            try:
                assert removed_id not in index.ids()
            finally:
                index.close()

    payload = json.loads(_journal_file(tmp_path).read_text(encoding="utf-8"))
    assert payload["state"] == "cleaned"


def test_cleanup_refuses_when_the_content_hash_differs(test_config, tmp_path):
    _migrated_library(test_config)
    canary = _canary(test_config, tmp_path)
    handle = canary.prepare(authorized=True)
    store = ContextStore(test_config)
    store.initialize(create_schema=False)
    try:
        with store.transaction():
            store._connection().execute(
                "UPDATE context_layers SET content=content || ' tampered' "
                "WHERE item_id=? AND layer='l2'",
                (handle.context_id,),
            )
    finally:
        store.close()

    with pytest.raises(CanaryVerificationError):
        canary.cleanup(handle)
    # Refusal keeps both sides and the journal for recovery.
    store = ContextStore(test_config)
    store.initialize(create_schema=False)
    try:
        assert store.legacy_projection().get_by_id(handle.legacy_id) is not None
        assert store.get_item(handle.context_id) is not None
    finally:
        store.close()
    payload = json.loads(_journal_file(tmp_path).read_text(encoding="utf-8"))
    assert payload["state"] == "prepared"


def test_cleanup_refuses_when_the_mapping_disagrees(test_config, tmp_path):
    _migrated_library(test_config)
    canary = _canary(test_config, tmp_path)
    handle = canary.prepare(authorized=True)
    store = ContextStore(test_config)
    store.initialize(create_schema=False)
    try:
        with store.transaction():
            store.delete_legacy_mapping(handle.legacy_id)
    finally:
        store.close()
    with pytest.raises(CanaryVerificationError):
        canary.cleanup(handle)
    store = ContextStore(test_config)
    store.initialize(create_schema=False)
    try:
        assert store.get_item(handle.context_id) is not None
    finally:
        store.close()


def test_cleanup_refuses_when_the_source_kind_is_missing(test_config, tmp_path):
    _migrated_library(test_config)
    canary = _canary(test_config, tmp_path)
    handle = canary.prepare(authorized=True)
    store = ContextStore(test_config)
    store.initialize(create_schema=False)
    try:
        with store.transaction():
            store._connection().execute(
                "DELETE FROM context_sources WHERE item_id=? AND source_kind=?",
                (handle.context_id, CANARY_SOURCE_KIND),
            )
    finally:
        store.close()
    with pytest.raises(CanaryVerificationError):
        canary.cleanup(handle)


def test_cleanup_refuses_a_tampered_or_missing_journal(test_config, tmp_path):
    _migrated_library(test_config)
    canary = _canary(test_config, tmp_path)
    handle = canary.prepare(authorized=True)
    journal_path = _journal_file(tmp_path)
    payload = json.loads(journal_path.read_text(encoding="utf-8"))
    payload["context_id"] = handle.context_id + 1000
    journal_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(CanaryVerificationError):
        canary.cleanup(handle)

    journal_path.unlink()
    with pytest.raises(CutoverCanaryError):
        canary.cleanup(handle)


def test_cleanup_refuses_to_run_twice(test_config, tmp_path):
    _migrated_library(test_config)
    canary = _canary(test_config, tmp_path)
    handle = canary.prepare(authorized=True)
    canary.cleanup(handle)
    with pytest.raises(CutoverCanaryError):
        canary.cleanup(handle)


# ---- real-Codex post-switch runner (deterministic: faked process) ----


def _tool_event(tool: str, payload: dict, server: str = "evolvmem") -> str:
    return json.dumps(
        {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": server,
                "tool": tool,
                "status": "completed",
                "result": {
                    "content": [
                        {"type": "text", "text": json.dumps(payload)}
                    ]
                },
            },
        }
    )


def _answer_event() -> str:
    return json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "synthetic answer"},
        }
    )


def _happy_stream(context_id: int) -> str:
    return "\n".join(
        [
            _tool_event(
                "context_session_start",
                {"used_chars": 120, "selected_ids": [context_id], "block": "..."},
            ),
            _tool_event("context_search", {"results": [{"id": context_id}]}),
            _tool_event("context_read", {"id": context_id, "layer": "l2"}),
            _answer_event(),
        ]
    )


def _missing_search_stream(context_id: int) -> str:
    return "\n".join(
        [
            _tool_event(
                "context_session_start",
                {"used_chars": 120, "selected_ids": [context_id], "block": "..."},
            ),
            _tool_event("context_read", {"id": context_id, "layer": "l2"}),
            _answer_event(),
        ]
    )


def _post_cutover_env(test_config, tmp_path):
    """A real completed cutover over a synthetic library; Codex is primary."""
    _seed_library(test_config)
    request, codex_path = _real_request(test_config, tmp_path)
    outcome = run_cutover(request, cli_probe=_cli_probe_for(codex_path))
    assert outcome.state == "awaiting_post_cutover_canary"
    journals = list(test_config.data_dir.rglob(JOURNAL_FILENAME))
    assert len(journals) == 1
    return journals[0], codex_path


def _canary_context_id(journal_path: Path) -> int:
    payload = json.loads(
        (journal_path.parent / real_accept.REAL_CANARY_JOURNAL_NAME).read_text(
            encoding="utf-8"
        )
    )
    return int(payload["context_id"])


def _count_items(config: Config) -> int:
    store = ContextStore(config)
    store.initialize(create_schema=False)
    try:
        return sum(store.count_by_status().values())
    finally:
        store.close()


def _stream_runner(stream_for):
    def runner(argv, *, timeout_s, workdir):
        return acc.RawRun(
            jsonl=stream_for(),
            exit_code=0,
            timed_out=False,
            duration_ms=10,
        )

    return runner


def test_runner_passes_cleans_and_completes_the_journal(test_config, tmp_path):
    journal_path, codex_path = _post_cutover_env(test_config, tmp_path)
    before_items = _count_items(test_config)
    forbidden_probe = str(test_config.data_dir)

    runner = _stream_runner(
        lambda: _happy_stream(_canary_context_id(journal_path))
    )
    report = real_accept.run_acceptance(
        journal_path=journal_path,
        codex_bin="/synthetic/codex",
        workdir=tmp_path,
        session_runner=runner,
    )

    assert report["passed"] is True
    assert report["reason_codes"] == []
    assert report["canary"]["cleaned"] is True
    assert report["journal_state"] == "complete"
    assert CutoverJournal.load(journal_path).state == "complete"
    assert _count_items(test_config) == before_items  # canary exact-cleaned
    # The switched config stays primary after a passing acceptance.
    stanza = CodexConfigEditor(codex_path).snapshot().stanza
    assert stanza["env"]["EVOLVMEM_CONTEXT_MODE"] == "primary"
    # The public report carries no canary content, query, or path.
    assert forbidden_probe not in json.dumps(report, ensure_ascii=False)


def test_runner_behavioral_failure_cleans_then_rolls_back_codex(
    test_config, tmp_path
):
    journal_path, codex_path = _post_cutover_env(test_config, tmp_path)
    before_items = _count_items(test_config)

    runner = _stream_runner(
        lambda: _missing_search_stream(_canary_context_id(journal_path))
    )
    report = real_accept.run_acceptance(
        journal_path=journal_path,
        codex_bin="/synthetic/codex",
        workdir=tmp_path,
        session_runner=runner,
    )

    assert report["passed"] is False
    assert "missing_context_search" in report["reason_codes"]
    assert report["canary"]["cleaned"] is True
    assert report["rollback"] == "cas_legacy"
    assert report["journal_state"] == "rolled_back"

    # Codex is back to explicit legacy; the database is never restored.
    stanza = CodexConfigEditor(codex_path).snapshot().stanza
    assert stanza["env"]["EVOLVMEM_CONTEXT_MODE"] == "legacy"
    journal = CutoverJournal.load(journal_path)
    assert journal.state == "rolled_back"
    persisted = json.loads(test_config.config_path.read_text(encoding="utf-8"))
    assert persisted["context_mode"] == "compat"  # retained, not restored
    assert _count_items(test_config) == before_items


def test_runner_refuses_a_journal_that_is_not_awaiting_canary(
    test_config, tmp_path
):
    journal_path, codex_path = _post_cutover_env(test_config, tmp_path)
    journal = CutoverJournal.load(journal_path)
    journal.advance("complete")
    before_items = _count_items(test_config)

    called = []

    def runner(argv, *, timeout_s, workdir):
        called.append(argv)
        return acc.RawRun(jsonl="", exit_code=0, timed_out=False, duration_ms=0)

    report = real_accept.run_acceptance(
        journal_path=journal_path,
        codex_bin="/synthetic/codex",
        workdir=tmp_path,
        session_runner=runner,
    )
    assert report["passed"] is False
    assert report["reason_codes"] == ["journal_state_invalid"]
    assert called == []  # no Codex session and no canary were ever started
    assert _count_items(test_config) == before_items
    assert not (journal_path.parent / real_accept.REAL_CANARY_JOURNAL_NAME).exists()


def test_runner_reports_cleanup_failure_and_still_rolls_back(
    test_config, tmp_path, monkeypatch
):
    journal_path, codex_path = _post_cutover_env(test_config, tmp_path)

    original_cleanup = CutoverCanary.cleanup

    def failing_cleanup(self, handle=None):
        raise CutoverCanaryError("synthetic_cleanup_failure")

    monkeypatch.setattr(CutoverCanary, "cleanup", failing_cleanup)
    runner = _stream_runner(
        lambda: _missing_search_stream(1)
    )
    report = real_accept.run_acceptance(
        journal_path=journal_path,
        codex_bin="/synthetic/codex",
        workdir=tmp_path,
        session_runner=runner,
    )
    assert report["passed"] is False
    assert "canary_cleanup_failed" in report["reason_codes"]
    assert "missing_context_search" in report["reason_codes"]
    assert report["canary"]["cleaned"] is False
    assert report["rollback"] == "cas_legacy"
    monkeypatch.setattr(CutoverCanary, "cleanup", original_cleanup)


def test_runner_never_accepts_the_isolated_fts_allowance_flag(
    test_config, tmp_path
):
    journal_path, _ = _post_cutover_env(test_config, tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        real_accept.main(
            [
                "--journal",
                str(journal_path),
                "--codex-bin",
                "/synthetic/codex",
                "--allow-isolated-fts-only",
            ]
        )
    assert excinfo.value.code == 2


def test_runner_requires_explicit_journal_and_codex_binary():
    with pytest.raises(SystemExit):
        real_accept.main(["--json"])
    with pytest.raises(SystemExit):
        real_accept.main(["--journal", "/tmp/x.json"])


def test_runner_rejects_a_missing_journal_file(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        real_accept.main(
            [
                "--journal",
                str(tmp_path / "missing.json"),
                "--codex-bin",
                "/synthetic/codex",
                "--json",
            ]
        )
    assert excinfo.value.code == 2

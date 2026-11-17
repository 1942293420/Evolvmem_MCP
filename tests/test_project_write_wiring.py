"""Typed-write project resolution wiring through ProjectResolver.

Every dual-write path (legacy add/replace, extraction batch) resolves the
write's project from structured signals only — never from content — writes
the resolved project onto the Context draft, and persists one resolution row
per written item in the same transaction. Conflict/unresolved writes keep
``project=""`` and land in the pending review queue; global content stays
global; quarantined candidates keep their isolation (no project, no row).
The transient ``workspace_path``/``project_hint`` request fields are consumed
in memory (the path is fingerprinted or dropped) and never persisted.
"""

import pytest

from evolvmem.context_migration import LegacyMemoryMigrator
from evolvmem.context_models import ContextMode
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.legacy_models import (
    LegacyAddRequest,
    LegacyExtractionItem,
    LegacyExtractionRequest,
    LegacyReplaceRequest,
)
from evolvmem.memory_store import MemoryStore
from evolvmem.project_models import ProjectResolutionDecision
from evolvmem.project_store import ProjectStore
from evolvmem.session_archive import SessionArchiver
from evolvmem.workspace_identity import WorkspaceIdentityProvider


@pytest.fixture
def store(test_config):
    with MemoryStore(test_config):
        pass  # legacy projection schema, mirroring a pre-cutover database
    with ContextStore(test_config) as instance:
        yield instance


@pytest.fixture
def service(test_config, store):
    instance = ContextService(test_config, store=store)
    instance.initialize(mode=ContextMode.SHADOW, adapter="codex")
    yield instance
    instance.close()


def _register(store, *projects, aliases=()):
    ps = ProjectStore(store._connection(), store._require_transaction, generic_names=())
    with store.transaction():
        for project in projects:
            ps.register_project(project)
        for alias, project in aliases:
            ps.add_alias(alias, project)


def _resolution_row(store, item_id):
    return store._connection().execute(
        "SELECT resolution_state, review_state, resolved_project "
        "FROM context_project_resolutions WHERE item_id=?",
        (item_id,),
    ).fetchone()


# ---- brief-mandated verbatim tests ----


def test_write_with_registered_key_signal_resolves_project(service, store):
    ps = ProjectStore(store._connection(), store._require_transaction, generic_names=())
    with store.transaction():
        ps.register_project("eva")
        ps.add_alias("evolv", "eva")
    result = service.legacy_add(LegacyAddRequest(key="project:evolv:decision:db", value="x"))
    item = store.get_item(result.context_id)
    assert item.project == "eva"
    row = store._connection().execute(
        "SELECT resolution_state, review_state FROM context_project_resolutions WHERE item_id=?",
        (result.context_id,),
    ).fetchone()
    assert tuple(row) == ("resolved", "not_required")


def test_conflicting_signals_leave_project_empty_and_pending(service, store):
    ps = ProjectStore(store._connection(), store._require_transaction, generic_names=())
    with store.transaction():
        ps.register_project("eva")
        ps.register_project("hermes")
    result = service.legacy_add(
        LegacyAddRequest(key="project:eva:fact:x", value="x", tags=("分类:hermes",))
    )
    item = store.get_item(result.context_id)
    assert item.project == ""
    row = store._connection().execute(
        "SELECT resolution_state, review_state FROM context_project_resolutions WHERE item_id=?",
        (result.context_id,),
    ).fetchone()
    assert tuple(row) == ("conflict", "pending")


def test_global_attribute_stays_global_without_project(service, store):
    result = service.legacy_add(
        LegacyAddRequest(key="user:editor", value="vim", attribute="preference")
    )
    item = store.get_item(result.context_id)
    assert item.project == ""


# ---- additional wiring coverage ----


def test_write_without_signals_is_unresolved_and_pending(service, store):
    result = service.legacy_add(LegacyAddRequest(key="alpha", value="x"))
    item = store.get_item(result.context_id)
    assert item.project == ""
    assert tuple(_resolution_row(store, result.context_id)) == (
        "unresolved",
        "pending",
        "",
    )


def test_explicit_project_hint_is_a_strong_signal(service, store):
    _register(store, "eva", aliases=(("evolv", "eva"),))
    result = service.legacy_add(
        LegacyAddRequest(key="k:1", value="x", project_hint="evolv")
    )
    item = store.get_item(result.context_id)
    assert item.project == "eva"
    assert tuple(_resolution_row(store, result.context_id)) == (
        "resolved",
        "not_required",
        "eva",
    )


def test_hint_disagreeing_with_key_is_a_conflict_never_a_guess(service, store):
    _register(store, "eva", "hermes")
    result = service.legacy_add(
        LegacyAddRequest(key="project:eva:fact:x", value="x", project_hint="hermes")
    )
    item = store.get_item(result.context_id)
    assert item.project == ""
    assert tuple(_resolution_row(store, result.context_id)) == (
        "conflict",
        "pending",
        "",
    )


def test_replace_records_resolution_for_the_new_item(service, store):
    _register(store, "eva", aliases=(("evolv", "eva"),))
    added = service.legacy_add(
        LegacyAddRequest(key="project:evolv:decision:db", value="v1")
    )
    replaced = service.legacy_replace(
        LegacyReplaceRequest(key="project:evolv:decision:db", new_value="v2")
    )
    assert replaced.old_context_id == added.context_id
    item = store.get_item(replaced.context_id)
    assert item.project == "eva"
    assert tuple(_resolution_row(store, replaced.context_id)) == (
        "resolved",
        "not_required",
        "eva",
    )


def test_extraction_batch_resolves_and_records_each_write(service, store):
    _register(store, "eva", aliases=(("evolv", "eva"),))
    result = service.persist_legacy_extraction(
        LegacyExtractionRequest(
            summary=LegacyExtractionItem(
                key="session:2026-09-02:summary",
                value="本次会话确定了数据库选型方向。",
                attribute="fact",
            ),
            candidates=(
                LegacyExtractionItem(
                    key="project:evolv:decision:db",
                    value="数据库采用 SQLite 单文件方案。",
                    attribute="decision",
                ),
            ),
            source_session="session_a",
        )
    )
    assert result.persisted == 2
    candidate = store.get_item(result.candidates[0].context_id)
    assert candidate.project == "eva"
    assert tuple(_resolution_row(store, result.candidates[0].context_id)) == (
        "resolved",
        "not_required",
        "eva",
    )
    summary = store.get_item(result.summary.context_id)
    assert summary.project == ""
    assert tuple(_resolution_row(store, result.summary.context_id)) == (
        "unresolved",
        "pending",
        "",
    )


def test_isolated_candidate_keeps_empty_project_and_writes_no_resolution(
    service, store, test_config
):
    archive = SessionArchiver(test_config, store).archive_session(
        "proj", "kimi", "session_iso", '{"messages": []}'
    )
    assert archive is not None
    result = service.persist_legacy_extraction(
        LegacyExtractionRequest(
            summary=LegacyExtractionItem(
                key="session:2026-09-02:summary",
                value="本次会话排查了 stdio 握手卡顿。",
                attribute="fact",
            ),
            candidates=(
                LegacyExtractionItem(
                    key="experience:proj:stdio-hang",
                    value="MCP 握手卡住时先检查 stdin 预读竞争。",
                    attribute="experience",
                    confidence=0.7,
                ),
            ),
        ),
        source_archive_id=archive.id,
    )
    assert result.persisted == 2
    isolated = result.candidates[0]
    assert isolated.legacy_id is None
    assert isolated.context_status == "candidate"
    item = store.get_item(isolated.context_id)
    assert item.project == ""
    assert _resolution_row(store, isolated.context_id) is None


def test_transient_fields_are_consumed_but_never_persisted(
    service, store, temp_dir, test_config
):
    # 无 workspace.key：身份解析 fail-closed 为空指纹，写入不被阻断
    _register(store, "eva", aliases=(("evolv", "eva"),))
    result = service.legacy_add(
        LegacyAddRequest(
            key="project:evolv:decision:db",
            value="x",
            workspace_path=str(temp_dir),
            project_hint="evolv",
        )
    )
    item = store.get_item(result.context_id)
    assert item.project == "eva"
    projection = store.legacy_projection().get_by_id(result.legacy_id)
    assert "workspace_path" not in projection
    assert "project_hint" not in projection
    row = store._connection().execute(
        "SELECT evidence_json FROM context_project_resolutions WHERE item_id=?",
        (result.context_id,),
    ).fetchone()
    # 证据只含归一化后的公开字段：绝无原始路径，也无原始候选名
    assert str(temp_dir) not in row["evidence_json"]
    assert "evolv" not in row["evidence_json"]


def test_active_default_workspace_binding_resolves_new_writes(
    service, store, temp_dir, test_config
):
    provider = WorkspaceIdentityProvider(test_config.data_dir / "workspace.key")
    provider.bootstrap_key()
    fingerprint = provider.resolve(str(temp_dir)).fingerprint
    ps = ProjectStore(store._connection(), store._require_transaction, generic_names=())
    with store.transaction():
        ps.register_project("eva")
        ps.bind_workspace(fingerprint, "eva", method="cli", make_default=True)
    result = service.legacy_add(
        LegacyAddRequest(key="k:1", value="x", workspace_path=str(temp_dir))
    )
    item = store.get_item(result.context_id)
    assert item.project == "eva"
    assert tuple(_resolution_row(store, result.context_id)) == (
        "resolved",
        "not_required",
        "eva",
    )


def test_migrator_project_for_without_decider_stays_empty(store, test_config):
    migrator = LegacyMemoryMigrator(store, test_config)
    assert migrator.project_for({"key": "project:eva:fact:x"}) == ""
    row = {"id": 1, "key": "project:eva:fact:x", "value": "v", "attribute": "fact"}
    assert migrator.draft_from_projection_row(row).project == ""


def test_migrator_decider_is_delegated_and_explicit_decision_wins(store, test_config):
    migrator = LegacyMemoryMigrator(
        store, test_config, project_decider=lambda row: "decided"
    )
    assert migrator.project_for({"key": "anything"}) == "decided"
    unresolved = ProjectResolutionDecision.unresolved("test-v1", ())
    assert migrator.project_for({"key": "anything"}, decision=unresolved) == ""
    resolved = ProjectResolutionDecision.resolved("eva", "strong", "test-v1", ())
    assert migrator.project_for({"key": "anything"}, decision=resolved) == "eva"

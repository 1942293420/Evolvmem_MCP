import pytest

from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayers,
)
from evolvmem.context_store import ContextStore
from evolvmem.project_models import ProjectResolutionDecision
from evolvmem.project_store import (
    ProjectResolutionRow,
    ProjectStore,
    ProjectStoreError,
)


FP1 = "hmac-sha256:" + "1" * 64
FP2 = "hmac-sha256:" + "2" * 64


@pytest.fixture
def store(test_config):
    with ContextStore(test_config) as instance:
        yield instance


@pytest.fixture
def ps(store):
    return ProjectStore(
        store._connection(),
        store._require_transaction,
        generic_names=("home", "src"),
    )


def make_draft(identity_key: str, *, project: str = "") -> ContextItemDraft:
    return ContextItemDraft(
        identity_key=identity_key,
        content_type=ContextContentType.FACT,
        layers=ContextLayers(
            l0="summary", l1="detail", l2="source", generator="test-suite"
        ),
        project=project,
    )


def _evidence() -> tuple[dict[str, str], ...]:
    return (
        {
            "source": "tags",
            "type": "bare_tag",
            "source_version": "v1",
            "normalized_value": "eva",
        },
    )


def _table_counts(store) -> dict[str, int]:
    conn = store._connection()
    return {
        table: conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        for table in (
            "context_project_registry",
            "context_project_aliases",
            "context_project_workspace_bindings",
            "continuity_focus",
        )
    }


def _resolution_row(store, item_id: int):
    return store._connection().execute(
        "SELECT * FROM context_project_resolutions WHERE item_id=?", (item_id,)
    ).fetchone()


# ---- registry ----


def test_register_project_is_idempotent(store, ps):
    with store.transaction():
        ps.register_project("eva")
        ps.register_project("eva")
    snapshot = ps.snapshot()
    assert snapshot.projects == ("eva",)
    row = store._connection().execute(
        "SELECT revision FROM context_project_registry WHERE project='eva'"
    ).fetchone()
    assert row["revision"] == 1


def test_register_project_with_display_name(store, ps):
    """注册时可带中文显示名；重复注册不覆盖已有显示名。"""
    with store.transaction():
        ps.register_project("eva", "EVA 客服")
        ps.register_project("eva", "不该覆盖")
    row = store._connection().execute(
        "SELECT display_name, revision FROM context_project_registry "
        "WHERE project='eva'"
    ).fetchone()
    assert row["display_name"] == "EVA 客服"
    assert row["revision"] == 1


def test_set_display_name_cas(store, ps):
    with store.transaction():
        ps.register_project("eva")
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.set_display_name("eva", "X", expected_revision=99)
        assert excinfo.value.code == "revision_conflict"
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.set_display_name("ghost", "X", expected_revision=1)
        assert excinfo.value.code == "project_not_found"
        ps.set_display_name("eva", "EVA 客服", expected_revision=1)
    row = store._connection().execute(
        "SELECT display_name, revision FROM context_project_registry "
        "WHERE project='eva'"
    ).fetchone()
    assert row["display_name"] == "EVA 客服"
    assert row["revision"] == 2
    # 空串清除显示名
    with store.transaction():
        ps.set_display_name("eva", "", expected_revision=2)
    row = store._connection().execute(
        "SELECT display_name FROM context_project_registry WHERE project='eva'"
    ).fetchone()
    assert row["display_name"] == ""


def test_registry_display_name_column_added_to_old_db(test_config):
    """老库（注册表无 display_name 列）打开时被幂等补列。"""
    import sqlite3

    test_config.ensure_dirs()
    raw = sqlite3.connect(str(test_config.db_path))
    raw.execute(
        "CREATE TABLE context_project_registry("
        "project TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'active', "
        "revision INTEGER NOT NULL DEFAULT 1, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    raw.execute(
        "INSERT INTO context_project_registry VALUES ('eva', 'active', 3, 't', 't')"
    )
    raw.commit()
    raw.close()

    with ContextStore(test_config) as store:
        cols = {
            row["name"]
            for row in store._connection().execute(
                "PRAGMA table_info(context_project_registry)"
            )
        }
        assert "display_name" in cols
        row = store._connection().execute(
            "SELECT display_name, revision FROM context_project_registry "
            "WHERE project='eva'"
        ).fetchone()
        assert row["display_name"] == ""
        assert row["revision"] == 3  # 既有数据不受影响


def test_archive_project_cas_and_snapshot(store, ps):
    with store.transaction():
        ps.register_project("eva")
        ps.register_project("hermes")
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.archive_project("eva", expected_revision=99)
        assert excinfo.value.code == "revision_conflict"
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.archive_project("ghost", expected_revision=1)
        assert excinfo.value.code == "project_not_found"
        ps.archive_project("eva", expected_revision=1)
    snapshot = ps.snapshot()
    assert snapshot.projects == ("hermes",)


# ---- aliases ----


def test_alias_conflict_is_store_error(store, ps):
    with store.transaction():
        ps.register_project("eva")
        ps.register_project("hermes")
        ps.add_alias("evolv", "eva")
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.add_alias("evolv", "hermes")
        assert excinfo.value.code == "alias_conflict"
    assert ps.snapshot().aliases == (("evolv", "eva"),)


def test_add_alias_requires_registered_project(store, ps):
    with store.transaction():
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.add_alias("ghost", "missing")
        assert excinfo.value.code == "project_not_found"


def test_remove_alias_cas(store, ps):
    with store.transaction():
        ps.register_project("eva")
        ps.add_alias("evolv", "eva")
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.remove_alias("evolv", expected_revision=99)
        assert excinfo.value.code == "revision_conflict"
        ps.remove_alias("evolv", expected_revision=1)
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.remove_alias("evolv", expected_revision=1)
        assert excinfo.value.code == "revision_conflict"
    assert ps.snapshot().aliases == ()


# ---- bindings ----


def test_bind_workspace_creates_active_binding_and_focus_row(store, ps):
    with store.transaction():
        ps.register_project("eva")
        ps.bind_workspace(FP1, "eva", method="cli", make_default=True)
    snapshot = ps.snapshot()
    assert len(snapshot.bindings) == 1
    binding = snapshot.bindings[0]
    assert binding.workspace_fingerprint == FP1
    assert binding.project == "eva"
    assert binding.state == "active"
    assert binding.is_default is True
    focus = store._connection().execute(
        "SELECT workstream_id, revision FROM continuity_focus "
        "WHERE project='eva' AND workspace_fingerprint=?",
        (FP1,),
    ).fetchone()
    assert focus["workstream_id"] is None
    assert focus["revision"] == 0


def test_bind_workspace_requires_registered_project(store, ps):
    with store.transaction():
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.bind_workspace(FP1, "missing", method="cli", make_default=False)
        assert excinfo.value.code == "project_not_found"


def test_candidate_binding_promoted_to_active(store, ps):
    conn = store._connection()
    with store.transaction():
        ps.register_project("eva")
        conn.execute(
            "INSERT INTO context_project_workspace_bindings("
            "workspace_fingerprint, project, state, is_default, method,"
            " revision, created_at, updated_at"
            ") VALUES (?, 'eva', 'candidate', 0, 'observed', 1,"
            " '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
            (FP1,),
        )
        ps.bind_workspace(FP1, "eva", method="cli", make_default=True)
    snapshot = ps.snapshot()
    assert len(snapshot.bindings) == 1
    binding = snapshot.bindings[0]
    assert binding.state == "active"
    assert binding.is_default is True
    row = conn.execute(
        "SELECT revision, method FROM context_project_workspace_bindings "
        "WHERE workspace_fingerprint=?",
        (FP1,),
    ).fetchone()
    assert row["revision"] == 2
    assert row["method"] == "cli"


def test_second_active_default_binding_rejected(store):
    ps = ProjectStore(store._connection(), store._require_transaction, generic_names=())
    with store.transaction():
        ps.register_project("eva")
        ps.bind_workspace("hmac-sha256:" + "1" * 64, "eva", method="cli", make_default=True)
        ps.register_project("hermes")
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.bind_workspace("hmac-sha256:" + "1" * 64, "hermes", method="cli", make_default=True)
        assert excinfo.value.code == "default_binding_conflict"
    # The rejected statement must not roll back the owner's transaction:
    # eva's default binding commits; hermes has no binding row.
    snapshot = ps.snapshot()
    assert [
        (b.workspace_fingerprint, b.project, b.state, b.is_default)
        for b in snapshot.bindings
    ] == [("hmac-sha256:" + "1" * 64, "eva", "active", True)]


def test_set_default_binding(store, ps):
    with store.transaction():
        ps.register_project("eva")
        ps.register_project("hermes")
        ps.bind_workspace(FP1, "eva", method="cli", make_default=False)
        ps.bind_workspace(FP1, "hermes", method="cli", make_default=False)
        ps.set_default_binding(FP1, "hermes", expected_revision=1)
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.set_default_binding(FP1, "eva", expected_revision=1)
        assert excinfo.value.code == "default_binding_conflict"
    snapshot = ps.snapshot()
    defaults = [b.project for b in snapshot.bindings if b.is_default]
    assert defaults == ["hermes"]


def test_set_default_binding_revision_conflict_and_missing(store, ps):
    with store.transaction():
        ps.register_project("eva")
        ps.bind_workspace(FP1, "eva", method="cli", make_default=False)
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.set_default_binding(FP1, "eva", expected_revision=99)
        assert excinfo.value.code == "revision_conflict"
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.set_default_binding(FP2, "eva", expected_revision=1)
        assert excinfo.value.code == "binding_not_found"


def test_revoke_binding_updates_focus_row(store, ps):
    with store.transaction():
        ps.register_project("eva")
        ps.bind_workspace(FP1, "eva", method="cli", make_default=True)
        ps.revoke_binding(FP1, "eva", expected_revision=1)
    snapshot = ps.snapshot()
    binding = snapshot.bindings[0]
    assert binding.state == "revoked"
    assert binding.is_default is False
    focus = store._connection().execute(
        "SELECT workstream_id, revision FROM continuity_focus "
        "WHERE project='eva' AND workspace_fingerprint=?",
        (FP1,),
    ).fetchone()
    assert focus["workstream_id"] is None
    assert focus["revision"] == 1


def test_revoke_binding_revision_conflict_and_missing(store, ps):
    with store.transaction():
        ps.register_project("eva")
        ps.bind_workspace(FP1, "eva", method="cli", make_default=False)
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.revoke_binding(FP1, "eva", expected_revision=99)
        assert excinfo.value.code == "revision_conflict"
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.revoke_binding(FP2, "eva", expected_revision=1)
        assert excinfo.value.code == "binding_not_found"


# ---- resolutions ----


def test_record_resolution_three_states(store, ps):
    resolved_item = store.create_item(make_draft("fact:resolved"))
    conflict_item = store.create_item(make_draft("fact:conflict"))
    unresolved_item = store.create_item(make_draft("fact:unresolved"))
    with store.transaction():
        ps.record_resolution(
            resolved_item.id,
            ProjectResolutionDecision.resolved("eva", "strong", "v1", _evidence()),
        )
        ps.record_resolution(
            conflict_item.id, ProjectResolutionDecision.conflict("v1", _evidence())
        )
        ps.record_resolution(
            unresolved_item.id, ProjectResolutionDecision.unresolved("v1", ())
        )
    rows = {
        row["item_id"]: row
        for row in store._connection().execute("SELECT * FROM context_project_resolutions")
    }
    resolved = rows[resolved_item.id]
    assert resolved["resolution_state"] == "resolved"
    assert resolved["review_state"] == "not_required"
    assert resolved["decision_source"] == "automatic"
    assert resolved["resolved_project"] == "eva"
    assert resolved["confidence"] == "high"
    assert resolved["method"] == "strong"
    for item_id, state in (
        (conflict_item.id, "conflict"),
        (unresolved_item.id, "unresolved"),
    ):
        row = rows[item_id]
        assert row["resolution_state"] == state
        assert row["review_state"] == "pending"
        assert row["decision_source"] == "automatic"
        assert row["resolved_project"] == ""
    pending = ps.list_pending_resolutions()
    assert {row.item_id for row in pending} == {conflict_item.id, unresolved_item.id}


def test_record_resolution_upsert_increments_revision(store, ps):
    item = store.create_item(make_draft("fact:upsert"))
    with store.transaction():
        ps.record_resolution(item.id, ProjectResolutionDecision.unresolved("v1", ()))
        ps.record_resolution(item.id, ProjectResolutionDecision.conflict("v1", _evidence()))
    row = _resolution_row(store, item.id)
    assert row["revision"] == 2
    assert row["resolution_state"] == "conflict"
    assert row["review_state"] == "pending"
    assert row["evidence_json"] == (
        '[{"normalized_value":"eva","source":"tags",'
        '"source_version":"v1","type":"bare_tag"}]'
    )


def test_list_pending_resolutions_rows_and_limit(store, ps):
    items = [store.create_item(make_draft(f"fact:pending:{i}")) for i in range(3)]
    with store.transaction():
        for item in items:
            ps.record_resolution(item.id, ProjectResolutionDecision.conflict("v1", ()))
    rows = ps.list_pending_resolutions(limit=2)
    assert len(rows) == 2
    row = rows[0]
    assert isinstance(row, ProjectResolutionRow)
    assert row.item_id == items[0].id
    assert row.resolution_state == "conflict"
    assert row.decision_source == "automatic"
    assert row.review_state == "pending"
    assert row.proposed_project == ""
    assert row.resolved_project == ""
    assert row.confidence == "none"
    assert row.method == ""
    assert row.evidence_json == "[]"
    assert row.resolver_version == "v1"
    assert row.revision == 1
    assert row.reviewed_at is None
    assert row.created_at
    assert row.updated_at


def test_accept_resolution_updates_item_project(store, ps):
    item = store.create_item(make_draft("fact:accept"))
    with store.transaction():
        ps.register_project("eva")
        ps.record_resolution(item.id, ProjectResolutionDecision.conflict("v1", ()))
        ps.accept_resolution(item.id, "eva", expected_revision=1)
    assert store.get_item(item.id).project == "eva"
    row = _resolution_row(store, item.id)
    assert row["review_state"] == "accepted"
    assert row["decision_source"] == "human"
    assert row["resolved_project"] == "eva"
    assert row["revision"] == 2
    assert row["reviewed_at"] is not None


def test_accept_resolution_revision_conflict(store, ps):
    item = store.create_item(make_draft("fact:accept-conflict"))
    with store.transaction():
        ps.register_project("eva")
        ps.record_resolution(item.id, ProjectResolutionDecision.conflict("v1", ()))
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.accept_resolution(item.id, "eva", expected_revision=99)
        assert excinfo.value.code == "revision_conflict"
    assert store.get_item(item.id).project == ""


def test_accept_resolution_unknown_project_or_missing_row(store, ps):
    item = store.create_item(make_draft("fact:accept-missing"))
    with store.transaction():
        ps.register_project("eva")
        ps.record_resolution(item.id, ProjectResolutionDecision.conflict("v1", ()))
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.accept_resolution(item.id, "ghost", expected_revision=1)
        assert excinfo.value.code == "project_not_found"
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.accept_resolution(999999, "eva", expected_revision=1)
        assert excinfo.value.code == "resolution_not_found"


def test_reject_resolution_keeps_project_empty(store, ps):
    item = store.create_item(make_draft("fact:reject"))
    with store.transaction():
        ps.record_resolution(item.id, ProjectResolutionDecision.conflict("v1", ()))
        ps.reject_resolution(item.id, expected_revision=1)
    assert store.get_item(item.id).project == ""
    row = _resolution_row(store, item.id)
    assert row["review_state"] == "rejected"
    assert row["decision_source"] == "human"
    assert row["resolved_project"] == ""
    assert row["revision"] == 2
    assert row["reviewed_at"] is not None
    assert ps.list_pending_resolutions() == ()


def test_reject_resolution_revision_conflict_and_missing(store, ps):
    item = store.create_item(make_draft("fact:reject-missing"))
    with store.transaction():
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.reject_resolution(item.id, expected_revision=1)
        assert excinfo.value.code == "resolution_not_found"
        ps.record_resolution(item.id, ProjectResolutionDecision.conflict("v1", ()))
        with pytest.raises(ProjectStoreError) as excinfo:
            ps.reject_resolution(item.id, expected_revision=99)
        assert excinfo.value.code == "revision_conflict"


# ---- seeding and snapshot ----


def test_seed_from_config_second_run_changes_nothing(store, ps):
    with store.transaction():
        ps.seed_from_config({"evolv": "eva", "hrm": "hermes"})
    first = ps.snapshot()
    counts_before = _table_counts(store)
    with store.transaction():
        ps.seed_from_config({"evolv": "eva", "hrm": "hermes"})
    second = ps.snapshot()
    assert second == first
    assert _table_counts(store) == counts_before
    assert second.projects == ("eva", "hermes")
    assert second.aliases == (("evolv", "eva"), ("hrm", "hermes"))


def test_snapshot_revision_is_sum_of_per_table_max_revisions(store, ps):
    with store.transaction():
        ps.register_project("eva")
        ps.register_project("hermes")
        ps.add_alias("evolv", "eva")
        ps.bind_workspace(FP1, "eva", method="cli", make_default=False)
    snapshot = ps.snapshot()
    # max(registry)=1 + max(aliases)=1 + max(bindings)=1
    assert snapshot.revision == 3
    with store.transaction():
        ps.archive_project("hermes", expected_revision=1)
    # registry max rises to 2
    assert ps.snapshot().revision == 4
    with store.transaction():
        ps.remove_alias("evolv", expected_revision=1)
    # empty aliases table contributes 0
    assert ps.snapshot().revision == 3


def test_snapshot_carries_generic_names(ps):
    snapshot = ps.snapshot()
    assert snapshot.generic_names == ("home", "src")
    assert snapshot.projects == ()
    assert snapshot.aliases == ()
    assert snapshot.bindings == ()
    assert snapshot.revision == 0


# ---- transaction boundary ----


def test_all_write_methods_require_active_transaction(ps):
    decision = ProjectResolutionDecision.unresolved("v1", ())
    calls = [
        lambda: ps.register_project("eva"),
        lambda: ps.archive_project("eva", expected_revision=1),
        lambda: ps.add_alias("evolv", "eva"),
        lambda: ps.remove_alias("evolv", expected_revision=1),
        lambda: ps.bind_workspace(FP1, "eva", method="cli", make_default=False),
        lambda: ps.revoke_binding(FP1, "eva", expected_revision=1),
        lambda: ps.set_default_binding(FP1, "eva", expected_revision=1),
        lambda: ps.record_resolution(1, decision),
        lambda: ps.accept_resolution(1, "eva", expected_revision=1),
        lambda: ps.reject_resolution(1, expected_revision=1),
        lambda: ps.seed_from_config({"evolv": "eva"}),
    ]
    for call in calls:
        with pytest.raises(RuntimeError):
            call()

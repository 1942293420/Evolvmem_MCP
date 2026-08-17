"""Behavioral contracts for the independent layered-context SQLite store."""

from dataclasses import replace
import hashlib
import sqlite3

import pytest

from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayer,
    ContextLayers,
    ContextScope,
    ContextStatus,
    ContextTier,
    ContextValidationError,
)
from evolvmem.context_store import ContextStore


@pytest.fixture
def store(test_config):
    with ContextStore(test_config) as instance:
        yield instance


@pytest.fixture
def draft_factory():
    def make(
        identity_key: str = "project:test:fact:sample",
        *,
        l0: str = "Short fact",
        l1: str = "A useful fact with supporting detail.",
        l2: str = "The complete source material for the useful fact.",
        status: ContextStatus = ContextStatus.CANDIDATE,
        project: str = "test",
        scope: ContextScope = ContextScope.PROJECT,
        expires_at: str | None = None,
    ) -> ContextItemDraft:
        return ContextItemDraft(
            identity_key=identity_key,
            content_type=ContextContentType.FACT,
            layers=ContextLayers(l0=l0, l1=l1, l2=l2, generator="test-suite"),
            project=project,
            scope=scope,
            status=status,
            tier=ContextTier.NORMAL,
            tags=("python", "context"),
            importance=7.5,
            confidence=0.8,
            expires_at=expires_at,
        )

    return make


EXPECTED_COLUMNS = {
    "context_items": {
        "id", "identity_key", "content_type", "project", "scope", "status",
        "tier", "tags", "importance", "confidence", "source_state",
        "source_count", "success_count", "failure_count", "access_count",
        "last_accessed", "last_verified_at", "expires_at", "supersedes",
        "superseded_by", "created_at", "updated_at",
    },
    "context_layers": {
        "id", "item_id", "layer", "content", "content_hash", "generator",
        "created_at", "updated_at",
    },
    "context_sources": {
        "id", "item_id", "archive_id", "source_kind", "source_ref",
        "extraction_version", "created_at",
    },
    "context_evidence": {
        "id", "item_id", "source_id", "outcome", "note", "observed_at",
        "created_at",
    },
    "session_archives": {
        "id", "project", "adapter", "external_session_id", "payload_path",
        "payload_sha256", "state", "expires_at", "purged_at", "created_at",
    },
    "legacy_memory_migrations": {
        "legacy_memory_id", "context_item_id", "migrated_at",
    },
}


def test_initialize_is_idempotent_and_enables_sqlite_safety_pragmas(test_config):
    """Reinitialization must not fail or leave writes outside WAL/FK safeguards."""
    store = ContextStore(test_config)
    store.initialize()
    store.initialize()

    assert store._conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    store.close()


def test_initialize_creates_the_required_schema_and_active_identity_index(test_config):
    """A missing column or uniqueness index would break later migration phases."""
    with ContextStore(test_config):
        pass

    conn = sqlite3.connect(test_config.db_path)
    try:
        for table, expected in EXPECTED_COLUMNS.items():
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            assert columns == expected

        index_sql = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='index' AND name='idx_context_items_one_active_identity'"
        ).fetchone()[0]
        assert "identity_key, project, scope" in index_sql
        assert "WHERE status = 'active'" in index_sql
    finally:
        conn.close()


def test_initialize_does_not_modify_a_preexisting_legacy_memories_table(test_config):
    """Context schema setup must not migrate or rewrite the legacy store."""
    test_config.ensure_dirs()
    conn = sqlite3.connect(test_config.db_path)
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, sentinel TEXT NOT NULL)")
    conn.execute("INSERT INTO memories VALUES (7, 'legacy row')")
    before = list(conn.execute("PRAGMA table_info(memories)"))
    conn.commit()
    conn.close()

    with ContextStore(test_config):
        pass

    conn = sqlite3.connect(test_config.db_path)
    try:
        assert list(conn.execute("PRAGMA table_info(memories)")) == before
        assert conn.execute("SELECT * FROM memories").fetchall() == [(7, "legacy row")]
    finally:
        conn.close()


def test_create_item_atomically_persists_typed_item_and_exactly_three_layers(
    store, draft_factory
):
    """Dropping metadata, hashes, or one layer would make the stored item incomplete."""
    draft = draft_factory()

    item = store.create_item(draft)

    assert item.id == 1
    assert item.identity_key == draft.identity_key
    assert item.content_type is ContextContentType.FACT
    assert item.project == "test"
    assert item.scope is ContextScope.PROJECT
    assert item.status is ContextStatus.CANDIDATE
    assert item.tier is ContextTier.NORMAL
    assert item.tags == ("python", "context")
    assert item.importance == 7.5
    assert item.confidence == 0.8
    assert item.source_state == "none"
    assert item.source_count == item.success_count == item.failure_count == 0
    assert item.access_count == 0
    assert item.layers == draft.layers

    rows = store._conn.execute(
        "SELECT layer, content, content_hash, generator "
        "FROM context_layers WHERE item_id=? ORDER BY layer",
        (item.id,),
    ).fetchall()
    assert [(row["layer"], row["content"]) for row in rows] == [
        ("l0", draft.layers.l0),
        ("l1", draft.layers.l1),
        ("l2", draft.layers.l2),
    ]
    assert [row["content_hash"] for row in rows] == [
        hashlib.sha256(draft.layers.l0.encode()).hexdigest(),
        hashlib.sha256(draft.layers.l1.encode()).hexdigest(),
        hashlib.sha256(draft.layers.l2.encode()).hexdigest(),
    ]
    assert {row["generator"] for row in rows} == {"test-suite"}


@pytest.mark.parametrize("oversized_layer", ["l0", "l1", "l2"])
def test_create_item_rejects_each_over_budget_layer_without_writing_rows(
    store, draft_factory, oversized_layer
):
    """Removing store-boundary validation would persist an over-budget layer."""
    store.config.context_l0_max_chars = 8
    store.config.context_l1_max_chars = 8
    store.config.context_l2_max_chars = 8
    layer_values = {"l0": "short", "l1": "detail", "l2": "source"}
    layer_values[oversized_layer] = "x" * 9

    with pytest.raises(ContextValidationError, match=oversized_layer):
        store.create_item(draft_factory(**layer_values))

    assert store._conn.execute("SELECT COUNT(*) FROM context_items").fetchone()[0] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM context_layers").fetchone()[0] == 0


def test_lexical_tables_physically_contain_only_l0_and_l1_rows(store, draft_factory):
    """Even a raw FTS scan must not expose an L2 row as indexed content."""
    item = store.create_item(draft_factory())
    layer_ids = {
        row["layer"]: row["id"]
        for row in store._conn.execute(
            "SELECT id, layer FROM context_layers WHERE item_id=?", (item.id,)
        )
    }
    expected = {layer_ids["l0"], layer_ids["l1"]}

    assert {
        row[0] for row in store._conn.execute("SELECT rowid FROM context_layers_fts")
    } == expected
    if store._has_trigram:
        assert {
            row[0]
            for row in store._conn.execute("SELECT rowid FROM context_layers_fts_trigram")
        } == expected


def test_create_item_rolls_back_item_and_layers_when_layer_insert_fails(
    store, draft_factory, monkeypatch
):
    """A partial layer write must never leave an orphaned context item behind."""
    original = store._insert_layers

    def fail_after_layer_writes(item_id, layers, now):
        original(item_id, replace(layers, l1=layers.l0, l2=layers.l0), now)
        raise RuntimeError("synthetic layer failure")

    monkeypatch.setattr(store, "_insert_layers", fail_after_layer_writes)

    with pytest.raises(RuntimeError, match="synthetic layer failure"):
        store.create_item(draft_factory())

    assert store.get_by_identity("project:test:fact:sample", project="test") == []
    assert store._conn.execute("SELECT COUNT(*) FROM context_layers").fetchone()[0] == 0


def test_get_item_returns_complete_item_or_skips_layer_loading(store, draft_factory):
    """The metadata-only path must not issue a layer query or populate text."""
    created = store.create_item(draft_factory())
    statements: list[str] = []
    store._conn.set_trace_callback(statements.append)
    without_layers = store.get_item(created.id, include_layers=False)
    store._conn.set_trace_callback(None)

    assert without_layers is not None
    assert without_layers.layers is None
    assert not any("context_layers" in sql.lower() for sql in statements)
    assert store.get_item(created.id) == created
    assert store.get_item(9999) is None


def test_active_identity_is_unique_while_candidates_can_coexist(store, draft_factory):
    """Two active rows are ambiguous, but multiple candidates are valid evidence."""
    candidate = draft_factory()
    store.create_item(candidate)
    store.create_item(candidate)
    active = replace(candidate, status=ContextStatus.ACTIVE)
    store.create_item(active)

    with pytest.raises(sqlite3.IntegrityError):
        store.create_item(active)

    items = store.get_by_identity(candidate.identity_key, project="test")
    assert sum(item.status is ContextStatus.CANDIDATE for item in items) == 2
    assert sum(item.status is ContextStatus.ACTIVE for item in items) == 1


def test_active_identity_uniqueness_includes_project_and_scope(store, draft_factory):
    """The same identity is allowed in distinct project/scope namespaces."""
    base = draft_factory(status=ContextStatus.ACTIVE)

    first = store.create_item(base)
    other_project = store.create_item(replace(base, project="other"))
    global_item = store.create_item(replace(base, scope=ContextScope.GLOBAL))

    assert {first.id, other_project.id, global_item.id} == {1, 2, 3}


def test_supersede_active_sets_both_links_and_leaves_one_active(store, draft_factory):
    """Replacement must retain navigable history without a dual-active result."""
    old = store.create_item(draft_factory(status=ContextStatus.ACTIVE))
    successor_draft = draft_factory(
        status=ContextStatus.ACTIVE,
        l0="New summary",
        l1="New decision detail",
        l2="New complete source",
    )

    successor = store.supersede_active(successor_draft)

    old_after = store.get_item(old.id)
    assert old_after.status is ContextStatus.SUPERSEDED
    assert old_after.superseded_by == successor.id
    assert successor.status is ContextStatus.ACTIVE
    assert successor.supersedes == old.id
    history = store.get_by_identity(old.identity_key, project="test")
    assert [item.id for item in history if item.status is ContextStatus.ACTIVE] == [
        successor.id
    ]


@pytest.mark.parametrize("oversized_layer", ["l0", "l1", "l2"])
def test_supersede_active_rejects_each_over_budget_layer_without_changing_active_item(
    store, draft_factory, oversized_layer
):
    """Validating after demotion would corrupt the active history on bad input."""
    store.config.context_l0_max_chars = 8
    store.config.context_l1_max_chars = 8
    store.config.context_l2_max_chars = 8
    old = store.create_item(
        draft_factory(
            status=ContextStatus.ACTIVE,
            l0="old",
            l1="detail",
            l2="source",
        )
    )
    layer_values = {"l0": "new", "l1": "detail", "l2": "source"}
    layer_values[oversized_layer] = "x" * 9

    with pytest.raises(ContextValidationError, match=oversized_layer):
        store.supersede_active(
            draft_factory(status=ContextStatus.ACTIVE, **layer_values)
        )

    old_after = store.get_item(old.id)
    assert old_after is not None
    assert old_after.status is ContextStatus.ACTIVE
    assert old_after.superseded_by is None
    assert old_after.layers == old.layers
    assert store._conn.execute("SELECT COUNT(*) FROM context_items").fetchone()[0] == 1
    assert store._conn.execute("SELECT COUNT(*) FROM context_layers").fetchone()[0] == 3


def test_supersede_active_rolls_back_old_status_when_successor_insert_fails(
    store, draft_factory, monkeypatch
):
    """A failed successor must not make the prior active item disappear."""
    old = store.create_item(draft_factory(status=ContextStatus.ACTIVE))

    def fail_layers(item_id, layers, now):
        raise RuntimeError("synthetic successor failure")

    monkeypatch.setattr(store, "_insert_layers", fail_layers)
    with pytest.raises(RuntimeError, match="synthetic successor failure"):
        store.supersede_active(
            draft_factory(
                status=ContextStatus.ACTIVE,
                l0="Replacement",
                l1="Replacement detail",
                l2="Replacement source",
            )
        )

    old_after = store.get_item(old.id)
    assert old_after.status is ContextStatus.ACTIVE
    assert old_after.superseded_by is None
    assert len(store.get_by_identity(old.identity_key, project="test")) == 1


def test_supersede_active_without_predecessor_creates_the_supplied_draft(
    store, draft_factory
):
    """First-time promotion must work without manufacturing a history link."""
    created = store.supersede_active(draft_factory(status=ContextStatus.ACTIVE))

    assert created.status is ContextStatus.ACTIVE
    assert created.supersedes is None
    assert created.superseded_by is None


def test_search_fts_finds_l0_and_l1_but_never_l2(store, draft_factory):
    """Lexical search must identify matched render layers without indexing raw L2."""
    l0_item = store.create_item(
        draft_factory(
            "project:test:fact:l0",
            l0="alphaonly summary",
            l1="ordinary supporting detail",
            l2="ordinary complete source",
            status=ContextStatus.ACTIVE,
        )
    )
    l1_item = store.create_item(
        draft_factory(
            "project:test:fact:l1",
            l0="ordinary summary",
            l1="betaonly supporting detail",
            l2="ordinary complete source",
            status=ContextStatus.CANDIDATE,
        )
    )
    store.create_item(
        draft_factory(
            "project:test:fact:l2",
            l0="ordinary summary",
            l1="ordinary supporting detail",
            l2="gammaonly complete source",
            status=ContextStatus.ACTIVE,
        )
    )

    l0_hits = store.search_fts("alphaonly")
    l1_hits = store.search_fts("betaonly")

    assert [(hit.item_id, hit.match_layers) for hit in l0_hits] == [
        (l0_item.id, (ContextLayer.L0,))
    ]
    assert [(hit.item_id, hit.match_layers) for hit in l1_hits] == [
        (l1_item.id, (ContextLayer.L1,))
    ]
    assert store.search_fts("gammaonly") == []
    assert store.search_fts("ordinary", statuses=(ContextStatus.ACTIVE,))
    assert all(
        hit.status is ContextStatus.ACTIVE
        for hit in store.search_fts("ordinary", statuses=(ContextStatus.ACTIVE,))
    )


def test_search_fts_keeps_all_matched_layers_after_item_ranking(store, draft_factory):
    """A crowded row ranking must not truncate a selected item's weaker layer."""
    target = store.create_item(
        draft_factory(
            "project:test:fact:target",
            l0="rankingneedle",
            l1="rankingneedle " + "padding " * 80,
            l2="complete source",
            status=ContextStatus.ACTIVE,
        )
    )
    store.create_item(
        draft_factory(
            "project:test:fact:competitor",
            l0="rankingneedle " + "padding " * 10,
            l1="unrelated detail",
            l2="complete source",
            status=ContextStatus.ACTIVE,
        )
    )

    hits = store.search_fts("rankingneedle", top_k=1)

    assert len(hits) == 1
    assert hits[0].item_id == target.id
    assert hits[0].match_layers == (ContextLayer.L0, ContextLayer.L1)


def test_fts_triggers_follow_direct_layer_updates_and_deletes(store, draft_factory):
    """Updating or deleting indexed layers must not leave stale lexical entries."""
    item = store.create_item(
        draft_factory(
            l0="originaltoken summary",
            l1="ordinary supporting detail",
            l2="ordinary complete source",
            status=ContextStatus.ACTIVE,
        )
    )
    l0_id = store._conn.execute(
        "SELECT id FROM context_layers WHERE item_id=? AND layer='l0'", (item.id,)
    ).fetchone()[0]

    store._conn.execute(
        "UPDATE context_layers SET content='replacementtoken summary' WHERE id=?",
        (l0_id,),
    )

    assert store.search_fts("originaltoken") == []
    assert [hit.item_id for hit in store.search_fts("replacementtoken")] == [item.id]

    store._conn.execute("DELETE FROM context_layers WHERE id=?", (l0_id,))
    assert store.search_fts("replacementtoken") == []


def test_search_fts_deduplicates_cjk_trigram_and_like_hits(store, draft_factory):
    """The trigram supplement must merge one item while retaining all matched layers."""
    item = store.create_item(
        draft_factory(
            l0="破损商品退款规则",
            l1="破损商品可以直接退款，不再补发。",
            l2="原始会话没有检索价值。",
            status=ContextStatus.ACTIVE,
        )
    )

    hits = store.search_fts("破损商品")

    assert [hit.item_id for hit in hits] == [item.id]
    assert hits[0].match_layers == (ContextLayer.L0, ContextLayer.L1)


def test_search_fts_uses_like_for_short_cjk_when_trigram_is_absent(
    store, draft_factory
):
    """Short CJK substrings must remain searchable without trigram support."""
    item = store.create_item(
        draft_factory(
            l0="售后规则",
            l1="破损商品可以直接退款。",
            l2="完整原文",
            status=ContextStatus.ACTIVE,
        )
    )
    store._has_trigram = False

    hits = store.search_fts("退款")

    assert [(hit.item_id, hit.match_layers) for hit in hits] == [
        (item.id, (ContextLayer.L1,))
    ]


def test_search_fts_sanitizes_special_syntax_and_honors_empty_status_filter(
    store, draft_factory
):
    """Caller punctuation must not become executable FTS syntax or bypass filters."""
    store.create_item(
        draft_factory(
            l0='literal OR "quoted" token',
            l1="safe detail",
            l2="safe source",
            status=ContextStatus.ACTIVE,
        )
    )

    assert store.search_fts('OR "quoted"')
    assert store.search_fts("quoted", statuses=()) == []


def test_list_vector_documents_returns_only_active_unexpired_l0(
    store, draft_factory
):
    """Vector synchronization must not embed history, candidates, expiry, or L1/L2."""
    active = store.create_item(
        draft_factory(
            "project:test:fact:active",
            l0="active l0",
            l1="active l1",
            l2="active l2",
            status=ContextStatus.ACTIVE,
        )
    )
    future = store.create_item(
        draft_factory(
            "project:test:fact:future",
            l0="future l0",
            status=ContextStatus.ACTIVE,
            expires_at="2999-01-01 00:00:00",
        )
    )
    store.create_item(
        draft_factory(
            "project:test:fact:expired",
            l0="expired l0",
            status=ContextStatus.ACTIVE,
            expires_at="2000-01-01 00:00:00",
        )
    )
    for status in (
        ContextStatus.CANDIDATE,
        ContextStatus.ARCHIVED,
        ContextStatus.DELETED,
        ContextStatus.SUPERSEDED,
    ):
        store.create_item(
            draft_factory(
                f"project:test:fact:{status.value}",
                l0=f"{status.value} l0",
                status=status,
            )
        )

    documents = store.list_vector_documents()

    assert [(doc.item_id, doc.l0) for doc in documents] == [
        (active.id, "active l0"),
        (future.id, "future l0"),
    ]


def test_update_access_batches_ids_without_changing_updated_at(store, draft_factory):
    """Retrieval telemetry must update all hits but not masquerade as content edits."""
    first = store.create_item(draft_factory("project:test:fact:first"))
    second = store.create_item(draft_factory("project:test:fact:second"))
    before = {item.id: item.updated_at for item in (first, second)}

    store.update_access([first.id, second.id])
    store.update_access([])

    for item_id in (first.id, second.id):
        item = store.get_item(item_id, include_layers=False)
        assert item.access_count == 1
        assert item.last_accessed is not None
        assert item.updated_at == before[item_id]


def test_count_by_status_reports_persisted_item_counts(store, draft_factory):
    """Status summaries must count every persisted state without expiry rewriting it."""
    store.create_item(draft_factory("one", status=ContextStatus.CANDIDATE))
    store.create_item(draft_factory("two", status=ContextStatus.CANDIDATE))
    store.create_item(draft_factory("three", status=ContextStatus.ACTIVE))
    store.create_item(draft_factory("four", status=ContextStatus.DELETED))

    assert store.count_by_status() == {"active": 1, "candidate": 2, "deleted": 1}


def test_nested_transaction_rolls_back_all_context_writes(store, draft_factory):
    """Public operations must join the outer transaction's atomic boundary."""
    with pytest.raises(RuntimeError, match="synthetic outer failure"):
        with store.transaction():
            store.create_item(draft_factory("project:test:fact:first"))
            with store.transaction():
                store.create_item(draft_factory("project:test:fact:second"))
            raise RuntimeError("synthetic outer failure")

    assert store.get_by_identity("project:test:fact:first", project="test") == []
    assert store.get_by_identity("project:test:fact:second", project="test") == []

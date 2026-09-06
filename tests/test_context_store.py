"""Behavioral contracts for the independent layered-context SQLite store."""

from dataclasses import replace
import hashlib
import inspect
import sqlite3

import pytest

import evolvmem.context_store
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
from evolvmem.legacy_projection import (
    LegacyProjectionInsert,
    LegacyProjectionUpdate,
)
from evolvmem.memory_store import MemoryStore


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
        "superseded_by", "created_at", "updated_at", "experience_payload",
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
        "created_at", "event_key", "task_id", "verification_level",
        "conditions_json", "revision", "experience_version",
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
        event_index = conn.execute("SELECT sql FROM sqlite_master WHERE name='idx_experience_event'").fetchone()[0]
        assert "item_id,event_key,revision" in event_index.replace(' ', '')
        assert "WHERE event_key != ''" in event_index
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


def test_upgrade_preserves_old_evidence_and_adds_revision_defaults(test_config, draft_factory):
    with ContextStore(test_config) as store:
        item = store.create_item(draft_factory())
        with store.transaction():
            store.insert_evidence(item.id, None, 'success', 'old note', '2026-09-01 00:00:00')
    conn = sqlite3.connect(test_config.db_path)
    conn.execute('DROP INDEX idx_experience_event')
    conn.execute('ALTER TABLE context_items DROP COLUMN experience_payload')
    for name in ('event_key','task_id','verification_level','conditions_json','revision','experience_version'):
        conn.execute(f'ALTER TABLE context_evidence DROP COLUMN {name}')
    conn.commit()
    conn.close()
    with ContextStore(test_config) as store:
        row = store.list_evidence(item.id)[0]
        assert row['note'] == 'old note'
        assert row['event_key'] == '' and row['revision'] == 1
        assert row['conditions_json'] == '{}'
        store._connection().execute(
            "INSERT INTO context_evidence(item_id,outcome,note,observed_at,created_at,event_key,revision) VALUES(?,?,?,?,?,?,?)",
            (item.id,'success','new note','2026-09-05','2026-09-05','event-1',1))
        with pytest.raises(sqlite3.IntegrityError):
            store._connection().execute(
                "INSERT INTO context_evidence(item_id,outcome,note,observed_at,created_at,event_key,revision) VALUES(?,?,?,?,?,?,?)",
                (item.id,'success','duplicate','2026-09-05','2026-09-05','event-1',1))


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


def test_search_fts_layers_filter_excludes_l1_only_matches(store, draft_factory):
    """L0-only candidate generation must never be fed by an L1-only match."""
    l1_only = store.create_item(
        draft_factory(
            "project:test:fact:l1-only",
            l0="ordinary summary",
            l1="layeroneterm supporting detail",
            l2="ordinary complete source",
            status=ContextStatus.ACTIVE,
        )
    )
    both = store.create_item(
        draft_factory(
            "project:test:fact:both",
            l0="layeroneterm summary",
            l1="layeroneterm supporting detail",
            l2="ordinary complete source",
            status=ContextStatus.ACTIVE,
        )
    )

    default_hits = store.search_fts("layeroneterm")
    l0_hits = store.search_fts("layeroneterm", layers=(ContextLayer.L0,))

    assert {hit.item_id for hit in default_hits} == {l1_only.id, both.id}
    assert [(hit.item_id, hit.match_layers) for hit in l0_hits] == [
        (both.id, (ContextLayer.L0,))
    ]


def test_search_fts_empty_layers_returns_nothing_without_changing_the_default(
    store, draft_factory
):
    """An empty layer set is an explicit no-result request, not a default."""
    store.create_item(
        draft_factory(l0="emptylayertoken summary", status=ContextStatus.ACTIVE)
    )

    assert store.search_fts("emptylayertoken", layers=()) == []
    assert store.search_fts("emptylayertoken")


def test_search_fts_layers_filter_applies_to_the_cjk_like_fallback(
    store, draft_factory
):
    """The LIKE path must honor the same layer condition as the FTS5 path."""
    item = store.create_item(
        draft_factory(
            l0="售后规则",
            l1="破损商品可以直接退款，不再补发。",
            l2="完整原文",
            status=ContextStatus.ACTIVE,
        )
    )
    store._has_trigram = False

    assert store.search_fts("退款", layers=(ContextLayer.L0,)) == []
    assert [
        (hit.item_id, hit.match_layers)
        for hit in store.search_fts("退款", layers=(ContextLayer.L1,))
    ] == [(item.id, (ContextLayer.L1,))]
    assert [hit.item_id for hit in store.search_fts("退款")] == [item.id]


def test_get_retrieval_records_returns_metadata_l0_and_layers_without_l1_l2(
    store, draft_factory
):
    """Candidate metadata reads must not pay for undisclosed L1/L2 text."""
    first = store.create_item(draft_factory("project:test:fact:first", l0="first l0"))
    second = store.create_item(draft_factory("project:test:fact:second", l0="second l0"))

    statements: list[str] = []
    store._conn.set_trace_callback(statements.append)
    records = store.get_retrieval_records([second.id, 9999, first.id])
    store._conn.set_trace_callback(None)

    assert [record.item.id for record in records] == [second.id, first.id]
    assert [record.l0 for record in records] == ["second l0", "first l0"]
    for record in records:
        assert record.item.layers is None
        assert record.available_layers == (
            ContextLayer.L0, ContextLayer.L1, ContextLayer.L2,
        )
    assert record.item.identity_key == first.identity_key
    assert record.item.status is ContextStatus.CANDIDATE
    assert len(statements) == 2
    assert not any("layer='l1'" in sql or "layer='l2'" in sql for sql in statements)

    assert store.get_retrieval_records([]) == ()


def test_get_layer_returns_only_the_requested_exact_layer(store, draft_factory):
    """Exact disclosure must return the addressed layer and nothing else."""
    item = store.create_item(draft_factory())

    assert store.get_layer(item.id, ContextLayer.L0) == item.layers.l0
    assert store.get_layer(item.id, ContextLayer.L1) == item.layers.l1
    assert store.get_layer(item.id, ContextLayer.L2) == item.layers.l2
    assert store.get_layer(9999, ContextLayer.L1) is None


def test_list_pinned_policy_records_applies_seed_filters_without_l1_l2(
    store, draft_factory
):
    """Only active, confident, unexpired pinned policy records may seed injection."""

    def pinned(identity, content_type, **kwargs):
        return replace(
            draft_factory(identity, status=ContextStatus.ACTIVE, **kwargs),
            content_type=content_type,
            tier=ContextTier.PINNED,
        )

    seed_project = store.create_item(
        pinned("project:test:workflow_policy:seed", ContextContentType.WORKFLOW_POLICY)
    )
    seed_global = store.create_item(
        pinned(
            "global:constraint:seed",
            ContextContentType.CONSTRAINT,
            scope=ContextScope.GLOBAL,
        )
    )
    seed_global_preference = store.create_item(
        pinned(
            "global:preference:seed",
            ContextContentType.PREFERENCE,
            scope=ContextScope.GLOBAL,
        )
    )
    store.create_item(  # pinned but not a policy type
        pinned("project:test:fact:pinned", ContextContentType.FACT)
    )
    store.create_item(  # policy type but not pinned
        replace(
            draft_factory("project:test:workflow_policy:normal", status=ContextStatus.ACTIVE),
            content_type=ContextContentType.WORKFLOW_POLICY,
        )
    )
    store.create_item(  # pinned policy but still a candidate
        replace(
            pinned("project:test:workflow_policy:candidate", ContextContentType.WORKFLOW_POLICY),
            status=ContextStatus.CANDIDATE,
        )
    )
    store.create_item(  # expired
        pinned(
            "project:test:workflow_policy:expired",
            ContextContentType.WORKFLOW_POLICY,
            expires_at="2000-01-01 00:00:00",
        )
    )
    store.create_item(  # below the confidence gate
        replace(
            pinned("project:test:workflow_policy:weak", ContextContentType.WORKFLOW_POLICY),
            confidence=0.3,
        )
    )
    store.create_item(  # pinned policy of another project
        pinned(
            "project:other:workflow_policy:seed",
            ContextContentType.WORKFLOW_POLICY,
            project="other",
        )
    )
    store.create_item(  # global but not an applicable policy type
        pinned(
            "global:session_summary:pinned",
            ContextContentType.SESSION_SUMMARY,
            scope=ContextScope.GLOBAL,
        )
    )

    records = store.list_pinned_policy_records(project="test", min_confidence=0.55)

    assert [record.item.id for record in records] == [
        seed_project.id, seed_global.id, seed_global_preference.id,
    ]
    for record in records:
        assert record.item.layers is None
        assert record.item.tier is ContextTier.PINNED
        assert record.l0
        assert record.available_layers == (
            ContextLayer.L0, ContextLayer.L1, ContextLayer.L2,
        )


def test_initialize_without_schema_creation_requires_an_existing_database(
    test_config,
):
    """create_schema=False must fail explicitly instead of creating an empty file."""
    store = ContextStore(test_config)

    with pytest.raises(sqlite3.OperationalError):
        store.initialize(create_schema=False)

    assert store._conn is None
    assert not test_config.db_path.exists()
    assert not (test_config.data_dir / "models").exists()


def test_initialize_without_schema_creation_leaves_context_schema_absent(test_config):
    """Opening an existing database must not create any Context table or trigger."""
    with MemoryStore(test_config) as legacy:
        legacy.add(key="fact:existing", value="A pre-existing legacy row.")

    store = ContextStore(test_config)
    store.initialize(create_schema=False)
    try:
        names = {
            row[0]
            for row in store._conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'trigger')"
            )
        }
    finally:
        store.close()

    assert "memories" in names
    assert "legacy_memory_migrations" not in names
    assert not any(name.startswith("context") for name in names)


def test_create_schema_in_transaction_requires_an_active_transaction(store):
    """Schema DDL must join an outer transaction rather than autocommitting."""
    with pytest.raises(RuntimeError, match="transaction"):
        store.create_schema_in_transaction()


def test_create_schema_in_transaction_is_idempotent(store, draft_factory):
    """Repeated in-transaction schema creation keeps the Foundation schema stable."""
    with store.transaction():
        store.create_schema_in_transaction()
    with store.transaction():
        store.create_schema_in_transaction()

    item = store.create_item(draft_factory())

    assert item.id == 1
    assert store.count_by_status() == {"candidate": 1}


def test_context_schema_creation_never_uses_executescript():
    """executescript implicitly commits, which would break atomic bootstrap."""
    source = inspect.getsource(evolvmem.context_store)
    assert "executescript" not in source


def test_legacy_projection_shares_the_context_connection_and_guard(test_config):
    """Projection writes must join the Context transaction, never commit early."""
    with MemoryStore(test_config) as legacy:
        legacy.add(key="fact:shared", value="Existing legacy row.")

    with ContextStore(test_config) as store:
        repo = store.legacy_projection()
        with pytest.raises(RuntimeError, match="transaction"):
            repo.insert(LegacyProjectionInsert(key="fact:uncommitted", value="v"))

        with store.transaction():
            legacy_id = repo.insert(
                LegacyProjectionInsert(key="fact:committed", value="New row.")
            )
            other = sqlite3.connect(test_config.db_path)
            try:
                assert other.execute(
                    "SELECT COUNT(*) FROM memories WHERE key='fact:committed'"
                ).fetchone()[0] == 0
            finally:
                other.close()

        assert repo.get_by_id(legacy_id)["value"] == "New row."


def test_legacy_projection_rolls_back_with_the_context_transaction(test_config):
    """A failed outer transaction must revert projection writes too."""
    with MemoryStore(test_config) as legacy:
        seeded = legacy.add(key="fact:rollback", value="Seeded row.")

    with ContextStore(test_config) as store:
        repo = store.legacy_projection()
        with pytest.raises(RuntimeError, match="synthetic outer failure"):
            with store.transaction():
                repo.insert(LegacyProjectionInsert(key="fact:new", value="v"))
                repo.soft_delete(seeded)
                raise RuntimeError("synthetic outer failure")

        assert repo.get_by_key("fact:new") == []
        assert repo.get_by_id(seeded)["status"] == "active"


def test_context_and_projection_writes_share_one_rollback(test_config, draft_factory):
    """The cutover invariant: both stores commit or revert as one transaction."""
    with MemoryStore(test_config):
        pass

    with ContextStore(test_config) as store:
        repo = store.legacy_projection()
        with pytest.raises(RuntimeError, match="synthetic failure"):
            with store.transaction():
                store.create_item(draft_factory())
                repo.insert(LegacyProjectionInsert(key="fact:paired", value="v"))
                raise RuntimeError("synthetic failure")

        assert store.count_by_status() == {}
        assert repo.get_by_key("fact:paired") == []


def test_supersede_item_uses_the_exact_mapped_predecessor(store, draft_factory):
    """Supersession must target the given ID, never an identity match."""
    predecessor = store.create_item(
        draft_factory("project:test:fact:old-identity", status=ContextStatus.ACTIVE)
    )
    decoy = store.create_item(
        draft_factory("project:test:fact:new-identity", status=ContextStatus.CANDIDATE)
    )

    with store.transaction():
        successor = store.supersede_item(
            predecessor.id,
            draft_factory("project:test:fact:new-identity"),
        )

    old = store.get_item(predecessor.id)
    assert old.status is ContextStatus.SUPERSEDED
    assert old.superseded_by == successor.id
    assert successor.status is ContextStatus.ACTIVE
    assert successor.supersedes == predecessor.id
    untouched = store.get_item(decoy.id)
    assert untouched.status is ContextStatus.CANDIDATE
    assert untouched.supersedes is None
    assert untouched.superseded_by is None


def test_supersede_item_requires_a_transaction_and_an_existing_predecessor(
    store, draft_factory
):
    """The primitive joins an outer transaction and fails loudly on bad IDs."""
    with pytest.raises(RuntimeError, match="transaction"):
        store.supersede_item(1, draft_factory())

    with store.transaction():
        with pytest.raises(ValueError, match="does not exist"):
            store.supersede_item(9999, draft_factory())

    assert store.count_by_status() == {}


def test_supersede_item_validates_layers_before_touching_the_predecessor(
    store, draft_factory
):
    """An invalid successor must leave the predecessor's status and links intact."""
    store.config.context_l0_max_chars = 8
    predecessor = store.create_item(
        draft_factory(status=ContextStatus.ACTIVE, l0="old", l1="detail", l2="source")
    )

    with store.transaction():
        with pytest.raises(ContextValidationError, match="l0"):
            store.supersede_item(
                predecessor.id,
                draft_factory(l0="x" * 9, l1="detail", l2="source"),
            )

    old = store.get_item(predecessor.id)
    assert old.status is ContextStatus.ACTIVE
    assert old.superseded_by is None


def test_set_item_status_requires_transaction_and_updates_only_the_target(
    store, draft_factory
):
    """Status changes are transaction-bound and addressed by exact item ID."""
    first = store.create_item(draft_factory("project:test:fact:one"))
    second = store.create_item(draft_factory("project:test:fact:two"))

    with pytest.raises(RuntimeError, match="transaction"):
        store.set_item_status(first.id, ContextStatus.ARCHIVED)

    with store.transaction():
        assert store.set_item_status(first.id, ContextStatus.ARCHIVED) is True
        assert store.set_item_status(9999, ContextStatus.ARCHIVED) is False

    assert store.get_item(first.id, include_layers=False).status is ContextStatus.ARCHIVED
    assert store.get_item(second.id, include_layers=False).status is ContextStatus.CANDIDATE


def test_update_item_from_legacy_mirrors_only_supplied_metadata(store, draft_factory):
    """Legacy metadata edits map onto the exact Context item, not the legacy ID."""
    item = store.create_item(draft_factory())

    with pytest.raises(RuntimeError, match="transaction"):
        store.update_item_from_legacy(item.id, LegacyProjectionUpdate(1, importance=8.0))

    with store.transaction():
        assert (
            store.update_item_from_legacy(
                item.id, LegacyProjectionUpdate(41, importance=8.5, tier="pinned")
            )
            is True
        )
        assert (
            store.update_item_from_legacy(9999, LegacyProjectionUpdate(41, importance=1.0))
            is False
        )

    updated = store.get_item(item.id, include_layers=False)
    assert updated.importance == 8.5
    assert updated.tier is ContextTier.PINNED


def test_hard_delete_item_removes_the_mapping_before_the_item(store, draft_factory):
    """The mapping must go first: the item is a foreign-key target of it."""
    item = store.create_item(draft_factory())
    with store.transaction():
        store.record_legacy_mapping(41, item.id)

    with pytest.raises(RuntimeError, match="transaction"):
        store.hard_delete_item(item.id)

    with store.transaction():
        assert store.hard_delete_item(item.id) is True
        assert store.hard_delete_item(9999) is False

    assert store.get_item(item.id) is None
    assert store.resolve_legacy_mapping(41) is None
    assert store._conn.execute(
        "SELECT COUNT(*) FROM context_layers WHERE item_id=?", (item.id,)
    ).fetchone()[0] == 0


def test_delete_legacy_mapping_requires_transaction_and_reports_existence(
    store, draft_factory
):
    """Mapping deletion is a transaction-bound primitive that leaves the item."""
    item = store.create_item(draft_factory())
    with store.transaction():
        store.record_legacy_mapping(41, item.id)

    with pytest.raises(RuntimeError, match="transaction"):
        store.delete_legacy_mapping(41)

    with store.transaction():
        assert store.delete_legacy_mapping(41) is True
        assert store.delete_legacy_mapping(41) is False

    assert store.resolve_legacy_mapping(41) is None
    assert store.get_item(item.id) is not None


# ---- session archive SQL primitives (Phase 3 P1) ----


def _archive_row(store, archive_id: int) -> dict | None:
    row = store._connection().execute(
        "SELECT * FROM session_archives WHERE id=?", (archive_id,)
    ).fetchone()
    return None if row is None else dict(row)


def test_upsert_session_archive_inserts_then_updates_in_place(store):
    """UNIQUE(adapter, external_session_id) makes re-archiving idempotent."""
    with store.transaction():
        archive_id = store.upsert_session_archive(
            "proj",
            "codex",
            "sess-1",
            payload_path="session_archives/aaa.bin",
            payload_sha256="a" * 64,
            expires_at="2026-01-31 00:00:00",
            recorded_at="2026-01-01 00:00:00",
        )
    row = _archive_row(store, archive_id)
    assert row["project"] == "proj"
    assert row["state"] == "available"
    assert row["purged_at"] is None
    assert row["created_at"] == "2026-01-01 00:00:00"

    with store.transaction():
        again = store.upsert_session_archive(
            "proj-renamed",
            "codex",
            "sess-1",
            payload_path="session_archives/bbb.bin",
            payload_sha256="b" * 64,
            expires_at="2026-02-10 00:00:00",
            recorded_at="2026-01-11 00:00:00",
        )
    assert again == archive_id
    row = _archive_row(store, archive_id)
    assert row["project"] == "proj-renamed"
    assert row["payload_path"] == "session_archives/bbb.bin"
    assert row["payload_sha256"] == "b" * 64
    assert row["expires_at"] == "2026-02-10 00:00:00"
    assert row["state"] == "available"
    assert row["created_at"] == "2026-01-01 00:00:00"  # original insert time kept
    assert store._connection().execute(
        "SELECT COUNT(*) FROM session_archives"
    ).fetchone()[0] == 1


def test_session_archive_primitives_require_transaction(store):
    with pytest.raises(RuntimeError, match="transaction"):
        store.upsert_session_archive(
            "proj",
            "codex",
            "sess-1",
            payload_path="session_archives/aaa.bin",
            payload_sha256="a" * 64,
            expires_at="2026-01-31 00:00:00",
            recorded_at="2026-01-01 00:00:00",
        )
    with pytest.raises(RuntimeError, match="transaction"):
        store.mark_session_archive_purged(1, purged_at="2026-02-01 00:00:00")
    with pytest.raises(RuntimeError, match="transaction"):
        store.record_session_source(1, 1, extraction_version="v1")
    with pytest.raises(RuntimeError, match="transaction"):
        store.recompute_source_states([1])


def test_list_expired_and_project_archives_filter_by_state(store):
    with store.transaction():
        expired_id = store.upsert_session_archive(
            "alpha", "codex", "sess-old",
            payload_path="session_archives/old.bin",
            payload_sha256="c" * 64,
            expires_at="2026-01-31 00:00:00",
            recorded_at="2026-01-01 00:00:00",
        )
        fresh_id = store.upsert_session_archive(
            "alpha", "codex", "sess-new",
            payload_path="session_archives/new.bin",
            payload_sha256="d" * 64,
            expires_at="2026-02-10 00:00:00",
            recorded_at="2026-01-11 00:00:00",
        )
        other_project_id = store.upsert_session_archive(
            "beta", "codex", "sess-other",
            payload_path="session_archives/other.bin",
            payload_sha256="e" * 64,
            expires_at="2026-01-31 00:00:00",
            recorded_at="2026-01-01 00:00:00",
        )

    expired = store.list_expired_session_archives("2026-01-31 00:00:00")
    assert [row["id"] for row in expired] == [expired_id, other_project_id]
    assert store.list_expired_session_archives("2026-01-30 23:59:59") == []

    alpha = store.list_available_project_archives("alpha")
    assert [row["id"] for row in alpha] == [expired_id, fresh_id]

    with store.transaction():
        assert store.mark_session_archive_purged(
            expired_id, purged_at="2026-02-01 00:00:00"
        ) is True
        # Already purged: a repeated mark must not report success.
        assert store.mark_session_archive_purged(
            expired_id, purged_at="2026-02-02 00:00:00"
        ) is False

    remaining = store.list_expired_session_archives("2026-02-03 00:00:00")
    assert [row["id"] for row in remaining] == [other_project_id]
    assert [row["id"] for row in store.list_available_project_archives("alpha")] == [
        fresh_id
    ]
    row = _archive_row(store, expired_id)
    assert row["state"] == "purged"
    assert row["purged_at"] == "2026-02-01 00:00:00"


def test_record_session_source_and_recompute_source_states(store, draft_factory):
    item = store.create_item(draft_factory("project:test:fact:sourced"))
    loner = store.create_item(draft_factory("project:test:fact:loner"))
    with store.transaction():
        archive_id = store.upsert_session_archive(
            "proj", "codex", "sess-src",
            payload_path="session_archives/src.bin",
            payload_sha256="f" * 64,
            expires_at="2026-01-31 00:00:00",
            recorded_at="2026-01-01 00:00:00",
        )
        store.record_session_source(item.id, archive_id, extraction_version="v1")

    assert store.get_item(item.id).source_state == "available"
    assert store.get_item(item.id).source_count == 1
    assert store.get_item(loner.id).source_state == "none"

    sources = store.list_archive_sources(archive_id)
    assert [row["item_id"] for row in sources] == [item.id]

    with store.transaction():
        store.mark_session_archive_purged(archive_id, purged_at="2026-02-01 00:00:00")
        store.recompute_source_states([item.id, loner.id])

    assert store.get_item(item.id).source_state == "purged"
    # Items without archive-backed sources are never touched.
    assert store.get_item(loner.id).source_state == "none"


# ---- evidence and lifecycle primitives ----


def test_insert_and_list_evidence_round_trip(store, draft_factory):
    item = store.create_item(draft_factory("project:test:fact:evidenced"))
    with store.transaction():
        first_id = store.insert_evidence(
            item.id, None, "success", "复用成功", "2026-01-02 00:00:00"
        )
        second_id = store.insert_evidence(
            item.id, None, "failure", "", "2026-01-03 00:00:00"
        )

    assert second_id > first_id
    rows = store.list_evidence(item.id)
    assert [row["id"] for row in rows] == [first_id, second_id]
    assert [row["outcome"] for row in rows] == ["success", "failure"]
    assert rows[0]["note"] == "复用成功"
    assert rows[0]["source_id"] is None
    assert rows[0]["observed_at"] == "2026-01-02 00:00:00"
    assert rows[0]["created_at"]
    assert store.list_evidence(item.id + 100) == []


def test_insert_evidence_requires_transaction(store, draft_factory):
    item = store.create_item(draft_factory("project:test:fact:evidence-tx"))
    with pytest.raises(RuntimeError, match="transaction"):
        store.insert_evidence(item.id, None, "success", "", "2026-01-02 00:00:00")


def test_update_outcome_stats_applies_deltas_and_optional_fields(
        store, draft_factory):
    item = store.create_item(draft_factory("project:test:fact:stats"))

    with store.transaction():
        assert store.update_outcome_stats(
            item.id, success_delta=2, last_verified_at="2026-01-05 00:00:00"
        ) is True
    reloaded = store.get_item(item.id)
    assert reloaded.success_count == 2
    assert reloaded.failure_count == 0
    assert reloaded.confidence == 0.8  # None keeps the stored value
    assert reloaded.last_verified_at == "2026-01-05 00:00:00"

    with store.transaction():
        store.update_outcome_stats(item.id, failure_delta=1, confidence=0.3)
    reloaded = store.get_item(item.id)
    assert reloaded.success_count == 2
    assert reloaded.failure_count == 1
    assert reloaded.confidence == 0.3
    # None never erases a stored verification timestamp.
    assert reloaded.last_verified_at == "2026-01-05 00:00:00"

    with store.transaction():
        assert store.update_outcome_stats(item.id + 100, success_delta=1) is False


def test_update_outcome_stats_requires_transaction(store, draft_factory):
    item = store.create_item(draft_factory("project:test:fact:stats-tx"))
    with pytest.raises(RuntimeError, match="transaction"):
        store.update_outcome_stats(item.id, success_delta=1)


def test_list_item_sources_returns_session_and_experience_rows(
        store, draft_factory):
    item = store.create_item(draft_factory("project:test:fact:multi-source"))
    other = store.create_item(draft_factory("project:test:fact:source-target"))
    with store.transaction():
        archive_id = store.upsert_session_archive(
            "proj", "codex", "sess-multi",
            payload_path="session_archives/multi.bin",
            payload_sha256="a" * 64,
            expires_at="2026-12-31 00:00:00",
            recorded_at="2026-01-01 00:00:00",
        )
        session_source = store.record_session_source(
            item.id, archive_id, extraction_version="v1"
        )
        experience_source = store.record_experience_source(
            item.id, other.id, extraction_version="v1"
        )

    rows = store.list_item_sources(item.id)
    assert [row["id"] for row in rows] == [session_source, experience_source]
    assert rows[0]["archive_id"] == archive_id
    assert rows[0]["source_kind"] == "session"
    assert rows[1]["archive_id"] is None
    assert rows[1]["source_kind"] == "experience"
    assert rows[1]["source_ref"] == str(other.id)
    assert store.list_item_sources(other.id) == []


def test_record_experience_source_counts_without_touching_source_state(
        store, draft_factory):
    playbook = store.create_item(draft_factory("project:test:fact:playbook-src"))
    experience = store.create_item(draft_factory("project:test:fact:exp-src"))

    with store.transaction():
        source_id = store.record_experience_source(
            playbook.id, experience.id, extraction_version="v9"
        )
    assert source_id > 0
    reloaded = store.get_item(playbook.id)
    assert reloaded.source_count == 1
    # Experience dependencies are not archive-backed: source_state stays none.
    assert reloaded.source_state == "none"

    with pytest.raises(RuntimeError, match="transaction"):
        store.record_experience_source(playbook.id, experience.id,
                                       extraction_version="v9")


def test_list_dependent_playbook_ids_scopes_to_active_playbooks(store):
    def make(identity_key, content_type, status):
        return store.create_item(
            ContextItemDraft(
                identity_key=identity_key,
                content_type=content_type,
                layers=ContextLayers(
                    l0=f"Summary of {identity_key}",
                    l1="Supporting detail for the dependent-playbook test.",
                    l2="Full source material for the dependent-playbook test.",
                    generator="test-suite",
                ),
                project="test",
                scope=ContextScope.PROJECT,
                status=status,
                tier=ContextTier.NORMAL,
                tags=("lifecycle",),
                importance=6.0,
                confidence=0.8,
            )
        )

    experience = make("experience:test:dep-exp", ContextContentType.EXPERIENCE,
                      ContextStatus.ACTIVE)
    active_playbook = make("playbook:test:dep-active", ContextContentType.PLAYBOOK,
                           ContextStatus.ACTIVE)
    candidate_playbook = make("playbook:test:dep-cand", ContextContentType.PLAYBOOK,
                              ContextStatus.CANDIDATE)
    active_fact = make("fact:test:dep-fact", ContextContentType.FACT,
                       ContextStatus.ACTIVE)
    with store.transaction():
        for item in (active_playbook, candidate_playbook, active_fact):
            store.record_experience_source(
                item.id, experience.id, extraction_version="v1"
            )

    # Only ACTIVE playbooks are demotion targets.
    assert store.list_dependent_playbook_ids(experience.id) == [active_playbook.id]
    assert store.list_dependent_playbook_ids(active_playbook.id) == []


def test_list_item_ids_filters_by_status_type_and_project(store, draft_factory):
    first = store.create_item(draft_factory("project:test:fact:ids-a"))
    second = store.create_item(
        draft_factory("project:test:fact:ids-b", status=ContextStatus.ACTIVE)
    )
    third = store.create_item(
        draft_factory("project:test:fact:ids-c", project="other")
    )

    assert store.list_item_ids() == [first.id, second.id, third.id]
    assert store.list_item_ids(status=ContextStatus.CANDIDATE) == [first.id, third.id]
    assert store.list_item_ids(status=ContextStatus.ACTIVE) == [second.id]
    assert store.list_item_ids(project="other") == [third.id]
    assert store.list_item_ids(
        status=ContextStatus.CANDIDATE, project="other"
    ) == [third.id]
    assert store.list_item_ids(
        content_type=ContextContentType.EXPERIENCE
    ) == []


def test_active_identity_exists_ignores_self_and_non_active(store, draft_factory):
    active = store.create_item(
        draft_factory("project:test:fact:identity", status=ContextStatus.ACTIVE)
    )
    candidate = store.create_item(draft_factory("project:test:fact:identity"))
    stranger = store.create_item(draft_factory("project:test:fact:stranger"))

    assert store.active_identity_exists(
        "project:test:fact:identity", "test", ContextScope.PROJECT,
        exclude_id=candidate.id,
    ) is True
    # The active item itself is excluded.
    assert store.active_identity_exists(
        "project:test:fact:identity", "test", ContextScope.PROJECT,
        exclude_id=active.id,
    ) is False
    assert store.active_identity_exists(
        "project:test:fact:stranger", "test", ContextScope.PROJECT,
        exclude_id=stranger.id,
    ) is False
    # Candidates never satisfy the active-identity probe: the stranger
    # identity is held only by a candidate, so even excluding an unrelated
    # id the probe stays False.
    assert store.active_identity_exists(
        "project:test:fact:stranger", "test", ContextScope.PROJECT,
        exclude_id=active.id,
    ) is False
    assert store.active_identity_exists(
        "project:test:fact:identity", "other", ContextScope.PROJECT,
        exclude_id=candidate.id,
    ) is False

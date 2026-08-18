"""Behavioral contracts for typed legacy mutations and the compatibility facade."""

from contextlib import contextmanager
import sqlite3

import numpy as np
import pytest

from evolvmem.context_models import (
    ContextContentType,
    ContextLayer,
    ContextMode,
    ContextScope,
    ContextServiceError,
    ContextStatus,
    ContextTier,
    ContextValidationError,
)
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.legacy_models import (
    LegacyAccessRequest,
    LegacyAccessResult,
    LegacyAddRequest,
    LegacyHardDeleteRequest,
    LegacyMutationResult,
    LegacyRemoveRequest,
    LegacyReplaceRequest,
    LegacyStatusRequest,
    LegacyUpdateRequest,
)
from evolvmem.memory_store import MemoryStore
from evolvmem.vector_index import VectorIndex


ALL_LAYERS = (ContextLayer.L0, ContextLayer.L1, ContextLayer.L2)


class InjectedFailure(RuntimeError):
    """Raised by failure-injection stores right after a named write step."""


class RecordingVectorIndex:
    """Duck-typed VectorIndex fake: records calls, never touches the filesystem."""

    def __init__(self, config, *, path, events=None, label="index"):
        self.config = config
        self.path = path.expanduser().resolve()
        self.events = events
        self.label = label
        self.calls: list[tuple] = []
        self.dirty = False

    def _record(self, action, *args):
        entry = (self.label, action, *args)
        self.calls.append(entry)
        if self.events is not None:
            self.events.append(entry)

    def is_dirty(self):
        return self.dirty

    def count(self):
        return 0

    def mark_dirty(self):
        self._record("mark_dirty")
        self.dirty = True

    def preserve_dirty(self):
        self._record("preserve_dirty")

    def clear_dirty(self):
        self._record("clear_dirty")
        self.dirty = False

    def initialize(self, dim=512):
        self._record("initialize", dim)

    def add(self, mem_id, embedding):
        self._record("add", mem_id)

    def remove(self, mem_id):
        self._record("remove", mem_id)
        return False

    def save(self):
        self._record("save")

    def close(self):
        pass


class FixedVectorEngine:
    """Deterministic loaded engine: one constant vector per encoded document."""

    is_loaded = True

    def __init__(self, dim=3):
        self._dim = dim
        self.encoded: list[str] = []

    def encode_document(self, text: str) -> list[float]:
        self.encoded.append(text)
        return [1.0] + [0.0] * (self._dim - 1)


class FailureInjectionStore(ContextStore):
    """ContextStore spy that fails right after a named write step."""

    def __init__(self, config):
        super().__init__(config)
        self.fail_after = ""

    @contextmanager
    def transaction(self):
        with super().transaction():
            yield self
            if self.fail_after == "before_commit":
                raise InjectedFailure("before_commit")

    def legacy_projection(self):
        repository = super().legacy_projection()
        if self.fail_after == "projection_insert":
            insert = repository.insert

            def insert_then_fail(request):
                insert(request)
                raise InjectedFailure("projection_insert")

            repository.insert = insert_then_fail
        elif self.fail_after == "projection_replace":
            replace = repository.replace

            def replace_then_fail(request):
                replace(request)
                raise InjectedFailure("projection_replace")

            repository.replace = replace_then_fail
        elif self.fail_after == "old_status_update":
            set_status = repository.set_status

            def set_status_then_fail(legacy_id, status):
                set_status(legacy_id, status)
                raise InjectedFailure("old_status_update")

            repository.set_status = set_status_then_fail
        elif self.fail_after == "projection_hard_delete":
            hard_delete = repository.hard_delete

            def hard_delete_then_fail(legacy_id):
                hard_delete(legacy_id)
                raise InjectedFailure("projection_hard_delete")

            repository.hard_delete = hard_delete_then_fail
        return repository

    def _insert_layers(self, item_id, layers, now):
        super()._insert_layers(item_id, layers, now)
        if self.fail_after == "context_layers_insert":
            raise InjectedFailure("context_layers_insert")

    def record_legacy_mapping(self, legacy_memory_id, context_item_id):
        super().record_legacy_mapping(legacy_memory_id, context_item_id)
        if self.fail_after == "mapping_insert":
            raise InjectedFailure("mapping_insert")

    def supersede_item(self, predecessor_id, draft):
        item = super().supersede_item(predecessor_id, draft)
        if self.fail_after == "new_link_update":
            raise InjectedFailure("new_link_update")
        return item

    def delete_legacy_mapping(self, legacy_id):
        result = super().delete_legacy_mapping(legacy_id)
        if self.fail_after == "mapping_delete":
            raise InjectedFailure("mapping_delete")
        return result

    def hard_delete_item(self, item_id):
        result = super().hard_delete_item(item_id)
        if self.fail_after == "context_item_delete":
            raise InjectedFailure("context_item_delete")
        return result


class CommitRecordingStore(ContextStore):
    """ContextStore spy that records every outermost commit into a shared log."""

    def __init__(self, config, events):
        super().__init__(config)
        self._events = events

    @contextmanager
    def transaction(self):
        outermost = self._transaction_depth == 0
        with super().transaction():
            yield self
        if outermost:
            self._events.append("sqlite-committed")


@pytest.fixture
def store(test_config):
    with ContextStore(test_config) as instance:
        yield instance


def _create_legacy_schema(config):
    with MemoryStore(config):
        pass


def _make_service(config, store, *, mode=ContextMode.COMPAT, engine=None):
    context_index = RecordingVectorIndex(
        config, path=config.context_vector_path, label="context"
    )
    legacy_index = RecordingVectorIndex(config, path=config.vector_path, label="legacy")
    service = ContextService(
        config, store=store, vector_index=context_index, embedding_engine=engine
    )
    service._legacy_vector = legacy_index
    service.initialize(mode=mode, adapter="test")
    service._test_context_index = context_index
    service._test_legacy_index = legacy_index
    return service


def _make_real_vector_service(config, store, *, engine, mode=ContextMode.COMPAT):
    context_index = VectorIndex(config, path=config.context_vector_path)
    legacy_index = VectorIndex(config)
    service = ContextService(
        config, store=store, vector_index=context_index, embedding_engine=engine
    )
    service._legacy_vector = legacy_index
    service.initialize(mode=mode, adapter="test")
    return service, legacy_index, context_index


def _index_ids(index, dim=3):
    if index.count() == 0:
        return set()
    probe = np.array([1.0] + [0.0] * (dim - 1), dtype=np.float32)
    return {entry["id"] for entry in index.search(probe, k=20)}


def _snapshot(config):
    conn = sqlite3.connect(config.db_path)
    try:
        return {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in (
                "memories",
                "context_items",
                "context_layers",
                "context_sources",
                "legacy_memory_migrations",
            )
        }
    finally:
        conn.close()


def _legacy_row(config, legacy_id):
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM memories WHERE id=?", (legacy_id,)).fetchone()
        return None if row is None else dict(row)
    finally:
        conn.close()


def _seed_pair(service, key="alpha", value="first version", **add_kwargs):
    result = service.legacy_add(LegacyAddRequest(key=key, value=value, **add_kwargs))
    return result.legacy_id, result.context_id


# ---- typed request/result contracts ----


def test_add_request_validates_and_normalizes_at_the_boundary():
    request = LegacyAddRequest(
        key="  alpha  ",
        value=" some value\r\n",
        attribute=" Decision ",
        tags=[" b ", "a", "b", ""],
        source_session=" session-1 ",
        importance=8,
        tier="PINNED",
        confidence=1,
    )
    assert request.key == "alpha"
    assert request.value == "some value"
    assert request.attribute == "Decision"
    assert request.tags == ("b", "a")
    assert request.source_session == "session-1"
    assert request.importance == 8
    assert request.tier == "pinned"
    assert request.confidence == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"key": "  "},
        {"key": 1},
        {"value": ""},
        {"value": None},
        {"tags": "not-a-sequence"},
        {"importance": 0.5},
        {"importance": 10.5},
        {"importance": True},
        {"tier": "durable"},
        {"tier": None},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"expires_at": 20260101},
    ],
)
def test_add_request_rejects_invalid_fields(overrides):
    fields = {"key": "alpha", "value": "some value", **overrides}
    with pytest.raises(ContextValidationError):
        LegacyAddRequest(**fields)


def test_replace_request_allows_inheritance_but_validates_present_fields():
    request = LegacyReplaceRequest(key="alpha", new_value="second version")
    assert request.attribute is None
    assert request.tags is None
    assert request.importance is None
    assert request.tier is None
    assert request.expires_at is None
    assert request.confidence is None
    with pytest.raises(ContextValidationError):
        LegacyReplaceRequest(key="", new_value="second version")
    with pytest.raises(ContextValidationError):
        LegacyReplaceRequest(key="alpha", new_value="  ")
    with pytest.raises(ContextValidationError):
        LegacyReplaceRequest(key="alpha", new_value="v", tier="durable")
    with pytest.raises(ContextValidationError):
        LegacyReplaceRequest(key="alpha", new_value="v", importance=0.0)
    with pytest.raises(ContextValidationError):
        LegacyReplaceRequest(key="alpha", new_value="v", tags="oops")


def test_id_requests_require_positive_integer_ids():
    for request_type in (LegacyRemoveRequest, LegacyStatusRequest, LegacyHardDeleteRequest):
        assert request_type(legacy_id=3).legacy_id == 3
        with pytest.raises(ContextValidationError):
            request_type(legacy_id=0)
        with pytest.raises(ContextValidationError):
            request_type(legacy_id=True)
    with pytest.raises(ContextValidationError):
        LegacyUpdateRequest(legacy_id=-1, importance=7.0)
    with pytest.raises(ContextValidationError):
        LegacyUpdateRequest(legacy_id=1, importance=11.0)
    with pytest.raises(ContextValidationError):
        LegacyUpdateRequest(legacy_id=1, tier="durable")
    assert LegacyUpdateRequest(legacy_id=1).importance is None


def test_update_request_normalizes_optional_classification_fields():
    """attribute/tags follow the ReplaceRequest optional-field conventions."""
    request = LegacyUpdateRequest(
        legacy_id=1, attribute=" Decision ", tags=[" b ", "a", "b", ""]
    )
    assert request.attribute == "Decision"
    assert request.tags == ("b", "a")
    # 缺省 None = 不变更该字段
    plain = LegacyUpdateRequest(legacy_id=1)
    assert plain.attribute is None
    assert plain.tags is None
    with pytest.raises(ContextValidationError):
        LegacyUpdateRequest(legacy_id=1, attribute=1)
    with pytest.raises(ContextValidationError):
        LegacyUpdateRequest(legacy_id=1, tags="not-a-sequence")


def test_access_request_dedupes_and_validates_ids():
    request = LegacyAccessRequest(legacy_ids=[3, 1, 3, 1])
    assert request.legacy_ids == (3, 1)
    with pytest.raises(ContextValidationError):
        LegacyAccessRequest(legacy_ids=(1, 0))
    with pytest.raises(ContextValidationError):
        LegacyAccessRequest(legacy_ids=(True,))
    with pytest.raises(ContextValidationError):
        LegacyAccessRequest(legacy_ids="12")


def test_results_are_immutable_and_validated():
    result = LegacyMutationResult(
        legacy_id=1,
        context_id=2,
        old_legacy_id=None,
        old_context_id=None,
        available_layers=[ContextLayer.L0],
        changed=True,
    )
    assert result.available_layers == (ContextLayer.L0,)
    with pytest.raises(AttributeError):
        result.changed = False
    with pytest.raises(ContextValidationError):
        LegacyMutationResult(
            legacy_id=0,
            context_id=None,
            old_legacy_id=None,
            old_context_id=None,
            available_layers=(),
            changed=True,
        )
    with pytest.raises(ContextValidationError):
        LegacyMutationResult(
            legacy_id=1,
            context_id=None,
            old_legacy_id=None,
            old_context_id=None,
            available_layers=(),
            changed="yes",
        )
    access = LegacyAccessResult(updated_legacy_ids=[2, 1], updated_context_ids=[9])
    assert access.updated_legacy_ids == (2, 1)
    assert access.updated_context_ids == (9,)
    with pytest.raises(AttributeError):
        access.updated_legacy_ids = ()
    with pytest.raises(ContextValidationError):
        LegacyAccessResult(updated_legacy_ids=(0,), updated_context_ids=())


# ---- add ----


def test_add_produces_projection_three_layers_mapping_and_result_ids(test_config, store):
    _create_legacy_schema(test_config)
    service = _make_service(test_config, store)
    before = _snapshot(test_config)

    result = service.legacy_add(
        LegacyAddRequest(
            key=" decision:database ",
            value="Use SQLite first.",
            attribute="decision",
            tags=["storage", "storage"],
            source_session="session-1",
            importance=8.0,
            tier="pinned",
            expires_at="2031-01-02 03:04:05",
            confidence=0.9,
        )
    )

    assert isinstance(result, LegacyMutationResult)
    assert result.changed is True
    assert result.old_legacy_id is None and result.old_context_id is None
    assert result.available_layers == ALL_LAYERS

    row = _legacy_row(test_config, result.legacy_id)
    assert row is not None
    assert row["key"] == "decision:database"
    assert row["value"] == "Use SQLite first."
    assert row["status"] == "active"
    assert row["attribute"] == "decision"
    assert row["tags"] == "storage"
    assert row["source_session"] == "session-1"
    assert row["importance"] == 8.0
    assert row["tier"] == "pinned"
    assert row["expires_at"] == "2031-01-02 03:04:05"

    assert store.resolve_legacy_mapping(result.legacy_id) == result.context_id
    item = store.get_item(result.context_id)
    assert item is not None
    assert item.identity_key == "decision:database"
    assert item.status is ContextStatus.ACTIVE
    assert item.tier is ContextTier.PINNED
    assert item.tags == ("storage",)
    assert item.importance == 8.0
    assert item.confidence == 0.9  # extractor confidence is retained
    assert item.expires_at == "2031-01-02 03:04:05"
    assert item.layers is not None
    assert item.layers.l2 == "Use SQLite first."
    assert item.layers.l1 == "Use SQLite first."
    assert item.layers.l0
    assert store.get_layer(result.context_id, ContextLayer.L1) == "Use SQLite first."

    after = _snapshot(test_config)
    assert len(after["memories"]) == len(before["memories"]) + 1
    assert len(after["context_items"]) == len(before["context_items"]) + 1
    assert len(after["context_layers"]) == len(before["context_layers"]) + 3
    assert len(after["legacy_memory_migrations"]) == len(before["legacy_memory_migrations"]) + 1
    service.close()


def test_add_without_extractor_confidence_uses_the_durability_default(test_config, store):
    _create_legacy_schema(test_config)
    service = _make_service(test_config, store)

    durable = service.legacy_add(LegacyAddRequest(key="alpha", value="durable value"))
    expiring = service.legacy_add(
        LegacyAddRequest(key="beta", value="expiring value", expires_at="2031-01-01 00:00:00")
    )

    assert store.get_item(durable.context_id).confidence == 1.0
    assert store.get_item(expiring.context_id).confidence == 0.5
    service.close()


# ---- replace ----


def test_replace_supersedes_the_exact_mapped_predecessor_on_both_sides(test_config, store):
    _create_legacy_schema(test_config)
    service = _make_service(test_config, store)
    old_legacy_id, old_context_id = _seed_pair(
        service,
        key="decision:database",
        value="Use SQLite first.",
        attribute="decision",
        tags=["storage"],
        importance=8.0,
        tier="pinned",
        expires_at="2031-01-02 03:04:05",
        confidence=0.9,
    )

    result = service.legacy_replace(
        LegacyReplaceRequest(key="decision:database", new_value="Use PostgreSQL.", confidence=0.7)
    )

    assert result.changed is True
    assert result.old_legacy_id == old_legacy_id
    assert result.old_context_id == old_context_id
    assert result.legacy_id != old_legacy_id
    assert result.context_id != old_context_id
    assert result.available_layers == ALL_LAYERS

    old_row = _legacy_row(test_config, old_legacy_id)
    assert old_row["status"] == "superseded"
    assert old_row["superseded_by"] == result.legacy_id
    new_row = _legacy_row(test_config, result.legacy_id)
    assert new_row["status"] == "active"
    assert new_row["supersedes"] == old_legacy_id
    assert new_row["value"] == "Use PostgreSQL."
    # omitted legacy fields are inherited by the projection
    assert new_row["attribute"] == "decision"
    assert new_row["tags"] == "storage"
    assert new_row["importance"] == 8.0
    assert new_row["tier"] == "pinned"
    assert new_row["expires_at"] == "2031-01-02 03:04:05"

    old_item = store.get_item(old_context_id)
    assert old_item.status is ContextStatus.SUPERSEDED
    assert old_item.superseded_by == result.context_id
    new_item = store.get_item(result.context_id)
    assert new_item.status is ContextStatus.ACTIVE
    assert new_item.supersedes == old_context_id
    assert new_item.identity_key == old_item.identity_key
    # Core metadata is derived from the stored projection row, so it inherits too
    assert new_item.tier is ContextTier.PINNED
    assert new_item.tags == ("storage",)
    assert new_item.importance == 8.0
    assert new_item.expires_at == "2031-01-02 03:04:05"
    assert new_item.confidence == 0.7
    assert new_item.layers.l2 == "Use PostgreSQL."

    assert store.resolve_legacy_mapping(result.legacy_id) == result.context_id
    assert store.resolve_legacy_mapping(old_legacy_id) == old_context_id
    service.close()


def test_replace_without_an_active_row_creates_both_sides(test_config, store):
    _create_legacy_schema(test_config)
    service = _make_service(test_config, store)

    result = service.legacy_replace(
        LegacyReplaceRequest(key="fresh", new_value="brand new value")
    )

    assert result.changed is True
    assert result.old_legacy_id is None and result.old_context_id is None
    assert _legacy_row(test_config, result.legacy_id)["status"] == "active"
    assert store.get_item(result.context_id).status is ContextStatus.ACTIVE
    assert store.resolve_legacy_mapping(result.legacy_id) == result.context_id
    service.close()


def test_replace_migrates_an_unmapped_predecessor_inside_the_same_transaction(
    test_config,
):
    _create_legacy_schema(test_config)
    with MemoryStore(test_config) as legacy:
        old_legacy_id = legacy.add(key="alpha", value="historical value", importance=6.5)
    store = ContextStore(test_config)
    store.initialize()
    service = _make_service(test_config, store)

    result = service.legacy_replace(
        LegacyReplaceRequest(key="alpha", new_value="replacement value")
    )

    assert result.old_legacy_id == old_legacy_id
    assert result.old_context_id is not None
    old_item = store.get_item(result.old_context_id)
    assert old_item.status is ContextStatus.SUPERSEDED
    assert old_item.layers.l2 == "historical value"
    # touch-migration preserved the historical timestamps and access telemetry
    old_row = _legacy_row(test_config, old_legacy_id)
    assert old_item.created_at == old_row["created_at"]
    assert old_item.updated_at == old_row["updated_at"]
    assert old_item.importance == 6.5
    assert store.resolve_legacy_mapping(old_legacy_id) == result.old_context_id
    assert store.get_item(result.context_id).supersedes == result.old_context_id
    assert store.resolve_legacy_mapping(result.legacy_id) == result.context_id
    service.close()
    store.close()


# ---- remove ----


def test_remove_marks_both_sides_deleted(test_config, store):
    _create_legacy_schema(test_config)
    service = _make_service(test_config, store)
    legacy_id, context_id = _seed_pair(service)

    result = service.legacy_remove(LegacyRemoveRequest(legacy_id=legacy_id))

    assert result == LegacyMutationResult(
        legacy_id=legacy_id,
        context_id=context_id,
        old_legacy_id=None,
        old_context_id=None,
        available_layers=ALL_LAYERS,
        changed=True,
    )
    assert _legacy_row(test_config, legacy_id)["status"] == "deleted"
    assert store.get_item(context_id).status is ContextStatus.DELETED
    assert store.resolve_legacy_mapping(legacy_id) == context_id
    service.close()


def test_remove_of_a_nonexistent_id_is_a_compatible_noop(test_config, store):
    _create_legacy_schema(test_config)
    service = _make_service(test_config, store)
    neighbor_id, neighbor_context_id = _seed_pair(service)
    before = _snapshot(test_config)

    result = service.legacy_remove(LegacyRemoveRequest(legacy_id=999999))

    assert result.changed is False
    assert result.context_id is None
    assert result.available_layers == ()
    assert _snapshot(test_config) == before
    assert _legacy_row(test_config, neighbor_id)["status"] == "active"
    assert store.get_item(neighbor_context_id).status is ContextStatus.ACTIVE
    service.close()


def test_remove_migrates_an_unmapped_row_before_deleting(test_config, store):
    _create_legacy_schema(test_config)
    with MemoryStore(test_config) as legacy:
        legacy_id = legacy.add(key="alpha", value="historical value")
    service = _make_service(test_config, store)

    result = service.legacy_remove(LegacyRemoveRequest(legacy_id=legacy_id))

    assert result.changed is True
    assert result.context_id is not None
    item = store.get_item(result.context_id)
    assert item.status is ContextStatus.DELETED
    assert item.layers.l2 == "historical value"
    assert _legacy_row(test_config, legacy_id)["status"] == "deleted"
    assert store.resolve_legacy_mapping(legacy_id) == result.context_id
    service.close()


# ---- metadata update ----


def test_update_mirrors_importance_and_tier_on_both_sides(test_config, store):
    _create_legacy_schema(test_config)
    service = _make_service(test_config, store)
    legacy_id, context_id = _seed_pair(service, importance=5.0, tier="normal")

    result = service.legacy_update(
        LegacyUpdateRequest(legacy_id=legacy_id, importance=7.5, tier="pinned")
    )

    assert result.changed is True
    assert result.context_id == context_id
    row = _legacy_row(test_config, legacy_id)
    assert row["importance"] == 7.5
    assert row["tier"] == "pinned"
    item = store.get_item(context_id)
    assert item.importance == 7.5
    assert item.tier is ContextTier.PINNED
    service.close()


def test_update_of_a_nonexistent_id_or_empty_change_is_a_noop(test_config, store):
    _create_legacy_schema(test_config)
    service = _make_service(test_config, store)
    legacy_id, _ = _seed_pair(service)
    before = _snapshot(test_config)

    missing = service.legacy_update(
        LegacyUpdateRequest(legacy_id=424242, importance=7.0)
    )
    empty = service.legacy_update(LegacyUpdateRequest(legacy_id=legacy_id))

    assert missing.changed is False and missing.context_id is None
    assert empty.changed is False
    assert _snapshot(test_config) == before
    service.close()


def test_update_mirrors_attribute_tags_and_derived_fields_on_both_sides(
    test_config, store
):
    """attribute/tags 与 importance/tier 一样同事务镜像到映射的 ContextItem。

    content_type 由迁移器公开策略 content_type_for 从刚落库的投影行重新推导，
    scope 作为 content_type 的纯派生一并跟随，两侧不再能悄悄分叉。
    """
    _create_legacy_schema(test_config)
    service = _make_service(test_config, store)
    legacy_id, context_id = _seed_pair(service, attribute="fact", tags=["a"])

    result = service.legacy_update(
        LegacyUpdateRequest(legacy_id=legacy_id, attribute="decision", tags=("x", "y"))
    )

    assert result.changed is True
    assert result.context_id == context_id
    row = _legacy_row(test_config, legacy_id)
    assert row["attribute"] == "decision"
    assert row["tags"] == "x,y"
    item = store.get_item(context_id)
    assert item.content_type is ContextContentType.DECISION
    assert item.scope is ContextScope.PROJECT
    assert item.tags == ("x", "y")
    # 未指定字段保持原值
    assert item.importance == 5.0
    assert item.tier is ContextTier.NORMAL
    service.close()


def test_update_attribute_to_global_type_moves_scope_with_content_type(
    test_config, store
):
    _create_legacy_schema(test_config)
    service = _make_service(test_config, store)
    legacy_id, context_id = _seed_pair(service, attribute="fact")
    assert store.get_item(context_id).scope is ContextScope.PROJECT

    service.legacy_update(
        LegacyUpdateRequest(legacy_id=legacy_id, attribute="preference")
    )

    item = store.get_item(context_id)
    assert item.content_type is ContextContentType.PREFERENCE
    assert item.scope is ContextScope.GLOBAL
    service.close()


def test_update_classification_rolls_back_both_sides_on_core_failure(test_config):
    """Core 侧写入失败时投影行的 attribute/tags 编辑一并回滚，无部分更新。"""
    _create_legacy_schema(test_config)

    class FailingStore(ContextStore):
        def update_item_from_legacy(self, *args, **kwargs):
            raise InjectedFailure("update_item_from_legacy")

    store = FailingStore(test_config)
    store.initialize()
    service = _make_service(test_config, store)
    legacy_id, context_id = _seed_pair(service, attribute="fact", tags=["a"])
    before_row = _legacy_row(test_config, legacy_id)

    with pytest.raises(InjectedFailure):
        service.legacy_update(
            LegacyUpdateRequest(legacy_id=legacy_id, attribute="decision", tags=("x",))
        )

    assert _legacy_row(test_config, legacy_id) == before_row
    item = store.get_item(context_id)
    assert item.content_type is ContextContentType.FACT
    assert item.tags == ("a",)
    service.close()


def test_legacy_mode_update_applies_attribute_and_tags_in_place(test_config, store):
    """legacy 模式：四字段就地更新 memories 行（旧行为等价），Core 保持空。"""
    _create_legacy_schema(test_config)
    service = _make_service(test_config, store, mode=ContextMode.LEGACY)
    added = service.legacy_add(
        LegacyAddRequest(
            key="alpha", value="old mode value", attribute="fact", tags=("a",)
        )
    )
    legacy_id = added.legacy_id

    result = service.legacy_update(
        LegacyUpdateRequest(legacy_id=legacy_id, attribute="decision", tags=("x", "y"))
    )

    assert result.changed is True
    assert result.context_id is None
    row = _legacy_row(test_config, legacy_id)
    assert row["attribute"] == "decision"
    assert row["tags"] == "x,y"
    assert store.count_by_status() == {}
    service.close()


# ---- archive / restore ----


def test_archive_and_restore_move_both_sides_together(test_config, store):
    _create_legacy_schema(test_config)
    service = _make_service(test_config, store)
    legacy_id, context_id = _seed_pair(service)

    archived = service.legacy_archive(LegacyStatusRequest(legacy_id=legacy_id))
    assert archived.changed is True
    assert archived.context_id == context_id
    assert _legacy_row(test_config, legacy_id)["status"] == "archived"
    assert store.get_item(context_id).status is ContextStatus.ARCHIVED

    restored = service.legacy_restore(LegacyStatusRequest(legacy_id=legacy_id))
    assert restored.changed is True
    assert _legacy_row(test_config, legacy_id)["status"] == "active"
    assert store.get_item(context_id).status is ContextStatus.ACTIVE

    missing = service.legacy_archive(LegacyStatusRequest(legacy_id=31337))
    assert missing.changed is False
    assert missing.context_id is None
    service.close()


def _delete_item_leaving_dangling_mapping(config, context_id):
    """Physically drop a mapped ContextItem, leaving a dangling mapping behind.

    A fresh SQLite connection defaults to foreign_keys=OFF, so the delete
    succeeds where the store's own FK-enforcing connection would refuse it.
    """
    conn = sqlite3.connect(config.db_path)
    try:
        conn.execute("DELETE FROM context_items WHERE id=?", (context_id,))
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize("mutation", ["archive", "update", "remove"])
def test_dangling_mapping_rolls_back_the_whole_mutation(test_config, store, mutation):
    """映射目标已消失时不得静默半提交：整体抛错回滚，投影与映射保持原样。"""
    _create_legacy_schema(test_config)
    service = _make_service(test_config, store)
    legacy_id, context_id = _seed_pair(service, importance=5.0)
    _delete_item_leaving_dangling_mapping(test_config, context_id)
    before_row = _legacy_row(test_config, legacy_id)

    if mutation == "archive":
        def call():
            return service.legacy_archive(LegacyStatusRequest(legacy_id=legacy_id))
    elif mutation == "update":
        def call():
            return service.legacy_update(
                LegacyUpdateRequest(legacy_id=legacy_id, importance=8.0)
            )
    else:
        def call():
            return service.legacy_remove(LegacyRemoveRequest(legacy_id=legacy_id))

    with pytest.raises(ContextServiceError):
        call()

    assert _legacy_row(test_config, legacy_id) == before_row
    assert store.resolve_legacy_mapping(legacy_id) == context_id
    service.close()


# ---- hard delete ----


def test_hard_delete_removes_only_the_exact_triple(test_config, store):
    _create_legacy_schema(test_config)
    service = _make_service(test_config, store)
    legacy_id, context_id = _seed_pair(service, key="doomed")
    neighbor_legacy_id, neighbor_context_id = _seed_pair(service, key="neighbor")

    result = service.legacy_hard_delete(LegacyHardDeleteRequest(legacy_id=legacy_id))

    assert result.changed is True
    assert result.context_id == context_id
    assert _legacy_row(test_config, legacy_id) is None
    assert store.get_item(context_id) is None
    assert store.resolve_legacy_mapping(legacy_id) is None
    assert store.count_by_status() == {"active": 1}
    assert _legacy_row(test_config, neighbor_legacy_id)["status"] == "active"
    assert store.get_item(neighbor_context_id).status is ContextStatus.ACTIVE

    missing = service.legacy_hard_delete(LegacyHardDeleteRequest(legacy_id=legacy_id))
    assert missing.changed is False
    assert missing.context_id is None
    service.close()


def test_hard_delete_of_an_unmapped_row_removes_only_the_projection(test_config, store):
    _create_legacy_schema(test_config)
    with MemoryStore(test_config) as legacy:
        legacy_id = legacy.add(key="alpha", value="unmapped value")
    service = _make_service(test_config, store)

    result = service.legacy_hard_delete(LegacyHardDeleteRequest(legacy_id=legacy_id))

    assert result.changed is True
    assert result.context_id is None
    assert _legacy_row(test_config, legacy_id) is None
    assert store.count_by_status() == {}
    service.close()


# ---- batched access ----


def test_access_increments_both_mapped_sides_exactly_once(test_config, store):
    _create_legacy_schema(test_config)
    service = _make_service(test_config, store)
    legacy_id, context_id = _seed_pair(service)
    other_legacy_id, other_context_id = _seed_pair(service, key="other")
    row_before = _legacy_row(test_config, legacy_id)
    item_before = store.get_item(context_id)

    result = service.legacy_access(
        LegacyAccessRequest(legacy_ids=(legacy_id, legacy_id, 999999))
    )

    assert result == LegacyAccessResult(
        updated_legacy_ids=(legacy_id,), updated_context_ids=(context_id,)
    )
    row = _legacy_row(test_config, legacy_id)
    assert row["access_count"] == row_before["access_count"] + 1
    assert row["updated_at"] == row_before["updated_at"]  # reads never rewrite recency
    item = store.get_item(context_id)
    assert item.access_count == item_before.access_count + 1
    assert item.updated_at == item_before.updated_at
    # unknown ids never touch a neighbor
    assert _legacy_row(test_config, other_legacy_id)["access_count"] == 0
    assert store.get_item(other_context_id).access_count == 0
    service.close()


def test_access_migrates_unmapped_rows_before_mirroring(test_config, store):
    _create_legacy_schema(test_config)
    with MemoryStore(test_config) as legacy:
        first = legacy.add(key="one", value="first historical")
        second = legacy.add(key="two", value="second historical")
    service = _make_service(test_config, store)

    result = service.legacy_access(LegacyAccessRequest(legacy_ids=(second, first)))

    assert result.updated_legacy_ids == (first, second)
    assert len(result.updated_context_ids) == 2
    for legacy_id, context_id in zip(
        result.updated_legacy_ids, result.updated_context_ids
    ):
        assert store.resolve_legacy_mapping(legacy_id) == context_id
        assert store.get_item(context_id).access_count == 1
        assert _legacy_row(test_config, legacy_id)["access_count"] == 1
    service.close()


def test_access_of_only_unknown_ids_changes_nothing(test_config, store):
    _create_legacy_schema(test_config)
    service = _make_service(test_config, store)
    _seed_pair(service)
    before = _snapshot(test_config)

    result = service.legacy_access(LegacyAccessRequest(legacy_ids=(123456,)))

    assert result.updated_legacy_ids == ()
    assert result.updated_context_ids == ()
    assert _snapshot(test_config) == before
    service.close()


# ---- injected failure rollback ----


@pytest.mark.parametrize(
    "fail_after",
    ["projection_insert", "context_layers_insert", "mapping_insert", "before_commit"],
)
def test_add_rolls_back_both_sides_on_any_injected_failure(test_config, fail_after):
    _create_legacy_schema(test_config)
    store = FailureInjectionStore(test_config)
    store.initialize()
    service = _make_service(test_config, store)
    before = _snapshot(test_config)

    store.fail_after = fail_after
    with pytest.raises(InjectedFailure):
        service.legacy_add(LegacyAddRequest(key="alpha", value="atomic value"))

    assert _snapshot(test_config) == before
    assert service._test_context_index.calls == []
    assert service._test_legacy_index.calls == []
    service.close()
    store.close()


@pytest.mark.parametrize(
    "fail_after",
    ["projection_replace", "new_link_update", "mapping_insert", "before_commit"],
)
def test_replace_rolls_back_both_sides_on_any_injected_failure(test_config, fail_after):
    _create_legacy_schema(test_config)
    store = FailureInjectionStore(test_config)
    store.initialize()
    service = _make_service(test_config, store)
    _seed_pair(service)
    before = _snapshot(test_config)
    service._test_context_index.calls.clear()
    service._test_legacy_index.calls.clear()

    store.fail_after = fail_after
    with pytest.raises(InjectedFailure):
        service.legacy_replace(
            LegacyReplaceRequest(key="alpha", new_value="replacement value")
        )

    assert _snapshot(test_config) == before
    assert service._test_context_index.calls == []
    assert service._test_legacy_index.calls == []
    service.close()
    store.close()


@pytest.mark.parametrize("fail_after", ["old_status_update", "before_commit"])
def test_remove_rolls_back_both_sides_on_any_injected_failure(test_config, fail_after):
    _create_legacy_schema(test_config)
    store = FailureInjectionStore(test_config)
    store.initialize()
    service = _make_service(test_config, store)
    _seed_pair(service)
    before = _snapshot(test_config)
    service._test_context_index.calls.clear()
    service._test_legacy_index.calls.clear()

    store.fail_after = fail_after
    with pytest.raises(InjectedFailure):
        service.legacy_remove(LegacyRemoveRequest(legacy_id=1))

    assert _snapshot(test_config) == before
    assert service._test_context_index.calls == []
    assert service._test_legacy_index.calls == []
    service.close()
    store.close()


@pytest.mark.parametrize(
    "fail_after",
    ["mapping_delete", "projection_hard_delete", "context_item_delete", "before_commit"],
)
def test_hard_delete_rolls_back_the_whole_triple_on_any_injected_failure(
    test_config, fail_after
):
    _create_legacy_schema(test_config)
    store = FailureInjectionStore(test_config)
    store.initialize()
    service = _make_service(test_config, store)
    _seed_pair(service)
    before = _snapshot(test_config)
    service._test_context_index.calls.clear()
    service._test_legacy_index.calls.clear()

    store.fail_after = fail_after
    with pytest.raises(InjectedFailure):
        service.legacy_hard_delete(LegacyHardDeleteRequest(legacy_id=1))

    assert _snapshot(test_config) == before
    assert service._test_context_index.calls == []
    assert service._test_legacy_index.calls == []
    service.close()
    store.close()


def test_touch_migration_rolls_back_with_the_failed_mutation(test_config):
    """The on-touch migration of an unmapped row joins the outer transaction."""
    _create_legacy_schema(test_config)
    with MemoryStore(test_config) as legacy:
        legacy_id = legacy.add(key="alpha", value="historical value")
    store = FailureInjectionStore(test_config)
    store.initialize()
    service = _make_service(test_config, store)
    before = _snapshot(test_config)

    store.fail_after = "old_status_update"
    with pytest.raises(InjectedFailure):
        service.legacy_remove(LegacyRemoveRequest(legacy_id=legacy_id))

    assert _snapshot(test_config) == before
    assert store.resolve_legacy_mapping(legacy_id) is None
    service.close()
    store.close()


# ---- legacy mode ----


def test_legacy_mode_add_writes_only_the_legacy_backend(test_config, store):
    service = _make_service(test_config, store, mode=ContextMode.LEGACY)

    result = service.legacy_add(
        LegacyAddRequest(key="alpha", value="legacy only value", confidence=0.9)
    )

    assert result.changed is True
    assert result.context_id is None
    assert result.old_legacy_id is None and result.old_context_id is None
    assert result.available_layers == ()
    assert _legacy_row(test_config, result.legacy_id)["value"] == "legacy only value"
    assert store.count_by_status() == {}
    # legacy mode makes no Core claims, not even vector ones
    assert service._test_context_index.calls == []
    assert service._test_legacy_index.calls != []
    service.close()


def test_legacy_mode_replace_and_remove_keep_old_contracts(test_config, store):
    service = _make_service(test_config, store, mode=ContextMode.LEGACY)
    added = service.legacy_add(LegacyAddRequest(key="alpha", value="first version"))

    replaced = service.legacy_replace(
        LegacyReplaceRequest(key="alpha", new_value="second version")
    )
    assert replaced.changed is True
    assert replaced.context_id is None and replaced.old_legacy_id is None
    assert _legacy_row(test_config, added.legacy_id)["status"] == "superseded"
    assert _legacy_row(test_config, replaced.legacy_id)["status"] == "active"

    missing = service.legacy_remove(LegacyRemoveRequest(legacy_id=999999))
    assert missing.changed is False
    removed = service.legacy_remove(LegacyRemoveRequest(legacy_id=replaced.legacy_id))
    assert removed.changed is True
    assert removed.context_id is None
    assert _legacy_row(test_config, replaced.legacy_id)["status"] == "deleted"
    assert store.count_by_status() == {}
    service.close()


def test_legacy_mode_status_update_access_and_hard_delete(test_config, store):
    service = _make_service(test_config, store, mode=ContextMode.LEGACY)
    added = service.legacy_add(LegacyAddRequest(key="alpha", value="legacy value"))
    legacy_id = added.legacy_id

    assert service.legacy_archive(LegacyStatusRequest(legacy_id=legacy_id)).changed is True
    assert _legacy_row(test_config, legacy_id)["status"] == "archived"
    assert service.legacy_restore(LegacyStatusRequest(legacy_id=legacy_id)).changed is True
    assert _legacy_row(test_config, legacy_id)["status"] == "active"
    updated = service.legacy_update(
        LegacyUpdateRequest(legacy_id=legacy_id, importance=7.5, tier="pinned")
    )
    assert updated.changed is True
    row = _legacy_row(test_config, legacy_id)
    assert row["importance"] == 7.5 and row["tier"] == "pinned"
    access = service.legacy_access(LegacyAccessRequest(legacy_ids=(legacy_id, 999999)))
    assert access.updated_legacy_ids == (legacy_id,)
    assert access.updated_context_ids == ()
    assert _legacy_row(test_config, legacy_id)["access_count"] == 1
    hard_deleted = service.legacy_hard_delete(LegacyHardDeleteRequest(legacy_id=legacy_id))
    assert hard_deleted.changed is True
    assert _legacy_row(test_config, legacy_id) is None
    assert store.count_by_status() == {}
    service.close()


# ---- compatibility facade ----


def test_facade_reads_see_projection_rows_in_compat_mode(test_config, store):
    _create_legacy_schema(test_config)
    service = _make_service(test_config, store)
    facade = service.legacy_facade()

    new_id = facade.add(
        key="alpha", value="searchable zebra", tags=["a", "b"], importance=6.0
    )
    assert isinstance(new_id, int)
    assert facade.get_by_id(new_id)["value"] == "searchable zebra"
    assert facade.get_by_id(new_id)["tags"] == "a,b"
    assert [row["id"] for row in facade.get_by_key("alpha")] == [new_id]
    assert facade.get_by_ids([new_id])[0]["key"] == "alpha"
    assert new_id in facade.all_ids()
    assert facade.count_active() == 1
    assert any(row["id"] == new_id for row in facade.get_active())
    assert any(row["id"] == new_id for row in facade.search_fts("zebra"))
    candidates = facade.get_forgetting_candidates(
        days_threshold=0, access_threshold=99, rate_limit_days=0
    )
    assert any(row["id"] == new_id for row in candidates)
    service.close()


def test_facade_mutations_delegate_and_return_old_shapes(test_config, store):
    _create_legacy_schema(test_config)
    service = _make_service(test_config, store)
    facade = service.legacy_facade()

    first = facade.add(key="alpha", value="first version")
    second = facade.replace("alpha", "second version")
    assert isinstance(second, int) and second != first
    assert facade.get_by_id(first)["status"] == "superseded"
    assert facade.get_by_id(second)["status"] == "active"
    context_id = store.resolve_legacy_mapping(second)
    assert context_id is not None

    assert facade.update_metadata(second, importance=6.5, tier="pinned") is None
    assert facade.get_by_id(second)["importance"] == 6.5
    assert store.get_item(context_id).importance == 6.5

    assert facade.archive(second) is None
    assert facade.get_by_id(second)["status"] == "archived"
    assert store.get_item(context_id).status is ContextStatus.ARCHIVED
    assert facade.restore(second) is None
    assert facade.get_by_id(second)["status"] == "active"
    assert store.get_item(context_id).status is ContextStatus.ACTIVE

    assert facade.update_access(second) is None
    assert facade.get_by_id(second)["access_count"] == 1
    assert store.get_item(context_id).access_count == 1

    doomed = facade.add(key="doomed", value="to be soft deleted")
    assert facade.remove(doomed) is None
    assert facade.get_by_id(doomed)["status"] == "deleted"
    assert facade.hard_delete(doomed) is None
    assert facade.get_by_id(doomed) is None
    service.close()


def test_facade_never_exposes_connections_sql_or_transactions(test_config, store):
    _create_legacy_schema(test_config)
    service = _make_service(test_config, store)

    facade = service.legacy_facade()

    assert service.legacy_facade() is facade
    for forbidden in ("_conn", "_execute", "transaction"):
        assert not hasattr(facade, forbidden)
    service.close()


def test_facade_reads_use_the_legacy_backend_in_legacy_mode(test_config, store):
    service = _make_service(test_config, store, mode=ContextMode.LEGACY)
    facade = service.legacy_facade()

    new_id = facade.add(key="alpha", value="legacy zebra")
    assert facade.get_by_id(new_id)["value"] == "legacy zebra"
    assert any(row["id"] == new_id for row in facade.search_fts("zebra"))
    assert facade.count_active() == 1
    assert store.count_by_status() == {}
    service.close()


# ---- post-commit vector aftermath ----


def test_vector_writes_start_only_after_the_sqlite_commit(test_config):
    _create_legacy_schema(test_config)
    events: list = []
    store = CommitRecordingStore(test_config, events)
    store.initialize()
    context_index = RecordingVectorIndex(
        test_config, path=test_config.context_vector_path, events=events, label="context"
    )
    legacy_index = RecordingVectorIndex(
        test_config, path=test_config.vector_path, events=events, label="legacy"
    )
    test_config.embedding_dim = 3
    service = ContextService(
        test_config,
        store=store,
        vector_index=context_index,
        embedding_engine=FixedVectorEngine(),
    )
    service._legacy_vector = legacy_index
    service.initialize(mode=ContextMode.COMPAT, adapter="test")
    events.clear()

    service.legacy_add(LegacyAddRequest(key="alpha", value="vectorized value"))

    committed_at = events.index("sqlite-committed")
    vector_positions = [
        index
        for index, event in enumerate(events)
        if isinstance(event, tuple) and event[0] in ("legacy", "context")
    ]
    assert vector_positions
    assert all(position > committed_at for position in vector_positions)
    service.close()
    store.close()


def test_add_updates_both_indexes_after_commit(test_config, store):
    _create_legacy_schema(test_config)
    test_config.embedding_dim = 3
    engine = FixedVectorEngine()
    service, legacy_index, context_index = _make_real_vector_service(
        test_config, store, engine=engine
    )

    result = service.legacy_add(LegacyAddRequest(key="alpha", value="vectorized value"))

    assert _index_ids(legacy_index) == {result.legacy_id}
    assert _index_ids(context_index) == {result.context_id}
    assert legacy_index.is_dirty() is False
    assert context_index.is_dirty() is False
    assert "vectorized value" in engine.encoded  # legacy value document
    service.close()


def test_replace_swaps_predecessor_entries_for_successors(test_config, store):
    _create_legacy_schema(test_config)
    test_config.embedding_dim = 3
    service, legacy_index, context_index = _make_real_vector_service(
        test_config, store, engine=FixedVectorEngine()
    )
    old_legacy_id, old_context_id = _seed_pair(service, value="first version")

    result = service.legacy_replace(
        LegacyReplaceRequest(key="alpha", new_value="second version")
    )

    assert _index_ids(legacy_index) == {result.legacy_id}
    assert _index_ids(context_index) == {result.context_id}
    assert old_legacy_id not in _index_ids(legacy_index)
    assert old_context_id not in _index_ids(context_index)
    service.close()


def test_remove_archive_and_hard_delete_drop_both_mapped_entries(test_config, store):
    _create_legacy_schema(test_config)
    test_config.embedding_dim = 3
    service, legacy_index, context_index = _make_real_vector_service(
        test_config, store, engine=FixedVectorEngine()
    )

    archived_id, archived_context_id = _seed_pair(service, key="archived")
    service.legacy_archive(LegacyStatusRequest(legacy_id=archived_id))
    assert archived_id not in _index_ids(legacy_index)
    assert archived_context_id not in _index_ids(context_index)

    restored_id, restored_context_id = archived_id, archived_context_id
    service.legacy_restore(LegacyStatusRequest(legacy_id=restored_id))
    assert restored_id in _index_ids(legacy_index)
    assert restored_context_id in _index_ids(context_index)

    removed_id, removed_context_id = _seed_pair(service, key="removed")
    service.legacy_remove(LegacyRemoveRequest(legacy_id=removed_id))
    assert removed_id not in _index_ids(legacy_index)
    assert removed_context_id not in _index_ids(context_index)

    purged_id, purged_context_id = _seed_pair(service, key="purged")
    service.legacy_hard_delete(LegacyHardDeleteRequest(legacy_id=purged_id))
    assert purged_id not in _index_ids(legacy_index)
    assert purged_context_id not in _index_ids(context_index)

    # surviving entries were never rebuilt away
    assert _index_ids(legacy_index) == {restored_id}
    assert _index_ids(context_index) == {restored_context_id}
    assert legacy_index.is_dirty() is False
    assert context_index.is_dirty() is False
    service.close()


def test_legacy_vector_failure_keeps_sqlite_committed_and_marks_only_legacy_dirty(
    test_config, store, monkeypatch
):
    _create_legacy_schema(test_config)
    test_config.embedding_dim = 3
    service, legacy_index, context_index = _make_real_vector_service(
        test_config, store, engine=FixedVectorEngine()
    )

    def fail_to_add(mem_id, embedding):
        raise RuntimeError("legacy index write failed")

    monkeypatch.setattr(legacy_index, "add", fail_to_add)
    result = service.legacy_add(LegacyAddRequest(key="alpha", value="vectorized value"))

    assert result.changed is True
    assert _legacy_row(test_config, result.legacy_id)["status"] == "active"
    assert store.get_item(result.context_id).status is ContextStatus.ACTIVE
    assert legacy_index.is_dirty() is True
    assert context_index.is_dirty() is False
    assert _index_ids(context_index) == {result.context_id}
    status = service.status()
    assert status.legacy_vector_dirty is True
    assert status.context_vector_dirty is False
    service.close()


def test_context_vector_failure_keeps_sqlite_committed_and_marks_only_context_dirty(
    test_config, store, monkeypatch
):
    _create_legacy_schema(test_config)
    test_config.embedding_dim = 3
    service, legacy_index, context_index = _make_real_vector_service(
        test_config, store, engine=FixedVectorEngine()
    )

    def fail_to_add(mem_id, embedding):
        raise RuntimeError("context index write failed")

    monkeypatch.setattr(context_index, "add", fail_to_add)
    result = service.legacy_add(LegacyAddRequest(key="alpha", value="vectorized value"))

    assert result.changed is True
    assert _legacy_row(test_config, result.legacy_id)["status"] == "active"
    assert context_index.is_dirty() is True
    assert legacy_index.is_dirty() is False
    assert _index_ids(legacy_index) == {result.legacy_id}
    status = service.status()
    assert status.context_vector_dirty is True
    assert status.legacy_vector_dirty is False
    service.close()


def test_successful_sync_never_clears_a_preexisting_dirty_marker(test_config, store):
    _create_legacy_schema(test_config)
    test_config.embedding_dim = 3
    service, legacy_index, context_index = _make_real_vector_service(
        test_config, store, engine=FixedVectorEngine()
    )
    legacy_index.initialize(dim=3)
    legacy_index.mark_dirty()  # an earlier foreign failure still awaits a rebuild

    result = service.legacy_add(LegacyAddRequest(key="alpha", value="vectorized value"))

    assert result.legacy_id in _index_ids(legacy_index)
    assert legacy_index.is_dirty() is True  # the marker outlives a single sync
    assert context_index.is_dirty() is False
    status = service.status()
    assert status.legacy_vector_dirty is True
    assert status.context_vector_dirty is False
    service.close()


def test_unavailable_engine_commits_sqlite_and_marks_both_indexes_dirty(
    test_config, store
):
    _create_legacy_schema(test_config)
    test_config.embedding_dim = 3
    service, legacy_index, context_index = _make_real_vector_service(
        test_config, store, engine=None
    )

    result = service.legacy_add(LegacyAddRequest(key="alpha", value="vectorized value"))

    assert result.changed is True
    assert _legacy_row(test_config, result.legacy_id)["status"] == "active"
    assert store.get_item(result.context_id).status is ContextStatus.ACTIVE
    assert legacy_index.is_dirty() is True
    assert context_index.is_dirty() is True
    status = service.status()
    assert status.legacy_vector_dirty is True
    assert status.context_vector_dirty is True
    service.close()


def test_exploding_context_synchronizer_keeps_commit_and_marks_context_dirty(
    test_config, store
):
    """同步器自身爆炸（区别于单条同步失败）：mutation 正常返回已提交的 ID，
    Context 侧如实置 dirty 等待重建，绝不向调用方抛错（retry 会造成重复写）。"""
    _create_legacy_schema(test_config)
    test_config.embedding_dim = 3
    service, legacy_index, context_index = _make_real_vector_service(
        test_config, store, engine=FixedVectorEngine()
    )

    class ExplodingSynchronizer:
        def remove_l0(self, item_id):
            raise RuntimeError("synchronizer exploded")

        def upsert_active_l0(self, item_id, l0):
            raise RuntimeError("synchronizer exploded")

    service._synchronizer = ExplodingSynchronizer()
    result = service.legacy_add(LegacyAddRequest(key="alpha", value="vectorized value"))

    assert result.changed is True
    assert result.legacy_id is not None and result.context_id is not None
    assert _legacy_row(test_config, result.legacy_id)["status"] == "active"
    assert store.get_item(result.context_id).status is ContextStatus.ACTIVE
    assert context_index.is_dirty() is True
    assert legacy_index.is_dirty() is False
    status = service.status()
    assert status.context_vector_dirty is True
    assert status.legacy_vector_dirty is False
    service.close()

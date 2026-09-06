"""MemoryStore tests."""

import sqlite3

import pytest

from evolvmem.legacy_projection import (
    LegacyProjectionInsert,
    LegacyProjectionReplace,
    LegacyProjectionRepository,
    LegacyProjectionUpdate,
)
from evolvmem.memory_store import MemoryStore


@pytest.fixture
def store(test_config):
    with MemoryStore(test_config) as instance:
        yield instance


@pytest.fixture
def projection(store):
    """A repository borrowing the MemoryStore connection and transaction guard."""
    return LegacyProjectionRepository(store._conn, store._require_transaction)


def test_transaction_commits_all_writes(store):
    with store.transaction():
        first = store.add("project:x:fact:first", "第一条长期记录内容。")
        second = store.add("project:x:fact:second", "第二条长期记录内容。")
    assert store.get_by_id(first)["status"] == "active"
    assert store.get_by_id(second)["status"] == "active"


def test_transaction_rolls_back_all_writes(store):
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with store.transaction():
            store.add("project:x:fact:first", "第一条长期记录内容。")
            store.add("project:x:fact:second", "第二条长期记录内容。")
            raise RuntimeError("synthetic failure")
    assert store.get_by_key("project:x:fact:first") == []
    assert store.get_by_key("project:x:fact:second") == []


def test_replace_joins_outer_transaction_and_rolls_back(store):
    old_id = store.add("project:x:decision:api", "采用旧接口方案。")
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with store.transaction():
            store.replace(
                "project:x:decision:api",
                "采用新接口方案，因为它避免重复写入。",
            )
            raise RuntimeError("synthetic failure")
    assert store.get_by_id(old_id)["status"] == "active"
    records = store.get_by_key("project:x:decision:api")
    active_ids = [record["id"] for record in records if record["status"] == "active"]
    assert active_ids == [old_id]


class TestMemoryStore:
    def test_initialize_creates_tables(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        # Verify memories table exists
        rows = store._execute("SELECT name FROM sqlite_master WHERE type='table' AND name='memories'")
        assert len(rows) == 1
        store.close()

    def test_add_and_get_active(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        mem_id = store.add(
            key="project:test:fact:sample",
            value="test memory content",
            attribute="fact",
            tags=["test", "example"],
            source_session="sess_001",
        )
        assert mem_id == 1

        actives = store.get_active()
        assert len(actives) == 1
        assert actives[0]["key"] == "project:test:fact:sample"
        assert actives[0]["status"] == "active"
        store.close()

    def test_replace_supersedes_old(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        old_id = store.add(key="project:test:decision:x", value="plan A", attribute="decision")
        new_id = store.replace(key="project:test:decision:x", new_value="plan B, abandoned plan A")

        # Old record is superseded
        old = store.get_by_id(old_id)
        assert old["status"] == "superseded"
        assert old["superseded_by"] == new_id

        # New record is active
        new = store.get_by_id(new_id)
        assert new["status"] == "active"
        assert new["supersedes"] == old_id

        # get_active returns only one
        actives = store.get_active()
        assert len(actives) == 1
        assert actives[0]["value"] == "plan B, abandoned plan A"
        store.close()

    def test_remove_soft_delete(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        mem_id = store.add(key="project:test:temp", value="temporary content")
        store.remove(mem_id)
        record = store.get_by_id(mem_id)
        assert record["status"] == "deleted"
        # get_active does not return deleted
        assert len(store.get_active()) == 0
        store.close()

    def test_fts_search_finds_chinese(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        assert store._has_cjk("退款") is True

        store.add(key="p:a:fact:1", value="破损商品直接退款，不再补发", tags=["售后"])
        store.add(key="p:a:fact:2", value="用户偏好暗色主题界面", tags=["偏好"])
        store.add(key="p:a:fact:3", value="Python 版本需要 3.10 以上", tags=["技术"])

        results = store.search_fts("退款")
        assert len(results) == 1
        assert "破损商品" in results[0]["value"]
        store.close()

    def test_trigram_search_finds_chinese_substring(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:a:fact:1", value="破损商品直接退款，不再补发", tags=["售后"])

        results = store.search_fts("破损")         # trigram can match
        assert len(results) == 1
        results2 = store.search_fts("直接退款")     # phrase substring
        assert len(results2) == 1
        store.close()

    def test_fts_falls_back_to_like_when_trigram_unavailable(self, test_config):
        """When trigram tokenizer is unavailable, fall back to LIKE search."""
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:a:fact:1", value="破损商品直接退款，不再补发", tags=["test"])

        # Verify CJK detection works
        assert store._has_cjk("退款") is True

        # Simulate trigram unavailable: force LIKE path directly
        results = store._search_like("退款")
        assert len(results) == 1
        assert "破损商品" in results[0]["value"]
        store.close()

    def test_search_like_excludes_deleted(self, test_config):
        """_search_like should not return soft-deleted records."""
        store = MemoryStore(test_config)
        store.initialize()
        mem_id = store.add(key="p:del:test", value="破损商品直接退款", tags=["test"])
        store.remove(mem_id)
        # Deleted record should not appear in LIKE results
        results = store._search_like("退款")
        assert len(results) == 0
        store.close()

    def test_duplicate_key_skip(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:dup:test", value="first entry")
        # Same key, same value add_if_changed should skip
        result = store.add_if_changed(key="p:dup:test", value="first entry")
        assert result is None  # not written
        assert len(store.get_active()) == 1
        store.close()

    def test_update_access_count(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        mem_id = store.add(key="p:acc:test", value="access test")
        store.update_access(mem_id)
        record = store.get_by_id(mem_id)
        assert record["access_count"] == 1
        assert record["last_accessed"] is not None
        store.close()

    def test_schema_has_importance_and_tier(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        cols = {r[1] for r in store._execute("PRAGMA table_info(memories)")}
        assert "importance" in cols
        assert "tier" in cols
        store.close()

    def test_backfill_importance_by_attribute(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        cid = store.add(key="p:t:constraint:x", value="硬约束", attribute="constraint")
        did = store.add(key="p:t:decision:x", value="架构决策", attribute="decision")
        pid = store.add(key="p:t:preference:x", value="用户偏好", attribute="preference")
        fid = store.add(key="p:t:fact:x", value="普通事实", attribute="fact")

        store._backfill_importance_tier()

        assert store.get_by_id(cid)["importance"] == 8.0
        assert store.get_by_id(did)["importance"] == 7.0
        assert store.get_by_id(pid)["importance"] == 6.0
        assert store.get_by_id(fid)["importance"] == 5.0
        assert store.get_by_id(cid)["tier"] == "pinned"
        assert store.get_by_id(pid)["tier"] == "pinned"
        assert store.get_by_id(did)["tier"] == "normal"
        store.close()

    def test_migration_idempotent_on_reinitialize(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.close()
        # 二次、三次 initialize 不报错
        store2 = MemoryStore(test_config)
        store2.initialize()
        store2.close()
        store3 = MemoryStore(test_config)
        store3.initialize()
        store3.close()

    def test_add_with_importance_and_tier(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        mid = store.add(key="p:t:decision:db", value="用 PostgreSQL",
                        attribute="decision", importance=9.0, tier="pinned")
        rec = store.get_by_id(mid)
        assert rec["importance"] == 9.0
        assert rec["tier"] == "pinned"
        store.close()

    def test_replace_inherits_importance_tier(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:t:decision:db", value="用 MySQL",
                  attribute="decision", importance=9.0, tier="pinned")
        new_id = store.replace(key="p:t:decision:db", new_value="改用 PostgreSQL")
        rec = store.get_by_id(new_id)
        assert rec["importance"] == 9.0
        assert rec["tier"] == "pinned"
        store.close()

    def test_update_access_does_not_touch_updated_at(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        mid = store.add(key="p:acc:test2", value="access test")
        before = store.get_by_id(mid)["updated_at"]
        import time
        time.sleep(1.1)  # updated_at 精度为秒
        store.update_access(mid)
        after = store.get_by_id(mid)
        assert after["access_count"] == 1
        assert after["updated_at"] == before
        store.close()

    def test_replace_inherits_tags_and_attribute(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:t:decision:db", value="用 MySQL",
                  attribute="decision", tags=["db", "arch"],
                  importance=9.0, tier="pinned")
        new_id = store.replace(key="p:t:decision:db", new_value="改用 PostgreSQL")
        rec = store.get_by_id(new_id)
        assert rec["attribute"] == "decision"
        assert rec["tags"] == "db,arch"
        store.close()

    def test_replace_explicit_tags_override(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:t:fact:x", value="v1", tags=["old"])
        new_id = store.replace(key="p:t:fact:x", new_value="v2", tags=["new"])
        assert store.get_by_id(new_id)["tags"] == "new"
        store.close()

    def test_update_metadata(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        mid = store.add(key="p:t:fact:m", value="v")
        store.update_metadata(mid, importance=8.5, tier="pinned")
        rec = store.get_by_id(mid)
        assert rec["importance"] == 8.5
        assert rec["tier"] == "pinned"
        store.update_metadata(mid, importance=3.0)
        rec2 = store.get_by_id(mid)
        assert rec2["importance"] == 3.0
        assert rec2["tier"] == "pinned"  # 未指定的字段不变
        store.close()

    def test_expired_memory_not_in_get_active(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:t:fact:temp", value="临时事实",
                  expires_at="2020-01-01 00:00:00")
        store.add(key="p:t:fact:durable", value="长期事实")
        actives = store.get_active()
        assert len(actives) == 1
        assert actives[0]["key"] == "p:t:fact:durable"
        store.close()

    def test_count_active_excludes_expired(self, test_config):
        store = MemoryStore(test_config)
        store.initialize()
        store.add(key="p:t:fact:temp", value="临时事实",
                  expires_at="2020-01-01 00:00:00")
        store.add(key="p:t:fact:durable", value="长期事实")
        assert store.count_active() == 1
        store.close()


class TestLegacyProjectionRepository:
    """A borrowed-connection projection repository with no lifecycle powers."""

    def test_borrows_an_existing_connection_and_transaction_guard(
        self, store, projection
    ):
        with store.transaction():
            legacy_id = projection.insert(
                LegacyProjectionInsert(key="p:b:fact:1", value="借连接写入")
            )

        assert projection.get_by_id(legacy_id)["value"] == "借连接写入"
        assert store.get_by_id(legacy_id) == projection.get_by_id(legacy_id)

    def test_rejects_a_non_connection_or_non_callable_guard(self, store):
        with pytest.raises(TypeError):
            LegacyProjectionRepository(object(), store._require_transaction)
        with pytest.raises(TypeError):
            LegacyProjectionRepository(store._conn, "not-a-guard")

    def test_has_no_lifecycle_or_commit_powers(self, projection):
        for forbidden in ("initialize", "close", "commit", "transaction"):
            assert not hasattr(projection, forbidden)

    def test_reads_preserve_legacy_dict_fields_and_order(self, store, projection):
        first = store.add(
            key="p:r:fact:1",
            value="第一条记录内容",
            attribute="fact",
            tags=["a", "b"],
            source_session="s1",
            importance=6.5,
            tier="pinned",
        )
        second = store.add(key="p:r:fact:2", value="第二条记录内容")
        expired = store.add(
            key="p:r:fact:exp", value="过期记录", expires_at="2000-01-01 00:00:00"
        )
        store.update_access(first)

        assert list(projection.get_by_id(first)) == [
            "id", "key", "value", "status", "attribute", "tags",
            "source_session", "access_count", "last_accessed", "supersedes",
            "superseded_by", "created_at", "updated_at", "importance", "tier",
            "expires_at",
        ]
        assert projection.get_by_id(first) == store.get_by_id(first)
        assert projection.get_by_id(9999) is None
        assert projection.get_by_key("p:r:fact:1") == store.get_by_key("p:r:fact:1")
        assert projection.get_by_ids([second, first, 9999]) == store.get_by_ids(
            [second, first, 9999]
        )
        assert projection.get_by_ids([]) == []
        assert projection.get_active() == store.get_active()
        assert projection.all_ids() == store.all_ids()
        assert projection.count_active() == store.count_active()
        assert projection.get_forgetting_candidates(
            days_threshold=0, access_threshold=100, rate_limit_days=0
        ) == store.get_forgetting_candidates(0, 100, 0)
        assert projection.get_expired_ids("2026-01-01 00:00:00") == [expired]
        assert projection.get_expired_ids("1999-01-01 00:00:00") == []

    def test_search_matches_legacy_fts_and_like_results(self, store, projection):
        store.add(key="p:s:fact:1", value="破损商品直接退款，不再补发", tags=["售后"])
        store.add(key="p:s:fact:2", value="Python 版本需要 3.10 以上")
        deleted = store.add(key="p:s:fact:3", value="已删除的退款记录")
        store.remove(deleted)

        assert projection.search_fts("退款") == store.search_fts("退款")
        assert [row["key"] for row in projection.search_fts("退款")] == ["p:s:fact:1"]
        assert projection.search_fts("Python") == store.search_fts("Python")
        assert projection.search_fts("破损") == store.search_fts("破损")

    @pytest.mark.parametrize(
        "operation",
        [
            "insert",
            "replace",
            "soft_delete",
            "set_status",
            "update_metadata",
            "update_access",
            "hard_delete",
        ],
    )
    def test_writes_require_the_owner_transaction(
        self, store, projection, operation
    ):
        existing = store.add(key="p:g:fact:1", value="v1", tags=["old"])
        calls = {
            "insert": lambda: projection.insert(
                LegacyProjectionInsert(key="p:g:fact:2", value="v2")
            ),
            "replace": lambda: projection.replace(
                LegacyProjectionReplace(key="p:g:fact:1", new_value="v2")
            ),
            "soft_delete": lambda: projection.soft_delete(existing),
            "set_status": lambda: projection.set_status(existing, "archived"),
            "update_metadata": lambda: projection.update_metadata(
                LegacyProjectionUpdate(existing, importance=8.0)
            ),
            "update_access": lambda: projection.update_access((existing,)),
            "hard_delete": lambda: projection.hard_delete(existing),
        }

        with pytest.raises(RuntimeError, match="transaction"):
            calls[operation]()

        row = store.get_by_id(existing)
        assert row["status"] == "active"
        assert row["importance"] == 5.0
        assert row["access_count"] == 0
        assert store.get_by_key("p:g:fact:2") == []

    def test_writes_roll_back_with_the_owner_transaction(self, store, projection):
        with pytest.raises(RuntimeError, match="synthetic failure"):
            with store.transaction():
                legacy_id = projection.insert(
                    LegacyProjectionInsert(key="p:rb:fact:1", value="会回滚")
                )
                raise RuntimeError("synthetic failure")

        assert projection.get_by_id(legacy_id) is None
        assert store.get_by_key("p:rb:fact:1") == []

    def test_insert_normalizes_csv_tags_date_only_expiry_and_autoincrement(
        self, store, projection
    ):
        with store.transaction():
            first = projection.insert(
                LegacyProjectionInsert(
                    key="p:i:fact:1",
                    value="v1",
                    tags=["a", "b"],
                    expires_at="2031-04-05",
                )
            )
            second = projection.insert(
                LegacyProjectionInsert(key="p:i:fact:2", value="v2")
            )

        row = projection.get_by_id(first)
        assert row["tags"] == "a,b"
        assert row["expires_at"] == "2031-04-05 00:00:00"
        assert row["created_at"] and row["updated_at"]
        assert row["status"] == "active"
        assert second > first

    def test_replace_inherits_omitted_fields_and_links_predecessor(
        self, store, projection
    ):
        old_id = store.add(
            key="p:rp:decision:db",
            value="用 MySQL",
            attribute="decision",
            tags=["db", "arch"],
            importance=9.0,
            tier="pinned",
            expires_at="2031-04-05 06:07:08",
            source_session="s-old",
        )

        with store.transaction():
            previous, new_id = projection.replace(
                LegacyProjectionReplace(key="p:rp:decision:db", new_value="改用 PostgreSQL")
            )

        assert previous == old_id
        old = projection.get_by_id(old_id)
        new = projection.get_by_id(new_id)
        assert old["status"] == "superseded"
        assert old["superseded_by"] == new_id
        assert new["status"] == "active"
        assert new["supersedes"] == old_id
        assert new["attribute"] == "decision"
        assert new["tags"] == "db,arch"
        assert new["importance"] == 9.0
        assert new["tier"] == "pinned"
        assert new["expires_at"] == "2031-04-05 06:07:08"

    def test_replace_without_predecessor_inserts_and_reports_none(
        self, store, projection
    ):
        with store.transaction():
            previous, new_id = projection.replace(
                LegacyProjectionReplace(
                    key="p:rp:fact:new",
                    new_value="v2",
                    tags=["x"],
                    expires_at="2031-04-05",
                )
            )

        assert previous is None
        row = projection.get_by_id(new_id)
        assert row["status"] == "active"
        assert row["supersedes"] is None
        assert row["tags"] == "x"
        assert row["expires_at"] == "2031-04-05 00:00:00"

    def test_soft_delete_nonexistent_is_a_compatible_noop(self, store, projection):
        existing = store.add(key="p:sd:fact:1", value="待软删除")

        with store.transaction():
            assert projection.soft_delete(9999) is False
            assert projection.soft_delete(existing) is True

        assert projection.get_by_id(existing)["status"] == "deleted"
        assert projection.get_active() == []
        assert projection.search_fts("待软删除") == []

    def test_set_status_supports_archive_and_restore(self, store, projection):
        existing = store.add(key="p:ss:fact:1", value="状态切换")

        with store.transaction():
            assert projection.set_status(existing, "archived") is True
        assert projection.get_by_id(existing)["status"] == "archived"
        with store.transaction():
            assert projection.set_status(existing, "active") is True
            assert projection.set_status(9999, "active") is False
        assert projection.get_by_id(existing)["status"] == "active"

    def test_update_metadata_updates_only_supplied_fields(self, store, projection):
        existing = store.add(key="p:um:fact:1", value="v")

        with store.transaction():
            assert (
                projection.update_metadata(
                    LegacyProjectionUpdate(existing, importance=8.5)
                )
                is True
            )
        row = projection.get_by_id(existing)
        assert row["importance"] == 8.5
        assert row["tier"] == "normal"
        with store.transaction():
            assert (
                projection.update_metadata(
                    LegacyProjectionUpdate(existing, tier="pinned")
                )
                is True
            )
            assert (
                projection.update_metadata(
                    LegacyProjectionUpdate(9999, importance=1.0)
                )
                is False
            )
        row = projection.get_by_id(existing)
        assert row["importance"] == 8.5
        assert row["tier"] == "pinned"

    def test_update_access_skips_unknown_ids_and_keeps_updated_at(
        self, store, projection
    ):
        first = store.add(key="p:ua:fact:1", value="v1")
        before = store.get_by_id(first)["updated_at"]

        with store.transaction():
            updated = projection.update_access((first, 9999))

        assert updated == (first,)
        row = projection.get_by_id(first)
        assert row["access_count"] == 1
        assert row["last_accessed"] is not None
        assert row["updated_at"] == before
        with store.transaction():
            assert projection.update_access(()) == ()

    def test_hard_delete_removes_row_and_fts_entries(self, store, projection):
        legacy_id = store.add(key="p:hd:fact:1", value="彻底删除的目标内容", tags=["清理"])
        assert projection.search_fts("彻底删除")

        with store.transaction():
            assert projection.hard_delete(legacy_id) is True
            assert projection.hard_delete(9999) is False

        assert projection.get_by_id(legacy_id) is None
        assert projection.search_fts("彻底删除") == []
        assert store._conn.execute(
            "SELECT COUNT(*) FROM memories_fts WHERE rowid=?", (legacy_id,)
        ).fetchone()[0] == 0

    def test_projection_never_commits_the_owner_transaction(
        self, store, projection, test_config
    ):
        with store.transaction():
            legacy_id = projection.insert(
                LegacyProjectionInsert(key="p:nc:fact:1", value="未提交不可见")
            )
            other = sqlite3.connect(test_config.db_path)
            try:
                assert other.execute(
                    "SELECT COUNT(*) FROM memories WHERE key='p:nc:fact:1'"
                ).fetchone()[0] == 0
            finally:
                other.close()

        assert projection.get_by_id(legacy_id) is not None

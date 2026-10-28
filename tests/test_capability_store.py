"""Capability memory persistence, provenance and category boundaries."""
import pytest

from evolvmem.capability_models import CAPABILITY_CATEGORIES
from evolvmem.capability_store import CapabilityMemoryDraft, CapabilityStore
from evolvmem.context_store import ContextStore


def draft(**changes):
    values = dict(
        category="frontend",
        title="复杂表单状态管理",
        content="按字段拆分状态并集中校验。",
        keywords=("Vue", "表单"),
        source_project="bluewhale",
        source_ref="archive:1",
        occurred_at="2026-10-01 08:00:00",
    )
    values.update(changes)
    return CapabilityMemoryDraft(**values)


def test_capability_store_keeps_source_project_without_project_filter(test_config):
    with ContextStore(test_config) as store:
        capabilities = CapabilityStore(store)
        first = capabilities.upsert(draft())

        assert capabilities.upsert(draft()) == first
        hit = capabilities.search("表单", categories=("frontend",))[0]
        assert hit["memory_id"] == first
        assert hit["source_project"] == "bluewhale"
        assert hit["source_ref"] == "archive:1"
        assert hit["keywords"] == ["Vue", "表单"]
        assert capabilities.search("表单", categories=("backend",)) == []
        assert {c["id"] for c in capabilities.categories()} == set(CAPABILITY_CATEGORIES)


def test_capability_store_searches_all_categories_without_source_project_filter(test_config):
    with ContextStore(test_config) as store:
        capabilities = CapabilityStore(store)
        capabilities.upsert(draft())
        capabilities.upsert(draft(
            category="database", title="MySQL重复写入治理", content="使用唯一索引并校验事务结果。",
            keywords=("MySQL",), source_project="integration", source_ref="archive:2",
        ))

        assert len(capabilities.search("校验")) == 2
        assert capabilities.search("MySQL")[0]["source_project"] == "integration"


def test_capability_store_status_uses_revision_and_never_searches_inactive(test_config):
    with ContextStore(test_config) as store:
        capabilities = CapabilityStore(store)
        memory_id = capabilities.upsert(draft())

        assert not capabilities.set_status(memory_id, "archived", expected_revision=0)
        assert capabilities.set_status(memory_id, "archived", expected_revision=1)
        assert capabilities.get(memory_id)["revision"] == 2
        assert capabilities.search("表单") == []


def test_capability_store_rejects_unknown_category_and_only_suggests_new_one(test_config):
    with ContextStore(test_config) as store:
        capabilities = CapabilityStore(store)
        with pytest.raises(ValueError, match="capability_category_not_found"):
            capabilities.upsert(draft(category="mobile"))

        memory_id = capabilities.upsert(draft())
        suggestion_id = capabilities.suggest_category("移动端", "出现独立移动开发记忆", memory_id)

        assert suggestion_id > 0
        assert {c["id"] for c in capabilities.categories()} == set(CAPABILITY_CATEGORIES)
        suggestion = store._connection().execute(
            "SELECT * FROM capability_category_suggestions WHERE suggestion_id=?",
            (suggestion_id,),
        ).fetchone()
        assert suggestion["status"] == "pending"
        assert suggestion["source_memory_id"] == memory_id

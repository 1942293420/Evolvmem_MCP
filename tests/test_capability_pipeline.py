"""Development records enter capability storage without project ownership gates."""
from types import SimpleNamespace

from evolvmem.capability_pipeline import CapabilityPipeline
from evolvmem.capability_store import CapabilityStore
from evolvmem.context_store import ContextStore, _now_iso


def service_with_task(test_config, store, text="蓝鲸前端新增表单校验，并修复MySQL重复写入。"):
    now = _now_iso()
    with store.transaction():
        task_id = store._connection().execute(
            "INSERT INTO organization_tasks(source_key,source_revision,rule_revision,created_at,updated_at) "
            "VALUES('archive:1','r1','rules1',?,?)", (now, now)).lastrowid
        store._connection().execute(
            "INSERT INTO organization_units(task_id,ordinal,digest,title,text,source_start,source_end,"
            "project,created_at,updated_at) VALUES(?,0,'d1','开发记录',?,0,?, 'bluewhale',?,?)",
            (task_id, text, len(text), now, now))
    service = SimpleNamespace(store=store, config=test_config)
    task = store._connection().execute("SELECT * FROM organization_tasks WHERE id=?", (task_id,)).fetchone()
    return service, task


def test_capability_pipeline_saves_category_and_source_project(test_config):
    with ContextStore(test_config) as store:
        service, task = service_with_task(test_config, store)
        def provider(prompt):
            return {"action":"keep", "category":"frontend", "title":"前端表单校验",
                    "content":"提交前统一校验字段。", "keywords":["Vue","表单"], "occurred_at":None}

        result = CapabilityPipeline(service).process_task(task, provider=provider)

        memory = CapabilityStore(store).get(result["memory_ids"][0])
        assert memory["category"] == "frontend"
        assert memory["source_project"] == "bluewhale"
        assert memory["source_ref"].startswith("archive:1#")
        assert result["pending"] == 0


def test_unknown_category_is_persisted_as_pending_intake(test_config):
    with ContextStore(test_config) as store:
        service, task = service_with_task(test_config, store)
        def provider(prompt):
            return {"action":"keep", "category":"mobile", "title":"移动端离线页面",
                    "content":"离线时保留页面草稿。", "keywords":["移动端"]}

        result = CapabilityPipeline(service).process_task(task, provider=provider)

        assert result["pending"] == 1
        row = store._connection().execute("SELECT * FROM capability_intake").fetchone()
        assert row["title"] == "移动端离线页面"
        assert row["suggested_category"] == "mobile"
        assert row["source_project"] == "bluewhale"
        assert CapabilityStore(store).search("离线") == []


def test_non_development_content_is_ignored(test_config):
    with ContextStore(test_config) as store:
        service, task = service_with_task(test_config, store, "今天闲聊并确认周末安排。")
        result = CapabilityPipeline(service).process_task(task, provider=lambda _: {"action":"ignore"})
        assert result["memory_ids"] == []
        assert store._connection().execute("SELECT count(*) FROM capability_memories").fetchone()[0] == 0


def test_capability_pipeline_syncs_vector_after_memory_write(test_config):
    from unittest.mock import Mock

    with ContextStore(test_config) as store:
        service, task = service_with_task(test_config, store)
        index = SimpleNamespace(sync=Mock(return_value="synchronized"))
        service.capability_index = lambda: index

        result = CapabilityPipeline(service).process_task(task, provider=lambda _: {
            "action":"keep", "category":"frontend", "title":"前端表单校验",
            "content":"提交前统一校验字段。", "keywords":["Vue"]})

        memory = CapabilityStore(store).get(result["memory_ids"][0])
        index.sync.assert_called_once_with(memory["memory_id"], memory["title"] + "\n" + memory["content"])

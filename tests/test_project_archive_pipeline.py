"""Project archive writes are independent from capability writes."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from evolvmem.capability_pipeline import CapabilityPipeline
from evolvmem.capability_store import CapabilityStore
from evolvmem.context_store import ContextStore
from evolvmem.project_archive_pipeline import ProjectArchivePipeline
from evolvmem.project_archive_store import ProjectArchiveStore
from tests.test_capability_pipeline import service_with_task


def provider(prompt):
    return {"events":[{"event_type":"feature", "title":"新增表单校验",
                       "content":"蓝鲸完成前端表单校验。", "occurred_at":"2026-10-01 00:00:00"}]}


def test_project_archive_pipeline_records_events_and_advances_watermark(test_config):
    with ContextStore(test_config) as store:
        service, task = service_with_task(test_config, store)
        result = ProjectArchivePipeline(service).process_task(task, provider=provider)

        archive = ProjectArchiveStore(store).get("bluewhale")
        assert len(result["event_ids"]) == 1
        assert "新增表单校验" in archive["summary"]
        assert archive["source_watermark"] == "archive:1:1"
        assert archive["revision"] == result["summary_revision"]


def test_archive_failure_does_not_roll_back_capability_or_advance_watermark(test_config):
    with ContextStore(test_config) as store:
        service, task = service_with_task(test_config, store)
        CapabilityPipeline(service).process_task(task, provider=lambda _: {
            "action":"keep", "category":"frontend", "title":"表单组件",
            "content":"使用可复用表单组件。", "keywords":["Vue"]})

        with patch.object(ProjectArchiveStore, "rebuild", side_effect=RuntimeError("model_down")):
            with pytest.raises(RuntimeError, match="model_down"):
                ProjectArchivePipeline(service).process_task(task, provider=provider)

        assert CapabilityStore(store).search("表单组件")
        assert ProjectArchiveStore(store).get("bluewhale") is None
        assert store._connection().execute("SELECT count(*) FROM project_events").fetchone()[0] == 1

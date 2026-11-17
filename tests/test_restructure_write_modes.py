"""All modes resolve ownership; only legacy/dual materialize the legacy view."""
from unittest.mock import Mock

import pytest

from evolvmem import auto_organization as org
from tests.test_auto_organization import base_service, service
from tests.test_primary_ownership import prepare


@pytest.mark.parametrize("mode, new_memories, legacy_calls", [
    ("legacy", 0, 1), ("dual", 1, 1), ("primary", 1, 0),
])
def test_write_modes_share_ownership_but_keep_materialization_boundary(
        service, monkeypatch, mode, new_memories, legacy_calls):
    worker, task, provider, _ = prepare(service, monkeypatch,
                                        ["evo 增加写入校验。"], mode=mode)
    legacy = Mock()
    monkeypatch.setattr(worker, "_ingest", legacy)
    monkeypatch.setattr(org, "_refresh_task_state", Mock())

    worker._process(service, task)

    units = org.unit_views(service, task["id"])
    assert units[0]["project"] == "evo"
    assert units[0]["decision"] == "auto"
    assert len(provider.ownership_batches) == 1
    assert service.store._connection().execute("SELECT count(*) FROM capability_memories").fetchone()[0] == new_memories
    assert service.store._connection().execute("SELECT count(*) FROM project_events").fetchone()[0] == new_memories
    assert legacy.call_count == legacy_calls

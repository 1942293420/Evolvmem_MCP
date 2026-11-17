import pytest

from evolvmem.context_models import ContextSessionStartRequest, ContextSessionStartResult, ContextValidationError
from evolvmem.lan_tools import LanTools
from tests.test_capability_retriever import service, add_capability


def test_primary_capability_ids_are_not_context_feedback_ids(service):
    identity = add_capability(service)
    service.config.knowledge_structure_mode = "primary"
    result = service.session_start(ContextSessionStartRequest(project="bluewhale", query="Vue表单"))
    assert result.selected_ids == ()
    assert result.capability_ids == (identity,)
    assert "capability:" + str(identity) in result.block


def test_lan_keeps_context_and_capability_namespaces_separate():
    value = LanTools._qualify({"selected_ids": [7], "capability_ids": [7]}, "personal", "context_session_start")
    assert value["selected_refs"] == ["personal:context:7"]
    assert value["capability_refs"] == ["personal:capability:7"]
    hit = LanTools._qualify({"results": [{"memory_id": 7, "sources": [{"context_item_id": 9}]}]}, "personal", "capability_search")["results"][0]
    assert hit["ref"] == "personal:capability:7"
    assert hit["sources"] == [{"context_item_id": 9}]


@pytest.mark.parametrize("bad", [(True,), (0,), ("7",)])
def test_session_result_rejects_invalid_capability_ids(bad):
    with pytest.raises(ContextValidationError):
        ContextSessionStartResult(block="", selected_ids=(), used_chars=0, excluded_counts=(), capability_ids=bad)

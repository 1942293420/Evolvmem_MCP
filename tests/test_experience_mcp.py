"""MCP boundary exercises real case writes, recall and source-linked feedback."""
import pytest
from tests.test_continuity_mcp import _make_server
from tests.test_experience_service import case, proof, experiences
from evolvmem.context_models import ContextMode
from evolvmem.mcp_contract import tool_specs

@pytest.fixture
def server(test_config, experiences):
    value = _make_server(test_config, mode='shadow', adapter='codex')
    value.context_service._experiences = experiences
    yield value
    value.shutdown()


def test_mcp_case_recall_and_evidence_roundtrip(server):
    exp = server.context_service.experiences()
    result = server.handle_tool_call('experience_record', {'case':case(), 'evidence':proof(exp)})
    assert result['status'] == 'active'
    recalled = server.handle_tool_call('experience_recall', {'project':'demo','query':'列表打开缓慢'})
    assert recalled['results'][0]['id'] == result['id']
    feedback = {'id':result['id'], **proof(exp, 'second-task','second-event')}
    saved = server.handle_tool_call('context_record_outcome', feedback)
    assert saved['validation_level'] == 'repeated_verified'
    assert server.handle_tool_call('context_record_outcome', feedback)['success_count'] == 2


def test_mcp_rejects_invalid_case_without_partial_write(server):
    result = server.handle_tool_call('experience_record', {'case':case(), 'evidence':{'outcome':'success'}})
    assert 'error' in result
    assert server.context_service.store.list_item_ids() == []


def test_dsh_has_experience_tools_when_core_ready(server):
    names = {s.name for s in tool_specs(adapter='dsh',mode=ContextMode.SHADOW,
                                        health=server.context_service.status())}
    assert {'experience_record','experience_recall','context_record_outcome'} <= names


def test_recall_schema_guides_fact_only_constraints(server):
    spec = next(s for s in tool_specs(
        adapter='codex', mode=ContextMode.SHADOW,
        health=server.context_service.status()) if s.name == 'experience_recall')
    guidance = spec.input_schema['properties']['constraints']['description']
    assert 'directly observed' in guidance
    assert 'requested actions' in guidance
    assert 'uncertain causes' in guidance


def test_generic_feedback_and_confirm_cannot_activate_unverified_case(server):
    created = server.handle_tool_call('experience_record', {'case':case()})
    item_id = created['id']
    for name, args in [('context_record_outcome', {'id':item_id,'outcome':'success'}),
                       ('context_confirm', {'id':item_id})]:
        assert 'error' in server.handle_tool_call(name, args)
    core = server.context_service
    from evolvmem.context_lifecycle import ContextLifecycleError
    with pytest.raises(ContextLifecycleError):
        core.record_outcome(item_id, 'success')
    with pytest.raises(ContextLifecycleError):
        core.confirm(item_id)
    assert core.experiences().read(item_id)['status'] == 'candidate'
    assert core.experiences().read(item_id)['success_count'] == 0


def test_structured_feedback_missing_event_returns_actionable_requirements(server):
    created = server.handle_tool_call('experience_record', {'case':case()})
    result = server.handle_tool_call('context_record_outcome', {
        'id':created['id'], 'outcome':'inapplicable', 'task_id':'current',
        'source_kind':'user_confirmation', 'quote':'这里没有索引',
        'note':'用户指出场景不同', 'conditions':{'index':'absent'}})
    assert result['error'] == 'invalid_arguments'
    assert 'event_id' in result['requirements']
    assert 'native session' in result['requirements']

"""Continuation has its own readiness gate; real Context gates stay live."""
import pytest

from tests.test_lan_sharing import lan  # noqa: F401
from tests.test_lan_context import PATH
from evolvmem.context_models import ContextMode

NAMES = {'continuity_begin', 'continuity_find', 'continuity_resume',
         'continuity_checkpoint', 'continuity_list'}


@pytest.mark.parametrize('user', ['jiangli', 'kane'])
def test_real_continuity_calls_do_not_evaluate_unrelated_context_health(lan, monkeypatch, user):
    runtime, adapter = lan
    server = runtime.server_for(user)
    evaluations = []
    evaluate = server._evaluate_live_status
    def observed():
        evaluations.append(True)
        return evaluate()
    monkeypatch.setattr(server, '_evaluate_live_status', observed)
    common = dict(workspace_path=PATH, device_id='continuity-cpu-test')
    begin_args = dict(**common, project='demo', objective='Verify exact continuation state', request_id='cpu-begin')
    first = adapter.call_tool(user, 'continuity_begin', begin_args)
    assert 'error' not in first, first
    assert adapter.call_tool(user, 'continuity_begin', begin_args)['workstream_id'] == first['workstream_id']
    assert adapter.call_tool(user, 'continuity_resume', dict(**common, project_hint='demo'))['workstream_id'] == first['workstream_id']
    found = adapter.call_tool(user, 'continuity_find', dict(**common, query='demo'))
    assert found['code'] == 'ok', found
    listed = adapter.call_tool(user, 'continuity_list', dict(**common, project_hint='demo'))
    assert listed['count'] == 1, listed
    update = dict(**common, action='update', project_hint='demo', workstream_id=first['workstream_id'],
                  expected_checkpoint_revision=first['checkpoint_revision'], expected_state_version=first['state_version'],
                  completed_steps=['Actual SQLite checkpoint verified'], request_id='cpu-update')
    saved = adapter.call_tool(user, 'continuity_checkpoint', update)
    assert 'error' not in saved, saved
    assert adapter.call_tool(user, 'continuity_checkpoint', update)['checkpoint_revision'] == saved['checkpoint_revision']
    stale = adapter.call_tool(user, 'continuity_checkpoint', {**update, 'request_id': 'cpu-stale'})
    assert stale['error'] == 'revision_conflict', stale
    assert evaluations == [], 'continuation registration scanned unrelated Context health'


def test_continuity_schema_and_visibility_match_live_service_mode_adapter(lan, monkeypatch):
    runtime, adapter = lan
    server = runtime.server_for('kane')
    service = server.context_service
    # Injected/static service fields are the ones status() exposes. Deliberately
    # keep the server fallback different so the fast path cannot use stale config.
    monkeypatch.setattr(server, 'adapter', 'mcp')
    monkeypatch.setattr(server, 'context_mode', ContextMode.LEGACY)
    for actual_adapter in ('codex', 'kimi', 'dsh', 'mcp'):
        for actual_mode in (ContextMode.LEGACY, ContextMode.COMPAT, ContextMode.SHADOW, ContextMode.PRIMARY):
            monkeypatch.setattr(service, '_adapter', actual_adapter)
            monkeypatch.setattr(service, '_mode', actual_mode)
            monkeypatch.setattr(server, '_contract_view', lambda: (service._adapter, service._mode, None))
            regular = adapter._specs('kane')
            fast = adapter._specs('kane', continuity_only=True)
            assert {k: v for k, v in fast.items() if k in NAMES} == {k: v for k, v in regular.items() if k in NAMES}
    # No initialized service falls back to the server's fail-closed config.
    monkeypatch.setattr(service, '_mode', None)
    monkeypatch.setattr(server, 'context_mode', None)
    monkeypatch.setattr(server, 'adapter', 'codex')
    assert not NAMES.intersection(adapter._specs('kane', continuity_only=True))


def test_core_context_reads_keep_their_health_evaluation(lan, monkeypatch):
    runtime, adapter = lan
    server = runtime.server_for('kane')
    evaluations = []
    evaluate = server._evaluate_live_status
    def observed():
        evaluations.append(True)
        return evaluate()
    monkeypatch.setattr(server, '_evaluate_live_status', observed)
    result = adapter.call_tool('kane', 'context_search', dict(query='No synthetic item yet', space='personal'))
    assert 'error' not in result, result
    assert evaluations, 'actual Context data served without its health gate'

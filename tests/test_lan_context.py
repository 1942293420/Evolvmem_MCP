"""Remote continuity exercises real personal SQLite; only OS access is guarded."""
import json
import pytest
from tests.test_lan_sharing import lan
from evolvmem.lan_runtime import LanRuntime
from evolvmem.lan_tools import LanTools

PATH = r'C:\Users\Kane\src\demo'
SNAP = {'kind': 'git', 'branch': 'main', 'root_commit': 'a' * 40, 'head_commit': 'b' * 40}


def begin(adapter, user='kane', device='pc1', rid='begin', **extra):
    return adapter.call_tool(user, 'continuity_begin', dict(workspace_path=PATH, device_id=device,
        project='demo', objective='Finish the remote demonstration', request_id=rid, **extra))


def resume(adapter, user='kane', device='pc1', **extra):
    return adapter.call_tool(user, 'continuity_resume', dict(workspace_path=PATH, device_id=device,
        project_hint='demo', **extra))


def test_remote_identity_isolated_durable_no_local_path_access(lan, monkeypatch):
    runtime, adapter = lan
    import evolvmem.continuity_service as core
    from evolvmem.workspace_identity import WorkspaceIdentityProvider
    def forbidden(*a, **kw):
        raise AssertionError('remote path reached local filesystem')
    monkeypatch.setattr(core, '_git', forbidden)
    monkeypatch.setattr(WorkspaceIdentityProvider, '_canonical_identity_bytes', forbidden)
    k = begin(adapter)
    assert 'error' not in k, k
    j = begin(adapter, 'jiangli')
    d = begin(adapter, device='pc2', rid='device2')
    assert len({k['workstream_id'], j['workstream_id'], d['workstream_id']}) == 3
    assert resume(adapter)['workstream_id'] == k['workstream_id']
    assert resume(adapter)['staleness'] == 'unknown'
    assert PATH not in json.dumps(resume(adapter))
    session = adapter.call_tool('kane', 'context_session_start', dict(project='demo', query='继续原任务', workspace_path=PATH, device_id='pc1'))
    assert k['workstream_id'] in json.dumps(session), session
    runtime.close()
    restarted = LanRuntime(runtime.settings)
    restarted.initialize()
    try:
        assert resume(LanTools(restarted))['workstream_id'] == k['workstream_id']
    finally:
        restarted.close()


def test_snapshot_validation_unknown_ancestry_and_handoff(lan):
    runtime, adapter = lan
    k = begin(adapter, repo_snapshot=SNAP)
    assert 'error' not in k, k
    assert resume(adapter, repo_snapshot=SNAP)['repo_source'] == 'client_reported'
    assert resume(adapter, repo_snapshot={**SNAP, 'head_commit': 'c' * 40})['staleness'] == 'unknown'
    for bad in ({'kind': 'git'}, {**SNAP, 'head_commit': 'invalid'}, {**SNAP, 'extra': '/etc'}):
        assert begin(adapter, device='bad', rid=str(len(json.dumps(bad))), repo_snapshot=bad)['error'] == 'invalid_repo_snapshot'
    bind = dict(workspace_path=PATH, device_id='pc2', project='demo', workstream_id=k['workstream_id'], repo_snapshot=SNAP, request_id='bind')
    bound = adapter.call_tool('kane', 'continuity_bind', bind)
    assert bound['bound'] is True
    assert bound['target_focused'] is True and bound['focus_switch'] is None
    assert resume(adapter, device='pc2', repo_snapshot=SNAP)['workstream_id'] == k['workstream_id']
    assert adapter.call_tool('kane', 'continuity_bind', bind)['bound'] is True
    for user, changes in [('jiangli', {}), ('kane', {'project': 'other'}), ('kane', {'repo_snapshot': {**SNAP, 'root_commit': 'c'*40}}), ('kane', {'repo_snapshot': None})]:
        assert 'error' in adapter.call_tool(user, 'continuity_bind', {**bind, **changes, 'request_id': 'reject' + user + str(len(str(changes)))})
    assert adapter.call_tool('kane', 'continuity_checkpoint', dict(action='update', workspace_path=PATH, device_id='pc1', project_hint='demo', workstream_id=k['workstream_id'], expected_checkpoint_revision=99, expected_state_version=99, request_id='stale'))['error']


def test_remote_missing_device_fails_before_mutation(lan):
    runtime, adapter = lan
    result = adapter.call_tool('kane', 'continuity_begin', dict(workspace_path=PATH, project='demo', objective='demo', request_id='missing'))
    assert result['error'] == 'invalid_device_id'
    assert runtime.server_for('kane').context_service.store._connection().execute('SELECT count(*) FROM continuity_workstreams').fetchone()[0] == 0


def test_handoff_existing_linux_fingerprint_and_history(lan, git_workspace):
    runtime, adapter = lan
    server = runtime.server_for('jiangli')
    original = server.handle_tool_call('continuity_begin', dict(workspace_path=str(git_workspace), project='demo', objective='Keep Linux history'))
    from evolvmem.continuity_service import _collect_repo_anchor
    snapshot = _collect_repo_anchor(str(git_workspace))
    conn = server.context_service.store._connection()
    before = tuple(conn.execute('SELECT workspace_fingerprint,current_context_id,checkpoint_revision FROM continuity_workstreams WHERE id=?', (original['workstream_id'],)).fetchone())
    bound = adapter.call_tool('jiangli', 'continuity_bind', dict(workspace_path=PATH, device_id='newpc', project='demo', workstream_id=original['workstream_id'], repo_snapshot=snapshot, request_id='linux-handoff'))
    assert bound.get('bound'), bound
    assert resume(adapter, 'jiangli', 'newpc', repo_snapshot=snapshot)['workstream_id'] == original['workstream_id']
    assert tuple(conn.execute('SELECT workspace_fingerprint,current_context_id,checkpoint_revision FROM continuity_workstreams WHERE id=?', (original['workstream_id'],)).fetchone()) == before
    assert server.handle_tool_call('continuity_resume', dict(workspace_path=str(git_workspace)))['workstream_id'] == original['workstream_id']


def test_remote_checkpoint_cas_discovery_and_restart_binding(lan):
    runtime, adapter = lan
    first = begin(adapter, repo_snapshot=SNAP)
    args = dict(action='update', workspace_path=PATH, device_id='pc1', project_hint='demo', workstream_id=first['workstream_id'],
                expected_checkpoint_revision=first['checkpoint_revision'], expected_state_version=first['state_version'],
                completed_steps=['Validated SQLite and HTTP'], next_action='Review final result', request_id='update', repo_snapshot=SNAP)
    update = adapter.call_tool('kane', 'continuity_checkpoint', args)
    assert 'error' not in update, update
    stale = adapter.call_tool('kane', 'continuity_checkpoint', {**args, 'request_id': 'stale-update'})
    assert stale['error'] == 'revision_conflict', stale
    found = adapter.call_tool('kane', 'continuity_find', dict(query='demo', workspace_path=PATH, device_id='pc1', repo_snapshot=SNAP))
    assert first['workstream_id'] in json.dumps(found)
    listed = adapter.call_tool('kane', 'continuity_list', dict(workspace_path=PATH, device_id='pc1', project_hint='demo'))
    assert first['workstream_id'] in json.dumps(listed)
    assert adapter.call_tool('kane', 'continuity_bind', dict(workspace_path=PATH, device_id='pc2', project='demo', workstream_id=first['workstream_id'], repo_snapshot=SNAP, request_id='bind'))['bound']
    runtime.close()
    restarted = LanRuntime(runtime.settings)
    restarted.initialize()
    try:
        assert resume(LanTools(restarted), device='pc2')['workstream_id'] == first['workstream_id']
    finally:
        restarted.close()


def test_remote_device_scope_preserves_opaque_whitespace_paths(lan):
    runtime, adapter = lan
    original_path = PATH + ' '
    first = adapter.call_tool('kane', 'continuity_begin', dict(workspace_path=original_path, device_id='pc1', project='demo', objective='Opaque path', repo_snapshot=SNAP, request_id='space'))
    other = begin(adapter, repo_snapshot=SNAP)
    conn = runtime.server_for('kane').context_service.store._connection()
    fingerprints = [conn.execute('SELECT workspace_fingerprint FROM continuity_workstreams WHERE id=?', (item['workstream_id'],)).fetchone()[0] for item in (first, other)]
    assert fingerprints[0] != fingerprints[1]


# Existing Git fixture performs only temporary-directory git operations.
from tests.test_continuity_mcp import git_workspace


def test_handoff_nonfocused_linux_task_reports_focus_then_allows_explicit_cas(lan, git_workspace):
    runtime, adapter = lan
    server = runtime.server_for('jiangli')
    local_path = str(git_workspace)
    target = server.handle_tool_call('continuity_begin', dict(workspace_path=local_path, project='demo', objective='Selected handoff task'))
    current = server.handle_tool_call('continuity_checkpoint', dict(action='create', workspace_path=local_path, project_hint='demo', objective='Current Linux focused task', make_focus=True, expected_focus_revision=target['focus_revision']))
    assert 'error' not in current, current
    assert target['workstream_id'] != current['workstream_id']
    from evolvmem.continuity_service import _collect_repo_anchor
    snapshot = _collect_repo_anchor(local_path)
    result = adapter.call_tool('jiangli', 'continuity_bind', dict(workspace_path=PATH, device_id='handoff', project='demo', workstream_id=target['workstream_id'], repo_snapshot=snapshot, request_id='bind-nonfocused'))
    assert result['bound'] is True
    assert result['target_workstream_id'] == target['workstream_id']
    assert result['target_focused'] is False
    assert result['focused_workstream_id'] == current['workstream_id']
    assert result['focus_changed'] is False
    assert result['focus_revision'] == current['focus_revision']
    assert resume(adapter, 'jiangli', 'handoff', repo_snapshot=snapshot)['workstream_id'] == current['workstream_id']
    assert server.handle_tool_call('continuity_resume', dict(workspace_path=local_path))['workstream_id'] == current['workstream_id']
    suggestion = result['focus_switch']
    assert suggestion['tool'] == 'continuity_checkpoint'
    assert suggestion['arguments']['action'] == 'switch_focus'
    switched = adapter.call_tool('jiangli', suggestion['tool'], dict(**suggestion['arguments'], workspace_path=PATH, device_id='handoff', request_id='switch-explicit'))
    assert 'error' not in switched, switched
    assert resume(adapter, 'jiangli', 'handoff', repo_snapshot=snapshot)['workstream_id'] == target['workstream_id']
    assert server.handle_tool_call('continuity_resume', dict(workspace_path=local_path))['workstream_id'] == target['workstream_id']
    stale = adapter.call_tool('jiangli', suggestion['tool'], dict(**suggestion['arguments'], workspace_path=PATH, device_id='handoff', request_id='switch-stale'))
    assert stale['error'] == 'focus_conflict'

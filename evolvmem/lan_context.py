"""Request-scoped client workspace observations, with no client path I/O."""
from contextlib import contextmanager
import json
import re

from evolvmem.lan_sharing import LanError
from evolvmem.workspace_identity import WorkspaceIdentity, WorkspaceIdentityProvider


def validate_snapshot(snapshot):
    unknown = dict(kind='non_git', branch='', root_commit='', head_commit='')
    if snapshot is None:
        return unknown
    if not isinstance(snapshot, dict) or set(snapshot) - set(unknown):
        raise LanError('invalid_repo_snapshot')
    value = {**unknown, **snapshot}
    if value['kind'] not in ('git', 'non_git'):
        raise LanError('invalid_repo_snapshot')
    branch = value['branch']
    if not isinstance(branch, str) or len(branch) > 256 or any(ord(c) < 32 for c in branch):
        raise LanError('invalid_repo_snapshot')
    for field in ('root_commit', 'head_commit'):
        text = value[field]
        if not isinstance(text, str) or (value['kind'] == 'git' and not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', text)):
            raise LanError('invalid_repo_snapshot')
    if value['kind'] == 'non_git' and any(value[k] for k in ('branch', 'root_commit', 'head_commit')):
        raise LanError('invalid_repo_snapshot')
    return value


class RemoteWorkspaceIdentity(WorkspaceIdentityProvider):
    def __init__(self, local, store, device_id, snapshot):
        self.local, self.store, self.device_id, self.snapshot = local, store, device_id, snapshot

    def status(self):
        return self.local.status()

    def digest_private(self, domain, payload):
        return self.local.digest_private(domain, payload)

    def private_key(self, path):
        if not isinstance(self.device_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,64}', self.device_id):
            raise LanError('invalid_device_id')
        if not isinstance(path, str) or not path.strip() or len(path) > 4096 or '\x00' in path:
            raise LanError('invalid_workspace_path')
        framed = json.dumps([self.device_id, path], ensure_ascii=False, separators=(',', ':')).encode()
        return self.local.digest_private('workspace.remote.v1', framed)

    def resolve(self, workspace_path):
        fingerprint = self.private_key(workspace_path)
        row = self.store._connection().execute('SELECT fingerprint FROM lan_workspace_bindings WHERE remote_key=?', (fingerprint,)).fetchone()
        return WorkspaceIdentity(row[0] if row else fingerprint, self.snapshot['kind'])


def prepare_bindings(server):
    with server.context_service.store.transaction():
        server.context_service.store._connection().execute('''CREATE TABLE IF NOT EXISTS lan_workspace_bindings (
            remote_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, workstream_id TEXT NOT NULL)''')


def prepare_memory_revision(server):
    """A durable change counter also observes writes made by native clients."""
    with server.context_service.store.transaction():
        conn = server.context_service.store._connection()
        conn.execute('CREATE TABLE IF NOT EXISTS lan_memory_revision(id INTEGER PRIMARY KEY, revision INTEGER NOT NULL)')
        conn.execute('INSERT OR IGNORE INTO lan_memory_revision VALUES(1,0)')
        tracked = {
            'context_items': 'status,tier,project,importance,confidence,source_state,success_count,failure_count,expires_at',
            'context_layers': 'content_hash',
            'context_project_registry': 'revision',
            'continuity_workstreams': 'checkpoint_revision,state_version',
            'continuity_focus': 'revision',
        }
        for table, columns in tracked.items():
            for action in ('INSERT', 'DELETE', 'UPDATE OF ' + columns):
                suffix = action.split()[0].lower()
                condition = (' WHEN ' + ' OR '.join(f'NEW.{c} IS NOT OLD.{c}' for c in columns.split(','))) if suffix == 'update' else ''
                conn.execute(f'CREATE TRIGGER IF NOT EXISTS lan_revision_{table}_{suffix} AFTER {action} ON {table}{condition} '
                             'BEGIN UPDATE lan_memory_revision SET revision=revision+1 WHERE id=1; END')


@contextmanager
def remote_context(server, device_id, snapshot):
    """Caller holds runtime dispatch lock. Always restore both lazy services."""
    context = server.context_service
    local = server._workspace_identity()
    provider = RemoteWorkspaceIdentity(local, context.store, device_id, snapshot)
    services = (server._continuity(), context._continuity())
    saved = [(s, s._workspace_identity, s._repo_anchor, s._ancestor) for s in services]
    context_identity = context._identity_provider
    from evolvmem.project_board_sync import ProjectBoardSync
    prior_board = server._project_board_adapter
    server._project_board_adapter = ProjectBoardSync(server.config, context.store, provider,
        config_path=server.config.data_dir / 'project_board.json')
    server._workspace_identity_provider = provider
    context._identity_provider = provider
    for service in services:
        service._workspace_identity = provider
        service._repo_anchor = lambda _path: snapshot.copy()
        service._ancestor = lambda *_args: None
    try:
        yield provider
    finally:
        server._project_board_adapter = prior_board
        server._workspace_identity_provider = local
        context._identity_provider = context_identity
        for service, identity, anchor, ancestor in saved:
            service._workspace_identity, service._repo_anchor, service._ancestor = identity, anchor, ancestor


def bind_workspace(server, provider, args):
    store = server.context_service.store
    remote_key = provider.private_key(args['workspace_path'])
    with store.transaction():
        conn = store._connection()
        row = conn.execute('SELECT * FROM continuity_workstreams WHERE id=?', (args['workstream_id'],)).fetchone()
        if row is None or row['project'] != args['project']:
            raise LanError('handoff_target_mismatch')
        anchor = provider.snapshot
        if (row['repo_kind'] != 'git' or anchor['kind'] != 'git' or not row['repo_root_commit']
                or row['repo_root_commit'] != anchor['root_commit']):
            raise LanError('handoff_repository_mismatch')
        focus = conn.execute('SELECT workstream_id,revision FROM continuity_focus WHERE project=? AND workspace_fingerprint=?',
                             (row['project'], row['workspace_fingerprint'])).fetchone()
        conn.execute('INSERT INTO lan_workspace_bindings VALUES(?,?,?) ON CONFLICT(remote_key) DO UPDATE SET fingerprint=excluded.fingerprint,workstream_id=excluded.workstream_id',
                     (remote_key, row['workspace_fingerprint'], row['id']))
    focused_id = focus['workstream_id'] if focus is not None else None
    focus_revision = int(focus['revision']) if focus is not None else 0
    target_focused = focused_id == row['id']
    return dict(
        bound=True, workstream_id=row['id'], target_workstream_id=row['id'],
        project=row['project'], repo_source='client_reported',
        focus_changed=False, target_focused=target_focused,
        focused_workstream_id=focused_id, focus_revision=focus_revision,
        focus_switch=None if target_focused else {
            'tool': 'continuity_checkpoint',
            'arguments': {
                'action': 'switch_focus', 'project_hint': row['project'],
                'workstream_id': row['id'],
                'expected_checkpoint_revision': int(row['checkpoint_revision']),
                'expected_state_version': int(row['state_version']),
                'expected_focus_revision': focus_revision,
            },
            'instructions': 'Binding preserved the existing workspace focus. To select this task, '
                            'call this tool with these arguments plus the same workspace_path/device_id '
                            'and a new request_id. On focus_conflict, resume to obtain the current '
                            'focus_revision before explicitly switching again.',
        },
    )

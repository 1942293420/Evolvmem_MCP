"""Identity-bound knowledge console supporting PostgreSQL em_control and legacy SQLite.

Account metadata, Feishu open_id bindings, directional grants, and registered
personal write libraries live in em_control (PostgreSQL mode) or separate SQLite
metadata (legacy test mode). In PostgreSQL mode, control unavailability fails
closed with 503 pg_control_unavailable ("PG 不可用") and never falls back to SQLite.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from evolvmem.experience_v2.control.console_sources import (
    HISTORY_KEYS,
    JSON_MIME as JSON,
    clean_history_env,
    read_experience_source,
    read_history_count,
    read_or_manage_history,
    validate_history_paths,
)
from evolvmem.experience_v2.control.console_sqlite_legacy import (
    bootstrap_sqlite,
    configure_sqlite_library,
    dispatch_sqlite_metadata,
)
from evolvmem.experience_v2.control.pg_console_store import (
    PgConsoleStore,
    PgControlUnavailableError,
)

SLUG = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z', re.ASCII)
IDENTITY_INPUTS = frozenset({
    'user', 'user_id', 'actor', 'actor_id', 'namespace',
    'data_dir', 'database_path', 'history_environ', 'source_path', 'registry_path',
    'is_admin', 'role', 'principal', 'principal_id', 'library_id',
    'default_write_library_id', 'device_id', 'credential_id', 'owner_id',
})


class ConsoleError(ValueError):
    def __init__(self, code, status=400, *, message=None):
        super().__init__(code)
        self.code, self.status = code, status
        self.human_message = message


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _slug(value):
    if not isinstance(value, str) or not SLUG.fullmatch(value):
        raise ConsoleError('invalid_user_id')
    return value


def _text(value, maximum=100):
    if (not isinstance(value, str) or not value.strip() or len(value) > maximum
            or any(ord(c) < 32 for c in value)):
        raise ConsoleError('invalid_text')
    return value.strip()


def _open_id(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 200
            or not value.isascii() or any(c.isspace() or ord(c) < 33 for c in value)):
        raise ConsoleError('invalid_login_identity')
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _pg_unavailable():
    return ConsoleError('pg_control_unavailable', 503, message='PG 不可用')


class ConsoleService:
    _now_fn = staticmethod(_now)

    def __init__(self, registry_path, owner_id, owner_data_dir, lan_data_dir, history_environ,
                 *, experience_search=None, pg_control=None):
        self.experience_search = experience_search
        self.registry_path = Path(registry_path).expanduser().resolve()
        self.owner_id = _slug(owner_id)
        self._pg_store = None
        if pg_control is not None:
            self._pg_store = pg_control if isinstance(pg_control, PgConsoleStore) else PgConsoleStore(pg_control)
        elif self.owner_id == 'kane':
            raise ValueError('configured owner and existing member must be distinct')
        self.owner_data_dir = Path(owner_data_dir).expanduser().resolve()
        self.lan_data_dir = Path(lan_data_dir).expanduser().resolve()
        self.history_environ = clean_history_env(history_environ)
        if self._pg_store is None:
            sources = [self.owner_data_dir / 'memory.db', self.lan_data_dir / 'users' / 'kane' / 'memory.db',
                       self.lan_data_dir / 'public' / 'memory.db']
            if any(self.registry_path == p.resolve() for p in sources):
                raise ValueError('metadata must be separate from source databases')
            validate_history_paths(self.history_environ, self.registry_path)
        else:
            validate_history_paths(self.history_environ, None)

    @property
    def pg_mode(self) -> bool:
        return self._pg_store is not None

    @contextmanager
    def _db(self, *, write=False):
        if self.pg_mode:
            raise _pg_unavailable()
        if not self.registry_path.is_file():
            raise ConsoleError('console_not_initialized', 503)
        conn = sqlite3.connect(self.registry_path.as_uri() + ('?mode=rw' if write else '?mode=ro'),
                               uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA foreign_keys=ON')
            if write:
                conn.execute('BEGIN IMMEDIATE')
            else:
                conn.execute('PRAGMA query_only=ON')
            meta = conn.execute("SELECT value FROM console_meta WHERE key='owner_id'").fetchone()
            if not meta or meta[0] != self.owner_id:
                raise ConsoleError('invalid_console_registry', 503)
            yield conn
            if write:
                conn.commit()
        except Exception:
            if write:
                conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _audit(conn, actor, action, target='', before=None, after=None, outcome='allowed'):
        conn.execute('INSERT INTO console_audit(created_at,actor_id,action,target_id,before_json,after_json,outcome) '
                     'VALUES(?,?,?,?,?,?,?)', (_now(), actor or '', action, target,
                     _json(before or {}), _json(after or {}), outcome))

    def _source_env(self, user_id, env):
        env = dict(env)
        env.setdefault('EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE', user_id)
        base_dir = self.owner_data_dir.parent if self.pg_mode else self.registry_path.parent
        env.setdefault('EVOLVMEM_HISTORY_MANAGEMENT_DB', str(base_dir / ('history-management-' + user_id + '.db')))
        if user_id != self.owner_id:
            env.setdefault('EVOLVMEM_HISTORY_ACCEPTED_AUDIT_FILE',
                           str(base_dir / ('no-accepted-audit-' + user_id + '.json')))
        validate_history_paths(env, None if self.pg_mode else self.registry_path)
        return env

    def bootstrap(self, owner_open_id, owner_name='管理员'):
        owner_open_id, owner_name = _open_id(owner_open_id), _text(owner_name)
        if self.pg_mode:
            try:
                return self._pg_store.bootstrap(
                    self.owner_id, owner_open_id, owner_name,
                    str(self.owner_data_dir), self._source_env(self.owner_id, self.history_environ),
                )
            except PgControlUnavailableError:
                raise _pg_unavailable() from None
        return bootstrap_sqlite(self, owner_open_id, owner_name, _now, _json)

    def observe_login(self, open_id, name):
        open_id, name = _open_id(open_id), _text(name)
        if self.pg_mode:
            try:
                return self._pg_store.observe_login(self.owner_id, open_id, name)
            except PgControlUnavailableError:
                raise _pg_unavailable() from None
        with self._db(write=True) as conn:
            if conn.execute('SELECT id FROM console_users WHERE open_id=?', (open_id,)).fetchone():
                return {'ok': True, 'pending': False}
            conn.execute('INSERT INTO console_pending VALUES(?,?,?,?) ON CONFLICT(open_id) '
                         'DO UPDATE SET name=excluded.name,last_seen=excluded.last_seen',
                         (open_id, name, _now(), _now()))
            return {'ok': True, 'pending': True}

    def user_enabled(self, user_id):
        if self.pg_mode:
            try:
                _slug(user_id)
            except ConsoleError:
                return False
            try:
                return self._pg_store.user_enabled(self.owner_id, user_id)
            except PgControlUnavailableError:
                raise _pg_unavailable() from None
        try:
            _slug(user_id)
            with self._db() as conn:
                row = conn.execute('SELECT enabled FROM console_users WHERE id=?', (user_id,)).fetchone()
                return bool(row and row[0])
        except (ValueError, sqlite3.Error, OSError):
            return False

    def manual_experience_source(self, user_id):
        user_id = _slug(user_id)
        if self.pg_mode:
            try:
                row = self._pg_store.get_user_source(self.owner_id, user_id)
            except PgControlUnavailableError:
                raise _pg_unavailable() from None
            if not row or not row['enabled']:
                raise ConsoleError('account_disabled', 403)
            if not row['default_write_library_id'] or not row['data_dir']:
                raise ConsoleError('experience_not_connected', 404)
            return {'user_id': row['id'], 'data_dir': row['data_dir'], 'library_id': row['default_write_library_id']}
        with self._db() as conn:
            row = conn.execute('''SELECT u.enabled, l.user_id, l.data_dir
                FROM console_users AS u LEFT JOIN console_libraries AS l ON l.user_id=u.id
                WHERE u.id=?''', (user_id,)).fetchone()
            if not row or not row['enabled']:
                raise ConsoleError('account_disabled', 403)
            if not row['data_dir']:
                raise ConsoleError('experience_not_connected', 404)
            return {'user_id': row['user_id'], 'data_dir': row['data_dir']}

    def login_allowed_in_transaction(self, conn, open_id):
        if self.pg_mode:
            return self.login_allowed(open_id)
        open_id = _open_id(open_id)
        meta = conn.execute("SELECT value FROM console_meta WHERE key='owner_id'").fetchone()
        if not meta or meta[0] != self.owner_id:
            raise ConsoleError('invalid_console_registry', 503)
        row = conn.execute('SELECT enabled FROM console_users WHERE open_id=?', (open_id,)).fetchone()
        if row:
            return bool(row[0])
        return bool(conn.execute('SELECT 1 FROM console_pending WHERE open_id=?', (open_id,)).fetchone())

    def login_allowed(self, open_id):
        open_id = _open_id(open_id)
        if self.pg_mode:
            try:
                return self._pg_store.login_allowed(self.owner_id, open_id)
            except PgControlUnavailableError:
                raise _pg_unavailable() from None
        with self._db() as conn:
            return self.login_allowed_in_transaction(conn, open_id)

    def registry_ready(self):
        if self.pg_mode:
            try:
                return self._pg_store.registry_ready(self.owner_id)
            except PgControlUnavailableError:
                raise _pg_unavailable() from None
        try:
            with self._db() as conn:
                version = conn.execute("SELECT value FROM console_meta WHERE key='version'").fetchone()
                conn.execute('SELECT id,enabled,open_id FROM console_users LIMIT 0')
                conn.execute('SELECT user_id,data_dir,history_environ FROM console_libraries LIMIT 0')
                return bool(version and version[0] == '1')
        except (ValueError, sqlite3.Error, OSError):
            return False

    def configure_library(self, user_id, *, data_dir=None, history_environ=None):
        user_id = _slug(user_id)
        if not self.pg_mode:
            return configure_sqlite_library(self, user_id, data_dir=data_dir, history_environ=history_environ, json_fn=_json)
        try:
            users = self._pg_store.list_users(self.owner_id)
        except PgControlUnavailableError:
            raise _pg_unavailable() from None
        row = next((u for u in users if u['id'] == user_id), None)
        if row is None:
            raise ValueError('unknown user')
        source = str(Path(data_dir).expanduser().resolve()) if data_dir is not None else row['data_dir']
        env = (self._source_env(user_id, clean_history_env(history_environ))
               if history_environ is not None else json.loads(row['history_environ']))
        for other in users:
            if other['id'] == user_id:
                continue
            if source and other['data_dir'] and Path(source).resolve() == Path(other['data_dir']).resolve():
                raise ValueError('source belongs to another user')
            cand = env.get('EVOLVMEM_HISTORY_CANDIDATE_DB')
            other_cand = json.loads(other['history_environ']).get('EVOLVMEM_HISTORY_CANDIDATE_DB')
            if cand and other_cand and Path(cand).resolve() == Path(other_cand).resolve():
                raise ValueError('history source belongs to another user')
        try:
            self._pg_store.configure_library(self.owner_id, self.owner_id, user_id, data_dir=source, history_environ=env)
        except PgControlUnavailableError:
            raise _pg_unavailable() from None
        return {'ok': True}

    @staticmethod
    def _actor_from_row(actor):
        if not actor:
            raise ConsoleError('account_pending', 403)
        if not actor['enabled']:
            raise ConsoleError('account_disabled', 403)
        return dict(actor)

    def _actor(self, conn, session):
        if not isinstance(session, dict) or not session.get('open_id'):
            raise ConsoleError('login_required', 401)
        return self._actor_from_row(conn.execute('SELECT * FROM console_users WHERE open_id=?', (session['open_id'],)).fetchone())

    @staticmethod
    def _admin(actor):
        if not actor['is_admin']:
            raise ConsoleError('admin_required', 403)

    @staticmethod
    def _account(row):
        return {k: (bool(row[k]) if k in ('enabled', 'is_admin', 'lan_enabled') else row[k])
                for k in ('id', 'display_name', 'enabled', 'is_admin', 'open_id', 'lan_enabled', 'revision')}

    @staticmethod
    def _library_view(actor, owner, source, perms, *, counts=False, inspect=False, search=None):
        history, experience = perms
        env = json.loads(source['history_environ']) if source else {}
        candidate = env.get('EVOLVMEM_HISTORY_CANDIDATE_DB')
        history_connected = bool(candidate and Path(candidate).is_file() and env.get('EVOLVMEM_HISTORY_CANDIDATE_TOKEN_FILE'))
        experience_connected = bool(source and source['data_dir'] and (Path(source['data_dir']) / 'memory.db').is_file())
        result = {
            'id': owner['id'], 'display_name': owner['display_name'], 'enabled': bool(owner['enabled']),
            'can_read_history': history, 'can_read_experience': experience,
            'can_manage_history': actor['id'] == owner['id'] and history_connected and not inspect,
            'can_manage_experience': actor['id'] == owner['id'] and experience_connected and not inspect,
            'history_connected': history_connected if history else None,
            'experience_connected': experience_connected if experience else None,
            'connected': bool(history and history_connected or experience and experience_connected),
        }
        result['knowledge_state'] = ('connected' if history and history_connected and experience and experience_connected
                                     else 'partially_connected' if result['connected'] else 'not_connected')
        if counts:
            if history:
                result['history_count'] = ConsoleService._history_count(env) if history_connected else 0
            if experience:
                result['experience_count'] = ConsoleService._experience(source, {'origin': 'manual'}, None, search=search)['total'] if experience_connected else 0
        return result

    def _permissions(self, conn, actor, owner, *, inspect=False):
        if actor['id'] == owner['id'] or inspect and actor['is_admin']:
            return True, True
        row = conn.execute('SELECT history_read,experience_read FROM console_grants WHERE viewer_id=? AND owner_id=?',
                           (actor['id'], owner['id'])).fetchone()
        return (bool(row[0]), bool(row[1])) if row else (False, False)

    @staticmethod
    def _history_count(env):
        return read_history_count(env)

    def _history(self, owner_id, source, suffix, method, body):
        return read_or_manage_history(owner_id, source, suffix, method, body, ConsoleError)

    @staticmethod
    def _experience(source, params, item_id, *, search=None):
        return read_experience_source(source, params, item_id, search=search, error_cls=ConsoleError)

    def _access_audit(self, actor_id, action, owner_id, outcome):
        if self.pg_mode:
            try:
                self._pg_store.record_audit(actor_id, action, owner_id, outcome=outcome)
            except PgControlUnavailableError:
                raise _pg_unavailable() from None
            return
        with self._db(write=True) as conn:
            self._audit(conn, actor_id, action, owner_id, outcome=outcome)

    def _validate_user_mutation_inputs(self, actor, target, body, *, create, old):
        self._admin(actor)
        allowed = {'id', 'display_name', 'open_id', 'enabled'} if create else {'display_name', 'open_id', 'enabled', 'revision'}
        if set(body) - allowed:
            raise ConsoleError('invalid_account_fields')
        target = _slug(body.get('id')) if create else _slug(target)
        if body.get('revision') is not None and (type(body['revision']) is not int or (old and body['revision'] != old['revision'])):
            raise ConsoleError('revision_conflict', 409)
        enabled = body.get('enabled', bool(old['enabled']) if old else True)
        if type(enabled) is not bool:
            raise ConsoleError('invalid_account_enabled')
        name = _text(body.get('display_name', old['display_name'] if old else target))
        openid = body.get('open_id', old['open_id'] if old else None)
        return target, name, (_open_id(openid) if openid is not None else None), enabled

    @staticmethod
    def _validate_grant_body(actor, body):
        ConsoleService._admin(actor)
        if set(body) != {'viewer_id', 'owner_id', 'history_read', 'experience_read', 'revision'}:
            raise ConsoleError('invalid_grant_fields')
        viewer, owner = _slug(body['viewer_id']), _slug(body['owner_id'])
        if viewer == owner:
            raise ConsoleError('self_access_immutable')
        if any(type(body[k]) is not bool for k in ('history_read', 'experience_read')) or type(body['revision']) is not int:
            raise ConsoleError('invalid_grant_fields')
        return viewer, owner

    def _dispatch_pg_metadata(self, method, path, body, session, inspect):
        if not self._pg_store.registry_ready(self.owner_id):
            raise ConsoleError('console_not_initialized', 503)
        if not isinstance(session, dict) or not session.get('open_id'):
            raise ConsoleError('login_required', 401)
        users = self._pg_store.list_users(self.owner_id)
        actor = self._actor_from_row(next((u for u in users if u['open_id'] == session['open_id']), None))
        if method not in ('GET', 'POST'):
            raise ConsoleError('method_not_allowed', 405)
        if inspect:
            self._admin(actor)
            if method != 'GET':
                raise ConsoleError('inspection_read_only', 403)
        if method == 'POST' and (path == '/users' or re.fullmatch(r'/users/[^/]+', path) or path == '/grants'):
            if path == '/grants':
                viewer, owner = self._validate_grant_body(actor, body)
                return actor, '', '', (200, JSON, self._pg_store.set_grant(
                    self.owner_id, actor['id'], viewer_id=viewer, owner_id=owner,
                    history_read=body['history_read'], experience_read=body['experience_read'], revision=body['revision']))
            create = path == '/users'
            old = None if create else next((u for u in users if u['id'] == _slug(path.split('/')[-1])), None)
            target, name, openid, enabled = self._validate_user_mutation_inputs(actor, path.split('/')[-1], body, create=create, old=old)
            cur = self._pg_store.upsert_user(
                self.owner_id, actor['id'], target, display_name=name, open_id=openid,
                enabled=enabled, revision=body.get('revision'), create=create)
            return actor, '', '', (201 if create else 200, JSON, {'ok': True, 'user': cur})
        if IDENTITY_INPUTS.intersection(body):
            raise ConsoleError('identity_override_forbidden')
        grants = {(g['viewer_id'], g['owner_id']): g for g in self._pg_store.list_grants(self.owner_id)}

        def perms(owner):
            if actor['id'] == owner['id'] or inspect and actor['is_admin']:
                return True, True
            g = grants.get((actor['id'], owner['id']))
            return (bool(g['history_read']), bool(g['experience_read'])) if g else (False, False)

        ordered = sorted(users, key=lambda u: (0 if u['id'] == actor['id'] else 1, u['display_name'], u['id']))
        if path == '/me' and method == 'GET':
            libs = [self._library_view(actor, o, o, perms(o), inspect=inspect, search=self.experience_search)
                    for o in ordered if (o['enabled'] or actor['is_admin']) and any(perms(o))]
            return actor, '', '', (200, JSON, {
                'ok': True, 'current_user': {'id': actor['id'], 'display_name': actor['display_name'], 'is_admin': bool(actor['is_admin'])},
                'csrf_token': session.get('csrf', ''), 'libraries': libs,
                'policy': {'cross_user_write': False, 'new_accounts_lan_provisioned': False, 'default_cross_user_read': False},
            })
        if path == '/libraries' and method == 'GET':
            libs = [self._library_view(actor, o, o, perms(o), counts=True, inspect=inspect, search=self.experience_search)
                    for o in ordered if (o['enabled'] or actor['is_admin']) and any(perms(o))]
            return actor, '', '', (200, JSON, {'ok': True, 'libraries': libs})
        if path == '/skills' and method == 'GET':
            self._admin(actor)
            from evolvmem.skill_library import catalog_root, read_catalog
            try:
                return actor, '', '', (200, JSON, read_catalog(catalog_root(self.owner_data_dir), body))
            except LookupError:
                raise ConsoleError('skill_not_found', 404) from None
        if path == '/users' and method == 'GET':
            self._admin(actor)
            out = [{**self._account(r), **self._library_view(actor, r, r, (True, True), inspect=True, search=self.experience_search),
                    'login_bound': bool(r['open_id']), 'lan_state': 'existing_identity' if r['lan_enabled'] else 'not_provisioned'}
                   for r in users]
            return actor, '', '', (200, JSON, {'ok': True, 'users': out, 'pending': self._pg_store.list_pending(self.owner_id)})
        if path == '/grants' and method == 'GET':
            self._admin(actor)
            uids = [u['id'] for u in users]
            rows = [grants.get((v, o), {'viewer_id': v, 'owner_id': o, 'history_read': False, 'experience_read': False, 'revision': 0})
                    for v in uids for o in uids if v != o]
            return actor, '', '', (200, JSON, {'ok': True, 'rows': rows, 'grants': rows})
        if path in ('/matrix', '/neural-matrix') and method == 'GET':
            peers = [{'id': r['id'], 'display_name': r['display_name'], 'enabled': bool(r['enabled']), 'lan_enabled': bool(r['lan_enabled'])}
                     for r in (users if actor['is_admin'] else [actor])]
            from evolvmem.neural_matrix import read_matrix
            return actor, '', '', (200, JSON, read_matrix(dict(actor), dict(actor), self.owner_data_dir, self.lan_data_dir, peers))
        match = re.fullmatch(r'/libraries/([^/]+)/(history|experiences)(/.*)?', path)
        if not match:
            raise ConsoleError('not_found', 404)
        target, resource, suffix = _slug(match[1]), match[2], match[3] or ''
        owner = next((u for u in users if u['id'] == target), None)
        if not owner or (not owner['enabled'] and not actor['is_admin']):
            raise ConsoleError('library_not_found', 404)
        return actor, target, resource, (suffix, *perms(owner), owner)

    def dispatch(self, method, relative_path, params_or_body, session):
        actor, target, resource = None, '', ''
        try:
            method = str(method).upper()
            if not isinstance(relative_path, str) or not relative_path.startswith('/') or '?' in relative_path or '#' in relative_path:
                raise ConsoleError('invalid_route')
            path = relative_path.rstrip('/') or '/'
            body = params_or_body if params_or_body is not None else {}
            if not isinstance(body, dict):
                raise ConsoleError('invalid_request')
            body = dict(body)
            inspect_value = body.pop('inspect', None)
            if inspect_value not in (None, '', 0, '0', False, 1, '1', True):
                raise ConsoleError('invalid_inspect')
            inspect = inspect_value in (1, '1', True)
            if self.pg_mode:
                try:
                    actor, target, resource, meta_res = self._dispatch_pg_metadata(method, path, body, session, inspect)
                except PgControlUnavailableError:
                    raise _pg_unavailable() from None
            else:
                actor, target, resource, meta_res = dispatch_sqlite_metadata(
                    self, method, path, body, session, inspect, IDENTITY_INPUTS, _slug, ConsoleError)
            if len(meta_res) == 3:
                return meta_res
            suffix, history, experience, source = meta_res
            if not (history if resource == 'history' else experience):
                raise ConsoleError('library_read_forbidden', 403)
            if (resource == 'experiences' and method == 'GET' and actor['id'] != target
                    and re.fullmatch(r'0*1', str(body.get('deleted', 0)))):
                raise ConsoleError('experience_trash_forbidden', 403)
            if resource == 'history' and actor['id'] != target and (method != 'GET' or suffix.startswith('/management/')):
                raise ConsoleError('history_management_forbidden', 403)
            if resource == 'experiences' and method != 'GET':
                if suffix:
                    raise ConsoleError('method_not_allowed', 405)
                if actor['id'] != target:
                    raise ConsoleError('experience_management_forbidden', 403)
            if resource == 'history' and method == 'POST' and suffix not in ('/management/delete', '/management/restore'):
                raise ConsoleError('method_not_allowed', 405)
            if resource == 'history':
                result = self._history(target, source, suffix, method, body)
            elif method == 'POST':
                if self.pg_mode and (not source or not source.get('default_write_library_id') or not source.get('data_dir')):
                    raise ConsoleError('experience_not_connected', 404)
                from evolvmem.manual_experiences import ManualExperienceError, write
                try:
                    status, data = write(source, actor['id'], body)
                except ManualExperienceError as exc:
                    raise ConsoleError(exc.code, exc.status) from None
                data['owner_id'] = target
                self._access_audit(actor['id'], 'experiences.manage', target, 'allowed')
                return status, JSON, data
            else:
                item_id = None
                if suffix:
                    if re.fullmatch(r'/manual-[a-f0-9]{32}', suffix) or re.fullmatch(r'/history-[a-f0-9]{64}', suffix):
                        item_id = suffix[1:]
                    elif re.fullmatch(r'/[1-9][0-9]{0,17}', suffix):
                        item_id = int(suffix[1:])
                    else:
                        raise ConsoleError('not_found', 404)
                data = self._experience(source, body, item_id, search=self.experience_search)
                if actor['id'] != target and data.get('origin') == 'manual' and isinstance(data.get('summary'), dict):
                    data['summary'].pop('deleted_total', None)
                data['owner_id'] = target
                result = 200, JSON, data
            self._access_audit(actor['id'], resource + '.' + ('manage' if method == 'POST' else 'inspect' if inspect else 'read'),
                               target, 'allowed' if result[0] < 400 else 'error')
            return result
        except ConsoleError as exc:
            if exc.code == 'pg_control_unavailable':
                return 503, JSON, {'ok': False, 'error': 'pg_control_unavailable', 'message': exc.human_message or 'PG 不可用'}
            if actor and resource:
                try:
                    self._access_audit(actor['id'], resource + '.read', target, 'denied' if exc.status == 403 else 'error')
                except ConsoleError as audit_exc:
                    if audit_exc.code == 'pg_control_unavailable':
                        return 503, JSON, {'ok': False, 'error': 'pg_control_unavailable', 'message': 'PG 不可用'}
                    return 503, JSON, {'ok': False, 'error': 'console_unavailable'}
                except Exception:
                    return 503, JSON, {'ok': False, 'error': 'console_unavailable'}
            return exc.status, JSON, {'ok': False, 'error': exc.code}
        except PgControlUnavailableError:
            return 503, JSON, {'ok': False, 'error': 'pg_control_unavailable', 'message': 'PG 不可用'}
        except LookupError:
            return 404, JSON, {'ok': False, 'error': 'experience_not_found'}
        except (ValueError, TypeError):
            return 400, JSON, {'ok': False, 'error': 'invalid_request'}
        except Exception:
            return 503, JSON, {'ok': False, 'error': 'console_unavailable'}

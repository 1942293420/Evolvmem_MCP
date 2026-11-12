"""Legacy SQLite console metadata helpers retained ONLY when PG control is unconfigured."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sqlite3

from .console_sources import JSON_MIME as JSON, clean_history_env

SQLITE_SCHEMA = '''
CREATE TABLE IF NOT EXISTS console_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS console_users(
  id TEXT PRIMARY KEY,display_name TEXT NOT NULL,enabled INTEGER NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,open_id TEXT UNIQUE,
  lan_enabled INTEGER NOT NULL DEFAULT 0,revision INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS console_pending(
  open_id TEXT PRIMARY KEY,name TEXT NOT NULL,first_seen TEXT NOT NULL,last_seen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS console_libraries(
  user_id TEXT PRIMARY KEY REFERENCES console_users(id),data_dir TEXT,
  history_environ TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS console_grants(
  viewer_id TEXT NOT NULL REFERENCES console_users(id),
  owner_id TEXT NOT NULL REFERENCES console_users(id),history_read INTEGER NOT NULL,
  experience_read INTEGER NOT NULL,revision INTEGER NOT NULL,
  PRIMARY KEY(viewer_id,owner_id),CHECK(viewer_id != owner_id));
CREATE TABLE IF NOT EXISTS console_audit(
  id INTEGER PRIMARY KEY,created_at TEXT NOT NULL,actor_id TEXT NOT NULL,
  action TEXT NOT NULL,target_id TEXT NOT NULL,before_json TEXT NOT NULL,
  after_json TEXT NOT NULL,outcome TEXT NOT NULL);
'''


def bootstrap_sqlite(svc, owner_open_id: str, owner_name: str, now_fn, json_fn) -> dict:
    svc.registry_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(svc.registry_path, timeout=10) as conn:
        conn.executescript(SQLITE_SCHEMA)
        conn.execute('BEGIN IMMEDIATE')
        meta = conn.execute("SELECT value FROM console_meta WHERE key='owner_id'").fetchone()
        existing = conn.execute('SELECT open_id FROM console_users WHERE id=?', (svc.owner_id,)).fetchone()
        if meta and meta[0] != svc.owner_id or existing and existing[0] != owner_open_id:
            raise ValueError('configured owner identity cannot be rebound')
        conn.execute("INSERT OR IGNORE INTO console_meta VALUES('owner_id',?)", (svc.owner_id,))
        conn.execute("INSERT OR IGNORE INTO console_meta VALUES('version','1')")
        for user_id, name, openid, admin in ((svc.owner_id, owner_name, owner_open_id, 1), ('kane', 'kane', None, 0)):
            conn.execute(
                'INSERT OR IGNORE INTO console_users '
                '(id,display_name,enabled,is_admin,open_id,lan_enabled,created_at,updated_at) VALUES(?,?,1,?,?,1,?,?)',
                (user_id, name, admin, openid, now_fn(), now_fn()),
            )
        for user_id, data_dir, env in (
            (svc.owner_id, svc.owner_data_dir, svc.history_environ),
            ('kane', svc.lan_data_dir / 'users' / 'kane', {}),
        ):
            conn.execute(
                'INSERT OR IGNORE INTO console_libraries(user_id,data_dir,history_environ) VALUES(?,?,?)',
                (user_id, str(data_dir), json_fn(svc._source_env(user_id, env))),
            )
        if not existing:
            svc._audit(conn, svc.owner_id, 'registry.bootstrap', svc.owner_id)
        conn.commit()
    if os.name != 'nt':
        os.chmod(svc.registry_path, 0o600)
    return {'ok': True}


def configure_sqlite_library(svc, user_id: str, *, data_dir, history_environ, json_fn) -> dict:
    with svc._db(write=True) as conn:
        if not conn.execute('SELECT 1 FROM console_users WHERE id=?', (user_id,)).fetchone():
            raise ValueError('unknown user')
        row = conn.execute('SELECT * FROM console_libraries WHERE user_id=?', (user_id,)).fetchone()
        source = str(Path(data_dir).expanduser().resolve()) if data_dir is not None else row['data_dir']
        env = (
            svc._source_env(user_id, clean_history_env(history_environ))
            if history_environ is not None
            else json.loads(row['history_environ'])
        )
        for other in conn.execute('SELECT * FROM console_libraries WHERE user_id != ?', (user_id,)):
            if source and other['data_dir'] and Path(source).resolve() == Path(other['data_dir']).resolve():
                raise ValueError('source belongs to another user')
            cand = env.get('EVOLVMEM_HISTORY_CANDIDATE_DB')
            other_cand = json.loads(other['history_environ']).get('EVOLVMEM_HISTORY_CANDIDATE_DB')
            if cand and other_cand and Path(cand).resolve() == Path(other_cand).resolve():
                raise ValueError('history source belongs to another user')
        if source and (Path(source) / 'memory.db').resolve() == svc.registry_path:
            raise ValueError('metadata cannot be a source database')
        conn.execute(
            'UPDATE console_libraries SET data_dir=?,history_environ=? WHERE user_id=?',
            (source, json_fn(env), user_id),
        )
        svc._audit(
            conn, svc.owner_id, 'library.configure', user_id,
            after={'experience_configured': bool(source), 'history_configured': bool(env.get('EVOLVMEM_HISTORY_CANDIDATE_DB'))},
        )
    return {'ok': True}


def update_sqlite_user(svc, conn, actor, target, body, *, create: bool, slug_fn, now_fn, err_cls):
    target_peek = slug_fn(body.get('id')) if create and 'id' in body else (slug_fn(target) if not create else None)
    old = conn.execute('SELECT * FROM console_users WHERE id=?', (target_peek,)).fetchone() if target_peek else None
    if create and old:
        svc._admin(actor)
        if set(body) - {'id', 'display_name', 'open_id', 'enabled'}:
            raise err_cls('invalid_account_fields')
        raise err_cls('account_exists', 409)
    if not create and not old:
        svc._admin(actor)
        if set(body) - {'display_name', 'open_id', 'enabled', 'revision'}:
            raise err_cls('invalid_account_fields')
        raise err_cls('account_not_found', 404)
    target, name, openid, enabled = svc._validate_user_mutation_inputs(actor, target, body, create=create, old=old)
    if target == svc.owner_id and (not enabled or openid != old['open_id']):
        raise err_cls('configured_owner_immutable')
    if openid is not None:
        if conn.execute('SELECT id FROM console_users WHERE open_id=? AND id!=?', (openid, target)).fetchone():
            raise err_cls('login_already_bound', 409)
        if (not old or openid != old['open_id']) and not conn.execute('SELECT 1 FROM console_pending WHERE open_id=?', (openid,)).fetchone():
            raise err_cls('login_not_observed')
    from evolvmem.web_sessions import revoke_identity
    if old and old['open_id'] and (not enabled or old['open_id'] != openid):
        revoke_identity(conn, old['open_id'])
    if not enabled and openid and (not old or openid != old['open_id']):
        revoke_identity(conn, openid)
    if create:
        conn.execute(
            'INSERT INTO console_users(id,display_name,enabled,is_admin,open_id,lan_enabled,created_at,updated_at) VALUES(?,?,?,0,?,0,?,?)',
            (target, name, int(enabled), openid, now_fn(), now_fn()),
        )
        conn.execute('INSERT INTO console_libraries(user_id) VALUES(?)', (target,))
    else:
        conn.execute(
            'UPDATE console_users SET display_name=?,enabled=?,open_id=?,revision=revision+1,updated_at=? WHERE id=?',
            (name, int(enabled), openid, now_fn(), target),
        )
    if openid:
        conn.execute('DELETE FROM console_pending WHERE open_id=?', (openid,))
    current = svc._account(conn.execute('SELECT * FROM console_users WHERE id=?', (target,)).fetchone())
    svc._audit(conn, actor['id'], 'account.create' if create else 'account.update', target,
               svc._account(old) if old else {}, current)
    return {'ok': True, 'user': current}


def update_sqlite_grant(svc, conn, actor, body, err_cls):
    viewer, owner = svc._validate_grant_body(actor, body)
    if conn.execute('SELECT count(*) FROM console_users WHERE id IN (?,?)', (viewer, owner)).fetchone()[0] != 2:
        raise err_cls('account_not_found', 404)
    old = conn.execute('SELECT * FROM console_grants WHERE viewer_id=? AND owner_id=?', (viewer, owner)).fetchone()
    before = dict(old) if old else {'viewer_id': viewer, 'owner_id': owner, 'history_read': False, 'experience_read': False, 'revision': 0}
    before.update(history_read=bool(before['history_read']), experience_read=bool(before['experience_read']))
    if body['revision'] != before['revision']:
        raise err_cls('revision_conflict', 409)
    after = {**body, 'revision': before['revision'] + 1}
    conn.execute(
        'INSERT INTO console_grants VALUES(?,?,?,?,?) ON CONFLICT(viewer_id,owner_id) DO UPDATE '
        'SET history_read=excluded.history_read,experience_read=excluded.experience_read,revision=excluded.revision',
        (viewer, owner, int(body['history_read']), int(body['experience_read']), after['revision']),
    )
    svc._audit(conn, actor['id'], 'grant.update', owner, before, after)
    return {'ok': True, 'grant': after, 'row': after}


def dispatch_sqlite_metadata(svc, method, path, body, session, inspect, identity_inputs, slug_fn, err_cls):
    with svc._db() as conn:
        actor = svc._actor(conn, session)
    if method not in ('GET', 'POST'):
        raise err_cls('method_not_allowed', 405)
    if inspect:
        svc._admin(actor)
        if method != 'GET':
            raise err_cls('inspection_read_only', 403)
    if method == 'POST' and (path == '/users' or re.fullmatch(r'/users/[^/]+', path) or path == '/grants'):
        with svc._db(write=True) as conn:
            actor = svc._actor(conn, session)
            res = update_sqlite_grant(svc, conn, actor, body, err_cls) if path == '/grants' else update_sqlite_user(
                svc, conn, actor, path.split('/')[-1], body, create=path == '/users',
                slug_fn=slug_fn, now_fn=svc._now_fn, err_cls=err_cls,
            )
        return actor, '', '', ((201 if path == '/users' else 200), JSON, res)
    if identity_inputs.intersection(body):
        raise err_cls('identity_override_forbidden')
    with svc._db() as conn:
        actor = svc._actor(conn, session)

        def libs_for(*, counts=False):
            out = []
            for o in conn.execute('SELECT * FROM console_users ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END,display_name,id', (actor['id'],)):
                if (o['enabled'] or actor['is_admin']) and any(svc._permissions(conn, actor, o, inspect=inspect)):
                    s = conn.execute('SELECT * FROM console_libraries WHERE user_id=?', (o['id'],)).fetchone()
                    out.append(svc._library_view(actor, o, s, svc._permissions(conn, actor, o, inspect=inspect), counts=counts, inspect=inspect, search=svc.experience_search))
            return out

        if path == '/me' and method == 'GET':
            return actor, '', '', (200, JSON, {
                'ok': True, 'current_user': {k: actor[k] for k in ('id', 'display_name')} | {'is_admin': bool(actor['is_admin'])},
                'csrf_token': session.get('csrf', ''), 'libraries': libs_for(counts=False),
                'policy': {'cross_user_write': False, 'new_accounts_lan_provisioned': False, 'default_cross_user_read': False},
            })
        if path == '/libraries' and method == 'GET':
            return actor, '', '', (200, JSON, {'ok': True, 'libraries': libs_for(counts=True)})
        if path == '/skills' and method == 'GET':
            svc._admin(actor)
            from evolvmem.skill_library import catalog_root, read_catalog
            try:
                return actor, '', '', (200, JSON, read_catalog(catalog_root(svc.owner_data_dir), body))
            except LookupError:
                raise err_cls('skill_not_found', 404) from None
        if path == '/users' and method == 'GET':
            svc._admin(actor)
            users = [{**svc._account(r), **svc._library_view(actor, r, conn.execute('SELECT * FROM console_libraries WHERE user_id=?', (r['id'],)).fetchone(), (True, True), inspect=True, search=svc.experience_search),
                      'login_bound': bool(r['open_id']), 'lan_state': 'existing_identity' if r['lan_enabled'] else 'not_provisioned'}
                     for r in conn.execute('SELECT * FROM console_users ORDER BY id')]
            pending = [dict(r) for r in conn.execute('SELECT * FROM console_pending ORDER BY first_seen,open_id')]
            return actor, '', '', (200, JSON, {'ok': True, 'users': users, 'pending': pending})
        if path == '/grants' and method == 'GET':
            svc._admin(actor)
            uids = [r[0] for r in conn.execute('SELECT id FROM console_users ORDER BY id')]
            saved = {(r['viewer_id'], r['owner_id']): dict(r) for r in conn.execute('SELECT * FROM console_grants')}
            rows = [{**saved.get((v, o), {'viewer_id': v, 'owner_id': o, 'history_read': False, 'experience_read': False, 'revision': 0}),
                     'history_read': bool(saved.get((v, o), {}).get('history_read', False)),
                     'experience_read': bool(saved.get((v, o), {}).get('experience_read', False))}
                    for v in uids for o in uids if v != o]
            return actor, '', '', (200, JSON, {'ok': True, 'rows': rows, 'grants': rows})
        if path in ('/matrix', '/neural-matrix') and method == 'GET':
            src = conn.execute('SELECT * FROM console_libraries WHERE user_id=?', (actor['id'],)).fetchone()
            peers = [{'id': r['id'], 'display_name': r['display_name'], 'enabled': bool(r['enabled']), 'lan_enabled': bool(r['lan_enabled'])}
                     for r in (conn.execute('SELECT id,display_name,enabled,lan_enabled FROM console_users ORDER BY id') if actor['is_admin'] else [actor])]
            from evolvmem.neural_matrix import read_matrix
            return actor, '', '', (200, JSON, read_matrix(dict(actor), dict(src) if src else None, svc.owner_data_dir, svc.lan_data_dir, peers))
        match = re.fullmatch(r'/libraries/([^/]+)/(history|experiences)(/.*)?', path)
        if not match:
            raise err_cls('not_found', 404)
        target, resource, suffix = slug_fn(match[1]), match[2], match[3] or ''
        owner = conn.execute('SELECT * FROM console_users WHERE id=?', (target,)).fetchone()
        if not owner or (not owner['enabled'] and not actor['is_admin']):
            raise err_cls('library_not_found', 404)
        history, experience = svc._permissions(conn, actor, owner, inspect=inspect)
        source = conn.execute('SELECT * FROM console_libraries WHERE user_id=?', (target,)).fetchone()
        return actor, target, resource, (suffix, history, experience, source)

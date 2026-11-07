"""Identity-bound knowledge console; account metadata is separate from sources.

This module does not initialize source schemas, run models, create source users,
issue LAN credentials, or change the original knowledge databases. The HTTP
integration must supply a trusted WebAuth session and enforce CSRF on POST.
Only bootstrap/configure_library receive trusted server paths; dispatch never does.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3

JSON = 'application/json; charset=utf-8'
SLUG = re.compile(r'[a-z][a-z0-9_-]{0,63}\Z', re.ASCII)
IDENTITY_INPUTS = frozenset({'user', 'user_id', 'actor', 'actor_id', 'namespace',
    'data_dir', 'database_path', 'history_environ', 'source_path', 'registry_path',
    'is_admin', 'role', 'principal'})
HISTORY_KEYS = frozenset({'EVOLVMEM_HISTORY_CANDIDATE_DB',
    'EVOLVMEM_HISTORY_CANDIDATE_TOKEN_FILE', 'EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE',
    'EVOLVMEM_HISTORY_MANAGEMENT_DB', 'EVOLVMEM_HISTORY_COVERAGE_FILE',
    'EVOLVMEM_HISTORY_ACCEPTED_AUDIT_FILE'})


class ConsoleError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


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


class ConsoleService:
    def __init__(self, registry_path, owner_id, owner_data_dir, lan_data_dir, history_environ):
        self.registry_path = Path(registry_path).expanduser().resolve()
        self.owner_id = _slug(owner_id)
        if self.owner_id == 'kane':
            raise ValueError('configured owner and existing member must be distinct')
        self.owner_data_dir = Path(owner_data_dir).expanduser().resolve()
        self.lan_data_dir = Path(lan_data_dir).expanduser().resolve()
        self.history_environ = self._clean_history_env(history_environ)
        sources = [self.owner_data_dir/'memory.db', self.lan_data_dir/'users'/'kane'/'memory.db',
                   self.lan_data_dir/'public'/'memory.db']
        if any(self.registry_path == p.resolve() for p in sources):
            raise ValueError('metadata must be separate from source databases')
        self._validate_history_paths(self.history_environ)

    def _clean_history_env(self, values):
        if not isinstance(values, dict):
            raise ValueError('history environment must be a server-controlled mapping')
        return {key: str(value) for key, value in values.items()
                if key in HISTORY_KEYS and value is not None and str(value)}

    def _validate_history_paths(self, env):
        source = env.get('EVOLVMEM_HISTORY_CANDIDATE_DB')
        if source and (not Path(source).is_absolute() or Path(source).resolve()==self.registry_path):
            raise ValueError('invalid source configuration')
        management = env.get('EVOLVMEM_HISTORY_MANAGEMENT_DB')
        if management and (not Path(management).is_absolute()
                or Path(management).resolve()==self.registry_path
                or source and Path(management).resolve()==Path(source).resolve()):
            raise ValueError('management must be separate from source databases')

    @contextmanager
    def _db(self, *, write=False):
        if not self.registry_path.is_file():
            raise ConsoleError('console_not_initialized', 503)
        conn = sqlite3.connect(self.registry_path.as_uri()+('?mode=rw' if write else '?mode=ro'),
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
        env.setdefault('EVOLVMEM_HISTORY_MANAGEMENT_DB',
                       str(self.registry_path.parent / ('history-management-'+user_id+'.db')))
        # The legacy reader has an owner-specific default audit artifact. Never
        # inherit that artifact into a member's explicitly connected history.
        if user_id != self.owner_id:
            env.setdefault('EVOLVMEM_HISTORY_ACCEPTED_AUDIT_FILE',
                           str(self.registry_path.parent / ('no-accepted-audit-'+user_id+'.json')))
        self._validate_history_paths(env)
        return env

    def bootstrap(self, owner_open_id, owner_name='管理员'):
        """Explicit installation only. Reads never create registry/schema/files."""
        owner_open_id, owner_name = _open_id(owner_open_id), _text(owner_name)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.registry_path, timeout=10) as conn:
            conn.executescript('''
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
            ''')
            conn.execute('BEGIN IMMEDIATE')
            meta = conn.execute("SELECT value FROM console_meta WHERE key='owner_id'").fetchone()
            existing = conn.execute('SELECT open_id FROM console_users WHERE id=?',(self.owner_id,)).fetchone()
            if meta and meta[0] != self.owner_id or existing and existing[0] != owner_open_id:
                raise ValueError('configured owner identity cannot be rebound')
            conn.execute("INSERT OR IGNORE INTO console_meta VALUES('owner_id',?)",(self.owner_id,))
            conn.execute("INSERT OR IGNORE INTO console_meta VALUES('version','1')")
            for user_id,name,openid,admin in ((self.owner_id,owner_name,owner_open_id,1),('kane','kane',None,0)):
                conn.execute('INSERT OR IGNORE INTO console_users '
                    '(id,display_name,enabled,is_admin,open_id,lan_enabled,created_at,updated_at) VALUES(?,?,1,?,?,1,?,?)',
                    (user_id,name,admin,openid,_now(),_now()))
            for user_id,data_dir,env in ((self.owner_id,self.owner_data_dir,self.history_environ),
                                       ('kane',self.lan_data_dir/'users'/'kane',{})):
                conn.execute('INSERT OR IGNORE INTO console_libraries(user_id,data_dir,history_environ) VALUES(?,?,?)',
                             (user_id,str(data_dir),_json(self._source_env(user_id,env))))
            if not existing:
                self._audit(conn,self.owner_id,'registry.bootstrap',self.owner_id)
            conn.commit()
        if os.name != 'nt':
            os.chmod(self.registry_path,0o600)
        return {'ok':True}

    def observe_login(self, open_id, name):
        """Call only after the real OAuth provider has verified identity/tenant."""
        open_id, name = _open_id(open_id), _text(name)
        with self._db(write=True) as conn:
            user = conn.execute('SELECT id FROM console_users WHERE open_id=?',(open_id,)).fetchone()
            if user:
                return {'ok':True,'pending':False}
            conn.execute('INSERT INTO console_pending VALUES(?,?,?,?) ON CONFLICT(open_id) '
                         'DO UPDATE SET name=excluded.name,last_seen=excluded.last_seen',
                         (open_id,name,_now(),_now()))
            return {'ok':True,'pending':True}

    def user_enabled(self, user_id):
        try:
            _slug(user_id)
            with self._db() as conn:
                row=conn.execute('SELECT enabled FROM console_users WHERE id=?',(user_id,)).fetchone()
                return bool(row and row[0])
        except (ValueError,sqlite3.Error,OSError):
            return False

    def registry_ready(self):
        """Read-only metadata readiness; distinguish unavailable from disabled."""
        try:
            with self._db() as conn:
                version=conn.execute("SELECT value FROM console_meta WHERE key='version'").fetchone()
                conn.execute('SELECT id,enabled,open_id FROM console_users LIMIT 0')
                conn.execute('SELECT user_id,data_dir,history_environ FROM console_libraries LIMIT 0')
                return bool(version and version[0]=='1')
        except (ValueError,sqlite3.Error,OSError):
            return False

    def configure_library(self, user_id, *, data_dir=None, history_environ=None):
        """Trusted installation API, deliberately absent from HTTP dispatch.

        Accounts start unconnected. An operator may explicitly attach their own
        independently identified source; no source path is accepted from a user.
        """
        user_id = _slug(user_id)
        with self._db(write=True) as conn:
            if not conn.execute('SELECT 1 FROM console_users WHERE id=?',(user_id,)).fetchone():
                raise ValueError('unknown user')
            row=conn.execute('SELECT * FROM console_libraries WHERE user_id=?',(user_id,)).fetchone()
            source = str(Path(data_dir).expanduser().resolve()) if data_dir is not None else row['data_dir']
            env = self._source_env(user_id,self._clean_history_env(history_environ)) if history_environ is not None else json.loads(row['history_environ'])
            # A source may have one owner. Registry/other user's physical DBs
            # cannot silently become this account's personal library.
            for other in conn.execute('SELECT * FROM console_libraries WHERE user_id != ?',(user_id,)):
                if source and other['data_dir'] and Path(source).resolve()==Path(other['data_dir']).resolve():
                    raise ValueError('source belongs to another user')
                candidate=env.get('EVOLVMEM_HISTORY_CANDIDATE_DB')
                other_candidate=json.loads(other['history_environ']).get('EVOLVMEM_HISTORY_CANDIDATE_DB')
                if candidate and other_candidate and Path(candidate).resolve()==Path(other_candidate).resolve():
                    raise ValueError('history source belongs to another user')
            if source and (Path(source)/'memory.db').resolve()==self.registry_path:
                raise ValueError('metadata cannot be a source database')
            conn.execute('UPDATE console_libraries SET data_dir=?,history_environ=? WHERE user_id=?',
                         (source,_json(env),user_id))
            self._audit(conn,self.owner_id,'library.configure',user_id,
                        after={'experience_configured':bool(source),'history_configured':bool(env.get('EVOLVMEM_HISTORY_CANDIDATE_DB'))})
        return {'ok':True}

    def _actor(self, conn, session):
        if not isinstance(session,dict) or not session.get('open_id'):
            raise ConsoleError('login_required',401)
        # Exact provider identity, never request-supplied user ID or name.
        actor=conn.execute('SELECT * FROM console_users WHERE open_id=?',(session['open_id'],)).fetchone()
        if not actor:
            raise ConsoleError('account_pending',403)
        if not actor['enabled']:
            raise ConsoleError('account_disabled',403)
        return dict(actor)

    @staticmethod
    def _admin(actor):
        if not actor['is_admin']:
            raise ConsoleError('admin_required',403)

    @staticmethod
    def _account(row):
        return {k:(bool(row[k]) if k in ('enabled','is_admin','lan_enabled') else row[k])
                for k in ('id','display_name','enabled','is_admin','open_id','lan_enabled','revision')}

    def _permissions(self, conn, actor, owner, *, inspect=False):
        if actor['id']==owner['id'] or inspect and actor['is_admin']:
            return True,True
        row=conn.execute('SELECT history_read,experience_read FROM console_grants WHERE viewer_id=? AND owner_id=?',
                         (actor['id'],owner['id'])).fetchone()
        return (bool(row[0]),bool(row[1])) if row else (False,False)

    def _library(self, conn, actor, owner, *, counts=False, inspect=False):
        history,experience=self._permissions(conn,actor,owner,inspect=inspect)
        source=conn.execute('SELECT * FROM console_libraries WHERE user_id=?',(owner['id'],)).fetchone()
        env=json.loads(source['history_environ']) if source else {}
        candidate=env.get('EVOLVMEM_HISTORY_CANDIDATE_DB')
        history_connected=bool(candidate and Path(candidate).is_file() and env.get('EVOLVMEM_HISTORY_CANDIDATE_TOKEN_FILE'))
        experience_connected=bool(source and source['data_dir'] and (Path(source['data_dir'])/'memory.db').is_file())
        result={'id':owner['id'],'display_name':owner['display_name'],'enabled':bool(owner['enabled']),
                'can_read_history':history,'can_read_experience':experience,
                'can_manage_history':actor['id']==owner['id'] and history_connected and not inspect,
                'history_connected':history_connected if history else None,
                'experience_connected':experience_connected if experience else None,
                'connected':bool(history and history_connected or experience and experience_connected)}
        result['knowledge_state']='connected' if history and history_connected and experience and experience_connected else 'partially_connected' if result['connected'] else 'not_connected'
        if counts:
            if history:
                result['history_count']=self._history_count(env) if history_connected else 0
            if experience:
                result['experience_count']=self._experience(source,{},None)['total'] if experience_connected else 0
        return result

    @staticmethod
    def _history_count(env):
        from evolvmem.history_candidate_read import open_reader
        with open_reader(dict(env)) as reader:
            return sum(reader.block_visible(row[0]) for row in reader.conn.execute('SELECT block_id FROM blocks WHERE message_count>0'))

    def _libraries(self, conn, actor, *, counts=False, inspect=False):
        result=[]
        for owner in conn.execute('SELECT * FROM console_users ORDER BY CASE WHEN id=? THEN 0 ELSE 1 END,display_name,id',(actor['id'],)):
            if not owner['enabled'] and not actor['is_admin']:
                continue
            if not any(self._permissions(conn,actor,owner,inspect=inspect)):
                continue
            result.append(self._library(conn,actor,owner,counts=counts,inspect=inspect))
        return result

    def _update_user(self, conn, actor, target, body, *, create=False):
        self._admin(actor)
        allowed={'id','display_name','open_id','enabled'} if create else {'display_name','open_id','enabled','revision'}
        if set(body)-allowed:
            raise ConsoleError('invalid_account_fields')
        target=_slug(body.get('id')) if create else _slug(target)
        old=conn.execute('SELECT * FROM console_users WHERE id=?',(target,)).fetchone()
        if create and old:
            raise ConsoleError('account_exists',409)
        if not create and not old:
            raise ConsoleError('account_not_found',404)
        if body.get('revision') is not None and (type(body['revision']) is not int or body['revision']!=old['revision']):
            raise ConsoleError('revision_conflict',409)
        enabled=body.get('enabled',bool(old['enabled']) if old else True)
        if type(enabled) is not bool:
            raise ConsoleError('invalid_account_enabled')
        name=_text(body.get('display_name',old['display_name'] if old else target))
        openid=body.get('open_id',old['open_id'] if old else None)
        if target==self.owner_id and (not enabled or openid!=old['open_id']):
            raise ConsoleError('configured_owner_immutable')
        if openid is not None:
            openid=_open_id(openid)
            duplicate=conn.execute('SELECT id FROM console_users WHERE open_id=? AND id!=?',(openid,target)).fetchone()
            if duplicate:
                raise ConsoleError('login_already_bound',409)
            if (not old or openid!=old['open_id']) and not conn.execute('SELECT 1 FROM console_pending WHERE open_id=?',(openid,)).fetchone():
                raise ConsoleError('login_not_observed')
        if create:
            conn.execute('INSERT INTO console_users(id,display_name,enabled,is_admin,open_id,lan_enabled,created_at,updated_at) '
                         'VALUES(?,?,?,0,?,0,?,?)',(target,name,int(enabled),openid,_now(),_now()))
            conn.execute('INSERT INTO console_libraries(user_id) VALUES(?)',(target,))
        else:
            conn.execute('UPDATE console_users SET display_name=?,enabled=?,open_id=?,revision=revision+1,updated_at=? WHERE id=?',
                         (name,int(enabled),openid,_now(),target))
        if openid:
            conn.execute('DELETE FROM console_pending WHERE open_id=?',(openid,))
        current=self._account(conn.execute('SELECT * FROM console_users WHERE id=?',(target,)).fetchone())
        self._audit(conn,actor['id'],'account.create' if create else 'account.update',target,
                    self._account(old) if old else {},current)
        return {'ok':True,'user':current}

    def _update_grant(self, conn, actor, body):
        self._admin(actor)
        if set(body)!={'viewer_id','owner_id','history_read','experience_read','revision'}:
            raise ConsoleError('invalid_grant_fields')
        viewer,owner=_slug(body['viewer_id']),_slug(body['owner_id'])
        if viewer==owner:
            raise ConsoleError('self_access_immutable')
        if any(type(body[key]) is not bool for key in ('history_read','experience_read')) or type(body['revision']) is not int:
            raise ConsoleError('invalid_grant_fields')
        if conn.execute('SELECT count(*) FROM console_users WHERE id IN (?,?)',(viewer,owner)).fetchone()[0]!=2:
            raise ConsoleError('account_not_found',404)
        old=conn.execute('SELECT * FROM console_grants WHERE viewer_id=? AND owner_id=?',(viewer,owner)).fetchone()
        before=dict(old) if old else {'viewer_id':viewer,'owner_id':owner,'history_read':False,'experience_read':False,'revision':0}
        before.update(history_read=bool(before['history_read']),experience_read=bool(before['experience_read']))
        if body['revision']!=before['revision']:
            raise ConsoleError('revision_conflict',409)
        after={**body,'revision':before['revision']+1}
        conn.execute('INSERT INTO console_grants VALUES(?,?,?,?,?) ON CONFLICT(viewer_id,owner_id) DO UPDATE '
                     'SET history_read=excluded.history_read,experience_read=excluded.experience_read,revision=excluded.revision',
                     (viewer,owner,int(body['history_read']),int(body['experience_read']),after['revision']))
        self._audit(conn,actor['id'],'grant.update',owner,before,after)
        return {'ok':True,'grant':after,'row':after}

    def _access_audit(self, actor_id, action, owner_id, outcome):
        with self._db(write=True) as conn:
            self._audit(conn,actor_id,action,owner_id,outcome=outcome)

    def _history(self, owner_id, source, suffix, method, body):
        env=json.loads(source['history_environ']) if source else {}
        candidate=env.get('EVOLVMEM_HISTORY_CANDIDATE_DB')
        connected=bool(candidate and Path(candidate).is_file() and env.get('EVOLVMEM_HISTORY_CANDIDATE_TOKEN_FILE'))
        if not connected:
            empty={'ok':True,'connected':False,'history_connected':False,'owner_id':owner_id,'reason':'history_not_connected'}
            if method=='GET' and suffix in ('','/','/projects'):
                return 200,JSON,{**empty,'projects':[]}
            if method=='GET' and suffix=='/unassigned':
                return 200,JSON,{**empty,'dialogue':[],'empty':[],'dialogue_count':0,'empty_count':0}
            raise ConsoleError('history_not_connected',404)
        if method=='GET' and suffix.startswith('/attachment/'):
            from evolvmem.history_candidate_view import handle
            return handle('GET','/history'+suffix,body,owner_verified=True,auth_enabled=True,environ=env)
        from evolvmem.history_candidate_web import dispatch
        status,result=dispatch(method,'/api/history-candidate'+(suffix if suffix not in ('','/') else '/projects'),body,
            {'X-EvolvMem-Action':'history-manage'},environ=env,owner_verified=True)
        if isinstance(result,dict):
            result={**result,'owner_id':owner_id,'connected':True,'history_connected':True}
        return status,JSON,result

    @staticmethod
    def _experience(source, params, item_id):
        path=Path(source['data_dir']) if source and source['data_dir'] else None
        if not path or not (path/'memory.db').is_file():
            if item_id is not None:
                raise ConsoleError('experience_not_found',404)
            return {'ok':True,'connected':False,'experience_connected':False,'reason':'experience_not_connected',
                    'rows':[],'total':0,'page':1,'page_size':20}
        from evolvmem.config import Config
        from evolvmem.web_reads import snapshot
        # Construct without loading an arbitrary persisted database_path, models,
        # global env, migrations, archive keys, directories, or background workers.
        config=Config(data_dir=path,apply_environment=False)
        with snapshot(config) as reader:
            result=reader.insights().experience(item_id) if item_id is not None else reader.insights().experiences(params)
        return {**result,'ok':True,'connected':True,'experience_connected':True}

    def dispatch(self, method, relative_path, params_or_body, session):
        actor=None
        target=''
        resource=''
        try:
            method=str(method).upper()
            if not isinstance(relative_path,str) or not relative_path.startswith('/') or '?' in relative_path or '#' in relative_path:
                raise ConsoleError('invalid_route')
            path=relative_path.rstrip('/') or '/'
            body=params_or_body if params_or_body is not None else {}
            if not isinstance(body,dict):
                raise ConsoleError('invalid_request')
            with self._db() as conn:
                actor=self._actor(conn,session)
            if method not in ('GET','POST'):
                raise ConsoleError('method_not_allowed',405)
            body=dict(body)
            inspect_value=body.pop('inspect',None)
            if inspect_value not in (None,'',0,'0',False,1,'1',True):
                raise ConsoleError('invalid_inspect')
            inspect=inspect_value in (1,'1',True)
            if inspect:
                self._admin(actor)
                if method!='GET':
                    raise ConsoleError('inspection_read_only',403)
            if method=='POST' and (path=='/users' or re.fullmatch(r'/users/[^/]+',path) or path=='/grants'):
                with self._db(write=True) as conn:
                    actor=self._actor(conn,session)
                    if path=='/grants':
                        result=self._update_grant(conn,actor,body)
                    else:
                        result=self._update_user(conn,actor,path.split('/')[-1],body,create=path=='/users')
                return (201 if path=='/users' else 200),JSON,result
            if IDENTITY_INPUTS.intersection(body):
                raise ConsoleError('identity_override_forbidden')
            with self._db() as conn:
                actor=self._actor(conn,session)
                if path=='/me' and method=='GET':
                    return 200,JSON,{'ok':True,'current_user':{k:actor[k] for k in ('id','display_name')} | {'is_admin':bool(actor['is_admin'])},
                        'csrf_token':session.get('csrf',''),'libraries':self._libraries(conn,actor,inspect=inspect),
                        'policy':{'cross_user_write':False,'new_accounts_lan_provisioned':False,'default_cross_user_read':False}}
                if path=='/users' and method=='GET':
                    self._admin(actor)
                    users=[]
                    for row in conn.execute('SELECT * FROM console_users ORDER BY id'):
                        users.append({**self._account(row),**self._library(conn,actor,row,inspect=True),
                            'login_bound':bool(row['open_id']),'lan_state':'existing_identity' if row['lan_enabled'] else 'not_provisioned'})
                    pending=[dict(row) for row in conn.execute('SELECT * FROM console_pending ORDER BY first_seen,open_id')]
                    return 200,JSON,{'ok':True,'users':users,'pending':pending}
                if path=='/grants' and method=='GET':
                    self._admin(actor)
                    users=[row[0] for row in conn.execute('SELECT id FROM console_users ORDER BY id')]
                    saved={(r['viewer_id'],r['owner_id']):dict(r) for r in conn.execute('SELECT * FROM console_grants')}
                    rows=[]
                    for viewer in users:
                        for owner in users:
                            if viewer==owner:
                                continue
                            row=saved.get((viewer,owner),{'viewer_id':viewer,'owner_id':owner,'history_read':False,'experience_read':False,'revision':0})
                            rows.append({**row,'history_read':bool(row['history_read']),'experience_read':bool(row['experience_read'])})
                    return 200,JSON,{'ok':True,'rows':rows,'grants':rows}
                if path=='/libraries' and method=='GET':
                    return 200,JSON,{'ok':True,'libraries':self._libraries(conn,actor,counts=True,inspect=inspect)}
                match=re.fullmatch(r'/libraries/([^/]+)/(history|experiences)(/.*)?',path)
                if not match:
                    raise ConsoleError('not_found',404)
                target,resource,suffix=_slug(match[1]),match[2],match[3] or ''
                owner=conn.execute('SELECT * FROM console_users WHERE id=?',(target,)).fetchone()
                if not owner or (not owner['enabled'] and not actor['is_admin']):
                    raise ConsoleError('library_not_found',404)
                history,experience=self._permissions(conn,actor,owner,inspect=inspect)
                if not (history if resource=='history' else experience):
                    raise ConsoleError('library_read_forbidden',403)
                if resource=='history' and actor['id']!=target and (method!='GET' or suffix.startswith('/management/')):
                    raise ConsoleError('history_management_forbidden',403)
                if resource=='experiences' and method!='GET':
                    raise ConsoleError('method_not_allowed',405)
                if resource=='history' and method=='POST' and suffix not in ('/management/delete','/management/restore'):
                    raise ConsoleError('method_not_allowed',405)
                source=conn.execute('SELECT * FROM console_libraries WHERE user_id=?',(target,)).fetchone()
            if resource=='history':
                result=self._history(target,source,suffix,method,body)
            else:
                item_id=None
                if suffix:
                    if not re.fullmatch(r'/[1-9][0-9]{0,17}',suffix):
                        raise ConsoleError('not_found',404)
                    item_id=int(suffix[1:])
                data=self._experience(source,body,item_id)
                data['owner_id']=target
                result=200,JSON,data
            self._access_audit(actor['id'],resource+'.'+('manage' if method=='POST' else 'inspect' if inspect else 'read'),target,
                               'allowed' if result[0]<400 else 'error')
            return result
        except ConsoleError as exc:
            if actor and resource:
                try:
                    self._access_audit(actor['id'],resource+'.read',target,'denied' if exc.status==403 else 'error')
                except Exception:
                    return 503,JSON,{'ok':False,'error':'console_unavailable'}
            return exc.status,JSON,{'ok':False,'error':exc.code}
        except LookupError:
            return 404,JSON,{'ok':False,'error':'experience_not_found'}
        except (ValueError,TypeError):
            return 400,JSON,{'ok':False,'error':'invalid_request'}
        except Exception:
            # No exception messages, source paths, OAuth IDs, or SQL in responses.
            return 503,JSON,{'ok':False,'error':'console_unavailable'}

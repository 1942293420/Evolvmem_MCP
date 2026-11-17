"""Explicit external-driver port. SQLite effects are NOT PostgreSQL/RLS evidence."""
import hashlib
import json
import re
import sqlite3
import sys
import threading
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

P = '00000000-0000-0000-0000-000000000001'
D = '00000000-0000-0000-0000-000000000010'
C = '00000000-0000-0000-0000-000000000020'
L = '00000000-0000-0000-0000-000000000100'
L2 = '00000000-0000-0000-0000-000000000101'
TOKEN = 'synthetic-host-token'
DIGEST = hashlib.sha256(TOKEN.encode()).digest()


def platform_shim():
    """Import-only on Windows; an attempted SQLite cutover lock always fails."""
    if sys.platform != 'win32':
        return
    module = ModuleType('fcntl')
    def forbidden(*args, **kwargs):
        raise AssertionError('Linux locking is not a supported Windows test port')
    module.flock = module.fcntl = forbidden
    module.LOCK_EX, module.LOCK_SH, module.LOCK_UN, module.LOCK_NB = 2, 1, 8, 4
    sys.modules.setdefault('fcntl', module)


DDL = '''
CREATE TABLE memories(id INTEGER PRIMARY KEY,library_id,project_id,key,value,attribute,tags,
 source_session,importance,tier,expires_at,supersedes,created_at,updated_at,
 status DEFAULT 'active',superseded_by,revision INTEGER DEFAULT 1,access_count DEFAULT 0,last_accessed);
CREATE TABLE context_items(id INTEGER PRIMARY KEY,library_id,project_id,identity_key,content_type,
 project,scope,status,tier,tags,importance,confidence,expires_at,supersedes,effective_from,effective_until,
 occurred_at,mentioned_at,created_at,updated_at,source_state DEFAULT 'none',source_count DEFAULT 0,
 success_count DEFAULT 0,failure_count DEFAULT 0,access_count DEFAULT 0,last_accessed,last_verified_at,
 superseded_by,personal_general DEFAULT 0,projects DEFAULT '[]',experience_payload,revision DEFAULT 1);
CREATE TABLE context_layers(library_id,project_id,item_id,layer,content,content_hash,generator,created_at,updated_at);
CREATE TABLE context_sources(id INTEGER PRIMARY KEY,library_id,project_id,item_id,source_kind,
 source_ref,extraction_version,created_at,archive_id);
CREATE TABLE legacy_memory_migrations(library_id,project_id,legacy_memory_id,context_item_id,migrated_at);
CREATE TABLE context_project_registry(library_id,project,status,revision,project_uuid);
CREATE TABLE context_project_aliases(library_id,alias,project,revision);
CREATE TABLE context_project_workspace_bindings(library_id,workspace_fingerprint,project,state,is_default,revision);
CREATE TABLE context_project_resolutions(library_id,project_id,item_id,resolution_state,review_state,
 proposed_project,resolved_project,confidence,method,evidence_json,resolver_version,created_at,updated_at);
CREATE TABLE vector_outbox(library_id,project_id,index_kind,entity_id,action,entity_revision,content_hash,
 model_contract,created_at,UNIQUE(library_id,index_kind,entity_id,entity_revision,action,model_contract));
CREATE TABLE lan_write_requests(library_id,project_id,principal_id,tool,request_id,payload_hash,created_at,
 result_json,completed_at,UNIQUE(library_id,principal_id,tool,request_id));
'''


class Status:
    name = 'IDLE'
    def __int__(self):
        return 0 if self.name == 'IDLE' else 2


class Port:
    def __init__(self):
        self.rows = {}
        self.databases = {}
        self.connections = []
        self.calls = []
        self.secrets = []
        self.on_sql = None
        self.commit_unknown = False
        self.on_commit = None
        self.peak = 0
        self.lock = threading.RLock()
        for i, lib in enumerate((L, L2)):
            self.rows[lib] = dict(credential_id=C,principal_id=P,device_id=D,
                credential_digest=DIGEST,credential_epoch=3,principal_epoch=2,device_epoch=4,
                credential_active=True,principal_active=True,device_active=True,expires_at=None,
                library_id=lib,kind='device',backend='postgres',connection_ref='synthetic.'+str(i),
                policy_epoch=7,route_epoch=11,authority_state='pg_active',acl_state='ready',
                write_backend='postgres',project_id=None,library_device_id=D,
                owner_principal_id=P,library_active=True,permission='write')
            db = sqlite3.connect(':memory:',check_same_thread=False,isolation_level=None)
            db.executescript(DDL)
            self.databases['content_'+str(i)] = (lib,db)

    def close(self):
        for _, db in self.databases.values(): db.close()

    def secret(self, ref):
        self.secrets.append(ref)
        return 'synthetic-password'

    def connect(self, **kwargs):
        with self.lock:
            raw = Raw(self,kwargs)
            self.connections.append(raw)
            self.peak = max(self.peak,sum(not c.closed for c in self.connections))
            return raw

    def options(self, **overrides):
        from evolvmem.config import Config
        from evolvmem.library_catalog import ControlBinding
        from evolvmem.library_foundation import ConnectionBinding
        from evolvmem.library_host import LibraryHost
        bindings = tuple(ConnectionBinding(row['connection_ref'],lib,P,C,D,'127.0.0.1',55439,
            'content_'+str(i),'content_role_'+str(i),'content.password.'+str(i))
            for i,(lib,row) in enumerate(self.rows.items()))
        kwargs = dict(control_binding=ControlBinding('127.0.0.1',55439,'control','reader','control.password'),
            content_bindings=bindings,control_connector=self.connect,content_connector=self.connect,
            secret_resolver=self.secret,config=Config(apply_environment=False),
            model_contract='synthetic-reviewed-contract',semantic_merge_enabled=True)
        kwargs.update(overrides)
        return LibraryHost(**kwargs)

    def driver_patch(self):
        module = ModuleType('psycopg')
        module.Connection = Raw
        module.capabilities = SimpleNamespace(has_cancel_safe=lambda **kwargs: True)
        previous = sys.modules.get('psycopg')
        def start(): sys.modules['psycopg'] = module
        def stop():
            if previous is None: sys.modules.pop('psycopg',None)
            else: sys.modules['psycopg'] = previous
        return SimpleNamespace(start=start,stop=stop)


class Raw:
    def __init__(self, port, kwargs):
        self.port, self.kwargs = port, kwargs
        self.autocommit = kwargs['autocommit']
        self.info = SimpleNamespace(transaction_status=Status())
        self.closed = self.pinned = False
        self.lib, self.db = port.databases.get(kwargs['dbname'],(None,None))
        self.read_only = False

    def cursor(self): return Cursor(self)
    def commit(self):
        if self.db is not None and self.db.in_transaction: self.db.commit()
        self.info.transaction_status.name='IDLE'
        if self.port.on_commit: self.port.on_commit(self)
        if self.db is not None and self.port.commit_unknown and not self.read_only:
            self.port.commit_unknown=False
            raise OSError('private simulated lost commit reply')
    def rollback(self):
        if self.db is not None and self.db.in_transaction: self.db.rollback()
        self.info.transaction_status.name='IDLE'
        self.pinned=False
    def close(self):
        self.rollback()
        self.closed=True
    def cancel_safe(self, *, timeout): self.cancelled=True


class Cursor:
    def __init__(self, raw):
        self.raw=raw;self.description=None;self.rows=[]
    def __enter__(self): return self
    def __exit__(self,*args): self.close()
    def close(self): pass
    def fetchall(self): return self.rows
    def fetchone(self): return self.rows[0] if self.rows else None

    def execute(self, sql, params=()):
        raw=self.raw;p=raw.port
        p.calls.append((raw,sql,params))
        if p.on_sql: p.on_sql(raw,sql,params)
        if sql.startswith('BEGIN'):
            raw.info.transaction_status.name='INTRANS';raw.read_only='READ ONLY' in sql
            if raw.db is not None: raw.db.execute('BEGIN')
            return self
        if 'pg_catalog.set_config' in sql or 'pg_advisory_lock' in sql:
            return self.result([{'ok':1}])
        if 'pg_catalog.pg_roles' in sql:
            role=raw.kwargs['user']
            if raw.db is None:
                return self.result([dict(session_user=role,current_user=role,superuser=False,
                    bypass_rls=False,create_role=False,create_db=False,memberships=False,
                    owns_protected=False,direct_access=False)])
            return self.values([(role,role,False,False,False,True,False,False,False)])
        if raw.db is None:
            if 'em_control.auth_binding' in sql:
                return self.result([p.rows[L]] if bytes(params[0])==DIGEST else [])
            if 'em_control.resolve_library' in sql or 'em_control.pin_library' in sql:
                row=p.rows.get(params[6]);raw.pinned |= 'pin_library' in sql
                return self.result([dict(row)] if row else [])
            if 'em_control.list_libraries' in sql: return self.result(list(p.rows.values()))
            if sql.strip()=='SELECT 1 AS alive':return self.result([{'alive':1}])
            raise AssertionError('unexpected fixed control SQL port')
        row=p.rows[raw.lib]
        if 'pg_catalog.pg_class c' in sql and 'c.relname IN' in sql:
            marks=','.join('?' for _ in params)
            return self.values(raw.db.execute('SELECT name FROM sqlite_master WHERE name IN ('+marks+')',params).fetchall())
        if 'em_security.session_context()' in sql or 'em_security.pin_session_context' in sql:
            return self.values([(P,raw.lib,row['policy_epoch'],row['route_epoch'],
                row['permission'],row['authority_state'],row['acl_state'])])
        if 'em_security.session_credential()' in sql: return self.values([(C,D,3)])
        # The test-only effect port executes repository SQL against tiny SQLite
        # tables; casts/to_jsonb are adapted, never shipped in runtime code.
        projected=bool(re.search(r'SELECT to_jsonb\([mc]\)',sql))
        sql=re.sub(r'SELECT to_jsonb\(([mc])\)',r'SELECT \1.*',sql)
        sql=sql.replace('em_content.','').replace('%s','?').replace('::jsonb','').replace('::text','')
        sql=sql.replace("result_json->>'context_id'=?", "CAST(json_extract(result_json,'$.context_id') AS TEXT)=?")
        if raw.read_only and not sql.lstrip().upper().startswith('SELECT'):
            raise AssertionError('write in read-only port transaction')
        cursor=raw.db.execute(sql,params)
        if cursor.description is None:
            self.description=None;self.rows=[];return self
        names=[d[0] for d in cursor.description]
        rows=cursor.fetchall()
        if projected:
            result=[]
            for values in rows:
                value=dict(zip(names,values))
                if 'context_items' in sql:
                    value['tags']=json.loads(value['tags']);value['projects']=json.loads(value['projects'])
                    value['personal_general']=bool(value['personal_general'])
                result.append((value,))
            return self.values(result)
        if 'result_json' in names:
            index=names.index('result_json')
            rows=[tuple(json.loads(x) if i==index and x is not None else x for i,x in enumerate(r)) for r in rows]
        return self.values(rows,names)

    def result(self, rows):
        names=list(rows[0]) if rows else []
        return self.values([tuple(r[k] for k in names) for r in rows],names)
    def values(self, rows, names=None):
        self.rows=list(rows)
        names=names if names is not None else [str(i) for i in range(len(rows[0]) if rows else 0)]
        self.description=[SimpleNamespace(name=x) for x in names]
        return self

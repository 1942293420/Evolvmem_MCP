"""Typed driver-port qualification tests; no PostgreSQL or HTTP is started.

Breaks caught: losing exact JOIN eligibility inserts two redundant timeout
setters; accepting modified/unbound SQL or stale caps suppresses required
setters. The real policy, guard, guarded cursor and PgBusinessCore._pending
execute unchanged; only the external Psycopg wire endpoint is synthetic.
"""
from contextlib import contextmanager
import hashlib
from types import SimpleNamespace as NS
import sys
from unittest.mock import patch
import pytest
from evolvmem.library_deadline import timeout_reuse
from evolvmem.library_deadline.driver import connect
from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from evolvmem.library_foundation import FoundationError
from evolvmem.pg_business_core.core import PgBusinessCore
from tests.library_deadline.test_timeout_reuse import Raw, Cursor, Status, CONTENT_SQL, SETTER

JOIN_SQL = """SELECT to_jsonb(c),l.content
                FROM em_content.context_items c
                LEFT JOIN em_content.context_layers l
                  ON l.library_id=c.library_id
                 AND l.project_id IS NOT DISTINCT FROM c.project_id
                 AND l.item_id=c.id
                 AND l.layer='l0'
                WHERE c.library_id=%s
                  AND c.project_id IS NOT DISTINCT FROM %s
                  AND c.id=%s"""
OLD_ITEM_SQL='SELECT to_jsonb(c) FROM em_content.context_items c WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND id=%s'
OLD_LAYER_SQL="SELECT content,content_hash FROM em_content.context_layers WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND item_id=%s AND layer='l0'"
OUTBOX_SQL="""INSERT INTO em_content.vector_outbox(library_id,project_id,index_kind,entity_id,action,entity_revision,content_hash,model_contract,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(library_id,index_kind,entity_id,entity_revision,action,model_contract) DO NOTHING"""
LIB='00000000-0000-0000-0000-000000000004'
PROJECT='00000000-0000-0000-0000-000000000007'
CONTEXT=(LIB,PROJECT,41)
BODY='bounded synthetic context body'

class WireCursor(Cursor):
    def execute(self,sql,params=()):
        self.statement=sql
        return super().execute(sql,params)
    def fetchall(self):
        if self.raw.fail_fetch:return super().fetchall()
        if self.statement==JOIN_SQL:return (({'revision':9},BODY),)
        if self.statement==OLD_ITEM_SQL:return (({'revision':9},),)
        if self.statement==OLD_LAYER_SQL:return ((BODY,hashlib.sha256(BODY.encode()).hexdigest()),)
        return super().fetchall()

class WireRaw(Raw):
    def cursor(self):
        if self.raise_cursor:return super().cursor()
        return WireCursor(self)

class SessionBoundary:
    def __init__(self,conn):self.conn=conn
    def execute(self,sql,params=()):
        with self.conn.cursor() as cursor:
            cursor.execute(sql,params)
            return cursor.fetchall()


def attested(**over):
    values=dict(kind='content',host='db.example',hostaddr='10.0.0.5',port=5432,
        database='content',user='runtime',sslmode='verify-full',sslrootcert='/ca.pem',
        ca_sha256='a'*64,statement_ms=10000,lock_ms=10000,
        source_sha256='b'*64,proof_sha256='c'*64)
    values.update(over)
    return timeout_reuse.attest(**values)


@contextmanager
def opened(*,profile=True,raw=None,seconds=12):
    timeout_reuse.clear_attested_profiles()
    raw=raw or WireRaw()
    guard=SyncRequestDeadlineGuard(seconds)
    kwargs=dict(host='db.example',hostaddr='10.0.0.5',port=5432,dbname='content',
        user='runtime',password='synthetic',sslmode='verify-full',sslrootcert='/ca.pem',
        connect_timeout=3,autocommit=True)
    actual=attested() if profile is True else profile
    fake=NS(Connection=Raw,capabilities=NS(has_cancel_safe=lambda **kw:True))
    try:
        with patch.dict(sys.modules,{'psycopg':fake}),guard.enforce():
            conn=connect(guard,lambda **kw:raw,kwargs,kind='content',statement_ms=10000,
                         lock_ms=10000,profile=actual)
            session=SessionBoundary(conn)
            session.execute('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ WRITE')
            yield guard,conn,session,raw
    finally:timeout_reuse.clear_attested_profiles()


def enqueue(session):
    core=PgBusinessCore.__new__(PgBusinessCore)
    core.session=session;core.lib=LIB;core.project_id=PROJECT;core.model_contract='model-contract-v1'
    core._pending('context',41,'upsert')


@pytest.mark.parametrize('warm', [False,True], ids=['cold','warm'])
def test_real_pending_join_and_outbox_keep_installed_caps(warm):
    with opened() as (guard,conn,session,raw):
        if warm:session.execute(CONTENT_SQL)
        before=(len(raw.calls),raw.setters,conn.timeout_reuse_hits)
        enqueue(session)
        calls=raw.calls[before[0]:]
        business=[(sql,params) for sql,params in calls if SETTER not in sql]
        assert len(business)==2
        assert business[0]==(JOIN_SQL,CONTEXT)
        assert business[1][0]==OUTBOX_SQL
        assert business[1][1][:-1]==(LIB,PROJECT,'context',41,'upsert',9,
            '8ebba87245a0fdc715fe60ff5ede1dc7a2ab62ea634f0b883adfdea5e8db3eed',
            'model-contract-v1')
        assert raw.setters-before[1]==(0 if warm else 1)
        assert conn.timeout_reuse_hits-before[2]==(2 if warm else 1)
        assert all(params==('10000ms','10000ms') for sql,params in raw.calls if SETTER in sql)


@pytest.mark.parametrize('warm',[False,True],ids=['cold','warm'])
def test_original_item_layer_outbox_chain_remains_qualified(warm):
    with opened() as (_,conn,session,raw):
        if warm:session.execute(CONTENT_SQL)
        before=(raw.setters,conn.timeout_reuse_hits)
        for sql in (OLD_ITEM_SQL,OLD_LAYER_SQL):session.execute(sql,CONTEXT)
        session.execute(OUTBOX_SQL,('synthetic',)*9)
        assert raw.setters-before[0]==(0 if warm else 1)
        assert conn.timeout_reuse_hits-before[1]==(3 if warm else 2)


@pytest.mark.parametrize('sql',[
    JOIN_SQL+' ',
    JOIN_SQL+'; SELECT 1',
    JOIN_SQL.replace('LEFT JOIN','JOIN'),
    JOIN_SQL.replace("l.layer='l0'","l.layer='l1'"),
    JOIN_SQL.replace('l.library_id=c.library_id','TRUE'),
    JOIN_SQL.replace('l.project_id IS NOT DISTINCT FROM c.project_id','TRUE'),
    JOIN_SQL.replace('c.project_id IS NOT DISTINCT FROM %s','c.project_id=%s'),
],ids=['trailing-space','extra-statement','inner-join','other-layer','missing-library','missing-join-project','other-project-predicate'])
def test_join_variants_run_setter_and_invalidate_next_outbox(sql):
    with opened() as (_,conn,session,raw):
        session.execute(CONTENT_SQL)
        before=(raw.setters,conn.timeout_reuse_hits)
        session.execute(sql,CONTEXT)
        session.execute(OUTBOX_SQL,('synthetic',)*9)
        assert raw.setters-before[0]==2
        assert conn.timeout_reuse_hits-before[1]==0


@pytest.mark.parametrize('case',['none','wrong-role','wrong-source-format','wrong-caps','resolved-role'])
def test_join_has_no_reuse_without_exact_valid_profile(case):
    from dataclasses import replace
    profile={'none':None,'wrong-role':attested(user='other'),
        'wrong-source-format':replace(attested(),source_sha256='invalid'),
        'wrong-caps':attested(statement_ms=1000,lock_ms=1000),
        'resolved-role':attested()}[case]
    raw=WireRaw(user='other') if case=='resolved-role' else WireRaw()
    with opened(profile=profile,raw=raw) as (_,conn,session,raw):
        before=(raw.setters,conn.timeout_reuse_hits)
        enqueue(session)
        assert raw.setters-before[0]==2
        assert conn.timeout_reuse_hits-before[1]==0


def test_unknown_sql_between_join_and_outbox_destroys_reuse():
    with opened() as (_,conn,session,raw):
        session.execute(JOIN_SQL,CONTEXT)
        before=(raw.setters,conn.timeout_reuse_hits)
        session.execute('SELECT 1')
        session.execute(OUTBOX_SQL,('synthetic',)*9)
        assert raw.setters-before[0]==2
        assert conn.timeout_reuse_hits-before[1]==0


@pytest.mark.parametrize('boundary',['SAVEPOINT s','COMMIT','ROLLBACK'])
def test_join_caps_do_not_survive_transaction_boundary(boundary):
    with opened() as (_,conn,session,raw):
        session.execute(JOIN_SQL,CONTEXT)
        session.execute(boundary)
        session.execute('BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ WRITE')
        before=(raw.setters,conn.timeout_reuse_hits)
        session.execute(OUTBOX_SQL,('synthetic',)*9)
        assert raw.setters-before[0]==1
        assert conn.timeout_reuse_hits-before[1]==0


def test_below_cap_budget_never_reuses_join_or_outbox():
    with opened(seconds=.5) as (_,conn,session,raw):
        before=raw.setters
        enqueue(session)
        assert raw.setters-before==2
        assert conn.timeout_reuse_hits==0
        assert all(0<int(params[0][:-2])<10000 for sql,params in raw.calls if SETTER in sql)


def test_join_fetch_failure_invalidates_caps_before_next_outbox():
    with opened() as (_,conn,session,raw):
        raw.fail_fetch=True
        with pytest.raises(RuntimeError,match='fetch failure'):
            session.execute(JOIN_SQL,CONTEXT)
        before=(raw.setters,conn.timeout_reuse_hits)
        session.execute(OUTBOX_SQL,('synthetic',)*9)
        assert raw.setters-before[0]==1
        assert conn.timeout_reuse_hits-before[1]==0

"""Continuation reads own their identity and committed connection, never the business lock."""
import concurrent.futures
import json
import sqlite3
import threading
import pytest

from tests.test_lan_sharing import lan
from tests.test_lan_context import begin,resume,PATH,SNAP


@pytest.mark.parametrize('name',['continuity_resume','continuity_list','continuity_find'])
def test_continuity_read_finishes_while_business_lock_is_held(lan,name):
    runtime,adapter=lan
    saved=begin(adapter)
    args=dict(workspace_path=PATH,device_id='pc1')
    args.update(query='demo') if name=='continuity_find' else args.update(project_hint='demo')
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        with adapter.lock:
            future=pool.submit(adapter.call_tool,'kane',name,args)
            try:result=future.result(timeout=1)
            except concurrent.futures.TimeoutError:pytest.fail('continuity read waited for business lock')
    assert 'error' not in result,result
    assert saved['workstream_id'] in json.dumps(result)


def test_continuity_read_never_enters_resident_identity_or_health(lan,monkeypatch):
    runtime,adapter=lan
    saved=begin(adapter)
    server=runtime.server_for('kane')
    def forbidden(*a,**kw):pytest.fail('shared mutable service was accessed')
    monkeypatch.setattr(server,'_workspace_identity',forbidden)
    monkeypatch.setattr(server,'_continuity',forbidden)
    monkeypatch.setattr(server,'_contract_view',forbidden)
    assert resume(adapter)['workstream_id']==saved['workstream_id']


def test_continuity_read_sees_only_committed_state(lan):
    runtime,adapter=lan
    saved=begin(adapter)
    conn=runtime.server_for('kane').context_service.store._connection()
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute('UPDATE continuity_workstreams SET checkpoint_revision=999 WHERE id=?',(saved['workstream_id'],))
        result=resume(adapter)
        assert result['checkpoint_revision']==saved['checkpoint_revision']
        assert conn.in_transaction
    finally:conn.rollback()


def test_continuity_mutation_keeps_business_lock(lan):
    runtime,adapter=lan
    entered=threading.Event()
    def write():
        entered.set()
        return begin(adapter)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        with adapter.lock:
            future=pool.submit(write)
            assert entered.wait(1)
            with pytest.raises(concurrent.futures.TimeoutError):future.result(timeout=.1)
        assert 'error' not in future.result(timeout=2)


@pytest.mark.parametrize('fail',[False,True])
def test_continuity_connection_is_readonly_and_closed(lan,monkeypatch,fail):
    from evolvmem.continuity_service import ContinuityService
    runtime,adapter=lan
    begin(adapter)
    resident=runtime.server_for('kane').context_service.store._connection()
    original=ContinuityService.resume
    observed=[]
    def read(service,request):
        conn=service._store._connection()
        assert conn is not resident
        assert conn.execute('PRAGMA query_only').fetchone()[0]==1
        assert conn.in_transaction and conn.total_changes==0
        observed.append(conn)
        with pytest.raises(sqlite3.OperationalError,match='readonly'):
            conn.execute('UPDATE continuity_workstreams SET checkpoint_revision=999')
        if fail:raise RuntimeError('synthetic')
        return original(service,request)
    monkeypatch.setattr(ContinuityService,'resume',read)
    result=resume(adapter)
    assert ('error' in result)==fail,result
    assert len(observed)==1
    with pytest.raises(sqlite3.ProgrammingError,match='closed'):observed[0].execute('SELECT 1')


def test_concurrent_devices_and_users_do_not_share_identity(lan):
    runtime,adapter=lan
    cases=[('jiangli','pc1'),('kane','pc1'),('kane','pc2')]
    saved={(user,device):begin(adapter,user,device,rid='begin-'+device)['workstream_id'] for user,device in cases}
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        jobs=[(key,pool.submit(resume,adapter,*key,repo_snapshot=SNAP)) for key in cases*3]
        for key,future in jobs:
            result=future.result(timeout=2)
            assert result['workstream_id']==saved[key]
            assert result['authenticated_user']==key[0]
            assert result['repo_source']=='client_reported'
    for user in ('jiangli','kane'):
        server=runtime.server_for(user)
        assert server._continuity()._workspace_identity is server._workspace_identity()


@pytest.mark.parametrize('changes,error',[
    ({'space':'public'},'public_write_forbidden'),
    ({'user':'jiangli'},'identity_override_forbidden'),
    ({'device_id':'bad device'},'invalid_device_id'),
    ({'repo_snapshot':{'kind':'git'}},'invalid_repo_snapshot'),
    ({'unknown':True},'invalid_arguments'),
])
def test_continuity_read_keeps_validation(lan,changes,error):
    _,adapter=lan
    args=dict(workspace_path=PATH,device_id='pc1',project_hint='demo')
    assert adapter.call_tool('kane','continuity_resume',{**args,**changes}).get('error')==error


def test_continuity_database_replacement_fails_closed(lan,tmp_path):
    runtime,adapter=lan
    begin(adapter)
    path=runtime.server_for('kane').config.db_path
    path.rename(tmp_path/'original.db')
    path.touch()
    assert resume(adapter).get('error')=='remote_tool_failed'


def test_commit_during_read_does_not_mix_focus_and_checkpoint(lan,monkeypatch):
    from evolvmem.continuity_service import ContinuityService
    runtime,adapter=lan
    saved=begin(adapter)
    original=ContinuityService._schema_ready
    committed=[]
    def gate(service):
        if not committed:
            other=sqlite3.connect(runtime.server_for('kane').config.db_path)
            try:
                other.execute('UPDATE continuity_workstreams SET checkpoint_revision=999 WHERE id=?',(saved['workstream_id'],))
                other.commit()
                committed.append(True)
            finally:other.close()
        return original(service)
    monkeypatch.setattr(ContinuityService,'_schema_ready',gate)
    assert resume(adapter)['checkpoint_revision']==saved['checkpoint_revision']
    assert committed==[True]


def test_reader_limit_reports_busy_then_recovers(lan):
    from evolvmem.lan_continuity_reads import MAX_READERS
    _,adapter=lan
    saved=begin(adapter)
    slots=adapter._continuity_reader._slots
    for _ in range(MAX_READERS):assert slots.acquire(blocking=False)
    try:assert resume(adapter).get('error')=='continuity_read_busy'
    finally:
        for _ in range(MAX_READERS):slots.release()
    assert resume(adapter)['workstream_id']==saved['workstream_id']


def test_multiple_adapters_share_the_runtime_reader_limit(lan):
    from evolvmem.lan_tools import LanTools
    from evolvmem.lan_continuity_reads import MAX_READERS
    runtime,first=lan
    saved=begin(first)
    second=LanTools(runtime)
    slots=first._continuity_reader._slots
    for _ in range(MAX_READERS):assert slots.acquire(blocking=False)
    try:assert resume(second).get('error')=='continuity_read_busy'
    finally:
        for _ in range(MAX_READERS):slots.release()
    assert resume(second)['workstream_id']==saved['workstream_id']


@pytest.mark.parametrize('during',[False,True])
def test_closed_runtime_never_returns_a_checkpoint(lan,monkeypatch,during):
    from evolvmem.continuity_service import ContinuityService
    runtime,adapter=lan
    begin(adapter)
    original=ContinuityService.resume
    def read(service,request):
        result=original(service,request)
        runtime._closed=True
        return result
    if during:monkeypatch.setattr(ContinuityService,'resume',read)
    else:runtime._closed=True
    try:assert resume(adapter).get('error')=='remote_tool_failed'
    finally:runtime._closed=False


def test_sql_deadline_releases_connection_and_permit(lan,monkeypatch):
    import evolvmem.lan_continuity_reads as implementation
    _,adapter=lan
    saved=begin(adapter)
    with monkeypatch.context() as patch:
        patch.setattr(implementation,'SQL_TIMEOUT_SECONDS',-1)
        result=resume(adapter)
        assert 'error' in result,result
    assert resume(adapter)['workstream_id']==saved['workstream_id']


def test_missing_key_keeps_find_without_workspace_available(lan):
    runtime,adapter=lan
    saved=begin(adapter)
    key=runtime.server_for('kane').config.data_dir/'workspace.key'
    key.rename(key.with_suffix('.test-retained'))
    found=adapter.call_tool('kane','continuity_find',{'query':'demo'})
    assert saved['workstream_id'] in json.dumps(found)
    assert not key.exists()
    assert 'error' in resume(adapter)

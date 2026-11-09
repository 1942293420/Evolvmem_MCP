"""Ordinary HTTP authentication tests with temporary accounts and source DBs."""
import hashlib
import http.client
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

STAGE=Path(__file__).parent


def load(name,filename,monkeypatch):
    path=STAGE/filename
    if not path.exists():
        installed=importlib.util.find_spec('evolvmem.'+Path(filename).stem)
        if installed and installed.origin:
            path=Path(installed.origin)
    assert path.exists(), filename+' implementation is missing'
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules,name,module)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate_modules(monkeypatch):
    pytest.importorskip('evolvmem')
    policy=load('evolvmem.product_policy','product_policy.py',monkeypatch)
    console=load('evolvmem.knowledge_console','knowledge_console.py',monkeypatch)
    helper=load('evolvmem.console_access','console_access.py',monkeypatch)
    server=load('console_lan_under_test','lan_server.py',monkeypatch)
    return policy,console,helper,server


@pytest.fixture
def service(gate_modules,tmp_path,monkeypatch):
    policy,console,helper,server_module=gate_modules
    from evolvmem.lan_config import LanSettings
    from evolvmem.lan_runtime import LanRuntime
    path=tmp_path/'policy.json'
    path.write_text(json.dumps({'console_enabled':True,'legacy_web_enabled':False,'ai_processing_enabled':False}))
    registry=tmp_path/'registry'/'users.db'
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE',str(path))
    monkeypatch.setenv('EVOLVMEM_CONSOLE_REGISTRY',str(registry))
    monkeypatch.setenv('EVOLVMEM_CONSOLE_OWNER_ID','jiangli')
    monkeypatch.setenv('EVOLVMEM_LAN_DATA_DIR',str(tmp_path/'lan'))
    settings=LanSettings(data_dir=tmp_path/'lan',owner_data_dir=tmp_path/'owner',
        token_hashes={name:hashlib.sha256((name+'-fixture-token').encode()).hexdigest() for name in ('jiangli','kane')},
        embedding_enabled=False)
    runtime=LanRuntime(settings)
    runtime.initialize()
    svc=console.ConsoleService(registry,'jiangli',settings.owner_data_dir,settings.data_dir,{})
    web=server_module.make_http_server(runtime,host='127.0.0.1',port=0)
    worker=threading.Thread(target=web.serve_forever,daemon=True)
    worker.start()
    yield web,svc,path,registry,helper,settings
    web.shutdown()
    worker.join(timeout=3)
    web.server_close()
    runtime.close()


def request(server,user='kane',path='/mcp',token=None,method='ping',params=None):
    client=http.client.HTTPConnection(*server.server_address,timeout=5)
    client.request('POST',path,body=json.dumps({'jsonrpc':'2.0','id':1,'method':method,'params':params or {}}),
        headers={'Authorization':'Bearer '+(token if token is not None else user+'-fixture-token'),
                 'Content-Type':'application/json'})
    response=client.getresponse()
    raw=response.read()
    status=response.status
    client.close()
    return status,json.loads(raw)


def manual_note(request_id='fixture-manual-note-1'):
    return dict(category='前端经验', problem='回滚后节点状态仍显示完成',
                solution='用当前阶段重算进度。', reusable_experience='区分当前状态和历史操作。',
                request_id=request_id)


def tool(server,name,args,user='jiangli',path='/mcp'):
    status,body=request(server,user=user,path=path,method='tools/call',params={'name':name,'arguments':args})
    assert status==200, body
    result=body['result']
    data=json.loads(result['content'][0]['text'])
    assert result['isError']==('error' in data)
    return data


def test_manual_mcp_write_and_search_share_the_web_library_without_core_health(service,monkeypatch):
    server,svc,*_=service
    from evolvmem.mcp_server import MemoryMCPServer
    svc.bootstrap('fixture-owner-open-id','Owner')
    owner_session={'open_id':'fixture-owner-open-id'}
    def deny_core_view(*_args,**_kwargs):
        raise AssertionError('manual experiences must not evaluate Core health')
    monkeypatch.setattr(MemoryMCPServer,'_contract_view',deny_core_view)
    status,body=request(server,user='jiangli',method='tools/list')
    assert status==200
    specs={spec['name']:spec for spec in body['result']['tools']}
    assert set(specs['manual_experience_save']['inputSchema']['required'])==set(manual_note())
    assert specs['manual_experience_search']['annotations']['readOnlyHint'] is True
    assert tool(server,'manual_experience_search',{})['rows']==[]
    assert tool(server,'manual_experience_save',{},path='/owner/mcp')['error']=='invalid_request_id'
    saved=tool(server,'manual_experience_save',manual_note())
    note=saved['experience']
    assert saved['ok'] and saved['authenticated_user']=='jiangli' and saved['replayed'] is False
    assert note['origin']=='manual' and note['category']=='前端经验'
    replay=tool(server,'manual_experience_save',manual_note())
    assert replay['replayed'] is True and replay['experience']==note
    assert tool(server,'manual_experience_save',{**manual_note(),'problem':'different'})['error']=='experience_request_id_conflict'
    assert tool(server,'manual_experience_search',{'q':'回滚','category':'前端经验'})['rows']==[note]
    assert tool(server,'manual_experience_search',{'category':'业务经验'})['rows']==[]
    assert tool(server,'manual_experience_search',{'q':'does-not-exist'})['rows']==[]
    status,_,web=svc.dispatch('GET','/libraries/jiangli/experiences',{'origin':'manual'},owner_session)
    assert status==200 and web['total']==1 and web['rows']==[note]
    deleted=svc.dispatch('POST','/libraries/jiangli/experiences',dict(action='delete',id=note['id'],
        revision=note['revision'],request_id='fixture-manual-delete'),owner_session)
    assert deleted[0]==200 and tool(server,'manual_experience_search',{})['rows']==[]


def test_manual_mcp_isolates_users_rejects_overrides_and_public_space(service):
    server,svc,*_=service
    svc.bootstrap('fixture-owner-open-id','Owner')
    owner_note=tool(server,'manual_experience_save',manual_note())['experience']
    assert tool(server,'manual_experience_search',{},user='kane')['rows']==[]
    member_note=tool(server,'manual_experience_save',{
        **manual_note(),'category':'业务经验','problem':'按供应商配置核对口径'},user='kane')['experience']
    assert member_note['id']!=owner_note['id']
    assert tool(server,'manual_experience_search',{},user='kane')['rows']==[member_note]
    assert tool(server,'manual_experience_search',{})['rows']==[owner_note]
    for name,args in [('manual_experience_search',{}),('manual_experience_save',manual_note())]:
        assert tool(server,name,{**args,'space':'public'},user='kane')['error']=='public_write_forbidden'
        assert tool(server,name,{**args,'user_id':'jiangli'},user='kane')['error']=='identity_override_forbidden'
        assert tool(server,name,{**args,'owner_id':'jiangli'},user='kane')['error']=='invalid_arguments'


def test_manual_mcp_fails_closed_when_registered_web_source_differs(service,tmp_path):
    server,svc,*_=service
    svc.bootstrap('fixture-owner-open-id','Owner')
    other=tmp_path/'other-source';other.mkdir()
    with sqlite3.connect(other/'memory.db') as conn:
        conn.execute('CREATE TABLE untouched(id INTEGER)')
    svc.configure_library('jiangli',data_dir=other)
    assert tool(server,'manual_experience_save',manual_note())['error']=='manual_experience_source_mismatch'
    assert tool(server,'manual_experience_search',{})['error']=='manual_experience_source_mismatch'
    for path in (other/'memory.db',svc.owner_data_dir/'memory.db'):
        with sqlite3.connect(path) as conn:
            assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='manual_experience_notes'").fetchone()


def test_manual_mcp_search_caps_context_without_shortening_the_saved_note(service):
    server,svc,*_=service
    svc.bootstrap('fixture-owner-open-id','Owner')
    note=tool(server,'manual_experience_save',{**manual_note(),'problem':'甲'*1000,
        'solution':'乙'*8000,'reusable_experience':'丙'*8000})['experience']
    for index in range(10):
        tool(server,'manual_experience_save',manual_note(f'fixture-manual-page-{index}'))
    assert len(note['solution'])==8000
    result=tool(server,'manual_experience_search',{})
    assert result['total']==11 and len(result['rows'])==10
    matched=tool(server,'manual_experience_search',{'q':'丙丙丙'})['rows']
    assert len(matched)==1 and matched[0]['id']==note['id']
    assert matched[0]['truncated_fields']==['problem','solution','reusable_experience']
    assert len(matched[0]['problem'])==300 and len(matched[0]['solution'])==600
    status,_,web=svc.dispatch('GET',f"/libraries/jiangli/experiences/{note['id']}",
        {'origin':'manual'},{'open_id':'fixture-owner-open-id'})
    assert status==200 and web['experience']==note


def test_native_stdio_lists_and_saves_manual_experiences_with_degraded_core(service,monkeypatch):
    _,svc,*_=service
    from evolvmem.config import Config
    from evolvmem.mcp_server import MemoryMCPServer
    svc.bootstrap('fixture-owner-open-id','Owner')
    native=MemoryMCPServer(Config(data_dir=svc.owner_data_dir,adapter='codex',context_mode='primary',
                                  apply_environment=False),embedding_engine=None)
    native._init_done.set()
    specs=native._handle_request({'id':1,'method':'tools/list'})['result']['tools']
    names={spec['name'] for spec in specs}
    assert {'manual_experience_save','manual_experience_search'}<=names
    assert 'experience_record' not in names
    def deny_core_view(*_args,**_kwargs):
        raise AssertionError('manual experiences must not evaluate Core health')
    monkeypatch.setattr(native,'_contract_view',deny_core_view)
    def call(name,args):
        result=native._handle_request({'id':2,'method':'tools/call','params':{
            'name':name,'arguments':args}})['result']
        return json.loads(result['content'][0]['text'])
    note=call('manual_experience_save',manual_note())['experience']
    assert call('manual_experience_save',manual_note())['replayed'] is True
    assert call('manual_experience_search',{})['rows']==[note]
    status,_,web=svc.dispatch('GET','/libraries/jiangli/experiences',{'origin':'manual'},
                              {'open_id':'fixture-owner-open-id'})
    assert status==200 and web['rows']==[note]
    native.config.data_dir=svc.lan_data_dir/'users'/'kane'
    assert call('manual_experience_save',manual_note())['error']=='manual_experience_source_mismatch'


def test_linux_stdio_relay_preserves_manual_request_id_and_search(service,tmp_path):
    server,svc,*_=service
    svc.bootstrap('fixture-owner-open-id','Owner')
    token_file=tmp_path/'fixture-token'
    token_file.write_text('jiangli-fixture-token')
    token_file.chmod(0o600)
    config=tmp_path/'fixture-relay.json'
    config.write_text(json.dumps({'url':f'http://127.0.0.1:{server.server_port}/owner/mcp',
                                  'token_file':str(token_file)}))
    client_dir=tmp_path/'client'
    client_dir.mkdir()
    (client_dir/'config.json').write_text(json.dumps({'lan_mcp_client_config':str(config)}))
    commands=[('manual_experience_save',manual_note()),('manual_experience_save',manual_note()),
              ('manual_experience_search',{'q':'回滚'})]
    requests=[{'jsonrpc':'2.0','id':index,'method':'tools/call','params':{
        'name':name,'arguments':args}} for index,(name,args) in enumerate(commands,1)]
    proc=subprocess.run([sys.executable,'-m','evolvmem.mcp_server'],
        input='\n'.join(map(json.dumps,requests))+'\n',text=True,encoding='utf-8',
        env={**os.environ,'EVOLVMEM_DATA_DIR':str(client_dir)},capture_output=True,timeout=15)
    assert proc.returncode==0, proc.stderr
    responses=[json.loads(line)['result'] for line in proc.stdout.splitlines()]
    assert len(responses)==3 and not any(response['isError'] for response in responses)
    saved,replay,search=[json.loads(response['content'][0]['text']) for response in responses]
    assert saved['replayed'] is False and replay['replayed'] is True
    assert search['total']==1 and search['rows']==[saved['experience']]
    assert not (client_dir/'memory.db').exists()


def test_console_gate_missing_registry_is_503_and_does_not_create_it(service):
    server,svc,policy,registry,*_=service
    status,body=request(server)
    assert status==503 and body=={'error':'console_registry_unavailable'}
    assert not registry.exists() and not registry.parent.exists()
    assert request(server,token='wrong-fixture-token')[0]==401


def test_console_gate_disabled_known_user_revokes_existing_bearer_immediately(service):
    server,svc,*_=service
    svc.bootstrap('fixture-owner-open-id','Owner')
    assert request(server)[0]==200
    assert svc.dispatch('POST','/users/kane',{'enabled':False},{'open_id':'fixture-owner-open-id'})[0]==200
    status,body=request(server)
    assert status==403 and body=={'error':'account_disabled'}
    for path in ('/embedding','/embedding/status','/rerank'):
        assert request(server,path=path)[0]==403
    assert request(server,user='jiangli')[0]==200
    assert svc.dispatch('POST','/users/kane',{'enabled':True},{'open_id':'fixture-owner-open-id'})[0]==200
    assert request(server)[0]==200


def test_console_gate_legacy_policy_keeps_existing_auth_without_registry(service):
    server,svc,policy,registry,*_=service
    policy.write_text(json.dumps({'console_enabled':False}))
    assert request(server)[0]==200
    assert not registry.exists()
    assert request(server,token='wrong-fixture-token')[0]==401


def test_console_gate_corrupt_or_mismatched_registry_is_bounded_503(service):
    server,svc,policy,registry,*_=service
    registry.parent.mkdir()
    registry.write_bytes(b'synthetic corrupt fixture')
    status,body=request(server)
    assert status==503 and body=={'error':'console_registry_unavailable'}
    assert str(registry) not in json.dumps(body)


def test_console_gate_invalid_policy_fails_closed(service):
    server,svc,policy,*_=service
    policy.write_text('{invalid synthetic fixture')
    assert request(server)[0]==503


def test_console_gate_wrong_registry_owner_is_503(service):
    server,svc,policy,registry,*_=service
    svc.bootstrap('fixture-owner-open-id','Owner')
    with sqlite3.connect(registry) as conn:
        conn.execute("UPDATE console_meta SET value='synthetic-different-owner' WHERE key='owner_id'")
    assert request(server)==(503,{'error':'console_registry_unavailable'})


def test_shared_constructor_uses_explicit_server_env_paths(gate_modules,tmp_path,monkeypatch):
    _,console,helper,_=gate_modules
    monkeypatch.setenv('EVOLVMEM_CONSOLE_REGISTRY',str(tmp_path/'configured.db'))
    monkeypatch.setenv('EVOLVMEM_CONSOLE_OWNER_ID','synthetic-owner')
    monkeypatch.setenv('EVOLVMEM_LAN_DATA_DIR',str(tmp_path/'configured-lan'))
    svc=helper.configured_console(owner_data_dir=tmp_path/'owner',lan_data_dir=tmp_path/'runtime-lan')
    assert svc.registry_path==tmp_path/'configured.db'
    assert svc.owner_id=='synthetic-owner'
    assert svc.lan_data_dir==tmp_path/'configured-lan'
    assert not svc.registry_path.exists()


def test_shared_constructor_defaults_are_synthetic_and_have_no_read_side_effects(gate_modules,tmp_path,monkeypatch):
    _,console,helper,_=gate_modules
    for name in ('EVOLVMEM_CONSOLE_REGISTRY','EVOLVMEM_CONSOLE_OWNER_ID','EVOLVMEM_LAN_DATA_DIR'):
        monkeypatch.delenv(name,raising=False)
    synthetic_home=tmp_path/'synthetic-home'
    monkeypatch.setattr(helper.Path,'home',classmethod(lambda cls:synthetic_home))
    svc=helper.configured_console()
    assert svc.registry_path==synthetic_home/'.config/evolvmem-console/users.db'
    assert svc.owner_id=='jiangli'
    assert svc.lan_data_dir==synthetic_home/'.local/share/evolvmem-lan'
    assert not synthetic_home.exists()

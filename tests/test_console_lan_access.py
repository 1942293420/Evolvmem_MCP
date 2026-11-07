"""Ordinary HTTP authentication tests with temporary accounts and source DBs."""
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import sqlite3
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


def request(server,user='kane',path='/mcp',token=None):
    client=http.client.HTTPConnection(*server.server_address,timeout=5)
    client.request('POST',path,body=json.dumps({'jsonrpc':'2.0','id':1,'method':'ping'}),
        headers={'Authorization':'Bearer '+(token if token is not None else user+'-fixture-token'),
                 'Content-Type':'application/json'})
    response=client.getresponse()
    raw=response.read()
    status=response.status
    client.close()
    return status,json.loads(raw)


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

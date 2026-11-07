"""Real make_handler/WebAuth through ordinary HTTP, synthetic sessions only."""
import hashlib
import http.client
import importlib.util
import json
from pathlib import Path
import socket

import pytest

spec=importlib.util.spec_from_file_location('console_web_fixture',Path(__file__).parent/'browser'/'launch_console_fixture.py')
fixture_module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture_module)
PREFIX='/api/knowledge-console'


@pytest.fixture
def fixture(tmp_path):
    value=fixture_module.Fixture(tmp_path/'synthetic-console').start()
    yield value
    value.close()


def request(fixture,path,actor='owner',method='GET',body=None,csrf=False):
    client=http.client.HTTPConnection('127.0.0.1',fixture.server.server_port,timeout=5)
    headers={}
    if actor:
        headers['Cookie']='evolvmem_session=test-'+actor+'-cookie'
    if csrf:
        headers['X-CSRF-Token']='test-'+actor+'-csrf' if csrf is True else csrf
    raw=None
    if body is not None:
        headers['Content-Type']='application/json'
        raw=json.dumps(body)
    client.request(method,path,body=raw,headers=headers)
    response=client.getresponse()
    data=response.read()
    status=response.status
    mime=response.getheader('Content-Type','')
    client.close()
    return status,json.loads(data) if 'application/json' in mime else data


def grant(fixture,viewer,owner,history=False,experience=False,revision=0):
    return request(fixture,PREFIX+'/grants',method='POST',csrf=True,body={
        'viewer_id':viewer,'owner_id':owner,'history_read':history,'experience_read':experience,'revision':revision})


def test_web_trusted_sessions_pending_and_no_pre_authorization_owner_snapshot(fixture):
    assert request(fixture,PREFIX+'/me',actor=None)[0]==401
    assert request(fixture,PREFIX+'/me',actor='pending')[0]==403
    status,body=request(fixture,PREFIX+'/me',actor='member')
    assert status==200 and body['current_user']['id']=='kane'
    assert request(fixture,PREFIX+'/libraries/jiangli/experiences',actor='member')[0]==403
    assert fixture.reads==[]


def test_web_csrf_for_account_grant_and_self_history_mutations(fixture):
    target=PREFIX+'/users/kane'
    assert request(fixture,target,method='POST',body={'enabled':False})[0]==403
    assert request(fixture,target,method='POST',body={'enabled':False},csrf='wrong-csrf')[0]==403
    assert request(fixture,PREFIX+'/grants',actor='member',method='POST',body={},csrf=True)[0]==403
    delete={'request_id':'synthetic-http-delete','targets':[{'kind':'project_block','project':'automation_control','key':'blk-dialogue'}]}
    path=PREFIX+'/libraries/kane/history/management/delete'
    assert request(fixture,path,actor='member',method='POST',body=delete)[0]==403
    assert request(fixture,path,actor='member',method='POST',body=delete,csrf=True)[0]==200
    assert request(fixture,PREFIX+'/libraries/kane/history/block/blk-dialogue',actor='member')[0]==410


def test_web_two_colliding_experiences_directional_grants_and_explicit_inspect(fixture):
    assert fixture.owner_experience_id==fixture.member_experience_id
    item=str(fixture.owner_experience_id)
    owner=PREFIX+'/libraries/jiangli/experiences/'+item
    member=PREFIX+'/libraries/kane/experiences/'+item
    assert request(fixture,owner)[1]['problem']=='Owner verified experience'
    assert request(fixture,member,actor='member')[1]['problem']=='Member verified experience'
    assert request(fixture,member)[0]==403
    assert request(fixture,member+'?inspect=1')[1]['problem']=='Member verified experience'
    assert request(fixture,owner+'?inspect=1',actor='member')[0]==403
    assert grant(fixture,'kane','jiangli',experience=True)[0]==200
    assert request(fixture,owner,actor='member')[1]['problem']=='Owner verified experience'
    assert request(fixture,member)[0]==403
    assert grant(fixture,'kane','jiangli',revision=1)[0]==200
    assert request(fixture,owner,actor='member')[0]==403


def test_web_history_images_inspect_and_cross_user_management_are_guarded(fixture):
    history=PREFIX+'/libraries/kane/history'
    assert request(fixture,history+'/attachment/'+fixture.image_sha)[0]==403
    status,png=request(fixture,history+'/attachment/'+fixture.image_sha+'?inspect=1')
    assert status==200 and png==fixture.image
    assert request(fixture,history+'/management/trash?inspect=1')[0]==403
    assert request(fixture,history+'/management/delete?inspect=1',method='POST',body={},csrf=True)[0]==403
    assert request(fixture,history+'/projects?inspect=1&inspect=1')[0]==400


def test_web_legacy_410_does_not_open_snapshot_or_modify_sources(fixture):
    path=fixture.owner.config.db_path
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    for route in ('/api/memories','/api/projects','/api/capabilities','/api/knowledge/history',
                  '/api/history-candidate/projects','/history/attachment/'+fixture.image_sha):
        assert request(fixture,route,actor='member')[0]==410
    assert request(fixture,'/api/memory/1/delete',method='POST',body={},csrf=True)[0]==410
    assert fixture.reads==[]
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before


def test_web_stop_existing_session_and_bound_pending_login_metadata(fixture):
    assert request(fixture,PREFIX+'/users',actor='member')[0]==403
    _,users=request(fixture,PREFIX+'/users')
    assert len(users['pending'])==2
    assert request(fixture,PREFIX+'/users/kane',method='POST',csrf=True,body={'enabled':False})[0]==200
    assert request(fixture,PREFIX+'/me',actor='member')[0]==401
    assert request(fixture,'/api/auth/me',actor='member')[1]['authenticated'] is False
    assert request(fixture,PREFIX+'/users',method='POST',csrf=True,body={
        'id':'synthetic-new-user','display_name':'Synthetic New User','open_id':'test-pending-open-id-1'})[0]==201
    status,me=request(fixture,PREFIX+'/me',actor='pending')
    assert status==200 and me['current_user']['id']=='synthetic-new-user'
    assert me['libraries'][0]['history_connected'] is False
    assert request(fixture,PREFIX+'/users/synthetic-new-user',method='POST',csrf=True,body={'enabled':False})[0]==200
    assert request(fixture,PREFIX+'/me',actor='pending')[0]==401
    assert request(fixture,PREFIX+'/users/synthetic-new-user',method='POST',csrf=True,body={'enabled':True})[0]==200
    assert request(fixture,PREFIX+'/me',actor='pending')[0]==401


def test_web_negative_body_length_is_rejected_without_account_creation(fixture):
    client=http.client.HTTPConnection('127.0.0.1',fixture.server.server_port,timeout=5)
    body=json.dumps({'id':'negative-length-user','display_name':'Synthetic Negative Length'})
    client.request('POST',PREFIX+'/users',body=body,headers={
        'Cookie':'evolvmem_session=test-owner-cookie','X-CSRF-Token':'test-owner-csrf',
        'Content-Type':'application/json','Content-Length':'-1'})
    client.sock.shutdown(socket.SHUT_WR)
    response=client.getresponse()
    status=response.status
    response.read()
    client.close()
    assert status==400
    _,users=request(fixture,PREFIX+'/users')
    assert not any(row['id']=='negative-length-user' for row in users['users'])


@pytest.mark.parametrize('extra,expected,incomplete',[
    ([('Content-Length','2'),('Content-Length','100')],400,False),
    ([('Content-Length','2'),('Transfer-Encoding','chunked')],400,False),
    ([('Transfer-Encoding','chunked')],400,False),
    ([('Content-Length',str(512*1024+1))],413,False),
    ([('Content-Length','not-a-number')],400,False),
    ([('Content-Length','\xb2')],400,False),
    ([],400,True),
])
def test_web_invalid_framing_has_no_metadata_writes(fixture,extra,expected,incomplete):
    before=hashlib.sha256(fixture.console.registry_path.read_bytes()).hexdigest()
    body=json.dumps({'id':'invalid-framing-user','display_name':'Synthetic Invalid Framing'}).encode()
    client=http.client.HTTPConnection('127.0.0.1',fixture.server.server_port,timeout=5)
    client.putrequest('POST',PREFIX+'/users')
    client.putheader('Cookie','evolvmem_session=test-owner-cookie')
    client.putheader('X-CSRF-Token','test-owner-csrf')
    client.putheader('Content-Type','application/json')
    for name,value in extra:
        client.putheader(name,value)
    if incomplete:
        client.putheader('Content-Length',str(len(body)+50))
    client.endheaders(body)
    if incomplete:
        client.sock.shutdown(socket.SHUT_WR)
    response=client.getresponse()
    status=response.status
    response.read()
    client.close()
    assert status==expected
    assert hashlib.sha256(fixture.console.registry_path.read_bytes()).hexdigest()==before
    _,users=request(fixture,PREFIX+'/users')
    assert not any(row['id']=='invalid-framing-user' for row in users['users'])

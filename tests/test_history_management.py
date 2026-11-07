import hashlib
import json
import pathlib
import sqlite3
import uuid

import pytest
from tests.test_history_candidate_read import build_fixture
from evolvmem.history_candidate_web import dispatch
from tests.test_web_auth import protected_web, Browser
from tests.conftest import temp_dir, test_config


@pytest.fixture
def setup(tmp_path, monkeypatch):
    db = tmp_path / 'candidate.db'
    build_fixture(db)
    c = sqlite3.connect(db)
    c.execute("insert into block_projects values ('blk-dialogue','other','shared')")
    c.execute("delete from block_projects where block_id='blk-empty'")
    c.commit(); c.close()
    token = tmp_path / 'token'; token.write_text('secret')
    env = {'EVOLVMEM_HISTORY_CANDIDATE_DB': str(db),
           'EVOLVMEM_HISTORY_CANDIDATE_TOKEN_FILE': str(token),
           'EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE': 'personal',
           'EVOLVMEM_HISTORY_MANAGEMENT_DB': str(tmp_path / 'management.db')}
    return db, env


def call(env, path, body=None, owner=True):
    return dispatch('POST' if body is not None else 'GET', '/api/history-candidate/' + path,
                    body or {}, {'X-EvolvMem-Action': 'history-manage'}, env, owner_verified=owner)


def change(env, targets, action='delete', request_id=None):
    return call(env, 'management/' + action,
                {'targets': targets, 'request_id': request_id or str(uuid.uuid4())})


def test_delete_restore_shared_project_and_immutable_original(setup):
    db, env = setup; before = hashlib.sha256(db.read_bytes()).hexdigest()
    target = {'kind': 'project', 'project': 'automation_control', 'key': 'automation_control'}
    assert change(env, [target])[0] == 200
    assert [p['project'] for p in call(env, 'projects')[1]['projects']] == ['other']
    assert call(env, 'project/automation_control')[0] == 410
    assert call(env, 'block/blk-dialogue/turns')[0] == 200
    assert call(env, 'project/other')[1]['history_count'] == 1
    assert call(env, 'unassigned')[1]['empty_count'] == 1
    assert len(call(env, 'management/trash')[1]['items']) == 1
    assert change(env, [target], 'restore')[0] == 200
    assert call(env, 'project/automation_control')[0] == 200
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_project_block_delete_is_scoped(setup):
    _, env = setup
    t = {'kind': 'project_block', 'project': 'automation_control', 'key': 'blk-dialogue'}
    assert change(env, [t])[0] == 200
    assert call(env, 'project/automation_control')[1]['history_count'] == 0
    assert call(env, 'project/other')[1]['history_count'] == 1
    assert call(env, 'block/blk-dialogue/turns')[0] == 200
    assert change(env, [t], 'restore')[0] == 200
    assert call(env, 'project/automation_control')[1]['history_count'] == 1


def test_unassigned_delete_direct_links_and_restore(setup):
    _, env = setup
    t = {'kind': 'block', 'project': '', 'key': 'blk-empty'}
    assert change(env, [t])[0] == 200
    assert call(env, 'unassigned')[1]['empty_count'] == 0
    assert call(env, 'block/blk-empty')[0] == 410
    assert change(env, [t], 'restore')[0] == 200
    assert call(env, 'unassigned')[1]['empty_count'] == 1


def test_batch_atomic_validation_and_idempotency(setup):
    _, env = setup
    t = {'kind': 'project', 'project': 'other', 'key': 'other'}
    bad = {'kind': 'block', 'project': '', 'key': 'blk-dialogue'}
    assert change(env, [t, bad])[0] == 400
    assert len(call(env, 'projects')[1]['projects']) == 2
    rid = str(uuid.uuid4()); first = change(env, [t], request_id=rid)
    assert first[0] == 200
    assert change(env, [t], request_id=rid) == first
    assert change(env, [bad], request_id=rid)[0] == 409


def test_writes_require_owner_custom_header_and_valid_body(setup):
    _, env = setup
    body = {'targets': [{'kind': 'project', 'project': 'other', 'key': 'other'}], 'request_id': 'x'}
    assert call(env, 'management/delete', body, owner=False)[0] == 403
    assert dispatch('POST', '/api/history-candidate/management/delete', body, {}, env, owner_verified=True)[0] == 403
    assert call(env, 'management/delete', {'targets': [], 'request_id': 'x'})[0] == 400
    assert call(env, 'management/delete', ['bad'])[0] == 400
    assert call(env, 'projects', body)[0] == 405


def test_body_search_finds_text_and_excludes_deleted_relations(setup):
    _, env = setup
    c = sqlite3.connect(env['EVOLVMEM_HISTORY_CANDIDATE_DB'])
    c.execute("update messages set content='独特答案：前端登录修复' where block_id='blk-dialogue' and ordinal=1")
    c.commit(); c.close()
    params = {'q': '独特答案', 'scope': 'projects'}
    s, data = call(env, 'search', None) if False else dispatch('GET', '/api/history-candidate/search', params, {}, env, owner_verified=True)
    assert s == 200 and data['total'] == 2
    assert all('独特答案' in x['match']['snippet'] for x in data['items'])
    assert data['items'][0]['match']['page'] == 1
    change(env, [{'kind': 'project', 'project': 'other', 'key': 'other'}])
    s, data = dispatch('GET', '/api/history-candidate/search', params, {}, env, owner_verified=True)
    assert s == 200 and data['total'] == 1


def test_read_does_not_create_state_and_foreign_db_is_never_modified(setup):
    db, env = setup
    for route in ('projects','unassigned','management/trash'):
        assert call(env, route)[0] == 200
    assert not pathlib.Path(env['EVOLVMEM_HISTORY_MANAGEMENT_DB']).exists()
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    env['EVOLVMEM_HISTORY_MANAGEMENT_DB'] = str(db)
    assert change(env, [{'kind':'project','project':'other','key':'other'}])[0] == 503
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_encoded_whitespace_context_does_not_expose_hidden_relation(setup):
    _, env = setup
    change(env, [{'kind':'project_block','project':'automation_control','key':'blk-dialogue'}])
    result = call(env, 'block/%20blk-dialogue/context')
    assert result[0] == 200 and [x['project'] for x in result[1]['projects']] == ['other']


def test_shared_attachment_visibility_and_restoration(setup):
    db, env = setup
    data=b'restored attachment'; sha=hashlib.sha256(data).hexdigest()
    folder=db.parent/'attachments'; folder.mkdir(); (folder/'test.bin').write_bytes(data)
    c=sqlite3.connect(db)
    c.execute('create table attachments(sha256 text,mime_type text,bytes integer,path text)')
    c.execute('insert into attachments values(?,?,?,?)',(sha,'application/octet-stream',len(data),'test.bin'))
    import json
    c.execute('update messages set attachments=? where block_id=? and ordinal=0',
              (json.dumps([{'sha256':sha}]),'blk-dialogue'));c.commit();c.close()
    from evolvmem.history_candidate_read import open_reader, HistoryCandidateError
    a={'kind':'project','project':'automation_control','key':'automation_control'}
    b={'kind':'project','project':'other','key':'other'}
    change(env,[a])
    with open_reader(env) as reader: assert reader.attachment(sha)['bytes']==len(data)
    change(env,[b])
    with open_reader(env) as reader:
        with pytest.raises(HistoryCandidateError):reader.attachment(sha)
    change(env,[a],'restore')
    with open_reader(env) as reader: assert reader.attachment(sha)['bytes']==len(data)


def test_retired_history_http_is_closed_for_all_identities(protected_web, setup, monkeypatch, tmp_path):
    policy=tmp_path/'policy.json'
    policy.write_text(json.dumps({'console_enabled':False,'legacy_web_enabled':False,'ai_processing_enabled':False}))
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE',str(policy))
    _, env=setup
    for key,value in env.items():monkeypatch.setenv(key,value)
    base=protected_web[0];browser=Browser(base)
    body={'targets':[{'kind':'project','project':'other','key':'other'}],'request_id':str(uuid.uuid4())}
    path='/api/history-candidate/management/delete'
    before=hashlib.sha256(pathlib.Path(env['EVOLVMEM_HISTORY_CANDIDATE_DB']).read_bytes()).hexdigest()
    assert browser.request(path,'POST',body)[0]==410
    viewer=Browser(base);v=viewer.login('viewer')
    assert viewer.request(path,'POST',body,{'X-CSRF-Token':v['csrf_token'],'X-EvolvMem-Action':'history-manage'})[0]==410
    owner=Browser(base);o=owner.login('owner')
    assert owner.request(path,'POST',body,{'X-EvolvMem-Action':'history-manage'})[0]==410
    assert owner.request(path,'POST',body,{'X-CSRF-Token':'wrong','X-EvolvMem-Action':'history-manage'})[0]==410
    assert owner.request(path,'POST',body,{'X-CSRF-Token':o['csrf_token']})[0]==410
    assert owner.request(path,'POST',body,{'X-CSRF-Token':o['csrf_token'],'X-EvolvMem-Action':'history-manage'})[0]==410
    assert hashlib.sha256(pathlib.Path(env['EVOLVMEM_HISTORY_CANDIDATE_DB']).read_bytes()).hexdigest()==before


def test_auth_disabled_does_not_enable_management(test_config, setup, monkeypatch, tmp_path):
    import json, threading
    from http.server import HTTPServer
    from tests.test_web_server import _make_service
    from evolvmem.web_server import make_handler
    policy=tmp_path/'policy.json'
    policy.write_text(json.dumps({'console_enabled':True,'legacy_web_enabled':False,'ai_processing_enabled':False}))
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE',str(policy))
    _,env=setup
    for key,value in env.items():monkeypatch.setenv(key,value)
    service=_make_service(test_config)
    (test_config.data_dir/'web_auth.json').write_text(json.dumps({'enabled':False}))
    server=HTTPServer(('127.0.0.1',0),make_handler(service))
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        browser=Browser('http://127.0.0.1:'+str(server.server_port))
        body={'targets':[{'kind':'project','project':'other','key':'other'}],'request_id':str(uuid.uuid4())}
        assert browser.request('/api/history-candidate/management/delete','POST',body,{'X-EvolvMem-Action':'history-manage'})[0]==410
        assert browser.request('/api/knowledge-console/libraries/jiangli/history/management/delete','POST',body,{'X-EvolvMem-Action':'history-manage'})[0]==401
    finally:server.shutdown();server.server_close();thread.join(5);service.close()


def test_search_locates_late_page_once_after_result_pagination(setup, monkeypatch):
    _,env=setup
    c=sqlite3.connect(env['EVOLVMEM_HISTORY_CANDIDATE_DB'])
    c.execute("delete from messages where block_id='blk-dialogue'")
    for i in range(120):
        c.execute('insert into messages(block_id,ordinal,role,content,attachments) values(?,?,?,?,?)',
                  ('blk-dialogue',i,'user' if i%2==0 else 'assistant','末页独特答案' if i==119 else str(i),'[]'))
    c.execute("update blocks set message_count=120,turn_count=60 where block_id='blk-dialogue'");c.commit();c.close()
    from evolvmem.history_management import ManagedReader
    original=ManagedReader.turns_for_block;calls=[]
    def counted(self,block):calls.append(block);return original(self,block)
    monkeypatch.setattr(ManagedReader,'turns_for_block',counted)
    s,d=dispatch('GET','/api/history-candidate/search',{'scope':'projects','q':'末页独特答案','page_size':1},{},env,owner_verified=True)
    assert s==200 and d['total']==2 and len(d['items'])==1
    assert d['items'][0]['match']['page']==3 and d['items'][0]['match']['ordinal']==119
    assert calls==['blk-dialogue']


def test_unassigned_body_search_excludes_deleted_original(setup):
    _,env=setup
    c=sqlite3.connect(env['EVOLVMEM_HISTORY_CANDIDATE_DB'])
    c.execute("delete from block_projects where block_id='blk-dialogue'")
    c.execute("update messages set content='未归属独特正文' where block_id='blk-dialogue' and ordinal=0")
    c.commit();c.close()
    params={'scope':'unassigned','q':'未归属独特正文'}
    assert dispatch('GET','/api/history-candidate/search',params,{},env,owner_verified=True)[1]['total']==1
    change(env,[{'kind':'block','project':'','key':'blk-dialogue'}])
    assert dispatch('GET','/api/history-candidate/search',params,{},env,owner_verified=True)[1]['total']==0


def test_namespace_mismatch_is_fail_closed(setup):
    _,env=setup
    change(env,[{'kind':'project','project':'other','key':'other'}])
    different=dict(env,EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE='another')
    assert call(different,'projects')[0]==503


def test_oversized_batch_rejected_before_any_state_write(setup):
    _,env=setup
    target={'kind':'project','project':'other','key':'other'}
    assert change(env,[target]*501)[0]==400
    assert not pathlib.Path(env['EVOLVMEM_HISTORY_MANAGEMENT_DB']).exists()

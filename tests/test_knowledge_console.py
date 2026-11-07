"""Tenant boundary tests. Linux integration uses only temporary source databases."""
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest

MODULE = Path(__file__).with_name('knowledge_console.py')
if MODULE.exists():
    spec = importlib.util.spec_from_file_location('console_under_test', MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ConsoleService = module.ConsoleService
else:
    try:
        from evolvmem.knowledge_console import ConsoleService
    except ModuleNotFoundError:
        ConsoleService = None

OWNER = {'open_id': 'test-owner-identity', 'name': 'Owner', 'csrf': 'test-csrf'}
MEMBER = {'open_id': 'test-member-identity', 'name': 'Member'}
OTHER = {'open_id': 'test-other-identity', 'name': 'Other'}


@pytest.fixture
def console(tmp_path):
    assert ConsoleService is not None, 'ConsoleService implementation is missing'
    svc = ConsoleService(tmp_path/'metadata'/'registry.db', 'jiangli', tmp_path/'owner', tmp_path/'lan', {})
    svc.bootstrap(OWNER['open_id'], 'Owner')
    svc.observe_login(MEMBER['open_id'], 'Member')
    assert svc.dispatch('POST', '/users/kane', {'open_id': MEMBER['open_id']}, OWNER)[0] == 200
    svc.observe_login(OTHER['open_id'], 'Other')
    assert svc.dispatch('POST', '/users', {'id': 'other', 'display_name': 'Other', 'open_id': OTHER['open_id']}, OWNER)[0] == 201
    return svc


def call(svc, path, session=OWNER, body=None, method='GET'):
    status, mime, data = svc.dispatch(method, path, body or {}, session)
    assert mime.startswith('application/json')
    return status, data


def grant(svc, viewer, owner, history=False, experience=False, revision=0):
    return call(svc, '/grants', body={'viewer_id': viewer, 'owner_id': owner,
                'history_read': history, 'experience_read': experience, 'revision': revision}, method='POST')


def test_missing_registry_is_not_created_by_reads(tmp_path):
    assert ConsoleService is not None, 'ConsoleService implementation is missing'
    path = tmp_path/'not-created'/'registry.db'
    svc = ConsoleService(path, 'jiangli', tmp_path/'owner', tmp_path/'lan', {})
    assert call(svc, '/me')[0] == 503
    assert not path.exists() and not path.parent.exists()
    assert svc.user_enabled('jiangli') is False


def test_exact_identity_mapping_and_pending_have_no_access(console):
    console.observe_login('unknown', 'Owner')
    assert call(console, '/me', {'open_id': 'unknown', 'user_id': 'jiangli'})[0] == 403
    assert call(console, '/me', {'open_id': OWNER['open_id']+' '})[0] == 403
    assert call(console, '/me', None)[0] == 401
    _, me = call(console, '/me', MEMBER)
    assert me['current_user']['id'] == 'kane'
    assert me['current_user']['is_admin'] is False
    assert [r['id'] for r in me['libraries']] == ['kane']
    assert 'csrf_token' in me


def test_admin_user_list_pending_and_no_promotion(console):
    console.observe_login('pending-identity', 'Pending')
    status, body = call(console, '/users')
    assert status == 200
    assert {u['id'] for u in body['users']} == {'jiangli', 'kane', 'other'}
    assert body['pending'][0]['open_id'] == 'pending-identity'
    assert next(u for u in body['users'] if u['id']=='other')['lan_enabled'] is False
    assert call(console, '/users', MEMBER)[0] == 403
    assert call(console, '/users', MEMBER, {'id':'bad'}, 'POST')[0] == 403
    assert call(console, '/users', OWNER, {'id':'bad','is_admin':True}, 'POST')[0] == 400


@pytest.mark.parametrize('body', [{'enabled': False}, {'open_id': MEMBER['open_id']}, {'is_admin': False}])
def test_configured_owner_cannot_be_disabled_rebound_or_demoted(console, body):
    assert call(console, '/users/jiangli', body=body, method='POST')[0] == 400
    assert console.user_enabled('jiangli') is True


def test_only_observed_trusted_logins_can_be_bound_and_duplicates_fail(console):
    assert call(console, '/users/other', body={'open_id': 'unobserved'}, method='POST')[0] == 400
    assert call(console, '/users/other', body={'open_id': MEMBER['open_id']}, method='POST')[0] == 409
    assert call(console, '/users', body={'id':'../evil','display_name':'Bad'}, method='POST')[0] == 400
    assert call(console, '/users', body={'id':'unsafe','data_dir':'/synthetic/owner/private-data'}, method='POST')[0] == 400


def test_disabled_user_is_denied_immediately_even_with_valid_old_session(console):
    assert call(console, '/users/kane', body={'enabled':False}, method='POST')[0] == 200
    assert call(console, '/me', MEMBER)[0] == 403
    assert console.user_enabled('kane') is False
    assert console.user_enabled('unknown') is False
    assert call(console, '/users/kane', body={'enabled':True}, method='POST')[0] == 200
    assert call(console, '/me', MEMBER)[0] == 200


def test_directional_kind_specific_grants_and_immediate_revocation(console):
    assert call(console, '/libraries/other/history/projects', MEMBER)[0] == 403
    assert grant(console, 'kane', 'other', history=True)[0] == 200
    _, body = call(console, '/libraries', MEMBER)
    target = next(r for r in body['libraries'] if r['id']=='other')
    assert target['can_read_history'] is True and target['can_read_experience'] is False
    assert 'experience_count' not in target
    assert call(console, '/libraries/other/experiences', MEMBER)[0] == 403
    assert call(console, '/libraries/kane/history/projects', OTHER)[0] == 403
    assert grant(console, 'kane', 'other', revision=0)[0] == 409
    assert grant(console, 'kane', 'other', revision=1)[0] == 200
    assert call(console, '/libraries/other/history/projects', MEMBER)[0] == 403
    assert call(console, '/grants', MEMBER)[0] == 403
    assert call(console, '/grants', MEMBER, {}, 'POST')[0] == 403
    assert grant(console, 'kane', 'kane', history=False)[0] == 400


def test_mutations_and_read_denials_have_metadata_audit(console):
    grant(console, 'kane', 'other', history=True)
    call(console, '/libraries/jiangli/experiences', MEMBER)
    with sqlite3.connect(console.registry_path) as con:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute('SELECT * FROM console_audit')]
    saved = next(r for r in rows if r['action']=='grant.update')
    assert saved['actor_id']=='jiangli' and saved['target_id']=='other'
    assert json.loads(saved['before_json'])['history_read'] is False
    assert json.loads(saved['after_json'])['history_read'] is True
    assert any(r['actor_id']=='kane' and r['target_id']=='jiangli' and r['outcome']=='denied' for r in rows)


def test_audit_unavailable_denial_remains_bounded_and_fails_closed(console,monkeypatch):
    def unavailable(*args,**kwargs):
        raise sqlite3.OperationalError('synthetic metadata write failure')
    monkeypatch.setattr(console,'_audit',unavailable)
    status,body=call(console,'/libraries/jiangli/experiences',MEMBER)
    assert status==503 and body=={'ok':False,'error':'console_unavailable'}


def test_explicit_admin_inspection_is_identifiable_in_audit(console):
    assert call(console,'/libraries/kane/experiences',body={'inspect':'1'})[0]==200
    with sqlite3.connect(console.registry_path) as con:
        row=con.execute("SELECT action,actor_id,target_id FROM console_audit ORDER BY id DESC LIMIT 1").fetchone()
    assert row==('experiences.inspect','jiangli','kane')


def test_admin_can_inspect_all_but_cannot_manage_another_history(console):
    _, normal = call(console, '/libraries')
    assert [r['id'] for r in normal['libraries']] == ['jiangli']
    _, body = call(console, '/libraries', body={'inspect':'1'})
    assert {r['id'] for r in body['libraries']} == {'jiangli','kane','other'}
    assert all(r['can_read_history'] and r['can_read_experience'] for r in body['libraries'])
    assert not next(r for r in body['libraries'] if r['id']=='kane')['can_manage_history']
    assert call(console, '/libraries/kane/history/management/delete', body={'targets':[]}, method='POST')[0] == 403


def test_unconnected_libraries_are_explicit_empty_and_no_owner_fallback(console):
    _, body = call(console, '/libraries/other/experiences', body={'inspect':'1'})
    assert body['rows']==[] and body['connected'] is False
    _, body = call(console, '/libraries/other/history/projects', body={'inspect':'1'})
    assert body['projects']==[] and body['connected'] is False
    assert call(console, '/libraries/other/experiences/1',body={'inspect':'1'})[0] == 404


def test_inspection_is_explicit_admin_get_only_and_not_a_reciprocal_grant(console):
    assert call(console,'/libraries',MEMBER,{'inspect':'1'})[0]==403
    assert call(console,'/me',MEMBER,{'inspect':'1'})[0]==403
    assert call(console,'/users',OWNER,{'inspect':'1','id':'test'},'POST')[0]==403
    assert call(console,'/libraries/kane/experiences')[0]==403
    assert call(console,'/libraries/kane/experiences',body={'inspect':'1'})[0]==200
    assert grant(console,'jiangli','kane',experience=True)[0]==200
    assert call(console,'/libraries/kane/experiences')[0]==200
    assert call(console,'/libraries/jiangli/experiences',MEMBER)[0]==403
    assert call(console,'/libraries/kane/history/management/trash',body={'inspect':'1'})[0]==403
    assert call(console,'/libraries/kane/history/management/delete',body={'inspect':'1'},method='POST')[0]==403


def test_sources_cannot_be_attached_to_another_account(console):
    with pytest.raises(ValueError,match='another user'):
        console.configure_library('other',data_dir=console.owner_data_dir)


def test_existing_uninitialized_source_does_not_fall_back(console):
    pytest.importorskip('evolvmem')
    directory=console.lan_data_dir/'users'/'kane'
    directory.mkdir(parents=True)
    with sqlite3.connect(directory/'memory.db'):
        pass
    status,body=call(console,'/libraries/kane/experiences',MEMBER)
    assert status==503 and body['error']=='console_unavailable'


@pytest.mark.parametrize('field', ['namespace','user_id','actor_id','data_dir','history_environ'])
def test_request_cannot_supply_actor_or_namespace(console, field):
    assert call(console, '/libraries/jiangli/experiences', MEMBER, {field:'jiangli'})[0] in (400,403)


def test_bootstrap_is_idempotent_and_does_not_rebind_existing_owner(console):
    console.bootstrap(OWNER['open_id'], 'Owner')
    with pytest.raises(ValueError):
        console.bootstrap('different-owner-identity')


def _source_db(path, label):
    from evolvmem.config import Config
    from evolvmem.context_service import ContextService
    from evolvmem.context_models import ContextMode
    from evolvmem.memory_store import MemoryStore
    config = Config(data_dir=path, apply_environment=False)
    config.ensure_dirs()
    with MemoryStore(config):
        pass
    core = ContextService(config)
    core.initialize(mode=ContextMode.SHADOW, adapter='codex')
    item = core.experiences().record({'project':'demo','problem':label,'conditions':{},'steps':['Inspect temporary source'], 'rationale':'','result':'','applicability':[],'exclusions':[],'transferable':False})
    core.close()
    return item['id']


def test_real_experience_readonly_owner_scoping_colliding_ids(console):
    pytest.importorskip('evolvmem')
    j = _source_db(console.owner_data_dir, 'Owner private experience')
    k = _source_db(console.lan_data_dir/'users'/'kane', 'Member private experience')
    assert j == k
    before = hashlib.sha256((console.owner_data_dir/'memory.db').read_bytes()).hexdigest()
    _, a = call(console, f'/libraries/jiangli/experiences/{j}')
    assert a['problem']=='Owner private experience' and a['validation_level']=='unverified'
    _, b = call(console, f'/libraries/kane/experiences/{k}', MEMBER)
    assert b['problem']=='Member private experience'
    assert call(console, f'/libraries/jiangli/experiences/{j}', MEMBER)[0] == 403
    assert grant(console,'kane','jiangli',experience=True)[0] == 200
    assert call(console, f'/libraries/jiangli/experiences/{j}', MEMBER)[1]['problem']=='Owner private experience'
    assert call(console, '/libraries/jiangli/experiences', MEMBER, {'q':'Member private'})[1]['total']==0
    assert call(console, f'/libraries/jiangli/experiences/{j}', MEMBER, {}, 'POST')[0] == 405
    assert hashlib.sha256((console.owner_data_dir/'memory.db').read_bytes()).hexdigest()==before


def test_real_history_delegation_attachment_and_self_only_tombstones(console, tmp_path):
    pytest.importorskip('evolvmem')
    from tests.test_history_candidate_read import build_fixture
    candidate = tmp_path/'candidate'/'candidate.db'
    candidate.parent.mkdir()
    build_fixture(candidate)
    raw = b'fixture image bytes'
    sha = hashlib.sha256(raw).hexdigest()
    (candidate.parent/'attachments').mkdir()
    (candidate.parent/'attachments'/'picture.png').write_bytes(raw)
    with sqlite3.connect(candidate) as con:
        con.execute('CREATE TABLE attachments(sha256 TEXT,mime_type TEXT,bytes INTEGER,path TEXT)')
        con.execute('INSERT INTO attachments VALUES(?,?,?,?)',(sha,'image/png',len(raw),'picture.png'))
        con.execute('UPDATE messages SET attachments=? WHERE ordinal=0',(json.dumps([{'sha256':sha}]),))
    env={'EVOLVMEM_HISTORY_CANDIDATE_DB':str(candidate),'EVOLVMEM_HISTORY_CANDIDATE_TOKEN_FILE':str(tmp_path/'unused-server-token-path'),'EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE':'personal'}
    console.configure_library('jiangli', history_environ=env)
    before=hashlib.sha256(candidate.read_bytes()).hexdigest()
    assert call(console,'/libraries/jiangli/history/projects')[0]==200
    assert call(console,'/libraries/jiangli/history/block/blk-dialogue',MEMBER)[0]==403
    assert console.dispatch('GET',f'/libraries/jiangli/history/attachment/{sha}',{},MEMBER)[0]==403
    assert grant(console,'kane','jiangli',history=True)[0]==200
    status,mime,data=console.dispatch('GET',f'/libraries/jiangli/history/attachment/{sha}',{},MEMBER)
    assert status==200 and mime=='image/png' and data==raw
    assert call(console,'/libraries/kane/history/block/blk-dialogue',MEMBER)[0]==404
    assert console.dispatch('GET',f'/libraries/kane/history/attachment/{sha}',{},MEMBER)[0]==404
    body={'request_id':'fixture-delete-1','targets':[{'kind':'project_block','project':'automation_control','key':'blk-dialogue'}]}
    assert call(console,'/libraries/jiangli/history/management/delete',MEMBER,body,'POST')[0]==403
    assert call(console,'/libraries/jiangli/history/management/delete',OWNER,body,'POST')[0]==200
    assert call(console,'/libraries/jiangli/history/block/blk-dialogue',MEMBER)[0]==410
    assert console.dispatch('GET',f'/libraries/jiangli/history/attachment/{sha}',{},MEMBER)[0]==404
    assert hashlib.sha256(candidate.read_bytes()).hexdigest()==before
    assert call(console,'/libraries/jiangli/history/management/restore',OWNER,{**body,'request_id':'fixture-restore-1'},'POST')[0]==200
    assert call(console,'/libraries/jiangli/history/block/blk-dialogue',MEMBER)[0]==200
    assert grant(console,'kane','jiangli',revision=1)[0]==200
    assert console.dispatch('GET',f'/libraries/jiangli/history/attachment/{sha}',{},MEMBER)[0]==403
    assert call(console,'/libraries/jiangli/history/search',MEMBER,{'q':'内容','scope':'projects'})[0]==403


def test_real_history_two_sources_with_colliding_block_ids(console,tmp_path):
    pytest.importorskip('evolvmem')
    from tests.test_history_candidate_read import build_fixture
    for user in ('jiangli','kane'):
        directory=tmp_path/('history-'+user)
        directory.mkdir()
        db=directory/'candidate.db'
        build_fixture(db)
        with sqlite3.connect(db) as con:
            con.execute('UPDATE messages SET content=?',(user+' history marker',))
            con.execute('CREATE TABLE attachments(sha256 TEXT,mime_type TEXT,bytes INTEGER,path TEXT)')
        console.configure_library(user,history_environ={
            'EVOLVMEM_HISTORY_CANDIDATE_DB':str(db),
            'EVOLVMEM_HISTORY_CANDIDATE_TOKEN_FILE':str(directory/'unused-token-path')})
    _, owner=call(console,'/libraries/jiangli/history/block/blk-dialogue')
    _, member=call(console,'/libraries/kane/history/block/blk-dialogue',MEMBER)
    assert 'jiangli history marker' in json.dumps(owner) and 'kane history marker' not in json.dumps(owner)
    assert 'kane history marker' in json.dumps(member) and 'jiangli history marker' not in json.dumps(member)
    _, search=call(console,'/libraries/kane/history/search',MEMBER,{'q':'jiangli','scope':'projects'})
    assert search['total']==0
    assert grant(console,'kane','jiangli',history=True)[0]==200
    assert call(console,'/libraries/jiangli/history/management/trash',MEMBER)[0]==403
    assert call(console,'/libraries/kane/history/management/trash',body={'inspect':'1'})[0]==403

"""Manual notebook behavior and tenant boundaries; all databases are temporary."""
import hashlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_knowledge_console import console, call, grant, OWNER, MEMBER


BASE = '/libraries/jiangli/experiences'
CATEGORIES = ('前端经验', '业务经验', '系统环境经验')


@pytest.fixture
def connected(console):
    for directory in (console.owner_data_dir, console.lan_data_dir/'users'/'kane'):
        directory.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(directory/'memory.db') as conn:
            conn.execute('CREATE TABLE untouched(value TEXT)')
            conn.execute("INSERT INTO untouched VALUES('preserve original source')")
    return console


def new(svc, request_id='create-test-0001', **changes):
    body=dict(action='create', request_id=request_id, category='前端经验',
              problem='Problem needle', solution='Actual solution\nSecond step',
              reusable_experience='When the same conditions hold')
    body.update(changes)
    return call(svc, BASE, body=body, method='POST')


def mutation(svc, note, action, request_id, **changes):
    return call(svc, BASE, body=dict(action=action, id=note['id'], revision=note['revision'],
                                   request_id=request_id, **changes), method='POST')


def test_manual_read_empty_source_never_initializes_schema(connected):
    path=connected.owner_data_dir/'memory.db'
    before=path.read_bytes()
    for _ in range(2):
        status, data=call(connected, BASE, body={'origin':'manual'})
        assert status==200 and data['rows']==[] and data['total']==0
        assert data['summary']=={'categories':dict.fromkeys(CATEGORIES,0), 'total':0,'deleted_total':0}
    assert path.read_bytes()==before
    with sqlite3.connect(path) as conn:
        assert conn.execute('SELECT value FROM untouched').fetchone()[0]=='preserve original source'
        assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'manual_experience_%'").fetchone()[0]==0


def test_manual_unconnected_read_and_write_do_not_create_sources(console):
    status, data=call(console, BASE, body={'origin':'manual'})
    assert status==200 and data['connected'] is False and data['rows']==[]
    assert new(console)[0]==404
    assert not (console.owner_data_dir/'memory.db').exists()


def test_create_read_update_delete_restore_with_compare_and_swap_and_no_core_changes(connected):
    status, created=new(connected)
    assert status==201 and not created['replayed']
    note=created['experience']
    assert note['id'].startswith('manual-') and note['revision']==1 and note['origin']=='manual'
    assert 'title' not in note and note['solution']=='Actual solution\nSecond step'
    assert call(connected, BASE+'/'+note['id'], body={'origin':'manual'})[1]['experience']==note
    updated=dict(category='业务经验',problem='Changed problem',solution='Changed solution',reusable_experience='Changed rule')
    status, result=mutation(connected,note,'update','update-test-0001',**updated)
    assert status==200 and result['experience']['revision']==2
    assert all(result['experience'][k]==v for k,v in updated.items())
    assert mutation(connected,note,'update','stale-update-0001',**updated)[0]==409
    note=result['experience']
    status, result=mutation(connected,note,'delete','delete-test-0001')
    assert status==200 and result['experience']['deleted_at']
    assert call(connected, BASE,body={'origin':'manual'})[1]['total']==0
    assert call(connected, BASE+'/'+note['id'],body={'origin':'manual'})[0]==404
    assert call(connected, BASE,body={'origin':'manual','deleted':'1'})[1]['total']==1
    deleted=result['experience']
    assert mutation(connected,deleted,'update','edit-deleted-001',**updated)[0]==409
    status, restored=mutation(connected,deleted,'restore','restore-test-0001')
    assert status==200 and restored['experience']['revision']==4 and restored['experience']['deleted_at'] is None
    assert call(connected, BASE,body={'origin':'manual'})[1]['total']==1
    with sqlite3.connect(connected.owner_data_dir/'memory.db') as conn:
        assert conn.execute('SELECT value FROM untouched').fetchone()[0]=='preserve original source'
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='context_items'").fetchone()


def test_request_replay_is_payload_bound_and_does_not_duplicate_or_repeat_deletion(connected):
    first=new(connected)[1]
    replay=new(connected)[1]
    assert replay['replayed'] and replay['experience']==first['experience']
    assert new(connected,problem='Different payload same request')[0]==409
    note=first['experience']
    first_delete=mutation(connected,note,'delete','retry-delete-0001')[1]
    replay_delete=mutation(connected,note,'delete','retry-delete-0001')[1]
    assert replay_delete['replayed'] and replay_delete['experience']==first_delete['experience']
    assert call(connected,BASE,body={'origin':'manual','deleted':1})[1]['total']==1


def test_concurrent_retry_of_one_request_is_exactly_once(connected):
    with ThreadPoolExecutor(max_workers=2) as workers:
        results=list(workers.map(lambda _:new(connected,request_id='concurrent-create-001'),range(2)))
    assert [status for status,_ in results]==[201,201]
    assert sorted(data['replayed'] for _,data in results)==[False,True]
    assert results[0][1]['experience']['id']==results[1][1]['experience']['id']
    assert call(connected,BASE,body={'origin':'manual'})[1]['total']==1
    with sqlite3.connect(connected.owner_data_dir/'memory.db') as conn:
        assert conn.execute('SELECT count(*) FROM manual_experience_events').fetchone()[0]==1


def test_source_commit_followed_by_metadata_audit_failure_can_be_safely_retried(connected,monkeypatch):
    original=connected._access_audit
    def unavailable(*args,**kwargs):
        raise sqlite3.OperationalError('synthetic metadata audit unavailable')
    monkeypatch.setattr(connected,'_access_audit',unavailable)
    assert new(connected,request_id='audit-response-lost-001')[0]==503
    monkeypatch.setattr(connected,'_access_audit',original)
    status,replayed=new(connected,request_id='audit-response-lost-001')
    assert status==201 and replayed['replayed'] is True
    assert call(connected,BASE,body={'origin':'manual'})[1]['total']==1


def test_search_category_pagination_and_summary_do_not_count_legacy_material(connected):
    notes=[]
    for i,category in enumerate(CATEGORIES):
        notes.append(new(connected,request_id=f'create-summary-{i}',category=category,problem=f'Needle {i}')[1]['experience'])
    mutation(connected,notes[-1],'delete','summary-delete-0001')
    status,data=call(connected,BASE,body={'origin':'manual','q':'needle','category':'前端经验','page_size':'1'})
    assert status==200 and data['total']==1 and len(data['rows'])==1
    assert data['summary']=={'categories':{'前端经验':1,'业务经验':1,'系统环境经验':0},'total':2,'deleted_total':1}
    assert call(connected,BASE,body={'origin':'manual','q':'Second step'})[1]['total']==2
    assert call(connected,BASE,body={'origin':'manual','q':'%'})[1]['total']==0
    assert call(connected,BASE,body={'origin':'manual','q':'_'})[1]['total']==0
    assert call(connected,BASE,body={'origin':'manual','page':'2','page_size':'1'})[1]['page']==2
    _,libraries=call(connected,'/libraries')
    own=next(x for x in libraries['libraries'] if x['id']=='jiangli')
    assert own['experience_count']==2 and own['can_manage_experience'] is True


def test_cross_user_read_grant_does_not_allow_any_writes_and_revocation_is_immediate(connected):
    note=new(connected)[1]['experience']
    for path in (BASE,BASE+'/'+note['id']):
        assert call(connected,path,MEMBER,{'origin':'manual'})[0]==403
    assert grant(connected,'kane','jiangli',experience=True)[0]==200
    status,active=call(connected,BASE,MEMBER,{'origin':'manual'})
    assert status==200 and active['total']==1 and 'deleted_total' not in active['summary']
    body=dict(action='delete',request_id='cross-delete-0001',id=note['id'],revision=1)
    assert call(connected,BASE,MEMBER,body,'POST')[0]==403
    assert call(connected,BASE,OWNER,{**body,'inspect':'1'},'POST')[0]==403
    _, libraries=call(connected,'/libraries',MEMBER)
    assert not next(x for x in libraries['libraries'] if x['id']=='jiangli')['can_manage_experience']
    assert grant(connected,'kane','jiangli',revision=1)[0]==200
    assert call(connected,BASE,MEMBER,{'origin':'manual'})[0]==403
    assert call(connected,BASE+'/'+note['id'],MEMBER,{'origin':'manual'})[0]==403


def test_admin_inspection_is_read_only_and_sources_do_not_fall_back(connected):
    new(connected)
    base='/libraries/kane/experiences'
    assert call(connected,base,body={'origin':'manual','inspect':'1'})[1]['total']==0
    assert call(connected,base,body={'origin':'manual','inspect':'1','action':'create'},method='POST')[0]==403
    assert call(connected,base,body={'action':'create','request_id':'admin-cross-0001'},method='POST')[0]==403


@pytest.mark.parametrize('deleted',[1,'1','01'])
@pytest.mark.parametrize('detail',[False,True])
@pytest.mark.parametrize('include_origin',[False,True])
def test_cross_user_manual_trash_is_owner_only_even_with_read_grant(connected,deleted,detail,include_origin):
    note=new(connected)[1]['experience']
    mutation(connected,note,'delete','owner-trash-test-0001')
    assert grant(connected,'kane','jiangli',experience=True)[0]==200
    # The collection selects manual notes with origin. A manual ID selects them
    # intrinsically, and must not become a bypass when origin is omitted.
    query={'deleted':deleted}
    if include_origin or not detail:
        query['origin']='manual'
    path=BASE+('/'+note['id'] if detail else '')
    status,data=call(connected,path,MEMBER,query)
    assert status==403 and data['error']=='experience_trash_forbidden'
    assert 'rows' not in data and 'experience' not in data and 'summary' not in data
    own=call(connected,path,OWNER,query)
    assert own[0]==200


def test_admin_inspection_cannot_read_other_manual_trash_but_self_inspection_can(connected):
    base='/libraries/kane/experiences'
    create=dict(action='create',request_id='trash-member-create-001',category='业务经验',problem='Private trash',solution='fix',reusable_experience='rule')
    member=call(connected,base,MEMBER,create,'POST')[1]['experience']
    deleted=call(connected,base,MEMBER,dict(action='delete',id=member['id'],revision=1,request_id='trash-member-delete-001'),'POST')[1]['experience']
    for path,query in [(base,{'origin':'manual','deleted':'1','inspect':'1'}),
                       (base+'/'+member['id'],{'deleted':'1','inspect':'1'})]:
        status,data=call(connected,path,OWNER,query)
        assert status==403 and data['error']=='experience_trash_forbidden'
    own=new(connected,request_id='trash-owner-create-001')[1]['experience']
    mutation(connected,own,'delete','trash-owner-delete-001')
    assert call(connected,BASE,OWNER,{'origin':'manual','deleted':'1','inspect':'1'})[1]['total']==1
    assert call(connected,BASE+'/'+own['id'],OWNER,{'deleted':'1','inspect':'1'})[1]['experience']['id']==own['id']
    assert call(connected,base,MEMBER,{'origin':'manual','deleted':'1'})[1]['rows'][0]['id']==deleted['id']
    active=call(connected,base,OWNER,{'origin':'manual','inspect':'1'})[1]
    assert active['total']==0 and 'deleted_total' not in active['summary']


def test_manual_ids_colliding_across_libraries_remain_owner_bound(connected):
    own=new(connected)[1]['experience']
    member_base='/libraries/kane/experiences'
    create=dict(action='create',request_id='member-collide-001',category='系统环境经验',problem='Member only',solution='fix',reusable_experience='rule')
    member=call(connected,member_base,MEMBER,create,'POST')[1]['experience']
    with sqlite3.connect(connected.lan_data_dir/'users'/'kane'/'memory.db') as conn:
        conn.execute('UPDATE manual_experience_notes SET id=? WHERE id=?',(own['id'],member['id']))
    assert call(connected,BASE+'/'+own['id'],body={'origin':'manual'})[1]['experience']['problem']=='Problem needle'
    assert call(connected,member_base+'/'+own['id'],MEMBER,{'origin':'manual'})[1]['experience']['problem']=='Member only'
    assert call(connected,member_base+'/'+own['id'],body={'origin':'manual','inspect':'1'})[1]['experience']['problem']=='Member only'


@pytest.mark.parametrize('changes',[
    {'category':'后端开发'}, {'category':''}, {'problem':' '}, {'solution':''},
    {'reusable_experience':''}, {'problem':'a'*1001}, {'solution':'a'*8001},
    {'reusable_experience':'a'*8001}, {'problem':'null\x00byte'},
    {'request_id':''},{'request_id':'../unsafe/path'}, {'title':'Forbidden title'}, {'origin':'history'}
])
def test_invalid_manual_input_never_changes_source(connected,changes):
    before=hashlib.sha256((connected.owner_data_dir/'memory.db').read_bytes()).hexdigest()
    assert new(connected,**changes)[0]==400
    assert hashlib.sha256((connected.owner_data_dir/'memory.db').read_bytes()).hexdigest()==before


@pytest.mark.parametrize('query',[{'deleted':'yes'},{'category':'旧分类'},{'page':'0'},{'page_size':'201'},{'q':'a'*201}])
def test_invalid_manual_read_parameters_are_bounded(connected,query):
    assert call(connected,BASE,body={'origin':'manual',**query})[0]==400


def test_disabled_account_cannot_read_or_replay_old_write(connected):
    member_base='/libraries/kane/experiences'
    create=dict(action='create',request_id='member-write-0001',category='系统环境经验',problem='member',solution='fix',reusable_experience='rule')
    assert call(connected,member_base,MEMBER,create,'POST')[0]==201
    assert call(connected,'/users/kane',body={'enabled':False},method='POST')[0]==200
    assert call(connected,member_base,MEMBER,create,'POST')[0]==403
    assert call(connected,member_base,MEMBER,{'origin':'manual'})[0]==403

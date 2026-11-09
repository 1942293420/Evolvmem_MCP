"""Manual writes use ordinary authenticated HTTP and existing CSRF enforcement."""
from tests.test_console_web_integration import fixture, request, grant, PREFIX


def test_manual_http_create_requires_csrf_and_retry_does_not_duplicate(fixture):
    path=PREFIX+'/libraries/jiangli/experiences'
    body=dict(action='create',request_id='http-manual-create-001',category='前端经验',
              problem='Synthetic manual note',solution='Synthetic fix',reusable_experience='Synthetic condition')
    assert request(fixture,path,method='POST',body=body)[0]==403
    assert request(fixture,path,method='POST',body=body,csrf='wrong-csrf')[0]==403
    assert request(fixture,path+'?origin=manual')[1]['total']==0
    status,data=request(fixture,path,method='POST',body=body,csrf=True)
    assert status==201 and data['experience']['revision']==1
    assert request(fixture,path,method='POST',body=body,csrf=True)[1]['replayed'] is True
    assert request(fixture,path+'?origin=manual')[1]['total']==1
    assert request(fixture,PREFIX+'/libraries')[1]['libraries'][0]['experience_count']==1
    assert fixture.reads==[]


def test_manual_http_grants_are_read_only_and_inspection_cannot_mutate(fixture):
    path=PREFIX+'/libraries/jiangli/experiences'
    body=dict(action='create',request_id='http-manual-grant-001',category='业务经验',
              problem='Owner-only note',solution='Fix',reusable_experience='Rule')
    note=request(fixture,path,method='POST',body=body,csrf=True)[1]['experience']
    assert grant(fixture,'kane','jiangli',experience=True)[0]==200
    assert request(fixture,path+'?origin=manual',actor='member')[1]['total']==1
    delete=dict(action='delete',request_id='http-cross-delete-001',id=note['id'],revision=note['revision'])
    assert request(fixture,path,actor='member',method='POST',body=delete,csrf=True)[0]==403
    assert request(fixture,path+'?inspect=1',method='POST',body=delete,csrf=True)[0]==403
    assert grant(fixture,'kane','jiangli',revision=1)[0]==200
    assert request(fixture,path+'?origin=manual',actor='member')[0]==403


def test_manual_http_recycle_bin_is_owner_only_for_list_detail_and_admin_inspection(fixture):
    path=PREFIX+'/libraries/jiangli/experiences'
    create=dict(action='create',request_id='http-trash-owner-create',category='系统环境经验',problem='Owner deleted content',solution='fix',reusable_experience='rule')
    note=request(fixture,path,method='POST',body=create,csrf=True)[1]['experience']
    delete=dict(action='delete',id=note['id'],revision=1,request_id='http-trash-owner-delete')
    assert request(fixture,path,method='POST',body=delete,csrf=True)[0]==200
    assert grant(fixture,'kane','jiangli',experience=True)[0]==200
    for route in (path+'?origin=manual&deleted=1',path+'/'+note['id']+'?deleted=1',path+'/'+note['id']+'?origin=manual&deleted=01'):
        status,data=request(fixture,route,actor='member')
        assert status==403 and data['error']=='experience_trash_forbidden'
    assert request(fixture,path+'?origin=manual&deleted=1')[1]['total']==1
    assert request(fixture,path+'?origin=manual&deleted=1&inspect=1')[1]['total']==1
    active=request(fixture,path+'?origin=manual',actor='member')[1]
    assert active['total']==0 and 'deleted_total' not in active['summary']
    member_path=PREFIX+'/libraries/kane/experiences'
    created=request(fixture,member_path,actor='member',method='POST',body={**create,'request_id':'http-trash-member-create'},csrf=True)[1]['experience']
    assert request(fixture,member_path,actor='member',method='POST',body={**delete,'id':created['id'],'request_id':'http-trash-member-delete'},csrf=True)[0]==200
    for route in (member_path+'?origin=manual&deleted=1&inspect=1',member_path+'/'+created['id']+'?deleted=1&inspect=1'):
        assert request(fixture,route)[0]==403


def test_manual_http_search_uses_hybrid_for_authorized_library(tmp_path, monkeypatch):
    from urllib.parse import urlencode

    from evolvmem import manual_experience_search
    from tests.test_console_web_integration import fixture_module

    class Model:
        documents = 0

        def initialize(self):
            pass

        def encode(self, text, kind):
            if kind == 'document':
                self.documents += 1
            return [1.0, 0.0]

    model = Model()
    search = manual_experience_search.ManualExperienceSearch(model)
    monkeypatch.setattr(manual_experience_search, 'configured_search', lambda config: search)
    test_server = fixture_module.Fixture(tmp_path/'hybrid-http').start()
    try:
        path = PREFIX+'/libraries/jiangli/experiences'
        body = dict(action='create', request_id='http-hybrid-create-001', category='前端经验',
                    problem='窄屏筛选栏溢出', solution='让控件换行', reusable_experience='保持操作可见')
        note = request(test_server, path, method='POST', body=body, csrf=True)[1]['experience']
        url = path+'?'+urlencode({'origin':'manual', 'q':'手机上按钮挤出屏幕', 'category':'前端经验'})
        assert request(test_server, url, actor=None)[0] == 401
        assert request(test_server, url, actor='member')[0] == 403
        assert model.documents == 0
        status, result = request(test_server, url)
        assert status == 200
        assert result['search_mode'] == 'hybrid'
        assert [row['id'] for row in result['rows']] == [note['id']]
        assert model.documents == 1
    finally:
        test_server.close()

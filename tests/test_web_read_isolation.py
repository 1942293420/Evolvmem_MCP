"""Every old GET uses an independent, non-mutating request snapshot."""
import concurrent.futures
import json
import threading
import urllib.request

from tests.test_knowledge_management import service


def test_old_gets_remain_available_during_slow_read_and_write(service, monkeypatch):
    from evolvmem import web_server
    original = web_server.api_stats
    entered, release = threading.Event(), threading.Event()
    def slow(*args, **kwargs):
        entered.set()
        assert release.wait(4)
        return original(*args, **kwargs)
    monkeypatch.setattr(web_server, 'api_stats', slow)
    server = web_server.make_http_server(service, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    def request(path, body=None):
        req = urllib.request.Request(base+path, data=json.dumps(body).encode() if body else None,
                                     headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req, timeout=2) as response: return json.load(response)
    try:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            pending = pool.submit(request, '/api/stats')
            assert entered.wait(1)
            try:
                assert 'projects' in request('/api/projects')
                assert request('/api/knowledge/projects', {'project':'parallel-save'})['ok']
                assert request('/api/knowledge/items')['total']==0
            finally: release.set()
            assert pending.result()['total_active']==0
    finally:
        release.set(); server.shutdown(); server.server_close(); thread.join()


def test_all_old_read_families_use_query_only_connection_and_leave_files_unchanged(service, monkeypatch):
    from evolvmem import web_server, knowledge_api
    from evolvmem.session_archive import SessionArchiver
    item=service.knowledge().create({'title':'退款规则','body':'退款必须核对原始订单金额。','project':'eva','action':'publish'})
    archive=SessionArchiver(service.config,service.store).archive_session('eva','kimi','read-only',json.dumps({'messages':[{'role':'user','content':'保留退款原始编号。'}]}))
    unassigned=SessionArchiver(service.config,service.store).archive_session('','kimi','unassigned-read',json.dumps({'messages':[{'role':'user','content':'保留资料原始编号。'}]}))
    original=knowledge_api.dispatch
    def checked(reader,*args,**kwargs):
        assert reader.store._connection().execute('PRAGMA query_only').fetchone()[0]==1
        return original(reader,*args,**kwargs)
    monkeypatch.setattr(knowledge_api,'dispatch',checked)
    server=web_server.make_http_server(service,port=0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def files():
        return {str(p.relative_to(service.config.data_dir)):p.read_bytes() for p in service.config.data_dir.rglob('*')
                if p.is_file() and not any(x in p.name for x in ('.db','.sqlite'))}
    before=files()
    routes=['projects','items',f'items/{item["id"]}',f'items/{item["id"]}/context','rules',
            'project-memory?project=eva',f'conversations/{archive.id}?project=eva',
            'history?project=eva','qa?project=eva','recall?project=eva&kind=both',
            'learning','learning/skill','learning/versions','skills','skills/ownership',
            'cleaning',f'cleaning/detail?key=archive:{unassigned.id}',
            'history/organization','facts','directory?project=eva','organization/metrics']
    try:
        for route in routes:
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/api/knowledge/'+route,timeout=4) as response:
                    assert response.status==200,route
                    json.load(response)
            except urllib.error.HTTPError as error:
                raise AssertionError((route, error.read())) from error
        assert files()==before,'GET must not export or rewrite files'
    finally:server.shutdown();server.server_close();thread.join()


import pytest


@pytest.mark.parametrize('route', ['extraction/preview','skills/extraction/verify'])
def test_slow_model_preview_does_not_hold_the_save_connection(service, monkeypatch, route):
    from evolvmem import web_server, extraction_preview
    entered,release=threading.Event(),threading.Event()
    def preview(reader,body,**kwargs):
        entered.set();assert release.wait(4)
        assert reader.store._connection().execute('PRAGMA query_only').fetchone()[0]==1
        if route == 'skills/extraction/verify':
            assert set(body) == {'messages', 'project'}, 'verification uses saved rules only'
        return {'persisted':0,'model_calls':1,'redacted':0,
                'draft':{'rule_revision':reader.knowledge().rules.read()['revision'],'candidates':[]}}
    monkeypatch.setattr(extraction_preview,'preview',preview)
    server=web_server.make_http_server(service,port=0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def post(route,body):
        req=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/knowledge/'+route,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=2) as response:return json.load(response)
    try:
        with concurrent.futures.ThreadPoolExecutor() as pool:
            pending=pool.submit(post,route,{'rules': {'instructions': 'unsaved draft'}})
            assert entered.wait(1)
            try:assert post('projects',{'project':'save-during-model'})['ok']
            finally:release.set()
            result=pending.result()
            assert result.get('persisted',result.get('created'))==0
    finally:release.set();server.shutdown();server.server_close();thread.join()

"""Large conversations and a slow request must not freeze the whole workbench."""
import concurrent.futures
import json
import threading
import urllib.request
import urllib.error

import pytest
from tests.test_cross_batch_context import config, service, craft, SESSION
from evolvmem import conversation_tasks as tasks


@pytest.mark.parametrize('read', [tasks.snapshot, tasks.revision])
def test_conversation_policy_scan_is_once_and_refreshes_between_reads(service, read):
    sources=[craft(service, session=SESSION, start=n*3+1, end=n*3+3,
                   text=f'需求 {n}：保存设置并显示结果。') for n in range(12)]
    key=tasks.register(service,[f'archive:{s.id}' for s in sources])[0]
    conn=service.store._connection();queries=[]
    conn.set_trace_callback(queries.append)
    try:
        read(service,key)
    finally:
        conn.set_trace_callback(None)
    scans=[q for q in queries if 'SELECT e.archive_id FROM archive_source_exclusions' in q]
    assert len(scans)==1, f'{len(scans)} full policy scans for one conversation'
    # Request-local reuse must never conceal a later source exclusion.
    with service.store.transaction():
        conn.execute("INSERT INTO archive_source_exclusions(archive_id,payload_sha256,reason,verified_at) "
                     "SELECT id,payload_sha256,'system','2026-10-19' FROM session_archives WHERE id=?",(sources[-1].id,))
    with pytest.raises(ValueError,match='classification_source_changed'):
        read(service,key)


def test_slow_business_request_keeps_assets_login_and_bounded_retry_available(config,monkeypatch):
    from tests.test_web_server import _make_service
    from evolvmem import web_server, knowledge_api
    s=_make_service(config)
    entered,release=threading.Event(),threading.Event()
    original=knowledge_api.dispatch
    def delayed(service,method,path,body):
        if path.endswith('/slow-test'):
            entered.set();assert release.wait(5)
            return {'ok':True}
        return original(service,method,path,body)
    monkeypatch.setattr(knowledge_api,'dispatch',delayed)
    monkeypatch.setattr(web_server,'_BUSINESS_WAIT_SECONDS',0.1,raising=False)
    server=web_server.make_http_server(s,port=0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_address[1]}'
    def get(path):
        with urllib.request.urlopen(base+path,timeout=1) as response:return response.read()
    try:
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            future=pool.submit(get,'/api/knowledge/slow-test')
            assert entered.wait(1)
            try:
                assert b'knowledge' in get('/knowledge.css')
                assert json.loads(get('/api/auth/me'))['enabled'] is False
                assert json.loads(get('/api/knowledge/projects'))['projects']==[]
                with pytest.raises(urllib.error.HTTPError) as error:
                    get('/api/stats')
                assert error.value.code==503
                assert json.loads(error.value.read())['error']=='service_busy'
            finally:release.set()
            assert json.loads(future.result())['ok']
        assert json.loads(get('/api/knowledge/projects'))['projects']==[]
    finally:
        release.set();server.shutdown();server.server_close();thread.join();s.close()


def test_slow_read_has_own_snapshot_and_does_not_block_save_or_next_read(config,monkeypatch):
    from tests.test_web_server import _make_service
    from evolvmem import web_server, knowledge_api
    s=_make_service(config); entered,release=threading.Event(),threading.Event()
    original=knowledge_api.dispatch
    def delayed(reader,method,path,body):
        if method=='GET' and body.get('hold'):
            # Establish the snapshot before a concurrent save commits.
            assert original(reader,method,path,{})['projects']==[]
            entered.set();assert release.wait(4)
        return original(reader,method,path,body)
    monkeypatch.setattr(knowledge_api,'dispatch',delayed)
    server=web_server.make_http_server(s,port=0)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    base=f'http://127.0.0.1:{server.server_address[1]}/api/knowledge/projects'
    def request(url,body=None):
        req=urllib.request.Request(url,data=json.dumps(body).encode() if body else None,
                                   headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=2) as response:return json.load(response)
    try:
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            old=pool.submit(request,base+'?hold=1')
            assert entered.wait(1)
            try:
                assert request(base,{'project':'parallel-save'})['ok']
                assert request(base)['projects'][0]['project']=='parallel-save'
            finally:release.set()
            assert old.result()['projects']==[]
        assert request(base)['projects'][0]['project']=='parallel-save'
    finally:
        release.set();server.shutdown();server.server_close();thread.join();s.close()

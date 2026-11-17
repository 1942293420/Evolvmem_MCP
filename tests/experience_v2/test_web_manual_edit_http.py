"""Real Web HTTP editing with required dual projections and atomic failures."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import subprocess
import psycopg
import pytest
from tests.experience_v2.test_embedding_runtime import live_server
from tests.stage5.test_web_console_v2_full_chain import v2_live_server, _http_req, OWNER_CSRF, PEER_CSRF
from evolvmem.experience_v2.embedding_client import note_content_sha256
from evolvmem.experience_v2.pg_experience_store import load_pg_experience_settings
from evolvmem.experience_v2.projection_maintenance import repair_projections, activate_policy


@pytest.fixture
def configured_web(v2_live_server,live_server,monkeypatch,tmp_path):
    client,runtime=live_server
    cfg=tmp_path/'warm-client.json';cfg.write_text(json.dumps(asdict(client.config)))
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_EMBEDDING_CONFIG',str(cfg))
    settings=load_pg_experience_settings()
    conn=psycopg.connect(v2_live_server['dsn'],options=f"-c search_path={v2_live_server['schema']},public")
    try:
        repair_projections(conn,secret=settings.ticket_secret,client=client)
        activate_policy(conn,client);conn.commit()
        yield v2_live_server,conn,client,runtime
    finally:conn.close()


def post(web,body,*,cookie='tok-owner-v2',csrf=OWNER_CSRF):
    status,_,raw=_http_req(web['port'],'POST','/api/console-v2/experiences/write',cookie=cookie,csrf=csrf,body=body)
    return status,json.loads(raw)


def edit_body(web,revision=1):
    return dict(action='update',note_id=web['seeds']['active_id'],expected_revision=revision,
                category='前端经验',problem='保存后的编辑内容需要刷新',solution='核对保存结果后刷新当前记录',
                reusable_experience='提交正文和两套向量时保持同一事务',request_id='web-http-edit-001')


def state(conn,nid):
    return conn.execute('SELECT category,problem,solution,reusable_experience,revision,embedding::text,retrieval_embedding::text,'
        'retrieval_embedding_content_sha256,scope,evidence,origin,kind FROM experience_notes WHERE id=%s',(nid,)).fetchone()


def test_web_edit_requires_current_positive_integer_revision(configured_web):
    web,conn,client,runtime=configured_web
    before=state(conn,web['seeds']['active_id']);calls=len(runtime.retrieval.calls)
    for revision in (None,True,False,0,-1,'1',1.5):
        body=edit_body(web)
        if revision is None:body.pop('expected_revision')
        else:body['expected_revision']=revision
        status,out=post(web,body)
        assert status==400,out
        assert out['error'] in ('expected_revision_required','invalid_expected_revision')
        assert state(conn,web['seeds']['active_id'])==before
    assert len(runtime.retrieval.calls)==calls


def test_web_edit_updates_both_projections_and_keeps_draft_safe_on_failures(configured_web):
    web,conn,client,runtime=configured_web;body=edit_body(web)
    before=state(conn,body['note_id']);calls=len(runtime.retrieval.calls)
    status,out=post(web,body)
    assert status==200 and out['ok'],out
    note=out['note'];after=state(conn,body['note_id'])
    assert note['revision']==2 and after[7]==note_content_sha256(note)
    assert after[8:]==before[8:]
    assert conn.execute('SELECT vector_dims(embedding),vector_dims(retrieval_embedding) FROM experience_notes WHERE id=%s',(body['note_id'],)).fetchone()==(768,1024)
    assert len(runtime.retrieval.calls)==calls+1
    status,replay=post(web,body)
    assert status==200 and replay['replayed']
    assert len(runtime.retrieval.calls)==calls+1
    status,stale=post(web,{**body,'request_id':'web-http-stale-001'})
    assert status==409 and stale['error']=='experience_revision_conflict'
    assert state(conn,body['note_id'])==after
    status,_=post(web,{**body,'owner_id':'admin_owner','expected_revision':2,'request_id':'web-http-peer-001'},cookie='tok-peer-v2',csrf=PEER_CSRF)
    assert status==403
    status,_=post(web,{**body,'expected_revision':2,'request_id':'web-http-csrf-001'},csrf='')
    assert status==403
    assert len(runtime.retrieval.calls)==calls+1
    runtime._ready=False
    status,failed=post(web,{**body,'expected_revision':2,'request_id':'web-http-outage-001','problem':'编码失败的正文不能保存'})
    assert status==503 and failed['error']=='embedding_not_ready'
    assert state(conn,body['note_id'])==after
    assert conn.execute("SELECT count(*) FROM experience_requests WHERE request_id LIKE 'web-http-%'").fetchone()==(1,)


@pytest.mark.skipif(os.environ.get('EVOLVMEM_RUN_BROWSER_TESTS')!='1',reason='optional real Chromium integration')
def test_real_browser_edit_reaches_pg_with_attested_vectors(configured_web):
    web,conn,client,runtime=configured_web
    env=dict(os.environ,EDITOR_PG_URL=f"http://127.0.0.1:{web['port']}",EDITOR_PG_NOTE_ID=web['seeds']['active_id'],EVOLVMEM_SYNTHETIC_FIXTURE='1')
    node=os.environ.get('EVOLVMEM_NODE_BINARY','node')
    result=subprocess.run([node,'tests/browser/manual_edit_pg.e2e.cjs'],env=env,cwd=Path(__file__).resolve().parents[2],capture_output=True,text=True,timeout=45)
    assert result.returncode==0,result.stdout+result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1])['passed']
    row=state(conn,web['seeds']['active_id'])
    note=dict(category=row[0],problem=row[1],solution=row[2],reusable_experience=row[3])
    assert row[1]=='真实浏览器修改的经验正文' and row[4]==2
    assert row[7]==note_content_sha256(note)
    assert conn.execute('SELECT vector_dims(embedding),vector_dims(retrieval_embedding) FROM experience_notes WHERE id=%s',(web['seeds']['active_id'],)).fetchone()==(768,1024)

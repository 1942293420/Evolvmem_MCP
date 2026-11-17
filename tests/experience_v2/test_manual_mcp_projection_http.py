"""Real MCP HTTP -> PG Writer -> warm embedding HTTP atomic create/edit."""
from dataclasses import asdict
import http.client
import json
import threading
import uuid
from psycopg.conninfo import make_conninfo
from tests.experience_v2.test_hybrid_search import db
from tests.experience_v2.test_embedding_runtime import live_server
from tests.stage2.conftest import _MaskedDsn
from tests.stage5.conftest import STAGE5_PW_FILE
from evolvmem.experience_v2.embedding_client import note_content_sha256
from evolvmem.experience_v2.pg_experience_store import PgExperienceSettings
from evolvmem.history_pg.unified_mcp_host import UnifiedMcpHost
from evolvmem.library_foundation import RequestIdentity
from evolvmem.lan_server import make_http_server


def test_mcp_create_edit_replay_cas_and_encoder_outage(db,live_server,monkeypatch,tmp_path):
    client,runtime=live_server
    config=tmp_path/'embedding.json';config.write_text(json.dumps(asdict(client.config)))
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_EMBEDDING_CONFIG',str(config))
    db.execute('INSERT INTO experience_embedding_policy(singleton,required,retrieval_contract_id,legacy_contract_id) VALUES(true,true,%s,%s)',
               (client.retrieval_contract_id,client.legacy_contract_id));db.commit()
    settings=PgExperienceSettings(dsn=_MaskedDsn(make_conninfo(db.info.dsn,password=STAGE5_PW_FILE.read_text().strip())))
    identity=RequestIdentity(principal_id=str(uuid.uuid4()),credential_id=str(uuid.uuid4()),device_id=str(uuid.uuid4()),credential_epoch=1)
    host=UnifiedMcpHost.__new__(UnifiedMcpHost)
    # Identity/ACL have separate real control-PG coverage. Keep this fixture
    # focused on the actual HTTP manual dispatch and atomic encoder/PG chain.
    host.authenticate=lambda token:identity if token=='synthetic' else None
    host._resolve_user_id=lambda identity,**kw:'alice'
    host._exp_settings=lambda:settings
    host.tokens={};host.allow_sleep_probe=False;host.request_timeout_seconds=3
    server=make_http_server(None,host='127.0.0.1',port=0,library_host=host,allow_legacy=False)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    def rpc(name,args):
        con=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=3)
        try:
            con.request('POST','/mcp',json.dumps(dict(id=1,method='tools/call',params=dict(name=name,arguments=args))),
                        {'Content-Type':'application/json','Authorization':'Bearer synthetic'})
            response=con.getresponse();assert response.status==200
            result=json.loads(response.read())['result']
            return result['isError'],json.loads(result['content'][0]['text'])
        finally:con.close()
    def state():
        return db.execute('SELECT problem,revision,embedding::text,retrieval_embedding::text,retrieval_embedding_content_sha256 FROM experience_notes').fetchone()
    try:
        body=dict(category='业务经验',problem='费用字段为空',solution='查原始费用记录补齐',reusable_experience='核实费用后才核算',request_id='http-manual-create-001')
        err,out=rpc('manual_experience_save',body);assert not err,out
        note=out['experience'];assert note['revision']==1
        assert db.execute('SELECT vector_dims(embedding),vector_dims(retrieval_embedding) FROM experience_notes').fetchone()==(768,1024)
        edit={**body,'id':note['id'],'revision':1,'request_id':'http-manual-edit-001','problem':'费用票据无法定位'}
        err,out=rpc('manual_experience_update',edit);assert not err,out
        assert out['experience']['revision']==2
        assert state()[-1]==note_content_sha256(out['experience'])
        assert len(runtime.retrieval.calls)==2
        assert rpc('manual_experience_update',edit)[1]['replayed'] is True
        err,stale=rpc('manual_experience_update',{**edit,'request_id':'http-stale-edit-001'})
        assert err and stale['error']=='experience_revision_conflict'
        assert len(runtime.retrieval.calls)==2
        before=state();runtime._ready=False
        err,failed=rpc('manual_experience_update',{**edit,'revision':2,'request_id':'http-outage-edit-001','problem':'不得提交的正文'})
        assert err and failed['error']=='embedding_not_ready'
        assert state()==before
        assert db.execute('SELECT count(*) FROM experience_requests').fetchone()==(2,)
    finally:
        server.shutdown();server.server_close();thread.join(2)

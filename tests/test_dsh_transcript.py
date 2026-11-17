"""Synthetic native DSH v4 fixtures; never copy private conversation contents."""
import hashlib
import json
import pytest
from tests.test_lan_sharing import lan
from tests.test_lan_capture import upload
from evolvmem.lan_sharing import LanError

FORMAT = 'dsh-session-jsonl-v1'

def native_rows(session='session-1'):
    header = dict(type='session',version=4,id=session,createdAt=1,cwd='/work/demo',
                  isSeeded=False,delegationDepth=0)
    def message(role, text, source):
        return dict(id=role+'-1',role=role,content=[dict(type='text',text=text)],source=source)
    data = [
        ('turn/start',dict(turn=1)),
        ('system/message',dict(turn=1,step=1,message=message('system','Synthetic system instructions',{'kind':'system-prompt'}))),
        ('user/message',message('user','Synthetic project export request',{'kind':'user'})),
        ('tool/call',dict(turn=1,step=1,callId='call-1',name='verify',arguments='{}')),
        ('tool/result',dict(turn=1,step=1,message={**message('tool','Synthetic verification result',{'kind':'tool','callId':'call-1'}),'toolCallId':'call-1'})),
        ('assistant/message',dict(turn=1,step=1,message=message('assistant','Synthetic export feature is verified',{'kind':'model','provider':'fixture','model':'fixture'}),stream=[])),
        ('turn/end',dict(turn=1,reason={'kind':'completed'})),
    ]
    return [header]+[dict(type=kind,seq=i,time=i,data=value) for i,(kind,value) in enumerate(data)]

def native_bytes(rows=None, session='session-1'):
    return ('\n'.join(json.dumps(v,ensure_ascii=False) for v in (rows or native_rows(session)))+'\n').encode()

def test_native_parser_keeps_every_event_and_derived_roles():
    from evolvmem.dsh_transcript import parse_transcript,workspace_details
    raw=native_bytes(); rows,messages=parse_transcript(raw,'session-1')
    assert rows==native_rows()
    assert [v['role'] for v in messages]==['system','user','tool','assistant']
    assert workspace_details(rows)['cwd']=='/work/demo'
    assert workspace_details(rows)['attribution_reason']==''

@pytest.mark.parametrize('mutation',[
    lambda r:r[0].update(id='wrong'),
    lambda r:r[0].update(version=5),
    lambda r:r[0].update(isSeeded=True),
    lambda r:r[2].update(seq=20),
    lambda r:r[2].update(seq=True),
    lambda r:r[2].update(time=float('nan')),
    lambda r:r[2].update(payload={}),
])
def test_native_parser_rejects_unsupported_or_discontinuous_source(mutation):
    from evolvmem.dsh_transcript import parse_transcript
    rows=native_rows();mutation(rows)
    with pytest.raises(LanError):parse_transcript(native_bytes(rows),'session-1')

def test_native_unicode_separators_and_full_bytes_survive(lan):
    from evolvmem.session_archive import SessionArchiver
    rows=native_rows();rows[3]['data']['content'][0]['text']+='\u2028not a physical row'
    raw=native_bytes(rows);runtime,adapter=lan
    saved=upload(adapter,raw,source_format=FORMAT,source_order=1,current_sha256=hashlib.sha256(raw).hexdigest())
    assert saved.get('status')=='archived',saved
    assert saved['source_format']==FORMAT
    server=runtime.server_for('jiangli');store=server.context_service.store
    archive=store.get_session_archive(saved['archive_id'])
    assert archive['adapter']=='dsh'
    payload=json.loads(SessionArchiver(server.config,store).read_payload(saved['archive_id']))
    assert payload['source_format']==FORMAT and payload['transcript'].encode()==raw
    assert 'Synthetic system instructions' in payload['transcript'] and 'Synthetic verification result' in payload['transcript']
    assert all(v['role'] in {'user','assistant'} for v in payload['conversation'])
    status=adapter.call_tool('jiangli','session_archive_status',{'device_id':'windows-main','session_id':'session-1','handoff':{'sha256':hashlib.sha256(raw).hexdigest(),'project':'demo','source_format':FORMAT}})
    assert status['handoff']['source_format']==FORMAT
    wrong=adapter.call_tool('jiangli','session_archive_status',{'device_id':'windows-main','session_id':'session-1','handoff':{'sha256':hashlib.sha256(raw).hexdigest(),'project':'demo'}})
    assert wrong['handoff']['reason']=='archive_source_format_mismatch'

def test_source_format_is_immutable_from_first_chunk(lan):
    raw=native_bytes();adapter=lan[1]
    first=upload(adapter,raw,end=80,source_format=FORMAT)
    assert first.get('source_format')==FORMAT,first
    changed=upload(adapter,raw,start=80)
    assert changed['error']=='upload_metadata_conflict'
    assert upload(adapter,raw,start=80,source_format=FORMAT)['status']=='archived'

def test_dsh_never_auto_detected_or_mislabeled(lan):
    adapter=lan[1];raw=native_bytes()
    assert upload(adapter,raw)['error'] in {'invalid_transcript','session_id_mismatch'}
    assert upload(adapter,raw,source_format='fake-codex')['error']=='invalid_arguments'

def test_native_subagent_is_archived_but_not_organized(lan):
    rows=native_rows();rows[0].update(parentSession='parent',origin='subagent',delegationDepth=1)
    raw=native_bytes(rows)
    saved=upload(lan[1],raw,source_format=FORMAT,extract=True)
    assert saved.get('status')=='archived',saved
    assert saved['extraction_status']=='excluded' and saved['project']==''
    from evolvmem.codex_transcript import payload_is_subagent
    assert payload_is_subagent({'source_format':FORMAT,'transcript':raw.decode(),'session_id':'session-1'})

def test_dsh_continuity_backfill_is_not_codex_projection(lan):
    from evolvmem.lan_backfill import _import
    from evolvmem.lan_capture import LanCapture
    runtime,adapter=lan;raw=native_bytes()
    saved=upload(adapter,raw,source_format=FORMAT)
    capture=LanCapture(runtime.server_for('jiangli'))
    row=capture.conn.execute('SELECT * FROM lan_session_uploads WHERE archive_id=?',(saved['archive_id'],)).fetchone()
    assert _import(capture,row)=={'code':'skipped','reason':'native_continuity_managed_by_client'}


def test_dsh_current_summary_uses_actual_archive_provenance(lan,tmp_path,monkeypatch):
    from tests import test_lan_handoff_status as proof_tests
    policy=tmp_path/'enabled-policy.json';policy.write_text(json.dumps({'ai_processing_enabled':True,'legacy_web_enabled':True}))
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE',str(policy))
    original_upload=proof_tests.upload
    monkeypatch.setattr(proof_tests,'transcript',lambda session,text:native_bytes(session=session))
    monkeypatch.setattr(proof_tests,'upload',lambda *a,**kw:original_upload(*a,**kw,source_format=FORMAT))
    service,raw,saved,task,result=proof_tests.seed(lan)
    before=service.store._connection().total_changes
    status=lan[1].call_tool('jiangli','session_archive_status',{'device_id':'windows-main','session_id':'session-1','handoff':{'sha256':hashlib.sha256(raw).hexdigest(),'project':'demo','source_format':FORMAT}})
    proof=status['handoff']
    assert proof['state']=='ready',proof
    assert proof['source_format']==FORMAT and proof['task_id']==task['id']
    assert proof['summary']['event_ids']==result['event_ids']
    assert service.store._connection().total_changes==before


def test_actual_installed_sdk_fixture_archives_all_native_bytes(lan):
    from pathlib import Path
    from evolvmem.dsh_transcript import parse_transcript
    from evolvmem.session_archive import SessionArchiver
    raw=(Path(__file__).parent/'fixtures'/'dsh-native-v4.jsonl').read_bytes()
    session=json.loads(raw.split(b'\n',1)[0])['id']
    rows,messages=parse_transcript(raw,session)
    assert len(rows)==17 and hashlib.sha256(raw).hexdigest()=='7e3e0a037a2e6c499aa2b8f534ed7434fbc471d805f30ea5b8d6f5501151abde'
    assert {'request/header','request/context','agent/inbox/spliced','tool/result','assistant/message'} <= {v['type'] for v in rows}
    saved=upload(lan[1],raw,session=session,source_format=FORMAT,source_order=1,current_sha256=hashlib.sha256(raw).hexdigest())
    assert saved.get('status')=='archived',saved
    server=lan[0].server_for('jiangli')
    payload=json.loads(SessionArchiver(server.config,server.context_service.store).read_payload(saved['archive_id']))
    assert payload['transcript'].encode()==raw

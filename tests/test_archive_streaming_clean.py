import hashlib
import json
from pathlib import Path
import tracemalloc

import pytest

from evolvmem import archive_streaming_clean as streaming
from evolvmem.conversation import from_payload
from evolvmem.extraction_policy import redact_messages
from evolvmem.session_archive import SessionArchiver
from tests.test_governance_migration import service, base_service, plan

SESSION='5b41915e-6ff8-4c2c-8fb8-7e0bdd3f070a'


def native(*, output='tool output', child=False):
    rows=[{'type':'session_meta','payload':{'id':SESSION,'cwd':'/tmp/demo','source':{'subagent':{}} if child else 'cli'}},
        {'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'evo 导入要防止重复，中文🌌。'}]}},
        {'type':'event_msg','payload':{'type':'user_message','message':'evo 导入要防止重复，中文🌌。'}},
        {'type':'response_item','payload':{'type':'function_call_output','output':output}},
        {'type':'response_item','payload':{'type':'message','role':'assistant','channel':'analysis','content':[{'type':'output_text','text':'private reasoning'}]}},
        {'type':'response_item','payload':{'type':'message','role':'assistant','content':[{'type':'output_text','text':'方案：检查幂等键。'}]}},
        {'type':'event_msg','payload':{'type':'agent_message','message':'方案：检查幂等键。'}}]
    return {'session_id':SESSION,'transcript':''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows)}


def archive(service,payload):
    result=SessionArchiver(service.config,service.store).archive_session('', 'codex', SESSION, json.dumps(payload,ensure_ascii=False))
    conn=service.store._connection()
    a=dict(conn.execute('SELECT * FROM session_archives WHERE id=?',(result.id,)).fetchone())
    p=dict(conn.execute('SELECT * FROM session_archive_storage WHERE archive_id=?',(result.id,)).fetchone())
    return a,p


@pytest.mark.parametrize('child',[False,True])
@pytest.mark.parametrize('output',['',{'structured':'tool output'},'some tool output'])
def test_streaming_clean_matches_original_semantics(service,child,output):
    payload=native(child=child,output=output)
    a,p=archive(service,payload)
    policy=service.knowledge().rules.read()
    expected,_=redact_messages(from_payload(payload,policy=policy))
    actual,proof=streaming.derive(service.config,a,p,policy)
    assert actual==expected
    assert proof['logical_bytes']==p['logical_bytes'] and proof['cleaned_messages']==len(expected)


def test_huge_tool_string_is_skipped_with_bounded_memory(service):
    payload=native(output='x'*(12*1024*1024))
    a,p=archive(service,payload)
    tracemalloc.start()
    messages,proof=streaming.derive(service.config,a,p,service.knowledge().rules.read())
    peak=tracemalloc.get_traced_memory()[1];tracemalloc.stop()
    assert len(messages)==2 and proof['logical_bytes']>12*1024*1024
    assert peak<4*1024*1024


def test_missing_body_repair_is_additive_idempotent_and_cas_guarded(service,monkeypatch):
    a,p=archive(service,native())
    conn=service.store._connection()
    original=dict(conn.execute('SELECT * FROM conversation_history WHERE archive_id=?',(a['id'],)).fetchone())
    conn.execute('DELETE FROM conversation_history WHERE archive_id=?',(a['id'],));conn.commit()
    prepared=plan(service);key=f"archive:{a['id']}"
    before=hashlib.sha256((service.config.data_dir/a['payload_path']).read_bytes()).hexdigest()
    preview=streaming.repair(service,prepared,key)
    assert preview['applied'] is False
    assert conn.execute('SELECT count(*) FROM conversation_history WHERE archive_id=?',(a['id'],)).fetchone()[0]==0
    result=streaming.repair(service,prepared,key,apply=True)
    assert result['applied']
    restored=dict(conn.execute('SELECT * FROM conversation_history WHERE archive_id=?',(a['id'],)).fetchone())
    assert (restored['messages'],restored['body'],restored['content_hash'])==(original['messages'],original['body'],original['content_hash'])
    assert streaming.repair(service,prepared,key,apply=True)['already_repaired']
    assert hashlib.sha256((service.config.data_dir/a['payload_path']).read_bytes()).hexdigest()==before


def test_concurrent_body_creation_or_source_drift_cannot_be_overwritten(service,monkeypatch):
    a,p=archive(service,native());conn=service.store._connection()
    conn.execute('DELETE FROM conversation_history WHERE archive_id=?',(a['id'],));conn.commit()
    prepared=plan(service);real=streaming.derive
    def race(*args,**kwargs):
        result=real(*args,**kwargs)
        with service.store.transaction():
            conn.execute('INSERT INTO conversation_history VALUES(?,?,?,?,?)',(a['id'],'[]','Human edited body','human','now'))
        return result
    monkeypatch.setattr(streaming,'derive',race)
    with pytest.raises(ValueError,match='migration_revision_conflict'):
        streaming.repair(service,prepared,f"archive:{a['id']}",apply=True)
    assert conn.execute('SELECT body FROM conversation_history WHERE archive_id=?',(a['id'],)).fetchone()[0]=='Human edited body'


def test_invalid_manifest_and_content_are_not_repaired(service):
    a,p=archive(service,native());policy=service.knowledge().rules.read()
    with pytest.raises(ValueError,match='manifest_changed'):
        streaming.derive(service.config,a,{**p,'manifest_sha256':'bad'},policy)
    with pytest.raises(ValueError,match='metadata_mismatch'):
        streaming.derive(service.config,a,{**p,'content_sha256':'bad'},policy)
    with pytest.raises(ValueError,match='logical_budget'):
        streaming.derive(service.config,a,{**p,'logical_bytes':streaming.MAX_LOGICAL_BYTES+1},policy)


def test_outer_conversation_precedence_is_not_silently_changed(service):
    payload={**native(),'conversation':[{'role':'user','content':'preferred source'}]}
    a,p=archive(service,payload)
    with pytest.raises(ValueError,match='unsupported_outer_dialogue'):
        streaming.derive(service.config,a,p,service.knowledge().rules.read())


def test_cleaned_dialogue_budget_never_truncates_into_success(service):
    a,p=archive(service,native())
    with pytest.raises(ValueError,match='cleaned_archive_budget_exceeded'):
        streaming.derive(service.config,a,p,service.knowledge().rules.read(),max_clean_bytes=10)

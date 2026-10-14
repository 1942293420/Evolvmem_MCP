"""Historical uploads predating native source validation must not poison recall."""
import json
import hashlib

from tests.test_source_eligibility_repair import (
    lan, upload, system_subagent_transcript, user_fork_transcript,
)
from evolvmem import history_memory, memory_recall
from evolvmem.context_store import _now_iso
from evolvmem.legacy_models import LegacyExtractionItem, LegacyExtractionRequest
from evolvmem.session_archive import SessionArchiver


def test_local_archive_without_upload_receipt_is_repaired_and_cannot_be_organized(lan):
    from evolvmem import history_organization, conversation_tasks, auto_organization as org
    runtime,_=lan;service=runtime.server_for('jiangli').context_service
    raw=system_subagent_transcript('local-guardian').decode()
    archive=SessionArchiver(service.config,service.store).archive_session('', 'codex', 'local-old',
        json.dumps({'session_id':'local-guardian','transcript':raw}))
    key=f'archive:{archive.id}'
    # Emulate an already organized local archive, before this repair existed.
    with service.store.transaction():
        service.store._connection().execute("UPDATE conversation_history SET messages=?,body=? WHERE archive_id=?",
            (json.dumps([{'role':'user','content':'重复检查副本'}]),'用户：重复检查副本',archive.id))
    task=conversation_tasks.enqueue(service,[key])['items'][0]
    result=repair(service,apply=True)
    assert archive.id in result['excluded_archive_ids']
    assert history_organization.source_excluded(service,key)
    assert history_organization.source_excluded(service,task['source_key'])
    assert not org.task_view(service,task['id'])['source_current']
    assert org.task_view(service,task['id'])['status']=='superseded'
    assert SessionArchiver(service.config,service.store).read_payload(archive.id)
    assert not conversation_tasks.enqueue(service,[task['source_key']])['items']


def test_native_subagent_payload_is_not_readable_user_dialogue():
    from evolvmem.conversation import from_payload
    payload={'session_id':'guardian-1','transcript':system_subagent_transcript('guardian-1').decode(),
             'conversation':[{'role':'user','content':'内部复制的用户问题'}]}
    assert from_payload(payload)==[]
    payload['session_id']='user-fork';payload['transcript']=user_fork_transcript('user-fork').decode()
    assert from_payload(payload), 'ordinary user forks and quoted keywords remain dialogue'


def test_later_local_batch_uses_matching_native_header_not_body_keywords(lan,tmp_path):
    runtime,_=lan;service=runtime.server_for('jiangli').context_service
    sid='later-guardian';path=tmp_path/'rollout.jsonl';path.write_bytes(system_subagent_transcript(sid))
    external=hashlib.sha256(sid.encode()).hexdigest()[:32]+':401-412:'+'a'*16
    payload={'conversation':[{'role':'user','content':'任意业务文字，正文不参与判断。'}],
             'transcript':'', 'source':{'kind':'local_codex_jsonl','session_id':sid,'file':str(path)}}
    archive=SessionArchiver(service.config,service.store).archive_session('','codex',external,json.dumps(payload))
    assert archive.id in repair(service)['excluded_archive_ids']
    path.write_bytes(system_subagent_transcript('different-session'))
    assert archive.id not in repair(service)['excluded_archive_ids']
    path.write_bytes(user_fork_transcript(sid))
    assert archive.id not in repair(service)['excluded_archive_ids']
    path.unlink()
    assert archive.id not in repair(service)['excluded_archive_ids']


def legacy(lan, session='guardian-1'):
    runtime, adapter = lan
    service = runtime.server_for('jiangli').context_service
    service.knowledge().save_project({'project':'demo'})
    saved = upload(adapter, system_subagent_transcript(session), session=session)
    aid = saved['archive_id']
    with service.store.transaction():
        conn = service.store._connection()
        conn.execute("UPDATE lan_session_uploads SET project='demo',attribution_reason='',"
                     "extraction_status='completed',backfill_status='completed' WHERE archive_id=?", (aid,))
        conn.execute("UPDATE session_archives SET project='demo' WHERE id=?", (aid,))
    result = service.persist_legacy_extraction(LegacyExtractionRequest(
        summary=LegacyExtractionItem(key='project:demo:progress:log:'+session,
            value='向量检索故障修复材料。', attribute='fact'),
        candidates=(), source_session=session), source_archive_id=aid)
    return service, aid, result.summary.context_id


def repair(service, **kw):
    from evolvmem.legacy_source_repair import repair_subagents
    return repair_subagents(service, **kw)


def test_preview_then_repair_old_summary_without_organization_task(lan):
    service, aid, iid = legacy(lan)
    archive = service.store.get_session_archive(aid)
    preview = repair(service)
    assert preview['excluded_archive_ids'] == [aid]
    assert preview['downgraded_item_ids'] == [iid]
    assert service.knowledge().detail(iid)['status'] == 'active'
    assert service.store.get_session_archive(aid) == archive

    applied = repair(service, apply=True)
    assert applied['downgraded_item_ids'] == [iid]
    assert service.knowledge().detail(iid)['status'] == 'candidate'
    assert history_memory.sessions(service, 'demo') == []
    assert not memory_recall.recall(service, {'project':'demo','kind':'history',
                                            'query':'向量检索故障修复'})['history']
    conn = service.store._connection()
    for lid in service.knowledge().detail(iid)['legacy_ids']:
        assert conn.execute('SELECT status FROM memories WHERE id=?', (lid,)).fetchone()[0] == 'candidate'
    after = service.store.get_session_archive(aid)
    assert after['project'] == ''
    assert after['payload_sha256'] == archive['payload_sha256']
    assert SessionArchiver(service.config, service.store).read_payload(aid)
    assert repair(service, apply=True)['downgraded_item_ids'] == []


def test_human_and_independently_backed_items_survive(lan):
    service, aid, human = legacy(lan)
    _, second, shared = legacy(lan, 'guardian-2')
    conn = service.store._connection()
    other = SessionArchiver(service.config, service.store).archive_session(
        'demo','kimi','valid-source',json.dumps({'messages':[{'role':'user','content':'真实业务依据'}]}))
    with service.store.transaction():
        conn.execute("UPDATE context_project_resolutions SET decision_source='human' WHERE item_id=?", (human,))
        conn.execute("INSERT INTO context_sources(item_id,archive_id,source_kind,source_ref,extraction_version,created_at) "
                     "VALUES(?,?,'session','valid-source','test',?)", (shared, other.id, _now_iso()))
        conn.execute("INSERT INTO knowledge_cleaning_reviews(source_key,source_revision,source_text,cleaned_text,"
                     "category,rule_revision,state,updated_at) VALUES(?, '', '', '人工稿','reference','','ready',?)",
                     (f'archive:{aid}',_now_iso()))
    result = repair(service, apply=True)
    assert result['downgraded_item_ids'] == []
    assert {human,shared} == set(result['preserved_item_ids'])
    assert all(service.knowledge().detail(i)['status'] == 'active' for i in (human,shared))
    assert service.store.get_session_archive(aid)['project'] == 'demo'
    # Preserving the human item is not permission to inject its technical source.
    assert aid not in {r['id'] for r in history_memory.sessions(service,'demo')}


def test_metadata_only_bounded_scan_never_matches_body_or_parent_lineage(lan):
    runtime, adapter = lan
    service = runtime.server_for('jiangli').context_service
    raw = user_fork_transcript('fork-1')
    saved = upload(adapter, raw, session='fork-1')
    _, aid, _ = legacy(lan)
    first = repair(service, limit=1)
    assert first['examined'] == 1 and first['after_id'] == saved['archive_id']
    assert first['excluded_archive_ids'] == [] and first['has_more']
    second = repair(service, limit=1, after_id=first['after_id'])
    assert second['excluded_archive_ids'] == [aid]
    from evolvmem.codex_transcript import payload_is_subagent
    body = json.loads(raw.splitlines()[1])
    body['payload']['content'][0]['text'] = 'subagent guardian parent_thread_id'
    payload = {'session_id':'fork-1','transcript':raw.decode().splitlines()[0]+'\n'+json.dumps(body)}
    assert not payload_is_subagent(payload)
    payload['transcript'] += '\n'+system_subagent_transcript().decode().splitlines()[0]
    assert not payload_is_subagent(payload), 'a later lineage meta never defines the current source'
    assert not payload_is_subagent({'session_id':'wrong-id','transcript':system_subagent_transcript().decode()})


def test_unreadable_source_stays_untouched_and_reported(lan, monkeypatch):
    service, aid, iid = legacy(lan)
    monkeypatch.setattr(SessionArchiver,'read_payload',lambda self, identity: None)
    result = repair(service, apply=True)
    assert result['unavailable_archive_ids'] == [aid]
    assert result['excluded_archive_ids'] == []
    assert service.knowledge().detail(iid)['status'] == 'active'


def test_known_excluded_archive_never_enters_history_even_with_project(lan):
    service, aid, iid = legacy(lan)
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE lan_session_uploads SET attribution_reason='subagent_session' WHERE archive_id=?", (aid,))
    assert history_memory.sessions(service,'demo') == []


def test_manual_source_link_survives_even_when_attached_to_excluded_archive(lan):
    service, aid, iid = legacy(lan)
    with service.store.transaction():
        service.store._connection().execute("UPDATE context_sources SET source_kind='manual' WHERE item_id=?", (iid,))
    assert iid in repair(service, apply=True)['preserved_item_ids']
    assert service.knowledge().detail(iid)['status'] == 'active'


def test_two_excluded_sources_do_not_corroborate_each_other_across_pages(lan):
    service, first, iid = legacy(lan)
    _, second, _ = legacy(lan,'guardian-2')
    with service.store.transaction():
        service.store._connection().execute("INSERT INTO context_sources(item_id,archive_id,source_kind,source_ref,"
            "extraction_version,created_at) VALUES(?,?,'session',?,'test',?)", (iid,second,str(second),_now_iso()))
    assert iid in repair(service,limit=1,apply=True)['preserved_item_ids']
    assert iid in repair(service,after_id=first,limit=1,apply=True)['downgraded_item_ids']


def test_existing_repair_endpoint_requires_explicit_apply_for_legacy_scan(lan):
    from evolvmem.knowledge_api import dispatch
    service, aid, iid = legacy(lan)
    preview=dispatch(service,'POST','organization/repair',{'legacy_subagents':True,'limit':1})
    assert preview['excluded_archive_ids']==[aid] and not preview['applied']
    assert service.knowledge().detail(iid)['status']=='active'

"""Bounded migration roles from real stored structure, never prose prefixes."""
import hashlib
import json
import sqlite3

import pytest

from evolvmem import auto_organization as org, conversation_tasks as ct
from evolvmem import capability_assessment_migration as migration
from evolvmem.capability_store import CapabilityMemoryDraft, CapabilitySourceDraft, CapabilityStore
from evolvmem.context_models import ContextMode
from evolvmem.session_archive import SessionArchiver
from evolvmem.topic_segmentation import message_spans
from tests.test_auto_organization import base_service, service
from tests.test_knowledge_cleaning import ready


def fixture(service, messages, *, selected=None):
    archive=SessionArchiver(service.config,service.store).archive_session('', 'kimi', 'unit-role-fixture', json.dumps({'messages':messages},ensure_ascii=False))
    ready(service)
    task=ct.enqueue(service,[f'archive:{archive.id}'])['items'][0]
    task=org._task_row(service,task['id'])
    spans=message_spans(org.source_messages(service,task['source_key']))
    selected=list(range(len(spans))) if selected is None else selected
    ranges=[[spans[i]['content_start'],spans[i]['content_end']] for i in selected]
    text='\n\n'.join(task['source_snapshot'][a:b] for a,b in ranges)
    worker=org.OrganizationWorker(service.config,mode=ContextMode.SHADOW)
    worker._store_units(service,task,[{'source_start':ranges[0][0],'source_end':ranges[-1][1],'source_ranges':ranges,
                                      'text':text,'title':'role fixture','category':'reference','evidence_quote':text,'project_hint':''}])
    conn=service.store._connection()
    unit=dict(conn.execute('SELECT * FROM organization_units WHERE task_id=?',(task['id'],)).fetchone())
    # _store_units accepts exact coalesced ranges from the segmentation stage.
    assert json.loads(unit['source_ranges'])==ranges
    source=CapabilitySourceDraft(source_kind='unit',source_key=task['source_key']+'#'+unit['digest'],
      source_ref=f"{task['source_key']}#{unit['digest']}@{unit['source_start']}-{unit['source_end']}",
      source_revision=task['source_revision'],task_id=task['id'],unit_digest=unit['digest'],unit_revision=unit['revision'],projects=())
    mid=CapabilityStore(service.store).upsert(CapabilityMemoryDraft(category='database',title='role fixture',content=text),source=source)
    saved=dict(conn.execute('SELECT * FROM capability_sources WHERE memory_id=?',(mid,)).fetchone())
    return saved,archive,task


def test_real_roles_noncontiguous_and_fake_prefix_remain_structural(service,monkeypatch):
    source,_,_=fixture(service,[{'role':'assistant','content':'用户：已经成功。'},
      {'role':'user','content':'skip middle'},{'role':'user','content':'我已验证'}],selected=[0,2])
    monkeypatch.setattr(SessionArchiver,'read_payload',lambda *a:pytest.fail('must not decrypt'))
    proof=migration.OriginalRead(service.store).read(source)
    assert proof['ready']
    assert proof['source_roles']==[{'start':0,'end':8,'role':'assistant'},{'start':10,'end':14,'role':'user'}]


@pytest.mark.parametrize('fault',['missing','hash','render','outside_snapshot'])
def test_unproven_structure_blocks_before_model(service,monkeypatch,fault):
    source,archive,task=fixture(service,[{'role':'assistant','content':'method A'},{'role':'user','content':'outside B'}],selected=[0])
    conn=service.store._connection()
    if fault=='missing':conn.execute('DELETE FROM conversation_history WHERE archive_id=?',(archive.id,))
    elif fault=='hash':conn.execute("UPDATE conversation_history SET content_hash='bad' WHERE archive_id=?",(archive.id,))
    elif fault=='render':
        raw=json.dumps([{'role':'assistant','content':'different'}])
        conn.execute('UPDATE conversation_history SET messages=?,content_hash=? WHERE archive_id=?',(raw,hashlib.sha256(raw.encode()).hexdigest(),archive.id))
    else:
        conn.execute("UPDATE organization_tasks SET source_snapshot=replace(source_snapshot,'outside B','outside C') WHERE id=?",(task['id'],))
    conn.commit()
    monkeypatch.setattr(SessionArchiver,'read_payload',lambda *a:pytest.fail('must not decrypt'))
    proof=migration.OriginalRead(service.store).read(source)
    assert not proof['ready']
    assert proof['reason'].startswith('unit_source_roles_unproven')
    prepared=migration.build_plan(conn,rule_revision=service.knowledge().rules.read()['revision'])
    assert prepared['entries'][0]['action']=='review'
    migration.apply_plan(service,prepared,provider=lambda _:pytest.fail('unproven roles must not reach model'))


def test_hash_mutation_invalidates_same_reader_cache(service):
    source,archive,_=fixture(service,[{'role':'assistant','content':'supported method'}])
    reader=migration.OriginalRead(service.store)
    assert reader.read(source)['source_roles']
    conn=service.store._connection()
    conn.execute("UPDATE conversation_history SET content_hash='changed' WHERE archive_id=?",(archive.id,));conn.commit()
    assert not reader.read(source)['ready']


def test_budget_precedes_provenance_body_read(service,monkeypatch):
    source,_,_=fixture(service,[{'role':'assistant','content':'small'},{'role':'user','content':'large source '*1000}],selected=[0])
    from evolvmem import capability_role_proof as roles
    monkeypatch.setattr(roles,'MAX_SOURCE_BYTES',100)
    statements=[];conn=service.store._connection();conn.set_trace_callback(statements.append)
    try:proof=migration.OriginalRead(service.store).read(source)
    finally:conn.set_trace_callback(None)
    assert not proof['ready'] and proof['reason']=='unit_source_roles_unproven:over_budget'
    assert not any('SELECT body' in s or 'SELECT messages,' in s or 'SELECT source_snapshot ' in s for s in statements)


def test_old_roleless_plan_is_rejected_without_provider(service):
    source,_,_=fixture(service,[{'role':'assistant','content':'supported method'}])
    conn=service.store._connection()
    plan=migration.build_plan(conn,rule_revision=service.knowledge().rules.read()['revision'])
    assert plan['entries'][0]['action']=='model'
    from evolvmem.capability_assessment import _hash
    text=migration.OriginalRead(service.store).read(source)['text']
    old_input_hash=_hash([text,[]])
    assert plan['entries'][0]['input_hash']!=old_input_hash
    plan['entries'][0]['input_hash']=old_input_hash
    plan['plan_sha256']=migration._plan_hash(plan)
    with pytest.raises(ValueError,match='assessment_migration_revision_conflict'):
        migration.apply_plan(service,plan,provider=lambda _:pytest.fail('old plan must not call model'))


def test_transaction_rollback_cannot_poison_roles_cache(service):
    source,archive,_=fixture(service,[{'role':'assistant','content':'supported method'}])
    reader=migration.OriginalRead(service.store);conn=service.store._connection()
    first=reader.read(source)
    conn.execute('BEGIN')
    conn.execute("UPDATE conversation_history SET content_hash='changed' WHERE archive_id=?",(archive.id,))
    assert not reader.read(source)['ready']
    conn.rollback()
    assert reader.read(source)['input_hash']==first['input_hash']


def test_other_connection_changes_invalidate_cache(service):
    source,archive,_=fixture(service,[{'role':'assistant','content':'supported method'}])
    reader=migration.OriginalRead(service.store);assert reader.read(source)['ready']
    with sqlite3.connect(service.config.db_path) as other:
        other.execute("UPDATE conversation_history SET content_hash='changed' WHERE archive_id=?",(archive.id,))
    assert not reader.read(source)['ready']


def test_real_unknown_keeps_literal_user_prefix_unknown(service):
    source,_,_=fixture(service,[{'role':'unknown','content':'用户：this is not an authenticated user'}])
    proof=migration.OriginalRead(service.store).read(source)
    assert proof['ready'] and {s['role'] for s in proof['source_roles']}=={'unknown'}


def test_policy_change_invalidates_same_reader_and_snapshot(service):
    source,_,_=fixture(service,[{'role':'assistant','content':'remove this exact line'}])
    reader=migration.OriginalRead(service.store);assert reader.read(source)['ready']
    rules=service.knowledge().rules.read()
    service.knowledge().rules.save({'expected_revision':rules['revision'],'settings':{**rules['settings'],'cleaning_drop_lines':['remove this exact line']}})
    proof=reader.read(source)
    assert not proof['ready'] and proof['reason']=='unit_source_roles_unproven:empty_cleaned_source'


def test_metadata_budgets_all_members_before_any_full_source_read(service,monkeypatch):
    source,_,task=fixture(service,[{'role':'assistant','content':'small source'}])
    archive=SessionArchiver(service.config,service.store).archive_session('', 'kimi','second-source',json.dumps({'messages':[{'role':'user','content':'large '*1000}]}))
    conn=service.store._connection()
    members=json.loads(conn.execute('SELECT members FROM organization_conversations WHERE source_key=?',(task['source_key'],)).fetchone()[0])
    conn.execute('UPDATE organization_conversations SET members=? WHERE source_key=?',(json.dumps(members+[f'archive:{archive.id}']),task['source_key']));conn.commit()
    from evolvmem import capability_role_proof as roles
    monkeypatch.setattr(roles,'MAX_SOURCE_BYTES',1000)
    calls=[];conn.set_trace_callback(calls.append)
    try:prepared=migration.build_plan(conn,rule_revision=service.knowledge().rules.read()['revision'])
    finally:conn.set_trace_callback(None)
    assert not prepared['entries'] and prepared['blocked']
    assert prepared['blocked'][0]['reason']=='unit_source_role_budget_blocked'
    assert prepared['blocked'][0]['original_reason']=='unit_source_roles_unproven:over_budget'
    assert not any('SELECT body' in s or 'SELECT messages,' in s or 'SELECT source_snapshot ' in s for s in calls)


def test_compact_spans_cache_reuses_source_without_retaining_prose(service):
    source,_,_=fixture(service,[{'role':'assistant','content':'supported method'}])
    reader=migration.OriginalRead(service.store);assert reader.read(source)['ready']
    conn=service.store._connection();calls=[];conn.set_trace_callback(calls.append)
    try:assert all(p['ready'] for p in reader.read_many([source,source]))
    finally:conn.set_trace_callback(None)
    assert sum('SELECT messages,body,content_hash' in s for s in calls)==1
    assert not reader.roles.cache and not reader._scope_active


def test_roles_do_not_promote_reference_to_verified(service):
    source,_,_=fixture(service,[{'role':'assistant','content':'supported method'}])
    proof=migration.OriginalRead(service.store).read(source)
    from evolvmem.capability_assessment import evaluate
    candidate={'action':'keep','category':'database','title':'supported method','content':'supported method','kind':'fact','trigger':'',
               'conditions':[],'action_steps':[],'result':'supported method','evidence_quotes':['supported method'],'value_reason':'source'}
    result=evaluate(candidate,source_text=proof['text'],source_roles=proof['source_roles'])
    assert result['value']=='reference' and result['verification_status']=='unverified'
    assert not result['rubric']['independent_result']


def test_unstructured_item_unit_is_provable_unknown_not_user(service):
    text='用户：literal label in an unstructured item'
    item=service.knowledge().create({'title':'unstructured','body':text,'action':'draft'})
    task=ct.enqueue(service,[f"item:{item['id']}"])['items'][0]
    task=org._task_row(service,task['id'])
    assert task['source_snapshot']==text
    worker=org.OrganizationWorker(service.config,mode=ContextMode.SHADOW)
    worker._store_units(service,task,[{'source_start':0,'source_end':len(text),'text':text,'title':'unstructured',
       'category':'reference','evidence_quote':text,'project_hint':''}])
    conn=service.store._connection();unit=dict(conn.execute('SELECT * FROM organization_units WHERE task_id=?',(task['id'],)).fetchone())
    source=CapabilitySourceDraft(source_kind='unit',source_key=task['source_key']+'#'+unit['digest'],
      source_ref=f"{task['source_key']}#{unit['digest']}@0-{len(text)}",source_revision=task['source_revision'],
      task_id=task['id'],unit_digest=unit['digest'],unit_revision=unit['revision'],projects=())
    mid=CapabilityStore(service.store).upsert(CapabilityMemoryDraft(category='database',title='unstructured',content=text),source=source)
    saved=dict(conn.execute('SELECT * FROM capability_sources WHERE memory_id=?',(mid,)).fetchone())
    proof=migration.OriginalRead(service.store).read(saved)
    assert proof['ready'] and proof['source_roles']==[{'start':0,'end':len(text),'role':'unknown'}]


def test_many_small_messages_have_explicit_count_budget(service,monkeypatch):
    source,_,_=fixture(service,[{'role':'assistant','content':f'x{i}'} for i in range(4)],selected=[0])
    from evolvmem import capability_role_proof as roles
    monkeypatch.setattr(roles,'MAX_MESSAGES',3,raising=False)
    proof=migration.OriginalRead(service.store).read(source)
    assert not proof['ready'] and proof['reason']=='unit_source_roles_unproven:message_count_over_budget'


def test_corrupt_oversized_unit_is_budgeted_before_legacy_discovery(service,monkeypatch):
    source,_,task=fixture(service,[{'role':'assistant','content':'small real source'}])
    conn=service.store._connection()
    conn.execute('UPDATE organization_units SET text=? WHERE task_id=?',('x'*10000,task['id']));conn.commit()
    from evolvmem import capability_role_proof as roles
    monkeypatch.setattr(roles,'MAX_UNIT_CHARS',100,raising=False)
    calls=[];conn.set_trace_callback(calls.append)
    try:proof=migration.OriginalRead(service.store).read(source)
    finally:conn.set_trace_callback(None)
    assert proof['reason']=='unit_source_roles_unproven:unit_over_budget'
    assert not any('SELECT * FROM organization_units' in s or 'source_end,text,source_ranges' in s for s in calls)


def test_overbudget_legacy_apply_does_not_read_body_through_persist(service,monkeypatch):
    source,_,_=fixture(service,[{'role':'assistant','content':'small method'},{'role':'user','content':'padding '*1000}],selected=[0])
    from evolvmem import capability_role_proof as roles
    monkeypatch.setattr(roles,'MAX_SOURCE_BYTES',1000)
    service.store._capability_provenance_cache=None
    conn=service.store._connection();calls=[];conn.set_trace_callback(calls.append)
    try:
        prepared=migration.build_plan(conn,rule_revision=service.knowledge().rules.read()['revision'])
        result=migration.apply_plan(service,prepared,provider=lambda _:pytest.fail('budget block must not call model'))
    finally:conn.set_trace_callback(None)
    assert not any('SELECT body' in s or 'SELECT messages,' in s for s in calls)
    assert result['applied']==0
    assert prepared['blocked'] and not prepared['entries']


def test_span_count_and_total_cached_span_limits_are_explicit(service,monkeypatch):
    source,_,_=fixture(service,[{'role':'assistant','content':'one'},{'role':'user','content':'two'}])
    from evolvmem import capability_role_proof as roles
    monkeypatch.setattr(roles,'MAX_SPANS',1)
    proof=migration.OriginalRead(service.store).read(source)
    assert not proof['ready'] and proof['reason']=='unit_source_roles_unproven:span_count_over_budget'
    monkeypatch.setattr(roles,'MAX_SPANS',8192)
    monkeypatch.setattr(roles,'MAX_CACHED_SPANS',1)
    reader=migration.OriginalRead(service.store)
    assert reader.read(source)['ready']
    assert not reader.roles.cache


def test_budget_rechecked_for_previously_prepared_entry_before_lease(service,monkeypatch):
    fixture(service,[{'role':'assistant','content':'small'},{'role':'user','content':'padding '*1000}],selected=[0])
    conn=service.store._connection()
    prepared=migration.build_plan(conn,rule_revision=service.knowledge().rules.read()['revision'])
    assert prepared['entries']
    from evolvmem import capability_role_proof as roles
    monkeypatch.setattr(roles,'MAX_SOURCE_BYTES',1000)
    with pytest.raises(ValueError,match='assessment_source_role_budget_blocked'):
        migration.apply_plan(service,prepared,provider=lambda _:pytest.fail('budget block must not call model'))
    assert conn.execute('SELECT count(*) FROM capability_assessments').fetchone()[0]==0
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name=?",(migration.AUDIT,)).fetchone()


def test_independent_overbudget_origin_blocks_whole_memory_without_guessing_preservation(service,monkeypatch):
    from dataclasses import fields
    source,_,_=fixture(service,[{'role':'assistant','content':'small independent method'}])
    # A distinct native source and identity, not a replacement current version.
    archive=SessionArchiver(service.config,service.store).archive_session('', 'kimi','other-origin',json.dumps({'messages':[{'role':'user','content':'large '*1000}]}))
    task=ct.enqueue(service,[f'archive:{archive.id}'])['items'][0];task=org._task_row(service,task['id'])
    text=task['source_snapshot'];worker=org.OrganizationWorker(service.config,mode=ContextMode.SHADOW)
    worker._store_units(service,task,[{'source_start':0,'source_end':len(text),'text':text,'title':'other','category':'reference','evidence_quote':text,'project_hint':''}])
    conn=service.store._connection();unit=dict(conn.execute('SELECT * FROM organization_units WHERE task_id=?',(task['id'],)).fetchone())
    other=CapabilitySourceDraft(source_kind='unit',source_key=task['source_key']+'#'+unit['digest'],
      source_ref=f"{task['source_key']}#{unit['digest']}@0-{len(text)}",source_revision=task['source_revision'],
      task_id=task['id'],unit_digest=unit['digest'],unit_revision=unit['revision'],projects=())
    CapabilityStore(service.store).bind_source(source['memory_id'],other)
    from evolvmem import capability_role_proof as roles
    monkeypatch.setattr(roles,'MAX_SOURCE_BYTES',1000)
    prepared=migration.build_plan(conn,rule_revision=service.knowledge().rules.read()['revision'])
    assert not prepared['entries'] and len(prepared['blocked'])==2
    assert prepared['summary']['role_budget_blocked_memories']==1


def test_closed_scope_does_not_keep_rolled_back_savepoint_results(service):
    source,archive,_=fixture(service,[{'role':'assistant','content':'stable'}])
    conn=service.store._connection();reader=migration.OriginalRead(service.store)
    conn.execute('BEGIN');conn.execute('SAVEPOINT before_change')
    conn.execute("UPDATE conversation_history SET content_hash='changed' WHERE archive_id=?",(archive.id,))
    assert not reader.read_many([source])[0]['ready']
    conn.execute('ROLLBACK TO before_change')
    assert reader.read_many([source])[0]['ready']
    assert not reader.roles.cache
    conn.rollback()


def test_closed_scope_rejects_mutation_and_clears_cache(service,monkeypatch):
    source,archive,_=fixture(service,[{'role':'assistant','content':'stable'}])
    reader=migration.OriginalRead(service.store);original=reader._read;conn=service.store._connection()
    def interleaved(row):
        proof=original(row)
        conn.execute("UPDATE conversation_history SET content_hash='changed' WHERE archive_id=?",(archive.id,))
        return proof
    monkeypatch.setattr(reader,'_read',interleaved)
    with pytest.raises(ValueError,match='unit_source_roles_scope_changed'):
        reader.read_many([source])
    assert not reader.roles.cache and not reader._scope_active and not conn.in_transaction


def test_scope_rejects_external_iterator_before_reading(service):
    reader=migration.OriginalRead(service.store)
    def arbitrary():
        pytest.fail('must not iterate arbitrary code inside read scope')
        yield None
    with pytest.raises(TypeError,match='materialized'):
        reader.read_many(arbitrary())


def test_old_empty_database_needs_neither_registry_nor_policy():
    from evolvmem.capability_assessment import _rows
    from evolvmem.capability_provenance import ReadStore
    conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
    try:assert _rows(ReadStore(conn))==[]
    finally:conn.close()


def test_unlocatable_policy_is_unit_failure_not_scope_exception(service):
    source,_,_=fixture(service,[{'role':'assistant','content':'stable'}])
    from evolvmem.capability_provenance import ReadStore
    conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
    service.store._connection().backup(conn)
    try:
        proof=migration.OriginalRead(ReadStore(conn)).read(source)
        assert not proof['ready'] and proof['reason']=='unit_source_roles_unproven:policy_unproven'
        assert not conn.in_transaction
    finally:conn.close()

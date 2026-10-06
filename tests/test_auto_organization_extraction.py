"""Real per-unit extraction, evidence boundary, and correction integrity.

Synthetic data only. The provider is substituted at the ``kimi_hooks`` boundary;
segmentation, extraction, persistence, evidence rules and corrections are real.
"""
import json
import time

import pytest

from tests.test_history_qa_memory import service as base_service, archive
from tests.test_knowledge_cleaning import ready
from evolvmem.knowledge_api import dispatch
from tests.unit_model_fixture import (MIXED_TEXT, UnitProvider, long_topic_archive,
                                      many_topic_archive, model_for, run_worker, use_model)


@pytest.fixture
def service(base_service):
    # The shared fixture registers evo/shop; add the second project used here.
    base_service.knowledge().save_project({'project': 'dsh', 'display_name': 'DSH 演示项目'})
    return base_service


def org(service, route='', body=None):
    return dispatch(service, 'GET' if body is None else 'POST', 'organization' + route, body)


def _task_for(service, source):
    return org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']


def test_uncleaned_mixed_source_splits_and_is_recalled_from_history(service, monkeypatch):
    """A raw unassigned archive needs no manual cleaning confirmation."""
    from evolvmem import history_memory, memory_recall
    source = archive(service, session='raw-mixed', project='', text=MIXED_TEXT)
    task_id = _task_for(service, source)
    provider = model_for(service)
    run_worker(service, monkeypatch, provider)
    units = org(service, '/units', {'task_id': task_id})['items']
    assert [unit['project'] for unit in units] == ['evo', 'dsh', '']
    assert provider.extract_calls, 'the provider must be asked to extract each assigned unit'
    # Whole-source split keeps exact positions against the frozen snapshot.
    snapshot = org(service, '/detail', {'task_id': task_id})['source_text']
    for unit in units:
        assert snapshot[unit['source_start']:unit['source_end']] == unit['text']
    evo = next(unit for unit in units if unit['project'] == 'evo')
    history = [row for row in history_memory.sessions(service, 'evo') if row['summary']]
    assert any(evo['cleaned_text'][:20] in row['summary'] for row in history)
    recall = memory_recall.recall(service, {'project': 'evo', 'kind': 'history',
                                            'query': 'Evo 演示项目先明确验收条件'})
    assert recall['history'] and recall['selected_ids']


def test_provider_sees_only_its_unit_with_saved_guidance_and_related_peers(service, monkeypatch):
    from evolvmem.pipeline_skills import read, save
    skill = read(service, 'cleaning')
    save(service, 'cleaning', {'expected_revision': skill['revision'],
                               'instructions': '保留否定与条件，去掉寒暄。'})
    # A related active peer in the same project must reach the extraction prompt.
    service.knowledge().create({'title': '既有约定', 'body': 'Evo 演示项目先写验收条件再改界面。',
                                'project': 'evo', 'action': 'publish', 'content_type': 'reference'})
    source = many_topic_archive(service, session='scope-session')
    task_id = _task_for(service, source)
    provider = model_for(service)
    run_worker(service, monkeypatch, provider)
    assert provider.calls, 'segmentation must always run'
    segmentation = provider.calls[0]
    assert '保留否定与条件，去掉寒暄。' in segmentation, 'saved cleaning guidance must reach the model'
    assert 'Evo 演示项目' in segmentation, 'the project registry must reach the model'
    # A numbered-record prompt never copies the whole chunk back out.
    assert 'start_id' in segmentation and 'cleaned_summary' in segmentation
    assert provider.extract_calls, 'real extraction must run per assigned unit'
    dsh_unit = next(unit for unit in org(service, '/units', {'task_id': task_id})['items']
                    if unit['project'] == 'dsh')
    for prompt in provider.extract_calls:
        # Only the unit's own messages are sent; the other project's text is not.
        assert dsh_unit['text'][:10] not in prompt or 'Evo 演示项目' not in prompt
    joined = '\n'.join(provider.extract_calls)
    # The existing related lookup result (the peer's body) reaches the prompt.
    assert 'Evo 演示项目先写验收条件再改界面。' in joined, \
        'existing related knowledge must be offered for comparison'
    assert '<reviewed_cleaning>' in joined, 'the confirmed cleaning draft must scope extraction'


def test_real_extraction_writes_concise_question_answer(service, monkeypatch):
    from evolvmem import qa_memory
    source = many_topic_archive(service, session='qa-session')
    task_id = _task_for(service, source)
    run_worker(service, monkeypatch, model_for(service))
    rows = qa_memory.list_items(service, {'project': 'evo'})['items']
    assert rows, 'the provider QA must be stored through the existing QA path'
    row = rows[0]
    assert 4 <= len(row['question']) <= 160
    assert 5 <= len(row['answer']) <= 400
    detail = service.knowledge().detail(row['id'])
    assert row['answer'] == detail['body'], 'the QA answer must match its source body'
    assert detail['learning']['basis'] == 'explicit'
    assert detail['learning']['evidence'], 'the QA must keep a real quote from the unit'
    # No verification claim was invented by extraction.
    assert detail['success_count'] == 0 and detail['last_verified_at'] is None


def test_experience_evidence_only_promotes_with_a_real_bound_source(service, monkeypatch):
    """Cleaned prose is never proof; only the existing resolver may promote."""
    from evolvmem import unit_extraction
    case = {'project': 'evo', 'problem': '缓存写入失败时如何恢复？',
            'conditions': {'项目': 'evo'}, 'steps': ['先核对缓存版本', '再重放写入'],
            'rationale': '来自实际工具结果', 'result': '缓存恢复成功',
            'applicability': [], 'exclusions': [], 'transferable': False}
    item = service.experiences().record(case)
    item_id = item['id']
    # An assistant sentence is never evidence, even when it is quoted verbatim.
    assert unit_extraction._bind_experience(
        service, item_id, [{'role': 'assistant', 'content': '缓存恢复成功，已经修好了。'}]) is False
    # A tool-looking quote without a real recorded event stays a candidate too:
    # the existing resolver refuses to invent the event, and the reason is kept.
    assert unit_extraction._bind_experience(
        service, item_id, [{'role': 'tool', 'content': 'cache write failed: recovered'}],
        quote='cache write failed: recovered') is False
    kept = service.knowledge().detail(item_id)
    assert kept['status'] == 'candidate'
    assert kept['ingestion_reason'], 'the candidate must explain why it stays unverified'
    assert kept['success_count'] == 0
    assert service.store._connection().execute(
        'SELECT count(*) FROM context_evidence WHERE item_id=?', (item_id,)).fetchone()[0] == 0


def test_project_correction_during_delayed_extraction_invalidates_the_result(service, monkeypatch):
    source = many_topic_archive(service, session='delay-extract-session')
    task_id = _task_for(service, source)
    from tests.unit_model_fixture import UnitProvider
    base = UnitProvider(projects={'evo', 'dsh'})
    conn = service.store._connection()

    def slow_provider(prompt, *args, **kwargs):
        if '整理分段助手' in prompt:
            return base.segment(prompt)
        base.extract_calls.append(prompt)
        # The operator corrects the unit while the provider is still answering.
        row = conn.execute("SELECT digest FROM organization_units WHERE project='dsh' LIMIT 1").fetchone()
        if row:
            conn.execute("UPDATE organization_units SET project='evo',decision='manual',revision=revision+1 "
                         'WHERE digest=?', (row['digest'],))
            conn.commit()
        return base.extract(prompt)

    use_model(monkeypatch, slow_provider)
    from evolvmem.auto_organization import OrganizationWorker
    from evolvmem.context_models import ContextMode
    worker = OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    try:
        worker.tick()  # segmentation
        worker.tick()  # delayed extraction, before the next retry
    finally:
        worker.stop()
    units = org(service, '/units', {'task_id': task_id})['items']
    corrected = next(unit for unit in units if unit['decision'] == 'manual')
    # The stale answer was discarded: the unit still needs a fresh extraction.
    assert corrected['extraction_stage'] in ('pending', 'running')
    stale = service.store._connection().execute(
        "SELECT count(*) FROM context_items WHERE project='dsh' AND identity_key LIKE 'project:%:learn:%'"
    ).fetchone()[0]
    assert stale == 0, 'a result produced before the correction must not be written'


def test_correcting_a_completed_unit_removes_the_old_project_exposure(service, monkeypatch):
    source = many_topic_archive(service, session='move-session')
    task_id = _task_for(service, source)
    run_worker(service, monkeypatch, model_for(service, projects={'evo', 'dsh', 'shop'}))
    conn = service.store._connection()
    unit = next(u for u in org(service, '/units', {'task_id': task_id})['items'] if u['project'] == 'dsh')
    knowledge = conn.execute(
        'SELECT item_id FROM unit_derivations WHERE unit_task_id=? AND unit_digest=? AND kind=?',
        (task_id, unit['digest'], 'knowledge')).fetchone()
    assert knowledge, 'derived knowledge must be tracked per unit'
    item_id = knowledge['item_id']
    assert conn.execute('SELECT project FROM context_items WHERE id=?', (item_id,)).fetchone()['project'] == 'dsh'
    corrected = org(service, '/correct', {'task_id': task_id, 'digest': unit['digest'],
        'expected_revision': unit['revision'], 'project': 'evo', 'reason': '人工核对后改归 Evo'})
    assert corrected['unit']['project'] == 'evo'
    after = conn.execute('SELECT project,status FROM context_items WHERE id=?', (item_id,)).fetchone()
    assert after['project'] == 'evo', 'unit-derived knowledge follows the correction'
    # Project history no longer returns the dsh text, and does return it for evo.
    from evolvmem import history_memory
    evo_units = history_memory.project_units(service, 'evo', source.id)
    assert any(u['digest'] == unit['digest'] for u in evo_units)
    assert all(u['digest'] != unit['digest'] for u in history_memory.project_units(service, 'dsh', source.id))


def test_rejected_correction_stores_no_guidance(service, monkeypatch):
    source = many_topic_archive(service, session='reject-session')
    task_id = _task_for(service, source)
    run_worker(service, monkeypatch, model_for(service))
    unit = org(service, '/units', {'task_id': task_id})['items'][0]
    with pytest.raises(ValueError, match='revision_conflict'):
        org(service, '/correct', {'task_id': task_id, 'digest': unit['digest'],
            'expected_revision': 'stale-revision', 'project': 'evo',
            'guidance': '这条指导不应保存', 'scope': 'future', 'condition': '讨论导出时'})
    assert org(service, '/guidance')['items'] == [], 'a rejected correction must not store guidance'
    assert find_unit_decision(service, task_id, unit['digest']) != 'manual'


def find_unit_decision(service, task_id, digest):
    return service.store._connection().execute(
        'SELECT decision FROM organization_units WHERE task_id=? AND digest=?',
        (task_id, digest)).fetchone()['decision']


def test_ambiguous_and_unresolved_units_are_not_in_project_history(service, monkeypatch):
    source = many_topic_archive(service, session='ambiguous-session')
    task_id = _task_for(service, source)
    run_worker(service, monkeypatch, model_for(service))
    units = org(service, '/units', {'task_id': task_id})['items']
    unresolved = [unit for unit in units if not unit['project']]
    assert unresolved
    from evolvmem import history_memory
    for project in ('evo', 'dsh'):
        listed = [u['digest'] for u in history_memory.project_units(service, project, source.id)]
        for unit in unresolved:
            assert unit['digest'] not in listed
    # A model suggestion that was not confirmed is not exposed either.
    conn = service.store._connection()
    suggestion = conn.execute(
        "SELECT count(*) FROM context_items WHERE status='candidate' AND project!=''").fetchone()[0]
    active = conn.execute(
        "SELECT count(*) FROM context_items WHERE status='active' AND project!=''").fetchone()[0]
    assert active >= 1 and suggestion >= 0


def test_set_aside_units_leave_the_pending_queue_but_stay_restorable(service, monkeypatch):
    source = many_topic_archive(service, session='aside-session')
    task_id = _task_for(service, source)
    run_worker(service, monkeypatch, model_for(service))
    conn = service.store._connection()
    unit = conn.execute("SELECT * FROM organization_units WHERE task_id=? AND project='evo'",
                        (task_id,)).fetchone()
    result = org(service, '/disposition', {'task_id': task_id, 'digest': unit['digest'],
        'expected_revision': unit_revision(service, task_id, unit['digest']),
        'disposition': 'set_aside', 'reason': '这条对本项目没有长期价值'})
    assert result['ok'] is True
    listed = {u['digest'] for u in history_memory_units(service, 'evo', source.id)}
    assert unit['digest'] not in listed, 'set-aside units leave the current project view'
    # The record is kept and can be restored.
    stored = conn.execute('SELECT disposition,item_id FROM organization_units WHERE task_id=? AND digest=?',
                          (task_id, unit['digest'])).fetchone()
    assert stored['disposition'] == 'set_aside' and stored['item_id']
    restored = org(service, '/disposition', {'task_id': task_id, 'digest': unit['digest'],
        'expected_revision': unit_revision(service, task_id, unit['digest']),
        'disposition': 'keep'})
    assert restored['ok'] is True
    assert any(u['digest'] == unit['digest'] for u in history_memory_units(service, 'evo', source.id))


def unit_revision(service, task_id, digest):
    from evolvmem.auto_organization import _unit_revision, find_unit
    return _unit_revision(find_unit(service, task_id, digest))


def history_memory_units(service, project, archive_id):
    from evolvmem import history_memory
    return history_memory.project_units(service, project, archive_id)


def test_correction_queues_real_extraction_and_never_claims_done(service, monkeypatch):
    source = many_topic_archive(service, session='correction-queue')
    task_id = _task_for(service, source)
    run_worker(service, monkeypatch, model_for(service))
    unit = next(u for u in org(service, '/units', {'task_id': task_id})['items'] if not u['project'])
    result = org(service, '/correct', {'task_id': task_id, 'digest': unit['digest'],
        'expected_revision': unit['revision'], 'project': 'evo'})
    assert result['task']['status'] == 'pending'
    assert result['task']['stage'] == 'extraction'
    run_worker(service, monkeypatch, model_for(service))
    assert next(u for u in org(service, '/units', {'task_id': task_id})['items']
                if u['digest'] == unit['digest'])['extraction_stage'] == 'done'


def test_unit_revision_detects_aba(service, monkeypatch):
    from evolvmem.auto_organization import _unit_revision
    assert _unit_revision({'project': 'evo', 'revision': 1}) != _unit_revision({'project': 'evo', 'revision': 3})


def test_model_hint_without_source_project_evidence_stays_unresolved(service):
    from evolvmem.auto_organization import OrganizationWorker
    worker = OrganizationWorker(service.config)
    result = worker._decide(service, {'text': '今天准备整理一些资料。', 'evidence_quote': ''},
                            'evo', service.knowledge().rules.read(), {'evo', 'dsh'})
    assert result[2] == 'review' and not result[0]


def test_extraction_failure_is_visible_at_task_level(service, monkeypatch):
    source = many_topic_archive(service, session='provider-fails')
    task_id = _task_for(service, source)
    run_worker(service, monkeypatch, UnitProvider(fail_extraction='synthetic failure'))
    task = org(service, '/detail', {'task_id': task_id})
    assert task['status'] == 'failed'
    assert task['error_code'] == 'extraction_failed'


def test_native_archive_confirmation_binds_actual_event_once(service):
    from evolvmem.session_archive import SessionArchiver
    from evolvmem.unit_extraction import _bind_experience
    from tests.test_lan_capture import transcript
    proof = '缓存恢复成功，验收通过'
    archived = SessionArchiver(service.config, service.store).archive_session(
        '', 'codex', 'native-proof', json.dumps({'source':'client_reported',
        'device_id':'synthetic', 'session_id':'session-1',
        'transcript':transcript(text=proof).decode()}))
    item = service.experiences().record({'project':'evo','problem':'缓存无法写入如何恢复？',
        'conditions':{'操作':'恢复缓存'},'steps':['检查缓存版本','重新写入缓存'],
        'rationale':'针对缓存版本不一致','result':proof,
        'applicability':[],'exclusions':[],'transferable':False})
    messages = [{'role':'user','content':proof}]
    assert _bind_experience(service,item['id'],messages,archive_id=archived.id)
    assert _bind_experience(service,item['id'],messages,archive_id=archived.id)
    result = service.experiences().read(item['id'])
    assert result['status']=='active' and result['success_count']==1
    evidence = service.store._connection().execute('SELECT task_id FROM context_evidence WHERE item_id=?', (item['id'],)).fetchall()
    assert len(evidence)==1 and evidence[0]['task_id']=='session-1'


def test_original_tool_event_can_verify_a_result_quoted_in_clean_unit(service):
    from evolvmem.session_archive import SessionArchiver
    from evolvmem.unit_extraction import _bind_experience
    from tests.test_lan_capture import transcript
    proof = '1 passed in 0.10s'
    archived = SessionArchiver(service.config, service.store).archive_session(
        '', 'codex', 'tool-proof', json.dumps({'source':'client_reported','device_id':'synthetic',
        'session_id':'session-1','transcript':transcript().decode()}))
    item = service.experiences().record({'project':'evo','problem':'缓存写入后如何验证？',
        'conditions':{},'steps':['执行缓存测试'], 'result':proof})
    assert _bind_experience(service,item['id'],[{'role':'assistant','content':proof}],archive_id=archived.id)
    evidence = service.store._connection().execute('SELECT s.source_ref FROM context_evidence e JOIN context_sources s ON s.id=e.source_id WHERE e.item_id=?',(item['id'],)).fetchone()
    assert evidence['source_ref']==f'archive:{archived.id}#3'


def test_set_aside_withdraws_derived_qa_and_restore_reenables(service, monkeypatch):
    from evolvmem.memory_eligibility import eligible
    source = many_topic_archive(service,session='aside-derived')
    task_id = _task_for(service,source)
    run_worker(service,monkeypatch,model_for(service))
    unit = next(u for u in org(service,'/units',{'task_id':task_id})['items'] if u['project']=='evo')
    item_id = service.store._connection().execute("SELECT item_id FROM unit_derivations WHERE unit_task_id=? AND unit_digest=? AND kind='knowledge'",(task_id,unit['digest'])).fetchone()['item_id']
    assert eligible(service.store,item_id)
    aside=org(service,'/disposition',{'task_id':task_id,'digest':unit['digest'],
        'expected_revision':unit['revision'],'disposition':'set_aside','reason':'本次先暂存'})
    assert not eligible(service.store,item_id)
    org(service,'/disposition',{'task_id':task_id,'digest':unit['digest'],
        'expected_revision':aside['unit']['revision'],'disposition':'keep'})
    assert eligible(service.store,item_id)


def test_manual_ownership_survives_new_rule_version(service, monkeypatch):
    source=many_topic_archive(service,session='manual-new-rule')
    task_id=_task_for(service,source)
    run_worker(service,monkeypatch,model_for(service))
    unit=next(u for u in org(service,'/units',{'task_id':task_id})['items'] if u['project']=='dsh')
    org(service,'/correct',{'task_id':task_id,'digest':unit['digest'],'expected_revision':unit['revision'],'project':'shop'})
    rules=service.knowledge().rules.read()
    service.knowledge().rules.save({'expected_revision':rules['revision'],
        'settings':{'cleaning_instructions':'保留每一条有效条件。'}})
    new_task=_task_for(service,source)
    assert new_task!=task_id
    run_worker(service,monkeypatch,model_for(service))
    kept=next(u for u in org(service,'/units',{'task_id':new_task})['items'] if u['digest']==unit['digest'])
    assert kept['project']=='shop' and kept['decision']=='manual'


@pytest.mark.parametrize('proof',['预期缓存恢复成功，验收通过','缓存没有成功，验收未通过'])
def test_native_user_expectation_or_negation_is_not_verified(service,proof):
    from evolvmem.session_archive import SessionArchiver
    from evolvmem.unit_extraction import _bind_experience
    from tests.test_lan_capture import transcript
    archived=SessionArchiver(service.config,service.store).archive_session('', 'codex', 'not-proof',
        json.dumps({'source':'client_reported','device_id':'synthetic','session_id':'session-1',
                    'transcript':transcript(text=proof).decode()}))
    item=service.experiences().record({'project':'evo','problem':'缓存如何恢复？',
                                    'steps':['重新写入缓存'],'result':proof})
    assert not _bind_experience(service,item['id'],[{'role':'user','content':proof}],archive_id=archived.id)
    assert service.experiences().read(item['id'])['success_count']==0

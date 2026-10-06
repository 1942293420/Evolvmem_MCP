"""Automatic whole-source organization: persistent tasks, exact spans, scoped guidance.

All data here is synthetic. The model provider is substituted; HTTP parsing,
storage, coverage validation and evidence rules stay real.
"""
import json
import os
import re
import time

import pytest

from tests.test_history_qa_memory import service as base_service, archive
from tests.test_knowledge_cleaning import ready
from evolvmem.knowledge_api import dispatch


@pytest.fixture
def service(base_service):
    # The shared fixture registers evo/shop; add the second project used here.
    base_service.knowledge().save_project({'project': 'dsh', 'display_name': 'DSH 演示项目'})
    return base_service


def org(service, route='', body=None):
    return dispatch(service, 'GET' if body is None else 'POST', 'organization' + route, body)


from tests.unit_model_fixture import (  # noqa: E402  (shared real-contract provider)
    MIXED_TEXT, UnitProvider, long_topic_archive, many_topic_archive, model_for, run_worker, use_model)


def test_multi_project_source_keeps_middle_units_and_exact_spans(service, monkeypatch):
    source = many_topic_archive(service)
    result = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})
    assert result['created'] == 1 and result['items'][0]['status'] == 'pending'
    task_id = result['items'][0]['id']
    tasks = run_worker(service, monkeypatch, model_for(service, projects={'evo', 'dsh'}))
    task = next(t for t in tasks if t['id'] == task_id)
    assert task['status'] == 'review'
    units = org(service, '/units', {'task_id': task_id})['items']
    assert [u['project'] for u in units] == ['evo', 'dsh', '']
    detail = org(service, '/detail', {'task_id': task_id})
    stored = service.store._connection().execute(
        'SELECT text,source_start,source_end,project FROM organization_units WHERE task_id=? ORDER BY ordinal', (task_id,)).fetchall()
    original = detail['source_text']
    for row in stored:
        assert original[row['source_start']:row['source_end']] == row['text']
    # The middle unit is not decoration: its decision and body survive.
    assert any(u['project'] == 'dsh' and '不能只看开头结尾' in u['text'] for u in units)
    assert any('中间这段' in u['text'] for u in units)
    # One incomplete unit is grouped for review with its own decision.
    assert all(u['decision'] in ('auto', 'review') for u in units)
    assert any(u['decision'] == 'review' for u in units)
    assert task['stage'] == 'done'


def test_item_source_extracts_the_real_body_with_unknown_author(service, monkeypatch):
    """An unstructured item has no author: offsets and role must stay real."""
    body = ('用户要求：Evo 演示项目先明确验收条件，最后确认批量删除必须逐条确认'
            '（完整尾句到此结束）。')
    item = service.knowledge().create({'title': '待整理资料', 'body': body,
                                       'scope': 'project', 'action': 'draft'})
    ready(service)
    model = model_for(service, projects={'evo', 'dsh'})
    task = org(service, '/tasks', {'items': [{'key': f'item:{item["id"]}'}]})['items'][0]
    tasks = run_worker(service, monkeypatch, model)
    task = next(t for t in tasks if t['id'] == task['id'])
    assert task['status'] == 'completed'
    assert model.extract_calls, 'the assigned unit must reach the real extraction stage'
    # The provider sees the complete body from offset zero, never a snapshot
    # whose transport prefix shifted the text, and never as the assistant.
    assert f'[unknown]: {body}' in model.extract_calls[0]
    records = service.store._connection().execute(
        'SELECT role,text FROM organization_units WHERE task_id=?', (task['id'],)).fetchall()
    assert records and {r['role'] for r in records} == {'unknown'}
    assert any(r['text'] == body for r in records)


def test_resegment_rebuilds_a_stale_snapshot_from_the_current_source(service, monkeypatch):
    """A runtime cleaning change keeps the archive revision but not the snapshot."""
    from evolvmem.auto_organization import source_snapshot
    source = many_topic_archive(service, session='stale-snapshot')
    task = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]
    current = source_snapshot(service, task['source_key'])[1]
    conn = service.store._connection()
    # An older release froze the snapshot before injected wrappers were cleaned:
    # the archive revision is unchanged while the runtime cleaning output differs.
    stale = '<recommended_plugins>\n[{"id":"plugin-alpha"}]\n</recommended_plugins>\n' + current
    conn.execute('UPDATE organization_tasks SET source_snapshot=? WHERE id=?', (stale, task['id']))
    conn.commit()
    resegmented = org(service, '/resegment', {'task_id': task['id']})
    assert resegmented['status'] == 'pending' and resegmented['stage'] == 'queued'
    detail = org(service, '/detail', {'task_id': task['id']})
    # The old derivation is no longer the current source and nothing injected remains.
    assert detail['source_text'] == current and 'recommended_plugins' not in detail['source_text']
    run_worker(service, monkeypatch, model_for(service, projects={'evo', 'dsh'}))
    units = org(service, '/units', {'task_id': task['id']})['items']
    assert units, 'resegmentation must produce units'
    for unit in units:
        assert current[unit['source_start']:unit['source_end']] == unit['text']
    assert any('不能只看开头结尾' in u['text'] for u in units), 'the middle content must survive'


def test_resegment_refuses_a_stale_task_instead_of_overwriting_decisions(service):
    source = many_topic_archive(service, session='resegment-stale')
    task = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]
    rules = service.knowledge().rules.read()
    service.knowledge().rules.save({'expected_revision': rules['revision'],
                                    'instructions': rules['instructions'] + '\n新增归属说明。'})
    with pytest.raises(ValueError, match='revision_conflict'):
        org(service, '/resegment', {'task_id': task['id']})
    # The stale task is not silently resegmented; re-enqueueing is the guided path.
    assert org(service)['items'][0]['id'] == task['id']
    assert org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['created'] == 1


def test_fabricated_quote_is_rejected_and_source_is_not_ingested(service, monkeypatch):
    source = many_topic_archive(service, session='fabricated-session')
    task_id = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']

    def liar(prompt, *args, **kwargs):
        if '整理分段助手' in prompt:
            text = prompt.split('完整正文：\n', 1)[1]
            return json.dumps({'units': [
                {'title': '捏造', 'body': '用户从未说过这句话。', 'category': 'reference',
                 'start': 0, 'end': 10, 'project_hint': 'evo'}]}, ensure_ascii=False)
        raise AssertionError('no assignment call may follow a failed segmentation')

    use_model(monkeypatch, liar)
    from evolvmem.auto_organization import OrganizationWorker
    from evolvmem.context_models import ContextMode
    worker = OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    worker.start()
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            task = next(t for t in worker.tasks() if t['id'] == task_id)
            if task['status'] in ('review', 'failed', 'completed'):
                break
            time.sleep(.05)
    finally:
        worker.stop()
    assert task['status'] == 'review'
    assert 'coverage' in task['error_code'] or 'quote' in task['error_code']
    assert service.store._connection().execute(
        "SELECT count(*) FROM context_items WHERE identity_key LIKE 'organization:%'").fetchone()[0] == 0


def test_enqueue_is_idempotent_per_source_revision_and_rule_revision(service):
    source = many_topic_archive(service, session='idempotent-session')
    first = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})
    again = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})
    assert again['created'] == 0 and again['duplicates'] == 1
    assert again['items'][0]['id'] == first['items'][0]['id']
    # A rule edit produces a new task; the old task is not reported as current.
    rules = service.knowledge().rules.read()
    service.knowledge().rules.save({'expected_revision': rules['revision'],
                                    'instructions': rules['instructions'] + '\n新增归属说明。'})
    changed = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})
    assert changed['created'] == 1
    assert changed['items'][0]['id'] != first['items'][0]['id']
    assert org(service)['current_task_ids'] == [changed['items'][0]['id']]


def test_interrupted_task_is_restored_and_retry_is_explicit(service, monkeypatch):
    from evolvmem.auto_organization import OrganizationWorker
    from evolvmem.context_models import ContextMode
    source = many_topic_archive(service, session='interrupted-session')
    task_id = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']
    conn = service.store._connection()
    conn.execute("UPDATE organization_tasks SET status='running',stage='segmentation',attempts=1 WHERE id=?", (task_id,))
    conn.commit()
    use_model(monkeypatch, model_for(service, projects={'evo', 'dsh'}))
    worker = OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            task = next(t for t in worker.tasks() if t['id'] == task_id)
            if task['status'] in ('completed', 'review', 'failed'):
                break
            time.sleep(.05)
    finally:
        worker.stop()
    assert task['status'] in ('completed', 'review')
    assert task['attempts'] >= 2
    # Explicit retry is idempotent and never duplicates ingested units.
    units = org(service, '/units', {'task_id': task_id})['items']
    retried = org(service, '/retry', {'task_id': task_id})
    assert retried['ok'] is True and retried['status'] == 'pending'
    run_worker(service, monkeypatch, model_for(service, projects={'evo', 'dsh'}))
    assert len(org(service, '/units', {'task_id': task_id})['items']) == len(units)
    assert service.store._connection().execute(
        "SELECT count(*) FROM context_items WHERE identity_key LIKE 'organization:%'").fetchone()[0] <= len(units)


def test_human_correction_defaults_to_this_batch_and_future_guidance_is_scoped(service, monkeypatch):
    source = many_topic_archive(service, session='correction-session')
    task_id = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']
    run_worker(service, monkeypatch, model_for(service, projects={'evo', 'dsh'}))
    units = org(service, '/units', {'task_id': task_id})['items']
    unassigned = next(u for u in units if not u['project'])
    corrected = org(service, '/correct', {'task_id': task_id, 'digest': unassigned['digest'],
        'expected_revision': unassigned['revision'], 'project': 'evo',
        'guidance': '批量删除确认属于 Evo 演示项目', 'scope': 'batch'})
    assert corrected['ok'] is True
    assert service.store._connection().execute(
        "SELECT count(*) FROM organization_guidance WHERE enabled=1").fetchone()[0] == 1
    future = org(service, '/correct', {'task_id': task_id, 'digest': unassigned['digest'],
        'expected_revision': corrected['unit']['revision'], 'project': 'evo',
        'guidance': '批量删除确认属于 Evo 演示项目', 'scope': 'future',
        'condition': '讨论批量删除时', 'exceptions': '仅当项目未登记时'})
    assert future['ok'] is True
    guidance = org(service, '/guidance')['items']
    reused = next(row for row in guidance if row['scope'] == 'future')
    assert reused['condition'] == '讨论批量删除时' and reused['exceptions'] == '仅当项目未登记时'
    assert reused['source_text'] == '批量删除确认属于 Evo 演示项目'
    assert reused['enabled'] == 1 and reused['project'] == 'evo'
    assert any(row['scope'] == 'batch' for row in guidance)
    # A negative example marks a matching unit as review rather than reusing it.
    negative = org(service, '/correct', {'task_id': task_id, 'digest': unassigned['digest'],
        'expected_revision': future['unit']['revision'], 'project': 'evo',
        'guidance': '不要用于 DSH 项目', 'scope': 'future', 'negative': True})
    assert negative['unit']['decision'] == 'review'
    assert '反例' in negative['unit']['reason'] or '不要' in negative['unit']['reason']
    # Disable stops future application; the batch correction is unaffected.
    disabled = org(service, '/guidance', {'id': reused['id'], 'enabled': False})
    assert disabled['items'][0]['enabled'] == 0
    assert all(row['enabled'] == 0 for row in org(service, '/guidance')['items'] if row['id'] == reused['id'])


def test_guidance_applies_to_future_matching_units_only(service, monkeypatch):
    from evolvmem import organization_guidance as guidance_store
    conn = service.store._connection()
    conn.execute("INSERT INTO organization_guidance(scope,project,condition,exceptions,guidance,state,negative,enabled,revision,source_text,created_at,updated_at) "
                 "VALUES('future','evo','讨论导出时','','导出限制按 Evo 处理','reusable',0,1,1,'导出限制按 Evo 处理','2026-10-07 00:00:00','2026-10-07 00:00:00')")
    conn.commit()
    applied = guidance_store.classify(service, {'text': '讨论导出时需要保留来源版本', 'project_hint': ''})
    assert applied['status'] == 'apply' and applied['project'] == 'evo'
    assert guidance_store.classify(service, {'text': '完全不相关的界面文案'})['status'] == 'none'
    # An exception present in the input excludes the rule and forces review.
    conn.execute("UPDATE organization_guidance SET exceptions='来源版本'")
    conn.commit()
    blocked = guidance_store.classify(service, {'text': '讨论导出时需要保留来源版本'})
    assert blocked['status'] == 'review' and blocked['negative'] == 1
    # A rule without a narrow condition stays a pending suggestion, never applied.
    conn.execute("UPDATE organization_guidance SET exceptions='',condition='资料'")
    conn.commit()
    pending = guidance_store.classify(service, {'text': '这是另一个项目的资料'})
    assert pending['status'] == 'pending' and 'suggestion' in pending

def test_assigned_unit_is_visible_in_project_history_with_provenance(service, monkeypatch):
    from evolvmem import history_memory, memory_recall
    source = many_topic_archive(service, session='history-session')
    task_id = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']
    run_worker(service, monkeypatch, model_for(service, projects={'evo', 'dsh'}))
    units = org(service, '/units', {'task_id': task_id})['items']
    evo_unit = next(u for u in units if u['project'] == 'evo')
    dsh_unit = next(u for u in units if u['project'] == 'dsh')
    # Project history is not empty: the unit reaches the project's session list.
    listed = [row for row in history_memory.sessions(service, 'evo') if row['summary']]
    assert listed, 'assigned units must appear in the project history listing'
    assert any(evo_unit['text'] in row['summary'] for row in listed)
    assert all(dsh_unit['text'] not in row['summary'] for row in listed)
    # Recall selects the unit and points at conversation_read.
    recall = memory_recall.recall(service, {'project': 'evo', 'query': 'Evo 演示项目先明确验收条件',
                                            'kind': 'history'})
    assert recall['history'], 'history recall must select the organized unit'
    assert any(evo_unit['text'] in row['summary'] for row in recall['history'])
    assert recall['selected_ids'], 'recall must expose the source item ids'
    # conversation_read returns only the requested project's units, with ranges.
    evo_read = history_memory.read(service, 'evo', source.id)
    assert [unit['source_start'] for unit in evo_read['units']] == [evo_unit['source_start']]
    assert evo_read['units'][0]['source_end'] == evo_unit['source_end']
    assert evo_unit['text'] in evo_read['text'] and dsh_unit['text'] not in evo_read['text']
    dsh_read = history_memory.read(service, 'dsh', source.id)
    assert [unit['text'] for unit in dsh_read['units']] == [dsh_unit['text']]
    with pytest.raises(ValueError, match='conversation_not_in_project'):
        history_memory.read(service, 'shop', source.id)
    # Range-exact provenance: every item carries the archive and its own position.
    conn = service.store._connection()
    provenance = conn.execute(
        "SELECT s.source_ref,s.archive_id,i.project FROM context_sources s JOIN context_items i ON i.id=s.item_id "
        "WHERE s.source_kind='organization' AND i.project='evo'").fetchall()
    assert provenance and all(row['archive_id'] == source.id for row in provenance)
    assert any(f'@{evo_unit["source_start"]}-{evo_unit["source_end"]}' in row['source_ref']
               for row in provenance)
    document = dispatch(service, 'GET', 'project-memory', {'project': 'evo'})
    assert evo_unit['text'] in document['body']

def test_generated_candidates_are_never_marked_verified(service, monkeypatch):
    source = many_topic_archive(service, session='evidence-session')
    task_id = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']
    run_worker(service, monkeypatch, model_for(service, projects={'evo', 'dsh'}))
    conn = service.store._connection()
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM context_items WHERE project!='' AND identity_key LIKE 'project:%'")]
    assert ids
    assert conn.execute('SELECT count(*) FROM context_evidence WHERE item_id IN (%s)'
                        % ','.join('?' * len(ids)), ids).fetchone()[0] == 0
    for item_id in ids:
        row = conn.execute('SELECT success_count,failure_count,last_verified_at,confidence,content_type '
                           'FROM context_items WHERE id=?', (item_id,)).fetchone()
        assert row['success_count'] == 0 and row['failure_count'] == 0
        assert row['last_verified_at'] is None
        # A history record states what was discussed; knowledge never claims
        # near-certainty without verification evidence.
        if row['content_type'] != 'session_summary':
            assert row['confidence'] < .95
    # A knowledge candidate derived from assistant text is never admitted by the
    # QA boundary without a user quote.
    assistant_units = [u for u in org(service, '/units', {'task_id': task_id})['items']
                       if u['role'] == 'assistant']
    if assistant_units:
        qa_ids = {row[0] for row in conn.execute("SELECT item_id FROM knowledge_qa WHERE status='active'")}
        for unit in assistant_units:
            if unit['item_id']:
                assert unit['item_id'] not in qa_ids

def test_duplicate_knowledge_keeps_source_links_without_repeated_success(service, monkeypatch):
    """The same knowledge from two archives shares one item and gains two sources."""
    first = many_topic_archive(service, session='dup-a')
    org(service, '/tasks', {'items': [{'key': f'archive:{first.id}'}]})
    run_worker(service, monkeypatch, model_for(service, projects={'evo', 'dsh'}))
    from evolvmem.session_archive import SessionArchiver
    second = SessionArchiver(service.config, service.store).archive_session('', 'kimi', 'dup-b', json.dumps({'messages': [
        {'role': 'user', 'content': '用户要求：Evo 演示项目先明确验收条件。\n'
                                    '中间这段是 DSH 项目导出的要求，不能只看开头结尾。\n'
                                    '最后用户确认：批量删除必须逐条确认。'}]}, ensure_ascii=False))
    ready(service)
    second_task = org(service, '/tasks', {'items': [{'key': f'archive:{second.id}'}]})['items'][0]['id']
    run_worker(service, monkeypatch, model_for(service, projects={'evo', 'dsh'}))
    conn = service.store._connection()
    # Knowledge items exclude the per-source history summaries: exactly one
    # knowledge item per topic survives both archives.
    knowledge = conn.execute(
        "SELECT id,success_count FROM context_items WHERE identity_key LIKE 'project:%:learn:%' "
        "AND project='evo'").fetchall()
    assert len(knowledge) == 1, 'the duplicate must share one knowledge item'
    shared_id = knowledge[0]['id']
    assert knowledge[0]['success_count'] == 0
    archives = {row[0] for row in conn.execute(
        'SELECT DISTINCT archive_id FROM context_sources WHERE item_id=? AND archive_id IS NOT NULL',
        (shared_id,))}
    assert {first.id, second.id} <= archives, 'both archives must stay linked to the shared item'
    assert conn.execute('SELECT count(*) FROM context_evidence WHERE item_id=?',
                        (shared_id,)).fetchone()[0] == 0
    refs = {row[0] for row in conn.execute(
        "SELECT source_kind FROM context_sources WHERE item_id=?", (shared_id,))}
    assert 'session' in refs
    units = org(service, '/units', {'task_id': second_task})['items']
    assert all(unit['item_id'] for unit in units if unit['project'])

def test_unit_mixing_two_projects_without_proof_waits_for_review(service, monkeypatch):
    source = many_topic_archive(service, session='mixed-session')
    task_id = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']
    # Only the first and last lines start a unit, so the DSH sentence stays inside
    # the Evo unit: two project names without a per-project quote must wait.
    provider = model_for(service, topics=[('用户要求', 'evo', 'reference'),
                                          ('最后用户确认', '', 'habit')])
    tasks = run_worker(service, monkeypatch, provider)
    task = next(t for t in tasks if t['id'] == task_id)
    units = org(service, '/units', {'task_id': task_id})['items']
    mixed = units[0]
    assert mixed['decision'] == 'review' and mixed['project'] == ''
    assert '人工' in mixed['reason'] or '冲突' in mixed['reason']
    assert task['status'] == 'review'
    assert mixed['item_id'] is None

def test_segmentation_prompt_merges_one_tasks_advice_and_keeps_a_real_switch():
    """Prompt contract for a synthetic mixed dialogue: no provider is called.

    The real-model verdict stays with the manual re-run; this test only pins the
    guidance the program hands over. One task's consecutive advice must stay one
    unit even though later lines drop the project name, a short insertion about
    another project must stay its own unit, and coverage, verbatim evidence and
    the review rule must not be relaxed.
    """
    from evolvmem.topic_segmentation import message_spans, prompt, record_windows
    messages = [{'role': 'user', 'content': 'Evo 演示项目这次的任务是整理流程，下面是连续七条改进建议。'}]
    messages += [{'role': 'assistant', 'content': f'改进建议{i}：这里只写做法，正文不再重复项目名。'}
                 for i in range(1, 8)]
    messages.append({'role': 'user', 'content': '另外 DSH 演示项目的导出必须保留来源版本。'})
    messages.append({'role': 'user', 'content': '回到 Evo 演示项目：结论是不要按每条建议机械拆开。'})
    records = record_windows(message_spans(messages))
    # The program never pre-merges: the model still sees every advice separately.
    assert len(records) == len(messages)
    rendered = prompt(records, 1, 1, projects=('evo', 'dsh'))
    for position in range(1, len(records) + 1):
        assert f'[{position}] ' in rendered, 'full numbered coverage must reach the model'
    # Same project and same task: continuous advice, conditions and conclusion are one unit.
    assert '同一项目' in rendered and '合并成一个整理单元' in rendered
    assert '不按每条建议' in rendered and '不按消息条数' in rendered
    assert '真正切换' in rendered
    # A short insertion about another project is not swallowed by its neighbour.
    assert '短插入' in rendered and '独立成单元' in rendered
    # Ownership is never inherited or inferred: only a directly named project counts.
    assert '不继承上一单元的项目' in rendered and '不推断' in rendered
    assert 'project_hint（仅当正文直接点名' in rendered
    # The existing validation rules stay exact.
    assert '不重叠、不遗漏、不跳号' in rendered
    assert '逐字' in rendered and 'review' in rendered


def test_worker_segments_long_source_in_bounded_chunks_and_keeps_middle(service, monkeypatch):
    source = long_topic_archive(service, session='long-session')
    task_id = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']
    source_text = org(service, '/detail', {'task_id': task_id})['source_text']
    provider = model_for(service, topics=[('开头要求', 'evo', 'reference'),
                                          ('中段要求', 'dsh', 'reference'),
                                          ('收尾要求', '', 'habit')])
    tasks = run_worker(service, monkeypatch, provider, timeout=30)
    task = next(t for t in tasks if t['id'] == task_id)
    assert task['status'] in ('completed', 'review'), (task['error_code'], task['error_detail'])
    units = org(service, '/units', {'task_id': task_id})['items']
    # The whole source is processed in bounded ordered calls, never first/last only.
    assert len(provider.calls) >= 2, 'a long source must be handled in ordered chunks'
    assert any('中段要求' in unit['text'] for unit in units)
    assert any('开头要求' in unit['text'] for unit in units)
    assert any('收尾要求' in unit['text'] for unit in units)
    for unit in units:
        assert source_text[unit['source_start']:unit['source_end']] == unit['text']
    assert [unit['source_start'] for unit in units] == sorted(unit['source_start'] for unit in units)
    assert task['unit_count'] == len(units) >= 3
    assert sum(len(unit['text']) for unit in units) <= len(source_text)

def test_middle_gap_is_detected_instead_of_classifying_first_and_last_only(service, monkeypatch):
    source = many_topic_archive(service, session='gap-session')
    task_id = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']
    record_id = re.compile(r'^\[(\d+)\] ', re.M)

    def skips_the_middle(prompt, *args, **kwargs):
        if '整理分段助手' not in prompt:
            raise AssertionError('extraction must not run after a coverage failure')
        records = [int(pos) for pos in record_id.findall(prompt)]
        assert len(records) >= 3, 'the fixture must provide at least three records'
        return json.dumps({'units': [
            {'start_id': records[0], 'end_id': records[0], 'title': '开头', 'cleaned_summary': '开头',
             'category': 'reference', 'project_hint': 'evo', 'evidence_quote': '用户要求',
             'disposition': 'keep', 'disposition_reason': ''},
            {'start_id': records[-1], 'end_id': records[-1], 'title': '结尾', 'cleaned_summary': '结尾',
             'category': 'habit', 'project_hint': '', 'evidence_quote': '',
             'disposition': 'keep', 'disposition_reason': ''}]}, ensure_ascii=False)

    use_model(monkeypatch, skips_the_middle)
    from evolvmem.auto_organization import OrganizationWorker
    from evolvmem.context_models import ContextMode
    worker = OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            task = next(t for t in worker.tasks() if t['id'] == task_id)
            if task['status'] in ('review', 'failed', 'completed'):
                break
            time.sleep(.05)
    finally:
        worker.stop()
    assert task['status'] == 'review' and task['error_code'] == 'coverage_coverage_gap'
    assert org(service, '/units', {'task_id': task_id})['items'] == []
    assert service.store._connection().execute(
        "SELECT count(*) FROM context_items WHERE identity_key LIKE 'project:%'").fetchone()[0] == 0

def test_human_decision_is_not_overwritten_by_a_stale_result(service, monkeypatch):
    source = many_topic_archive(service, session='manual-wins-session')
    task_id = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']
    run_worker(service, monkeypatch, model_for(service, projects={'evo', 'dsh'}))
    unit = next(u for u in org(service, '/units', {'task_id': task_id})['items'] if u['project'] == 'dsh')
    corrected = org(service, '/correct', {'task_id': task_id, 'digest': unit['digest'],
        'expected_revision': unit['revision'], 'project': 'shop', 'reason': '人工核对后改为 shop'})
    assert corrected['unit']['project'] == 'shop' and corrected['unit']['decision'] == 'manual'
    # Re-running the same task never rewrites the human decision.
    org(service, '/retry', {'task_id': task_id})
    run_worker(service, monkeypatch, model_for(service, projects={'evo', 'dsh'}))
    after = next(u for u in org(service, '/units', {'task_id': task_id})['items'] if u['digest'] == unit['digest'])
    assert after['project'] == 'shop' and after['decision'] == 'manual'


def test_failed_task_is_retried_explicitly_and_never_deletes(service, monkeypatch):
    source = many_topic_archive(service, session='retry-session')
    task_id = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']
    use_model(monkeypatch, lambda *a, **k: (_ for _ in ()).throw(RuntimeError('provider down')))
    from evolvmem.auto_organization import OrganizationWorker
    from evolvmem.context_models import ContextMode
    worker = OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    worker.start()
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            task = next(t for t in worker.tasks() if t['id'] == task_id)
            if task['status'] == 'failed' and task['attempts'] >= 3:
                break
            time.sleep(.05)
    finally:
        worker.stop()
    assert task['status'] == 'failed' and task['attempts'] >= 3
    assert task['error_code'] == 'organization_failed'
    # Nothing was deleted, purged or ingested by the failure.
    assert service.store.get_session_archive(source.id)['state'] == 'available'
    assert service.store._connection().execute(
        "SELECT count(*) FROM context_items WHERE identity_key LIKE 'organization:%'").fetchone()[0] == 0
    # The pending task remains visible and the source is not purged or deleted.
    assert any(t['id'] == task_id for t in org(service)['items'])
    assert service.store._connection().execute(
        "SELECT count(*) FROM knowledge_cleaning_reviews WHERE source_key=? AND state='deleted'",
        (f'archive:{source.id}',)).fetchone()[0] == 0
    retried = org(service, '/retry', {'task_id': task_id})
    assert retried['status'] == 'pending'
    tasks = run_worker(service, monkeypatch, model_for(service, projects={'evo', 'dsh'}))
    assert next(t for t in tasks if t['id'] == task_id)['status'] in ('completed', 'review')


def test_batch_correction_keeps_independent_outcomes(service, monkeypatch):
    source = many_topic_archive(service, session='batch-correct-session')
    task_id = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']
    run_worker(service, monkeypatch, model_for(service, projects={'evo', 'dsh'}))
    units = org(service, '/units', {'task_id': task_id})['items']
    first, second = units[0], units[1]
    result = org(service, '/correct', {'task_id': task_id, 'items': [
        {'digest': first['digest'], 'expected_revision': first['revision'], 'project': 'shop',
         'guidance': '第一条按 shop 处理', 'scope': 'batch'},
        {'digest': second['digest'], 'expected_revision': 'stale-revision', 'project': 'shop',
         'guidance': '这一条不应生效', 'scope': 'batch'}]})
    assert (result['succeeded'], result['failed']) == (1, 1)
    assert result['items'][1]['error'] == 'revision_conflict'
    rows = {unit['digest']: unit for unit in org(service, '/units', {'task_id': task_id})['items']}
    assert rows[first['digest']]['project'] == 'shop' and rows[first['digest']]['decision'] == 'manual'
    assert rows[second['digest']]['decision'] != 'manual'
    guidance = org(service, '/guidance')['items']
    assert [row['guidance'] for row in guidance] == ['第一条按 shop 处理']


def test_http_endpoints_expose_task_queue_and_actions(service, monkeypatch):
    import threading
    from http.server import HTTPServer
    import urllib.request
    from evolvmem.web_server import make_handler
    source = many_topic_archive(service, session='http-session')
    holder, ready_event = {}, threading.Event()

    def serve():
        srv = HTTPServer(('127.0.0.1', 0), make_handler(service))
        holder['srv'] = srv
        ready_event.set()
        srv.serve_forever()
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    ready_event.wait(timeout=5)
    base = f"http://127.0.0.1:{holder['srv'].server_address[1]}"
    try:
        request = urllib.request.Request(base + '/api/knowledge/organization/tasks',
            data=json.dumps({'items': [{'key': f'archive:{source.id}'}]}).encode(),
            headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request, timeout=5) as response:
            payload = json.loads(response.read())
        assert payload['created'] == 1
        with urllib.request.urlopen(base + '/api/knowledge/organization', timeout=5) as response:
            listed = json.loads(response.read())
        assert listed['items'] and listed['items'][0]['id'] == payload['items'][0]['id']
    finally:
        holder['srv'].shutdown()
        holder['srv'].server_close()
        thread.join(timeout=5)


def test_correcting_an_unassigned_unit_ingests_it_and_updates_counts(service, monkeypatch):
    source = many_topic_archive(service, session='correct-ingest-session')
    task_id = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']
    run_worker(service, monkeypatch, model_for(service, projects={'evo', 'dsh'}))
    before = org(service, '/units', {'task_id': task_id})['items']
    unassigned = next(u for u in before if not u['project'])
    assert unassigned['item_id'] is None
    corrected = org(service, '/correct', {'task_id': task_id, 'digest': unassigned['digest'],
        'expected_revision': unassigned['revision'], 'project': 'evo',
        'guidance': '这条按 Evo 处理', 'scope': 'batch'})
    assert corrected['ok'] is True
    assert corrected['unit']['item_id'], 'a corrected unit must be written, not left empty'
    assert corrected['unit']['project'] == 'evo' and corrected['unit']['decision'] == 'manual'
    # The task state reflects the new resolution instead of staying stale.
    task = corrected['task']
    assert task['review_count'] <= len([u for u in before if not u['project']])
    conn = service.store._connection()
    item = conn.execute('SELECT project,scope,status FROM context_items WHERE id=?',
                        (corrected['unit']['item_id'],)).fetchone()
    assert item['project'] == 'evo' and item['scope'] == 'project'
    assert item['status'] == 'active', 'confirmed history must not stay a candidate'
    # It is reachable from the project history now.
    from evolvmem import history_memory
    assert any(unassigned['cleaned_text'][:20] in row['summary']
               for row in history_memory.sessions(service, 'evo') if row['summary'])


def test_empty_project_correction_never_becomes_global(service, monkeypatch):
    source = many_topic_archive(service, session='unresolve-session')
    task_id = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']
    run_worker(service, monkeypatch, model_for(service, projects={'evo', 'dsh'}))
    unit = next(u for u in org(service, '/units', {'task_id': task_id})['items'] if u['project'] == 'evo')
    corrected = org(service, '/correct', {'task_id': task_id, 'digest': unit['digest'],
        'expected_revision': unit['revision'], 'project': '', 'reason': '归属未确认，先撤回'})
    assert corrected['unit']['project'] == '' and corrected['unit']['decision'] == 'manual'
    conn = service.store._connection()
    assert corrected['unit']['item_id']
    row = conn.execute('SELECT project,scope,status FROM context_items WHERE id=?',
                       (corrected['unit']['item_id'],)).fetchone()
    assert row['project'] == '' and row['scope'] == 'project', 'empty project must not become global'
    assert row['status'] == 'candidate'
    resolution = conn.execute('SELECT resolution_state FROM context_project_resolutions WHERE item_id=?',
                              (corrected['unit']['item_id'],)).fetchone()
    assert resolution['resolution_state'] == 'unresolved'


def test_rule_revision_reuses_identity_and_supersedes_old_task(service, monkeypatch):
    source = many_topic_archive(service, session='rule-revision-session')
    first = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']
    run_worker(service, monkeypatch, model_for(service, projects={'evo', 'dsh'}))
    conn = service.store._connection()
    knowledge_before = {row[0] for row in conn.execute(
        "SELECT id FROM context_items WHERE identity_key LIKE 'project:%:learn:%'")}
    assert knowledge_before
    rules = service.knowledge().rules.read()
    service.knowledge().rules.save({'expected_revision': rules['revision'],
                                    'instructions': rules['instructions'] + '\n新增一条归属说明。'})
    second = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']
    assert second != first
    run_worker(service, monkeypatch, model_for(service, projects={'evo', 'dsh'}))
    knowledge_after = {row[0] for row in conn.execute(
        "SELECT id FROM context_items WHERE identity_key LIKE 'project:%:learn:%'")}
    # The same source ranges keep one durable identity: no duplicated knowledge.
    assert knowledge_after == knowledge_before
    assert org(service)['current_task_ids'] == [second]
    assert org(service, '/detail', {'task_id': first})['status'] == 'superseded'


def test_source_revision_change_during_model_call_is_not_written(service, monkeypatch):
    source = many_topic_archive(service, session='delay-revision-session')
    task_id = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']
    base = model_for(service, projects={'evo', 'dsh'})
    conn = service.store._connection()

    def slow_editor(prompt, *args, **kwargs):
        result = base(prompt, *args, **kwargs)
        # The operator edits the source while the provider is still answering.
        conn.execute('UPDATE conversation_history SET body=body||? WHERE archive_id=?',
                     ('\n用户：补充了一条新的要求。', source.id))
        conn.commit()
        return result

    use_model(monkeypatch, slow_editor)
    from evolvmem.auto_organization import OrganizationWorker
    from evolvmem.context_models import ContextMode
    worker = OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            task = next(t for t in worker.tasks() if t['id'] == task_id)
            if task['status'] in ('review', 'failed', 'completed'):
                break
            time.sleep(.05)
    finally:
        worker.stop()
    assert task['status'] == 'review' and task['error_code'] == 'revision_conflict'
    assert org(service, '/units', {'task_id': task_id})['items'] == []
    assert conn.execute("SELECT count(*) FROM context_items WHERE identity_key LIKE 'project:%'").fetchone()[0] == 0


def test_arrival_mode_queues_only_sources_after_activation(service, monkeypatch):
    from evolvmem.organization_arrival import discover_new, settings, update_settings
    old = many_topic_archive(service, session='arrival-old')
    enabled = update_settings(service, {'auto_new': True})
    assert enabled['auto_new'] == 1 and enabled['baseline_id'] >= old.id
    # The historical backlog stays explicit: discovery does not pick it up.
    first = discover_new(service)
    assert first['created'] == 0
    new_source = many_topic_archive(service, session='arrival-new')
    discovered = discover_new(service)
    assert discovered['created'] == 1
    assert discovered['items'][0]['source_key'] == f'archive:{new_source.id}'
    # The same source is never queued twice.
    assert discover_new(service)['created'] == 0
    tasks = run_worker(service, monkeypatch, model_for(service, projects={'evo', 'dsh'}))
    assert next(t for t in tasks if t['id'] == discovered['items'][0]['id'])['status'] in ('completed', 'review')
    # Pausing new arrivals leaves queued work alone.
    paused = update_settings(service, {'auto_new': False})
    assert paused['auto_new'] == 0 and settings(service)['auto_new'] == 0
    assert discover_new(service)['created'] == 0
    assert [t['id'] for t in org(service)['items']], 'queued work must survive pausing'


def test_backlog_is_bounded_and_reports_progress(service, monkeypatch):
    for index in range(3):
        many_topic_archive(service, session=f'backlog-{index}')
    result = org(service, '/backlog', {'limit': 2})
    assert result['created'] == 2 and result['queue_total'] == 3 and result['remaining'] == 1
    assert org(service)['total'] == 2
    rest = org(service, '/backlog', {'limit': 2})
    assert rest['created'] == 1 and rest['queue_total'] == 1 and rest['remaining'] == 0
    # Nothing is queued again once every source has a current task.
    assert org(service, '/backlog', {'limit': 2})['created'] == 0


def test_task_view_shows_a_readable_source_title_not_only_the_key(service):
    source = many_topic_archive(service, session='title-session')
    task = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]
    assert 'title-session' in task['source_title']
    assert task['source_title'] != task['source_key']
    assert '标题' not in task['source_title']

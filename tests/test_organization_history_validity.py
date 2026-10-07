"""Organization history validity: a removed/set-aside/moved unit must not keep
its summary in current project history, recall or the project document.

Synthetic data only; the provider is substituted at the kimi_hooks boundary.
The regression that motivated these cases is a re-segmentation that left the old
summary's ``organization_units`` row gone while its ``unit_derivations`` row and
active ``context_items`` row survived.
"""
import pytest

from tests.conftest import temp_dir, test_config  # noqa: F401  (fixture chain)
from tests.test_history_qa_memory import service as base_service  # noqa: F401  (fixture chain)
from tests.test_auto_organization_extraction import service, org, _task_for  # noqa: F401
from tests.unit_model_fixture import (UnitProvider, many_topic_archive, model_for,
                                      run_worker)
from evolvmem import memory_recall, project_memory

RECALL = {'project': 'evo', 'kind': 'history', 'query': '验收', 'max_chars': 8000}


def _history_ids(service, **overrides):
    options = {**RECALL, **overrides}
    return memory_recall.recall(service, options)['selected_ids']


def _document_ids(service, project='evo'):
    return project_memory.document(service, project)['source_ids']


def _qa_answers(service, project):
    from evolvmem import qa_memory
    return [r['answer'] for r in qa_memory.list_items(service, {'project': project})['items']]


def _qa_recall(service, project, query):
    return [r['answer'] for r in memory_recall.recall(
        service, {'project': project, 'query': query, 'kind': 'experience'})['qa']]


# --- 1) a re-segmentation replaces the source's summaries -------------------

def test_resegment_replaces_old_summaries(service, monkeypatch):
    src = many_topic_archive(service, session='history-resegment',
        text='甲段：Evo 演示项目先确认需求与验收。\n乙段：Evo 演示项目再进行发布核对。')
    tid = _task_for(service, src)
    old = UnitProvider(topics=[('甲段', 'evo', 'task_requirement'),
                               ('乙段', 'evo', 'task_requirement')])
    run_worker(service, monkeypatch, old)
    ids = {u['item_id'] for u in org(service, '/units', {'task_id': tid})['items']}
    assert len(ids) == 2
    org(service, '/resegment', {'task_id': tid})
    new = UnitProvider(topics=[('甲段', 'evo', 'task_requirement')])
    run_worker(service, monkeypatch, new)
    current = {u['item_id'] for u in org(service, '/units', {'task_id': tid})['items']}
    assert len(current) == 1 and not ids.intersection(current)

    recalled = _history_ids(service)
    assert not ids.intersection(recalled), 'history recall leaks removed segment summaries'
    assert current.issubset(recalled), 'the current segment summary must stay queryable'
    document = _document_ids(service)
    assert not ids.intersection(document), 'project document leaks removed segment summaries'
    assert current.issubset(document)


# --- 2) a set-aside unit leaves current history and comes back on restore ----

def test_set_aside_withdraws_history_and_restore_returns_it(service, monkeypatch):
    src = many_topic_archive(service, session='history-aside')
    tid = _task_for(service, src)
    run_worker(service, monkeypatch, model_for(service))
    unit = next(u for u in org(service, '/units', {'task_id': tid})['items'] if u['project'] == 'evo')
    item_id = unit['item_id']
    assert item_id in _history_ids(service)
    assert item_id in _document_ids(service)

    aside = org(service, '/disposition', {'task_id': tid, 'digest': unit['digest'],
        'expected_revision': unit['revision'], 'disposition': 'set_aside',
        'reason': '这条对本项目没有长期价值'})
    assert aside['ok'] is True
    assert item_id not in _history_ids(service), 'a set-aside summary must leave current history'
    assert item_id not in _document_ids(service)

    org(service, '/disposition', {'task_id': tid, 'digest': unit['digest'],
        'expected_revision': aside['unit']['revision'], 'disposition': 'keep'})
    assert item_id in _history_ids(service), 'restoring the unit restores its summary'
    assert item_id in _document_ids(service)


# --- 2b) a moved unit's summary leaves history even when its old derivation
#         still records the old project (the derivation fast path must not win)

@pytest.mark.parametrize('target', ['shop', ''])
def test_moved_unit_with_unchanged_derivation_leaves_current_history(service, monkeypatch, target):
    # Two units keep the archive listed for evo after one of them is moved, so
    # the summary is decided by the validity filter, not dropped with the archive.
    src = many_topic_archive(service, session='history-manual-move',
        text='甲段：Evo 演示项目先确认需求与验收。\n乙段：Evo 演示项目再进行发布核对。')
    tid = _task_for(service, src)
    run_worker(service, monkeypatch, UnitProvider(
        topics=[('甲段', 'evo', 'task_requirement'), ('乙段', 'evo', 'task_requirement')]))
    conn = service.store._connection()
    units = [u for u in org(service, '/units', {'task_id': tid})['items'] if u['project'] == 'evo']
    assert len(units) == 2
    moved, kept = units[0], units[1]
    assert moved['item_id'] in _history_ids(service)
    # A manual decision changes the unit's project while the old derivation and
    # the summary row keep the old project (the item write did not happen).
    with service.store.transaction():
        conn.execute("UPDATE organization_units SET project=?,decision='manual' WHERE task_id=? AND digest=?",
                     (target, tid, moved['digest']))
    derivation = conn.execute(
        "SELECT project FROM unit_derivations WHERE unit_task_id=? AND unit_digest=? AND item_id=?",
        (tid, moved['digest'], moved['item_id'])).fetchone()
    assert derivation['project'] == 'evo', 'the stale derivation must stay unchanged for this regression'
    assert conn.execute('SELECT project FROM context_items WHERE id=?',
                        (moved['item_id'],)).fetchone()['project'] == 'evo'
    assert moved['item_id'] not in _history_ids(service), 'a moved unit must not keep its old summary current'
    assert moved['item_id'] not in _document_ids(service)
    # The other current unit of the same project is unaffected.
    assert kept['item_id'] in _history_ids(service)
    assert kept['item_id'] in _document_ids(service)


# --- 3) a summary backed by an independent source survives one withdrawal ----

def test_shared_summary_survives_one_source_withdrawal(service, monkeypatch):
    from tests.test_history_qa_memory import archive
    src = many_topic_archive(service, session='history-shared-a')
    tid = _task_for(service, src)
    run_worker(service, monkeypatch, model_for(service))
    unit = next(u for u in org(service, '/units', {'task_id': tid})['items'] if u['project'] == 'evo')
    shared_id = unit['item_id']
    assert shared_id in _history_ids(service)
    # A second, independent archive corroborates the same summary item, so the
    # item now has a source outside its own organization unit.
    other = archive(service, session='history-shared-b', project='evo')
    with service.store.transaction():
        service.store.record_session_source(
            shared_id, other.id, extraction_version='history-validity.v1')
    conn = service.store._connection()
    archives = {row[0] for row in conn.execute(
        'SELECT DISTINCT archive_id FROM context_sources WHERE item_id=? AND archive_id IS NOT NULL',
        (shared_id,))}
    assert {src.id, other.id} <= archives, 'both archives must stay linked'

    aside = org(service, '/disposition', {'task_id': tid, 'digest': unit['digest'],
        'expected_revision': unit['revision'], 'disposition': 'set_aside', 'reason': '暂存一个来源'})
    assert aside['ok'] is True
    # The independent archive still backs the shared summary, so it stays current.
    assert shared_id in _history_ids(service)
    assert shared_id in _document_ids(service)



# --- 4) a correction keeps an active Q&A valid, promotes nothing, adds nothing -

def test_correction_keeps_active_qa_valid_without_promotion(service, monkeypatch):
    from evolvmem import qa_memory
    src = many_topic_archive(service, session='history-correct-qa')
    tid = _task_for(service, src)
    model = model_for(service, projects={'evo', 'dsh', 'shop'})
    run_worker(service, monkeypatch, model)
    conn = service.store._connection()
    unit = next(u for u in org(service, '/units', {'task_id': tid})['items'] if u['project'] == 'dsh')
    item_id = conn.execute(
        "SELECT item_id FROM unit_derivations WHERE unit_task_id=? AND unit_digest=? AND kind='knowledge'",
        (tid, unit['digest'])).fetchone()['item_id']
    body = service.knowledge().detail(item_id)['body']
    assert any(body in a for a in _qa_answers(service, 'dsh'))
    assert any(body in a for a in _qa_recall(service, 'dsh', body[:20]))

    def qa_states():
        return {r[0]: r[1] for r in conn.execute('SELECT item_id,status FROM knowledge_qa')}

    def knowledge_ids():
        return {r[0] for r in conn.execute("SELECT id FROM context_items WHERE identity_key LIKE 'project:%:learn:%'")}

    states_before, knowledge_before = qa_states(), knowledge_ids()
    origin_before = qa_memory.detail(service, item_id)['origin']
    # shop has no prior answer for this question, so the move keeps the Q&A valid.
    org(service, '/correct', {'task_id': tid, 'digest': unit['digest'],
        'expected_revision': unit['revision'], 'project': 'shop', 'reason': '人工核对后改归 Shop'})
    calls_before = len(model.extract_calls)
    run_worker(service, monkeypatch, model)
    assert len(model.extract_calls) > calls_before, 'the correction must queue a re-extraction'

    detail = qa_memory.detail(service, item_id)
    assert detail['status'] == 'active' and detail['effective'] is True, detail
    assert detail['origin'] == origin_before, 'the original origin must be preserved'
    assert any(body in a for a in _qa_recall(service, 'shop', body[:20])), 'new project must recall it'
    assert all(body not in a for a in _qa_recall(service, 'dsh', body[:20])), 'old project must not'
    # Nothing is promoted and the re-run adds no knowledge.
    assert qa_states() == states_before, 'a move must not promote or demote any Q&A'
    assert knowledge_ids() == knowledge_before, 're-running must not add knowledge'
    row = conn.execute('SELECT success_count,last_verified_at FROM context_items WHERE id=?',
                       (item_id,)).fetchone()
    assert row['success_count'] == 0 and row['last_verified_at'] is None


# --- 4b) an already-stale Q&A is never reactivated by the move ---------------

def test_stale_qa_is_not_reactivated_by_a_project_move(service, monkeypatch):
    from evolvmem import qa_memory
    src = many_topic_archive(service, session='history-stale-qa')
    tid = _task_for(service, src)
    model = model_for(service, projects={'evo', 'dsh', 'shop'})
    run_worker(service, monkeypatch, model)
    conn = service.store._connection()
    unit = next(u for u in org(service, '/units', {'task_id': tid})['items'] if u['project'] == 'dsh')
    item_id = conn.execute(
        "SELECT item_id FROM unit_derivations WHERE unit_task_id=? AND unit_digest=? AND kind='knowledge'",
        (tid, unit['digest'])).fetchone()['item_id']
    body = service.knowledge().detail(item_id)['body']
    # The stored answer was written under a different trigger, so it is stale
    # before any correction and must not be activated by the move.
    detail = service.knowledge().detail(item_id)
    service.learning().classify(item_id, {'expected_revision': detail['revision'],
                                          'category': detail['learning']['category'],
                                          'trigger': '变更后的适用条件'})
    assert qa_memory.detail(service, item_id)['status'] == 'stale'

    org(service, '/correct', {'task_id': tid, 'digest': unit['digest'],
        'expected_revision': unit['revision'], 'project': 'shop', 'reason': '人工核对后改归 Shop'})
    run_worker(service, monkeypatch, model)
    assert service.knowledge().detail(item_id)['project'] == 'shop'
    assert qa_memory.detail(service, item_id)['status'] != 'active'
    assert all(body not in a for a in _qa_recall(service, 'shop', body[:20]))


# --- 4c) a conflicting answer in the new project keeps both from being active --

def test_conflicting_answer_in_new_project_is_not_both_active(service, monkeypatch):
    from evolvmem import qa_memory
    src = many_topic_archive(service, session='history-conflict-qa')
    tid = _task_for(service, src)
    model = model_for(service, projects={'evo', 'dsh', 'shop'})
    run_worker(service, monkeypatch, model)
    conn = service.store._connection()
    unit = next(u for u in org(service, '/units', {'task_id': tid})['items'] if u['project'] == 'dsh')
    item_id = conn.execute(
        "SELECT item_id FROM unit_derivations WHERE unit_task_id=? AND unit_digest=? AND kind='knowledge'",
        (tid, unit['digest'])).fetchone()['item_id']
    body = service.knowledge().detail(item_id)['body']
    question = qa_memory.detail(service, item_id)['question']
    # The target project (shop) already carries the same question with another answer.
    conflicting = qa_memory.save(service, {
        'question': question, 'answer': '目标项目已有的另一个不同答案。',
        'project': 'shop', 'category': 'task_requirement', 'trigger': '', 'action': 'publish'})
    assert conflicting['status'] == 'active' and conflicting['effective'] is True

    org(service, '/correct', {'task_id': tid, 'digest': unit['digest'],
        'expected_revision': unit['revision'], 'project': 'shop', 'reason': '人工核对后改归 Shop'})
    run_worker(service, monkeypatch, model)
    assert qa_memory.detail(service, item_id)['status'] == 'candidate'
    active = conn.execute(
        "SELECT q.item_id,q.answer FROM knowledge_qa q JOIN context_items i ON i.id=q.item_id "
        "WHERE i.project='shop' AND q.status='active' AND q.question=?", (question,)).fetchall()
    answers = {r['answer'] for r in active}
    assert body not in answers, 'the conflicting moved answer must not stay active'
    assert len(answers) == 1, 'exactly the pre-existing answer stays active'
    assert all(body not in a for a in _qa_recall(service, 'shop', body[:20]))


# --- 5) a correction must not leave a duplicate history summary --------------

def test_correction_does_not_duplicate_history_summary(service, monkeypatch):
    src = many_topic_archive(service, session='history-correct-duplicate')
    tid = _task_for(service, src)
    model = model_for(service, projects={'evo', 'dsh', 'shop'})
    run_worker(service, monkeypatch, model)
    conn = service.store._connection()
    unit = next(u for u in org(service, '/units', {'task_id': tid})['items'] if u['project'] == 'dsh')
    org(service, '/correct', {'task_id': tid, 'digest': unit['digest'],
        'expected_revision': unit['revision'], 'project': 'evo', 'reason': '人工核对后改归 Evo'})
    run_worker(service, monkeypatch, model)
    # Storage may keep two summary rows for the same digest; the read path must
    # expose only the item the current unit points at.
    summaries = {r[0] for r in conn.execute(
        "SELECT id FROM context_items WHERE content_type='session_summary' AND project='evo'")}
    current = {u['item_id'] for u in org(service, '/units', {'task_id': tid})['items'] if u['project']}
    assert summaries - current, 'the duplicate summary row must exist for this regression'
    selected = [i for i in _history_ids(service) if i in summaries]
    assert current.issubset(set(selected)), (current, selected)
    assert not (summaries - current) & set(selected), 'orphan duplicate summary leaked into selected_ids'
    # The same text is never shown twice for one archive.
    for row in project_memory.document(service, 'evo')['sessions']:
        if row['summary']:
            parts = [p for p in row['summary'].split('\n') if p]
            assert len(parts) == len(set(parts)), row['summary']


# --- 6) shared knowledge is never moved by one source's correction -----------

def test_correction_does_not_move_shared_knowledge(service, monkeypatch):
    text = '用户要求：Evo 演示项目先明确验收条件。'
    first = many_topic_archive(service, session='history-shared-knowledge-a', text=text)
    second = many_topic_archive(service, session='history-shared-knowledge-b', text=text)
    tasks = org(service, '/tasks', {'items': [
        {'key': f'archive:{first.id}'}, {'key': f'archive:{second.id}'}]})['items']
    run_worker(service, monkeypatch, model_for(service, projects={'evo'}))
    conn = service.store._connection()
    shared = conn.execute(
        "SELECT id,project FROM context_items WHERE identity_key LIKE '%:learn:%'").fetchall()
    assert len(shared) == 1, 'identical knowledge must stay one shared entity'
    item_id = shared[0]['id']
    assert shared[0]['project'] == 'evo'
    unit = next(u for u in org(service, '/units', {'task_id': tasks[0]['id']})['items']
                if u['project'] == 'evo')

    org(service, '/correct', {'task_id': tasks[0]['id'], 'digest': unit['digest'],
        'expected_revision': unit['revision'], 'project': 'dsh', 'reason': '人工核对后改归 DSH'})
    after = conn.execute('SELECT project FROM context_items WHERE id=?', (item_id,)).fetchone()
    assert after['project'] == 'evo', 'shared knowledge must not follow one source'
    sources = {row[0] for row in conn.execute(
        'SELECT DISTINCT source_kind FROM context_sources WHERE item_id=?', (item_id,))}
    assert 'session' in sources


# --- 7) a verified experience that changes scope follows the derived-case rule
#         and never inherits its success count --------------------------------

def test_verified_experience_scope_change_does_not_inherit_success(service, monkeypatch):
    from evolvmem.auto_organization import _now_iso
    from evolvmem import unit_derivations
    src = many_topic_archive(service, session='history-experience-scope')
    tid = _task_for(service, src)
    run_worker(service, monkeypatch, model_for(service))
    conn = service.store._connection()
    unit = next(u for u in org(service, '/units', {'task_id': tid})['items'] if u['project'] == 'evo')
    # A unit-derived experience that is verified and active, as the read paths require.
    case = {'project': 'evo', 'problem': '缓存写入失败时如何恢复？', 'conditions': {'项目': 'evo'},
            'steps': ['先核对缓存版本', '再重放写入'], 'rationale': '来自实际工具结果',
            'result': '缓存恢复成功', 'applicability': [], 'exclusions': [], 'transferable': False}
    exp_id = service.experiences().record(case)['id']
    with service.store.transaction():
        conn.execute("UPDATE context_items SET status='active',success_count=1,failure_count=0,"
                     "last_verified_at=?,confidence=.9 WHERE id=?", (_now_iso(), exp_id))
    unit_derivations.record_derivation(service, tid, unit['digest'], exp_id,
                                       kind='knowledge', project='evo')
    unit = next(u for u in org(service, '/units', {'task_id': tid})['items'] if u['digest'] == unit['digest'])

    org(service, '/correct', {'task_id': tid, 'digest': unit['digest'],
        'expected_revision': unit['revision'], 'project': 'shop', 'reason': '人工核对后改归 Shop'})
    children = conn.execute(
        "SELECT id,project,success_count,status FROM context_items "
        "WHERE content_type='experience' AND project='shop'").fetchall()
    assert children, 'a scope change must derive a new case'
    child = children[0]
    assert child['success_count'] == 0, 'the derived case must not inherit verification'
    assert child['status'] == 'candidate', 'the derived case starts unverified'
    assert service.experiences()._payload(child['id'])['parent_experience_id'] == exp_id
    parent = conn.execute('SELECT status,success_count FROM context_items WHERE id=?',
                          (exp_id,)).fetchone()
    assert parent['success_count'] == 1, 'the original verification record is preserved'
    assert parent['status'] != 'active', 'the moved parent leaves current use'

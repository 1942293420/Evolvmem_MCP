"""A human archives knowledge once; transport and segmentation do not own it."""
import pytest

from tests.test_cross_batch_context import config, service, craft, SESSION, OTHER_SESSION
from evolvmem import auto_organization as org, conversation_tasks
from evolvmem.organization_review_decisions import archive_one
from evolvmem.topic_segmentation import message_spans


FACT = 'Demo 的运行环境固定为 Linux，执行器部署在 Windows。'


def material(service, *, session=SESSION, text=FACT, hint='', category='environment'):
    source = craft(service, session=session, start=1, end=4, text=text)
    task = org.enqueue(service, [f'archive:{source.id}'])['items'][0]
    rebuild(service, task['id'], hint=hint, category=category)
    return task, view(service, task['id'])[0]


def view(service, task_id):
    return org.unit_views(service, task_id)


def rebuild(service, task_id, *, split=False, hint='', category='environment', title='部署说明'):
    task = org._task_row(service, task_id)
    text = task['source_snapshot']
    spans = message_spans(org.source_messages(service, task['source_key']))
    ranges = [[s['start'], s['end']] for s in spans] if split else [[0, len(text)]]
    units = [
        {'source_start': a, 'source_end': b, 'source_ranges': [[a, b]], 'text': text[a:b],
         'title': title, 'cleaned_text': title + '：' + text[a:b],
         'category': category, 'project_hint': hint, 'evidence_quote': ''}
        for a, b in ranges
    ]
    org.OrganizationWorker(service.config)._store_units(service, task, units)
    return view(service, task_id)


def save(service, task_id, *, action='include', index=0, project='demo'):
    unit = view(service, task_id)[index]
    return archive_one(service, {
        'task_id': task_id, 'digest': unit['digest'], 'expected_revision': unit['revision'],
        'action': action, 'project': project,
    })['unit']


def ingest(service, task_id):
    worker = org.OrganizationWorker(service.config)
    worker._ingest(service, org.task_view(service, task_id), extract=False)


def test_split_and_merge_keep_human_decision_one_history_and_same_sources(service):
    task, _ = material(service)
    original = save(service, task['id'])
    item_id = original['item_id']
    before_sources = service.store.list_item_sources(item_id)
    split = rebuild(service, task['id'], split=True, title='换一种摘要表达')
    assert all(u['decision'] == 'manual' and u['project'] == 'demo' for u in split)
    ingest(service, task['id'])
    assert {u['item_id'] for u in view(service, task['id'])} == {item_id}
    from evolvmem.unit_derivations import currently_backed
    assert currently_backed(service.store, item_id)
    merged = rebuild(service, task['id'], title='再次合为完整资料')[0]
    assert merged['decision'] == 'manual' and merged['project'] == 'demo'
    ingest(service, task['id'])
    current = view(service, task['id'])[0]
    assert current['item_id'] == item_id
    assert current['knowledge_id'] == original['knowledge_id']
    assert service.store.list_item_sources(item_id) == before_sources
    assert currently_backed(service.store, item_id)


def test_deferred_material_stays_deferred_after_split_and_merge(service):
    task, _ = material(service)
    save(service, task['id'], action='defer')
    split = rebuild(service, task['id'], split=True)
    assert all(u['disposition'] == 'set_aside' for u in split)
    merged = rebuild(service, task['id'])[0]
    assert merged['disposition'] == 'set_aside'
    assert merged['item_id'] is None
    restored = save(service, task['id'])
    assert restored['decision'] == 'manual' and restored['project'] == 'demo'
    assert restored['item_id']


def test_deferred_unassigned_material_can_return_to_the_review_list(service):
    from evolvmem.organization_review import groups
    task, _ = material(service)
    deferred = save(service, task['id'], action='defer')
    restored = org.set_disposition(service, {
        'task_id': task['id'], 'digest': deferred['digest'],
        'expected_revision': deferred['revision'], 'disposition': 'keep'})['unit']
    assert restored['decision'] == 'review' and restored['project'] == ''
    assert groups(service, {'task_id': task['id']})['total'] == 1
    rebuilt = rebuild(service, task['id'])[0]
    assert rebuilt['decision'] == 'review' and rebuilt['disposition'] == 'keep'


def test_exact_project_scoped_fact_from_another_agent_adds_source_without_rearchiving(service):
    first, _ = material(service)
    original = save(service, first['id'])
    second, duplicate = material(service, session=OTHER_SESSION)
    assert duplicate['decision'] == 'manual' and duplicate['project'] == 'demo'
    ingest(service, second['id'])
    duplicate = view(service, second['id'])[0]
    assert duplicate['knowledge_id'] == original['knowledge_id']
    assert duplicate['item_id'] == original['item_id']
    sources = service.store.list_item_sources(original['item_id'])
    assert {s['archive_id'] for s in sources if s['archive_id'] is not None} == {
        int(first['source_key'].split(':')[1]), int(second['source_key'].split(':')[1])}
    before = len(sources)
    rebuild(service, second['id'], title='另一个 Agent 的摘要')
    ingest(service, second['id'])
    assert len(service.store.list_item_sources(original['item_id'])) == before
    assert view(service, second['id'])[0]['decision'] == 'manual'
    assert view(service, second['id'])[0]['project'] == 'demo'

def test_partial_confirmation_cannot_archive_an_entire_verbatim_copy(service):
    task, _ = material(service)
    parts = rebuild(service, task['id'], split=True)
    save(service, task['id'], index=0)
    _, other = material(service, session=OTHER_SESSION)
    assert other['decision'] == 'review' and other['item_id'] is None


def test_model_set_aside_is_not_recorded_as_a_human_decision(service):
    task, _ = material(service)
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE organization_units SET disposition='set_aside',disposition_reason='模型建议稍后处理' WHERE task_id=?",
            (task['id'],))
    rebuilt = rebuild(service, task['id'])[0]
    assert rebuilt['decision'] == 'review'
    assert rebuilt['disposition'] == 'keep'
    assert service.store._connection().execute('SELECT COUNT(*) FROM knowledge_decisions').fetchone()[0] == 0


def test_changing_one_split_piece_does_not_move_the_other_piece(service):
    task, _ = material(service)
    original = save(service, task['id'])
    split = rebuild(service, task['id'], split=True)
    moved = save(service, task['id'], index=0, project='other')
    units = view(service, task['id'])
    assert units[0]['project'] == 'other' and units[1]['project'] == 'demo'
    assert units[0]['knowledge_id'] != units[1]['knowledge_id']
    assert moved['item_id'] != original['item_id']
    assert service.knowledge().detail(moved['item_id'])['project'] == 'other'
    from evolvmem.unit_derivations import currently_backed
    assert not currently_backed(service.store, original['item_id'])
    merged = rebuild(service, task['id'])[0]
    assert merged['decision'] == 'review' and merged['item_id'] is None


def test_retired_legacy_aggregate_cannot_reappear_after_partial_correction(service):
    first, _ = material(service)
    save(service, first['id'])
    current = conversation_tasks.enqueue(service, [first['source_key']])['items'][0]
    rebuild(service, current['id'], split=True)
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE organization_tasks SET status='superseded' WHERE id=?", (first['id'],))
    save(service, current['id'], project='other', index=0)
    before = {u['digest']: u['knowledge_id'] for u in view(service, current['id'])}
    repeated = rebuild(service, current['id'], split=True)
    assert {u['digest']: u['knowledge_id'] for u in repeated} == before
    assert [u['project'] for u in repeated] == ['other', 'demo']
    old = view(service, first['id'])[0]
    retired = service.store._connection().execute(
        'SELECT retired FROM knowledge_objects WHERE knowledge_id=?', (old['knowledge_id'],)).fetchone()[0]
    assert retired == 1


def test_full_reassignment_updates_existing_aliases_and_survives_their_rebuild(service):
    first, _ = material(service)
    original = save(service, first['id'])
    second, _ = material(service, session=OTHER_SESSION)
    ingest(service, second['id'])
    changed = save(service, first['id'], project='other')
    assert changed['item_id'] == original['item_id']
    assert view(service, second['id'])[0]['project'] == 'other'
    repeated = rebuild(service, second['id'])[0]
    assert repeated['decision'] == 'manual' and repeated['project'] == 'other'
    assert repeated['item_id'] == original['item_id']


@pytest.mark.parametrize('text,hint,category', [
    ('运行环境固定为 Linux，执行器部署在 Windows。', '', 'environment'),
    (FACT, 'other', 'environment'),
    ('在 Demo 增加写入开关。', '', 'task_requirement'),
])
def test_same_words_do_not_reassign_unscoped_conflicting_or_new_task_sources(service, text, hint, category):
    first, _ = material(service, text=text, category=category)
    save(service, first['id'])
    _, current = material(service, session=OTHER_SESSION, text=text, hint=hint, category=category)
    assert current['decision'] == 'review' and current['project'] == ''
    assert current['item_id'] is None


def test_new_requirement_is_not_covered_by_old_knowledge_identity(service):
    first, _ = material(service)
    saved = save(service, first['id'])
    additional = craft(service, session=SESSION, start=5, end=8,
                       text='新增要求：Other 项目也需要运行这套执行器。')
    task = conversation_tasks.enqueue(service, [first['source_key'], f'archive:{additional.id}'])['items'][0]
    rebuild(service, task['id'])
    conversation_tasks.protect_prior_decisions(service, org._task_row(service, task['id']))
    unit = view(service, task['id'])[0]
    assert unit['decision'] == 'review' and unit['project'] == ''
    assert unit['item_id'] is None
    assert service.knowledge().detail(saved['item_id'])['project'] == 'demo'


def test_changed_original_snapshot_cannot_approve_a_duplicate_from_another_source(service):
    first, _ = material(service)
    save(service, first['id'])
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE organization_tasks SET source_snapshot=source_snapshot||' changed' WHERE id=?", (first['id'],))
    _, duplicate = material(service, session=OTHER_SESSION)
    assert duplicate['decision'] == 'review' and not duplicate['project']
    assert duplicate['item_id'] is None


def test_duplicate_imported_material_keeps_both_non_archive_sources(service):
    tasks = []
    for title in ('导入的部署资料', '另一份部署资料'):
        source = service.knowledge().create({'title': title, 'body': FACT,
            'scope': 'project', 'action': 'draft', 'content_type': 'reference'})
        task = org.enqueue(service, [f"item:{source['id']}"])['items'][0]
        rebuild(service, task['id'])
        tasks.append(task)
        if len(tasks) == 1:
            original = save(service, task['id'])
        ingest(service, task['id'])
    duplicate = view(service, tasks[1]['id'])[0]
    assert duplicate['item_id'] == original['item_id']
    sources = service.store.list_item_sources(original['item_id'])
    assert {s['source_ref'].split('#')[0] for s in sources if s['source_kind'] == 'organization'} == {
        t['source_key'] for t in tasks}


def test_legacy_history_is_reused_when_a_new_summary_is_written(service):
    from evolvmem import unit_extraction
    from evolvmem.legacy_models import LegacyExtractionItem, LegacyExtractionRequest
    task, _ = material(service)
    original = save(service, task['id'])
    # Existing installations have a history item but no canonical summary payload.
    with service.store.transaction():
        service.store._connection().execute("UPDATE knowledge_objects SET history_json='{}'")
        service.store._connection().execute(
            "UPDATE organization_units SET cleaned_text='模型把摘要换了个说法' WHERE task_id=?", (task['id'],))
    unit = view(service, task['id'])[0]
    draft = LegacyExtractionRequest(summary=LegacyExtractionItem(
        key='SESSION_SUMMARY', value='模型新写的会话摘要，只应作为提炼锚点。'))
    request = unit_extraction._with_history_summary(service, draft, task, unit)
    result = service.persist_legacy_extraction(request)
    assert result.persisted == 0
    assert service.store._connection().execute(
        "SELECT COUNT(*) FROM context_items WHERE content_type='session_summary'").fetchone()[0] == 1
    assert service.knowledge().detail(original['item_id'])['status'] == 'active'


def test_failed_save_rolls_back_knowledge_decision_and_sources(service, monkeypatch):
    task, _ = material(service)
    original = org._apply_manual_project
    def fail_after_real_write(*args, **kwargs):
        original(*args, **kwargs)
        raise ValueError('simulated_write_failure')
    monkeypatch.setattr(org, '_apply_manual_project', fail_after_real_write)
    with pytest.raises(ValueError, match='simulated_write_failure'):
        save(service, task['id'])
    current = view(service, task['id'])[0]
    assert current['decision'] == 'review' and current['item_id'] is None
    conn = service.store._connection()
    assert conn.execute('SELECT COUNT(*) FROM knowledge_decisions').fetchone()[0] == 0
    assert conn.execute('SELECT COUNT(*) FROM context_sources').fetchone()[0] == 0
    assert conn.execute('SELECT decision_json FROM knowledge_objects').fetchone()[0] == '{}'


@pytest.mark.parametrize('another_source', [False, True])
def test_legacy_manual_records_keep_their_existing_id_without_reprocessing(service, another_source):
    first, _ = material(service)
    original = save(service, first['id'])
    # Simulate a pre-upgrade database: the original manual unit and history
    # exist, but the new identity tables have never seen them.
    with service.store.transaction():
        conn = service.store._connection()
        conn.execute("UPDATE organization_units SET knowledge_id=''")
        for table in ('knowledge_decisions', 'knowledge_source_bindings', 'knowledge_objects'):
            conn.execute('DELETE FROM '+table)
    if another_source:
        second, _ = material(service, session=OTHER_SESSION)
        tid = second['id']
    else:
        tid = first['id']
        rebuild(service, tid, split=True)
    ingest(service, tid)
    assert all(u['decision'] == 'manual' and u['project'] == 'demo' for u in view(service, tid))
    assert {u['item_id'] for u in view(service, tid)} == {original['item_id']}
    assert service.store._connection().execute(
        "SELECT COUNT(*) FROM context_items WHERE content_type='session_summary'").fetchone()[0] == 1

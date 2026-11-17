"""Only an explicit human save assigns a project; AI output stays a suggestion."""
import pytest
from evolvmem import knowledge_scope as ks
from tests.test_auto_organization import service, base_service, org
from tests.test_review_groups import group_source
from tests.unit_model_fixture import many_topic_archive, model_for, run_worker


def unit(service, tid):
    return org(service, '/units', {'task_id': tid})['items'][0]


def archive(service, row, **fields):
    return org(service, '/archive', {'task_id': row['task_id'], 'digest': row['digest'],
        'expected_revision': row['revision'], 'action': 'include', 'project': 'evo', **fields})


@pytest.mark.parametrize('disposition', ['keep', 'review'])
def test_project_selection_can_be_saved_without_preview_or_ai(service, monkeypatch, disposition):
    tid, _ = group_source(service, monkeypatch, count=1)
    row = unit(service, tid)
    if disposition == 'review':
        org(service, '/disposition', {**row, 'expected_revision': row['revision'],
            'disposition': 'review', 'reason': '需要判断'})
        row = unit(service, tid)
    assert archive(service, row)['ok']
    saved = unit(service, tid)
    assert (saved['decision'], saved['disposition'], saved['project']) == ('manual', 'keep', 'evo')
    assert saved['item_id']
    assert service.store._connection().execute('SELECT COUNT(*) FROM organization_review_jobs').fetchone()[0] == 0


def test_defer_needs_no_project_and_preserves_original(service, monkeypatch):
    tid, source = group_source(service, monkeypatch, count=1)
    row = unit(service, tid)
    assert archive(service, row, action='defer', project='')['ok']
    saved = unit(service, tid)
    assert saved['disposition'] == 'set_aside' and not saved['project']
    assert saved['text'] == row['text']
    assert not org(service, '/groups')['items']
    assert archive(service, saved)['ok'], 'deferred material can be explicitly archived later'


def test_direct_save_requires_project_and_current_revision(service, monkeypatch):
    tid, _ = group_source(service, monkeypatch, count=1)
    row = unit(service, tid)
    with pytest.raises(ValueError, match='project_required'):
        archive(service, row, project='')
    with pytest.raises(ValueError, match='revision_conflict'):
        archive(service, row, expected_revision='stale')
    assert unit(service, tid) == row


def test_direct_save_rolls_back_if_history_write_fails(service, monkeypatch):
    from evolvmem import auto_organization as auto
    tid, _ = group_source(service, monkeypatch, count=1)
    row = unit(service, tid)
    def fail(*args, **kwargs):
        assert unit(service, tid)['decision'] == 'manual'
        raise RuntimeError('history_write_failed')
    monkeypatch.setattr(auto, '_ensure_unit_item', fail)
    with pytest.raises(RuntimeError, match='history_write_failed'):
        archive(service, row)
    assert unit(service, tid) == row


def test_ai_owner_is_only_a_suggestion_until_user_saves(service, monkeypatch):
    from evolvmem import history_memory
    source = many_topic_archive(service, text='用户要求：Evo 演示项目导出必须保留编号。')
    tid = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']
    model = model_for(service)
    run_worker(service, monkeypatch, model)
    row = unit(service, tid)
    assert row['decision'] == 'review' and row['project'] == '' and ks.projects(row) == []
    assert row['business_analysis']['projects'] == ['evo']
    assert not row['item_id'] and not model.extract_calls
    assert not history_memory.sessions(service, 'evo')
    detail = org(service, '/groups/detail', {'task_id': tid, 'digest': row['digest']})
    assert detail['items'][0]['suggestion']['projects'] == ['evo']
    archive(service, row)
    assert history_memory.sessions(service, 'evo')
    assert unit(service, tid)['decision'] == 'manual'


def test_future_guidance_cannot_automatically_assign_a_project(service, monkeypatch):
    from evolvmem import organization_semantic_review as semantic
    source = many_topic_archive(service, text='用户要求：Evo 演示项目导出必须保留编号。')
    tid = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']
    monkeypatch.setattr(semantic, 'classify_guidance', lambda *_: {
        'status': 'apply', 'id': 0, 'project': 'evo', 'projects': ['evo'],
        'knowledge_scope': 'project', 'reason': '符合旧指导'})
    run_worker(service, monkeypatch, model_for(service))
    row = unit(service, tid)
    assert row['decision'] == 'review' and row['project'] == ''
    assert row['business_analysis']['projects'] == ['evo']


def test_other_human_correction_entry_also_archives_without_keep_step(service, monkeypatch):
    tid, _ = group_source(service, monkeypatch, count=1)
    row = unit(service, tid)
    org(service, '/disposition', {**row, 'expected_revision': row['revision'],
        'disposition': 'review', 'reason': '需要判断'})
    row = unit(service, tid)
    org(service, '/correct', {**row, 'expected_revision': row['revision'], 'project': 'evo', 'projects': ['evo']})
    saved = unit(service, tid)
    assert (saved['disposition'], saved['decision'], saved['project']) == ('keep', 'manual', 'evo')

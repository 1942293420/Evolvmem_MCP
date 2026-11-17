"""Automatic organization must finish work instead of multiplying human tasks."""
import pytest

from tests.test_auto_organization import service, org
from tests.test_history_qa_memory import service as base_service, archive
from tests.unit_model_fixture import many_topic_archive, model_for, run_worker
from evolvmem.context_models import ContextContentType, ContextItemDraft, ContextScope, ContextStatus
from evolvmem.context_store import _now_iso


def test_clear_projects_are_archived_and_only_ambiguous_unit_needs_review(service, monkeypatch):
    source = many_topic_archive(service, session='automatic-with-correction')
    tid = org(service, '/tasks', {'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']
    run_worker(service, monkeypatch, model_for(service))
    units = org(service, '/units', {'task_id': tid})['items']
    assert [(u['project'], u['decision']) for u in units] == [('evo', 'auto'), ('dsh', 'auto'), ('', 'review')]
    assert org(service, '/detail', {'task_id': tid})['review_count'] == 1
    for unit in units[:2]:
        item = service.knowledge().detail(unit['item_id'])
        assert item['status'] == 'active'
        assert item['ownership']['decision_source'] == 'automatic'
    first = units[0]
    org(service, '/correct', {'task_id': tid, 'digest': first['digest'],
        'expected_revision': first['revision'], 'project': 'shop'})
    from evolvmem.auto_organization import OrganizationWorker, _task_row
    OrganizationWorker(service.config)._assign(service, _task_row(service, tid))
    corrected = org(service, '/units', {'task_id': tid})['items'][0]
    assert (corrected['project'], corrected['decision']) == ('shop', 'manual')
    assert service.knowledge().detail(corrected['item_id'])['project'] == 'shop'


def old_output(service, *, status='candidate', human=False, independent=False, live=False, edited=False):
    """Historical fixture: a real item produced by a retired task."""
    from evolvmem.project_models import ProjectResolutionDecision
    from evolvmem.unit_derivations import record_derivation
    from dataclasses import replace
    conn = service.store._connection()
    serial = conn.execute('SELECT count(*) FROM context_items').fetchone()[0]
    source = archive(service, session=f'retired-{serial}', text='Evo 项目保留可追溯的任务历史。')
    now = _now_iso()
    with service.store.transaction():
        item = service.store._create_legacy_item(ContextItemDraft(
            identity_key=f'project:evo:progress:retired-{serial}',
            content_type=ContextContentType.SESSION_SUMMARY,
            layers=replace(service.knowledge()._layers('旧任务结果', 'Evo 项目保留可追溯的任务历史。', ContextContentType.SESSION_SUMMARY),
                           generator='knowledge-edit.v1' if edited else 'automatic-fixture'),
            project='evo', scope=ContextScope.PROJECT, status=ContextStatus(status)))
        service._project_store().record_resolution(item.id,
            ProjectResolutionDecision.resolved('evo', 'knowledge_rules', 'test', ()))
        if human:
            service._project_store().review_item_project(item.id, 'evo', expected_revision=1)
        tid = conn.execute("INSERT INTO organization_tasks(source_key,source_revision,rule_revision,"
            "source_snapshot,source_title,status,stage,created_at,updated_at) VALUES(?,?,'test','','旧任务','superseded','retired',?,?)",
            (f'archive:{source.id}', str(serial), now, now)).lastrowid
        conn.execute("INSERT INTO organization_units(task_id,ordinal,digest,title,text,source_start,source_end,"
            "project,decision,item_id,created_at,updated_at) VALUES(?,0,'old','旧任务','原文',0,2,'evo','auto',?,?,?)",
            (tid, item.id, now, now))
        record_derivation(service, tid, 'old', item.id, kind='history', project='evo')
        conn.execute("INSERT INTO context_sources(item_id,archive_id,source_kind,source_ref,extraction_version,created_at) "
            "VALUES(?,?,'organization',?,'test',?)", (item.id, source.id, f'archive:{source.id}#old', now))
        if independent:
            conn.execute("INSERT INTO context_sources(item_id,source_kind,source_ref,extraction_version,created_at) "
                "VALUES(?,'manual','独立人工资料','test',?)", (item.id, now))
        if live:
            newer = conn.execute("INSERT INTO organization_tasks(source_key,source_revision,rule_revision,"
                "source_snapshot,source_title,status,stage,created_at,updated_at) VALUES(?,?,'new','','新任务','completed','done',?,?)",
                (f'archive:{source.id}', str(serial)+'-new', now, now)).lastrowid
            record_derivation(service, newer, 'live', item.id, kind='history', project='evo')
    return source, tid, item.id


@pytest.mark.parametrize('status', ['active', 'candidate'])
def test_superseded_outputs_become_history_not_new_review_work(service, status):
    from evolvmem.auto_organization import _mark_outputs_stale
    source, tid, iid = old_output(service, status=status)
    before = service.knowledge().detail(iid)['body']
    _mark_outputs_stale(service, f'archive:{source.id}', current_task=tid+1)
    row = service.knowledge().detail(iid)
    assert row['status'] == 'superseded'
    assert row['body'] == before
    assert service.knowledge().projects()['pending'] == 0
    assert service.store.get_session_archive(source.id)['state'] == 'available'


def test_repair_retired_backlog_is_bounded_previewable_and_preserves_supported_items(service, monkeypatch):
    from evolvmem.organization_retirement import repair_retired_outputs
    from evolvmem import kimi_hooks
    monkeypatch.setattr(kimi_hooks, '_call_llm_with_retry', lambda *a, **k: pytest.fail('repair must not call AI'))
    _, _, stale = old_output(service)
    protected = [old_output(service, **options)[2] for options in
                 ({'human': True}, {'independent': True}, {'live': True}, {'edited': True})]
    _, _, second = old_output(service, status='active')
    preview = repair_retired_outputs(service, limit=1)
    assert preview['eligible_ids'] == [stale] and preview['updated_ids'] == []
    assert service.knowledge().detail(stale)['status'] == 'candidate'
    changed, after = [], 0
    while True:
        batch = repair_retired_outputs(service, after_id=after, limit=2, apply=True)
        assert batch['examined'] <= 2
        changed.extend(batch['updated_ids']); after = batch['after_id']
        if not batch['has_more']: break
    assert changed == [stale, second]
    assert all(service.knowledge().detail(i)['status'] == 'candidate' for i in protected)
    assert repair_retired_outputs(service, apply=True)['updated_ids'] == []


@pytest.mark.parametrize('basis,expected', [('explicit', 'active'), ('inferred', 'candidate')])
def test_same_knowledge_reuses_retired_identity_but_requires_fresh_evidence(service, basis, expected):
    from tests.test_history_qa_memory import extracted_qa
    from evolvmem.organization_retirement import mark_retired
    from evolvmem import qa_memory
    first, source = extracted_qa(service, session='first-evidence')
    iid = first.candidates[0].context_id
    with service.store.transaction():
        assert mark_retired(service, iid)
    newer, second = extracted_qa(service, key='rephrased-key', basis=basis, session='new-evidence')
    assert newer.candidates[0].context_id == iid
    row = service.knowledge().detail(iid)
    assert row['status'] == expected
    assert {source.id, second.id} <= {s['archive_id'] for s in row['sources']}
    assert qa_memory.detail(service, iid)['effective'] == (basis == 'explicit')

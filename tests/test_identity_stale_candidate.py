"""An obsolete optional manual candidate must not invalidate a healthy source."""
import copy
import json
import sqlite3
import sys

import pytest

from tests.test_cross_batch_context import config, service  # noqa: F401
from evolvmem import auto_organization as org, conversation_tasks as conversations
from evolvmem import knowledge_identity as identity
from evolvmem.session_archive import SessionArchiver

FACT = 'Demo 的运行环境固定为 Linux，执行器部署在 Windows。'


def source(service, head, version):
    # Distinct whole-snapshot bytes with the same exact dialogue.
    payload = json.dumps({'messages': [{'role': 'user', 'content': FACT}],
                          'fixture_transport_version': version}, ensure_ascii=False)
    archive = SessionArchiver(service.config, service.store).archive_session(
        '', 'codex', head + ':' + str(version), payload)
    task = conversations.enqueue(service, [f'archive:{archive.id}'])['items'][0]
    return archive, org._task_row(service, task['id'])


def draft(task):
    text = task['source_snapshot']
    record = {'version': 'complete-task-v1', 'requirement': [
        {'text': FACT, 'evidence': [{'message': 1, 'quote': FACT, 'role': 'user'}]}],
        'analysis': [], 'steps': [], 'result': None, 'outstanding': [],
        'state': 'open', 'verification': 'unverified', 'facts': [], 'relations': []}
    return {'source_start': 0, 'source_end': len(text), 'source_ranges': [[0, len(text)]],
            'text': text, 'title': '部署约束', 'category': 'environment', 'project_hint': '',
            'evidence_quote': FACT, 'task_record': record, 'cleaned_text': FACT}


def stored(service, task):
    return dict(service.store._connection().execute(
        'SELECT * FROM organization_units WHERE task_id=?', (task['id'],)).fetchone())


def legacy_manual(service, task):
    service.store._connection().execute(
        "UPDATE organization_units SET knowledge_id='',decision='manual',project='demo',"
        "projects='[\"demo\"]',knowledge_scope='project',revision=revision+1 WHERE task_id=?", (task['id'],))
    service.store._connection().commit()


def scenario(service, *, good=False):
    worker = org.OrganizationWorker(service.config)
    _, old = source(service, 'old-head', 1)
    worker._store_units(service, old, [draft(old)])
    if good:
        _, valid = source(service, 'good-head', 1)
        worker._store_units(service, valid, [draft(valid)])
    legacy_manual(service, old)
    if good:
        legacy_manual(service, valid)
    before = stored(service, old)
    _, current = source(service, 'old-head', 2)
    assert current['source_key'] != old['source_key']
    org._assert_current(service, current)
    with pytest.raises(ValueError, match='classification_source_changed'):
        identity._parts(service, old)
    return worker, old, current, before, valid if good else None


def test_stale_manual_candidate_does_not_abort_cached_narrative(service, monkeypatch):
    worker, old, current, before, _ = scenario(service)
    unit = draft(current)
    service.store._connection().execute(
        "UPDATE organization_tasks SET task_draft=?,stage='narrative' WHERE id=?",
        (json.dumps([unit], ensure_ascii=False), current['id']))
    service.store._connection().commit()
    monkeypatch.setattr(org, '_load_llm', lambda: ({}, lambda *a, **k: pytest.fail('cached narrative must not call a model')))
    worker._segment_complete(service, org._task_row(service, current['id']))
    result = stored(service, current)
    assert result['decision'] == 'review' and result['project'] == '' and result['item_id'] is None
    assert json.loads(result['task_record']) == unit['task_record']
    assert org._task_row(service, current['id'])['stage'] == 'assignment'
    assert stored(service, old) == before
    assert not service.store._connection().execute(
        'SELECT 1 FROM knowledge_source_bindings WHERE task_id=? AND approved=1', (old['id'],)).fetchone()


def test_bad_candidate_does_not_hide_later_valid_manual_candidate(service):
    worker, old, current, before, good = scenario(service, good=True)
    worker._store_units(service, current, [draft(current)])
    result = stored(service, current)
    assert result['decision'] == 'manual' and result['project'] == 'demo'
    assert result['knowledge_id'] == stored(service, good)['knowledge_id']
    assert stored(service, old) == before
    bindings = list(service.store._connection().execute(
        'SELECT task_id FROM knowledge_source_bindings WHERE knowledge_id=? AND approved=1', (result['knowledge_id'],)))
    assert good['id'] in {r[0] for r in bindings} and old['id'] not in {r[0] for r in bindings}
    before_counts = [service.store._connection().execute('SELECT count(*) FROM '+t).fetchone()[0]
                     for t in ('knowledge_objects', 'knowledge_source_bindings', 'knowledge_decisions')]
    worker._store_units(service, current, [draft(current)])
    assert [service.store._connection().execute('SELECT count(*) FROM '+t).fetchone()[0]
            for t in ('knowledge_objects', 'knowledge_source_bindings', 'knowledge_decisions')] == before_counts


def test_current_source_staleness_still_rejects_before_materialization(service):
    worker, old, current, before, _ = scenario(service)
    source(service, 'old-head', 3)
    with pytest.raises(ValueError, match='classification_source_changed'):
        worker._store_units(service, current, [draft(current)])
    assert not service.store._connection().execute('SELECT 1 FROM organization_units WHERE task_id=?', (current['id'],)).fetchone()
    assert stored(service, old) == before


def test_skipping_bad_candidate_does_not_borrow_project_from_generic_good_fact(service, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], 'FACT', '运行环境固定为 Linux，执行器部署在 Windows。')
    worker, old, current, before, good = scenario(service, good=True)
    worker._store_units(service, current, [draft(current)])
    result = stored(service, current)
    assert result['decision'] == 'review' and result['project'] == '' and result['item_id'] is None
    assert result['knowledge_id'] != stored(service, good)['knowledge_id']
    assert stored(service, old) == before
    assert not service.store._connection().execute(
        'SELECT 1 FROM knowledge_source_bindings WHERE task_id=? AND approved=1', (current['id'],)).fetchone()


@pytest.mark.parametrize('failure', [ValueError('unexpected_legacy_contract'), ValueError('cleaning_source_deleted'),
                                   sqlite3.OperationalError('synthetic_sql_error')])
def test_unrelated_candidate_errors_still_propagate(service, monkeypatch, failure):
    worker, old, current, before, _ = scenario(service)
    original = identity._parts
    def broken(s, task):
        if task['id'] == old['id']:
            raise failure
        return original(s, task)
    monkeypatch.setattr(identity, '_parts', broken)
    with pytest.raises(type(failure), match=str(failure)):
        worker._store_units(service, current, [draft(current)])
    assert stored(service, old) == before
    assert not service.store._connection().execute('SELECT 1 FROM organization_units WHERE task_id=?', (current['id'],)).fetchone()


@pytest.mark.parametrize('same', ['source_key', 'id'])
def test_current_source_error_is_not_optional_even_from_legacy_row(service, same):
    worker, old, current, before, _ = scenario(service)
    current_like = {**current, same: old[same]}
    with pytest.raises(ValueError, match='classification_source_changed'):
        identity._verbatim_fact(service, current_like, draft(current_like))
    assert stored(service, old) == before

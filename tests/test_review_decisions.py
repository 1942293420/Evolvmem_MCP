"""Explicit mixed review decisions save together and can reverse scoped exclusions."""
import pytest
from tests.test_review_exclusions import prepare, decide, service, base_service
from tests.test_review_groups import org
from evolvmem import organization_semantic_review as semantic


def choice(unit, action):
    return {k: unit[k] for k in ('task_id', 'digest')} | {'action': action}


def save(service, proposal, decisions):
    return org(service, '/groups/decisions', {'proposal': proposal, 'decisions': decisions})


def test_save_mixed_choices_only_changes_named_rows(service, monkeypatch):
    tid, request, calls, job, p = prepare(service, monkeypatch)
    a, b, untouched = p['eligible']
    result = save(service, p, [choice(a, 'include'), choice(b, 'exclude')])
    assert result['included'] == result['excluded'] == 1
    units = {u['digest']: u for u in org(service, '/units', {'task_id': tid})['items']}
    assert units[a['digest']]['decision'] == 'manual'
    assert units[a['digest']]['project'] == 'evo'
    assert units[b['digest']]['decision'] == units[untouched['digest']]['decision'] == 'review'
    remaining = semantic.get(service, job['id'])['proposal']
    assert [u['digest'] for u in remaining['eligible']] == [untouched['digest']]
    assert remaining['excluded'][0]['reason_code'] == 'manual_excluded'
    assert len(calls) == 3


def test_saved_complete_batch_is_not_offered_for_review_again(service, monkeypatch):
    _, request, calls, job, p = prepare(service, monkeypatch)
    save(service,p,[choice(u,'include' if i==0 else 'exclude') for i,u in enumerate(p['eligible'])])
    assert semantic.recent(service)['job'] is None
    assert semantic.get(service,job['id'])['proposal']['handled_count']==1
    import json
    old=json.loads(semantic._row(service,job['id'])['request']);old.pop('_applied')
    with service.store.transaction():
        service.store._connection().execute('UPDATE organization_review_jobs SET request=? WHERE id=?',(json.dumps(old),job['id']))
    assert semantic.recent(service)['job'] is None, 'old saved batches must also stay closed'


def test_all_excluded_can_be_batch_included_without_new_ai(service, monkeypatch):
    tid, request, calls, job, p = prepare(service, monkeypatch)
    save(service, p, [choice(u, 'exclude') for u in p['eligible']])
    current = semantic.get(service, job['id'])['proposal']
    assert not current['eligible'] and len(current['excluded']) == 3
    result = save(service, current, [choice(u, 'include') for u in current['excluded']])
    assert result['included'] == 3 and len(calls) == 3
    assert service.store._connection().execute('SELECT COUNT(*) FROM organization_scope_exclusions').fetchone()[0] == 0
    assert all(u['decision'] == 'manual' for u in org(service, '/units', {'task_id': tid})['items'])


def test_human_can_include_ai_rejected_or_uncertain_rows(service, monkeypatch):
    tid, request, calls, job, p = prepare(service, monkeypatch)
    import json
    row = semantic._row(service, job['id']); judgments = json.loads(row['judgments'])
    for i, value in enumerate(judgments.values()): value['decision'] = 'exclude' if i else 'uncertain'
    with service.store.transaction():
        service.store._connection().execute('UPDATE organization_review_jobs SET judgments=? WHERE id=?', (json.dumps(judgments), job['id']))
    current = semantic.get(service, job['id'])['proposal']
    assert not current['eligible']
    assert save(service, current, [choice(u, 'include') for u in current['excluded']])['included'] == 3


def test_invalid_selection_or_stale_preview_writes_nothing(service, monkeypatch):
    tid, request, calls, job, p = prepare(service, monkeypatch)
    first = p['eligible'][0]
    for bad in ([choice(first, 'exclude'), {'task_id': tid, 'digest': 'outside', 'action': 'include'}],
                [choice(first, 'include'), choice(first, 'exclude')], [], [choice(first, 'delete')]):
        with pytest.raises(ValueError): save(service, p, bad)
    with service.store.transaction():
        service.store._connection().execute('UPDATE organization_units SET revision=revision+1 WHERE task_id=? AND ordinal=1', (tid,))
    with pytest.raises(ValueError, match='review_preview_changed'):
        save(service, p, [choice(first, 'exclude')])
    assert service.store._connection().execute('SELECT COUNT(*) FROM organization_scope_exclusions').fetchone()[0] == 0


def test_failed_include_rolls_back_preceding_exclusion(service, monkeypatch):
    from evolvmem import auto_organization
    tid, request, calls, job, p = prepare(service, monkeypatch)
    def fail(*args, **kwargs): raise ValueError('write_failed')
    monkeypatch.setattr(auto_organization, 'correct_one', fail)
    with pytest.raises(ValueError, match='write_failed'):
        save(service, p, [choice(p['eligible'][0], 'exclude'), choice(p['eligible'][1], 'include')])
    assert service.store._connection().execute('SELECT COUNT(*) FROM organization_scope_exclusions').fetchone()[0] == 0


def test_reversing_one_scope_preserves_other_scope_exclusion(service, monkeypatch):
    from evolvmem import organization_review as review
    tid, request, calls, job, p = prepare(service, monkeypatch)
    unit = p['eligible'][0]
    other = review.preview(service, {**unit, 'group_id': p['group_id'], 'selection': 'item', 'project': 'dsh'})
    decide(service, other, unit)
    p = semantic.get(service, job['id'])['proposal']
    save(service, p, [choice(unit, 'exclude')])
    p = semantic.get(service, job['id'])['proposal']
    save(service, p, [choice(p['excluded'][0], 'include')])
    assert [r[0] for r in service.store._connection().execute('SELECT scope_key FROM organization_scope_exclusions')] == ['project:dsh']


def test_completed_choices_remain_readable_without_automatic_reopening(service, monkeypatch):
    tid, request, calls, job, p = prepare(service, monkeypatch)
    a, b, c = p['eligible']
    save(service, p, [choice(a, 'include'), choice(b, 'exclude'), choice(c, 'exclude')])
    assert semantic.recent(service)['job'] is None
    restored = semantic.get(service,job['id'])
    assert len(restored['proposal']['excluded']) == 2 and len(calls) == 3


def test_failure_after_first_real_write_rolls_back_whole_review(service, monkeypatch):
    from evolvmem import auto_organization
    tid, request, calls, job, p = prepare(service, monkeypatch)
    original = auto_organization.correct_one
    attempts = []
    def second_fails(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 2: raise ValueError('write_failed')
        return original(*args, **kwargs)
    before = service.store._connection().execute('SELECT COUNT(*) FROM context_items').fetchone()[0]
    monkeypatch.setattr(auto_organization, 'correct_one', second_fails)
    with pytest.raises(ValueError, match='write_failed'):
        save(service, p, [choice(u, 'include') for u in p['eligible']])
    assert service.store._connection().execute('SELECT COUNT(*) FROM context_items').fetchone()[0] == before
    assert all(u['decision'] == 'review' for u in org(service, '/units', {'task_id': tid})['items'])


@pytest.mark.parametrize('rollback', [False, True])
def test_review_indexes_only_committed_decisions(service, monkeypatch, rollback):
    from types import SimpleNamespace
    from evolvmem import auto_organization
    tid, request, calls, job, p = prepare(service, monkeypatch)
    writes = []
    def vector(*args): writes.append(service.store._connection().in_transaction)
    monkeypatch.setattr(service, '_context_synchronizer', lambda: SimpleNamespace(upsert_active_l0=vector, remove_l0=vector))
    if rollback:
        original = auto_organization.correct_one
        def fail_after_write(*args, **kwargs):
            original(*args, **kwargs)
            raise ValueError('write_failed')
        monkeypatch.setattr(auto_organization, 'correct_one', fail_after_write)
        with pytest.raises(ValueError, match='write_failed'):
            save(service, p, [choice(p['eligible'][0], 'include')])
        assert writes == []
    else:
        save(service, p, [choice(p['eligible'][0], 'include')])
        assert writes and not any(writes)


def test_hundred_item_batch_saves_mixed_choices(service, monkeypatch):
    from tests.test_review_result_cache import setup_job
    tid, request, calls = setup_job(service, monkeypatch, count=100)
    job = semantic.start(service, request); semantic.tick(service)
    p = semantic.get(service, job['id'])['proposal']
    result = save(service, p, [choice(u, 'include' if i < 10 else 'exclude') for i, u in enumerate(p['eligible'])])
    assert result['included'] == result['excluded'] == 10
    assert service.store._connection().execute('SELECT COUNT(*) FROM organization_scope_exclusions').fetchone()[0] == 10
    assert len(calls) == 20

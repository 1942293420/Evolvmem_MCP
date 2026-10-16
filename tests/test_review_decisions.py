"""Explicit mixed review decisions save together and can reverse scoped exclusions."""
import pytest
from tests.test_review_exclusions import prepare, decide, service, base_service
from tests.test_review_groups import org
from evolvmem import organization_semantic_review as semantic


def value_review(service, monkeypatch, *, count=1):
    from tests.test_review_result_cache import setup_job
    tid, request, calls = setup_job(service, monkeypatch, count=count)
    units = org(service, '/units', {'task_id': tid})['items']
    unit = units[-1]
    org(service, '/disposition', {'task_id': tid, 'digest': unit['digest'],
        'expected_revision': unit['revision'], 'disposition': 'review', 'reason': '需要确认保留价值'})
    job = semantic.start(service, request)
    semantic.tick(service)
    return tid, calls, job, semantic.get(service, job['id'])['proposal']


def test_value_review_can_be_explicitly_retained_and_filed_without_ai(service, monkeypatch):
    tid, calls, job, p = value_review(service, monkeypatch)
    assert not p['eligible'] and p['excluded'][0]['reason_code'] == 'value_review'
    assert calls == [], 'AI must not silently retain material awaiting a human'
    result = save(service, p, [choice(p['excluded'][0], 'include')])
    assert result['included'] == 1
    unit = org(service, '/units', {'task_id': tid})['items'][0]
    assert (unit['disposition'], unit['decision'], unit['project']) == ('keep', 'manual', 'evo')
    assert unit['item_id'], 'manual filing must actually retain readable history'
    assert calls == []
    assert semantic.recent(service)['job'] is None


def test_value_exclusion_preserves_material_and_can_be_reversed(service, monkeypatch):
    tid, calls, job, p = value_review(service, monkeypatch)
    before = org(service, '/units', {'task_id': tid})['items'][0]
    assert save(service, p, [choice(p['excluded'][0], 'exclude')])['excluded'] == 1
    assert org(service, '/units', {'task_id': tid})['items'][0] == before
    p = semantic.get(service, job['id'])['proposal']
    assert p['excluded'][0]['reason_code'] == 'manual_excluded'
    assert save(service, p, [choice(p['excluded'][0], 'include')])['included'] == 1
    assert org(service, '/units', {'task_id': tid})['items'][0]['disposition'] == 'keep'
    assert calls == []


def test_defer_saves_without_project_exclusion_and_is_not_restored_as_pending(service, monkeypatch):
    tid, calls, job, p = value_review(service, monkeypatch)
    before = org(service, '/units', {'task_id': tid})['items'][0]
    assert save(service, p, [choice(p['excluded'][0], 'defer')])['deferred'] == 1
    after = org(service, '/units', {'task_id': tid})['items'][0]
    assert after['disposition'] == 'set_aside' and after['text'] == before['text']
    assert not after['project']
    assert service.store._connection().execute('SELECT COUNT(*) FROM organization_scope_exclusions').fetchone()[0] == 0
    assert semantic.recent(service)['job'] is None
    assert calls == []


@pytest.mark.parametrize('retained', [False, True])
def test_unhandled_value_review_survives_saving_other_judgments(service, monkeypatch, retained):
    tid, calls, job, p = value_review(service, monkeypatch, count=2)
    if retained:
        unit = org(service, '/units', {'task_id': tid})['items'][-1]
        org(service, '/disposition', {'task_id': tid, 'digest': unit['digest'],
            'expected_revision': unit['revision'], 'disposition': 'keep'})
        p = semantic.get(service, job['id'])['proposal']
    assert len(p['eligible']) == 1 and len(p['excluded']) == 1
    save(service, p, [choice(p['eligible'][0], 'include')])
    restored = semantic.recent(service)['job']
    assert restored and restored['id'] == job['id']
    assert restored['proposal']['excluded'][0]['reason_code'] == ('batch_limit' if retained else 'value_review')
    # Older completed jobs may lack the applied flag; recovery uses the same rule.
    import json
    request = json.loads(semantic._row(service, job['id'])['request']);request.pop('_applied', None)
    with service.store.transaction():
        service.store._connection().execute('UPDATE organization_review_jobs SET request=? WHERE id=?', (json.dumps(request),job['id']))
    assert semantic.recent(service)['job']['id'] == job['id']


def test_previously_retained_material_can_be_filed_from_restored_preview(service, monkeypatch):
    tid, calls, job, p = value_review(service, monkeypatch)
    unit = org(service, '/units', {'task_id': tid})['items'][0]
    org(service, '/disposition', {'task_id': tid, 'digest': unit['digest'],
        'expected_revision': unit['revision'], 'disposition': 'keep'})
    p = semantic.get(service, job['id'])['proposal']
    assert p['excluded'][0]['reason_code'] == 'batch_limit'
    assert save(service, p, [choice(p['excluded'][0], 'include')])['included'] == 1
    saved = org(service, '/units', {'task_id': tid})['items'][0]
    assert (saved['disposition'], saved['decision'], saved['project']) == ('keep', 'manual', 'evo')
    assert calls == []


def test_failed_filing_rolls_back_value_confirmation(service, monkeypatch):
    from evolvmem import auto_organization as org_module
    tid, calls, job, p = value_review(service, monkeypatch)
    before = org(service, '/units', {'task_id': tid})['items'][0]
    def fail(*args, **kwargs):
        current = org(service, '/units', {'task_id': tid})['items'][0]
        assert current['disposition'] == 'keep'
        raise ValueError('write_failed')
    monkeypatch.setattr(org_module, '_ensure_unit_item', fail)
    with pytest.raises(ValueError, match='write_failed'):
        save(service, p, [choice(p['excluded'][0], 'include')])
    assert org(service, '/units', {'task_id': tid})['items'][0] == before
    assert service.store._connection().execute('SELECT COUNT(*) FROM organization_scope_exclusions').fetchone()[0] == 0


def test_stale_value_review_cannot_be_saved(service, monkeypatch):
    tid, calls, job, p = value_review(service, monkeypatch)
    with service.store.transaction():
        service.store._connection().execute('UPDATE organization_units SET revision=revision+1 WHERE task_id=?', (tid,))
    with pytest.raises(ValueError, match='review_preview_changed'):
        save(service, p, [choice(p['excluded'][0], 'include')])
    assert org(service, '/units', {'task_id': tid})['items'][0]['disposition'] == 'review'


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

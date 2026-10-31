import sqlite3

import pytest

from evolvmem.capability_store import CapabilityStore
from tests.test_capability_provenance import provenance_db, add_memory, unit_source, context_source, DIGEST


@pytest.fixture
def modules(provenance_db):
    from evolvmem.project_modules import SCHEMA, ProjectModuleStore
    with provenance_db.transaction():
        for statement in SCHEMA:
            provenance_db._connection().execute(statement)
    return ProjectModuleStore(provenance_db)


def create(modules, project='alpha', name='Order handling', **kwargs):
    return modules.create(project, name, explicit=True, **kwargs)


def bind(modules, module, **kwargs):
    values = dict(quote='user: a fact', role='owner', decision='auto',
                  chain={'change': 'user: a fact'}, expected_revision=0)
    values.update(kwargs)
    return modules.bind_unit(module['module_id'], 1, DIGEST, **values)


def test_empty_registry_and_technical_queries_never_create_modules(modules):
    before = modules.conn.total_changes
    assert modules.list('alpha') == []
    assert modules.candidates('alpha', 'frontend backend database') == []
    assert modules.conn.total_changes == before


def test_active_creation_requires_explicit_action_and_active_canonical_project(modules):
    with pytest.raises(ValueError, match='explicit_module_operation_required'):
        modules.create('alpha', 'Orders')
    with pytest.raises(ValueError, match='active_project_required'):
        create(modules, 'not-registered')
    with modules.store.transaction():
        modules.conn.execute("UPDATE context_project_registry SET status='archived' WHERE project='alpha'")
    with pytest.raises(ValueError, match='active_project_required'):
        create(modules)


def test_suggestion_is_pending_until_explicit_cas_activation(modules):
    row = modules.suggest('alpha', 'Orders', aliases=['订单'])
    assert row['status'] == 'pending' and row['revision'] == 1
    assert modules.list('alpha') == []
    assert modules.suggest('alpha', ' orders ')['module_id'] == row['module_id']
    with pytest.raises(ValueError, match='explicit_module_operation_required'):
        modules.activate(row['module_id'], expected_revision=1)
    active = modules.activate(row['module_id'], expected_revision=1, explicit=True)
    assert active['status'] == 'active' and active['revision'] == 2
    assert modules.list('alpha')[0]['module_id'] == row['module_id']
    assert [r[0] for r in modules.conn.execute('SELECT action FROM project_module_audit ORDER BY audit_id')] == ['suggest', 'activate']


def test_names_aliases_uniqueness_is_scoped_to_project_and_cas_is_atomic(modules):
    row = create(modules, name='Orders', aliases=['订单'])
    assert create(modules, 'beta', name='Orders')['project'] == 'beta'
    for name in (' orders ', '订单'):
        with pytest.raises(ValueError, match='module_name_conflict'):
            create(modules, name=name)
    other = create(modules, name='Shipping')
    with pytest.raises(ValueError, match='module_name_conflict'):
        modules.update(other['module_id'], expected_revision=1, aliases=['订单'], explicit=True)
    assert modules.get(other['module_id'])['revision'] == 1
    updated = modules.update(row['module_id'], expected_revision=1, description='Customer orders', explicit=True)
    assert updated['revision'] == 2
    with pytest.raises(ValueError, match='revision_conflict'):
        modules.update(row['module_id'], expected_revision=1, name='Wrong', explicit=True)
    assert modules.get(row['module_id'])['name'] == 'Orders'


def test_candidate_vector_scores_are_project_scoped_and_never_write(modules):
    first = create(modules, name='Orders', aliases=['订单'])
    second = create(modules, 'beta', name='Shipping')
    before = modules.conn.total_changes
    rows = modules.candidates('alpha', '订单', vector_scores={first['module_id']: 0.7, second['module_id']: 1.0})
    assert [r['module_id'] for r in rows] == [first['module_id']]
    assert rows[0]['lexical_score'] > 0
    assert modules.conn.total_changes == before
    assert modules.conn.execute('SELECT count(*) FROM project_module_bindings').fetchone()[0] == 0


def test_valid_current_unit_binding_and_multiple_memory_sources_merge(modules):
    module = create(modules)
    binding = bind(modules, module, decision='manual', explicit=True)
    assert binding['revision'] == 1
    memory_id = add_memory(modules.store, source=unit_source(modules.store))
    source_id = CapabilityStore(modules.store).sources(memory_id)[0]['source_id']
    second = modules.bind_source(module['module_id'], source_id, quote='user: a fact',
                                 role='applicable', decision='manual', expected_revision=0, explicit=True)
    result = modules.modules_for_memory(memory_id)
    assert len(result) == 1 and result[0]['module_id'] == module['module_id']
    assert result[0]['binding_ids'] == [binding['binding_id'], second['binding_id']]
    assert modules.modules_for_memory(memory_id, allowed_source_ids=set()) == []
    assert modules.modules_for_memory(memory_id, allowed_source_ids={source_id})[0]['module_id'] == module['module_id']


def test_cross_project_and_unconfirmed_project_cannot_bind(modules):
    module = create(modules)
    with modules.store.transaction():
        modules.conn.execute("UPDATE organization_units SET project='beta',projects='[\"beta\"]'")
    with pytest.raises(ValueError, match='module_project_mismatch'):
        bind(modules, module)
    with modules.store.transaction():
        modules.conn.execute("UPDATE organization_units SET decision='review'")
    with pytest.raises(ValueError, match='module_project_mismatch'):
        bind(modules, module)


@pytest.mark.parametrize('changes,error', [
    ({'quote': 'not in original'}, 'quote_not_verifiable'),
    ({'chain': {}}, 'module_role_evidence_required'),
    ({'role': 'related'}, 'module_role_evidence_required'),
    ({'chain': {'change': 'fabricated'}}, 'module_role_evidence_required'),
])
def test_auto_binding_requires_verifiable_role_and_quotes(modules, changes, error):
    with pytest.raises(ValueError, match=error):
        bind(modules, create(modules), **changes)
    assert modules.conn.execute('SELECT count(*) FROM project_module_bindings').fetchone()[0] == 0


def test_manual_binding_is_not_overwritten_or_resurrected_by_auto(modules):
    module = create(modules)
    manual = bind(modules, module, decision='manual', explicit=True, chain={})
    with pytest.raises(ValueError, match='manual_module_binding_protected'):
        bind(modules, module, expected_revision=manual['revision'])
    withdrawn = modules.unbind(manual['binding_id'], expected_revision=1, explicit=True)
    assert withdrawn['status'] == 'inactive'
    with pytest.raises(ValueError, match='manual_module_binding_protected'):
        bind(modules, module, expected_revision=withdrawn['revision'])


@pytest.mark.parametrize('sql,reason', [
    ("UPDATE organization_units SET text='changed'", 'source_invalid'),
    ("UPDATE organization_units SET project='beta',projects='[\"beta\"]',revision=2", 'module_project_mismatch'),
    ("UPDATE organization_tasks SET status='superseded'", 'source_invalid'),
    ("UPDATE session_archives SET state='purged'", 'source_invalid'),
    ("UPDATE context_project_registry SET status='archived' WHERE project='alpha'", 'module_project_inactive'),
])
def test_source_or_project_changes_invalidate_live_binding(modules, sql, reason):
    module = create(modules)
    binding = bind(modules, module)
    with modules.store.transaction():
        modules.conn.execute(sql)
    status = modules.binding_status(binding['binding_id'])
    assert not status['valid'] and status['reason'] == reason
    assert modules.modules_for_unit(1, DIGEST) == []


def test_module_deactivation_and_pending_module_never_supply_bindings(modules):
    pending = modules.suggest('alpha', 'Pending')
    with pytest.raises(ValueError, match='module_not_active'):
        bind(modules, pending)
    module = create(modules)
    binding = bind(modules, module)
    modules.deactivate(module['module_id'], expected_revision=1, explicit=True)
    assert not modules.binding_status(binding['binding_id'])['valid']
    assert modules.modules_for_unit(1, DIGEST) == []


def test_binding_cas_and_review_do_not_publish(modules):
    module = create(modules)
    review = bind(modules, module, decision='review', role='uncertain', chain={})
    assert not modules.binding_status(review['binding_id'])['valid']
    with pytest.raises(ValueError, match='revision_conflict'):
        bind(modules, module)
    updated = bind(modules, module, expected_revision=1)
    assert updated['revision'] == 2
    assert modules.binding_status(updated['binding_id'])['valid']


def test_context_capability_source_can_bind_but_ownership_and_source_changes_invalidate(modules):
    module = create(modules)
    with modules.store.transaction():
        modules.conn.execute("INSERT INTO context_project_resolutions(item_id,resolution_state,decision_source,review_state,resolved_project,created_at,updated_at) VALUES(1,'resolved','human','accepted','alpha','old','old')")
    memory_id = add_memory(modules.store, source=context_source(modules.store))
    source_id = CapabilityStore(modules.store).sources(memory_id)[0]['source_id']
    binding = modules.bind_source(module['module_id'], source_id, quote='A fact', role='owner',
                                  decision='manual', expected_revision=0, explicit=True)
    assert modules.modules_for_memory(memory_id)[0]['module_id'] == module['module_id']
    with modules.store.transaction():
        modules.conn.execute("UPDATE context_project_resolutions SET resolved_project='beta',revision=2 WHERE item_id=1")
    assert not modules.binding_status(binding['binding_id'])['valid']
    assert modules.modules_for_memory(memory_id) == []


def test_archived_capability_source_is_not_current_module_evidence(modules):
    module = create(modules)
    memory_id = add_memory(modules.store, source=unit_source(modules.store))
    source_id = CapabilityStore(modules.store).sources(memory_id)[0]['source_id']
    binding = modules.bind_source(module['module_id'], source_id, quote='user: a fact', role='owner',
                                  decision='manual', expected_revision=0, explicit=True)
    with modules.store.transaction():
        modules.conn.execute("UPDATE capability_memories SET status='archived' WHERE memory_id=?", (memory_id,))
    assert not modules.binding_status(binding['binding_id'])['valid']


def test_pending_memory_can_be_classified_for_browsing_without_becoming_recalled(modules):
    module = create(modules)
    memory_id = add_memory(modules.store, source=unit_source(modules.store))
    source_id = CapabilityStore(modules.store).sources(memory_id)[0]['source_id']
    with modules.store.transaction():
        modules.conn.execute("UPDATE capability_memories SET status='pending' WHERE memory_id=?", (memory_id,))
    binding = modules.bind_source(module['module_id'], source_id, quote='user: a fact', role='owner',
                                  decision='manual', expected_revision=0, explicit=True)
    status = modules.binding_status(binding['binding_id'])
    assert status['valid'] and status['not_recalled']
    row = modules.modules_for_memory(memory_id)[0]
    assert row['module_id'] == module['module_id'] and row['not_recalled']
    assert modules.modules_for_memory(memory_id, allowed_source_ids=set()) == []
    assert modules.conn.execute('SELECT status FROM capability_memories WHERE memory_id=?', (memory_id,)).fetchone()[0] == 'pending'


def test_manual_withdrawal_cannot_be_bypassed_via_same_unit_source_alias(modules):
    module = create(modules)
    memory_id = add_memory(modules.store, source=unit_source(modules.store))
    source_id = CapabilityStore(modules.store).sources(memory_id)[0]['source_id']
    auto = modules.bind_source(module['module_id'], source_id, quote='user: a fact', role='owner',
                               chain={'change': 'user: a fact'}, decision='auto', expected_revision=0)
    manual = bind(modules, module, decision='manual', explicit=True)
    modules.unbind(manual['binding_id'], expected_revision=1, explicit=True)
    assert not modules.binding_status(auto['binding_id'])['valid']
    assert modules.modules_for_memory(memory_id) == []
    with pytest.raises(ValueError, match='manual_module_binding_protected'):
        modules.bind_source(module['module_id'], source_id, quote='user: a fact', role='owner',
                            chain={'change': 'user: a fact'}, decision='auto', expected_revision=1)


def test_legacy_none_source_id_can_be_explicitly_allowed_without_enabling_other_sources(modules):
    module = create(modules)
    bind(modules, module)
    memory_id = add_memory(modules.store, ref=f'archive:1#{DIGEST}@0-12')
    assert modules.modules_for_memory(memory_id, allowed_source_ids={None})[0]['module_id'] == module['module_id']
    assert modules.modules_for_memory(memory_id, allowed_source_ids={999}) == []


def test_source_ownership_only_revision_keeps_module_when_current_project_still_matches(modules):
    module = create(modules)
    memory_id = add_memory(modules.store, source=unit_source(modules.store))
    capabilities = CapabilityStore(modules.store)
    source_id = capabilities.sources(memory_id)[0]['source_id']
    binding = modules.bind_source(module['module_id'], source_id, quote='user: a fact', role='owner',
                                  decision='manual', expected_revision=0, explicit=True)
    with modules.store.transaction():
        modules.conn.execute("UPDATE organization_units SET revision=2,project='alpha',projects='[\"alpha\"]'")
    capabilities.bind_source(memory_id, unit_source(modules.store, unit_revision=2, projects=('alpha',)))
    assert capabilities.sources(memory_id)[0]['revision'] == 2
    assert modules.binding_status(binding['binding_id'])['valid']
    assert binding['source_binding_revision'] == 1
    with modules.store.transaction():
        modules.conn.execute("UPDATE organization_units SET revision=3,project='beta',projects='[\"beta\"]'")
    capabilities.bind_source(memory_id, unit_source(modules.store, unit_revision=3, projects=('beta',)))
    assert not modules.binding_status(binding['binding_id'])['valid']


def test_audit_failure_rolls_back_module_names_inside_outer_transaction(modules):
    with modules.store.transaction():
        modules.conn.execute("CREATE TRIGGER reject_module_audit BEFORE INSERT ON project_module_audit BEGIN SELECT RAISE(ABORT,'synthetic_failure'); END")
    with modules.store.transaction():
        with pytest.raises(sqlite3.IntegrityError, match='synthetic_failure'):
            create(modules)
    assert modules.list('alpha') == []
    assert modules.conn.execute('SELECT count(*) FROM project_module_names').fetchone()[0] == 0


def test_binding_audit_failure_is_atomic_and_registry_project_never_changes(modules):
    module = create(modules)
    registry = [dict(r) for r in modules.conn.execute('SELECT * FROM context_project_registry')]
    with modules.store.transaction():
        modules.conn.execute("CREATE TRIGGER reject_binding_audit BEFORE INSERT ON project_module_audit WHEN NEW.binding_id IS NOT NULL BEGIN SELECT RAISE(ABORT,'synthetic_failure'); END")
    with pytest.raises(sqlite3.IntegrityError, match='synthetic_failure'):
        bind(modules, module)
    assert modules.conn.execute('SELECT count(*) FROM project_module_bindings').fetchone()[0] == 0
    renamed = modules.update(module['module_id'], expected_revision=1, name='Renamed orders', explicit=True)
    assert renamed['module_id'] == module['module_id'] and renamed['project'] == 'alpha'
    assert [dict(r) for r in modules.conn.execute('SELECT * FROM context_project_registry')] == registry


def test_precise_source_allowlist_never_treats_legacy_none_as_wildcard(modules):
    from evolvmem.capability_provenance import annotate
    module = create(modules)
    bind(modules, module)
    memory_id = add_memory(modules.store, ref=f'archive:1#{DIGEST}@0-12')
    valid = annotate(modules.store, [memory_id])[memory_id]['sources'][0]
    assert valid['valid'] and valid.get('source_id') is None
    assert modules.modules_for_memory(memory_id, allowed_sources=[valid])[0]['module_id'] == module['module_id']
    different = {**valid, 'source_key': 'another-legacy-source'}
    assert modules.modules_for_memory(memory_id, allowed_sources=[different]) == []
    assert modules.modules_for_memory(memory_id, allowed_sources=[]) == []

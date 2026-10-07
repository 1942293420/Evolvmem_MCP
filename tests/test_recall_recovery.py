"""Focused tests for the projection status repair and the stuck dirty marker.

All data here is synthetic: no production database, config, log, or credential
is read. The tests drive the real KnowledgeService/ContextService code paths but
replace the model provider with the shared deterministic unit provider.

The drifted legacy status is *injected explicitly* rather than produced by the
``auto_organization`` double-write defect, so these fixtures keep their meaning
once that defect is fixed at the call site.
"""
from __future__ import annotations

import numpy as np
import pytest

from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayers,
    ContextStatus,
)
from evolvmem.context_store import ContextStore
from evolvmem.cutover_checks import check_projection_lag
from evolvmem.recall_recovery import (
    apply_projection_status_recovery,
    classify_projection_status_drift,
    diagnose_projection_status,
)
from evolvmem.vector_index import VectorIndex
from tests.test_auto_organization import org
from tests.test_history_qa_memory import service as base_service
from tests.unit_model_fixture import many_topic_archive, model_for, run_worker

_NOW = '2026-01-01 00:00:00'


@pytest.fixture
def service(base_service):
    # The shared fixture registers evo/shop; the pipeline also assigns dsh.
    base_service.knowledge().save_project({'project': 'dsh', 'display_name': 'DSH 演示项目'})
    return base_service


# ---- explicit drift construction ---------------------------------------------


def _unit_item_ids(service, task_id):
    return [row['item_id'] for row in service.store._connection().execute(
        'SELECT item_id FROM organization_units WHERE task_id=? AND item_id IS NOT NULL',
        (task_id,)).fetchall()]


def _source_key(service, task_id):
    row = service.store._connection().execute(
        'SELECT source_key FROM organization_tasks WHERE id=?', (task_id,)).fetchone()
    return row['source_key']


def _legacy_row(service, item_id):
    return service.store._connection().execute(
        'SELECT m.id AS legacy_id, m.status AS legacy_status FROM memories m '
        'JOIN legacy_memory_migrations map ON map.legacy_memory_id=m.id '
        'WHERE map.context_item_id=?', (item_id,)).fetchone()


def _legacy_status(service, item_id):
    row = _legacy_row(service, item_id)
    return None if row is None else row['legacy_status']


def run_real_first_task(service, monkeypatch, *, session='drift-session'):
    """Run the real organization pipeline once and return its task and items."""
    source = many_topic_archive(service, session=session)
    task_id = org(service, '/tasks', {
        'items': [{'key': f'archive:{source.id}'}]})['items'][0]['id']
    run_worker(service, monkeypatch, model_for(service, projects={'evo', 'dsh'}))
    item_ids = _unit_item_ids(service, task_id)
    assert item_ids, 'the real pipeline must write the session summaries'
    assert all(_legacy_status(service, item_id) == 'active' for item_id in item_ids)
    return task_id, item_ids


def supersede_task(service, task_id):
    """Enqueue a successor revision and retire the first task, as enqueue does."""
    source_key = _source_key(service, task_id)
    rules = service.knowledge().rules.read()
    service.knowledge().rules.save({
        'expected_revision': rules['revision'],
        'instructions': rules['instructions'] + '\n新增一条归属说明。',
    })
    successor = org(service, '/tasks', {'items': [{'key': source_key}]})['items'][0]['id']
    assert successor != task_id
    conn = service.store._connection()
    assert conn.execute('SELECT 1 FROM organization_units WHERE task_id=?',
                        (successor,)).fetchone() is None, 'successor must not own units yet'
    with service.store.transaction():
        conn.execute(
            "UPDATE organization_tasks SET status='superseded',superseded_by=?,"
            "finished_at=?,updated_at=? WHERE id=?",
            (successor, _NOW, _NOW, task_id))
    return successor


def inject_legacy_active_drift(service, item_ids, *, project=None):
    """Reproduce exactly what the missing legacy write leaves: candidate+active.

    The Context side takes the state the automatic downgrade produces and the
    mapped legacy row is forced back to ``active``, so the drift exists here
    even when the production call site stops producing it by accident.
    """
    with service.store.transaction():
        conn = service.store._connection()
        for item_id in item_ids:
            if project is None:
                conn.execute(
                    "UPDATE context_items SET status='candidate',updated_at=? WHERE id=?",
                    (_NOW, item_id))
            else:
                conn.execute(
                    "UPDATE context_items SET status='candidate',project=?,scope='project',"
                    'updated_at=? WHERE id=?', (project, _NOW, item_id))
            conn.execute(
                "UPDATE memories SET status='active',updated_at=? WHERE id=("
                'SELECT legacy_memory_id FROM legacy_memory_migrations '
                'WHERE context_item_id=?)', (_NOW, item_id))
    return list(item_ids)


def make_drifted_projection(service, monkeypatch, *, session='drift-session'):
    """Real pipeline output, then the explicit superseded-output drift."""
    task_id, item_ids = run_real_first_task(service, monkeypatch, session=session)
    supersede_task(service, task_id)
    inject_legacy_active_drift(service, item_ids)
    return task_id, item_ids


def make_retracted_projection(service, monkeypatch, *, session='retract-session'):
    """The second reported shape: the automatic unassign retraction.

    ``auto_organization._unresolve_item`` clears the project and marks the item
    candidate while the already-written legacy projection stays active. The
    real call is used, then only the missing legacy half is injected.
    """
    from evolvmem.auto_organization import _unresolve_item

    task_id, item_ids = run_real_first_task(service, monkeypatch, session=session)
    item_id = item_ids[0]
    _unresolve_item(service, item_id, '归属未确认，先撤回')
    inject_legacy_active_drift(service, [item_id], project='')
    item = service.store.get_item(item_id)
    assert item.status is ContextStatus.CANDIDATE
    assert item.project == '', 'the retraction keeps the item unassigned'
    resolution = service.store._connection().execute(
        'SELECT decision_source,resolution_state,resolver_version '
        'FROM context_project_resolutions WHERE item_id=?', (item_id,)).fetchone()
    assert resolution['decision_source'] == 'automatic'
    assert resolution['resolution_state'] == 'unresolved'
    assert resolution['resolver_version'] == 'auto-organization.v1'
    return task_id, item_id


def drifted_items(service, item_ids):
    """The items that are Context candidates while their legacy row is active."""
    return [
        item_id for item_id in item_ids
        if service.store.get_item(item_id).status is ContextStatus.CANDIDATE
        and _legacy_status(service, item_id) == 'active'
    ]


def _set_human_resolution(service, item_id):
    with service.store.transaction():
        service.store._connection().execute(
            "INSERT OR REPLACE INTO context_project_resolutions"
            '(item_id,resolution_state,decision_source,review_state,proposed_project,'
            'resolved_project,confidence,method,evidence_json,resolver_version,revision,'
            'reviewed_at,created_at,updated_at) '
            "VALUES(?,'resolved','human','accepted','','evo','high','operator','[]','operator.v1',1,"
            "?,?,?)",
            (item_id, _NOW, _NOW, _NOW))


def _set_legacy_already_candidate(service, item_id):
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE memories SET status='candidate' WHERE id=("
            'SELECT legacy_memory_id FROM legacy_memory_migrations WHERE context_item_id=?)',
            (item_id,))


def _add_live_owner(service, item_id):
    """Give one drifted item a non-superseded owner: it must never be repaired."""
    task_id = _add_task(service, status='review')
    _add_unit(service, task_id, item_id, ordinal=0)
    return task_id


def _add_superseded_owner(service, item_id, *, ordinal=0):
    task_id = _add_task(service, status='superseded')
    _add_unit(service, task_id, item_id, ordinal=ordinal)
    return task_id


def _add_task(service, *, status):
    with service.store.transaction():
        cursor = service.store._connection().execute(
            "INSERT INTO organization_tasks(source_key,source_revision,rule_revision,"
            "source_snapshot,source_title,stage,status,created_at,updated_at) "
            "VALUES('synthetic:0',?,'0','',?,'done',?,?,?)",
            (f'{status}-{_next_task_seed()}', status, status, _NOW, _NOW))
        return cursor.lastrowid


_task_seed = [0]


def _next_task_seed():
    _task_seed[0] += 1
    return _task_seed[0]


def _add_unit(service, task_id, item_id, *, ordinal):
    with service.store.transaction():
        service.store._connection().execute(
            "INSERT INTO organization_units(task_id,ordinal,digest,title,text,source_start,"
            "source_end,category,role,cleaned_text,disposition,disposition_reason,extraction_stage,"
            "extraction_error,extraction_signature,project_hint,project,decision,reason,"
            "evidence_quote,revision,item_id,created_at,updated_at) "
            "VALUES(?,?,'synthetic-digest','synthetic','synthetic text',0,9,'reference','user',"
            "'synthetic text','keep','','pending','','','','evo','auto','','',1,?,?,?)",
            (task_id, ordinal, item_id, _NOW, _NOW))


def _make_candidate_with_legacy(service, *, identity_key, value='synthetic candidate'):
    """One candidate Context item with an active, mapped legacy projection.

    The value and the L1/L2 layers are derived the same way the real migration
    derives them, so the synthetic rows introduce no lag class of their own.
    """
    from evolvmem.context_migration import LegacyMemoryMigrator

    migrator = LegacyMemoryMigrator(service.store, service.config)
    layers = migrator.layers_for(value, ContextContentType.SESSION_SUMMARY)
    item = service.store.create_item(ContextItemDraft(
        identity_key=identity_key,
        content_type=ContextContentType.SESSION_SUMMARY,
        layers=ContextLayers(
            l0=layers.l0, l1=layers.l1, l2=layers.l2, generator='test-suite'),
        status=ContextStatus.CANDIDATE,
    ))
    with service.store.transaction():
        conn = service.store._connection()
        cursor = conn.execute(
            "INSERT INTO memories(key,value,status,attribute,tags,source_session,supersedes,"
            "importance,tier,expires_at,created_at,updated_at) "
            "VALUES(?,?,'active','','','',NULL,5.0,'normal',NULL,?,?)",
            (identity_key, value, _NOW, _NOW))
        conn.execute(
            'INSERT INTO legacy_memory_migrations(legacy_memory_id,context_item_id,migrated_at) '
            'VALUES(?,?,?)', (cursor.lastrowid, item.id, _NOW))
    return item.id


def _make_unsuperseded_candidate(service):
    """A mapped candidate owned by no organization task: unattributed drift."""
    return _make_candidate_with_legacy(service, identity_key='recovery:unrelated:candidate')


def _make_native_candidate(service, *, identity_key):
    """A Context-native candidate with no legacy projection at all."""
    return service.store.create_item(ContextItemDraft(
        identity_key=identity_key,
        content_type=ContextContentType.FACT,
        layers=ContextLayers(l0='native candidate', l1='d', l2='s', generator='test-suite'),
        status=ContextStatus.CANDIDATE,
    )).id


# ---- the two provable shapes, with the drift injected -----------------------


def test_injected_superseded_drift_is_the_only_projection_lag(service, monkeypatch):
    """Explicit drift: candidate Context items, active legacy rows, nothing else."""
    task_id, item_ids = make_drifted_projection(service, monkeypatch)
    drifted = drifted_items(service, item_ids)
    assert set(drifted) == set(item_ids)
    report = check_projection_lag(service.config, service.store)
    assert report.status_mismatch == len(drifted)
    assert report.projection_lag == report.status_mismatch
    assert report.missing_mapping == 0
    classification = classify_projection_status_drift(service.store)
    assert set(classification.eligible_item_ids) == set(item_ids)
    assert classification.superseded_count == len(item_ids)
    assert classification.retracted_count == 0
    assert classification.human_confirmed_count == 0
    assert classification.live_owner_count == 0
    assert classification.other_drift_count == 0


def test_injected_retraction_drift_is_repairable_without_assigning_a_project(
        service, monkeypatch):
    """The automatic unassign shape: candidate, empty project, legacy active."""
    task_id, item_id = make_retracted_projection(service, monkeypatch)
    classification = classify_projection_status_drift(service.store)
    assert classification.eligible_item_ids == (item_id,)
    assert classification.superseded_count == 0
    assert classification.retracted_count == 1
    assert classification.other_drift_count == 0

    report = apply_projection_status_recovery(service.config, service.store)

    assert report.status == 'applied'
    assert report.updated_item_ids == (item_id,)
    assert _legacy_status(service, item_id) == 'candidate'
    item = service.store.get_item(item_id)
    assert item.status is ContextStatus.CANDIDATE, 'never re-activated'
    assert item.project == '', 'the retraction stays unassigned'
    assert check_projection_lag(service.config, service.store).status_mismatch == 0


# ---- diagnosis is read-only --------------------------------------------------


def test_diagnosis_is_read_only_and_reports_only_counts_and_ids(service, monkeypatch):
    task_id, item_ids = make_drifted_projection(service, monkeypatch)
    drifted = set(drifted_items(service, item_ids))
    before = check_projection_lag(service.config, service.store).status_mismatch
    assert before == len(drifted) > 0

    diagnosis = diagnose_projection_status(service.config, service.store)

    assert diagnosis.status == 'drift'
    assert diagnosis.eligible_count == len(drifted)
    assert set(diagnosis.eligible_item_ids) == drifted
    assert diagnosis.superseded_count == len(drifted)
    assert diagnosis.retracted_count == 0
    assert diagnosis.other_drift_count == 0
    assert diagnosis.human_confirmed_count == 0
    assert diagnosis.live_owner_count == 0
    assert diagnosis.legacy_updated == 0
    # A diagnosis never writes.
    assert check_projection_lag(service.config, service.store).status_mismatch == before
    public = diagnosis.public_dict()
    assert public['status'] == 'drift'
    assert public['eligible_count'] == len(drifted)
    assert sorted(public['eligible_item_ids']) == sorted(drifted)
    assert public['updated_item_ids'] == []
    assert 'content_type' not in public and 'l0' not in public


def test_clean_database_diagnoses_without_changes(service):
    diagnosis = diagnose_projection_status(service.config, service.store)

    assert diagnosis.status == 'clean'
    assert diagnosis.eligible_count == 0
    assert diagnosis.other_drift_count == 0
    assert diagnose_projection_status(service.config, service.store).eligible_item_ids == ()


def test_context_native_candidates_are_not_projection_drift(service):
    """An unmapped Context candidate has no projection to disagree with."""
    for index in range(3):
        _make_native_candidate(service, identity_key=f'native:candidate:{index}')

    classification = classify_projection_status_drift(service.store)

    assert classification.status == 'clean'
    assert classification.eligible_count == 0
    assert classification.other_drift_count == 0
    assert classification.human_confirmed_count == 0
    assert classification.live_owner_count == 0
    assert check_projection_lag(service.config, service.store).status_mismatch == 0


# ---- apply is narrow, transactional and idempotent ---------------------------


def test_apply_synchronizes_only_proven_automatic_outputs(service):
    """Two repairable items against four exclusion cases in one batch."""
    repairable = [
        _make_candidate_with_legacy(service, identity_key=f'recovery:repair:{index}')
        for index in range(2)
    ]
    for item_id in repairable:
        _add_superseded_owner(service, item_id, ordinal=item_id)
    human_item = _make_candidate_with_legacy(service, identity_key='recovery:human')
    _add_superseded_owner(service, human_item, ordinal=100)
    _set_human_resolution(service, human_item)
    already = _make_candidate_with_legacy(service, identity_key='recovery:already')
    _add_superseded_owner(service, already, ordinal=101)
    _set_legacy_already_candidate(service, already)
    live_owned = _make_candidate_with_legacy(service, identity_key='recovery:live')
    _add_superseded_owner(service, live_owned, ordinal=102)
    _add_live_owner(service, live_owned)
    unrelated = _make_unsuperseded_candidate(service)
    baseline = check_projection_lag(service.config, service.store)
    assert baseline.status_mismatch == 5  # 2 repairable + human + live owner + unattributed

    diagnosis = diagnose_projection_status(service.config, service.store)
    assert set(diagnosis.eligible_item_ids) == set(repairable)
    assert diagnosis.human_confirmed_count == 1
    assert diagnosis.live_owner_count == 1
    assert diagnosis.other_drift_count == 1

    report = apply_projection_status_recovery(service.config, service.store)

    assert report.status == 'applied'
    assert report.legacy_updated == len(repairable)
    assert set(report.updated_item_ids) == set(repairable)
    assert report.human_confirmed_count == 1
    assert report.live_owner_count == 1
    assert report.other_drift_count == 1  # the unrelated candidate is reported only
    for item_id in repairable:
        assert _legacy_status(service, item_id) == 'candidate'
    # Excluded cases never move.
    assert _legacy_status(service, human_item) == 'active'
    assert _legacy_status(service, live_owned) == 'active'
    assert _legacy_status(service, already) == 'candidate'
    assert _legacy_status(service, unrelated) == 'active'
    remaining = check_projection_lag(service.config, service.store)
    assert remaining.status_mismatch == 3  # human + live owner + unattributed
    assert baseline.status_mismatch - remaining.status_mismatch == len(repairable)
    # Candidates stay candidates: nothing is re-activated.
    for item_id in (*repairable, human_item, live_owned, already, unrelated):
        assert service.store.get_item(item_id).status is ContextStatus.CANDIDATE
    assert all(isinstance(i, int) for i in report.public_dict()['updated_item_ids'])


def test_apply_skips_an_item_whose_decision_changed_after_the_scan(service, monkeypatch):
    """The write re-checks every predicate inside its own transaction."""
    task_id, item_ids = make_drifted_projection(service, monkeypatch)
    target = item_ids[0]
    other = item_ids[1]

    real_apply = apply_projection_status_recovery
    from evolvmem import recall_recovery

    real_classify = recall_recovery.classify_projection_status_drift
    seen = {}

    def classifying_then_deciding(store):
        report = real_classify(store)
        if 'done' not in seen:
            seen['done'] = True
            # A human decision lands after the scan but before the write.
            _set_human_resolution(service, target)
        return report

    monkeypatch.setattr(
        recall_recovery, 'classify_projection_status_drift', classifying_then_deciding)
    result = real_apply(service.config, service.store)

    assert set(result.updated_item_ids) == {other}
    assert _legacy_status(service, target) == 'active'
    assert _legacy_status(service, other) == 'candidate'


def test_repeated_apply_is_idempotent(service, monkeypatch):
    task_id, item_ids = make_drifted_projection(service, monkeypatch)
    drifted = set(drifted_items(service, item_ids))
    first_report = apply_projection_status_recovery(service.config, service.store)
    assert first_report.legacy_updated == len(drifted) > 0

    second_report = apply_projection_status_recovery(service.config, service.store)

    assert second_report.status == 'clean'
    assert second_report.eligible_count == 0
    assert second_report.legacy_updated == 0
    assert second_report.updated_item_ids == ()
    assert check_projection_lag(service.config, service.store).status_mismatch == 0


def test_apply_never_reactivates_a_candidate_context_item(service):
    """A legacy-active row without a provable source is only ever diagnosed."""
    item = _make_candidate_with_legacy(service, identity_key='recovery:never:reactivate')

    report = apply_projection_status_recovery(service.config, service.store)

    assert report.status == 'unattributed_drift'
    assert report.other_drift_count == 1
    assert report.legacy_updated == 0
    assert service.store.get_item(item).status is ContextStatus.CANDIDATE
    assert _legacy_status(service, item) == 'active'


def test_legacy_status_repair_rolls_back_when_the_transaction_fails(service, monkeypatch):
    task_id, item_ids = make_drifted_projection(service, monkeypatch)
    targets = drifted_items(service, item_ids)
    assert len(targets) >= 2
    from evolvmem import recall_recovery

    real_set_status = recall_recovery.LegacyProjectionRepository.set_status
    calls = {'n': 0}

    def flaky_set_status(self, legacy_id, status):
        calls['n'] += 1
        if calls['n'] == 2:
            raise RuntimeError('injected projection failure')
        return real_set_status(self, legacy_id, status)

    monkeypatch.setattr(
        recall_recovery.LegacyProjectionRepository, 'set_status', flaky_set_status)

    with pytest.raises(RuntimeError):
        apply_projection_status_recovery(service.config, service.store)

    for item_id in targets:
        assert _legacy_status(service, item_id) == 'active'
    assert check_projection_lag(service.config, service.store).status_mismatch == len(targets)


# ---- bounded Context vector recovery ----------------------------------------


class CountingEngine:
    """Deterministic document encoder that records every encoded text."""

    is_loaded = True

    def __init__(self, dim=3):
        self.dim = dim
        self.documents: list[str] = []

    def encode_document(self, text):
        self.documents.append(text)
        return self._vector(text)

    def encode_query(self, text):
        return self._vector(text)

    def _vector(self, text):
        vector = np.zeros(self.dim, dtype=np.float32)
        for token in str(text).split():
            vector[sum(token.encode('utf-8')) % self.dim] += 1.0
        if not vector.any():
            vector[0] = 1.0
        return vector


class FailingEngine:
    is_loaded = True

    def encode_document(self, _text):
        raise RuntimeError('synthetic encode failure')


def _make_store_item(test_config, store, *, identity_key, l0, project='', confidence=0.8):
    return store.create_item(ContextItemDraft(
        identity_key=identity_key,
        content_type=ContextContentType.FACT,
        project=project,
        confidence=confidence,
        layers=ContextLayers(l0=l0, l1='detail', l2='source', generator='test-suite'),
        status=ContextStatus.ACTIVE,
    ))


def test_recovery_rebuilds_a_dirty_context_cache_and_clears_the_marker(test_config):
    from evolvmem.recall_recovery import recover_context_vector

    test_config.embedding_dim = 3
    engine = CountingEngine()
    with ContextStore(test_config) as store:
        item = _make_store_item(test_config, store, identity_key='recover:one', l0='recoverable l0')
        index = VectorIndex(test_config, path=test_config.context_vector_path)
        index.initialize(dim=3)
        index.mark_dirty()

        report = recover_context_vector(test_config, store, index, engine)

        assert report.status == 'recovered'
        assert report.dirty is False
        assert report.document_count == 1
        assert index.is_dirty() is False
        assert index.count() == 1
        assert index.ids() == [item.id]
        assert engine.documents == ['recoverable l0']
        index.close()


def test_recovery_is_a_noop_when_the_cache_is_not_dirty(test_config):
    from evolvmem.recall_recovery import recover_context_vector

    test_config.embedding_dim = 3
    engine = CountingEngine()
    with ContextStore(test_config) as store:
        _make_store_item(test_config, store, identity_key='clean:one', l0='clean l0')
        index = VectorIndex(test_config, path=test_config.context_vector_path)
        index.initialize(dim=3)

        report = recover_context_vector(test_config, store, index, engine)

        assert report.status == 'clean'
        assert report.attempted is False
        assert engine.documents == []
        index.close()


def test_recovery_without_a_loaded_engine_keeps_dirty_and_the_old_index(test_config):
    from evolvmem.recall_recovery import recover_context_vector

    test_config.embedding_dim = 3
    engine = CountingEngine()
    with ContextStore(test_config) as store:
        _make_store_item(test_config, store, identity_key='keep:old', l0='old l0')
        index = VectorIndex(test_config, path=test_config.context_vector_path)
        index.initialize(dim=3)
        index.add(999, np.array([1.0, 0.0, 0.0], dtype=np.float32))
        index.save()
        index.clear_dirty()
        index.mark_dirty()

        report = recover_context_vector(test_config, store, index, None)

        assert report.status == 'unavailable'
        assert report.reason_codes == ('engine_unavailable',)
        assert index.is_dirty() is True
        assert index.ids() == [999], 'the previous usable cache must survive'

        reopened = VectorIndex(test_config, path=test_config.context_vector_path)
        reopened.initialize(dim=3)
        assert reopened.ids() == [999]
        reopened.close()
        index.close()


def test_recovery_encode_failure_keeps_dirty_and_the_formal_file(test_config):
    from evolvmem.recall_recovery import recover_context_vector

    test_config.embedding_dim = 3
    with ContextStore(test_config) as store:
        _make_store_item(test_config, store, identity_key='fail:one', l0='sensitive l0')
        index = VectorIndex(test_config, path=test_config.context_vector_path)
        index.initialize(dim=3)
        index.add(999, np.array([1.0, 0.0, 0.0], dtype=np.float32))
        index.save()
        index.clear_dirty()
        index.mark_dirty()

        report = recover_context_vector(test_config, store, index, FailingEngine())

        assert report.status == 'failed'
        assert report.reason_codes == ('stage_failed',)
        assert index.is_dirty() is True
        assert index.ids() == [999]

        reopened = VectorIndex(test_config, path=test_config.context_vector_path)
        reopened.initialize(dim=3)
        assert reopened.ids() == [999]
        reopened.close()
        index.close()


def test_recovery_rejects_an_index_pointing_at_the_legacy_cache(test_config):
    from evolvmem.recall_recovery import recover_context_vector

    with ContextStore(test_config) as store:
        legacy_index = VectorIndex(test_config)
        legacy_index.initialize(dim=test_config.embedding_dim)
        legacy_index.mark_dirty()

        report = recover_context_vector(test_config, store, legacy_index, CountingEngine())

        assert report.status == 'failed'
        assert report.reason_codes == ('index_path_mismatch',)
        assert legacy_index.is_dirty() is True
        legacy_index.close()


def test_recovery_keeps_dirty_when_sqlite_truth_moves_during_encoding(test_config):
    """A write during encoding invalidates the snapshot that was encoded."""
    from evolvmem.recall_recovery import recover_context_vector

    test_config.embedding_dim = 3

    class WritingEngine(CountingEngine):
        def __init__(self, store):
            super().__init__()
            self.store = store

        def encode_document(self, text):
            if not self.documents:
                # The first encode reproduces "a new write lands mid-rebuild".
                _make_store_item(
                    test_config, self.store, identity_key='late:writer', l0='late l0')
                # The writer also updates its own Context vector, as production
                # does after a commit: the marker stays until a rebuild agrees.
            return super().encode_document(text)

    with ContextStore(test_config) as store:
        _make_store_item(test_config, store, identity_key='early:one', l0='early l0')
        index = VectorIndex(test_config, path=test_config.context_vector_path)
        index.initialize(dim=3)
        index.mark_dirty()

        report = recover_context_vector(test_config, store, index, WritingEngine(store))

        assert report.status == 'failed'
        assert report.reason_codes == ('source_changed_during_rebuild',)
        assert report.dirty is True
        assert index.is_dirty() is True

        # The next attempt sees stable truth and finally recovers.
        stable = recover_context_vector(test_config, store, index, CountingEngine())
        assert stable.status == 'recovered'
        assert index.is_dirty() is False
        assert index.ids() == sorted(
            doc.item_id for doc in store.list_vector_documents())
        index.close()


def test_recovery_loop_defers_until_the_interval_elapses(test_config):
    from evolvmem.recall_recovery import ContextVectorRecoveryLoop

    class Clock:
        def __init__(self):
            self.now = 0.0

        def __call__(self):
            return self.now

        def advance(self, seconds):
            self.now += seconds

    test_config.embedding_dim = 3
    clock = Clock()
    engine = CountingEngine()
    loop = ContextVectorRecoveryLoop(clock=clock)
    assert loop.interval_seconds >= 60.0

    class FakeService:
        pass

    with ContextStore(test_config) as store:
        _make_store_item(test_config, store, identity_key='loop:one', l0='loop l0')
        index = VectorIndex(test_config, path=test_config.context_vector_path)
        index.initialize(dim=3)
        index.mark_dirty()
        service = FakeService()
        service.config = test_config
        service.store = store
        service.vector_index = index
        service.embedding_engine = engine

        first = loop.tick([service])
        assert [report.status for report in first] == ['recovered']
        assert engine.documents == ['loop l0']

        # Every later poll within the interval must not rebuild again.
        clock.advance(5.0)
        assert loop.tick([service]) == ()
        clock.advance(5.0)
        assert loop.tick([service]) == ()
        assert engine.documents == ['loop l0']
        assert index.is_dirty() is False
        index.close()


def test_recovery_loop_backs_off_after_a_failure(test_config):
    from evolvmem.recall_recovery import ContextVectorRecoveryLoop

    class Clock:
        def __init__(self):
            self.now = 0.0

        def __call__(self):
            return self.now

        def advance(self, seconds):
            self.now += seconds

    test_config.embedding_dim = 3
    clock = Clock()
    failing = FailingEngine()
    loop = ContextVectorRecoveryLoop(clock=clock, failure_backoff_seconds=120.0)

    class FakeService:
        pass

    with ContextStore(test_config) as store:
        _make_store_item(test_config, store, identity_key='backoff:one', l0='backoff l0')
        index = VectorIndex(test_config, path=test_config.context_vector_path)
        index.initialize(dim=3)
        index.mark_dirty()
        service = FakeService()
        service.config = test_config
        service.store = store
        service.vector_index = index
        service.embedding_engine = failing

        assert [r.status for r in loop.tick([service])] == ['failed']
        assert loop.consecutive_failures == 1
        assert index.is_dirty() is True

        # One failure defers the next attempt by failure_backoff_seconds, not by
        # the base interval: every later poll inside the backoff is silent.
        clock.advance(loop.interval_seconds + 1.0)
        assert loop.tick([service]) == ()
        assert loop.consecutive_failures == 1

        clock.advance(loop.failure_backoff_seconds)
        assert [r.status for r in loop.tick([service])] == ['failed']
        assert loop.consecutive_failures == 2
        index.close()


def test_recovery_loop_skips_a_library_above_the_document_budget(test_config):
    from evolvmem.recall_recovery import ContextVectorRecoveryLoop

    test_config.embedding_dim = 3
    loop = ContextVectorRecoveryLoop(document_budget=1)

    class FakeService:
        pass

    with ContextStore(test_config) as store:
        _make_store_item(test_config, store, identity_key='budget:one', l0='one')
        _make_store_item(test_config, store, identity_key='budget:two', l0='two')
        index = VectorIndex(test_config, path=test_config.context_vector_path)
        index.initialize(dim=3)
        index.mark_dirty()
        service = FakeService()
        service.config = test_config
        service.store = store
        service.vector_index = index
        service.embedding_engine = CountingEngine()

        reports = loop.tick([service])

        assert [r.status for r in reports] == ['deferred']
        assert reports[0].reason_codes == ('document_budget_exceeded',)
        assert index.is_dirty() is True
        index.close()


def test_recovery_loop_rejects_intervals_below_one_minute():
    from evolvmem.recall_recovery import ContextVectorRecoveryLoop

    with pytest.raises(ValueError, match='once a minute'):
        ContextVectorRecoveryLoop(interval_seconds=2.0)


def test_recovery_loop_leaves_a_clean_service_alone(test_config):
    from evolvmem.recall_recovery import ContextVectorRecoveryLoop

    test_config.embedding_dim = 3
    loop = ContextVectorRecoveryLoop()
    engine = CountingEngine()

    class FakeService:
        pass

    with ContextStore(test_config) as store:
        _make_store_item(test_config, store, identity_key='quiet:one', l0='quiet l0')
        index = VectorIndex(test_config, path=test_config.context_vector_path)
        index.initialize(dim=3)
        service = FakeService()
        service.config = test_config
        service.store = store
        service.vector_index = index
        service.embedding_engine = engine

        assert loop.tick([service]) == ()
        assert engine.documents == []
        index.close()


# ---- recovery restores serving, and the resident cache follows -------------


def test_primary_becomes_ready_and_new_knowledge_is_retrievable_after_repair(
        service, monkeypatch):
    """End to end: the stuck gate clears and real retrieval works again."""
    from evolvmem.context_models import ContextMode, ContextSearchRequest
    from evolvmem.recall_recovery import recover_context_vector

    task_id, item_ids = make_drifted_projection(service, monkeypatch)
    assert drifted_items(service, item_ids)
    # A loaded shared engine and an open Context cache, exactly what the LAN
    # runtime injects before any request is served.
    service.embedding_engine = CountingEngine(dim=service.config.embedding_dim)
    service.retriever.embedding_engine = service.embedding_engine
    service.vector_index.initialize(dim=service.config.embedding_dim)
    service._mode = ContextMode.PRIMARY
    service._refresh_health()
    assert service.status().ready is False
    assert service.status().reason_codes == ('degraded_legacy',)
    assert 'projection_lag_nonzero' in service.status().diagnostics

    apply_projection_status_recovery(service.config, service.store)
    recovery = recover_context_vector(
        service.config, service.store, service.vector_index, service.embedding_engine)
    assert recovery.status == 'recovered'
    service._refresh_health()

    status = service.status()
    assert status.ready is True
    assert status.reason_codes == ()
    assert status.projection_lag == 0
    assert status.context_vector_dirty is False
    assert status.diagnostics == ()

    # New material that lands after the repair is retrievable through the very
    # same Primary retrieval path that was blocked.
    item = _make_store_item(
        service.config, service.store, identity_key='after:repair:knowledge',
        l0='recovered retrieval marker 20261007', project='evo')
    assert item.id in {h.item_id for h in service.store.search_fts('recovered')}
    results = service.search(ContextSearchRequest(
        query='recovered retrieval marker', project='evo'))
    assert item.id in {result.id for result in results}


def test_resident_context_cache_sees_the_rebuilt_file(test_config):
    """A resident LAN cache object must reload the atomically swapped file."""
    from evolvmem.recall_recovery import recover_context_vector

    test_config.embedding_dim = 3
    test_config.lan_shared_vector_cache = True
    resident = VectorIndex(test_config, path=test_config.context_vector_path)
    resident.initialize(dim=3)
    assert resident.count() == 0
    assert resident._index is not None, 'the resident cache is already open'

    with ContextStore(test_config) as store:
        item = _make_store_item(test_config, store, identity_key='resident:one', l0='resident l0')
        writer = VectorIndex(test_config, path=test_config.context_vector_path)
        writer.initialize(dim=3)
        writer.mark_dirty()
        report = recover_context_vector(test_config, store, writer, CountingEngine())
        assert report.status == 'recovered'
        writer.close()

    # The already-open resident object is not restarted: its next read refreshes
    # from the swapped file because the stamp changed.
    assert resident.count() == 1
    assert resident.ids() == [item.id]
    resident.close()


def test_recovery_keeps_dirty_when_the_atomic_swap_fails(test_config, monkeypatch):
    """A failed swap must never clear the marker or damage the live cache."""
    from evolvmem import cutover_vector
    from evolvmem.recall_recovery import recover_context_vector

    test_config.embedding_dim = 3

    def failing_replace(_src, _dst):
        raise OSError('injected replace failure')

    monkeypatch.setattr(cutover_vector.os, 'replace', failing_replace)
    with ContextStore(test_config) as store:
        _make_store_item(test_config, store, identity_key='swap:fail', l0='swap l0')
        index = VectorIndex(test_config, path=test_config.context_vector_path)
        index.initialize(dim=3)
        index.add(999, np.array([1.0, 0.0, 0.0], dtype=np.float32))
        index.save()
        index.clear_dirty()
        index.mark_dirty()

        report = recover_context_vector(test_config, store, index, CountingEngine())

        assert report.status == 'failed'
        assert report.dirty is True
        assert index.is_dirty() is True
        reopened = VectorIndex(test_config, path=test_config.context_vector_path)
        reopened.initialize(dim=3)
        assert reopened.ids() == [999]
        reopened.close()
        index.close()


def test_recovery_thread_uses_the_shared_connection_without_blocking_requests(
        tmp_path):
    """The real worker thread, the real shared connection, concurrent readers.

    The reviewer's boundary: recovery runs on the very connection the request
    path uses, the runtime's own thread performs it, and the status stays
    readable while a large library is encoded. SQLite is opened with
    ``check_same_thread=False`` and the store documents a bounded background
    worker sharing it, so this test proves the real path, not a mock.
    """
    import threading
    import time

    from evolvmem.lan_config import LanSettings
    from evolvmem.lan_runtime import LanRuntime

    class Engine:
        is_loaded = False

        def __init__(self, dim):
            self._dim = dim

        def initialize(self):
            self.is_loaded = True

        def encode_document(self, _text):
            return [1.0] + [0.0] * (self._dim - 1)

        def encode_query(self, _text):
            return self.encode_document('')

        def close(self):
            return None

    settings = LanSettings(
        data_dir=tmp_path / 'lan-data',
        owner_data_dir=tmp_path / 'owner-data',
        token_hashes={'jiangli': 'a' * 64, 'kane': 'b' * 64},
        embedding_enabled=True,
    )
    runtime = LanRuntime(settings, Engine(768))
    runtime.initialize()
    try:
        server = runtime.server_for('jiangli')
        service = server.context_service
        store = service.store
        for index in range(200):
            store.create_item(ContextItemDraft(
                identity_key=f'lan:bulk:{index}',
                content_type=ContextContentType.FACT,
                confidence=0.8,
                layers=ContextLayers(
                    l0=f'bulk recoverable l0 {index}', l1='d', l2='s',
                    generator='test-suite'),
                status=ContextStatus.ACTIVE,
            ))

        # The runtime already owns a recovery thread; this test starts from a
        # clean cache so the assertion below proves that thread did the work.
        index_handle = service.vector_index
        index_handle.initialize(dim=service.config.embedding_dim)
        assert index_handle.is_dirty() is False
        report = server.handle_tool_call('context_status', {})
        assert report['ready'] is True
        assert report['context_vector_dirty'] is False
        assert service.retriever._vector_available() is False  # empty cache

        # A reader thread uses the same connection continuously, exactly as a
        # request thread does, for the whole recovery round.
        observed: list[str] = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    status = server.handle_tool_call('context_status', {})
                    observed.append(str(status.get('context_vector_dirty')))
                    service.store.search_fts('bulk')
                except Exception as exc:  # pragma: no cover - failure detail
                    observed.append(f'error:{exc.__class__.__name__}')

        reader_thread = threading.Thread(target=reader, daemon=True)
        reader_thread.start()
        index_handle.mark_dirty()

        started = time.monotonic()
        deadline = time.monotonic() + 60
        recovery = ()
        while time.monotonic() < deadline:
            recovery = getattr(runtime, '_vector_recovery_reports', ())
            if recovery:
                break
            time.sleep(0.05)
        elapsed = time.monotonic() - started
        stop.set()
        reader_thread.join(timeout=5)

        assert recovery, 'the runtime recovery thread must run one round'
        assert [r.status for r in recovery] == ['recovered']
        assert index_handle.is_dirty() is False
        assert index_handle.ids() == [item.item_id
                                      for item in store.list_vector_documents()]
        # A 200-document local rebuild finishes well inside one poll cycle; the
        # real model dominates production time, so a slower round is visible
        # here rather than hidden.
        assert elapsed < 30, f'recovery round took {elapsed:.1f}s'
        assert not any(entry.startswith('error:') for entry in observed), observed[:5]
        assert observed.count('True') >= 1, observed[:5]
        assert observed[-1] == 'False'
        assert service.retriever._vector_available() is True
    finally:
        runtime.close()


def test_runtime_recovery_thread_recovers_a_dirty_cache_without_capture(tmp_path):
    """The runtime's own recovery thread runs with no capture adapter at all."""
    import time

    from evolvmem.lan_config import LanSettings
    from evolvmem.lan_runtime import LanRuntime
    from evolvmem.context_models import ContextItemDraft as Draft
    from evolvmem.context_models import ContextLayers as Layers

    class Engine:
        is_loaded = False

        def __init__(self, dim):
            self._dim = dim

        def initialize(self):
            self.is_loaded = True

        def encode_document(self, _text):
            return [1.0] + [0.0] * (self._dim - 1)

        def encode_query(self, _text):
            return self.encode_document('')

        def close(self):
            return None

    settings = LanSettings(
        data_dir=tmp_path / 'lan-data',
        owner_data_dir=tmp_path / 'owner-data',
        token_hashes={'jiangli': 'a' * 64, 'kane': 'b' * 64},
        embedding_enabled=True,
    )
    runtime = LanRuntime(settings, Engine(768))
    runtime.initialize()
    try:
        server = runtime.server_for('jiangli')
        service = server.context_service
        store = service.store
        item = store.create_item(Draft(
            identity_key='lan:loop:recover',
            content_type=ContextContentType.FACT,
            layers=Layers(l0='lan loop l0', l1='d', l2='s', generator='test-suite'),
            status=ContextStatus.ACTIVE,
        ))
        index = service.vector_index
        index.initialize(dim=service.config.embedding_dim)
        index.mark_dirty()
        assert index.is_dirty() is True
        recovery = None
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            reports = getattr(runtime, '_vector_recovery_reports', ())
            if reports:
                recovery = reports
                break
            time.sleep(0.05)
        assert recovery, 'the runtime recovery thread must run one bounded round'
        assert recovery[0].status == 'recovered'
        assert index.is_dirty() is False
        assert index.ids() == [item.id]
    finally:
        runtime.close()


def test_diagnosis_on_an_unreadable_schema_reports_a_code_not_a_raise(test_config):
    """A probe must always print a verdict, even on a broken namespace."""
    from evolvmem.memory_store import MemoryStore
    from evolvmem.recall_recovery import diagnose_projection_status

    with MemoryStore(test_config):
        pass  # the legacy projection schema exists, as in production
    with ContextStore(test_config) as store:
        store._connection().execute('DROP TABLE memories')
        store._connection().commit()
        report = diagnose_projection_status(test_config, store)

    assert report.status == 'failed'
    assert report.error_code == 'OperationalError'
    assert report.eligible_count == 0
    assert report.public_dict()['status'] == 'failed'


# ---- the reported mixed shape: 4 superseded + 1 retracted -------------------


def test_reported_mixed_shape_is_repaired_class_by_class(service, monkeypatch):
    """Mirror of the reported state: only status_mismatch, everything else zero.

    The reported five-row shape: four superseded automatic outputs plus one
    automatic unassign retraction. Two rows come from the real pipeline (one
    later retracted through the real ``_unresolve_item``) and three are
    synthetic superseded outputs; every drifted legacy row is injected
    explicitly, so the fixture does not depend on the double-write defect.
    """
    from evolvmem.auto_organization import _unresolve_item

    task_id, item_ids = run_real_first_task(service, monkeypatch, session='mixed-session')
    superseded_ids = [item_ids[0]]
    retracted_id = item_ids[1]
    supersede_task(service, task_id)
    _unresolve_item(service, retracted_id, '归属未确认，先撤回')
    for index in range(3):
        synthetic = _make_candidate_with_legacy(
            service, identity_key=f'mixed:superseded:{index}')
        _add_superseded_owner(service, synthetic, ordinal=200 + index)
        superseded_ids.append(synthetic)
    inject_legacy_active_drift(service, superseded_ids + [retracted_id])

    drifted = set(drifted_items(service, superseded_ids + [retracted_id]))
    assert drifted == set(superseded_ids) | {retracted_id}
    conn = service.store._connection()
    kinds = {
        row['id']: row['content_type'] for row in conn.execute(
            'SELECT id, content_type FROM context_items')
    }
    assert kinds[retracted_id] == 'session_summary'
    assert service.store.get_item(retracted_id).project == ''

    before = check_projection_lag(service.config, service.store)
    assert before.projection_lag == before.status_mismatch == len(drifted)
    assert before.missing_mapping == 0
    assert before.duplicate_mapping_target == 0
    assert before.layer_mismatch == 0
    assert before.l1_mismatch == 0
    assert before.supersession_mismatch == 0
    assert before.orphan_mapping == 0
    assert before.dangling_item_mapping == 0

    diagnosis = diagnose_projection_status(service.config, service.store)
    assert set(diagnosis.eligible_item_ids) == drifted
    assert diagnosis.eligible_count == 5
    assert diagnosis.superseded_count == 4
    assert diagnosis.retracted_count == 1
    assert diagnosis.human_confirmed_count == 0
    assert diagnosis.live_owner_count == 0
    assert diagnosis.other_drift_count == 0

    report = apply_projection_status_recovery(service.config, service.store)

    assert report.status == 'applied'
    assert report.legacy_updated == 5
    after = check_projection_lag(service.config, service.store)
    assert after.projection_lag == 0
    assert after.status_mismatch == 0
    # The retraction keeps its unassigned candidate status.
    item = service.store.get_item(retracted_id)
    assert item.status is ContextStatus.CANDIDATE
    assert item.project == ''
    assert _legacy_status(service, retracted_id) == 'candidate'


def test_a_native_candidate_is_never_touched_by_a_repair(service, monkeypatch):
    """Context-native candidates are not projection drift and stay untouched."""
    native = _make_native_candidate(service, identity_key='native:stays')
    task_id, item_ids = make_drifted_projection(service, monkeypatch, session='native-session')

    diagnosis = diagnose_projection_status(service.config, service.store)
    assert set(diagnosis.eligible_item_ids) == set(item_ids)
    assert diagnosis.other_drift_count == 0

    apply_projection_status_recovery(service.config, service.store)

    assert _legacy_row(service, native) is None
    assert service.store.get_item(native).status is ContextStatus.CANDIDATE


# ---- real MCP initialization: tools/list, context_status, gated recall ------


def test_real_mcp_initialization_recovers_the_gate_without_a_restart(test_config):
    """A fully initialized server regains its tools after recovery.

    Only the optional model is faked; the server, the service, the projection
    evaluator, the tool registry, and the per-request health re-check are the
    real ones. No cached ``_ready`` value or stubbed vector index is involved.
    """
    from evolvmem.memory_store import MemoryStore
    from evolvmem.mcp_server import MemoryMCPServer
    from evolvmem.recall_recovery import recover_context_vector

    class Engine:
        is_loaded = False

        def __init__(self, dim):
            self._dim = dim

        def initialize(self):
            self.is_loaded = True

        def encode_document(self, _text):
            return [1.0] + [0.0] * (self._dim - 1)

        def encode_query(self, _text):
            return self.encode_document('')

        def close(self):
            return None

    test_config.context_mode = 'primary'
    test_config.adapter = 'codex'
    with MemoryStore(test_config):
        pass  # the legacy projection schema exists, as in production
    engine = Engine(768)
    server = MemoryMCPServer(config=test_config, embedding_engine=engine)
    server.initialize()
    # A borrowed engine is owned by its caller, exactly as LanRuntime owns and
    # loads the one shared model; the server never loads it a second time.
    engine.initialize()
    server._init_done.set()
    try:
        def tool_names():
            return {
                tool['name']
                for tool in server._handle_request(
                    {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['result']['tools']
            }

        healthy_tools = tool_names()
        healthy = server.handle_tool_call('context_status', {})
        assert healthy['mode'] == 'primary'
        assert healthy['ready'] is True
        assert healthy['context_vector_dirty'] is False

        # Real write, then a real stuck dirty marker on the live index.
        added = server.handle_tool_call(
            'memory_add', {'key': 'project:evo:fact:mcp', 'value': 'mcp recovery marker'})
        assert added['status'] == 'added'
        service = server.context_service
        index = service.vector_index
        # Record the real post-write state, then force the stuck marker this
        # test is about (a legal write with no engine, or a failed encode).
        observed_dirty_after_write = index.is_dirty()
        assert service.embedding_engine is server.engine
        assert server.engine.is_loaded is True
        assert observed_dirty_after_write is False, (
            'a legal write with a loaded shared engine must not leave the '
            'Context cache dirty')
        index.mark_dirty()

        degraded = server.handle_tool_call('context_status', {})
        assert degraded['ready'] is False
        assert degraded['context_vector_dirty'] is True
        assert degraded['reason_codes'] == ['degraded_legacy']
        assert service._ready is False, 'the live re-check must update the service flag'
        blocked_tools = tool_names()
        assert blocked_tools != healthy_tools, 'the registry must follow live health'
        # The registry shrinks to the diagnostic entry point, while legacy
        # search deliberately degrades to FTS instead of failing.
        assert 'context_status' in blocked_tools
        assert 'context_search' not in blocked_tools
        assert 'knowledge_recall' not in blocked_tools
        blocked_context = server.handle_tool_call(
            'context_search', {'query': 'mcp recovery marker'})
        assert 'error' in blocked_context
        degraded_read = server.handle_tool_call(
            'memory_search', {'query': 'mcp recovery marker'})
        assert set(degraded_read) == {'results', 'count'}

        report = recover_context_vector(
            test_config, service.store, index, service.embedding_engine)
        assert report.status == 'recovered'

        recovered = server.handle_tool_call('context_status', {})
        assert recovered['ready'] is True
        assert recovered['context_vector_dirty'] is False
        assert recovered['reason_codes'] == []
        assert service._ready is True
        recovered_tools = tool_names()
        assert recovered_tools == healthy_tools
        assert 'context_search' in recovered_tools
        found = server.handle_tool_call(
            'memory_search', {'query': 'mcp recovery marker'})
        assert 'error' not in found
        assert any('mcp recovery marker' in row.get('value', '')
                   for row in found.get('results', []))
        # The Core-side recall tool serves again through the same live server.
        core = server.handle_tool_call(
            'knowledge_recall', {'query': 'mcp recovery marker'})
        assert 'error' not in core
        assert type(observed_dirty_after_write) is bool
    finally:
        server.shutdown()


def test_diagnosis_rejects_a_store_from_a_different_database(test_config, temp_dir):
    from evolvmem.config import Config
    from evolvmem.recall_recovery import recover_context_vector

    other = Config(data_dir=temp_dir / 'other', apply_environment=False)
    with ContextStore(test_config) as store:
        with pytest.raises(ValueError, match='same database'):
            diagnose_projection_status(other, store)
        with pytest.raises(ValueError, match='same database'):
            apply_projection_status_recovery(other, store)
        index = VectorIndex(test_config, path=test_config.context_vector_path)
        with pytest.raises(ValueError, match='same database'):
            recover_context_vector(other, store, index, CountingEngine())
        index.close()


# ---- legitimate duplicate-active quarantine is not drift ---------------------


def _insert_legacy_row(service, *, key, value, status, attribute='fact', updated_at):
    with service.store.transaction():
        conn = service.store._connection()
        cursor = conn.execute(
            "INSERT INTO memories(key,value,status,attribute,tags,source_session,supersedes,"
            "importance,tier,expires_at,created_at,updated_at) "
            "VALUES(?,?,?,?,'','',NULL,5.0,'normal',NULL,?,?)",
            (key, value, status, attribute, updated_at, updated_at))
        return cursor.lastrowid


def _map_item(service, item_id, legacy_id):
    with service.store.transaction():
        service.store._connection().execute(
            'INSERT INTO legacy_memory_migrations(legacy_memory_id,context_item_id,migrated_at) '
            'VALUES(?,?,?)', (legacy_id, item_id, _NOW))


def _add_migration_source(service, item_id, *, extraction_version):
    with service.store.transaction():
        service.store._connection().execute(
            "INSERT INTO context_sources(item_id,archive_id,source_kind,source_ref,"
            "extraction_version,created_at) VALUES(?,NULL,'migration',?,?,?)",
            (item_id, f'legacy-v1:{item_id}', extraction_version, _NOW))


def _add_project_resolution(service, item_id, *, decision_source, resolution_state,
                            review_state, resolver_version):
    with service.store.transaction():
        service.store._connection().execute(
            "INSERT OR REPLACE INTO context_project_resolutions"
            '(item_id,resolution_state,decision_source,review_state,proposed_project,'
            'resolved_project,confidence,method,evidence_json,resolver_version,revision,'
            'reviewed_at,created_at,updated_at) '
            "VALUES(?,?,?,?,'','','none','','[]',?,1,NULL,?,?)",
            (item_id, resolution_state, decision_source, review_state,
             resolver_version, _NOW, _NOW))


def build_duplicate_active_migration(service, *, identity):
    """Two active legacy rows for one identity plus the quarantine marker.

    The losing row of the group is the migrator's documented duplicate-active
    quarantine: a Context candidate whose legacy row legitimately stays active.
    """
    from evolvmem.cutover_checks import duplicate_active_legacy_ids

    winner_legacy = _insert_legacy_row(
        service, key=identity, value=f'{identity} newest', status='active',
        updated_at='2026-01-02 00:00:00')
    loser_legacy = _insert_legacy_row(
        service, key=identity, value=f'{identity} older', status='active',
        updated_at='2026-01-01 00:00:00')
    losers = duplicate_active_legacy_ids(service.store.iter_legacy_rows())
    assert loser_legacy in losers and winner_legacy not in losers, 'test premise'
    winner = _make_native_candidate(service, identity_key='quarantine:winner')
    loser = _make_native_candidate(service, identity_key='quarantine:loser')
    _map_item(service, winner, winner_legacy)
    _map_item(service, loser, loser_legacy)
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE memories SET status='candidate' WHERE id=?", (winner_legacy,))
    _add_migration_source(service, loser, extraction_version='legacy-v1:duplicate-active')
    _add_migration_source(service, winner, extraction_version='legacy-v1')
    _add_project_resolution(
        service, loser, decision_source='automatic', resolution_state='unresolved',
        review_state='pending', resolver_version='project-resolver.v1')
    return loser, winner, loser_legacy


def test_legitimate_duplicate_active_migration_is_not_drift_and_is_not_repaired(
        service):
    """The reported false positive: a plain quarantined duplicate must be left alone."""
    loser, winner, _loser_legacy = build_duplicate_active_migration(
        service, identity='proj:fact:duplicate')

    classification = classify_projection_status_drift(service.store)

    assert classification.status == 'clean'
    assert classification.eligible_count == 0
    assert classification.eligible_item_ids == ()
    assert classification.superseded_count == 0
    assert classification.retracted_count == 0
    assert classification.human_confirmed_count == 0
    assert classification.live_owner_count == 0
    assert classification.other_drift_count == 0, 'quarantine is not unattributed drift'

    report = apply_projection_status_recovery(service.config, service.store)

    assert report.status == 'clean'
    assert report.legacy_updated == 0
    assert _legacy_status(service, loser) == 'active'
    assert _legacy_status(service, winner) == 'candidate'
    # The formal gate agrees: these rows are not status mismatches.
    official = check_projection_lag(service.config, service.store)
    assert official.status_mismatch == 0


def test_a_migration_marker_alone_never_licenses_a_repair(service):
    """A migration source with no organization origin is never a retraction."""
    item = _make_candidate_with_legacy(service, identity_key='plain:migration')
    _add_migration_source(service, item, extraction_version='legacy-v1')
    _add_project_resolution(
        service, item, decision_source='automatic', resolution_state='unresolved',
        review_state='pending', resolver_version='project-resolver.v1')

    classification = classify_projection_status_drift(service.store)

    assert classification.eligible_item_ids == ()
    assert classification.retracted_count == 0
    assert classification.other_drift_count == 1
    report = apply_projection_status_recovery(service.config, service.store)
    assert report.status == 'unattributed_drift'
    assert report.legacy_updated == 0
    assert _legacy_status(service, item) == 'active'


@pytest.mark.parametrize(
    'mutate, reason',
    [
        (lambda service, item: _add_project_resolution(
            service, item, decision_source='automatic', resolution_state='conflict',
            review_state='pending', resolver_version='auto-organization.v1'),
         'conflict is not a retraction'),
        (lambda service, item: _add_project_resolution(
            service, item, decision_source='automatic', resolution_state='unresolved',
            review_state='pending', resolver_version='auto-organization.v2'),
         'another resolver version is not this organizer'),
        (lambda service, item: _add_project_resolution(
            service, item, decision_source='automatic', resolution_state='unresolved',
            review_state='accepted', resolver_version='auto-organization.v1'),
         'an already reviewed decision is not pending'),
    ],
)
def test_retraction_shape_requires_the_exact_auto_organization_decision(
        service, mutate, reason):
    item = _make_candidate_with_legacy(service, identity_key=f'strict:{reason}')
    # A live owner, so only the retraction shape could possibly qualify.
    _add_live_owner(service, item)
    mutate(service, item)

    classification = classify_projection_status_drift(service.store)

    assert classification.retracted_count == 0, reason
    assert classification.eligible_item_ids == ()


def test_the_confirmed_auto_organization_retraction_summary_is_repaired(
        service, monkeypatch):
    """The exact second shape, built through the real organizer path."""
    from evolvmem.auto_organization import _unresolve_item

    task_id, item_ids = run_real_first_task(service, monkeypatch, session='shaped-session')
    item_id = item_ids[0]
    _unresolve_item(service, item_id, '归属未确认，先撤回')
    inject_legacy_active_drift(service, [item_id])

    shape = service.store._connection().execute(
        'SELECT i.content_type, i.project, r.decision_source, r.resolution_state, '
        'r.review_state, r.resolver_version, '
        "(SELECT COUNT(*) FROM organization_units u WHERE u.item_id=i.id) AS units, "
        "(SELECT COUNT(*) FROM context_sources s WHERE s.item_id=i.id "
        " AND s.source_kind='organization' "
        " AND s.extraction_version='auto-organization.v1') AS org_sources "
        'FROM context_items i JOIN context_project_resolutions r ON r.item_id=i.id '
        'WHERE i.id=?', (item_id,)).fetchone()
    assert shape['content_type'] == 'session_summary'
    assert shape['project'] == ''
    assert shape['decision_source'] == 'automatic'
    assert shape['resolution_state'] == 'unresolved'
    assert shape['review_state'] == 'pending'
    assert shape['resolver_version'] == 'auto-organization.v1'
    assert shape['units'] >= 1
    assert shape['org_sources'] >= 1

    classification = classify_projection_status_drift(service.store)

    assert classification.eligible_item_ids == (item_id,)
    assert classification.retracted_count == 1
    assert classification.other_drift_count == 0
    report = apply_projection_status_recovery(service.config, service.store)
    assert report.legacy_updated == 1
    assert _legacy_status(service, item_id) == 'candidate'
    assert service.store.get_item(item_id).project == ''
    assert check_projection_lag(service.config, service.store).status_mismatch == 0


def test_a_resolution_alone_without_organization_origin_is_never_repaired(service):
    """The exact false positive: the decision shape without the organizer origin."""
    item = _make_candidate_with_legacy(service, identity_key='no-origin')
    _add_live_owner(service, item)
    _add_project_resolution(
        service, item, decision_source='automatic', resolution_state='unresolved',
        review_state='pending', resolver_version='auto-organization.v1')
    _add_migration_source(service, item, extraction_version='legacy-v1')

    classification = classify_projection_status_drift(service.store)

    assert classification.retracted_count == 0
    assert classification.eligible_item_ids == ()
    assert classification.live_owner_count == 1
    assert classification.other_drift_count == 0
    report = apply_projection_status_recovery(service.config, service.store)
    assert report.legacy_updated == 0
    assert _legacy_status(service, item) == 'active'


def test_a_resolution_without_any_owning_unit_is_never_repaired(service):
    """Even with the organizer source and decision, no unit means no retraction."""
    item = _make_candidate_with_legacy(service, identity_key='no-unit')
    _add_project_resolution(
        service, item, decision_source='automatic', resolution_state='unresolved',
        review_state='pending', resolver_version='auto-organization.v1')
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE context_items SET project='' WHERE id=?", (item,))
    _add_organization_source(service, item)

    classification = classify_projection_status_drift(service.store)

    assert classification.retracted_count == 0
    assert classification.eligible_item_ids == ()
    assert _legacy_status(service, item) == 'active'


def _add_organization_source(service, item_id):
    with service.store.transaction():
        service.store._connection().execute(
            "INSERT INTO context_sources(item_id,archive_id,source_kind,source_ref,"
            "extraction_version,created_at) VALUES(?,NULL,'organization',?,?,?)",
            (item_id, f'archive:0#digest@0-9', 'auto-organization.v1', _NOW))

def _add_organization_source(service, item_id):
    with service.store.transaction():
        service.store._connection().execute(
            "INSERT INTO context_sources(item_id,archive_id,source_kind,source_ref,"
            "extraction_version,created_at) VALUES(?,NULL,'organization',?,?,?)",
            (item_id, f'archive:0#digest@0-9', 'auto-organization.v1', _NOW))


def _official_status_mismatch_items(service):
    """The rows `check_projection_lag` itself counts as status mismatches."""
    from evolvmem.context_migration import LegacyMemoryMigrator
    from evolvmem.cutover_checks import (
        _duplicate_active_quarantined_items,
        duplicate_active_legacy_ids,
    )

    store = service.store
    rows = store.iter_legacy_rows()
    duplicate_active = duplicate_active_legacy_ids(rows)
    mapped = sorted({int(row['context_item_id']) for row in rows
                     if row['context_item_id'] is not None})
    quarantined = _duplicate_active_quarantined_items(store, mapped)
    mismatched = set()
    for row in rows:
        legacy_id = LegacyMemoryMigrator.legacy_id_for(row['id'])
        if row['context_item_id'] is None:
            continue
        item_id = int(row['context_item_id'])
        item = store.get_item(item_id, include_layers=False)
        if item is None:
            continue
        expected = (
            ContextStatus.CANDIDATE.value if legacy_id in duplicate_active
            else LegacyMemoryMigrator.status_for(row.get('status')).value)
        if item.status.value != expected:
            if not (item.status is ContextStatus.CANDIDATE
                    and expected == ContextStatus.ACTIVE.value
                    and item_id in quarantined):
                mismatched.add(item_id)
    return mismatched


def test_eligible_items_are_always_a_subset_of_the_official_mismatch_set(
        service, monkeypatch):
    """The repair can never touch a row the formal gate does not flag."""
    task_id, item_ids = make_drifted_projection(service, monkeypatch)
    build_duplicate_active_migration(service, identity='proj:fact:mixed-duplicate')
    _make_native_candidate(service, identity_key='native:unrelated')
    unrelated = _make_unsuperseded_candidate(service)

    official = _official_status_mismatch_items(service)
    classification = classify_projection_status_drift(service.store)
    eligible = set(classification.eligible_item_ids)

    assert official, 'the fixture must contain real mismatches'
    assert eligible <= official, 'never repair beyond the formal gate'
    assert unrelated not in eligible
    assert set(item_ids) <= eligible
    assert classification.eligible_count == len(item_ids)

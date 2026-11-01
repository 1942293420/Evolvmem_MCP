"""Native-module regressions for checked read-snapshot assessment reuse.

No AST extraction or role/provenance replacement. Existing synthetic fixtures
prepare actual store/schema/source/role evidence; spies only count real calls.
"""
from copy import deepcopy
import json

from evolvmem import capability_assessment as assessment
from evolvmem import capability_assessment_migration as migration
from evolvmem.capability_provenance import provenance_snapshot
from tests.test_capability_provenance import provenance_db
from tests.test_capability_assessment_migration import (
    base_service, service, old_unit_memory, plan, fields,
)


def test_original_heads_share_one_scan_only_inside_owned_snapshot(provenance_db):
    store=provenance_db
    conn=store._connection()
    scans=[]
    conn.set_trace_callback(lambda sql:scans.append(1) if sql.startswith(
        'SELECT id,adapter,external_session_id FROM session_archives') else None)
    try:
        with provenance_snapshot(store):
            first=migration.OriginalRead(store)._heads()
            assert first=={1}
            first.clear()  # Caller cannot corrupt the immutable shared copy.
            assert migration.OriginalRead(store)._heads()=={1}
        assert len(scans)==1
        assert migration.OriginalRead(store)._heads()=={1}
        assert migration.OriginalRead(store)._heads()=={1}
        assert len(scans)==3  # Outside the owned scope each reader scans again.
    finally:
        conn.set_trace_callback(None)


def test_original_heads_new_snapshot_observes_new_current_archive(provenance_db):
    store=provenance_db
    conn=store._connection()
    with provenance_snapshot(store):
        assert migration.OriginalRead(store)._heads()=={1}
    from evolvmem.session_identity import identity_key
    assert identity_key('codex','session:1')==identity_key('codex','session:2')
    with store.transaction():
        conn.execute("INSERT INTO session_archives(id,project,adapter,external_session_id,payload_path,payload_sha256,expires_at,created_at) "
            "SELECT 2,project,adapter,'session:2','unused-new','new-payload','2099','znew' FROM session_archives WHERE id=1")
    with provenance_snapshot(store):
        assert migration.OriginalRead(store)._heads()=={2}
        assert migration.OriginalRead(store)._heads()=={2}


def two_assessments(service,monkeypatch):
    identity,_,_=old_unit_memory(service,monkeypatch)
    result=migration.apply_plan(service,plan(service),provider=lambda _:fields())
    assert result['applied']==1
    first=assessment.assessments(service.store,identity)[0]
    assert first['value']=='reference' and not first['stale']
    rejected=deepcopy(first['details']);rejected['value']='review'
    assessment.persist(service.store,identity,first['source_id'],rejected,existing_status='active')
    return identity


def test_status_latest_rejection_never_falls_back_to_old_valid_assessment(service,monkeypatch):
    identity=two_assessments(service,monkeypatch)
    history=assessment.assessments(service.store,identity)
    assert [row['value'] for row in history]==['review','reference']
    assert all(not row['stale'] for row in history)
    with provenance_snapshot(service.store):
        status=assessment.assessment_status(service.store,[identity])[identity]
    assert status['assessments_count']==2
    assert status['eligible_source_ids']==[]
    assert status['stale_count']==0
    assert not status['assessment_eligible']


def test_status_skips_old_proof_entry_but_history_still_validates_old_assessment(service,monkeypatch):
    identity=two_assessments(service,monkeypatch)
    # Corrupt only a historical saved proof; history must still surface it.
    conn=service.store._connection()
    row=conn.execute('SELECT assessment_id,details FROM capability_assessments WHERE memory_id=? ORDER BY assessment_id LIMIT 1',
                     (identity,)).fetchone()
    details=json.loads(row['details']);details['original_proof']['proof_hash']='stale-historical-proof'
    with service.store.transaction():
        conn.execute('UPDATE capability_assessments SET details=? WHERE assessment_id=?',
                     (json.dumps(details),row['assessment_id']))
    import evolvmem.capability_source_proof as source_proof
    calls=[]
    original=source_proof.unit_source_proof
    def counted(*args,**kwargs):
        calls.append(1)
        return original(*args,**kwargs)
    monkeypatch.setattr(source_proof,'unit_source_proof',counted)
    with provenance_snapshot(service.store):
        status=assessment.assessment_status(service.store,[identity])[identity]
    assert status['assessments_count']==2
    assert status['stale_count']==0  # Latest assessment is unchanged/current.
    status_calls=len(calls)
    assert status_calls>=1
    calls.clear()
    with provenance_snapshot(service.store):
        history=assessment.assessments(service.store,identity)
    assert len(history)==2
    assert not history[0]['stale']
    assert history[1]['stale'] and 'original_proof_changed' in history[1]['stale_reasons']
    # Counts function entries (which may hit an existing snapshot cache), not
    # underlying original-body reads. History still proves the extra old row.
    assert len(calls)==status_calls+1

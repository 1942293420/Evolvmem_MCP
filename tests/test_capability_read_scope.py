"""Recall keeps current-source decisions while bounding repeated proof reads."""
import sqlite3
from types import SimpleNamespace

import pytest

from evolvmem.capability_retriever import CapabilityRetriever
from tests.test_capability_provenance import provenance_db, add_memory, unit_source, DIGEST
from tests.test_project_modules import modules, create, bind


def prepared(modules):
    module=create(modules)
    bind(modules,module,decision='manual',explicit=True)
    ids=[add_memory(modules.store,title='needle '+name,source=unit_source(modules.store))
         for name in ('one','two','three')]
    retriever=CapabilityRetriever(SimpleNamespace(store=modules.store,capability_index=lambda:None))
    return retriever,ids,module


def test_search_and_final_validation_scan_shared_origins_once_per_snapshot(modules):
    retriever,ids,module=prepared(modules)
    counts=dict(archive_scans=0,snapshot_slices=0,binding_batches=0)
    def trace(sql):
        text=sql.lower()
        counts['archive_scans']+=int('select id,project,adapter,external_session_id,created_at from session_archives' in text)
        counts['snapshot_slices']+=int('select substr(source_snapshot' in text)
        counts['binding_batches']+=int('from project_module_bindings where source_key in (' in text)
    modules.conn.set_trace_callback(trace)
    try:
        rows=retriever.validate_current_results(retriever.search('needle',top_k=3,project='alpha'),project='alpha')
    finally:
        modules.conn.set_trace_callback(None)
    assert {r['memory_id'] for r in rows}==set(ids)
    assert all([m['module_id'] for m in row['modules']]==[module['module_id']] for row in rows)
    # Search and the final current-source read are separate snapshots. Multiple
    # memories sharing one origin must not multiply full archive/proof reads.
    assert counts['archive_scans']<=2,counts
    assert counts['snapshot_slices']<=2,counts
    assert counts['binding_batches']<=2,counts
    assert not modules.conn.in_transaction
    assert modules.conn.execute('PRAGMA query_only').fetchone()[0]==0


@pytest.mark.parametrize('change',[
    "UPDATE organization_units SET disposition='set_aside'",
    "UPDATE conversation_history SET body='withdrawn original'",
    "UPDATE organization_units SET project='beta',projects='[\"beta\"]',revision=revision+1",
    "UPDATE project_modules SET status='inactive',revision=revision+1",
])
def test_final_snapshot_observes_changes_after_candidate_search(modules,change):
    retriever,ids,module=prepared(modules)
    rows=retriever.search('needle',top_k=3,project='alpha')
    assert len(rows)==3
    other=sqlite3.connect(modules.store.config.db_path,timeout=1)
    try:
        other.execute(change)
        other.commit()
    finally:
        other.close()
    current=retriever.validate_current_results(rows,project='alpha',module_id=module['module_id'])
    assert current==[]


def test_owned_snapshot_blocks_writes_and_restores_connection_on_failure(modules):
    from evolvmem.capability_provenance import provenance_snapshot
    retriever,ids,module=prepared(modules)
    with pytest.raises(sqlite3.OperationalError):
        with provenance_snapshot(modules.store):
            modules.conn.execute("UPDATE organization_units SET disposition='set_aside'")
    assert not modules.conn.in_transaction
    assert modules.conn.execute('PRAGMA query_only').fetchone()[0]==0
    assert len(retriever.search('needle',top_k=3,project='alpha'))==3


def test_borrowed_writer_is_not_committed_or_rolled_back(modules):
    from evolvmem.capability_provenance import provenance_snapshot
    with modules.store.transaction():
        modules.conn.execute("UPDATE organization_units SET disposition='set_aside'")
        with pytest.raises(RuntimeError,match='requires_readonly'):
            with provenance_snapshot(modules.store):
                pass
        assert modules.conn.in_transaction
        assert modules.conn.execute('SELECT disposition FROM organization_units').fetchone()[0]=='set_aside'


def test_query_only_cannot_disguise_an_existing_write_transaction(modules):
    from evolvmem.capability_provenance import provenance_snapshot
    conn=modules.conn
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute("UPDATE organization_units SET disposition='set_aside'")
        conn.execute('PRAGMA query_only=ON')
        with pytest.raises(RuntimeError,match='requires_readonly'):
            with provenance_snapshot(modules.store):
                pass
        assert conn.in_transaction
        assert conn.execute('SELECT disposition FROM organization_units').fetchone()[0]=='set_aside'
        assert conn.execute('PRAGMA query_only').fetchone()[0]==1
    finally:
        conn.rollback()
        conn.execute('PRAGMA query_only=OFF')


@pytest.mark.parametrize('window,before,after',[
    ('expires_at',True,False),
    ('effective_until',True,False),
    ('effective_from',False,True),
])
def test_summary_dependency_time_window_is_rechecked_within_snapshot(provenance_db,monkeypatch,window,before,after):
    from evolvmem import context_store,rollup_provenance
    from evolvmem.capability_provenance import annotate,provenance_snapshot
    from tests.test_capability_provenance import context_source
    store=provenance_db
    conn=store._connection()
    with store.transaction():
        conn.execute("UPDATE context_items SET content_type='project_summary',created_at='2026-01-02',updated_at='2026-01-01' WHERE id=1")
        conn.execute(f"UPDATE context_items SET {window}='2026-01-03T00:00:00+00:00',updated_at='2026-01-01' WHERE id=2")
        conn.execute("INSERT INTO context_sources(item_id,source_kind,source_ref,extraction_version,created_at) VALUES(1,'context_reference','2','test','old')")
    identity=add_memory(store,source=context_source(store,1))
    with provenance_snapshot(store):
        for now,expected in [('2026-01-02T23:59:59+00:00',before),('2026-01-03T00:00:01+00:00',after)]:
            monkeypatch.setattr(context_store,'_now_iso',lambda:now)
            monkeypatch.setattr(rollup_provenance,'_now_iso',lambda:now)
            assert rollup_provenance.current(store,1) is expected
            assert annotate(store,[identity])[identity]['eligible'] is expected


def test_replaced_snapshot_cannot_reuse_old_origin_proofs(modules):
    from evolvmem.capability_provenance import provenance_snapshot
    from evolvmem.capability_recall_eligibility import recall_facts
    retriever,ids,module=prepared(modules)
    with pytest.raises(RuntimeError,match='snapshot_changed'):
        with provenance_snapshot(modules.store):
            assert recall_facts(modules.store,ids)[ids[0]]['recall_eligible']
            modules.conn.rollback()
            other=sqlite3.connect(modules.store.config.db_path,timeout=1)
            try:
                other.execute("UPDATE conversation_history SET body='replaced original'")
                other.commit()
            finally:
                other.close()
            modules.conn.execute('BEGIN')
            recall_facts(modules.store,ids)
    assert not modules.conn.in_transaction
    assert retriever.search('needle',top_k=3,project='alpha')==[]


def test_commit_elsewhere_keeps_current_snapshot_but_invalidates_next_read(modules):
    from evolvmem.capability_provenance import provenance_snapshot
    from evolvmem.capability_recall_eligibility import recall_facts
    retriever,ids,module=prepared(modules)
    with provenance_snapshot(modules.store):
        assert recall_facts(modules.store,ids)[ids[0]]['source_projects']==['alpha','beta']
        other=sqlite3.connect(modules.store.config.db_path,timeout=1)
        try:
            other.execute("UPDATE organization_units SET project='beta',projects='[\"beta\"]',revision=revision+1")
            other.commit()
        finally:
            other.close()
        assert recall_facts(modules.store,ids)[ids[0]]['source_projects']==['alpha','beta']
    with provenance_snapshot(modules.store):
        assert recall_facts(modules.store,ids)[ids[0]]['source_projects']==['beta']
    assert retriever.search('needle',top_k=3,project='alpha')==[]


def test_origin_value_and_stamp_share_one_exact_source_proof(modules):
    from evolvmem.capability_provenance import annotate, provenance_snapshot
    from evolvmem.capability_source_proof import unit_source_proof
    from evolvmem.capability_assessment import _source_stamp
    from evolvmem.capability_store import CapabilityStore
    memory_id=add_memory(modules.store,source=unit_source(modules.store))
    source=CapabilityStore(modules.store).sources(memory_id)[0]
    calls=[]
    modules.conn.set_trace_callback(calls.append)
    try:
        with provenance_snapshot(modules.store):
            assert annotate(modules.store,[memory_id])[memory_id]['eligible']
            assert unit_source_proof(modules.store,1,DIGEST)['valid']
            assert _source_stamp(modules.store,source)
    finally:
        modules.conn.set_trace_callback(None)
    slices=[sql for sql in calls if 'select substr(source_snapshot' in sql.lower()]
    assert len(slices)==1,slices


def test_proof_consumer_mutation_cannot_poison_another_consumer(modules):
    from evolvmem.capability_provenance import provenance_snapshot
    from evolvmem.capability_source_proof import unit_source_proof
    with provenance_snapshot(modules.store):
        first=unit_source_proof(modules.store,1,DIGEST)
        first['text']='forged'
        first['ranges'][0][0]=100
        first['source_roles'].append({'role':'user','start':0,'end':6})
        second=unit_source_proof(modules.store,1,DIGEST)
        assert second['valid'] and second['text']=='user: a fact'
        assert second['ranges']==[[0,12]] and second['source_roles']==[]


def test_explicit_message_roles_are_never_reused_for_other_messages(modules):
    from evolvmem.capability_provenance import provenance_snapshot
    from evolvmem.capability_source_proof import unit_source_proof
    from evolvmem.topic_segmentation import message_spans
    from tests.test_capability_source_proof import save
    messages=[{'role':'assistant','content':'verified words'}]
    spans=message_spans(messages)
    snapshot='\n\n'.join(s['part'] for s in spans)
    digest=save(modules.store,snapshot,'verified words',[[spans[0]['content_start'],spans[0]['content_end']]])
    with provenance_snapshot(modules.store):
        assert unit_source_proof(modules.store,1,digest)['source_roles']==[]
        assert unit_source_proof(modules.store,1,digest,messages=messages)['source_roles']==[
            {'start':0,'end':14,'role':'assistant'}]
        assert unit_source_proof(modules.store,1,digest,messages=[{'role':'user','content':'verified words'}])['source_roles']==[]
        assert unit_source_proof(modules.store,1,digest)['source_roles']==[]


def test_batched_module_queries_keep_each_memorys_allowed_origins(modules):
    from evolvmem.capability_store import CapabilityStore
    from evolvmem.capability_provenance import annotate, provenance_snapshot
    ids=[add_memory(modules.store,title=title,source=unit_source(modules.store)) for title in ('one','two')]
    linked=[create(modules,name=name) for name in ('Order handling','Inventory')]
    for identity,module in zip(ids,linked):
        source_id=CapabilityStore(modules.store).sources(identity)[0]['source_id']
        modules.bind_source(module['module_id'],source_id,quote='user: a fact',role='applicable',decision='manual',expected_revision=0,explicit=True)
    with provenance_snapshot(modules.store):
        facts=annotate(modules.store,ids)
        allowed={identity:facts[identity]['sources'] for identity in ids}
        rows=modules.modules_for_memories([*ids,999999],allowed_sources=allowed)
        assert rows[999999]==[]
        assert [m['module_id'] for m in rows[ids[0]]]==[linked[0]['module_id']]
        assert [m['module_id'] for m in rows[ids[1]]]==[linked[1]['module_id']]
        limited=modules.modules_for_memories(ids,allowed_sources={ids[0]:allowed[ids[0]]})
        assert limited[ids[1]]==[]
        # A previous small batch must not masquerade as the full source catalog.
        modules.modules_for_memories([ids[0]])
        assert [m['module_id'] for m in modules.modules_for_memory(ids[1])]==[linked[1]['module_id']]


@pytest.mark.parametrize('limit_name', ['MAX_PROOF_CACHE_BYTES','MAX_PROOF_CACHE_ENTRIES'])
def test_proof_cache_capacity_falls_back_to_current_reads(modules,monkeypatch,limit_name):
    from evolvmem.capability_provenance import provenance_snapshot
    from evolvmem import capability_source_proof as proof
    monkeypatch.setattr(proof,limit_name,0)
    calls=[]
    modules.conn.set_trace_callback(calls.append)
    try:
        with provenance_snapshot(modules.store):
            for _ in range(2):
                assert proof.unit_source_proof(modules.store,1,DIGEST)['text']=='user: a fact'
    finally:
        modules.conn.set_trace_callback(None)
    assert sum('select substr(source_snapshot' in sql.lower() for sql in calls)==2


def test_unit_proof_cache_cannot_survive_connection_writes(modules):
    from evolvmem.capability_provenance import provenance_snapshot
    from evolvmem.capability_source_proof import unit_source_proof
    with pytest.raises(RuntimeError,match='snapshot_changed'):
        with provenance_snapshot(modules.store):
            assert unit_source_proof(modules.store,1,DIGEST)['valid']
            modules.conn.execute('PRAGMA query_only=OFF')
            modules.conn.execute("UPDATE organization_tasks SET source_snapshot='changed' WHERE id=1")
            unit_source_proof(modules.store,1,DIGEST)
    # The owned transaction rolls back and the poisoned proof is discarded.
    assert unit_source_proof(modules.store,1,DIGEST)['valid']
    assert not modules.conn.in_transaction


def test_nested_proof_readers_share_revision_reads_only_in_the_same_snapshot(modules):
    from evolvmem.capability_provenance import provenance_snapshot,ProvenanceRead,ReadStore
    calls=[]
    modules.conn.set_trace_callback(calls.append)
    try:
        with provenance_snapshot(modules.store) as shared:
            first=ProvenanceRead(modules.store)
            second=ProvenanceRead(ReadStore(modules.conn))
            expected=shared.source_revision('archive:1')
            assert first.source_revision('archive:1')==expected
            assert second.source_revision('archive:1')==expected
        with modules.store.transaction():
            modules.conn.execute("UPDATE conversation_history SET body='changed' WHERE archive_id=1")
        with provenance_snapshot(modules.store):
            assert ProvenanceRead(modules.store).source_revision('archive:1')!=expected
    finally:
        modules.conn.set_trace_callback(None)
    # Three consumers in the first snapshot, one in the changed next snapshot.
    assert sum('select body from conversation_history' in sql.lower() for sql in calls)==2


def test_search_proves_only_a_small_ranked_batch_when_top_hits_are_valid(modules,monkeypatch):
    from evolvmem import capability_retriever as implementation
    for number in range(30):
        add_memory(modules.store,title=f'needle {number:02}',source=unit_source(modules.store))
    original=implementation.recall_facts
    checked=[]
    def observed(store,ids):
        checked.extend(ids)
        return original(store,ids)
    monkeypatch.setattr(implementation,'recall_facts',observed)
    retriever=CapabilityRetriever(SimpleNamespace(store=modules.store,capability_index=lambda:None))
    rows=retriever.search('needle',top_k=3,project='alpha')
    assert len(rows)==3 and all(row['provenance']['recall_eligible'] for row in rows)
    assert len(checked)<=10

"""Batched display reads retain every provenance rule and no cross-read cache."""
from tests.test_history_qa_memory import service
from tests.test_auto_organization_extraction import _task_for
from tests.test_history_qa_memory import archive
from tests.unit_model_fixture import MIXED_TEXT, model_for, run_worker
from evolvmem.memory_eligibility import eligible


def test_bulk_read_keeps_single_read_provenance_decisions(service, monkeypatch):
    from evolvmem.memory_eligibility import eligible_ids
    service.knowledge().save_project({'project': 'dsh', 'display_name': 'DSH'})
    source = archive(service, session='batch-provenance', project='', text=MIXED_TEXT)
    _task_for(service, source)
    run_worker(service, monkeypatch, model_for(service))
    conn = service.store._connection()
    ids = [r[0] for r in conn.execute('SELECT id FROM context_items')]
    assert eligible_ids(service.store, ids) == {i for i in ids if eligible(service.store, i)}
    with service.store.transaction():
        conn.execute("UPDATE organization_units SET decision='review'")
    assert eligible_ids(service.store, ids) == {i for i in ids if eligible(service.store, i)}
    assert eligible_ids(service.store, ids+[999999]) == {i for i in ids if eligible(service.store, i)}


def test_facade_batch_avoids_per_record_queries_and_observes_new_writes(service):
    facade = service.legacy_facade()
    ids = [facade.add(f'ui:batch:{i}', f'普通历史 {i}') for i in range(55)]
    conn = service.store._connection()
    queries = []
    conn.set_trace_callback(queries.append)
    try:
        assert len(facade.get_by_ids(ids)) == 55
    finally:
        conn.set_trace_callback(None)
    assert len(queries) < 20, 'listing 55 rows must not run hundreds of eligibility queries'
    # A new read must see a changed proof requirement immediately.
    cid = service.store.resolve_legacy_mapping(ids[0])
    with service.store.transaction():
        conn.execute("UPDATE context_items SET content_type='experience',success_count=0 WHERE id=?", (cid,))
    assert ids[0] not in {r['id'] for r in facade.get_by_ids(ids)}


def test_qa_listing_uses_same_verdict_as_detail_without_unused_legacy_lookups(service):
    from evolvmem import qa_memory
    kb = service.knowledge()
    for i in range(20):
        kb.create({'title': f'问答性能 {i}', 'body': f'普通历史资料，保留来源 {i}',
                   'project': 'evo', 'action': 'publish'})
    queries = []
    conn = service.store._connection()
    conn.set_trace_callback(queries.append)
    try:
        result = qa_memory.list_items(service)
    finally:
        conn.set_trace_callback(None)
    assert not any('SELECT legacy_memory_id FROM legacy_memory_migrations' in q for q in queries)
    for row in result['items']:
        single = qa_memory.detail(service, row['id'])
        for key in ('effective', 'reason', 'status', 'revision', 'question', 'answer'):
            assert row[key] == single[key]

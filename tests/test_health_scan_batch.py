"""Equivalent health scans retain every model/layer gate without N+1 queries."""
import pytest
from tests.test_context_retriever import store, add_item
from evolvmem.context_service import ContextService
from evolvmem.context_models import ContextMode
from evolvmem.memory_eligibility import eligible


def test_vector_documents_use_batch_eligibility_and_observe_new_writes(store):
    items = [add_item(store, f'batch-health-{i}') for i in range(55)]
    conn = store._connection()
    expected = {i.id for i in items if eligible(store, i.id)}
    queries = []
    conn.set_trace_callback(queries.append)
    try:
        assert {d.item_id for d in store.list_vector_documents()} == expected
    finally:
        conn.set_trace_callback(None)
    assert len(queries) < 20, 'one read must not issue eligibility SQL per document'
    with store.transaction():
        conn.execute("UPDATE context_items SET content_type='experience',success_count=0 WHERE id=?", (items[0].id,))
    assert items[0].id not in {d.item_id for d in store.list_vector_documents()}


def test_health_validates_metadata_and_layers_without_loading_each_body(store, test_config, monkeypatch):
    for i in range(20):
        add_item(store, f'batch-item-{i}', l2='PRIVATE_SOURCE_BODY')
    service = ContextService(test_config, store=store)
    service._mode = ContextMode.PRIMARY
    service._adapter = 'codex'
    test_config.context_vectors_required = False
    service._ensure_dependencies()
    monkeypatch.setattr(service, '_integrity_diagnostics', lambda: ())
    def per_item_forbidden(*args, **kwargs):
        pytest.fail('health must validate the same structure in bounded batches')
    monkeypatch.setattr(store, 'get_item', per_item_forbidden)
    assert service._primary_diagnostics() == ()


@pytest.mark.parametrize('damage', [
    "DELETE FROM context_layers WHERE item_id=? AND layer='l2'",
    "UPDATE context_layers SET generator='different' WHERE item_id=? AND layer='l1'",
    "UPDATE context_items SET projects='not json' WHERE id=?",
    "UPDATE context_items SET tags='{}' WHERE id=?",
    "UPDATE context_items SET scope='bad-scope' WHERE id=?",
    "UPDATE context_items SET tier='bad-tier' WHERE id=?",
    "UPDATE context_items SET projects='42' WHERE id=?",
])
def test_bulk_health_keeps_existing_layer_generator_and_json_failures(store, test_config, monkeypatch, damage):
    item = add_item(store, 'damaged-health')
    store._connection().execute('PRAGMA ignore_check_constraints=ON')
    with store.transaction():
        store._connection().execute(damage, (item.id,))
    service = ContextService(test_config, store=store)
    service._mode = ContextMode.PRIMARY
    service._adapter = 'codex'
    test_config.context_vectors_required = False
    service._ensure_dependencies()
    monkeypatch.setattr(service, '_integrity_diagnostics', lambda: ())
    assert 'layer_invariant_failed' in service._primary_diagnostics()


def test_structure_validation_batches_all_original_sqlite_text_reads(store):
    import sqlite3
    ids = [add_item(store, f'no-body-{i}').id for i in range(55)]
    reads = []
    def authorize(action, table, column, database, trigger):
        if action == sqlite3.SQLITE_READ:
            reads.append((table, column))
        return sqlite3.SQLITE_OK
    conn = store._connection()
    conn.set_authorizer(authorize)
    try:
        store.validate_item_structure(ids)
    finally:
        conn.set_authorizer(None)
    assert ('context_layers', 'content') in reads
    assert ('context_items', 'experience_payload') in reads


@pytest.mark.parametrize("layer", ["l0", "l1", "l2", "experience_payload"])
def test_bulk_health_preserves_sqlite_utf8_decode_failure(store, test_config, monkeypatch, layer):
    item = add_item(store, "invalid-utf8-health")
    conn = store._connection()
    with store.transaction():
        if layer == "experience_payload":
            conn.execute("UPDATE context_items SET experience_payload=CAST(X'80' AS TEXT) WHERE id=?", (item.id,))
        else:
            conn.execute("UPDATE context_layers SET content=CAST(X'80' AS TEXT) WHERE item_id=? AND layer=?", (item.id, layer))
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    import sqlite3
    with pytest.raises(sqlite3.OperationalError):
        store.get_item(item.id)
    service = ContextService(test_config, store=store)
    service._mode = ContextMode.PRIMARY
    service._adapter = "codex"
    test_config.context_vectors_required = False
    service._ensure_dependencies()
    monkeypatch.setattr(service, "_integrity_diagnostics", lambda: ())
    assert "layer_invariant_failed" in service._primary_diagnostics()


def _unbacked_derivation(store, item_id):
    conn = store._connection()
    with store.transaction():
        task = conn.execute("INSERT INTO organization_tasks(source_key,source_revision,rule_revision,status,created_at,updated_at) VALUES('fixture-origin','1','1','superseded','now','now')").lastrowid
        conn.execute("INSERT INTO unit_derivations(unit_task_id,unit_digest,item_id,created_at) VALUES(?,'digest',?,'now')", (task,item_id))


@pytest.mark.parametrize("field", ["learning_payload", "unqualified_experience"])
def test_vector_eligibility_preserves_original_unused_text_decode(store, field):
    import sqlite3
    item = add_item(store, "eligibility-decode")
    conn = store._connection()
    with store.transaction():
        if field == "learning_payload":
            conn.execute("INSERT INTO learning_memories(item_id,category,payload) VALUES(?,'reference',CAST(X'80' AS TEXT))", (item.id,))
        else:
            conn.execute("UPDATE context_items SET content_type='experience',success_count=0,experience_payload=CAST(X'80' AS TEXT) WHERE id=?", (item.id,))
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    with pytest.raises(sqlite3.OperationalError):
        eligible(store, item.id)
    with pytest.raises(sqlite3.OperationalError):
        store.list_vector_documents()


def test_vector_eligibility_keeps_backing_short_circuit_before_payload_decode(store):
    item = add_item(store, "backing-short-circuit")
    _unbacked_derivation(store, item.id)
    with store.transaction():
        store._connection().execute("INSERT INTO learning_memories(item_id,category,payload) VALUES(?,'reference',CAST(X'80' AS TEXT))", (item.id,))
    assert not eligible(store, item.id)
    assert store.list_vector_documents() == []


@pytest.mark.parametrize("first_good,derivation", [(False,True), (True,True), (False,False)])
def test_vector_eligibility_preserves_source_decode_and_short_circuit(store, first_good, derivation):
    import sqlite3
    item = add_item(store, "source-decode")
    if derivation:
        _unbacked_derivation(store, item.id)
    conn = store._connection()
    with store.transaction():
        if first_good:
            conn.execute("INSERT INTO context_sources(item_id,source_kind,source_ref,extraction_version,created_at) VALUES(?,'manual','first','v1','now')", (item.id,))
        conn.execute("INSERT INTO context_sources(item_id,source_kind,source_ref,extraction_version,created_at) VALUES(?,'manual','last',CAST(X'80' AS TEXT),'now')", (item.id,))
    if derivation and not first_good:
        with pytest.raises(sqlite3.OperationalError):
            eligible(store, item.id)
        with pytest.raises(sqlite3.OperationalError):
            store.list_vector_documents()
    else:
        assert eligible(store, item.id)
        assert [d.item_id for d in store.list_vector_documents()] == [item.id]

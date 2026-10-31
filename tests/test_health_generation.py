"""SQLite commit generations separate semantic health from archive intake."""
import sqlite3
import threading
import time

import pytest

from tests.test_lan_sharing import lan, add
from evolvmem import request_health


def test_archive_writer_during_each_health_pass_does_not_starve_reads(lan, monkeypatch):
    runtime, adapter = lan
    item = add(adapter, 'kane')
    server = runtime.server_for('kane')
    conn = server.context_service.store._connection()
    with server.context_service.store.transaction():
        conn.execute("INSERT INTO lan_session_heads(device_id,session_id,sha256,source_order) VALUES('review','upload','',1)")
    # Establish physical baseline before concurrent unrelated intake.
    assert server._live_status().ready
    request_health._entry(server).snapshot = None
    entered, committed, stop = threading.Event(), threading.Event(), threading.Event()
    failures = []
    def writer():
        try:
            with sqlite3.connect(server.config.db_path) as database:
                assert entered.wait(3)
                while not stop.is_set():
                    database.execute("UPDATE lan_session_heads SET source_order=source_order+1 WHERE device_id='review'")
                    database.commit()
                    committed.set()
                    stop.wait(.002)
        except BaseException as exc:
            failures.append(exc)
    worker = threading.Thread(target=writer)
    worker.start()
    original = server._evaluate_live_status
    evaluations = []
    def evaluate():
        result = original()
        evaluations.append(result.ready)
        entered.set()
        assert committed.wait(3)
        time.sleep(.025)
        return result
    monkeypatch.setattr(server, '_evaluate_live_status', evaluate)
    try:
        result = adapter.call_tool('kane', 'context_search', {'query':'orchard', 'project':'lan', 'space':'personal'})
    finally:
        stop.set()
        worker.join(3)
    assert not failures and not worker.is_alive()
    assert 'error' not in result
    assert result['results'][0]['id'] == item['context_id']
    assert evaluations == [True]


@pytest.mark.parametrize('sql', [
    "DELETE FROM context_layers WHERE item_id=? AND layer='l2'",
    "UPDATE context_layers SET content='damaged without updating hash' WHERE item_id=? AND layer='l1'",
    "DELETE FROM legacy_memory_migrations WHERE context_item_id=?",
])
def test_concurrent_semantic_damage_still_rejects_ready(lan, monkeypatch, sql):
    runtime, adapter = lan
    item = add(adapter, 'kane')
    server = runtime.server_for('kane')
    assert server._live_status().ready
    request_health._entry(server).snapshot = None
    original = server._evaluate_live_status
    written = []
    def evaluate():
        result = original()
        if not written:
            with sqlite3.connect(server.config.db_path) as writer:
                writer.execute(sql, (item['context_id'],))
            written.append(True)
        return result
    monkeypatch.setattr(server, '_evaluate_live_status', evaluate)
    result = adapter.call_tool('kane', 'context_search', {'query':'orchard', 'project':'lan', 'space':'personal'})
    assert result.get('error') in {'degraded_legacy', 'unknown_tool'}
    assert not result.get('results')


def test_contract_tracks_every_action_and_raw_column_on_all_dependencies():
    from contextlib import contextmanager
    from evolvmem.health_generation import TABLES, observed, prepare
    conn = sqlite3.connect(':memory:')
    for table in TABLES:
        conn.execute(f'CREATE TABLE {table}(value INTEGER)')
    class Store:
        def _connection(self):
            return conn
        @contextmanager
        def transaction(self):
            with conn:
                yield
    store = Store()
    prepare(store)
    schema = conn.execute('PRAGMA schema_version').fetchone()[0]
    generation = observed(store, schema)
    assert generation == 0
    for table in TABLES:
        for sql in (f'INSERT INTO {table} VALUES(1)', f'UPDATE {table} SET value=2', f'DELETE FROM {table}'):
            conn.execute(sql)
            generation += 1
            assert observed(store, schema) == generation
    prepare(store)
    assert conn.execute('PRAGMA schema_version').fetchone()[0] == schema
    assert observed(store, schema) == generation
    conn.close()


@pytest.mark.parametrize('damage', ['row', 'table', 'version', 'schema', 'trigger', 'forged_schema', 'mutated_trigger'])
def test_untrusted_generation_uses_conservative_all_commit_state(lan, damage):
    from evolvmem.health_generation import observed
    runtime, _ = lan
    server = runtime.server_for('kane')
    store = server.context_service.store
    conn = store._connection()
    schema = conn.execute('PRAGMA schema_version').fetchone()[0]
    assert observed(store, schema) is not None
    with store.transaction():
        if damage == 'row':
            conn.execute('DELETE FROM context_health_generation')
        elif damage == 'table':
            conn.execute('DROP TABLE context_health_generation')
        elif damage == 'version':
            conn.execute('UPDATE context_health_generation SET version=999')
        elif damage == 'schema':
            conn.execute('CREATE TABLE unexpected_schema(id INTEGER)')
        else:
            conn.execute('DROP TRIGGER health_generation_context_layers_update')
            if damage == 'mutated_trigger':
                conn.execute('CREATE TRIGGER health_generation_context_layers_update AFTER UPDATE ON context_layers BEGIN SELECT 1; END')
            if damage in ('forged_schema', 'mutated_trigger'):
                schema = conn.execute('PRAGMA schema_version').fetchone()[0]
                conn.execute('UPDATE context_health_generation SET schema_version=?', (schema,))
    schema = conn.execute('PRAGMA schema_version').fetchone()[0]
    assert observed(store, schema) is None
    before = request_health._state(server)
    with sqlite3.connect(server.config.db_path) as writer:
        writer.execute("INSERT INTO lan_session_heads(device_id,session_id,sha256,source_order) VALUES('review','fallback','',1)")
    assert request_health._state(server) != before


def test_replaced_database_cannot_authorize_old_connection(lan, tmp_path):
    runtime, _ = lan
    server = runtime.server_for('kane')
    assert server._live_status().ready
    replacement = tmp_path / 'replacement.db'
    with sqlite3.connect(replacement) as other:
        other.execute('CREATE TABLE replacement_only(id INTEGER)')
    replacement.replace(server.config.db_path)
    assert server._live_status() is None
    assert server._context_gate_error() is not None


def test_semantic_generation_observes_rollbacks_without_false_changes(lan):
    from evolvmem.health_generation import observed
    runtime, adapter = lan
    item = add(adapter, 'kane')
    store = runtime.server_for('kane').context_service.store
    conn = store._connection()
    schema = conn.execute('PRAGMA schema_version').fetchone()[0]
    before = observed(store, schema)
    with pytest.raises(RuntimeError, match='rollback fixture'):
        with store.transaction():
            conn.execute("DELETE FROM context_layers WHERE item_id=? AND layer='l2'", (item['context_id'],))
            assert observed(store, schema) > before
            raise RuntimeError('rollback fixture')
    assert observed(store, schema) == before

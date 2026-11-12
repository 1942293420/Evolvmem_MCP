import json
import sqlite3

import pytest

from tests.test_capability_provenance import provenance_db, add_memory, DIGEST


def seed(store):
    first = add_memory(store)
    second = add_memory(store, title='Unit', ref=f'archive:1#{DIGEST}')
    with store.transaction():
        store._connection().execute("INSERT INTO knowledge_restructure_map VALUES(1,'capability',?,'migrated','old')", (str(first),))
    return first, second


def test_repair_is_readonly_then_cas_insert_and_idempotent(provenance_db):
    from evolvmem.capability_provenance_repair import build_plan, apply_plan
    store = provenance_db
    seed(store)
    conn = store._connection()
    before = conn.total_changes
    plan = build_plan(conn)
    assert conn.total_changes == before
    assert plan['summary']['proposed_bindings'] == 2
    assert apply_plan(conn, plan)['applied'] == 2
    assert apply_plan(conn, plan)['already_applied']
    assert conn.execute('SELECT count(*) FROM capability_sources').fetchone()[0] == 2
    assert all(r[0] == '' for r in conn.execute('SELECT source_project FROM capability_memories'))
    audit = conn.execute('SELECT before_json,after_json FROM capability_provenance_repairs').fetchone()
    assert json.loads(audit[0]) == [None, None]
    assert len(json.loads(audit[1])) == 2


@pytest.mark.parametrize('sql', [
    "UPDATE organization_units SET revision=2,decision='manual',project='beta'",
    "UPDATE capability_memories SET revision=revision+1 WHERE memory_id=2",
    "UPDATE context_layers SET content_hash='changed' WHERE item_id=1",
    "UPDATE conversation_history SET body='changed'",
    "UPDATE organization_tasks SET status='superseded'",
    "UPDATE context_project_registry SET status='archived' WHERE project='beta'",
])
def test_repair_drift_aborts_entire_batch(provenance_db, sql):
    from evolvmem.capability_provenance_repair import build_plan, apply_plan
    store = provenance_db
    seed(store)
    conn = store._connection()
    plan = build_plan(conn)
    with store.transaction():
        conn.execute(sql)
    with pytest.raises(ValueError, match='stale_repair_plan'):
        apply_plan(conn, plan)
    assert conn.execute('SELECT count(*) FROM capability_sources').fetchone()[0] == 0


def test_hash_check_and_late_failure_roll_back_audit(provenance_db):
    from evolvmem.capability_provenance_repair import build_plan, apply_plan
    store = provenance_db
    seed(store)
    conn = store._connection()
    plan = build_plan(conn)
    plan['plan_sha256'] = 'invalid'
    with pytest.raises(ValueError, match='invalid_repair_plan'):
        apply_plan(conn, plan)
    plan = build_plan(conn)
    conn.execute("CREATE TRIGGER reject_binding BEFORE INSERT ON capability_sources WHEN NEW.memory_id=2 BEGIN SELECT RAISE(ABORT,'synthetic_failure'); END")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match='synthetic_failure'):
        apply_plan(conn, plan)
    assert conn.execute('SELECT count(*) FROM capability_sources').fetchone()[0] == 0
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='capability_provenance_repairs'").fetchone()


def test_baseline_without_sources_table_can_prepare_but_cannot_apply(provenance_db):
    from evolvmem.capability_provenance_repair import build_plan, apply_plan
    store = provenance_db
    seed(store)
    conn = store._connection()
    conn.execute('DROP TABLE capability_sources')
    conn.commit()
    plan = build_plan(conn)
    assert plan['summary']['proposed_bindings'] == 2
    with pytest.raises(ValueError, match='capability_sources_schema_required'):
        apply_plan(conn, plan)
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='capability_sources'").fetchone()


def test_historical_only_is_audited_not_reclassified(provenance_db):
    from evolvmem.capability_provenance_repair import build_plan
    store = provenance_db
    seed(store)
    with store.transaction():
        store._connection().execute("UPDATE organization_tasks SET status='superseded'")
    plan = build_plan(store._connection())
    assert plan['summary']['proposed_bindings'] == 1
    row = next(r for r in plan['inventory'] if r['memory_id'] == 2)
    assert row['sources'][0]['reason'] == 'historical_unit_only'
    assert not row['eligible']


def test_existing_binding_is_preserved_while_independent_origin_is_backfilled(provenance_db):
    from evolvmem.capability_provenance_repair import build_plan, apply_plan
    from evolvmem.capability_store import CapabilityStore, CapabilitySourceDraft
    store = provenance_db
    first, second = seed(store)
    CapabilityStore(store).bind_source(first, CapabilitySourceDraft(source_kind='manual', source_key='manual:keep', source_ref='local note'))
    conn = store._connection()
    existing = dict(conn.execute('SELECT * FROM capability_sources WHERE memory_id=?', (first,)).fetchone())
    plan = build_plan(conn)
    assert {(r['binding']['memory_id'], r['binding']['source_kind']) for r in plan['changes']} == {(first, 'context'), (second, 'unit')}
    assert apply_plan(conn, plan)['applied'] == 2
    assert dict(conn.execute('SELECT * FROM capability_sources WHERE source_id=?', (existing['source_id'],)).fetchone()) == existing


def test_rollback_deletes_only_batch_bindings_keeps_memories_and_audit(provenance_db):
    from evolvmem.capability_provenance_repair import build_plan, apply_plan, rollback_plan
    store = provenance_db
    seed(store)
    conn = store._connection()
    memories = [dict(r) for r in conn.execute('SELECT * FROM capability_memories')]
    plan = build_plan(conn)
    apply_plan(conn, plan)
    assert rollback_plan(conn, plan)['rolled_back'] == 2
    assert conn.execute('SELECT count(*) FROM capability_sources').fetchone()[0] == 0
    assert [dict(r) for r in conn.execute('SELECT * FROM capability_memories')] == memories
    assert conn.execute('SELECT rolled_back_at FROM capability_provenance_repairs').fetchone()[0]
    assert rollback_plan(conn, plan)['already_rolled_back']
    with pytest.raises(ValueError, match='repair_previously_rolled_back'):
        apply_plan(conn, plan)


@pytest.mark.parametrize('sql', [
    "UPDATE capability_sources SET projects='[\"beta\"]',revision=2 WHERE memory_id=2",
    "UPDATE organization_units SET revision=2,decision='manual',project='beta'",
    "UPDATE organization_tasks SET source_revision='changed'",
    "UPDATE conversation_history SET body='changed'",
    "UPDATE context_project_registry SET revision=revision+1 WHERE project='alpha'",
    "UPDATE capability_memories SET title='Human correction',revision=revision+1 WHERE memory_id=1",
    "UPDATE context_items SET project='beta' WHERE id=1",
])
def test_rollback_refuses_entire_batch_after_later_changes(provenance_db, sql):
    from evolvmem.capability_provenance_repair import build_plan, apply_plan, rollback_plan
    store = provenance_db
    seed(store)
    conn = store._connection()
    plan = build_plan(conn)
    apply_plan(conn, plan)
    with store.transaction():
        conn.execute(sql)
    before = [dict(r) for r in conn.execute('SELECT * FROM capability_sources')]
    with pytest.raises(ValueError, match='rollback_conflict'):
        rollback_plan(conn, plan)
    assert [dict(r) for r in conn.execute('SELECT * FROM capability_sources')] == before
    assert conn.execute('SELECT rolled_back_at FROM capability_provenance_repairs').fetchone()[0] is None


def test_rollback_late_delete_failure_is_atomic(provenance_db):
    from evolvmem.capability_provenance_repair import build_plan, apply_plan, rollback_plan
    store = provenance_db
    seed(store)
    conn = store._connection()
    plan = build_plan(conn)
    apply_plan(conn, plan)
    conn.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON capability_sources WHEN OLD.memory_id=2 BEGIN SELECT RAISE(ABORT,'synthetic_failure'); END")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match='synthetic_failure'):
        rollback_plan(conn, plan)
    assert conn.execute('SELECT count(*) FROM capability_sources').fetchone()[0] == 2
    assert conn.execute('SELECT rolled_back_at FROM capability_provenance_repairs').fetchone()[0] is None


def test_cli_plan_apply_and_rollback_are_explicit(provenance_db, test_config, tmp_path, capsys):
    from evolvmem.capability_provenance_repair import main
    seed(provenance_db)
    plan = tmp_path / 'plan.json'
    assert main(['--database', str(test_config.db_path), '--output', str(plan)]) == 0
    assert json.loads(capsys.readouterr().out)['mode'] == 'dry_run'
    assert main(['--database', str(test_config.db_path), '--apply-plan', str(plan)]) == 0
    assert json.loads(capsys.readouterr().out)['applied'] == 2
    assert main(['--database', str(test_config.db_path), '--rollback-plan', str(plan)]) == 0
    assert json.loads(capsys.readouterr().out)['rolled_back'] == 2

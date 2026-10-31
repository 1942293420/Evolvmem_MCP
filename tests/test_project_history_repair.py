"""Bounded historical date repair operates only on reviewed mapped events."""
import json
from types import SimpleNamespace
import pytest
from evolvmem.context_store import ContextStore
from evolvmem.knowledge_restructure import migrate_batch
from tests.test_project_history_dates import legacy, register


def seed(store, config):
    register(store)
    for tag in ('2026-07-20-2328', '2026-08-05-1144', '2026-08-31-1730', 'undated'):
        legacy(store, tag)
    legacy(store, '2026-06-01-1200', project='other')
    migrate_batch(SimpleNamespace(store=store, config=config))
    with store.transaction():
        store._connection().execute("UPDATE project_events SET occurred_at=NULL,recorded_at='2026-10-28 12:00:00'")
    return store._connection()


def test_dry_run_has_literal_days_and_preserves_unknown_and_unrelated_scope(test_config):
    from evolvmem.project_history_repair import build_plan
    with ContextStore(test_config) as store:
        conn = seed(store, test_config)
        before = conn.total_changes
        plan = build_plan(conn, 'ai_purchase')
        assert [c['new_occurred_at'] for c in plan['changes']] == ['2026-07-20', '2026-08-05', '2026-08-31']
        assert all(c['before']['occurred_at'] is None for c in plan['changes'])
        assert all(c['before']['recorded_at'] == '2026-10-28 12:00:00' for c in plan['changes'])
        assert plan['skipped'][-1]['reason'] == 'no_verifiable_source_date'
        assert conn.total_changes == before


def test_apply_preserves_full_originals_source_bindings_and_recorded_time(test_config):
    from evolvmem.project_history_repair import build_plan, apply_plan
    with ContextStore(test_config) as store:
        conn = seed(store, test_config)
        # Archived legacy evidence remains evidence; it must never be unarchived.
        with store.transaction():
            conn.execute("UPDATE context_items SET status='archived' WHERE id=1")
        sources_before = [tuple(r) for r in conn.execute('SELECT * FROM context_items')]
        mappings_before = [tuple(r) for r in conn.execute('SELECT * FROM knowledge_restructure_map')]
        plan = build_plan(conn, 'ai_purchase')
        result = apply_plan(conn, plan)
        assert result['applied'] == 3
        assert apply_plan(conn, plan)['already_applied'] is True
        assert build_plan(conn, 'ai_purchase')['changes'] == []
        for change in plan['changes']:
            event = dict(conn.execute('SELECT * FROM project_events WHERE event_id=?', (change['event_id'],)).fetchone())
            assert event['occurred_at'] == change['new_occurred_at']
            assert event['recorded_at'] == change['before']['recorded_at']
            assert event['source_ref'] == change['before']['source_ref']
        audit = conn.execute('SELECT plan_json FROM project_history_date_repairs').fetchone()[0]
        assert json.loads(audit) == plan
        assert [tuple(r) for r in conn.execute('SELECT * FROM context_items')] == sources_before
        assert [tuple(r) for r in conn.execute('SELECT * FROM knowledge_restructure_map')] == mappings_before
        assert conn.execute("SELECT occurred_at FROM project_events WHERE project='other'").fetchone()[0] is None


def test_apply_rejects_stale_plan_without_partial_writes(test_config):
    from evolvmem.project_history_repair import build_plan, apply_plan
    with ContextStore(test_config) as store:
        conn = seed(store, test_config)
        plan = build_plan(conn, 'ai_purchase')
        with store.transaction():
            conn.execute("UPDATE project_events SET occurred_at='2026-01-01' WHERE event_id=2")
        with pytest.raises(ValueError, match='stale_repair_plan'):
            apply_plan(conn, plan)
        assert conn.execute('SELECT occurred_at FROM project_events WHERE event_id=1').fetchone()[0] is None
        assert conn.execute('SELECT occurred_at FROM project_events WHERE event_id=2').fetchone()[0] == '2026-01-01'


def test_manual_dates_and_corrected_events_are_not_overwritten(test_config):
    from evolvmem.project_history_repair import build_plan, apply_plan
    with ContextStore(test_config) as store:
        conn = seed(store, test_config)
        with store.transaction():
            conn.execute("UPDATE project_events SET occurred_at='2026-01-01' WHERE event_id=1")
            conn.execute("UPDATE project_events SET status='corrected' WHERE event_id=2")
        plan = build_plan(conn, 'ai_purchase')
        assert [c['new_occurred_at'] for c in plan['changes']] == ['2026-08-31']
        result = apply_plan(conn, plan)
        assert result['applied'] == 1


def test_tampered_plan_is_rejected(test_config):
    from evolvmem.project_history_repair import build_plan, apply_plan
    with ContextStore(test_config) as store:
        conn = seed(store, test_config)
        plan = build_plan(conn, 'ai_purchase')
        plan['changes'][0]['new_occurred_at'] = '1999-01-01'
        with pytest.raises(ValueError, match='invalid_repair_plan'):
            apply_plan(conn, plan)


def test_cli_dry_run_is_default_and_apply_requires_saved_plan(test_config, tmp_path, capsys):
    from evolvmem.project_history_repair import main
    with ContextStore(test_config) as store:
        seed(store, test_config)
    plan_path = tmp_path / 'review.json'
    assert main(['--database', str(test_config.db_path), '--project', 'ai_purchase', '--output', str(plan_path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['mode'] == 'dry_run'
    assert len(json.loads(plan_path.read_text())['changes']) == 3
    with ContextStore(test_config) as store:
        assert store._connection().execute('SELECT count(*) FROM project_events WHERE occurred_at IS NOT NULL').fetchone()[0] == 0
    assert main(['--database', str(test_config.db_path), '--project', 'ai_purchase', '--apply-plan', str(plan_path)]) == 0
    assert json.loads(capsys.readouterr().out)['applied'] == 3

def test_conflicting_mapped_source_dates_are_not_guessed(test_config):
    from evolvmem.project_history_repair import build_plan
    with ContextStore(test_config) as store:
        conn = seed(store, test_config)
        with store.transaction():
            conn.execute("UPDATE knowledge_restructure_map SET target_id='1' WHERE legacy_item_id=2")
        plan = build_plan(conn, 'ai_purchase')
        reasons = {item['event_id']: item['reason'] for item in plan['skipped']}
        assert reasons[1] == 'conflicting_source_dates'
        assert reasons[2] == 'no_legacy_mapping'
        assert [item['new_occurred_at'] for item in plan['changes']] == ['2026-08-31']


def test_transaction_rolls_back_audit_and_first_event_on_later_failure(test_config):
    from evolvmem.project_history_repair import build_plan, apply_plan
    import sqlite3
    with ContextStore(test_config) as store:
        conn = seed(store, test_config)
        plan = build_plan(conn, 'ai_purchase')
        with store.transaction():
            conn.execute("CREATE TRIGGER reject_second BEFORE UPDATE ON project_events WHEN NEW.event_id=2 BEGIN SELECT RAISE(ABORT, 'fixture failure'); END")
        with pytest.raises(sqlite3.IntegrityError, match='fixture failure'):
            apply_plan(conn, plan)
        assert conn.execute('SELECT count(*) FROM project_events WHERE occurred_at IS NOT NULL').fetchone()[0] == 0
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='project_history_date_repairs'").fetchone()


def test_source_edit_invalidates_review_even_when_proposed_date_is_same(test_config):
    from evolvmem.project_history_repair import build_plan, apply_plan
    with ContextStore(test_config) as store:
        conn = seed(store, test_config)
        plan = build_plan(conn, 'ai_purchase')
        with store.transaction():
            conn.execute("UPDATE context_items SET identity_key='project:jiangli:progress:log:2026-07-20-2359' WHERE id=1")
        with pytest.raises(ValueError, match='stale_repair_plan'):
            apply_plan(conn, plan)
        assert conn.execute('SELECT count(*) FROM project_events WHERE occurred_at IS NOT NULL').fetchone()[0] == 0

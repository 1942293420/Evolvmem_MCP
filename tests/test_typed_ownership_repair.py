"""Real SQLite regression tests for bounded typed ownership repair."""
import json
import sqlite3

import pytest

from evolvmem.context_store import ContextStore


@pytest.fixture
def db(test_config):
    with ContextStore(test_config) as store:
        conn = store._connection()
        with store.transaction():
            conn.execute("INSERT INTO context_project_registry(project,created_at,updated_at) VALUES('alpha','old','old')")
            for item_id, kind in ((1, 'workstream_checkpoint'), (2, 'project_summary'), (3, 'experience')):
                conn.execute("INSERT INTO context_items(id,identity_key,content_type,project,status,created_at,updated_at) VALUES(?,?,?,'alpha','active','old','old')", (item_id, f'item:{item_id}', kind))
            conn.execute("INSERT INTO continuity_workstreams(id,project,workspace_fingerprint,current_context_id,checkpoint_revision,state_version,status,created_at,updated_at) VALUES('ws','alpha','fp',1,2,3,'completed','old','old')")
            conn.execute("INSERT INTO context_project_rollups(project,current_context_id,status,revision,updated_at) VALUES('alpha',2,'ready',4,'old')")
        yield conn, test_config.db_path


def snapshot(conn, table):
    return [dict(row) for row in conn.execute(f'SELECT * FROM {table}')]


def test_plan_is_readonly_and_only_repairs_current_typed_rows(db):
    from evolvmem.typed_ownership_repair import build_plan, apply_plan
    conn, path = db
    before = snapshot(conn, 'context_items')
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as ro:
        ro.row_factory = sqlite3.Row
        ro.execute('PRAGMA query_only=ON')
        plan = build_plan(ro)
        assert ro.total_changes == 0
    assert [c['item_id'] for c in plan['changes']] == [1, 2]
    assert plan['summary']['unresolved_experience_count'] == 1
    assert plan['changes'][0]['expected_resolution_revision'] == 0
    assert plan['changes'][0]['source_before']['checkpoint_revision'] == 2
    assert plan['changes'][1]['source_before']['revision'] == 4
    assert apply_plan(conn, plan)['applied'] == 2
    assert snapshot(conn, 'context_items') == before
    rows = snapshot(conn, 'context_project_resolutions')
    assert {row['item_id'] for row in rows} == {1, 2}
    for row in rows:
        assert row['decision_source'] == 'automatic'
        assert row['review_state'] == 'not_required'
        assert row['resolved_project'] == 'alpha'
        assert row['proposed_project'] == ''
        assert row['confidence'] == 'medium'
        assert set(json.loads(row['evidence_json'])[0]) == {'source', 'type', 'source_version', 'normalized_value'}
    audit = snapshot(conn, 'typed_ownership_repairs')[0]
    assert json.loads(audit['before_json']) == [None, None]
    assert json.loads(audit['after_json']) == rows


@pytest.mark.parametrize('sql,reason', [
    ("DELETE FROM context_project_registry", 'project_not_registered'),
    ("UPDATE context_project_registry SET status='archived'", 'project_not_active'),
    ("UPDATE context_items SET scope='global' WHERE id=1", 'non_project_scope'),
    ("UPDATE context_items SET project='' WHERE id=1", 'empty_project'),
    ("UPDATE context_items SET status='superseded' WHERE id=1", 'item_not_active'),
    ("UPDATE context_items SET superseded_by=3 WHERE id=1", 'item_superseded'),
    ("UPDATE continuity_workstreams SET project='other'", 'project_mismatch'),
    ("UPDATE continuity_workstreams SET current_context_id=3", 'missing_current_pointer'),
    ("UPDATE context_project_rollups SET status='failed'", 'source_not_ready'),
    ("UPDATE continuity_workstreams SET current_context_id=2; UPDATE context_project_rollups SET current_context_id=NULL", 'pointer_type_mismatch'),
    ("INSERT INTO continuity_workstreams SELECT 'duplicate',project,workspace_fingerprint,parent_id,current_context_id,checkpoint_revision,state_version,status,repo_kind,repo_branch,repo_root_commit,repo_head_commit,created_at,updated_at,completed_at FROM continuity_workstreams", 'ambiguous_current_pointer'),
])
def test_unsafe_rows_are_skipped(db, sql, reason):
    from evolvmem.typed_ownership_repair import build_plan
    conn, _ = db
    conn.executescript(sql)
    conn.commit()
    plan = build_plan(conn)
    assert reason in {row['reason'] for row in plan['skipped']}


@pytest.mark.parametrize('decision', ['human', 'automatic'])
def test_existing_resolution_is_never_overwritten(db, decision):
    from evolvmem.typed_ownership_repair import build_plan, apply_plan
    conn, _ = db
    conn.execute("INSERT INTO context_project_resolutions(item_id,resolution_state,decision_source,review_state,created_at,updated_at) VALUES(1,'unresolved',?,'rejected','old','old')", (decision,))
    conn.commit()
    existing = snapshot(conn, 'context_project_resolutions')
    plan = build_plan(conn)
    assert plan['skipped'][0]['reason'] == 'existing_resolution_preserved'
    assert apply_plan(conn, plan)['applied'] == 1
    assert snapshot(conn, 'context_project_resolutions')[0] == existing[0]


@pytest.mark.parametrize('sql', [
    "UPDATE continuity_workstreams SET current_context_id=3",
    "UPDATE continuity_workstreams SET state_version=state_version+1",
    "UPDATE context_items SET scope='global' WHERE id=2",
    "UPDATE context_items SET project='other' WHERE id=2",
    "UPDATE context_project_registry SET revision=revision+1",
    "INSERT INTO context_project_resolutions(item_id,resolution_state,decision_source,created_at,updated_at) VALUES(2,'ignored','human','now','now')",
])
def test_stale_plan_aborts_whole_batch(db, sql):
    from evolvmem.typed_ownership_repair import build_plan, apply_plan
    conn, _ = db
    plan = build_plan(conn)
    conn.execute(sql)
    conn.commit()
    before = snapshot(conn, 'context_project_resolutions')
    with pytest.raises(ValueError, match='stale_repair_plan'):
        apply_plan(conn, plan)
    assert snapshot(conn, 'context_project_resolutions') == before
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='typed_ownership_repairs'").fetchone()


def test_invalid_hash_and_rehashed_unsafe_plan_are_rejected(db):
    from evolvmem.typed_ownership_repair import build_plan, apply_plan, _digest
    conn, _ = db
    plan = build_plan(conn)
    plan['changes'][0]['project'] = 'other'
    with pytest.raises(ValueError, match='invalid_repair_plan'):
        apply_plan(conn, plan)
    plan['plan_sha256'] = _digest(plan)
    with pytest.raises(ValueError, match='stale_repair_plan'):
        apply_plan(conn, plan)
    assert snapshot(conn, 'context_project_resolutions') == []


def test_late_sql_failure_rolls_back_writes_and_audit(db):
    from evolvmem.typed_ownership_repair import build_plan, apply_plan
    conn, _ = db
    plan = build_plan(conn)
    conn.execute("CREATE TRIGGER reject_second BEFORE INSERT ON context_project_resolutions WHEN NEW.item_id=2 BEGIN SELECT RAISE(ABORT,'synthetic_failure'); END")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match='synthetic_failure'):
        apply_plan(conn, plan)
    assert snapshot(conn, 'context_project_resolutions') == []
    assert not conn.execute("SELECT 1 FROM sqlite_master WHERE name='typed_ownership_repairs'").fetchone()


def test_idempotent_apply_and_rollback(db):
    from evolvmem.typed_ownership_repair import build_plan, apply_plan, rollback_plan
    conn, _ = db
    plan = build_plan(conn)
    assert apply_plan(conn, plan)['applied'] == 2
    assert apply_plan(conn, plan)['already_applied']
    assert len(snapshot(conn, 'typed_ownership_repairs')) == 1
    assert rollback_plan(conn, plan)['rolled_back'] == 2
    assert snapshot(conn, 'context_project_resolutions') == []
    assert rollback_plan(conn, plan)['already_rolled_back']
    with pytest.raises(ValueError, match='repair_previously_rolled_back'):
        apply_plan(conn, plan)


@pytest.mark.parametrize('sql', [
    "UPDATE context_project_resolutions SET decision_source='human',revision=2 WHERE item_id=2",
    "UPDATE continuity_workstreams SET current_context_id=3",
    "UPDATE context_items SET project='other' WHERE id=2",
])
def test_rollback_preserves_later_changes_for_entire_batch(db, sql):
    from evolvmem.typed_ownership_repair import build_plan, apply_plan, rollback_plan
    conn, _ = db
    plan = build_plan(conn)
    apply_plan(conn, plan)
    conn.execute(sql)
    conn.commit()
    before = snapshot(conn, 'context_project_resolutions')
    assert apply_plan(conn, plan)['already_applied']
    with pytest.raises(ValueError, match='rollback_conflict'):
        rollback_plan(conn, plan)
    assert snapshot(conn, 'context_project_resolutions') == before
    assert snapshot(conn, 'typed_ownership_repairs')[0]['rolled_back_at'] is None


def test_cli_uses_only_explicit_database_readonly_by_default(db, tmp_path, capsys, monkeypatch):
    from evolvmem.typed_ownership_repair import main
    conn, path = db
    def no_store(*args, **kwargs):
        raise AssertionError('CLI must not initialize ContextStore')
    monkeypatch.setattr(ContextStore, '__init__', no_store)
    plan_path = tmp_path / 'plan.json'
    assert main(['--database', str(path), '--output', str(plan_path)]) == 0
    assert json.loads(capsys.readouterr().out)['mode'] == 'dry_run'
    assert snapshot(conn, 'context_project_resolutions') == []
    assert main(['--database', str(path), '--apply-plan', str(plan_path)]) == 0
    assert json.loads(capsys.readouterr().out)['applied'] == 2
    assert main(['--database', str(path), '--rollback-plan', str(plan_path)]) == 0
    assert json.loads(capsys.readouterr().out)['rolled_back'] == 2
    with pytest.raises(FileNotFoundError):
        main(['--database', str(tmp_path / 'missing.db')])


def test_alias_is_not_a_canonical_project_match(db):
    from evolvmem.typed_ownership_repair import build_plan
    conn, _ = db
    conn.execute("INSERT INTO context_project_aliases(alias,project,created_at,updated_at) VALUES('nickname','alpha','old','old')")
    conn.execute("UPDATE context_items SET project='nickname' WHERE id=1")
    conn.execute("UPDATE continuity_workstreams SET project='nickname'")
    conn.commit()
    plan = build_plan(conn)
    assert plan['skipped'][0] == {'item_id': 1, 'reason': 'project_not_registered'}


def test_late_rollback_failure_preserves_all_resolution_rows_and_audit(db):
    from evolvmem.typed_ownership_repair import build_plan, apply_plan, rollback_plan
    conn, _ = db
    plan = build_plan(conn)
    apply_plan(conn, plan)
    before = snapshot(conn, 'context_project_resolutions')
    conn.execute("CREATE TRIGGER reject_second_delete BEFORE DELETE ON context_project_resolutions WHEN OLD.item_id=2 BEGIN SELECT RAISE(ABORT,'synthetic_failure'); END")
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match='synthetic_failure'):
        rollback_plan(conn, plan)
    assert snapshot(conn, 'context_project_resolutions') == before
    assert snapshot(conn, 'typed_ownership_repairs')[0]['rolled_back_at'] is None

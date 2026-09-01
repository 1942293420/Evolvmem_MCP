"""Tests for the one-shot historical backfill: plan / apply / verify."""

from datetime import datetime, timezone
import json
import sqlite3
import zlib

import numpy as np
import pytest

from evolvmem import maintenance_cli
from evolvmem.config import Config
from evolvmem.context_migration import LegacyMemoryMigrator
from evolvmem.context_store import ContextStore
from evolvmem.maintenance import (
    MaintenanceError,
    build_plan,
    verify_invariants,
)
from evolvmem.maintenance_cli import apply_plan, main as cli_main
from evolvmem.memory_store import MemoryStore
from evolvmem.project_store import ProjectStore


class _FakeEngine:
    """Deterministic stand-in embedding engine for the vector rebuild step."""

    def __init__(self, dim):
        self.is_loaded = True
        self._dim = dim

    def encode_document(self, text):
        rng = np.random.default_rng(zlib.crc32(text.encode("utf-8")))
        return rng.random(self._dim, dtype=np.float32)

    def close(self):
        pass


def _fake_engine(config):
    return _FakeEngine(config.embedding_dim)


# ---- fixture: a legacy database with the three signal classes ----


@pytest.fixture
def legacy_db_with_rows(tmp_path):
    """Legacy schema + rows: project key, conflicting tags, no signal, global.

    Mirrors tests/test_legacy_compat.py's boot: MemoryStore creates the
    legacy schema, ContextStore adds the Context schema beside it.
    """
    config = Config(data_dir=tmp_path)
    with MemoryStore(config) as legacy:
        # canonical key + bare project tag: two medium signal types agree
        legacy.add(
            key="project:alpha:decision:db",
            value="Use SQLite first everywhere.",
            attribute="decision",
            tags=["alpha"],
        )
        legacy.add(
            key="reporting:etl:choice",
            value="conflicting tag signals row",
            tags=["分类:alpha", "分类:beta"],
        )
        legacy.add(key="random-note", value="no project signal at all")
        legacy.add(
            key="user preference",
            value="dark mode is preferred",
            attribute="preference",
        )
    _register_projects(config, "alpha", "beta")
    return tmp_path


def _register_projects(config, *names):
    with ContextStore(config) as store:
        project_store = ProjectStore(
            store._connection(), store._require_transaction, generic_names=()
        )
        with store.transaction():
            for name in names:
                project_store.register_project(name)


def _table_counts(config):
    conn = sqlite3.connect(config.db_path)
    try:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in tables
        }
    finally:
        conn.close()


def _resolution_rows(config):
    conn = sqlite3.connect(config.db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT item_id, resolution_state, review_state, resolved_project "
            "FROM context_project_resolutions ORDER BY item_id"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _item_projects(config):
    conn = sqlite3.connect(config.db_path)
    try:
        rows = conn.execute(
            "SELECT id, project, status FROM context_items ORDER BY id"
        ).fetchall()
        return {row[0]: (row[1], row[2]) for row in rows}
    finally:
        conn.close()


def _premigrate(config):
    """Batch-migrate without a decider: mapped rows, empty projects, no resolutions."""
    with ContextStore(config) as store:
        report = LegacyMemoryMigrator(store, config).migrate()
    return report


# ---- the brief's flow tests ----


def test_plan_is_read_only_and_deterministic(legacy_db_with_rows):
    before = _table_counts(Config(data_dir=legacy_db_with_rows))
    first = build_plan(Config(data_dir=legacy_db_with_rows))
    second = build_plan(Config(data_dir=legacy_db_with_rows))
    assert first.digest == second.digest
    after = _table_counts(Config(data_dir=legacy_db_with_rows))
    assert before == after  # plan never writes


def test_apply_migrates_and_is_idempotent(legacy_db_with_rows):
    config = Config(data_dir=legacy_db_with_rows)
    plan = build_plan(config)
    assert plan.unmapped == 4

    report = apply_plan(
        config,
        plan_digest=plan.digest,
        timestamp=datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc),
    )
    assert report.plan_digest == plan.digest
    assert report.backup_directory

    after = build_plan(config)
    assert after.unmapped == 0
    assert after.planned_actions == ()
    # the digest moves only with the database fingerprint; actions stay empty
    assert after.digest != plan.digest

    second = apply_plan(
        config,
        plan_digest=after.digest,
        timestamp=datetime(2026, 9, 2, 0, 0, 1, tzinfo=timezone.utc),
    )
    assert second.planned_actions == 0
    third = build_plan(config)
    assert third.digest == after.digest  # an empty apply writes nothing
    assert third.planned_actions == ()


# ---- plan content ----


def test_plan_counts_and_migrate_actions(legacy_db_with_rows):
    plan = build_plan(Config(data_dir=legacy_db_with_rows))
    assert plan.legacy_total == 4
    assert plan.mapped == 0
    assert plan.unmapped == 4
    # resolved: project:alpha key; conflict: 分类:alpha vs 分类:beta;
    # unresolved: no signal; global: preference scope
    assert plan.resolved == 1
    assert plan.conflict == 1
    assert plan.unresolved == 1
    assert plan.global_ == 1
    assert [action.action for action in plan.planned_actions] == ["migrate"] * 4
    assert [action.item_ref for action in plan.planned_actions] == [
        "legacy:1",
        "legacy:2",
        "legacy:3",
        "legacy:4",
    ]
    assert all(action.reason == "unmapped_legacy_row" for action in plan.planned_actions)
    assert len(plan.digest) == 64


def test_plan_on_database_without_legacy_table(tmp_path):
    config = Config(data_dir=tmp_path)
    with ContextStore(config):
        pass
    plan = build_plan(config)
    assert plan.legacy_total == 0
    assert plan.planned_actions == ()
    assert build_plan(config).digest == plan.digest


# ---- historical rows: project set but no resolution row (Task 3 ledger) ----


def test_backfill_covers_mapped_rows_missing_resolutions(legacy_db_with_rows):
    config = Config(data_dir=legacy_db_with_rows)
    _premigrate(config)
    # Simulate the write-path lazy migration: the row got the decider's
    # project inside its write transaction but no resolution row was written.
    with ContextStore(config) as store:
        with store.transaction():
            store._connection().execute(
                "UPDATE context_items SET project='alpha' WHERE id=1"
            )

    plan = build_plan(config)
    assert plan.legacy_total == 4
    assert plan.mapped == 4
    assert plan.unmapped == 0
    actions = {(action.item_ref, action.action) for action in plan.planned_actions}
    # every mapped row still lacks its resolution row -> one action each
    assert ("item:1", "backfill_project") in actions  # resolved, project kept
    assert ("item:2", "queue_review") in actions  # conflict
    assert ("item:3", "queue_review") in actions  # unresolved
    assert ("item:4", "backfill_project") in actions  # global resolution row

    report = apply_plan(
        config,
        plan_digest=plan.digest,
        timestamp=datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc),
    )
    assert report.planned_actions == 4

    resolutions = _resolution_rows(config)
    assert len(resolutions) == 4
    by_item = {row["item_id"]: row for row in resolutions}
    assert by_item[1]["resolution_state"] == "resolved"
    assert by_item[1]["resolved_project"] == "alpha"
    assert by_item[1]["review_state"] == "not_required"
    assert by_item[2]["resolution_state"] == "conflict"
    assert by_item[2]["review_state"] == "pending"
    assert by_item[2]["resolved_project"] == ""
    assert by_item[3]["resolution_state"] == "unresolved"
    assert by_item[3]["review_state"] == "pending"
    assert by_item[4]["resolution_state"] == "global"

    projects = _item_projects(config)
    assert projects[1] == ("alpha", "active")
    assert projects[2][0] == ""  # conflict stays project-free
    assert projects[3][0] == ""  # unresolved stays project-free
    assert projects[4][0] == ""  # global stays project-free

    followup = build_plan(config)
    assert followup.planned_actions == ()


def test_apply_rejects_stale_digest(legacy_db_with_rows):
    config = Config(data_dir=legacy_db_with_rows)
    with pytest.raises(MaintenanceError) as excinfo:
        apply_plan(config, plan_digest="0" * 64)
    assert excinfo.value.code == "plan_digest_mismatch"
    # nothing was migrated
    assert build_plan(config).unmapped == 4


# ---- verify ----


def test_verify_passes_after_apply(legacy_db_with_rows):
    config = Config(data_dir=legacy_db_with_rows)
    plan = build_plan(config)
    apply_plan(
        config,
        plan_digest=plan.digest,
        timestamp=datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc),
        embedding_engine=_fake_engine(config),
    )
    report = verify_invariants(config)
    assert report.ok is True
    assert all(invariant.passed for invariant in report.invariants)
    assert [invariant.name for invariant in report.invariants] == [
        "mapping_lag_zero",
        "mapped_item_layers",
        "resolved_project_nonempty",
        "review_counts_match_plan",
        "project_summary_singleton",
        "vector_documents_match",
        "plan_digest_stable",
    ]


def test_verify_detects_project_cleared_after_apply(legacy_db_with_rows):
    config = Config(data_dir=legacy_db_with_rows)
    plan = build_plan(config)
    apply_plan(
        config,
        plan_digest=plan.digest,
        timestamp=datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc),
        embedding_engine=_fake_engine(config),
    )
    with ContextStore(config) as store:
        with store.transaction():
            store._connection().execute(
                "UPDATE context_items SET project='' WHERE id=1"
            )
    report = verify_invariants(config)
    assert report.ok is False
    by_name = {invariant.name: invariant for invariant in report.invariants}
    assert by_name["resolved_project_nonempty"].passed is False


def test_verify_detects_missing_layer(legacy_db_with_rows):
    config = Config(data_dir=legacy_db_with_rows)
    plan = build_plan(config)
    apply_plan(
        config,
        plan_digest=plan.digest,
        timestamp=datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc),
        embedding_engine=_fake_engine(config),
    )
    with ContextStore(config) as store:
        with store.transaction():
            store._connection().execute(
                "DELETE FROM context_layers WHERE item_id=1 AND layer='l1'"
            )
    report = verify_invariants(config)
    assert report.ok is False
    by_name = {invariant.name: invariant for invariant in report.invariants}
    assert by_name["mapped_item_layers"].passed is False


# ---- CLI ----


def _cli(*argv):
    return cli_main(list(argv))


def test_cli_plan_json(legacy_db_with_rows, capsys):
    rc = _cli("--data-dir", str(legacy_db_with_rows), "plan", "--json")
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["legacy_total"] == 4
    assert payload["unmapped"] == 4
    assert len(payload["digest"]) == 64


def test_cli_apply_requires_yes(legacy_db_with_rows, capsys):
    plan = build_plan(Config(data_dir=legacy_db_with_rows))
    with pytest.raises(SystemExit) as excinfo:
        _cli(
            "--data-dir",
            str(legacy_db_with_rows),
            "apply",
            "--plan-digest",
            plan.digest,
        )
    assert excinfo.value.code == 2
    capsys.readouterr()


def test_cli_apply_rejects_bad_digest(legacy_db_with_rows, capsys):
    rc = _cli(
        "--data-dir",
        str(legacy_db_with_rows),
        "apply",
        "--plan-digest",
        "0" * 64,
        "--yes",
    )
    assert rc == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == "plan_digest_mismatch"
    assert build_plan(Config(data_dir=legacy_db_with_rows)).unmapped == 4


def test_cli_apply_then_verify_roundtrip(legacy_db_with_rows, capsys, monkeypatch):
    monkeypatch.setattr(
        maintenance_cli, "_load_embedding_engine", _fake_engine
    )
    plan = build_plan(Config(data_dir=legacy_db_with_rows))
    rc = _cli(
        "--data-dir",
        str(legacy_db_with_rows),
        "apply",
        "--plan-digest",
        plan.digest,
        "--yes",
        "--json",
    )
    assert rc == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["ok"] is True
    assert applied["backup_directory"]
    assert str(legacy_db_with_rows) not in json.dumps(applied)

    rc = _cli("--data-dir", str(legacy_db_with_rows), "verify", "--json")
    assert rc == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["ok"] is True
    assert all(entry["passed"] for entry in verified["invariants"])


def test_cli_verify_fails_on_tampered_database(legacy_db_with_rows, capsys):
    config = Config(data_dir=legacy_db_with_rows)
    plan = build_plan(config)
    apply_plan(
        config,
        plan_digest=plan.digest,
        timestamp=datetime(2026, 9, 2, 0, 0, 0, tzinfo=timezone.utc),
        embedding_engine=_fake_engine(config),
    )
    with ContextStore(config) as store:
        with store.transaction():
            store._connection().execute(
                "UPDATE context_items SET project='' WHERE id=1"
            )
    rc = _cli("--data-dir", str(legacy_db_with_rows), "verify", "--json")
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False

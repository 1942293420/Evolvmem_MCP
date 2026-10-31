"""Current source inventory and bounded queue restoration; no provider calls."""
import copy
import json
import sqlite3

import pytest

from evolvmem import auto_organization as org, conversation_tasks
from evolvmem import governance_migration as migration
from evolvmem.session_archive import SessionArchiver
from tests.test_auto_organization import base_service, service
from tests.test_primary_ownership import prepare as prepare_units


def plan(service):
    return migration.build_plan(service.store._connection(),
                                rule_revision=service.knowledge().rules.read()["revision"])


def completed(service, monkeypatch, *, manual=False):
    worker, task, _, archive = prepare_units(service, monkeypatch, ["evo 保留原话并核对范围。"])
    conn = service.store._connection()
    conn.execute("UPDATE organization_tasks SET status='completed',stage='done' WHERE id=?", (task["id"],))
    if manual:
        conn.execute("UPDATE organization_units SET decision='manual',project='evo',projects='[\"evo\"]' WHERE task_id=?", (task["id"],))
    conn.commit()
    return task, archive


def task_row(service, identity):
    return dict(service.store._connection().execute("SELECT * FROM organization_tasks WHERE id=?", (identity,)).fetchone())


def test_prepare_select_only_and_current_completed_retry(service, monkeypatch):
    task, archive = completed(service, monkeypatch)
    conn = service.store._connection()
    before = conn.total_changes
    conn.execute("PRAGMA query_only=ON")
    try:
        result = plan(service)
    finally:
        conn.execute("PRAGMA query_only=OFF")
    assert conn.total_changes == before
    action = next(a for a in result["actions"] if a["source_key"] == task["source_key"])
    assert action["action"] == "retry" and action["task_id"] == task["id"]
    assert result["summary"]["completed_unassigned_tasks"] == 1
    assert all(a["source_key"] != f"archive:{archive.id}" for a in result["actions"])
    assert "source_snapshot" not in json.dumps(result)
    assert "保留原话并核对范围" not in json.dumps(result, ensure_ascii=False)


def test_old_completed_retry_reuses_task_and_preserves_manual(service, monkeypatch):
    task, _ = completed(service, monkeypatch)
    conn = service.store._connection()
    # A second manual unit remains byte-for-byte intact during the restore.
    conn.execute("UPDATE organization_units SET decision='manual',project='evo',projects='[\"evo\"]' WHERE task_id=?", (task["id"],))
    conn.execute("UPDATE organization_tasks SET status='failed' WHERE id=?", (task["id"],))
    conn.commit()
    before = [dict(r) for r in conn.execute("SELECT * FROM organization_units")]
    prepared = plan(service)
    out = migration.apply_plan(service, prepared)
    assert out["applied"] == 1
    assert task_row(service, task["id"])["stage"] == "assignment"
    assert task_row(service, task["id"])["status"] == "pending"
    assert [dict(r) for r in conn.execute("SELECT * FROM organization_units")] == before
    assert conn.execute("SELECT count(*) FROM organization_tasks").fetchone()[0] == 1
    assert migration.apply_plan(service, prepared)["applied"] == 0


@pytest.mark.parametrize("change", ["manual", "body", "rule", "claim"])
def test_stale_plan_rejected_before_any_queue_write(service, monkeypatch, change):
    task, archive = completed(service, monkeypatch)
    prepared = plan(service)
    conn = service.store._connection()
    if change == "manual":
        conn.execute("UPDATE organization_units SET decision='manual',revision=revision+1 WHERE task_id=?", (task["id"],))
    elif change == "body":
        conn.execute("UPDATE conversation_history SET body=body||' changed' WHERE archive_id=?", (archive.id,))
    elif change == "claim":
        conn.execute("UPDATE organization_tasks SET status='running' WHERE id=?", (task["id"],))
    else:
        rules = service.knowledge().rules.read()
        service.knowledge().rules.save({"expected_revision": rules["revision"], "instructions": rules["instructions"] + "\n额外要求"})
    conn.commit()
    with pytest.raises(ValueError, match="migration_revision_conflict"):
        migration.apply_plan(service, prepared)
    assert task_row(service, task["id"])["status"] != "pending"


def test_plan_tamper_limit_and_resume_new_sources(service):
    for index in range(7):
        SessionArchiver(service.config, service.store).archive_session(
            "", "kimi", f"new-{index}", json.dumps({"messages": [{"role": "user", "content": f"evo 待分类事项 {index}"}]}))
    prepared = plan(service)
    assert len(prepared["actions"]) == 7
    tampered = copy.deepcopy(prepared)
    tampered["actions"][0]["source_key"] = "archive:99999"
    with pytest.raises(ValueError, match="invalid_migration_plan"):
        migration.apply_plan(service, tampered)
    assert migration.apply_plan(service, prepared)["applied"] == 5
    assert migration.apply_plan(service, prepared)["applied"] == 2
    assert migration.apply_plan(service, prepared)["applied"] == 0


def test_rollback_only_unstarted_queue_and_never_new_manual_decision(service, monkeypatch):
    task, _ = completed(service, monkeypatch)
    prepared = plan(service)
    before = task_row(service, task["id"])
    migration.apply_plan(service, prepared)
    result = migration.rollback_plan(service, prepared)
    assert result["rolled_back"] == 1
    assert task_row(service, task["id"]) == before
    assert migration.apply_plan(service, prepared)["applied"] == 0


def test_rollback_rejects_worker_or_human_changes(service, monkeypatch):
    task, _ = completed(service, monkeypatch)
    prepared = plan(service)
    migration.apply_plan(service, prepared)
    conn = service.store._connection()
    conn.execute("UPDATE organization_units SET decision='manual',revision=revision+1 WHERE task_id=?", (task["id"],))
    conn.commit()
    result = migration.rollback_plan(service, prepared)
    assert result["rolled_back"] == 0 and result["retained"] == 1
    assert conn.execute("SELECT decision FROM organization_units").fetchone()[0] == "manual"


def test_missing_body_is_gap_not_empty_and_repair_is_bounded_cas(service):
    archive = SessionArchiver(service.config, service.store).archive_session(
        "", "kimi", "missing-body", json.dumps({"messages": [{"role": "user", "content": "evo 恢复正文仍保留原始档案。"}]}))
    conn = service.store._connection()
    original = dict(conn.execute("SELECT * FROM session_archives WHERE id=?", (archive.id,)).fetchone())
    conn.execute("DELETE FROM conversation_history WHERE archive_id=?", (archive.id,)); conn.commit()
    prepared = plan(service)
    entry = next(e for e in prepared["sources"] if e["source_key"] == f"archive:{archive.id}")
    assert entry["cleaning"]["body_state"] == "missing"
    assert entry["reason"] == "source_body_unverifiable"
    assert not prepared["actions"]
    assert len(prepared["cleaning_repairs"]) == 1
    assert migration.repair_cleaning(service, prepared)["repaired"] == 1
    assert migration.repair_cleaning(service, prepared)["repaired"] == 0
    assert dict(conn.execute("SELECT * FROM session_archives WHERE id=?", (archive.id,)).fetchone()) == original
    assert conn.execute("SELECT body FROM conversation_history").fetchone()[0]
    assert plan(service)["actions"]


def test_retired_snapshot_deleted_and_system_sources_do_not_queue(service):
    archiver = SessionArchiver(service.config, service.store)
    ids = [archiver.archive_session("", "codex", f"family:{i}", json.dumps({"messages": [{"role": "user", "content": f"evo {i}"}]})).id for i in range(2)]
    current = archiver.archive_session("", "kimi", "system", json.dumps({"messages": [{"role": "user", "content": "evo"}]}))
    conn = service.store._connection()
    row = conn.execute("SELECT payload_sha256 FROM session_archives WHERE id=?", (current.id,)).fetchone()
    conn.execute("INSERT INTO archive_source_exclusions VALUES(?,?,?,?)", (current.id, row[0], "system", "now")); conn.commit()
    prepared = plan(service)
    actions = {a["source_key"] for a in prepared["actions"]}
    assert f"archive:{ids[0]}" not in actions and f"archive:{current.id}" not in actions
    assert f"archive:{ids[1]}" in actions

def test_rollback_preserves_new_scope_protection(service, monkeypatch):
    task, _ = completed(service, monkeypatch)
    prepared = plan(service)
    migration.apply_plan(service, prepared)
    conn = service.store._connection()
    unit = conn.execute("SELECT digest FROM organization_units WHERE task_id=?", (task["id"],)).fetchone()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(organization_scope_exclusions)")]
    values = {"source_key": task["source_key"], "digest": unit[0], "scope_key": "project:evo",
              "reason": "人工撤回", "content_stamp": "confirmed-source", "created_at": "now", "updated_at": "now"}
    selected = [c for c in cols if c in values]
    conn.execute("INSERT INTO organization_scope_exclusions(" + ",".join(selected) + ") VALUES(" +
                 ",".join("?" for _ in selected) + ")", [values[c] for c in selected]); conn.commit()
    result = migration.rollback_plan(service, prepared)
    assert result["rolled_back"] == 0 and result["retained"] == 1


def test_invalid_stored_messages_are_not_enqueued_as_empty_snapshot(service):
    archive = SessionArchiver(service.config, service.store).archive_session(
        "", "kimi", "corrupt-messages", json.dumps({"messages": [{"role": "user", "content": "evo 完整对话。"}]}))
    conn = service.store._connection()
    conn.execute("UPDATE conversation_history SET messages='broken' WHERE archive_id=?", (archive.id,)); conn.commit()
    prepared = plan(service)
    with pytest.raises(ValueError, match="migration_snapshot_conflict"):
        migration.apply_plan(service, prepared)
    assert conn.execute("SELECT count(*) FROM organization_tasks").fetchone()[0] == 0


def test_current_assigned_archive_missing_body_is_repairable_not_reclassified(service):
    archive = SessionArchiver(service.config, service.store).archive_session(
        "evo", "kimi", "assigned-missing", json.dumps({"messages": [{"role": "user", "content": "已归属原文补齐。"}]}))
    conn = service.store._connection()
    conn.execute("DELETE FROM conversation_history WHERE archive_id=?", (archive.id,)); conn.commit()
    prepared = plan(service)
    assert not prepared["actions"]
    assert [e["source_key"] for e in prepared["cleaning_repairs"]] == [f"archive:{archive.id}"]
    assert migration.repair_cleaning(service, prepared)["repaired"] == 1
    assert conn.execute("SELECT project FROM session_archives").fetchone()[0] == "evo"


def test_missing_payload_stays_unavailable_and_does_not_create_empty_body(service):
    archive = SessionArchiver(service.config, service.store).archive_session(
        "", "kimi", "lost-payload", json.dumps({"messages": [{"role": "user", "content": "evo 原文不可读不能说空。"}]}))
    conn = service.store._connection()
    row = conn.execute("SELECT payload_path FROM session_archives WHERE id=?", (archive.id,)).fetchone()
    (service.config.data_dir / row[0]).unlink()
    conn.execute("DELETE FROM conversation_history WHERE archive_id=?", (archive.id,)); conn.commit()
    prepared = plan(service)
    result = migration.repair_cleaning(service, prepared)
    assert result["repaired"] == 0 and result["unavailable"] == [f"archive:{archive.id}"]
    assert conn.execute("SELECT count(*) FROM conversation_history").fetchone()[0] == 0

def test_cleaning_repair_refuses_newer_logical_snapshot(service):
    archiver = SessionArchiver(service.config, service.store)
    old = archiver.archive_session("", "codex", "repair-family:v1", json.dumps({"messages": [{"role": "user", "content": "旧正文。"}]}))
    conn = service.store._connection()
    conn.execute("DELETE FROM conversation_history WHERE archive_id=?", (old.id,)); conn.commit()
    prepared = plan(service)
    archiver.archive_session("", "codex", "repair-family:v2", json.dumps({"messages": [{"role": "user", "content": "新正文。"}]}))
    with pytest.raises(ValueError, match="migration_revision_conflict"):
        migration.repair_cleaning(service, prepared)
    assert not conn.execute("SELECT 1 FROM conversation_history WHERE archive_id=?", (old.id,)).fetchone()


def test_rollback_retains_work_after_source_change(service, monkeypatch):
    task, archive = completed(service, monkeypatch)
    prepared = plan(service)
    migration.apply_plan(service, prepared)
    conn = service.store._connection()
    conn.execute("UPDATE conversation_history SET body=body||' 改动' WHERE archive_id=?", (archive.id,)); conn.commit()
    result = migration.rollback_plan(service, prepared)
    assert result == {"rolled_back": 0, "retained": 1}


def test_cli_prepare_does_not_initialize_schema_or_touch_database(service, tmp_path):
    from evolvmem.knowledge_rules import KnowledgeRules
    import subprocess
    import sys
    db_path = service.config.db_path
    output = tmp_path / "prepare.json"
    before = service.store._connection().execute("SELECT count(*) FROM sqlite_master").fetchone()[0]
    subprocess.run([sys.executable, "-m", "evolvmem.governance_migration", "prepare",
                    "--db", str(db_path), "--data-dir", str(service.config.data_dir),
                    "--output", str(output)], check=True)
    result = json.loads(output.read_text())
    assert result["policy_sha256"] == KnowledgeRules(service.config.data_dir).read()["revision"]
    assert service.store._connection().execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == before

def test_cli_apply_item_queue_and_rollback_without_initializing_service(service, tmp_path):
    import subprocess
    import sys
    item = service.knowledge().create({"title": "离线新源", "body": "evo 数据库来源先分类，再提炼。", "scope": "project", "action": "draft"})
    prepared = plan(service)
    assert [e["source_key"] for e in prepared["actions"]] == [f"item:{item['id']}"]
    path = tmp_path / "exact-plan.json"
    path.write_text(json.dumps(prepared, ensure_ascii=False))
    base = [sys.executable, "-m", "evolvmem.governance_migration"]
    options = ["--db", str(service.config.db_path), "--data-dir", str(service.config.data_dir), "--plan", str(path)]
    applied = subprocess.run([*base, "apply", *options], text=True, capture_output=True)
    assert applied.returncode == 0, applied.stderr
    assert json.loads(applied.stdout)["applied"] == 1
    rolled = subprocess.run([*base, "rollback", *options], text=True, capture_output=True)
    assert rolled.returncode == 0, rolled.stderr
    assert json.loads(rolled.stdout)["rolled_back"] == 1
    assert service.store._connection().execute("SELECT count(*) FROM organization_tasks").fetchone()[0] == 0

def test_processed_outputs_remain_audited_after_rollback(service, monkeypatch):
    from evolvmem.context_models import ContextMode
    task, _ = completed(service, monkeypatch)
    prepared = plan(service)
    migration.apply_plan(service, prepared)
    worker = org.OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    worker._process(service, org.task_view(service, task["id"]))
    conn = service.store._connection()
    before = conn.execute("SELECT count(*) FROM project_events").fetchone()[0]
    assert before == 1
    assert migration.rollback_plan(service, prepared) == {"rolled_back": 0, "retained": 1}
    assert conn.execute("SELECT count(*) FROM project_events").fetchone()[0] == before
    assert conn.execute("SELECT state FROM governance_migration_audit").fetchone()[0] == "applied"

def test_cleaning_repair_uses_physical_manifest_hash_after_lossless_compaction(service, monkeypatch):
    archive = SessionArchiver(service.config, service.store).archive_session(
        "", "kimi", "compacted-source", json.dumps({"messages": [{"role": "user", "content": "evo 无损压缩后原文仍可恢复。"}]}))
    conn = service.store._connection()
    # Compaction deliberately preserves the legacy evidence revision while
    # session_archive_storage binds the replacement physical manifest.
    conn.execute("UPDATE session_archives SET payload_sha256=? WHERE id=?", ("a" * 64, archive.id))
    conn.execute("DELETE FROM conversation_history WHERE archive_id=?", (archive.id,)); conn.commit()
    prepared = plan(service)
    def no_key_creation(*args, **kwargs):
        raise AssertionError("read-only archive decoding must never call key creation")
    monkeypatch.setattr(SessionArchiver, "_load_or_create_key", no_key_creation)
    assert migration.repair_cleaning(service, prepared)["repaired"] == 1
    assert conn.execute("SELECT payload_sha256 FROM session_archives").fetchone()[0] == "a" * 64


def test_cleaning_size_budget_rejects_oversized_before_unpack(service, monkeypatch):
    from evolvmem.archive_storage import ArchiveStorage
    archive = SessionArchiver(service.config, service.store).archive_session(
        "", "kimi", "oversized-repair", json.dumps({"messages": [{"role": "user", "content": "safe original"}]}))
    conn = service.store._connection()
    with service.store.transaction():
        conn.execute("DELETE FROM conversation_history WHERE archive_id=?", (archive.id,))
        conn.execute("UPDATE session_archive_storage SET logical_bytes=? WHERE archive_id=?", (33 * 1024 * 1024, archive.id))
    prepared = plan(service)
    monkeypatch.setattr(ArchiveStorage, "unpack", lambda *a: pytest.fail("oversized payload must not be decrypted"))
    result = migration.repair_cleaning(service, prepared)
    assert result["repaired"] == 0
    assert result["oversized_pending"] == [{"source_key": f"archive:{archive.id}", "logical_bytes": 33 * 1024 * 1024}]
    assert not conn.execute("SELECT 1 FROM conversation_history WHERE archive_id=?", (archive.id,)).fetchone()


def test_cleaning_unknown_size_is_pending_without_decryption(service, monkeypatch):
    from evolvmem.archive_storage import ArchiveStorage
    archive = SessionArchiver(service.config, service.store).archive_session(
        "", "kimi", "unknown-size-repair", json.dumps({"messages": [{"role": "user", "content": "safe original"}]}))
    with service.store.transaction():
        conn = service.store._connection()
        conn.execute("DELETE FROM conversation_history WHERE archive_id=?", (archive.id,))
        conn.execute("DELETE FROM session_archive_storage WHERE archive_id=?", (archive.id,))
    monkeypatch.setattr(ArchiveStorage, "unpack", lambda *a: pytest.fail("unknown size must not be decrypted"))
    result = migration.repair_cleaning(service, plan(service))
    assert result["repaired"] == 0
    assert result["unknown_size_pending"] == [f"archive:{archive.id}"]


def test_cleaning_batch_budget_resumes_without_oversized_starvation(service):
    ids = []
    for i in range(3):
        archive = SessionArchiver(service.config, service.store).archive_session(
            "", "kimi", f"budget-{i}", json.dumps({"messages": [{"role": "user", "content": "safe original " * 30}]}))
        ids.append(archive.id)
    conn = service.store._connection()
    with service.store.transaction():
        conn.execute("DELETE FROM conversation_history")
        conn.execute("UPDATE session_archive_storage SET logical_bytes=? WHERE archive_id=?", (33 * 1024 * 1024, ids[0]))
    size = conn.execute("SELECT logical_bytes FROM session_archive_storage WHERE archive_id=?", (ids[1],)).fetchone()[0]
    prepared = plan(service)
    first = migration.repair_cleaning(service, prepared, max_batch_bytes=size)
    assert first["repaired"] == 1 and first["budget_deferred"] == [f"archive:{ids[2]}"]
    second = migration.repair_cleaning(service, prepared, max_batch_bytes=size)
    assert second["repaired"] == 1
    assert second["oversized_pending"][0]["source_key"] == f"archive:{ids[0]}"


@pytest.mark.parametrize("kwargs", [{"max_archive_bytes": 0}, {"max_archive_bytes": True},
    {"max_archive_bytes": 33 * 1024 * 1024}, {"max_batch_bytes": 65 * 1024 * 1024}])
def test_cleaning_budget_cannot_be_disabled_or_raised(service, kwargs):
    with pytest.raises(ValueError, match="invalid_cleaning_budget"):
        migration.repair_cleaning(service, plan(service), **kwargs)


@pytest.mark.parametrize("analysis_state", ["old_version", "invalid_text_hash", "missing"])
def test_completed_assigned_auto_with_stale_ownership_is_retried(service, monkeypatch, analysis_state):
    from evolvmem import ownership_reasoning
    task, _ = completed(service, monkeypatch)
    conn = service.store._connection()
    unit = dict(conn.execute("SELECT * FROM organization_units WHERE task_id=?", (task["id"],)).fetchone())
    analysis = {"version": ownership_reasoning.VERSION, "text_hash": ownership_reasoning.digest(unit["text"]),
                "rule_revision": service.knowledge().rules.read()["revision"]}
    if analysis_state == "old_version": analysis["version"] = "business-chain-v1"
    elif analysis_state == "invalid_text_hash": analysis["text_hash"] = "stale"
    else: analysis = {}
    conn.execute("UPDATE organization_units SET decision='auto',project='evo',projects='[\"evo\"]',ownership_analysis=? WHERE task_id=?",
                 (json.dumps(analysis), task["id"])); conn.commit()
    before = [dict(row) for row in conn.execute("SELECT * FROM organization_units")]
    prepared = plan(service)
    entry = next(row for row in prepared["sources"] if row["source_key"] == task["source_key"])
    assert entry["reason"] == "ownership_policy_changed"
    assert entry["action"] == "retry" and entry["unresolved_units"] == 0
    assert entry["outdated_ownership_units"] == 1
    assert migration.apply_plan(service, prepared)["applied"] == 1
    assert task_row(service, task["id"])["stage"] == "assignment"
    assert [dict(row) for row in conn.execute("SELECT * FROM organization_units")] == before


def test_completed_auto_with_current_ownership_is_not_retried(service, monkeypatch):
    from evolvmem import ownership_reasoning
    task, _ = completed(service, monkeypatch)
    conn = service.store._connection()
    unit = dict(conn.execute("SELECT * FROM organization_units WHERE task_id=?", (task["id"],)).fetchone())
    analysis = {"version": ownership_reasoning.VERSION, "text_hash": ownership_reasoning.digest(unit["text"]),
                "rule_revision": service.knowledge().rules.read()["revision"]}
    conn.execute("UPDATE organization_units SET decision='auto',project='evo',projects='[\"evo\"]',ownership_analysis=? WHERE task_id=?",
                 (json.dumps(analysis), task["id"])); conn.commit()
    prepared = plan(service)
    entry = next(row for row in prepared["sources"] if row["source_key"] == task["source_key"])
    assert entry["reason"] == "already_organized"
    assert entry["action"] is None and entry["outdated_ownership_units"] == 0
    assert not prepared["actions"]


def test_completed_manual_only_has_no_ownership_upgrade_or_queue_writes(service, monkeypatch):
    task, _ = completed(service, monkeypatch, manual=True)
    conn = service.store._connection()
    before = [dict(row) for row in conn.execute("SELECT * FROM organization_units")]
    prepared = plan(service)
    entry = next(row for row in prepared["sources"] if row["source_key"] == task["source_key"])
    assert entry["manual_units"] == 1
    assert entry["outdated_ownership_units"] == 0 and entry["action"] is None
    assert migration.apply_plan(service, prepared)["applied"] == 0
    assert [dict(row) for row in conn.execute("SELECT * FROM organization_units")] == before

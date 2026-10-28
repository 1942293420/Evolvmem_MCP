"""Resumable, non-destructive migration from Context items."""
from types import SimpleNamespace

from evolvmem.capability_store import CapabilityStore
from evolvmem.context_models import ContextContentType, ContextItemDraft, ContextLayers, ContextStatus
from evolvmem.context_store import ContextStore
from evolvmem.knowledge_restructure import (
    backup_database, dry_run, migrate_batch, verify_migration,
)
from evolvmem.project_archive_store import ProjectArchiveStore


def service(test_config, store):
    return SimpleNamespace(store=store, config=test_config)


def add_legacy_item(store, content_type, text, project="bluewhale", status=ContextStatus.ACTIVE):
    item = store.create_item(ContextItemDraft(
        identity_key=f"test:{content_type}:{text}",
        content_type=ContextContentType(content_type),
        layers=ContextLayers(l0=text[:80], l1=text, l2=text, generator="test"),
        project=project,
        status=status,
    ))
    return item.id


def provider(prompt):
    if prompt.startswith("CAPABILITY"):
        if "Vue" in prompt:
            return {"action":"keep", "category":"frontend", "title":"Vue表单校验",
                    "content":"提交前统一校验字段。", "keywords":["Vue"]}
        return {"action":"keep", "category":"mobile", "title":"移动端离线页面",
                "content":"离线时保留草稿。", "keywords":["移动端"]}
    return {"event_type":"feature", "title":"新增统一权限管理",
            "content":"蓝鲸完成统一权限管理。", "occurred_at":None}


def test_migration_is_resumable_and_preserves_sources(test_config):
    with ContextStore(test_config) as store:
        svc = service(test_config, store)
        capability = add_legacy_item(store, "fact", "Vue提交前统一校验")
        historical = add_legacy_item(store, "session_summary", "新增统一权限管理")

        first = migrate_batch(svc, batch_size=1, provider=provider)
        second = migrate_batch(svc, batch_size=100, provider=provider)
        third = migrate_batch(svc, batch_size=100, provider=provider)

        assert first["last_item_id"] == capability
        assert second["last_item_id"] == historical
        assert third["scanned"] == 0
        migrated = CapabilityStore(store).search("Vue")[0]
        assert migrated["source_project"] == "bluewhale"
        assert migrated["source_ref"] == f"context_item:{capability}"
        assert "新增统一权限管理" in ProjectArchiveStore(store).get("bluewhale")["summary"]
        assert store.get_item(capability, include_layers=False) is not None
        assert store.get_item(historical, include_layers=False) is not None
        assert verify_migration(svc)["ok"] is True


def test_unknown_category_is_pending_not_forced_into_taxonomy(test_config):
    with ContextStore(test_config) as store:
        svc = service(test_config, store)
        legacy_id = add_legacy_item(store, "fact", "移动端离线页面")

        result = migrate_batch(svc, provider=provider)

        assert result["pending"] == 1
        row = store._connection().execute("SELECT * FROM capability_intake").fetchone()
        assert row["suggested_category"] == "mobile"
        assert row["source_ref"] == f"context_item:{legacy_id}"
        assert CapabilityStore(store).search("离线") == []


def test_incomplete_model_candidate_is_pending_instead_of_aborting_batch(test_config):
    with ContextStore(test_config) as store:
        svc = service(test_config, store)
        legacy_id = add_legacy_item(store, "fact", "Vue提交前统一校验")

        result = migrate_batch(svc, provider=lambda _: {
            "action":"keep", "category":"frontend", "title":"", "content":"", "keywords":[]})

        assert result["pending"] == 1
        row = store._connection().execute("SELECT * FROM capability_intake").fetchone()
        assert row["content"] == "Vue提交前统一校验"
        assert row["source_ref"] == f"context_item:{legacy_id}"
        assert result["last_item_id"] == legacy_id


def test_skill_shaped_model_response_is_normalized_and_classified(test_config):
    with ContextStore(test_config) as store:
        svc = service(test_config, store)
        add_legacy_item(store, "fact", "Hermes配置编辑")

        result = migrate_batch(svc, provider=lambda _: {
            "name":"hermes-config-editing",
            "description":"编辑 Hermes config.yaml 时 API key 的脱敏规则。",
            "content":"使用 execute_code 拼接密钥并写入配置。",
            "metadata":{"tags":["config","secrets"]},
        })

        assert result["capabilities"] == 1
        memory = CapabilityStore(store).search("Hermes")[0]
        assert memory["category"] == "delivery"
        assert memory["title"] == "编辑 Hermes config.yaml 时 API key 的脱敏规则。"


def test_backup_and_dry_run_use_consistent_database_copy(test_config, tmp_path):
    with ContextStore(test_config) as store:
        add_legacy_item(store, "fact", "Vue提交前统一校验")
        target = tmp_path / "backup.db"
        backup_database(test_config, target)

        report = dry_run(target)

        assert report["capability_candidates"] == 1
        assert report["integrity_check"] == "ok"
        assert report["historical_candidates"] == 0


def test_cli_dry_run_prints_machine_readable_report(test_config, tmp_path, capsys):
    from evolvmem.knowledge_restructure_cli import main
    import json

    with ContextStore(test_config) as store:
        add_legacy_item(store, "fact", "Vue提交前统一校验")
        target = tmp_path / "backup.db"
        backup_database(test_config, target)

    assert main(["dry-run", "--copy", str(target)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["capability_candidates"] == 1


def test_cli_migrate_accepts_database_copy_path(test_config, tmp_path, monkeypatch):
    from evolvmem.knowledge_restructure_cli import main

    with ContextStore(test_config) as store:
        add_legacy_item(store, "fact", "Vue提交前统一校验")
        target = tmp_path / "backup.db"
        backup_database(test_config, target)
    monkeypatch.setattr(
        "evolvmem.knowledge_restructure_cli.migrate_batch",
        lambda service, batch_size: {
            "scanned": 0, "capabilities": 0, "events": 0,
            "pending": 0, "skipped": 0, "last_item_id": 1,
        },
    )

    assert main(["migrate", "--copy", str(target), "--batch", "1"]) == 0

"""Behavioral contracts for AES-GCM session archives and their purge lifecycle."""

from datetime import datetime, timedelta, timezone
import hashlib
import logging
from pathlib import Path
import stat

import pytest

import evolvmem.session_archive
from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayers,
    ContextScope,
    ContextStatus,
    ContextTier,
)
from evolvmem.context_store import ContextStore
from evolvmem.session_archive import SessionArchiver, SessionPurgeReport


T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(test_config):
    with ContextStore(test_config) as instance:
        yield instance


@pytest.fixture
def archiver(test_config, store):
    return SessionArchiver(test_config, store)


def _make_item(store, identity_key: str, *, project: str = "proj"):
    return store.create_item(
        ContextItemDraft(
            identity_key=identity_key,
            content_type=ContextContentType.EXPERIENCE,
            layers=ContextLayers(
                l0=f"Summary of {identity_key}",
                l1="Supporting detail for the session-derived experience.",
                l2="Full source material for the session-derived experience.",
                generator="test-suite",
            ),
            project=project,
            scope=ContextScope.PROJECT,
            status=ContextStatus.ACTIVE,
            tier=ContextTier.NORMAL,
            tags=("session",),
            importance=6.0,
            confidence=0.7,
        )
    )


def _link_source(store, item_id: int, archive_id: int) -> None:
    with store.transaction():
        store.record_session_source(
            item_id, archive_id, extraction_version="test-v1"
        )


def _archive_rows(store) -> list[dict]:
    rows = store._connection().execute(
        "SELECT * FROM session_archives ORDER BY id"
    ).fetchall()
    return [dict(row) for row in rows]


# ---- encryption round-trip and file hygiene ----


def test_archive_round_trip_restores_unicode_payload(archiver, store, test_config):
    payload = "用户说：MCP stdio 握手卡住时先检查 stdin 预读竞争。\nSecond line."

    record = archiver.archive_session("proj", "codex", "sess-1", payload, now=T0)

    assert record is not None
    assert record.project == "proj"
    assert record.adapter == "codex"
    assert record.external_session_id == "sess-1"
    assert record.state == "available"
    assert record.purged_at is None
    assert archiver.read_payload(record.id) == payload

    blob = (test_config.data_dir / record.payload_path).read_bytes()
    assert payload.encode("utf-8") not in blob
    assert record.payload_sha256 == hashlib.sha256(blob).hexdigest()


def test_archive_ttl_defaults_to_30_days_and_is_configurable(
        archiver, store, test_config):
    record = archiver.archive_session("proj", "codex", "sess-ttl", "正文内容", now=T0)
    assert record.expires_at == "2026-01-31 12:00:00"

    test_config.context_archive_ttl_days = 7
    record7 = archiver.archive_session("proj", "codex", "sess-ttl-7", "正文内容", now=T0)
    assert record7.expires_at == "2026-01-08 12:00:00"


def test_key_file_and_archive_directory_permissions(archiver, test_config):
    archiver.archive_session("proj", "codex", "sess-perm", "secret payload", now=T0)

    key_mode = stat.S_IMODE((test_config.data_dir / "archive.key").stat().st_mode)
    dir_mode = stat.S_IMODE(
        (test_config.data_dir / "session_archives").stat().st_mode
    )
    assert key_mode == 0o600
    assert dir_mode == 0o700


def test_missing_crypto_backend_fails_without_plaintext_fallback(
        archiver, store, test_config, monkeypatch, caplog):
    monkeypatch.setattr(evolvmem.session_archive, "AESGCM", None)
    payload = "绝不明文落盘的会话正文"

    with caplog.at_level(logging.WARNING):
        result = archiver.archive_session("proj", "codex", "sess-nocrypto", payload, now=T0)

    assert result is None
    payload_dir = test_config.data_dir / "session_archives"
    leftovers = list(payload_dir.iterdir()) if payload_dir.exists() else []
    assert leftovers == []
    assert _archive_rows(store) == []
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert rendered  # a content-free warning was recorded
    assert payload not in rendered
    assert str(test_config.data_dir) not in rendered


def test_missing_crypto_backend_read_returns_none(
        archiver, store, monkeypatch):
    record = archiver.archive_session("proj", "codex", "sess-read", "payload", now=T0)
    monkeypatch.setattr(evolvmem.session_archive, "AESGCM", None)

    assert archiver.read_payload(record.id) is None


# ---- idempotent upsert ----


def test_rearchiving_same_session_updates_payload_and_expiry(
        archiver, store, test_config):
    first = archiver.archive_session("proj", "codex", "sess-dupe", "old payload", now=T0)
    later = T0 + timedelta(days=10)
    second = archiver.archive_session(
        "proj", "codex", "sess-dupe", "new payload 更新后的正文", now=later
    )

    rows = _archive_rows(store)
    assert len(rows) == 1
    assert second.id == first.id
    assert second.expires_at == "2026-02-10 12:00:00"
    assert archiver.read_payload(first.id) == "new payload 更新后的正文"
    # The superseded ciphertext file is gone; exactly one payload remains.
    payloads = list((test_config.data_dir / "session_archives").iterdir())
    assert len(payloads) == 1
    assert second.payload_sha256 != first.payload_sha256


def test_db_failure_removes_new_payload_and_keeps_previous_archive(
        archiver, store, test_config, monkeypatch):
    first = archiver.archive_session("proj", "codex", "sess-tx", "old payload", now=T0)
    old_path = test_config.data_dir / first.payload_path
    old_blob = old_path.read_bytes()

    def failing_upsert(*args, **kwargs):
        raise RuntimeError("simulated DB outage")

    monkeypatch.setattr(store, "upsert_session_archive", failing_upsert)
    with pytest.raises(RuntimeError, match="simulated DB outage"):
        archiver.archive_session("proj", "codex", "sess-tx", "new payload", now=T0)

    rows = _archive_rows(store)
    assert len(rows) == 1
    assert rows[0]["payload_sha256"] == first.payload_sha256
    assert old_path.read_bytes() == old_blob
    payloads = list((test_config.data_dir / "session_archives").iterdir())
    assert len(payloads) == 1


# ---- TTL sweep ----


def test_sweep_purges_exactly_expired_archive_and_keeps_unexpired(
        archiver, store, test_config):
    record = archiver.archive_session("proj", "codex", "sess-exp", "expired payload", now=T0)

    boundary = T0 + timedelta(days=30)  # expires_at == now must purge
    report = archiver.sweep_expired(now=boundary - timedelta(seconds=1))
    assert report.purged_archive_ids == ()
    assert report.failed_archive_ids == ()
    assert {row["state"] for row in _archive_rows(store)} == {"available"}
    assert (test_config.data_dir / record.payload_path).exists()

    report = archiver.sweep_expired(now=boundary)
    assert report.purged_archive_ids == (record.id,)
    assert _archive_rows(store)[0]["state"] == "purged"
    assert not (test_config.data_dir / record.payload_path).exists()


def test_sweep_ttl_boundary_mixed_expiries(archiver, store, test_config):
    old = archiver.archive_session("proj", "codex", "sess-old", "old payload", now=T0)
    recent = archiver.archive_session(
        "proj", "codex", "sess-recent", "recent payload", now=T0 + timedelta(days=10)
    )

    boundary = T0 + timedelta(days=30)  # old expires exactly now; recent has 10 days left
    report = archiver.sweep_expired(now=boundary)

    assert report.purged_archive_ids == (old.id,)
    assert report.failed_archive_ids == ()
    rows = {row["id"]: row for row in _archive_rows(store)}
    assert rows[old.id]["state"] == "purged"
    assert rows[old.id]["purged_at"] == "2026-01-31 12:00:00"
    assert rows[recent.id]["state"] == "available"
    assert rows[recent.id]["purged_at"] is None
    assert not (test_config.data_dir / old.payload_path).exists()
    assert (test_config.data_dir / recent.payload_path).exists()
    assert archiver.read_payload(old.id) is None
    assert archiver.read_payload(recent.id) == "recent payload"


def test_sweep_payload_removal_failure_keeps_available_and_retries(
        archiver, store, test_config, monkeypatch):
    record = archiver.archive_session("proj", "codex", "sess-fail", "payload", now=T0)
    payload_file = test_config.data_dir / record.payload_path

    real_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self == payload_file:
            raise OSError("simulated EACCES")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    report = archiver.sweep_expired(now=T0 + timedelta(days=31))
    assert report.purged_archive_ids == ()
    assert report.failed_archive_ids == (record.id,)
    rows = _archive_rows(store)
    assert rows[0]["state"] == "available"  # never faked as purged
    assert rows[0]["purged_at"] is None
    assert payload_file.exists()

    monkeypatch.undo()
    report = archiver.sweep_expired(now=T0 + timedelta(days=31))
    assert report.purged_archive_ids == (record.id,)
    assert report.failed_archive_ids == ()
    assert _archive_rows(store)[0]["state"] == "purged"
    assert not payload_file.exists()


def test_sweep_missing_payload_file_is_truthfully_purged(
        archiver, store, test_config):
    record = archiver.archive_session("proj", "codex", "sess-gone", "payload", now=T0)
    (test_config.data_dir / record.payload_path).unlink()

    report = archiver.sweep_expired(now=T0 + timedelta(days=31))

    assert report.purged_archive_ids == (record.id,)
    assert _archive_rows(store)[0]["state"] == "purged"


def test_sweep_failure_of_one_archive_does_not_block_others(
        archiver, store, test_config, monkeypatch):
    bad = archiver.archive_session("proj", "codex", "sess-bad", "payload 1", now=T0)
    good = archiver.archive_session("proj", "codex", "sess-good", "payload 2", now=T0)
    bad_file = test_config.data_dir / bad.payload_path

    real_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self == bad_file:
            raise OSError("simulated EACCES")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    report = archiver.sweep_expired(now=T0 + timedelta(days=31))

    assert report.failed_archive_ids == (bad.id,)
    assert report.purged_archive_ids == (good.id,)
    rows = {row["id"]: row for row in _archive_rows(store)}
    assert rows[bad.id]["state"] == "available"
    assert rows[good.id]["state"] == "purged"


# ---- source_state recomputation ----


def test_source_state_partial_and_full_purge(archiver, store, test_config):
    arch_first = archiver.archive_session("proj", "codex", "sess-1", "payload 1", now=T0)
    arch_second = archiver.archive_session(
        "proj", "codex", "sess-2", "payload 2", now=T0 + timedelta(days=10)
    )

    item_all = _make_item(store, "proj:test:experience:all")
    item_mixed = _make_item(store, "proj:test:experience:mix")
    item_live = _make_item(store, "proj:test:experience:ok")
    item_none = _make_item(store, "proj:test:experience:none")

    _link_source(store, item_all.id, arch_first.id)
    _link_source(store, item_all.id, arch_second.id)
    _link_source(store, item_mixed.id, arch_first.id)
    _link_source(store, item_mixed.id, arch_second.id)
    _link_source(store, item_live.id, arch_second.id)

    assert store.get_item(item_all.id).source_state == "available"
    assert store.get_item(item_live.id).source_state == "available"
    assert store.get_item(item_none.id).source_state == "none"

    report = archiver.sweep_expired(now=T0 + timedelta(days=30))
    assert report.purged_archive_ids == (arch_first.id,)

    assert store.get_item(item_all.id).source_state == "partial_purged"
    assert store.get_item(item_mixed.id).source_state == "partial_purged"
    assert store.get_item(item_live.id).source_state == "available"
    assert store.get_item(item_none.id).source_state == "none"

    report = archiver.sweep_expired(now=T0 + timedelta(days=40))
    assert report.purged_archive_ids == (arch_second.id,)

    assert store.get_item(item_all.id).source_state == "purged"
    assert store.get_item(item_mixed.id).source_state == "purged"
    assert store.get_item(item_live.id).source_state == "purged"
    assert store.get_item(item_none.id).source_state == "none"


# ---- project purge ----


def test_purge_project_only_clears_target_project_and_keeps_items(
        archiver, store, test_config):
    target = archiver.archive_session("alpha", "codex", "sess-alpha", "alpha payload", now=T0)
    other = archiver.archive_session("beta", "codex", "sess-beta", "beta payload", now=T0)
    item = _make_item(store, "alpha:test:experience:kept", project="alpha")
    _link_source(store, item.id, target.id)

    report = archiver.purge_project("alpha")

    assert report.purged_archive_ids == (target.id,)
    assert report.failed_archive_ids == ()
    rows = {row["id"]: row for row in _archive_rows(store)}
    assert rows[target.id]["state"] == "purged"
    assert rows[target.id]["purged_at"] is not None
    assert rows[other.id]["state"] == "available"
    assert not (test_config.data_dir / target.payload_path).exists()
    assert (test_config.data_dir / other.payload_path).exists()

    # ContextItems are never deleted by a project purge.
    kept = store.get_item(item.id)
    assert kept is not None
    assert kept.status == ContextStatus.ACTIVE
    assert kept.source_state == "purged"
    assert archiver.read_payload(other.id) == "beta payload"


def test_purge_project_is_idempotent_and_skips_already_purged(
        archiver, store, test_config):
    archiver.archive_session("alpha", "codex", "sess-a1", "payload", now=T0)

    first = archiver.purge_project("alpha")
    second = archiver.purge_project("alpha")

    assert len(first.purged_archive_ids) == 1
    assert second.purged_archive_ids == ()
    assert second.failed_archive_ids == ()


# ---- reporting hygiene ----


def test_purge_report_and_logs_carry_no_payload_or_absolute_paths(
        archiver, store, test_config, caplog, monkeypatch):
    payload = "敏感正文：生产库密码是 hunter2"
    doomed = archiver.archive_session("proj", "codex", "sess-log-bad", payload, now=T0)
    archiver.archive_session("proj", "codex", "sess-log-good", "另一个正文", now=T0)
    bad_file = test_config.data_dir / doomed.payload_path

    real_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self == bad_file:
            raise OSError("simulated EACCES with /abs/path and secret inside")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    with caplog.at_level(logging.WARNING):
        report = archiver.sweep_expired(now=T0 + timedelta(days=31))

    assert isinstance(report, SessionPurgeReport)
    rendered_report = repr(report)
    assert payload not in rendered_report
    assert str(test_config.data_dir) not in rendered_report
    for record in caplog.records:
        message = record.getMessage()
        assert payload not in message
        assert str(test_config.data_dir) not in message
        assert "hunter2" not in message
        assert "/abs/path" not in message

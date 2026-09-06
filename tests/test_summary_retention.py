"""Behavioral contracts for coverage-gated session summary retention (Task 6).

Pinned here: an expired SESSION_SUMMARY without rollup coverage is never
archived (its project is reported pending and a ``pending`` rollup row is
created only when none exists); covered summaries beyond the per-project
keep count are archived in one transaction with their
``session_archive_holds`` released; the archiver's purge skips any archive
with a hold until the hold is released; and a persisted extraction batch
carrying a ``source_archive_id`` records a ``rollup_pending`` hold for its
summary in the same transaction. Reports and logs carry ids and stable
reason codes only — never content or paths.
"""

from datetime import datetime, timedelta, timezone
import json
import logging

import pytest

from evolvmem.config import Config
from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayers,
    ContextMode,
    ContextScope,
    ContextStatus,
)
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.legacy_models import LegacyExtractionItem, LegacyExtractionRequest
from evolvmem.memory_store import MemoryStore
from evolvmem.project_rollup import ProjectRollupGenerator
from evolvmem.project_store import ProjectStore
from evolvmem.session_archive import SessionArchiver
from evolvmem.summary_retention import (
    SummaryRetention,
    SummaryRetentionReport,
    insert_rollup_pending_hold,
)


T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
NOW = "2026-02-01 00:00:00"
EXPIRED = "2026-01-01 00:00:00"


@pytest.fixture
def store(test_config):
    with ContextStore(test_config) as instance:
        yield instance


@pytest.fixture
def dual_store(test_config):
    with MemoryStore(test_config):
        pass  # legacy projection schema, mirroring a pre-cutover database
    with ContextStore(test_config) as instance:
        yield instance


@pytest.fixture
def service(test_config, dual_store):
    instance = ContextService(test_config, store=dual_store)
    instance.initialize(mode=ContextMode.SHADOW, adapter="kimi")
    yield instance
    instance.close()


def _make_summary(store, project: str, tag: str, *, expires_at: str | None = None) -> int:
    item = store.create_item(
        ContextItemDraft(
            identity_key=f"project:{project}:progress:log:{tag}",
            content_type=ContextContentType.SESSION_SUMMARY,
            layers=ContextLayers(
                l0=f"会话摘要 {tag} 要点。",
                l1=f"细节：{tag} 的进展与决定。",
                l2=f"完整正文：{tag} 的症状、假设、修改与验证。",
                generator="test-suite",
            ),
            project=project,
            scope=ContextScope.PROJECT,
            status=ContextStatus.ACTIVE,
            expires_at=expires_at,
        )
    )
    return item.id


def _ok_response() -> str:
    return json.dumps(
        {
            "l0": "项目当前聚焦滚动摘要链路收敛。",
            "l1": "进展：已完成项目归属接线；待办：滚动摘要按来源集滚动更新。",
            "l2": "完整细节：来源为项目会话摘要；旧摘要携带历史脉络；验证为来源闭包一致。",
        },
        ensure_ascii=False,
    )


def _rollup_ready(config, store, project: str) -> None:
    """Run a real rollup so the ready row's source closure covers the summaries."""
    report = ProjectRollupGenerator(
        config, store, llm=lambda _prompt: _ok_response()
    ).rollup_project(project)
    assert report.status == "ready"


def _rollup_row(store, project: str):
    return store._connection().execute(
        "SELECT project, current_context_id, status, revision"
        " FROM context_project_rollups WHERE project=?",
        (project,),
    ).fetchone()


def _insert_hold(store, archive_id: int, context_id: int) -> None:
    with store.transaction():
        insert_rollup_pending_hold(store, archive_id, context_id)


def _hold_rows(store) -> list[dict]:
    rows = store._connection().execute(
        "SELECT archive_id, source_context_id, reason FROM session_archive_holds"
        " ORDER BY archive_id, source_context_id"
    ).fetchall()
    return [dict(row) for row in rows]


def _register(store, *projects: str) -> None:
    ps = ProjectStore(store._connection(), store._require_transaction, generic_names=())
    with store.transaction():
        for project in projects:
            ps.register_project(project)


def _extraction_request(project: str, tag: str) -> LegacyExtractionRequest:
    return LegacyExtractionRequest(
        summary=LegacyExtractionItem(
            key=f"project:{project}:progress:log:{tag}",
            value="本次完成覆盖门控归档接线并补齐回归验证。",
            attribute="fact",
            tags=("日志", f"分类:{project}"),
            confidence=1.0,
        ),
        candidates=(),
        max_writes=8,
        source_session=f"session-{tag}",
    )


# ---- the brief's pinned tests ----


def test_expired_uncovered_summary_is_not_archived(test_config, store):
    expired_id = _make_summary(store, "eva", "t1", expires_at=EXPIRED)
    fresh_id = _make_summary(store, "eva", "t2")  # no expiry: never held

    report = SummaryRetention(test_config, store).sweep(NOW)

    assert report.archived_ids == ()
    assert report.held_ids == (expired_id,)
    assert report.pending_projects == ("eva",)
    assert store.get_item(expired_id).status is ContextStatus.ACTIVE
    assert store.get_item(fresh_id).status is ContextStatus.ACTIVE
    row = _rollup_row(store, "eva")
    assert row is not None
    assert row["status"] == "pending"
    assert row["current_context_id"] is None


def test_covered_summary_beyond_keep_is_archived_and_hold_released(
        test_config, store):
    test_config.context_session_summary_keep = 2
    ids = [_make_summary(store, "eva", tag) for tag in ("t1", "t2", "t3")]
    _rollup_ready(test_config, store, "eva")  # ready closure covers all three
    record = SessionArchiver(test_config, store).archive_session(
        "eva", "kimi", "sess-eva", "原始会话正文", now=T0
    )
    for item_id in ids:
        _insert_hold(store, record.id, item_id)

    report = SummaryRetention(test_config, store).sweep(NOW)

    assert report.archived_ids == (ids[0],)  # the oldest beyond keep
    assert report.held_ids == ()
    assert report.pending_projects == ()
    assert store.get_item(ids[0]).status is ContextStatus.ARCHIVED
    assert store.get_item(ids[1]).status is ContextStatus.ACTIVE
    assert store.get_item(ids[2]).status is ContextStatus.ACTIVE
    holds = _hold_rows(store)
    assert [
        (row["archive_id"], row["source_context_id"]) for row in holds
    ] == [(record.id, ids[1]), (record.id, ids[2])]


def test_archiver_purge_skips_held_archive(test_config, store):
    archiver = SessionArchiver(test_config, store)
    record = archiver.archive_session(
        "eva", "kimi", "sess-held", "原始会话正文", now=T0
    )
    item_id = _make_summary(store, "eva", "t1")
    _insert_hold(store, record.id, item_id)

    later = T0 + timedelta(days=31)
    first = archiver.sweep_expired(now=later)
    assert first.purged_archive_ids == ()
    assert first.failed_archive_ids == ()
    assert first.held_archive_ids == (record.id,)
    assert store.get_session_archive(record.id)["state"] == "available"
    assert (test_config.data_dir / record.payload_path).exists()

    with store.transaction():
        store._connection().execute(
            "DELETE FROM session_archive_holds WHERE archive_id=?", (record.id,)
        )
    second = archiver.sweep_expired(now=later)
    assert second.purged_archive_ids == (record.id,)
    assert second.held_archive_ids == ()
    assert store.get_session_archive(record.id)["state"] == "purged"
    assert not (test_config.data_dir / record.payload_path).exists()


# ---- retention gate edge semantics ----


def test_pending_marker_preserves_existing_ready_row(test_config, store):
    _make_summary(store, "eva", "base")
    _rollup_ready(test_config, store, "eva")
    ready_revision = _rollup_row(store, "eva")["revision"]
    expired_id = _make_summary(store, "eva", "t2", expires_at=EXPIRED)

    report = SummaryRetention(test_config, store).sweep(NOW)

    assert report.held_ids == (expired_id,)
    assert report.pending_projects == ("eva",)
    row = _rollup_row(store, "eva")
    assert row["status"] == "ready"  # 已有行不降级
    assert row["revision"] == ready_revision
    assert row["current_context_id"] is not None


def test_project_free_active_summary_is_out_of_scope(test_config, store):
    """project='' 的活跃摘要不参与扫描：空项目没有覆盖闭包，永不归档/挂起。"""
    free_id = _make_summary(store, "", "t1", expires_at=EXPIRED)
    expired_id = _make_summary(store, "eva", "t2", expires_at=EXPIRED)

    report = SummaryRetention(test_config, store).sweep(NOW)

    assert report.archived_ids == ()
    assert report.held_ids == (expired_id,)  # the real project still processed
    assert report.pending_projects == ("eva",)
    assert store.get_item(free_id).status is ContextStatus.ACTIVE
    assert store.get_item(expired_id).status is ContextStatus.ACTIVE
    assert _rollup_row(store, "") is None  # no pending marker for ''


def test_expired_covered_summary_within_keep_stays_active(test_config, store):
    summary_id = _make_summary(store, "eva", "t1", expires_at=EXPIRED)
    _rollup_ready(test_config, store, "eva")

    report = SummaryRetention(test_config, store).sweep(NOW)

    assert report == SummaryRetentionReport()
    assert store.get_item(summary_id).status is ContextStatus.ACTIVE
    assert _rollup_row(store, "eva")["status"] == "ready"


def test_purge_project_also_skips_held_archive(test_config, store):
    archiver = SessionArchiver(test_config, store)
    record = archiver.archive_session(
        "eva", "kimi", "sess-project", "原始会话正文", now=T0
    )
    _insert_hold(store, record.id, _make_summary(store, "eva", "t1"))

    report = archiver.purge_project("eva")

    assert report.purged_archive_ids == ()
    assert report.held_archive_ids == (record.id,)
    assert store.get_session_archive(record.id)["state"] == "available"


def test_retention_rejects_invalid_arguments(test_config, store):
    from evolvmem.context_models import ContextValidationError

    with pytest.raises(ContextValidationError):
        SummaryRetention(None, store)  # type: ignore[arg-type]
    with pytest.raises(ContextValidationError):
        SummaryRetention(test_config, None)  # type: ignore[arg-type]
    with pytest.raises(ContextValidationError):
        SummaryRetention(test_config, store).sweep("")


# ---- hold lifecycle at the extraction write path ----


def test_persisted_summary_with_archive_creates_rollup_pending_hold(
        test_config, dual_store, service):
    record = SessionArchiver(test_config, dual_store).archive_session(
        "eva", "kimi", "sess-hold", "原始会话正文", now=T0
    )
    _register(dual_store, "eva")

    result = service.persist_legacy_extraction(
        _extraction_request("eva", "t1"), source_archive_id=record.id
    )

    assert result.persisted == 1
    assert result.summary is not None and result.summary.context_id is not None
    holds = _hold_rows(dual_store)
    assert [
        (row["archive_id"], row["source_context_id"], row["reason"])
        for row in holds
    ] == [(record.id, result.summary.context_id, "rollup_pending")]


def test_persisted_summary_without_archive_creates_no_hold(
        test_config, dual_store, service):
    _register(dual_store, "eva")

    result = service.persist_legacy_extraction(_extraction_request("eva", "t1"))

    assert result.persisted == 1
    assert _hold_rows(dual_store) == []


def test_repeated_persist_of_same_archive_keeps_single_hold(
        test_config, dual_store, service):
    record = SessionArchiver(test_config, dual_store).archive_session(
        "eva", "kimi", "sess-hold-twice", "原始会话正文", now=T0
    )
    _register(dual_store, "eva")

    request = _extraction_request("eva", "t1")
    first = service.persist_legacy_extraction(request, source_archive_id=record.id)
    second = service.persist_legacy_extraction(request, source_archive_id=record.id)

    assert first.persisted == 1
    assert second.persisted == 0  # equivalent summary accepted, nothing rewritten
    assert len(_hold_rows(dual_store)) == 1


# ---- service wiring: the archive sweep carries the retention pass ----


def test_service_sweep_archives_runs_summary_retention(test_config, dual_store):
    test_config.context_session_summary_keep = 1
    ids = [_make_summary(dual_store, "eva", tag) for tag in ("t1", "t2")]
    _rollup_ready(test_config, dual_store, "eva")
    service = ContextService(test_config, store=dual_store)
    service.initialize(mode=ContextMode.SHADOW, adapter="kimi")
    try:
        report = service.sweep_archives()
        assert report.purged_archive_ids == ()
        assert dual_store.get_item(ids[0]).status is ContextStatus.ARCHIVED
        assert dual_store.get_item(ids[1]).status is ContextStatus.ACTIVE
    finally:
        service.close()


def test_service_sweep_archives_retention_failure_is_fail_open(
        test_config, dual_store, monkeypatch, caplog):
    def _explode(self, now):
        raise RuntimeError("retention exploded at /home/alice/secret.db")

    monkeypatch.setattr(
        "evolvmem.context_service.SummaryRetention.sweep", _explode
    )
    service = ContextService(test_config, store=dual_store)
    service.initialize(mode=ContextMode.SHADOW, adapter="kimi")
    try:
        with caplog.at_level(logging.WARNING, logger="evolvmem.context_service"):
            report = service.sweep_archives()
    finally:
        service.close()

    assert report.purged_archive_ids == ()
    assert report.failed_archive_ids == ()
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert rendered  # a content-free warning was recorded
    assert "secret" not in rendered
    assert "exploded" not in rendered


# ---- config contract ----


def test_summary_retention_config_defaults_and_roundtrip(test_config):
    assert test_config.context_session_summary_ttl_days == 30
    assert test_config.context_session_summary_keep == 10
    assert test_config.validate_runtime() == ()

    test_config.context_session_summary_ttl_days = 45
    test_config.context_session_summary_keep = 4
    test_config.save()
    loaded = Config.from_file(test_config.config_path)
    assert loaded.context_session_summary_ttl_days == 45
    assert loaded.context_session_summary_keep == 4


@pytest.mark.parametrize(
    "field_name",
    ["context_session_summary_ttl_days", "context_session_summary_keep"],
)
@pytest.mark.parametrize("bad", [0, -1, True])
def test_summary_retention_settings_are_validated(test_config, field_name, bad):
    setattr(test_config, field_name, bad)

    diagnostics = test_config.validate_runtime()

    assert any(field_name in message for message in diagnostics)

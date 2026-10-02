"""First-round EvolvMem trust improvements: project ownership + decision time.

These tests run the real store/service/recall boundaries with synthetic
fixtures only. They deliberately never mock the answer: every assertion is
made against ContextStore rows, ContextService results, or the recall
renderers.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from evolvmem.config import Config
from evolvmem.context_models import (
    ContextContentType,
    ContextItemDraft,
    ContextLayers,
    ContextScope,
    ContextSearchRequest,
    ContextSessionStartRequest,
    ContextStatus,
    ContextTier,
    ContextValidationError,
)
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.project_models import ProjectResolutionDecision
from evolvmem.project_progress_recall import read_recent_project_progress
from evolvmem.project_store import ProjectStore

FP = "hmac-sha256:" + "a" * 64
NOW = "2026-10-02 00:00:00"


# ---------------------------------------------------------------------------
# synthetic environment helpers
# ---------------------------------------------------------------------------


class Env:
    def __init__(self, data_dir: Path) -> None:
        self.config = Config(data_dir=data_dir)
        self.store = ContextStore(self.config)
        self.store.initialize()
        self.service = ContextService(self.config, store=self.store)
        from evolvmem.context_models import ContextMode

        self.service.initialize(mode=ContextMode.SHADOW, adapter="codex")
        self.projects = ProjectStore(
            self.store._connection(),
            self.store._require_transaction,
            generic_names=("home", "src"),
        )

    def close(self) -> None:
        self.service.close(close_embedding_engine=False)
        self.store.close()


@pytest.fixture
def env(temp_dir):
    instance = Env(temp_dir)
    yield instance
    instance.close()


def register(env, project: str, *aliases: str) -> None:
    with env.store.transaction():
        env.projects.register_project(project)
        for alias in aliases:
            env.projects.add_alias(alias, project)


def item(
    env,
    identity_key: str,
    *,
    project: str,
    l0: str,
    l1: str | None = None,
    content_type: ContextContentType = ContextContentType.FACT,
    status: ContextStatus = ContextStatus.ACTIVE,
    confidence: float = 0.9,
    importance: float = 5.0,
    **temporal,
):
    draft = ContextItemDraft(
        identity_key=identity_key,
        content_type=content_type,
        layers=ContextLayers(
            l0=l0,
            l1=l1 or f"detail for {identity_key}",
            l2=f"source for {identity_key}",
            generator="trust-test",
        ),
        project=project,
        scope=ContextScope.PROJECT,
        status=status,
        tier=ContextTier.NORMAL,
        importance=importance,
        confidence=confidence,
        **temporal,
    )
    return env.store.create_item(draft)


def resolution(env, item_id: int, *, state: str, review: str) -> int:
    """Write one resolution row and return its revision."""
    with env.store.transaction():
        if state == "conflict":
            env.projects.record_resolution(
                item_id, ProjectResolutionDecision.conflict("v1", ())
            )
        elif state == "unresolved":
            env.projects.record_resolution(
                item_id, ProjectResolutionDecision.unresolved("v1", ())
            )
        else:
            decision = ProjectResolutionDecision.resolved(
                "evolvmem", "strong", "v1", ()
            )
            env.projects.record_resolution(item_id, decision)
        if review == "accepted":
            env.projects.accept_resolution(
                item_id, "evolvmem", expected_revision=1
            )
        elif review == "rejected":
            env.projects.reject_resolution(item_id, expected_revision=1)
    row = env.store._connection().execute(
        "SELECT revision FROM context_project_resolutions WHERE item_id=?",
        (item_id,),
    ).fetchone()
    return int(row["revision"])


def bind_workstream(env, project: str, context_id: int, workstream_id: str) -> None:
    with env.store.transaction():
        env.store._connection().execute(
            "INSERT INTO continuity_workstreams (id, project,"
            " workspace_fingerprint, current_context_id, checkpoint_revision,"
            " state_version, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (workstream_id, project, FP, context_id, 1, 1, "open", NOW, NOW),
        )


# ---------------------------------------------------------------------------
# 1. project ownership trust
# ---------------------------------------------------------------------------


def test_progress_recall_holds_untrusted_and_unreviewed_ownership(env):
    register(env, "evolvmem", "EvolvMem")
    confirmed = item(
        env, "project:evolvmem:checkpoint:confirmed", project="evolvmem",
        l0="evolvmem 确认归属的进展", l1="目标: 修好召回\n已完成: 第一步",
        content_type=ContextContentType.WORKSTREAM_CHECKPOINT,
    )
    pending = item(
        env, "project:evolvmem:checkpoint:pending", project="evolvmem",
        l0="evolvmem 待审归属的进展", l1="目标: 待审\n已完成: 无",
        content_type=ContextContentType.WORKSTREAM_CHECKPOINT,
    )
    unreviewed = item(
        env, "project:evolvmem:checkpoint:no-resolution", project="evolvmem",
        l0="evolvmem 无归属记录的进展", l1="目标: 无归属记录\n已完成: 无",
        content_type=ContextContentType.WORKSTREAM_CHECKPOINT,
    )
    resolution(env, confirmed.id, state="resolved", review="accepted")
    resolution(env, pending.id, state="unresolved", review="pending")
    bind_workstream(env, "evolvmem", confirmed.id, "ws-confirmed")
    bind_workstream(env, "evolvmem", pending.id, "ws-pending")
    bind_workstream(env, "evolvmem", unreviewed.id, "ws-unresolved")

    result = read_recent_project_progress(env.service, "evolvmem", max_chars=6000)

    assert confirmed.id in result.selected_ids
    # Uncertain ownership (pending review, or no resolution row at all) is held
    # out of the default progress surface instead of being injected.
    assert pending.id not in result.selected_ids
    assert unreviewed.id not in result.selected_ids
    assert f"id={confirmed.id}" in result.text
    assert "ownership=confirmed" in result.text
    assert f"id={pending.id}" not in result.text
    assert f"id={unreviewed.id}" not in result.text
    excluded = {row["id"]: row["reason"] for row in result.diagnostics["excluded"]}
    assert excluded[pending.id] == "project_ownership_pending"
    assert excluded[unreviewed.id] == "project_ownership_unverified"


def test_session_injection_holds_pending_and_unreviewed_ownership(env):
    register(env, "evolvmem", "EvolvMem")
    confirmed = item(
        env, "project:evolvmem:fact:confirmed", project="evolvmem",
        l0="zebra 已确认归属", importance=8.0,
    )
    pending = item(
        env, "project:evolvmem:fact:pending", project="evolvmem",
        l0="zebra 待审归属", importance=8.0,
    )
    unreviewed = item(
        env, "project:evolvmem:fact:self", project="evolvmem",
        l0="zebra 自述归属无审核记录", importance=8.0,
    )
    resolution(env, confirmed.id, state="resolved", review="accepted")
    resolution(env, pending.id, state="conflict", review="pending")

    result = env.service.session_start(
        ContextSessionStartRequest(project="evolvmem", query="zebra"),
        project_only=True,
    )

    assert confirmed.id in result.selected_ids
    assert pending.id not in result.selected_ids
    assert unreviewed.id not in result.selected_ids
    reasons = {entry.reason: entry.count for entry in result.excluded_counts}
    assert reasons.get("project_ownership_conflict") == 1
    assert reasons.get("project_ownership_unverified") == 1


def test_mention_recall_reports_bounded_diagnostics(env):
    from evolvmem.project_mention_recall import recall_mentioned_projects

    register(env, "evolvmem", "EvolvMem")
    keep = item(
        env, "project:evolvmem:fact:keep", project="evolvmem",
        l0="evolvmem 诊断 保留项", importance=8.0,
    )
    drop = item(
        env, "project:evolvmem:fact:drop", project="evolvmem",
        l0="evolvmem 诊断 被拒项", importance=8.0,
    )
    resolution(env, keep.id, state="resolved", review="accepted")
    resolution(env, drop.id, state="resolved", review="rejected")

    result = recall_mentioned_projects(env.service, query="evolvmem 诊断")
    diagnostics = result.diagnostics

    assert diagnostics["matched_projects"] == ["evolvmem"]
    assert diagnostics["query_surfaces"] == [
        {"project": "evolvmem", "surface": "evolvmem"}
    ]
    assert diagnostics["selected_ids"]
    assert keep.id in result.selected_ids
    assert drop.id not in result.selected_ids
    excluded = {row["id"]: row["reason"] for row in diagnostics["excluded"]}
    assert excluded[drop.id] == "project_ownership_rejected"
    blob = json.dumps(diagnostics, ensure_ascii=False, sort_keys=True)
    assert "evolvmem 诊断" not in blob  # raw query text never echoed
    assert "保留项" not in blob  # L0 text never echoed
    assert "/" not in blob and "\\" not in blob  # no paths


def test_ambiguous_alias_surface_is_never_guessed(env):
    from evolvmem.project_mention_recall import recall_mentioned_projects

    register(env, "AI 采购")
    register(env, "AI采购")
    item(
        env, "project:ai:fact:one", project="AI采购",
        l0="AI采购 待定", importance=8.0,
    )

    result = recall_mentioned_projects(env.service, query="请继续 AI采购 的进度")

    assert result.matched_projects == ()
    assert result.block == ""
    assert result.diagnostics["ambiguous_surfaces"] == ["ai采购"]


def test_review_api_can_correct_a_workstream_row_without_resolution(env):
    from evolvmem.web_server import api_resolution_accept, api_resolutions

    class _NoLegacy:
        def get_by_ids(self, ids):
            return []

    facade = _NoLegacy()
    register(env, "evolvmem", "EvolvMem")
    register(env, "inventory")
    row = item(
        env, "project:inventory:checkpoint:misassigned", project="inventory",
        l0="inventory 断点", content_type=ContextContentType.WORKSTREAM_CHECKPOINT,
    )

    unreviewed = api_resolutions(facade, env.store, {"state": "unreviewed"})
    assert [entry["item_id"] for entry in unreviewed] == [row.id]
    assert unreviewed[0]["revision"] == 0

    # An absent row is created under exact CAS: revision 0 means "no row yet".
    accepted = api_resolution_accept(
        env.service, row.id, {"project": "evolvmem", "expected_revision": 1}
    )
    assert accepted == {"ok": False, "error": "resolution_not_found"}
    accepted = api_resolution_accept(
        env.service, row.id, {"project": "evolvmem", "expected_revision": 0}
    )
    assert accepted["ok"] is True
    stored = env.store.get_item(row.id, include_layers=False)
    assert stored.project == "evolvmem"
    review_rows = api_resolutions(facade, env.store, {"state": "accepted"})
    assert any(entry["item_id"] == row.id for entry in review_rows)


# ---------------------------------------------------------------------------
# 2. temporal validity
# ---------------------------------------------------------------------------


def test_draft_normalizes_iso_timestamps_to_utc_and_keeps_unknown(env):
    entry = item(
        env, "project:evolvmem:decision:tz", project="evolvmem",
        l0="决策时区归一", content_type=ContextContentType.DECISION,
        effective_from="2026-03-01T08:00:00+08:00",
        mentioned_at="2026-02-28T23:30:00Z",
    )
    assert entry.effective_from == "2026-03-01 00:00:00"
    assert entry.mentioned_at == "2026-02-28 23:30:00"
    assert entry.effective_until is None
    assert entry.occurred_at is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("effective_from", "not-a-date"),
        ("effective_until", "2026-13-01"),
        ("occurred_at", "2026-09-31T00:00:00"),
    ],
)
def test_invalid_iso_timestamps_are_rejected(env, field, value):
    with pytest.raises(ContextValidationError):
        item(
            env, f"project:evolvmem:decision:bad-{field}", project="evolvmem",
            l0="坏时间戳", content_type=ContextContentType.DECISION,
            **{field: value},
        )


def test_default_recall_excludes_future_and_expired_windows(env):
    register(env, "evolvmem")
    expired = item(
        env, "project:evolvmem:decision:expired", project="evolvmem",
        l0="zebra 过期决策", content_type=ContextContentType.DECISION,
        effective_from="2026-01-01 00:00:00", effective_until="2026-05-01 00:00:00",
        importance=8.0,
    )
    future = item(
        env, "project:evolvmem:decision:future", project="evolvmem",
        l0="zebra 未来决策", content_type=ContextContentType.DECISION,
        effective_from="2027-01-01 00:00:00", importance=8.0,
    )
    current = item(
        env, "project:evolvmem:decision:current", project="evolvmem",
        l0="zebra 当前决策", content_type=ContextContentType.DECISION,
        effective_from="2026-09-01 00:00:00", importance=8.0,
    )

    results = env.service.search(
        ContextSearchRequest(query="zebra", project="evolvmem", top_k=10)
    )
    ids = {result.id for result in results}
    assert current.id in ids
    assert expired.id not in ids
    assert future.id not in ids


def test_effective_until_is_exclusive(env):
    register(env, "evolvmem")
    boundary = item(
        env, "project:evolvmem:decision:boundary", project="evolvmem",
        l0="zebra 边界决策", content_type=ContextContentType.DECISION,
        effective_from="2026-01-01 00:00:00", effective_until="2026-10-02 00:00:00",
    )
    results = env.service.search(
        ContextSearchRequest(
            query="zebra", project="evolvmem", top_k=10,
            as_of="2026-10-02 00:00:00",
        )
    )
    assert boundary.id not in {result.id for result in results}
    inside = env.service.search(
        ContextSearchRequest(
            query="zebra", project="evolvmem", top_k=10,
            as_of="2026-10-01 23:59:59",
        )
    )
    assert boundary.id in {result.id for result in inside}


def test_as_of_reads_superseded_decision_at_event_time(env):
    register(env, "evolvmem")
    old = item(
        env, "project:evolvmem:decision:supplier", project="evolvmem",
        l0="zebra 供应商旧决策", content_type=ContextContentType.DECISION,
        effective_from="2026-01-01 00:00:00", importance=8.0,
    )
    new_draft = ContextItemDraft(
        identity_key="project:evolvmem:decision:supplier",
        content_type=ContextContentType.DECISION,
        layers=ContextLayers(
            l0="zebra 供应商新决策", l1="detail", l2="source",
            generator="trust-test",
        ),
        project="evolvmem",
        scope=ContextScope.PROJECT,
        status=ContextStatus.ACTIVE,
        importance=8.0,
        confidence=0.9,
        effective_from="2026-06-01 00:00:00",
    )
    new = env.store.supersede_active(new_draft)
    assert env.store.get_item(old.id, include_layers=False).status is (
        ContextStatus.SUPERSEDED
    )

    current = env.service.search(
        ContextSearchRequest(query="zebra 供应商", project="evolvmem", top_k=10)
    )
    assert new.id in {result.id for result in current}
    assert old.id not in {result.id for result in current}

    historical = env.service.search(
        ContextSearchRequest(
            query="zebra 供应商", project="evolvmem", top_k=10,
            as_of="2026-03-01 00:00:00",
        )
    )
    ids = {result.id for result in historical}
    assert old.id in ids
    assert new.id not in ids


def test_candidate_deleted_and_low_confidence_stay_gated_at_as_of(env):
    register(env, "evolvmem")
    candidate = item(
        env, "project:evolvmem:decision:candidate", project="evolvmem",
        l0="zebra 候选决策", content_type=ContextContentType.DECISION,
        status=ContextStatus.CANDIDATE, effective_from="2026-01-01 00:00:00",
    )
    removed = item(
        env, "project:evolvmem:decision:deleted", project="evolvmem",
        l0="zebra 已删决策", content_type=ContextContentType.DECISION,
        status=ContextStatus.DELETED, effective_from="2026-01-01 00:00:00",
    )
    weak = item(
        env, "project:evolvmem:decision:weak", project="evolvmem",
        l0="zebra 低置信决策", content_type=ContextContentType.DECISION,
        confidence=0.1, effective_from="2026-01-01 00:00:00",
    )
    results = env.service.search(
        ContextSearchRequest(
            query="zebra", project="evolvmem", top_k=10,
            as_of="2026-03-01 00:00:00",
        )
    )
    ids = {result.id for result in results}
    assert not {candidate.id, removed.id, weak.id} & ids


def test_late_backfill_does_not_outrank_later_effective_decision(env):
    register(env, "evolvmem")
    later = item(
        env, "project:evolvmem:decision:supplier", project="evolvmem",
        l0="zebra 供应商 六月生效决策", content_type=ContextContentType.DECISION,
        effective_from="2026-06-01 00:00:00", importance=8.0,
    )
    backfilled = ContextItemDraft(
        identity_key="project:evolvmem:decision:supplier",
        content_type=ContextContentType.DECISION,
        layers=ContextLayers(
            l0="zebra 供应商 三月补录事件", l1="detail", l2="source",
            generator="trust-test",
        ),
        project="evolvmem",
        scope=ContextScope.PROJECT,
        status=ContextStatus.ACTIVE,
        importance=8.0,
        confidence=0.9,
        effective_from="2026-03-01 00:00:00",
        occurred_at="2026-03-01 00:00:00",
        mentioned_at="2026-10-01 00:00:00",
    )
    stored = env.store.supersede_active(backfilled)

    assert env.store.get_item(later.id, include_layers=False).status is (
        ContextStatus.ACTIVE
    )
    assert stored.status is ContextStatus.SUPERSEDED
    assert stored.superseded_by == later.id
    current = env.service.search(
        ContextSearchRequest(query="zebra 供应商", project="evolvmem", top_k=10)
    )
    assert later.id in {result.id for result in current}
    assert stored.id not in {result.id for result in current}
    historical = env.service.search(
        ContextSearchRequest(
            query="zebra 供应商", project="evolvmem", top_k=10,
            as_of="2026-04-01 00:00:00",
        )
    )
    assert stored.id in {result.id for result in historical}


def test_decision_window_write_and_read_through_service(env):
    register(env, "evolvmem")
    entry = item(
        env, "project:evolvmem:decision:api", project="evolvmem",
        l0="zebra API 决策", content_type=ContextContentType.DECISION,
    )
    written = env.service.set_decision_window(
        entry.id,
        effective_from="2026-03-01T08:00:00+08:00",
        effective_until="2026-12-31",
        mentioned_at="2026-03-02T00:00:00Z",
    )
    assert written == {
        "id": entry.id,
        "effective_from": "2026-03-01 00:00:00",
        "effective_until": "2026-12-31 00:00:00",
        "occurred_at": None,
        "mentioned_at": "2026-03-02 00:00:00",
        "temporal_state": "known",
    }
    read_back = env.service.decision_window(entry.id)
    assert read_back == written


def test_temporal_columns_are_additive_on_an_old_database(tmp_path):
    """An old DB without temporal columns opens read-only-compatible."""
    db_path = tmp_path / "memory.db"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE context_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identity_key TEXT NOT NULL,
            content_type TEXT NOT NULL,
            project TEXT NOT NULL DEFAULT '',
            scope TEXT NOT NULL DEFAULT 'project',
            status TEXT NOT NULL DEFAULT 'candidate',
            tier TEXT NOT NULL DEFAULT 'normal',
            tags TEXT NOT NULL DEFAULT '',
            importance REAL NOT NULL DEFAULT 5.0,
            confidence REAL NOT NULL DEFAULT 0.5,
            source_state TEXT NOT NULL DEFAULT 'none',
            source_count INTEGER NOT NULL DEFAULT 0,
            success_count INTEGER NOT NULL DEFAULT 0,
            failure_count INTEGER NOT NULL DEFAULT 0,
            access_count INTEGER NOT NULL DEFAULT 0,
            last_accessed TEXT,
            last_verified_at TEXT,
            expires_at TEXT,
            supersedes INTEGER REFERENCES context_items(id),
            superseded_by INTEGER REFERENCES context_items(id),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE context_layers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id INTEGER NOT NULL REFERENCES context_items(id) ON DELETE CASCADE,
            layer TEXT NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            generator TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(item_id, layer)
        );
        INSERT INTO context_items (identity_key, content_type, project, scope,
            status, tier, tags, importance, confidence, source_state,
            source_count, success_count, failure_count, access_count,
            created_at, updated_at)
        VALUES ('project:evolvmem:decision:legacy', 'decision', 'evolvmem',
            'project', 'active', 'normal', '[]', 5.0, 0.9, 'none', 0, 0, 0, 0,
            '2026-01-01 00:00:00', '2026-01-01 00:00:00');
        INSERT INTO context_layers (item_id, layer, content, content_hash,
            generator, created_at, updated_at) VALUES
            (1, 'l0', '旧库决策', 'hash-l0', 'legacy', '2026-01-01 00:00:00',
             '2026-01-01 00:00:00'),
            (1, 'l1', '旧库决策细节', 'hash-l1', 'legacy', '2026-01-01 00:00:00',
             '2026-01-01 00:00:00'),
            (1, 'l2', '旧库决策来源', 'hash-l2', 'legacy', '2026-01-01 00:00:00',
             '2026-01-01 00:00:00');
        """
    )
    connection.commit()
    connection.close()

    config = Config(data_dir=tmp_path)
    with ContextStore(config) as store:
        columns = {
            row["name"]
            for row in store._connection().execute(
                "PRAGMA table_info(context_items)"
            )
        }
        assert {"effective_from", "effective_until", "occurred_at", "mentioned_at"} <= columns
        legacy = store.get_item(1, include_layers=False)
        assert legacy is not None
        assert legacy.effective_from is None
        assert legacy.effective_until is None


# ---------------------------------------------------------------------------
# 3. acceptance runner
# ---------------------------------------------------------------------------


def test_mcp_exposes_as_of_read_and_decision_window_write(env):
    """The temporal feature is reachable through the MCP API, not store-only."""
    from evolvmem.context_models import ContextMode
    from evolvmem.mcp_server import MemoryMCPServer
    from evolvmem.memory_store import MemoryStore

    register(env, "evolvmem", "EvolvMem")
    old = item(
        env, "project:evolvmem:decision:supplier", project="evolvmem",
        l0="zebra 供应商 旧决定", content_type=ContextContentType.DECISION,
        effective_from="2026-01-01 00:00:00",
        effective_until="2026-06-01 00:00:00", importance=8.0,
    )
    new = item(
        env, "project:evolvmem:decision:supplier2", project="evolvmem",
        l0="zebra 供应商 新决定", content_type=ContextContentType.DECISION,
        importance=8.0,
    )

    env.config.context_mode = ContextMode.SHADOW.value
    env.config.adapter = "codex"
    with MemoryStore(env.config):
        pass
    server = MemoryMCPServer(config=env.config)
    server._init_done.set()
    server.context_service = env.service

    def call(name, arguments):
        response = server._handle_request({
            "method": "tools/call", "id": 7, "jsonrpc": "2.0",
            "params": {"name": name, "arguments": arguments},
        })
        assert "error" not in response, response
        return json.loads(response["result"]["content"][0]["text"])

    written = call(
        "context_decision_window",
        {"id": new.id, "effective_from": "2026-06-01T00:00:00Z"},
    )
    assert written["effective_from"] == "2026-06-01 00:00:00"
    assert call("context_decision_window", {"id": new.id}) == written

    current = call("context_search", {"query": "zebra 供应商", "project": "evolvmem"})
    assert [row["id"] for row in current["results"]] == [new.id]
    historical = call(
        "context_search",
        {"query": "zebra 供应商", "project": "evolvmem",
         "as_of": "2026-03-01T00:00:00Z"},
    )
    assert [row["id"] for row in historical["results"]] == [old.id]
    assert historical["as_of"] == "2026-03-01 00:00:00"
    assert "context_decision_window" in {
        tool["name"]
        for tool in server._handle_request(
            {"method": "tools/list", "id": 8, "jsonrpc": "2.0"}
        )["result"]["tools"]
    }


def test_web_api_decision_window_roundtrip(env):
    from evolvmem.web_server import api_decision_window

    register(env, "evolvmem")
    entry = item(
        env, "project:evolvmem:decision:web", project="evolvmem",
        l0="zebra web 决策", content_type=ContextContentType.DECISION,
    )
    bad = api_decision_window(env.service, {"id": entry.id,
                                            "effective_from": "nope"})
    assert bad["ok"] is False and bad["error"] == "invalid_effective_from"
    written = api_decision_window(
        env.service,
        {"id": entry.id, "effective_from": "2026-03-01T08:00:00+08:00"},
    )
    assert written["ok"] is True
    assert written["effective_from"] == "2026-03-01 00:00:00"
    read_back = api_decision_window(env.service, {"id": entry.id})
    assert read_back == written
    missing = api_decision_window(env.service, {"id": 9999})
    assert missing == {"ok": False, "error": "item_not_found"}


def test_acceptance_runner_has_required_cases_and_passes():
    from evolvmem.trust_acceptance import REQUIRED_CASE_IDS, run_cases

    results = run_cases()
    by_id = {result.case_id: result for result in results}
    assert set(REQUIRED_CASE_IDS) <= set(by_id)
    assert len(results) >= 10
    for case_id in REQUIRED_CASE_IDS:
        result = by_id[case_id]
        assert result.passed, (case_id, result.expected, result.observed,
                              result.reason)
        json.dumps(result.to_json(), ensure_ascii=False)

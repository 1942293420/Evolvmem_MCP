"""Views package: read-only sync chain, sourced project knowledge, temporal edge.

Real store/service boundaries with synthetic fixtures only; no model call, no
network, no production data. The temporal tests cover the confirmed integration
risk where a future-dated same-identity successor erased the current decision,
and the LAN revision trigger that must move when only a resolution row changes.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer

import pytest

from evolvmem.context_models import ContextContentType, ContextStatus
from evolvmem.lan_capture import LanCapture
from evolvmem.lan_context import prepare_memory_revision
from evolvmem.trust_acceptance import (
    _Env,
    _bind,
    _cover_source,
    _ids,
    _item,
    _pin_source_time,
    _register,
    _review,
    _rollup_row,
    _search,
    _summary_item,
    _supersede,
)
from evolvmem.trust_views import TrustViews


# ---------------------------------------------------------------------------
# synthetic helpers
# ---------------------------------------------------------------------------


class _Shim:
    """The minimal server surface ``prepare_memory_revision`` touches."""

    def __init__(self, service):
        self.context_service = service


def _capture(env: _Env) -> LanCapture:
    server = _Shim(env.service)
    server.config = env.config
    return LanCapture(server)


def _archive(env: _Env, external_id: str) -> int:
    with env.store.transaction():
        cursor = env.store._connection().execute(
            "INSERT INTO session_archives (project, adapter, external_session_id,"
            " payload_path, payload_sha256, state, expires_at, created_at)"
            " VALUES ('evolvmem','codex',?,'fixture.enc','fixture-hash','available',"
            " '2027-01-01 00:00:00','2026-10-01 00:00:00')",
            (external_id,),
        )
        return int(cursor.lastrowid)


def _upload(cap: LanCapture, *, session: str, sha: str, archive_id, extraction: str,
            extracted_at: str | None = None, received: float = 1000.0,
            total: int = 10, received_bytes: int = 10) -> None:
    with cap.store.transaction():
        cap.conn.execute(
            "INSERT INTO lan_session_uploads (device_id, session_id, sha256, project,"
            " declared_project, attribution_reason, received_at, backfill_status,"
            " backfill_result, total_bytes, received_bytes, archive_id,"
            " extraction_status, extraction_result, error, extracted_at)"
            " VALUES ('device-a',?,?,'evolvmem','evolvmem','',?,'pending','{}',?,?,?,?,'{}','',?)",
            (session, sha, received, total, received_bytes, archive_id, extraction,
             extracted_at),
        )


def _revision(env: _Env) -> int:
    return int(env.store._connection().execute(
        "SELECT revision FROM lan_memory_revision WHERE id=1"
    ).fetchone()[0])


@pytest.fixture
def env(tmp_path):
    environment = _Env(tmp_path)
    _register(environment, "evolvmem", "EvolvMem")
    yield environment
    environment.close()


# ---------------------------------------------------------------------------
# A. temporal correctness: a scheduled successor must not erase the current one
# ---------------------------------------------------------------------------


def test_future_successor_keeps_current_predecessor(env):
    old = _item(
        env, "project:evolvmem:decision:supplier", project="evolvmem",
        l0="供应商当前决定 A", content_type=ContextContentType.DECISION,
        effective_from="2026-01-01 00:00:00",
    )
    new = _supersede(
        env, "project:evolvmem:decision:supplier", project="evolvmem",
        l0="供应商未来决定 B", effective_from="2027-01-01 00:00:00",
    )
    selected = _ids(_search(env, "供应商", "evolvmem"))
    assert selected == [old.id]
    assert new.id not in selected
    assert env.store.get_item(new.id, include_layers=False).status is ContextStatus.ACTIVE


# ---------------------------------------------------------------------------
# A2. applicability winner is resolved over the whole identity family, not
#     only the rows whose wording matched the query
# ---------------------------------------------------------------------------


def test_newer_applicable_family_row_with_other_wording_vetoes_stale_match(env):
    old = _item(
        env, "project:evolvmem:decision:wording", project="evolvmem",
        l0="zebra 原决定", l1="zebra 旧文字",
        content_type=ContextContentType.DECISION,
        effective_from="2026-01-01 00:00:00", effective_until="2026-12-31 00:00:00",
    )
    _supersede(
        env, "project:evolvmem:decision:wording", project="evolvmem",
        l0="lion 新决定", effective_from="2026-06-01 00:00:00",
    )
    # The matching text is stale history; the applicable winner is the un-matched
    # newer row, so a query for the stale words returns no hit.
    assert _ids(_search(env, "zebra", "evolvmem", as_of="2026-08-01 00:00:00")) == []
    stored = env.store.get_item(old.id, include_layers=False)
    assert stored.effective_until == "2026-12-31 00:00:00"


def test_family_winner_with_matching_newer_wording_is_returned(env):
    old = _item(
        env, "project:evolvmem:decision:winner-match", project="evolvmem",
        l0="zebra 原决定", content_type=ContextContentType.DECISION,
        effective_from="2026-01-01 00:00:00", effective_until="2026-12-31 00:00:00",
    )
    new = _supersede(
        env, "project:evolvmem:decision:winner-match", project="evolvmem",
        l0="zebra 新决定", effective_from="2026-06-01 00:00:00",
    )
    assert _ids(_search(env, "zebra", "evolvmem", as_of="2026-08-01 00:00:00")) == [new.id]
    assert old.id not in _ids(
        _search(env, "zebra", "evolvmem", as_of="2026-08-01 00:00:00")
    )


def test_scheduled_future_successor_other_wording_keeps_predecessor_current(env):
    old = _item(
        env, "project:evolvmem:decision:scheduled", project="evolvmem",
        l0="zebra 当前有效决定", content_type=ContextContentType.DECISION,
        effective_from="2026-01-01 00:00:00",
    )
    new = _supersede(
        env, "project:evolvmem:decision:scheduled", project="evolvmem",
        l0="lion 未来决定", effective_from="2027-01-01 00:00:00",
    )
    # The predecessor is the family winner while the successor is scheduled, so
    # the matching stale-looking row is still the correct current answer.
    assert _ids(_search(env, "zebra", "evolvmem")) == [old.id]
    # After the boundary the successor wins; with other wording the old hit must
    # not be revived.
    assert _ids(_search(env, "zebra", "evolvmem", as_of="2027-06-01 00:00:00")) == []
    assert env.store.get_item(new.id, include_layers=False).status is ContextStatus.ACTIVE


def test_multiple_delayed_history_orders_by_effective_rank(env):
    key = "project:evolvmem:decision:delayed-multi"
    _item(env, key, project="evolvmem", l0="zebra 最早决定",
          content_type=ContextContentType.DECISION,
          effective_from="2025-01-01 00:00:00")
    _supersede(env, key, project="evolvmem", l0="zebra 当前决定",
               effective_from="2026-01-01 00:00:00")
    first_late = _supersede(env, key, project="evolvmem", l0="zebra 迟到二月的决定",
                            effective_from="2025-02-01 00:00:00")
    second_late = _supersede(env, key, project="evolvmem", l0="zebra 迟到四月的决定",
                             effective_from="2025-04-01 00:00:00")
    selected = _ids(_search(env, "zebra", "evolvmem", as_of="2025-05-01 00:00:00"))
    assert selected == [second_late.id]
    assert first_late.id not in selected


def test_successor_boundary_before_at_after(env):
    old = _item(
        env, "project:evolvmem:decision:window", project="evolvmem",
        l0="zebra 当前决定 A", content_type=ContextContentType.DECISION,
        effective_from="2026-01-01 00:00:00",
    )
    new = _supersede(
        env, "project:evolvmem:decision:window", project="evolvmem",
        l0="zebra 继任决定 B", effective_from="2027-01-01 00:00:00",
    )
    before = _ids(_search(env, "zebra", "evolvmem", as_of="2026-12-31 23:59:59"))
    at = _ids(_search(env, "zebra", "evolvmem", as_of="2027-01-01 00:00:00"))
    after = _ids(_search(env, "zebra", "evolvmem", as_of="2027-02-01 00:00:00"))
    assert before == [old.id]
    assert at == [new.id]
    assert after == [new.id]


def test_explicit_overlap_successor_takes_precedence(env):
    old = _item(
        env, "project:evolvmem:decision:overlap", project="evolvmem",
        l0="zebra 重叠窗口的旧决定", content_type=ContextContentType.DECISION,
        effective_from="2026-01-01 00:00:00", effective_until="2026-12-31 00:00:00",
    )
    new = _supersede(
        env, "project:evolvmem:decision:overlap", project="evolvmem",
        l0="zebra 已生效的继任决定", effective_from="2026-06-01 00:00:00",
    )
    assert _ids(_search(env, "zebra", "evolvmem")) == [new.id]
    # The superseded row and its explicit end stay stored for as_of history.
    stored = env.store.get_item(old.id, include_layers=False)
    assert stored.effective_until == "2026-12-31 00:00:00"
    assert stored.status is ContextStatus.SUPERSEDED


def test_superseded_non_decision_and_unknown_never_current(env):
    fact_old = _item(
        env, "project:evolvmem:fact:scheduled", project="evolvmem",
        l0="未来的事实 A", content_type=ContextContentType.FACT,
        effective_from="2026-01-01 00:00:00",
    )
    _supersede(
        env, "project:evolvmem:fact:scheduled", project="evolvmem",
        l0="未来的事实 B", effective_from="2027-01-01 00:00:00",
    )
    assert _ids(_search(env, "未来的事实", "evolvmem")) == []

    decision_old = _item(
        env, "project:evolvmem:decision:undated", project="evolvmem",
        l0="无日期的旧决定", content_type=ContextContentType.DECISION,
    )
    _supersede(
        env, "project:evolvmem:decision:undated", project="evolvmem",
        l0="无日期的继任决定", effective_from="2027-01-01 00:00:00",
    )
    assert _ids(_search(env, "无日期的旧决定", "evolvmem")) == []


# ---------------------------------------------------------------------------
# B. sync chain observability
# ---------------------------------------------------------------------------


def _stages(env: _Env) -> dict:
    return {stage["stage"]: stage for stage in TrustViews(env.service).sync_chain()["stages"]}


def test_sync_chain_unknown_without_receipts(env):
    stages = _stages(env)
    assert stages["client_capture"]["state"] == "unknown"
    assert stages["client_capture"]["evidence_at"] is None
    assert stages["upload_archive"]["state"] == "unknown"
    assert stages["upload_archive"]["reason"] == "no_upload_receipt"
    assert stages["extraction"]["state"] == "unknown"
    assert stages["recall"]["state"] == "unknown"
    assert stages["recall"]["evidence"]["retrieved_for_hook"] == "unknown"
    assert stages["hook_delivery"]["state"] == "unknown"
    assert stages["model_adoption"]["state"] == "unverified"


def test_sync_chain_pending_archive_and_extraction(env):
    cap = _capture(env)
    _upload(cap, session="s-pending", sha="a" * 64, archive_id=None,
            extraction="not_requested", total=20, received_bytes=5, received=1000.0)
    stages = _stages(env)
    assert stages["upload_archive"]["state"] == "pending"
    assert stages["upload_archive"]["backlog"] == 1
    assert stages["upload_archive"]["evidence_at"] == "1970-01-01 00:16:40"

    archive_id = _archive(env, "s-extract-pending")
    _upload(cap, session="s-extract-pending", sha="b" * 64, archive_id=archive_id,
            extraction="pending")
    stages = _stages(env)
    assert stages["extraction"]["state"] == "pending"
    assert stages["extraction"]["reason"] == "extraction_queued"
    assert stages["extraction"]["backlog"] == 1


def test_sync_chain_success_and_error_timestamps(env):
    cap = _capture(env)
    ok_archive = _archive(env, "s-ok")
    _upload(cap, session="s-ok", sha="c" * 64, archive_id=ok_archive,
            extraction="extracted", extracted_at="2026-10-02 03:04:05",
            received=2000.0)
    failed_archive = _archive(env, "s-failed")
    _upload(cap, session="s-failed", sha="d" * 64, archive_id=failed_archive,
            extraction="failed", extracted_at="2026-10-02 04:05:06",
            received=3000.0)
    stages = _stages(env)
    assert stages["upload_archive"]["state"] == "success"
    assert stages["upload_archive"]["backlog"] == 0
    assert stages["extraction"]["state"] == "error"
    assert stages["extraction"]["reason"] == "extraction_failed"
    assert stages["extraction"]["backlog"] == 1
    assert stages["extraction"]["evidence_at"] == "2026-10-02 04:05:06"

    # A successful extraction must not be promoted into recall/delivery proof.
    assert stages["recall"]["state"] == "unknown"
    assert stages["recall"]["evidence"]["retrieved_for_hook"] == "unknown"
    assert stages["hook_delivery"]["state"] == "unknown"
    assert stages["model_adoption"]["state"] == "unverified"


def test_sync_chain_success_when_only_extraction_completed(env):
    cap = _capture(env)
    archive_id = _archive(env, "s-clean")
    _upload(cap, session="s-clean", sha="e" * 64, archive_id=archive_id,
            extraction="extracted", extracted_at="2026-10-02 06:07:08")
    stages = _stages(env)
    assert stages["upload_archive"]["state"] == "success"
    assert stages["extraction"]["state"] == "success"
    assert stages["extraction"]["reason"] == "extraction_complete"
    assert stages["extraction"]["evidence_at"] == "2026-10-02 06:07:08"


# ---------------------------------------------------------------------------
# C. project knowledge page
# ---------------------------------------------------------------------------


def _failed_rollup(env: _Env):
    summary = _summary_item(
        env, "project:evolvmem:rollup", project="evolvmem",
        l0="知识摘要 L0", l1="保留的旧知识内容",
    )
    _review(env, summary.id, state="resolved", review="accepted")
    source = _item(
        env, "session:evolvmem:covered", project="evolvmem",
        l0="已覆盖会话来源", content_type=ContextContentType.SESSION_SUMMARY,
    )
    _review(env, source.id, state="resolved", review="accepted")
    _cover_source(env, summary.id, source.id)
    _rollup_row(env, "evolvmem", summary.id, "failed", "2026-01-01 00:00:00")
    return summary, source


def test_knowledge_failed_rollup_retained_and_not_fresh(env):
    summary, _ = _failed_rollup(env)
    page = TrustViews(env.service).knowledge("evolvmem")
    assert page["rollup"]["status"] == "failed"
    assert page["rollup"]["freshness"] == "failed"
    assert page["rollup"]["freshness"] != "fresh"
    assert page["rollup"]["needs_refresh"] is True
    assert page["latest_progress"]["retained"] is True
    assert page["latest_progress"]["available"] is True
    assert page["latest_progress"]["item_id"] == summary.id
    assert page["latest_progress"]["l1"] == "保留的旧知识内容"
    assert page["latest_progress"]["content_updated_at"] is not None


def test_knowledge_deleted_summary_not_shown(env):
    summary, _ = _failed_rollup(env)
    with env.store.transaction():
        env.store._connection().execute(
            "UPDATE context_items SET status='deleted' WHERE id=?", (summary.id,)
        )
    page = TrustViews(env.service).knowledge("evolvmem")
    assert page["latest_progress"]["available"] is False
    assert page["latest_progress"]["l1"] == ""


def test_knowledge_ready_needs_refresh_after_new_source(env):
    summary = _summary_item(
        env, "project:evolvmem:rollup", project="evolvmem",
        l0="知识摘要 L0", l1="当前知识内容",
    )
    _review(env, summary.id, state="resolved", review="accepted")
    source = _item(
        env, "session:evolvmem:covered", project="evolvmem",
        l0="已覆盖会话来源", content_type=ContextContentType.SESSION_SUMMARY,
    )
    _review(env, source.id, state="resolved", review="accepted")
    _cover_source(env, summary.id, source.id)
    _pin_source_time(env, source.id)
    _rollup_row(env, "evolvmem", summary.id, "ready", "2026-10-01 00:00:00")
    views = TrustViews(env.service)
    before = views.knowledge("evolvmem")
    assert before["rollup"]["freshness"] == "fresh"
    assert before["rollup"]["needs_refresh"] is False

    added = _item(
        env, "project:evolvmem:fact:new", project="evolvmem",
        l0="新来源事实", content_type=ContextContentType.FACT,
    )
    _review(env, added.id, state="resolved", review="accepted")
    after = views.knowledge("evolvmem")
    assert after["rollup"]["freshness"] == "stale"
    assert after["rollup"]["needs_refresh"] is True
    assert added.id in after["rollup"]["new_eligible_source_ids"]
    assert source.id in after["rollup"]["covered_source_ids"]


def test_knowledge_pending_ownership_held_but_listed(env):
    held = _item(
        env, "project:evolvmem:decision:held", project="evolvmem",
        l0="归属待确认的决定", content_type=ContextContentType.DECISION,
        effective_from="2026-01-01 00:00:00",
    )
    _review(env, held.id, state="conflict", review="")
    page = TrustViews(env.service).knowledge("evolvmem")
    assert [item["id"] for item in page["current_rules"]["items"]] == []
    assert any(row["reason"] == "project_ownership_conflict"
               for row in page["current_rules"]["excluded"])
    backlog_ids = [row["item_id"] for row in page["open_issues"]["review_backlog"]["items"]]
    assert held.id in backlog_ids


def test_knowledge_current_rules_future_successor_boundary(env):
    old = _item(
        env, "project:evolvmem:decision:choice", project="evolvmem",
        l0="当前规则 A", content_type=ContextContentType.DECISION,
        effective_from="2026-01-01 00:00:00",
    )
    new = _supersede(
        env, "project:evolvmem:decision:choice", project="evolvmem",
        l0="未来规则 B", effective_from="2027-01-01 00:00:00",
    )
    _review(env, old.id, state="resolved", review="accepted")
    _review(env, new.id, state="resolved", review="accepted")
    views = TrustViews(env.service)
    before = views.knowledge("evolvmem", now="2026-06-01 00:00:00")
    assert [item["id"] for item in before["current_rules"]["items"]] == [old.id]
    rule = before["current_rules"]["items"][0]
    assert rule["applicability"] == "current_predecessor"
    assert rule["successor_effective_from"] == "2027-01-01 00:00:00"

    at = views.knowledge("evolvmem", now="2027-01-01 00:00:00")
    assert [item["id"] for item in at["current_rules"]["items"]] == [new.id]
    after = views.knowledge("evolvmem", now="2027-06-01 00:00:00")
    assert [item["id"] for item in after["current_rules"]["items"]] == [new.id]


def test_knowledge_unknown_dates_visible(env):
    rule = _item(
        env, "project:evolvmem:constraint:undated", project="evolvmem",
        l0="无日期约束", content_type=ContextContentType.CONSTRAINT,
    )
    _review(env, rule.id, state="resolved", review="accepted")
    page = TrustViews(env.service).knowledge("evolvmem")
    assert page["rollup"]["covered_through"] is None
    assert page["rollup"]["covered_through_known"] is False
    item = next(entry for entry in page["current_rules"]["items"] if entry["id"] == rule.id)
    assert item["temporal_state"] == "unknown"
    assert item["effective_from"] is None


def test_knowledge_requires_project(env):
    page = TrustViews(env.service).knowledge("")
    assert page == {"ok": False, "error": "invalid_project"}


# ---------------------------------------------------------------------------
# C2. latest-progress applicability gates (review finding 1)
# ---------------------------------------------------------------------------


def _ready_rollup(env: _Env):
    summary = _summary_item(
        env, "project:evolvmem:rollup", project="evolvmem",
        l0="摘要 L0", l1="摘要 L1",
    )
    _review(env, summary.id, state="resolved", review="accepted")
    _rollup_row(env, "evolvmem", summary.id, "ready", "2026-10-01 00:00:00")
    return summary


def test_latest_progress_held_when_summary_candidate(env):
    summary = _ready_rollup(env)
    with env.store.transaction():
        env.store._connection().execute(
            "UPDATE context_items SET status='candidate' WHERE id=?", (summary.id,)
        )
    progress = TrustViews(env.service).knowledge("evolvmem")["latest_progress"]
    assert progress["available"] is False
    assert progress["held"] is True
    assert progress["held_reason"] == "item_candidate"
    assert progress["l1"] == ""


def test_latest_progress_held_when_summary_expired(env):
    summary = _ready_rollup(env)
    with env.store.transaction():
        env.store._connection().execute(
            "UPDATE context_items SET expires_at='2020-01-01 00:00:00' WHERE id=?",
            (summary.id,),
        )
    progress = TrustViews(env.service).knowledge("evolvmem")["latest_progress"]
    assert progress["held_reason"] == "expired"


def test_latest_progress_held_when_summary_out_of_window(env):
    summary = _ready_rollup(env)
    with env.store.transaction():
        env.store._connection().execute(
            "UPDATE context_items SET effective_from='2030-01-01 00:00:00' WHERE id=?",
            (summary.id,),
        )
    progress = TrustViews(env.service).knowledge("evolvmem")["latest_progress"]
    assert progress["held_reason"] == "out_of_window"


def test_latest_progress_held_when_summary_wrong_project(env):
    summary = _ready_rollup(env)
    with env.store.transaction():
        env.store._connection().execute(
            "UPDATE context_items SET project='other' WHERE id=?", (summary.id,)
        )
    progress = TrustViews(env.service).knowledge("evolvmem")["latest_progress"]
    assert progress["held_reason"] == "project_mismatch"


def test_latest_progress_held_when_ownership_pending(env):
    summary = _ready_rollup(env)
    _review(env, summary.id, state="conflict", review="")
    progress = TrustViews(env.service).knowledge("evolvmem")["latest_progress"]
    assert progress["held"] is True
    assert progress["held_reason"] == "project_ownership_conflict"


# ---------------------------------------------------------------------------
# C3. current-rule metadata gates (review finding 2)
# ---------------------------------------------------------------------------


def test_current_rules_low_confidence_and_expiry_excluded(env):
    weak = _item(
        env, "project:evolvmem:decision:weak", project="evolvmem",
        l0="低置信决定", content_type=ContextContentType.DECISION,
        confidence=0.1, effective_from="2026-01-01 00:00:00",
    )
    _review(env, weak.id, state="resolved", review="accepted")
    stale = _item(
        env, "project:evolvmem:constraint:stale", project="evolvmem",
        l0="已过期约束", content_type=ContextContentType.CONSTRAINT,
        effective_from="2026-01-01 00:00:00",
    )
    _review(env, stale.id, state="resolved", review="accepted")
    with env.store.transaction():
        env.store._connection().execute(
            "UPDATE context_items SET expires_at='2020-01-01 00:00:00' WHERE id=?",
            (stale.id,),
        )
    section = TrustViews(env.service).knowledge("evolvmem")["current_rules"]
    assert [item["id"] for item in section["items"]] == []
    reasons = {row["id"]: row["reason"] for row in section["excluded"]}
    assert reasons[weak.id] == "low_confidence"
    assert reasons[stale.id] == "expired"


def test_current_rules_superseded_constraint_not_current(env):
    old = _item(
        env, "project:evolvmem:constraint:policy", project="evolvmem",
        l0="旧约束 A", content_type=ContextContentType.CONSTRAINT,
        effective_from="2026-01-01 00:00:00",
    )
    new = _supersede(
        env, "project:evolvmem:constraint:policy", project="evolvmem",
        l0="未来约束 B", effective_from="2027-01-01 00:00:00",
    )
    _review(env, old.id, state="resolved", review="accepted")
    _review(env, new.id, state="resolved", review="accepted")
    section = TrustViews(env.service).knowledge(
        "evolvmem", now="2026-06-01 00:00:00"
    )["current_rules"]
    # Superseded non-decisions are never served as current, even when the
    # successor is still scheduled.
    assert [item["id"] for item in section["items"]] == []
    assert any(row["id"] == old.id and row["reason"] == "not_current"
               for row in section["excluded"])


def test_current_rules_no_stale_fallback_when_winner_untrusted(env):
    """Knowledge uses the same family winner: a held winner is no hit, not a fallback."""
    old = _item(
        env, "project:evolvmem:decision:fallback", project="evolvmem",
        l0="旧的可信决定", content_type=ContextContentType.DECISION,
        effective_from="2026-01-01 00:00:00", effective_until="2026-12-31 00:00:00",
    )
    new = _supersede(
        env, "project:evolvmem:decision:fallback", project="evolvmem",
        l0="新的归属待确认决定", effective_from="2026-06-01 00:00:00",
    )
    _review(env, old.id, state="resolved", review="accepted")
    _review(env, new.id, state="conflict", review="")
    section = TrustViews(env.service).knowledge(
        "evolvmem", now="2026-08-01 00:00:00"
    )["current_rules"]
    assert [item["id"] for item in section["items"]] == []
    reasons = {row["id"]: row["reason"] for row in section["excluded"]}
    assert reasons[new.id] == "project_ownership_conflict"
    assert reasons[old.id] == "not_current"


def test_current_rules_newer_winner_with_other_wording_served(env):
    """The applicable winner is served even when its text differs (knowledge has no query)."""
    old = _item(
        env, "project:evolvmem:decision:wording-rule", project="evolvmem",
        l0="旧规则文字", content_type=ContextContentType.DECISION,
        effective_from="2026-01-01 00:00:00", effective_until="2026-12-31 00:00:00",
    )
    new = _supersede(
        env, "project:evolvmem:decision:wording-rule", project="evolvmem",
        l0="完全不同的新规则文字", effective_from="2026-06-01 00:00:00",
    )
    _review(env, old.id, state="resolved", review="accepted")
    _review(env, new.id, state="resolved", review="accepted")
    section = TrustViews(env.service).knowledge(
        "evolvmem", now="2026-08-01 00:00:00"
    )["current_rules"]
    assert [item["id"] for item in section["items"]] == [new.id]
    assert section["items"][0]["l0"] == "完全不同的新规则文字"


# ---------------------------------------------------------------------------
# C4. source trust gating + modified covered sources (review finding 3)
# ---------------------------------------------------------------------------


def test_knowledge_held_source_not_fresh(env):
    summary = _summary_item(
        env, "project:evolvmem:rollup", project="evolvmem",
        l0="摘要 L0", l1="摘要 L1",
    )
    _review(env, summary.id, state="resolved", review="accepted")
    held = _item(
        env, "session:evolvmem:held", project="evolvmem",
        l0="归属待确认的来源", content_type=ContextContentType.SESSION_SUMMARY,
    )
    _review(env, held.id, state="conflict", review="")
    _cover_source(env, summary.id, held.id)
    _pin_source_time(env, held.id)
    _rollup_row(env, "evolvmem", summary.id, "ready", "2026-10-01 00:00:00")
    page = TrustViews(env.service).knowledge("evolvmem")
    assert page["rollup"]["freshness"] == "source_hold"
    assert page["rollup"]["reason"] == "held_source_ownership_or_validity"
    assert held.id in page["rollup"]["held_source_ids"]
    assert held.id not in page["rollup"]["trusted_source_ids"]
    source = next(s for s in page["sources"] if s["id"] == held.id)
    assert source["trusted"] is False
    assert source["held_reasons"]


def test_knowledge_modified_covered_source_needs_refresh(env):
    summary = _summary_item(
        env, "project:evolvmem:rollup", project="evolvmem",
        l0="摘要 L0", l1="摘要 L1",
    )
    _review(env, summary.id, state="resolved", review="accepted")
    source = _item(
        env, "project:evolvmem:fact:covered", project="evolvmem",
        l0="已被覆盖的事实", content_type=ContextContentType.FACT,
    )
    _review(env, source.id, state="resolved", review="accepted")
    _cover_source(env, summary.id, source.id)
    _pin_source_time(env, source.id)
    _rollup_row(env, "evolvmem", summary.id, "ready", "2026-10-01 00:00:00")
    views = TrustViews(env.service)
    assert views.knowledge("evolvmem")["rollup"]["needs_refresh"] is False
    with env.store.transaction():
        env.store._connection().execute(
            "UPDATE context_items SET updated_at='2026-10-02 00:00:00' WHERE id=?",
            (source.id,),
        )
    after = views.knowledge("evolvmem")
    assert source.id in after["rollup"]["modified_covered_source_ids"]
    assert after["rollup"]["needs_refresh"] is True
    assert after["rollup"]["freshness"] == "stale"


# ---------------------------------------------------------------------------
# C5. current workstream pointers (review finding 4)
# ---------------------------------------------------------------------------


def _workstream(env: _Env, wc: int, *, workstream_id: str, status: str = "open"):
    _bind(env, "evolvmem", wc, workstream_id)
    with env.store.transaction():
        env.store._connection().execute(
            "UPDATE continuity_workstreams SET status=? WHERE id=?",
            (status, workstream_id),
        )


def _set_l2(env: _Env, item_id: int, payload: dict) -> None:
    with env.store.transaction():
        env.store._connection().execute(
            "UPDATE context_layers SET content=? WHERE item_id=? AND layer='l2'",
            (json.dumps(payload, ensure_ascii=False), item_id),
        )


def test_knowledge_exposes_current_unfinished_workstream(env):
    checkpoint = _item(
        env, "project:evolvmem:workstream:ws1", project="evolvmem",
        l0="任务断点 L0", content_type=ContextContentType.WORKSTREAM_CHECKPOINT,
    )
    _review(env, checkpoint.id, state="resolved", review="accepted")
    _bind(env, "evolvmem", checkpoint.id, "ws1")
    _set_l2(env, checkpoint.id, {
        "objective": "完成信任视图",
        "current_step": "等待业务验收",
        "next_action": "检查知识页",
        "blockers": [],
    })
    page = TrustViews(env.service).knowledge("evolvmem")
    assert page["workstreams"]["total"] == 1
    item = page["workstreams"]["items"][0]
    assert item["id"] == "ws1"
    assert item["status"] == "open"
    assert item["current_step"] == "等待业务验收"
    assert item["next_action"] == "检查知识页"
    assert page["open_issues"]["unfinished_workstreams"][0]["id"] == "ws1"
    assert page["open_issues"]["blocked_count"] == 0


def test_knowledge_holds_untrusted_workstream_checkpoint(env):
    checkpoint = _item(
        env, "project:evolvmem:workstream:ws2", project="evolvmem",
        l0="归属待确认的断点", content_type=ContextContentType.WORKSTREAM_CHECKPOINT,
    )
    _review(env, checkpoint.id, state="conflict", review="")
    _bind(env, "evolvmem", checkpoint.id, "ws2")
    page = TrustViews(env.service).knowledge("evolvmem")
    assert page["workstreams"]["items"] == []
    assert page["workstreams"]["held"][0]["reason"] == "project_ownership_conflict"


# ---------------------------------------------------------------------------
# C6. extraction evidence separation (review finding 5)
# ---------------------------------------------------------------------------


def test_sync_chain_mixed_extraction_keeps_all_counts(env):
    cap = _capture(env)
    ok_archive = _archive(env, "s-mix-ok")
    _upload(cap, session="s-mix-ok", sha="f" * 64, archive_id=ok_archive,
            extraction="extracted", extracted_at="2026-10-02 01:00:00")
    bad_archive = _archive(env, "s-mix-bad")
    _upload(cap, session="s-mix-bad", sha="g" * 64, archive_id=bad_archive,
            extraction="failed", extracted_at="2026-10-02 02:00:00")
    stage = _stages(env)["extraction"]
    assert stage["state"] == "error"
    assert stage["evidence"]["extracted"] == 1
    assert stage["evidence"]["failed"] == 1
    assert stage["evidence"]["last_success_at"] == "2026-10-02 01:00:00"
    assert stage["evidence"]["last_failure_at"] == "2026-10-02 02:00:00"
    assert stage["evidence_at"] == "2026-10-02 02:00:00"


def test_sync_chain_pending_keeps_failure_evidence(env):
    cap = _capture(env)
    pending_archive = _archive(env, "s-pend")
    _upload(cap, session="s-pend", sha="h" * 64, archive_id=pending_archive,
            extraction="pending")
    bad_archive = _archive(env, "s-pend-bad")
    _upload(cap, session="s-pend-bad", sha="i" * 64, archive_id=bad_archive,
            extraction="failed", extracted_at="2026-10-02 05:00:00")
    stage = _stages(env)["extraction"]
    assert stage["state"] == "pending"
    assert stage["evidence"]["queued"] == 1
    assert stage["evidence"]["failed"] == 1
    assert stage["evidence"]["last_failure_at"] == "2026-10-02 05:00:00"


def test_sync_chain_upload_timestamp_basis(env):
    cap = _capture(env)
    archive_id = _archive(env, "s-basis")
    _upload(cap, session="s-basis", sha="j" * 64, archive_id=archive_id,
            extraction="extracted", extracted_at="2026-10-02 06:00:00",
            received=4000.0)
    stage = _stages(env)["upload_archive"]
    assert stage["state"] == "success"
    assert stage["evidence"]["evidence_at_basis"] == "archive_created_at"
    assert stage["evidence_at"] == "2026-10-01 00:00:00"  # session_archives.created_at
    assert stage["evidence"]["last_upload_receipt_at"] == "1970-01-01 01:06:40"


# ---------------------------------------------------------------------------
# C7. revision-0 reject path (review finding 8)
# ---------------------------------------------------------------------------


def test_reject_unreviewed_revision_zero(env):
    from evolvmem.web_server import api_resolution_reject

    item = _item(
        env, "project:evolvmem:fact:reject0", project="evolvmem",
        l0="被误标的历史条目",
    )
    before = env.store.get_item(item.id, include_layers=False)
    result = api_resolution_reject(env.service, item.id, {"expected_revision": 0})
    assert result == {"ok": True, "item_id": item.id, "review_state": "rejected"}
    row = env.store._connection().execute(
        "SELECT resolution_state, review_state, revision FROM"
        " context_project_resolutions WHERE item_id=?", (item.id,)
    ).fetchone()
    assert row["resolution_state"] == "unresolved"
    assert row["review_state"] == "rejected"
    assert int(row["revision"]) == 1
    # No content or project move, and a duplicate stale reject conflicts.
    after = env.store.get_item(item.id, include_layers=False)
    assert after.project == before.project
    assert after.identity_key == before.identity_key
    assert api_resolution_reject(
        env.service, item.id, {"expected_revision": 0}
    )["error"] == "revision_conflict"


def test_reject_revision_zero_keeps_bound_workstream(env):
    from evolvmem.web_server import api_resolution_reject

    checkpoint = _item(
        env, "project:evolvmem:workstream:wsr", project="evolvmem",
        l0="绑定工作流的断点", content_type=ContextContentType.WORKSTREAM_CHECKPOINT,
    )
    _bind(env, "evolvmem", checkpoint.id, "wsr")
    result = api_resolution_reject(env.service, checkpoint.id, {"expected_revision": 0})
    assert result["ok"] is True
    workstream = env.store._connection().execute(
        "SELECT project FROM continuity_workstreams WHERE id='wsr'"
    ).fetchone()
    assert workstream["project"] == "evolvmem"
    assert env.store.get_item(checkpoint.id, include_layers=False).project == "evolvmem"


def test_reject_revision_zero_requires_item(env):
    from evolvmem.web_server import api_resolution_reject

    assert api_resolution_reject(
        env.service, 999999, {"expected_revision": 0}
    )["error"] == "resolution_not_found"


def test_review_accept_can_pick_target_for_unbound_row(env):
    """A regular unreviewed row may be confirmed into a different registered project."""
    from evolvmem.web_server import api_resolution_accept

    _register(env, "inventory", "Inventory")
    item = _item(
        env, "project:evolvmem:fact:target", project="evolvmem",
        l0="可能被误标的事实",
    )
    result = api_resolution_accept(
        env.service, item.id, {"project": "inventory", "expected_revision": 0}
    )
    assert result["ok"] is True
    assert result["resolved_project"] == "inventory"
    assert env.store.get_item(item.id, include_layers=False).project == "inventory"


def test_review_accept_refuses_bound_workstream_cross_project(env):
    from evolvmem.web_server import api_resolution_accept

    _register(env, "inventory", "Inventory")
    checkpoint = _item(
        env, "project:evolvmem:workstream:bound", project="evolvmem",
        l0="绑定到 evolvmem 的断点",
        content_type=ContextContentType.WORKSTREAM_CHECKPOINT,
    )
    _bind(env, "evolvmem", checkpoint.id, "ws-bound")
    result = api_resolution_accept(
        env.service, checkpoint.id, {"project": "inventory", "expected_revision": 0}
    )
    assert result["error"] == "workstream_project_mismatch"
    assert env.store.get_item(checkpoint.id, include_layers=False).project == "evolvmem"
    workstream = env.store._connection().execute(
        "SELECT project FROM continuity_workstreams WHERE id='ws-bound'"
    ).fetchone()
    assert workstream["project"] == "evolvmem"


# ---------------------------------------------------------------------------
# D. LAN memory revision: resolution review must move the Windows refresh token
# ---------------------------------------------------------------------------


def test_resolution_review_moves_lan_memory_revision(env):
    prepare_memory_revision(_Shim(env.service))
    item = _item(
        env, "project:evolvmem:decision:revision", project="evolvmem",
        l0="评审会改变归属的决定", content_type=ContextContentType.DECISION,
    )
    baseline = _revision(env)
    # INSERT: an automatic pending decision writes only the resolution row.
    _review(env, item.id, state="conflict", review="")
    inserted = _revision(env)
    assert inserted > baseline
    # UPDATE: same row, only review_state/decision_source/revision change.
    with env.store.transaction():
        env.projects.accept_resolution(item.id, "evolvmem", expected_revision=1)
    accepted = _revision(env)
    assert accepted > inserted
    # DELETE: holding/removing a resolution must also move the revision.
    with env.store.transaction():
        env.store._connection().execute(
            "DELETE FROM context_project_resolutions WHERE item_id=?", (item.id,)
        )
    deleted = _revision(env)
    assert deleted > accepted


def test_resolution_trigger_is_required_for_revision_bump(env):
    """Red emulation: without the trigger the revision stays put (the old defect)."""
    prepare_memory_revision(_Shim(env.service))
    item = _item(
        env, "project:evolvmem:decision:trigger", project="evolvmem",
        l0="触发器缺失时的评审", content_type=ContextContentType.DECISION,
    )
    conn = env.store._connection()
    for action in ("insert", "update", "delete"):
        conn.execute(
            f"DROP TRIGGER IF EXISTS lan_revision_context_project_resolutions_{action}"
        )
    baseline = _revision(env)
    _review(env, item.id, state="conflict", review="")
    assert _revision(env) == baseline  # red: no trigger, no revision move
    prepare_memory_revision(_Shim(env.service))  # idempotent refresh re-installs it
    with env.store.transaction():
        conn.execute(
            "UPDATE context_project_resolutions SET review_state='rejected' WHERE item_id=?",
            (item.id,),
        )
    assert _revision(env) > baseline  # green: trigger observed the change


# ---------------------------------------------------------------------------
# E. LAN contract visibility (temporal args + diagnostics description)
# ---------------------------------------------------------------------------


def test_lan_contract_exposes_temporal_args_and_diagnostics(tmp_path):
    from tests.test_lan_sharing import settings_for
    from evolvmem.lan_runtime import LanRuntime
    from evolvmem.lan_tools import LanTools

    runtime = LanRuntime(settings_for(tmp_path))
    runtime.initialize()
    try:
        specs = LanTools(runtime)._specs("jiangli")
    finally:
        runtime.close()
    search = specs["context_search"]["inputSchema"]["properties"]
    assert "as_of" in search
    window = specs["context_decision_window"]["inputSchema"]["properties"]
    assert {"effective_from", "effective_until", "occurred_at", "mentioned_at"} <= set(window)
    assert "diagnostic" in specs["context_project_recall"]["description"].lower()


# ---------------------------------------------------------------------------
# F. real served /trust page and read-only APIs
# ---------------------------------------------------------------------------


def _http_get(url: str):
    with urllib.request.urlopen(url, timeout=10) as response:
        body = response.read().decode("utf-8")
        return response.status, response.headers.get("Content-Type", ""), body


@pytest.fixture
def http_trust(tmp_path):
    holder: dict = {}
    ready = threading.Event()

    def serve():
        environment = _Env(tmp_path)
        _register(environment, "evolvmem", "EvolvMem")
        summary, _ = _failed_rollup(environment)
        environment.service._legacy_backend()
        server = HTTPServer(("127.0.0.1", 0), _handler(environment))
        holder["server"] = server
        holder["env"] = environment
        holder["summary"] = summary
        ready.set()
        server.serve_forever()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(timeout=10)
    yield f"http://127.0.0.1:{holder['server'].server_address[1]}"
    holder["server"].shutdown()
    holder["server"].server_close()
    thread.join(timeout=5)
    holder["env"].close()


def _handler(environment: _Env):
    from evolvmem.web_server import make_handler

    return make_handler(environment.service)


def test_http_homepage_links_trust_page(http_trust):
    status, content_type, body = _http_get(http_trust + "/")
    assert status == 200
    assert "text/html" in content_type
    assert 'href="/trust"' in body
    assert "来源与归属明细" in body


def test_http_trust_page_and_readonly_apis(http_trust):
    status, content_type, body = _http_get(http_trust + "/trust")
    assert status == 200
    assert "text/html" in content_type
    assert 'id="trust-project"' in body
    assert "/trust.js" in body

    status, _, body = _http_get(http_trust + "/api/trust/projects")
    assert status == 200
    projects = json.loads(body)["projects"]
    assert any(row["project"] == "evolvmem" for row in projects)

    status, _, body = _http_get(http_trust + "/api/knowledge?project=evolvmem")
    assert status == 200
    page = json.loads(body)
    assert page["ok"] is True
    assert page["rollup"]["status"] == "failed"
    assert page["rollup"]["freshness"] == "failed"
    assert page["latest_progress"]["retained"] is True
    assert page["latest_progress"]["l1"] == "保留的旧知识内容"
    assert page["model_adoption"] == "UNVERIFIED"

    status, _, body = _http_get(http_trust + "/api/sync-chain")
    assert status == 200
    sync = json.loads(body)
    stages = {stage["stage"]: stage for stage in sync["stages"]}
    assert stages["client_capture"]["state"] == "unknown"
    assert stages["model_adoption"]["state"] == "unverified"

    with pytest.raises(urllib.error.HTTPError) as error:
        _http_get(http_trust + "/api/knowledge?project=")
    assert error.value.code == 400

"""Read-only trust views: sync-chain observability + sourced project knowledge.

Both views exist to answer *what can actually be proven* and nothing more:

* ``sync_chain`` composes the client-capture → upload/archive → extraction →
  recall → hook-delivery chain from persisted server receipts only. Stages the
  server cannot observe (local Windows capture, the client's hook retrieval and
  delivery, desktop-model adoption) are reported ``unknown``/``unverified`` with
  a stable reason; they are never inferred from a connection, an access count,
  a successful extraction or a configured hook.
* ``knowledge`` composes the project's Current rules / Latest progress / Open
  issues page from the persisted rollup and exact context rows on read. It
  performs no model call and no write. A failed rollup keeps serving the
  previous summary labelled ``failed``; a new eligible source past
  ``covered_through`` reports ``needs_refresh``; pending/conflicting/deleted
  ownership never enters the default sections.

Every timestamp is either an observed receipt time or ``None`` (UNKNOWN). No
absolute paths, tokens, credentials or message bodies leave this module.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from evolvmem.context_models import ContextContentType, ContextLayer, ContextStatus
from evolvmem.context_temporal import (
    applicability_preference,
    successor_takes_precedence,
    temporal_rank,
    temporal_state,
    window_contains,
)
from evolvmem.project_ownership import UNREVIEWED_FACT, load_ownership
from evolvmem.project_rollup import ProjectRollupGenerator

MODEL_ADOPTION = "UNVERIFIED"

_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"

# States a stage may report. ``unverified`` is reserved for claims that require
# a live desktop session; ``unknown`` means the server holds no evidence.
SUCCESS = "success"
PENDING = "pending"
ERROR = "error"
UNKNOWN = "unknown"
UNVERIFIED = "unverified"

_SYNC_STAGES = (
    "client_capture",
    "upload_archive",
    "extraction",
    "recall",
    "hook_delivery",
    "model_adoption",
)

_RULE_TYPES = (
    ContextContentType.DECISION.value,
    ContextContentType.CONSTRAINT.value,
    ContextContentType.WORKFLOW_POLICY.value,
)

_MAX_RULES = 20
_MAX_BACKLOG = 50
_MAX_ITEMS = 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime(_TIMESTAMP_FORMAT)


def _epoch_to_iso(value) -> str | None:
    """Observed receipt timestamps are stored as epoch seconds; render as UTC."""
    if value is None:
        return None
    try:
        return time.strftime(_TIMESTAMP_FORMAT, time.gmtime(float(value)))
    except (TypeError, ValueError, OverflowError):
        return None


def _stage(stage: str, state: str, *, evidence_at: str | None = None,
           reason: str = "", backlog: int | None = None,
           evidence: dict | None = None) -> dict:
    return {
        "stage": stage,
        "state": state,
        "evidence_at": evidence_at,
        "reason": reason,
        "backlog": backlog,
        "evidence": evidence or {},
    }


class TrustViews:
    """Read-only projections over one ContextService; safe to call per request."""

    def __init__(self, service) -> None:
        self.service = service
        self.store = service.store

    # ---- small connection helpers ----

    def _rows(self, sql: str, args=()) -> list[dict]:
        try:
            return [dict(row) for row in self.store._connection().execute(sql, args)]
        except Exception:
            # A local console store may predate the LAN capture table; a missing
            # relation means "no evidence here", never a fabricated stage.
            return []

    def _one(self, sql: str, args=()):
        try:
            return self.store._connection().execute(sql, args).fetchone()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # sync chain observability
    # ------------------------------------------------------------------

    def sync_chain(self) -> dict:
        """Compose the read-only sync chain; evidence only, never inference."""
        uploads = self._one(
            "SELECT COUNT(*) AS total,"
            " SUM(CASE WHEN u.archive_id IS NOT NULL THEN 1 ELSE 0 END) AS archived,"
            " SUM(CASE WHEN u.archive_id IS NULL THEN 1 ELSE 0 END) AS receiving,"
            " MAX(u.received_at) AS last_received,"
            " MAX(a.created_at) AS last_archive"
            " FROM lan_session_uploads u"
            " LEFT JOIN session_archives a ON a.id=u.archive_id"
        )
        total = int(uploads["total"] or 0) if uploads is not None else 0
        archived = int(uploads["archived"] or 0) if uploads is not None else 0
        receiving = int(uploads["receiving"] or 0) if uploads is not None else 0
        last_received = _epoch_to_iso(uploads["last_received"]) if uploads else None
        last_archive = uploads["last_archive"] if uploads else None

        extraction = self._one(
            "SELECT"
            " SUM(CASE WHEN extraction_status='extracted' THEN 1 ELSE 0 END) AS extracted,"
            " SUM(CASE WHEN extraction_status IN ('pending','processing') THEN 1 ELSE 0 END) AS queued,"
            " SUM(CASE WHEN extraction_status='failed' THEN 1 ELSE 0 END) AS failed,"
            " SUM(CASE WHEN extraction_status='not_requested' THEN 1 ELSE 0 END) AS not_requested,"
            " SUM(CASE WHEN extraction_status='superseded' THEN 1 ELSE 0 END) AS superseded,"
            " MAX(CASE WHEN extraction_status='extracted' THEN extracted_at END) AS last_success,"
            " MAX(CASE WHEN extraction_status='failed' THEN extracted_at END) AS last_failure"
            " FROM lan_session_uploads WHERE archive_id IS NOT NULL"
        )
        extracted = int(extraction["extracted"] or 0) if extraction else 0
        queued = int(extraction["queued"] or 0) if extraction else 0
        failed = int(extraction["failed"] or 0) if extraction else 0
        not_requested = int(extraction["not_requested"] or 0) if extraction else 0
        superseded = int(extraction["superseded"] or 0) if extraction else 0
        last_success = extraction["last_success"] if extraction else None
        last_failure = extraction["last_failure"] if extraction else None

        recallable = self._one(
            "SELECT COUNT(DISTINCT s.item_id) AS items, MAX(i.created_at) AS last_item"
            " FROM context_sources s JOIN context_items i ON i.id=s.item_id"
            " WHERE s.archive_id IS NOT NULL AND i.status != 'deleted'"
        )
        recallable_items = int(recallable["items"] or 0) if recallable else 0
        last_item = recallable["last_item"] if recallable else None

        if total == 0:
            upload = _stage(
                "upload_archive", UNKNOWN, reason="no_upload_receipt",
                evidence={"archived": 0, "receiving": 0},
            )
        elif receiving > 0:
            upload = _stage(
                "upload_archive", PENDING, evidence_at=last_received,
                reason="archive_chunks_receiving", backlog=receiving,
                evidence={
                    "archived": archived, "receiving": receiving,
                    "last_upload_receipt_at": last_received,
                    "last_archive_at": last_archive,
                    "evidence_at_basis": "last_upload_receipt",
                },
            )
        else:
            # Archived rows are proven complete; the archive creation time is
            # the observed completion evidence (received_at only marks the last
            # chunk arriving, so it stays a separate receipt value).
            upload = _stage(
                "upload_archive", SUCCESS, evidence_at=last_archive,
                reason="archive_receipts_complete", backlog=0,
                evidence={
                    "archived": archived, "receiving": 0,
                    "last_upload_receipt_at": last_received,
                    "last_archive_at": last_archive,
                    "evidence_at_basis": "archive_created_at",
                },
            )

        extraction_evidence = {
            "extracted": extracted, "queued": queued, "failed": failed,
            "not_requested": not_requested, "superseded": superseded,
            "last_success_at": last_success, "last_failure_at": last_failure,
            "evidence_at_basis": "extracted_at",
        }
        if archived == 0:
            extract = _stage(
                "extraction", UNKNOWN, reason="no_archive_to_extract",
                evidence=extraction_evidence,
            )
        elif queued > 0:
            extract = _stage(
                "extraction", PENDING, evidence_at=last_success,
                reason="extraction_queued", backlog=queued,
                evidence=extraction_evidence,
            )
        elif failed > 0:
            extract = _stage(
                "extraction", ERROR, evidence_at=last_failure,
                reason="extraction_failed", backlog=failed,
                evidence=extraction_evidence,
            )
        elif extracted > 0:
            extract = _stage(
                "extraction", SUCCESS, evidence_at=last_success,
                reason="extraction_complete", backlog=0,
                evidence=extraction_evidence,
            )
        elif not_requested > 0:
            extract = _stage(
                "extraction", PENDING, evidence_at=last_success,
                reason="extraction_not_requested", backlog=not_requested,
                evidence=extraction_evidence,
            )
        else:
            extract = _stage(
                "extraction", UNKNOWN, reason="extraction_state_unavailable",
                evidence=extraction_evidence,
            )

        # The server persists archives and extracted memory; it does not record
        # the client's hook retrieval, hook delivery, or model adoption.
        capture = _stage(
            "client_capture", UNKNOWN, reason="client_local_stage_unobservable",
            evidence={"last_upload_received_at": last_received,
                      "note": "server cannot observe the Windows local capture stage"},
        )
        recall = _stage(
            "recall", UNKNOWN, reason="hook_retrieval_not_recorded",
            evidence={"retrieved_for_hook": UNKNOWN,
                      "recallable_context_items": recallable_items,
                      "last_recallable_item_at": last_item},
        )
        hook = _stage(
            "hook_delivery", UNKNOWN, reason="client_hook_delivery_unobservable",
            evidence={"note": "actual hook delivery happens on the client"},
        )
        adoption = _stage(
            "model_adoption", UNVERIFIED, reason="no_live_desktop_test",
            evidence={"model_adoption": MODEL_ADOPTION},
        )

        stages = [capture, upload, extract, recall, hook, adoption]
        backlog_values = [
            stage["backlog"] for stage in stages if stage["backlog"] is not None
        ]
        times = [stage["evidence_at"] for stage in stages if stage["evidence_at"]]
        return {
            "as_of": _now_iso(),
            "stages": stages,
            "stage_order": list(_SYNC_STAGES),
            "backlog_total": sum(backlog_values) if backlog_values else 0,
            "last_evidence_at": max(times) if times else None,
            "model_adoption": MODEL_ADOPTION,
            "notes": [
                "客户端本机采集无法从服务端证明，保持 unknown。",
                "服务端只证明归档与提炼回执；检索/注入是否真的发生不可观测。",
                "桌面模型是否采纳召回内容未经验证（UNVERIFIED）。",
            ],
        }

    # ------------------------------------------------------------------
    # project knowledge page
    # ------------------------------------------------------------------

    def projects(self) -> list[dict]:
        """Registered projects with display names for the page selector."""
        return self._rows(
            "SELECT project, display_name, status, revision"
            " FROM context_project_registry ORDER BY project"
        )

    def _rollup(self) -> ProjectRollupGenerator:
        return ProjectRollupGenerator(self.service.config, self.store, llm=None)

    def knowledge(self, project: str, *, now: str | None = None) -> dict:
        """Compose one project's read-only knowledge page; unknown -> empty page.

        ``now`` is the evaluation instant; it defaults to the current UTC clock
        and is injectable so temporal boundaries stay deterministically testable.
        """
        project = str(project or "").strip()
        if not project or len(project) > 200:
            return {"ok": False, "error": "invalid_project"}
        registry = self._one(
            "SELECT display_name, status FROM context_project_registry WHERE project=?",
            (project,),
        )
        snapshot = self._rollup().knowledge_snapshot(project)
        now = now or _now_iso()
        freshness, reason = self._freshness(snapshot)
        rules = self._current_rules(project, now)
        workstreams = self._current_workstreams(project, now)
        return {
            "ok": True,
            "project": project,
            "display_name": registry["display_name"] if registry else "",
            "registered": registry is not None,
            "generated_at": now,
            "model_adoption": MODEL_ADOPTION,
            "rollup": {
                "status": snapshot["status"],
                "freshness": freshness,
                "reason": reason,
                "needs_refresh": bool(snapshot["needs_refresh"]),
                "content_updated_at": None,
                "rollup_updated_at": snapshot["rollup_updated_at"],
                "covered_through": snapshot["covered_through"],
                "covered_through_known": snapshot["covered_through"] is not None,
                "current_context_id": snapshot["current_context_id"],
                "covered_source_ids": snapshot["covered_source_ids"],
                "eligible_source_ids": snapshot["eligible_source_ids"],
                "trusted_source_ids": snapshot["trusted_source_ids"],
                "unverified_source_ids": snapshot["unverified_source_ids"],
                "held_source_ids": snapshot["held_source_ids"],
                "new_eligible_source_ids": snapshot["new_eligible_source_ids"],
                "new_trusted_source_ids": snapshot["new_trusted_source_ids"],
                "new_held_source_ids": snapshot["new_held_source_ids"],
                "modified_covered_source_ids": snapshot["modified_covered_source_ids"],
                "newest_eligible_source_at": snapshot["newest_eligible_source_at"],
                "source_hold": snapshot["source_hold"],
            },
            "latest_progress": self._latest_progress(snapshot, project, now),
            "current_rules": rules,
            "workstreams": workstreams,
            "open_issues": self._open_issues(
                project, rules["unverified_ids"], workstreams, snapshot
            ),
            "sources": self._sources(snapshot),
        }

    @staticmethod
    def _freshness(snapshot: dict) -> tuple[str, str]:
        status = snapshot["status"]
        if status == "failed":
            return "failed", "rollup_generation_failed"
        if status == "vector_dirty":
            return "index_pending", "vector_index_not_synced"
        if status in ("pending", "missing"):
            return ("pending", "rollup_pending") if snapshot["trusted_source_ids"] \
                else ("empty", "no_trusted_source")
        if snapshot["source_hold"]:
            # The summary may be current, but at least one source it covers is
            # not trustworthy/valid: report that instead of an apparently fresh
            # trusted summary. Review fixes the source; it is never auto-adopted.
            return "source_hold", "held_source_ownership_or_validity"
        if snapshot["new_trusted_source_ids"] or snapshot["modified_covered_source_ids"]:
            return "stale", "new_or_modified_source_after_coverage"
        return "fresh", "covered_through_watermark"

    def _latest_progress(self, snapshot: dict, project: str, now: str) -> dict:
        """Ready summary, or the retained previous summary labelled failed.

        The rollup pointer is only evidence of *where* the summary lives; the
        referenced item still has to be a currently applicable, ownership-trusted
        project summary. Candidate/archived/superseded/deleted rows, a wrong
        project, an expired row, a row outside its known window, or an excluded
        ownership decision are all held out of the default page (and named in
        the open issues instead). Nothing is regenerated or rewritten here.
        """
        item_id = snapshot["current_context_id"]
        payload = {
            "available": False,
            "retained": False,
            "held": False,
            "held_reason": "",
            "source_hold": False,
            "state": snapshot["status"],
            "item_id": item_id,
            "content_updated_at": None,
            "covered_through": snapshot["covered_through"],
            "l0": "",
            "l1": "",
            "l2": "",
            "source_ids": snapshot["covered_source_ids"],
            "note": "",
        }
        if item_id is None:
            payload["note"] = (
                "尚未生成项目摘要。" if snapshot["trusted_source_ids"]
                else "这个项目还没有可用于摘要的可信来源。"
            )
            return payload
        item = self.store.get_item(int(item_id), include_layers=False)
        fact = load_ownership(self.store, [int(item_id)]).get(
            int(item_id), UNREVIEWED_FACT
        )
        reason = self._summary_hold_reason(item, project, fact, now)
        if reason is not None:
            payload.update(
                held=True, held_reason=reason,
                note=self._hold_note(reason),
            )
            return payload
        if snapshot["source_hold"]:
            payload["source_hold"] = True
        payload.update(
            available=True,
            retained=snapshot["status"] in ("failed", "pending", "missing"),
            content_updated_at=item.updated_at,
            l0=self.store.get_layer(int(item_id), ContextLayer.L0) or "",
            l1=self.store.get_layer(int(item_id), ContextLayer.L1) or "",
            l2=self.store.get_layer(int(item_id), ContextLayer.L2) or "",
        )
        if snapshot["status"] == "failed":
            payload["note"] = "最近一次生成失败，下面保留的是上一次成功保存的内容，不是最新结论。"
        elif snapshot["status"] == "vector_dirty":
            payload["note"] = "摘要正文已保存，检索索引仍待同步。"
        elif snapshot["source_hold"]:
            payload["note"] = "摘要已保存，但它覆盖的部分来源归属或时间仍待确认。"
        return payload

    @staticmethod
    def _hold_note(reason: str) -> str:
        return {
            "item_missing": "摘要条目已不存在，不展示内容。",
            "item_deleted": "摘要条目已删除，不展示内容。",
            "item_candidate": "摘要条目仍是候选，尚未确认，不展示内容。",
            "item_archived": "摘要条目已归档，不展示内容。",
            "item_superseded": "摘要条目已被替代，不展示内容。",
            "project_mismatch": "摘要条目的项目归属与当前项目不一致，不展示内容。",
            "not_project_summary": "指针指向的不是项目摘要条目，不展示内容。",
            "expired": "摘要条目已过期，不展示内容。",
            "out_of_window": "摘要条目不在当前生效时间窗口内，不展示内容。",
            "item_not_current": "摘要条目当前不生效，不展示内容。",
        }.get(reason, "摘要条目当前不可用，不展示内容。")

    @staticmethod
    def _summary_hold_reason(item, project: str, fact, now: str) -> str | None:
        """Stable reason why a rollup pointer must not serve content, else None."""
        if item is None:
            return "item_missing"
        if item.status is ContextStatus.DELETED:
            return "item_deleted"
        if fact.excluded:
            return fact.reason
        if item.content_type is not ContextContentType.PROJECT_SUMMARY:
            return "not_project_summary"
        if item.project != project:
            return "project_mismatch"
        if item.status is ContextStatus.CANDIDATE:
            return "item_candidate"
        if item.status is ContextStatus.ARCHIVED:
            return "item_archived"
        if item.status is ContextStatus.SUPERSEDED:
            return "item_superseded"
        if item.status is not ContextStatus.ACTIVE:
            return "item_not_current"
        if item.expires_at is not None and item.expires_at <= now:
            return "expired"
        if not window_contains(item.effective_from, item.effective_until, now):
            return "out_of_window"
        return None

    @staticmethod
    def _applicability(*, status: str, content_type: str, effective_from,
                       effective_until, occurred_at, implicit_end, now: str,
                       allow_predecessor: bool) -> str | None:
        """Current applicability using the same rule as retrieval.

        An active row is current when its known window contains ``now``. A
        superseded record stays current only for a *decision* whose known
        successor is still scheduled; a superseded constraint/policy, an
        unknown-dated row, and a row whose successor already took precedence are
        not current. Supersession takes precedence over an overlapping explicit
        ``effective_until`` for the same identity.
        """
        if status == ContextStatus.ACTIVE.value:
            application = "current"
        elif (
            allow_predecessor
            and status == ContextStatus.SUPERSEDED.value
            and content_type == ContextContentType.DECISION.value
            and temporal_rank(effective_from, occurred_at) is not None
            and implicit_end is not None
            and not successor_takes_precedence(implicit_end, now)
        ):
            application = "current_predecessor"
        else:
            return None
        end = effective_until
        if end is None and status == ContextStatus.SUPERSEDED.value:
            end = implicit_end
        if not window_contains(effective_from, end, now):
            return None
        return application

    def _current_rules(self, project: str, now: str) -> dict:
        """Currently effective decisions/constraints/policies, ownership-gated.

        Applies the same temporal rule as retrieval (a superseded *decision*
        stays current only while its known successor is still scheduled) plus the
        retrieval metadata gates: minimum confidence, unexpired row, known window
        containing ``now``, and non-excluded ownership. Each rejection carries a
        stable reason so the page can explain the hold.
        """
        placeholders = ",".join("?" for _ in _RULE_TYPES)
        rows = self._rows(
            "SELECT i.id, i.identity_key, i.content_type, i.status, i.project,"
            " i.effective_from, i.effective_until, i.occurred_at, i.mentioned_at,"
            " i.confidence, i.expires_at, i.tier, i.updated_at,"
            " (SELECT content FROM context_layers l WHERE l.item_id=i.id AND l.layer='l0') AS l0"
            " FROM context_items i"
            " WHERE i.project=? AND i.status IN ('active','superseded')"
            f" AND i.content_type IN ({placeholders})"
            " ORDER BY i.updated_at DESC, i.id DESC LIMIT ?",
            (project, *_RULE_TYPES, _MAX_ITEMS),
        )
        min_confidence = self.service.config.context_min_confidence
        facts = load_ownership(self.store, [row["id"] for row in rows])
        implicit_ends = self.store.successor_ranks([row["id"] for row in rows])
        # Resolve the applicable winner over the whole identity family first, so
        # a stale row can never be shown as current merely because the newer
        # applicable row carries different wording or fails a later trust gate.
        groups: dict[tuple, list[dict]] = {}
        order: list[tuple] = []
        for row in rows:
            item_id = int(row["id"])
            application = self._applicability(
                status=str(row["status"]),
                content_type=str(row["content_type"]),
                effective_from=row["effective_from"],
                effective_until=row["effective_until"],
                occurred_at=row["occurred_at"],
                implicit_end=implicit_ends.get(item_id),
                now=now,
                allow_predecessor=True,
            )
            key = (str(row["identity_key"]), str(row["project"]))
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append({
                "row": row,
                "id": item_id,
                "application": application,
                "implicit_end": implicit_ends.get(item_id),
                "expired": row["expires_at"] is not None and row["expires_at"] <= now,
                "low_confidence": float(row["confidence"] or 0.0) < min_confidence,
                "fact": facts.get(item_id, UNREVIEWED_FACT),
            })
        items: list[dict] = []
        excluded: list[dict] = []
        unverified: list[int] = []
        for key in order:
            family = groups[key]
            temporal = [entry for entry in family if entry["application"] is not None]
            if temporal:
                winner = max(
                    temporal,
                    key=lambda entry: applicability_preference(
                        active=entry["row"]["status"] == "active",
                        effective_from=entry["row"]["effective_from"],
                        occurred_at=entry["row"]["occurred_at"],
                        item_id=entry["id"],
                    ),
                )
                losers = [entry for entry in family if entry is not winner]
            else:
                winner = None
                losers = family
            for entry in losers:
                excluded.append({
                    "id": entry["id"], "reason": "not_current",
                    "reasons": ["not_current"],
                })
            if winner is None:
                continue
            row = winner["row"]
            fact = winner["fact"]
            reasons: list[str] = []
            if fact.excluded:
                reasons.append(fact.reason)
            if winner["low_confidence"]:
                reasons.append("low_confidence")
            if winner["expired"]:
                reasons.append("expired")
            if reasons:
                excluded.append({
                    "id": winner["id"], "reason": reasons[0], "reasons": reasons,
                })
                continue
            if fact.state == "unverified":
                unverified.append(winner["id"])
            items.append({
                "id": winner["id"],
                "identity_key": str(row["identity_key"]),
                "content_type": row["content_type"],
                "status": row["status"],
                "project": row["project"],
                "l0": row["l0"] or "",
                "confidence": row["confidence"],
                "tier": row["tier"],
                "effective_from": row["effective_from"],
                "effective_until": row["effective_until"],
                "occurred_at": row["occurred_at"],
                "mentioned_at": row["mentioned_at"],
                "temporal_state": temporal_state(
                    row["effective_from"], row["effective_until"],
                    row["occurred_at"], row["mentioned_at"],
                ),
                "applicability": winner["application"],
                "successor_effective_from": (
                    winner["implicit_end"]
                    if winner["application"] == "current_predecessor" else None
                ),
                "ownership": fact.public(),
                "updated_at": row["updated_at"],
            })
        items.sort(key=lambda entry: (entry["updated_at"], entry["id"]), reverse=True)
        items = items[:_MAX_RULES]
        return {
            "items": items,
            "total": len(items),
            "excluded": excluded,
            "unverified_ids": unverified,
            "note": (
                "只列当前有效的决定/约束/策略；未来的继任者不会抹掉当前生效的前任（仅限决定），"
                "继任者到期后由继任者优先。"
            ),
        }

    def _current_workstreams(self, project: str, now: str) -> dict:
        """Exact current unfinished workstream pointers, with common gates.

        Reads ``continuity_workstreams.current_context_id`` (the exact pointer),
        requires the checkpoint item to be the same active project row inside its
        known window and not ownership-excluded, and returns the bounded
        objective/current step/next action/blockers. Successful tests are never
        turned into "completed" here; the stored workstream status is reported
        as-is.
        """
        rows = self._rows(
            "SELECT w.id, w.status, w.current_context_id AS item_id,"
            " w.checkpoint_revision, w.updated_at,"
            " i.status AS item_status, i.project AS item_project,"
            " i.expires_at, i.effective_from, i.effective_until,"
            " l.content AS l2"
            " FROM continuity_workstreams w"
            " LEFT JOIN context_items i ON i.id=w.current_context_id"
            " LEFT JOIN context_layers l ON l.item_id=i.id AND l.layer='l2'"
            " WHERE w.project=? AND w.status IN ('open','paused','blocked')"
            " ORDER BY w.updated_at DESC, w.id LIMIT ?",
            (project, _MAX_BACKLOG),
        )
        facts = load_ownership(
            self.store,
            [int(row["item_id"]) for row in rows if row["item_id"] is not None],
        )
        items: list[dict] = []
        held: list[dict] = []
        for row in rows:
            item_id = int(row["item_id"]) if row["item_id"] is not None else None
            reason = None
            if item_id is None:
                reason = "checkpoint_missing"
            elif row["item_status"] is None:
                reason = "checkpoint_missing"
            elif str(row["item_status"]) != ContextStatus.ACTIVE.value:
                reason = "item_not_current"
            elif str(row["item_project"] or "") != project:
                reason = "project_mismatch"
            else:
                fact = facts.get(item_id, UNREVIEWED_FACT)
                if fact.excluded:
                    reason = fact.reason
                elif row["expires_at"] is not None and row["expires_at"] <= now:
                    reason = "expired"
                elif not window_contains(
                    row["effective_from"], row["effective_until"], now
                ):
                    reason = "out_of_window"
            if reason is not None:
                held.append({
                    "id": row["id"], "item_id": item_id, "status": row["status"],
                    "reason": reason,
                })
                continue
            payload = self._workstream_payload(row["l2"])
            items.append({
                "id": str(row["id"]),
                "status": str(row["status"]),
                "item_id": item_id,
                "checkpoint_revision": int(row["checkpoint_revision"]),
                "updated_at": row["updated_at"],
                "objective": payload.get("objective") or "",
                "current_step": payload.get("current_step") or "",
                "next_action": payload.get("next_action") or "",
                "blockers": payload.get("blockers") or [],
                "ownership": facts.get(item_id, UNREVIEWED_FACT).public(),
            })
        return {"items": items, "total": len(items), "held": held}

    @staticmethod
    def _workstream_payload(raw) -> dict:
        try:
            payload = json.loads(raw or "{}")
        except ValueError:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        return {
            key: payload.get(key)
            for key in ("objective", "current_step", "next_action", "blockers")
        }

    def _open_issues(self, project: str, unverified_ids: list[int],
                     workstreams: dict, snapshot: dict) -> dict:
        """Review backlog + current unfinished workstreams + source trust holds.

        The page is scoped to one project: another project's held rows must not
        leak in (the standalone review API still lists them globally).
        """
        pending = self._rows(
            "SELECT r.item_id, r.resolution_state, r.review_state, r.proposed_project,"
            " r.resolved_project, r.confidence, r.method, r.revision, r.updated_at,"
            " i.identity_key, i.content_type, i.project AS current_project,"
            " (SELECT content FROM context_layers l WHERE l.item_id=i.id AND l.layer='l0') AS l0"
            " FROM context_project_resolutions r JOIN context_items i ON i.id=r.item_id"
            " WHERE r.review_state='pending' AND i.status != 'deleted' AND i.project=?"
            " ORDER BY r.updated_at DESC, r.item_id LIMIT ?",
            (project, _MAX_BACKLOG),
        )
        unreviewed = self._rows(
            "SELECT i.id AS item_id, i.identity_key, i.content_type, i.project,"
            " i.created_at,"
            " (SELECT content FROM context_layers l WHERE l.item_id=i.id AND l.layer='l0') AS l0"
            " FROM context_items i LEFT JOIN context_project_resolutions r ON r.item_id=i.id"
            " WHERE r.item_id IS NULL AND i.project=? AND i.status != 'deleted'"
            " ORDER BY i.id LIMIT ?",
            (project, _MAX_BACKLOG),
        )
        backlog = [
            {
                "item_id": int(row["item_id"]),
                "kind": "pending_review",
                "key": row["identity_key"],
                "value": row["l0"] or "",
                "content_type": row["content_type"],
                "current_project": row["current_project"],
                "proposed_project": row["proposed_project"],
                "resolution_state": row["resolution_state"],
                "review_state": row["review_state"],
                "confidence": row["confidence"],
                "method": row["method"],
                "revision": int(row["revision"]),
                "updated_at": row["updated_at"],
            }
            for row in pending
        ]
        backlog.extend(
            {
                "item_id": int(row["item_id"]),
                "kind": "unreviewed",
                "key": row["identity_key"],
                "value": row["l0"] or "",
                "content_type": row["content_type"],
                "current_project": row["project"],
                "proposed_project": "",
                "resolution_state": "none",
                "review_state": "none",
                "confidence": "none",
                "method": "",
                "revision": 0,
                "updated_at": row["created_at"],
            }
            for row in unreviewed
        )
        held = list(snapshot.get("held_sources", []))
        return {
            "review_backlog": {
                "total": len(backlog),
                "items": backlog,
                "note": (
                    "这些条目的项目归属尚未确认，默认知识页与注入面都会扣留，"
                    "确认或拒绝后才离开队列。"
                ) if backlog else "没有待确认归属的条目。",
            },
            "unverified_ownership_ids": list(unverified_ids),
            "unfinished_workstreams": workstreams["items"],
            "held_workstreams": workstreams["held"],
            "blocked_count": sum(
                1 for item in workstreams["items"] if item["status"] == "blocked"
            ),
            "held_sources": [
                {"id": entry["id"], "reason": entry["reasons"][0],
                 "reasons": entry["reasons"]}
                for entry in held
            ],
            "held_source_count": len(held),
        }

    def _sources(self, snapshot: dict) -> list[dict]:
        """Bounded source evidence: ids, types, coverage, trust and hold reason."""
        ids = list(dict.fromkeys(
            [*snapshot["covered_source_ids"], *snapshot["eligible_source_ids"]]
        ))
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        rows = self._rows(
            "SELECT id, content_type, status, project, created_at, updated_at"
            f" FROM context_items WHERE id IN ({marks}) ORDER BY created_at, id",
            tuple(ids),
        )
        covered = set(snapshot["covered_source_ids"])
        trusted = set(snapshot["trusted_source_ids"])
        unverified = set(snapshot.get("unverified_source_ids", []))
        held = {
            int(entry["id"]): list(entry["reasons"])
            for entry in snapshot.get("held_sources", [])
        }
        return [
            {
                "id": int(row["id"]),
                "content_type": row["content_type"],
                "status": row["status"],
                "project": row["project"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "covered": int(row["id"]) in covered,
                "trusted": int(row["id"]) in trusted,
                "ownership_unverified": int(row["id"]) in unverified,
                "held_reasons": held.get(int(row["id"]), []),
            }
            for row in rows
        ]


__all__ = ["MODEL_ADOPTION", "TrustViews"]

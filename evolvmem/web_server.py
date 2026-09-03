"""Local web memory management console: stdlib-only HTTP server + JSON API.

Serves a single-page UI (evolvmem/web_static/index.html) and a JSON API on
top of a ContextService + legacy compatibility facade — no third-party
dependencies. Every lifecycle mutation (importance/tier/attribute/tags
update, archive, restore, soft delete, hard delete) routes through the typed
facade so compat/shadow/primary modes mirror it onto the mapped Context side
in one transaction; list/read keep returning legacy rows through the facade.

Project attribution and the resolution review queue are served from the
ContextStore owned by the service: attribution reads enrich legacy rows with
``context_items.project`` through the migration mapping, and review writes
(accept/reject/register) go through ``ProjectStore`` inside the store's own
transaction — the same path as ``evolvmem.project_cli``, revision CAS
included. Unlike the operator CLI this console already serves full memory
content to its local user, so review rows carry key/value previews; evidence
stays limited to the resolver's bounded public rows (no paths, no raw text).

Run:
    PYTHONPATH=. .venv/bin/python -m evolvmem.web_server --port 9377
"""

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from evolvmem.config import Config
from evolvmem.context_models import ContextMode, ContextServiceError, parse_context_mode
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.legacy_compat import LegacyCompatibilityFacade
from evolvmem.project_store import ProjectStore, ProjectStoreError

_STATIC_INDEX = Path(__file__).parent / "web_static" / "index.html"

_SORT_COLUMNS = {
    "access_count": "access_count",
    "last_accessed": "last_accessed",
    "importance": "importance",
    "created_at": "created_at",
}
_VALID_TIERS = ("pinned", "normal", "reference")
_VALID_STATUSES = ("active", "archived", "superseded", "deleted")

# The exact column set/order of the old list query; extra projection columns
# (supersedes/superseded_by) never leak into the JSON shape.
_MEMORY_FIELDS = (
    "id", "key", "value", "attribute", "tags", "tier", "importance",
    "access_count", "last_accessed", "created_at", "updated_at",
    "expires_at", "status",
)


def _bounded_error(exc: Exception) -> str:
    """Bounded, content-free HTTP 500 surface: stable code or class name only."""
    if isinstance(exc, ContextServiceError):
        return f"context_error:{exc.code}"
    return type(exc).__name__


def _context_mode(config: Config) -> ContextMode:
    """Parse the configured mode; unknown values fail closed to legacy serving.

    mcp_server 对非法 mode 置 None 并关闭 Context 功能；Web 控制台本质是
    legacy 管理界面，按 legacy 继续服务（Core 不写不读）。
    """
    return parse_context_mode(config.context_mode) or ContextMode.LEGACY


# ---- API logic (plain functions over the facade, unit-testable) ----

def api_stats(
    facade: LegacyCompatibilityFacade, store: ContextStore | None = None
) -> dict:
    """Aggregate stats over active memories.

    Semantics preserved from the SQL version: total_active counts active and
    unexpired rows; the breakdowns scan status='active' rows (including
    not-yet-archived expired ones). When the Context store is available the
    payload also carries project-attribution counters and the pending review
    queue depth.
    """
    total_active = facade.count_active()
    rows = [
        r for r in facade.get_by_ids(facade.all_ids())
        if r["status"] == "active"
    ]

    by_tier: dict[str, int] = {}
    for r in rows:
        by_tier[r["tier"]] = by_tier.get(r["tier"], 0) + 1
    for t in _VALID_TIERS:
        by_tier.setdefault(t, 0)

    by_attribute: dict[str, int] = {}
    for r in rows:
        cat = r["attribute"] or "(未分类)"
        by_attribute[cat] = by_attribute.get(cat, 0) + 1

    never_accessed = sum(1 for r in rows if r["access_count"] == 0)

    # 分类分布：tags 里以 "分类:" 开头的标签
    by_project: dict[str, int] = {}
    for r in rows:
        if "分类:" not in (r["tags"] or ""):
            continue
        for t in r["tags"].split(","):
            t = t.strip()
            if t.startswith("分类:"):
                by_project[t] = by_project.get(t, 0) + 1

    top = sorted(
        rows,
        key=lambda r: (-(r["importance"] * (r["access_count"] + 1)), r["id"]),
    )[:10]
    top_accessed = [
        {"id": r["id"], "key": r["key"], "access_count": r["access_count"],
         "importance": r["importance"]}
        for r in top
    ]

    # 项目归属：经迁移映射把 context_items.project 挂到 legacy 行上
    by_attribution: dict[str, int] = {}
    attributed_active = 0
    pending_review = 0
    if store is not None:
        proj_map = _legacy_project_map(store)
        for r in rows:
            project = proj_map.get(r["id"], "")
            if project:
                attributed_active += 1
                by_attribution[project] = by_attribution.get(project, 0) + 1
        pending_review = _count_pending_resolutions(store)

    return {
        "total_active": total_active,
        "by_tier": by_tier,
        "by_attribute": by_attribute,
        "by_project": by_project,
        "by_attribution": by_attribution,
        "attributed_active": attributed_active,
        "unattributed_active": len(rows) - attributed_active,
        "pending_review": pending_review,
        "never_accessed": never_accessed,
        "top_accessed": top_accessed,
    }


# ---- 项目归属与审核队列 ----

_REVIEW_STATES = ("pending", "accepted", "rejected", "not_required")
_BATCH_ACCEPT_MAX = 500


def _legacy_attribution_map(store: ContextStore) -> dict[int, dict]:
    """legacy_memory_id -> attribution edit metadata via the migration mapping.

    Each entry carries the context ``item_id``, the attributed ``project``
    ("" when unattributed), and the resolution row's ``revision`` (None when
    the item has no resolution row) so the console can reassign attribution
    through the same revision-CAS accept path as the review queue.
    """
    rows = store._connection().execute(
        "SELECT m.legacy_memory_id AS legacy_id, i.id AS item_id,"
        " i.project AS project, r.revision AS resolution_revision "
        "FROM legacy_memory_migrations m "
        "JOIN context_items i ON i.id = m.context_item_id "
        "LEFT JOIN context_project_resolutions r ON r.item_id = i.id"
    ).fetchall()
    return {
        row["legacy_id"]: {
            "item_id": row["item_id"],
            "project": row["project"],
            "resolution_revision": row["resolution_revision"],
        }
        for row in rows
    }


def _legacy_project_map(store: ContextStore) -> dict[int, str]:
    """legacy_memory_id -> attributed project via the migration mapping."""
    return {
        legacy_id: meta["project"]
        for legacy_id, meta in _legacy_attribution_map(store).items()
    }


def _count_pending_resolutions(store: ContextStore) -> int:
    row = store._connection().execute(
        "SELECT COUNT(*) AS c FROM context_project_resolutions "
        "WHERE review_state='pending'"
    ).fetchone()
    return int(row["c"])


def _project_store(service: ContextService) -> ProjectStore:
    """Fresh ProjectStore borrowing the service store's connection.

    Like the operator CLI the console never resolves projects, so the
    resolver's generic-name list is satisfied with an empty tuple.
    """
    store = service.store
    return ProjectStore(
        store._connection(),
        store._require_transaction,
        generic_names=(),
    )


def api_projects(
    facade: LegacyCompatibilityFacade, store: ContextStore
) -> dict:
    """Registry projects with active-item counts plus the review queue depth.

    Counts are computed over the same facade-active, attribution-enriched row
    set the memory list shows, so a dropdown count matches the filtered list.
    Each project carries its Chinese ``display_name`` ("" when unset) and the
    registry row revision for display-name CAS edits.
    """
    registry = store._connection().execute(
        "SELECT project, status, display_name, revision "
        "FROM context_project_registry ORDER BY project"
    ).fetchall()
    proj_map = _legacy_project_map(store)
    active_rows = [
        r for r in facade.get_by_ids(facade.all_ids())
        if r["status"] == "active"
    ]
    active_counts: dict[str, int] = {}
    for r in active_rows:
        project = proj_map.get(r["id"], "")
        if project:
            active_counts[project] = active_counts.get(project, 0) + 1
    return {
        "projects": [
            {
                "project": row["project"],
                "status": row["status"],
                "display_name": row["display_name"],
                "revision": row["revision"],
                "active_items": active_counts.get(row["project"], 0),
            }
            for row in registry
        ],
        "pending_review": _count_pending_resolutions(store),
    }


def api_resolutions(
    facade: LegacyCompatibilityFacade, store: ContextStore, params: dict
) -> list[dict]:
    """List resolution review rows with a content preview for human triage.

    params: state (pending|accepted|rejected|not_required|all; default
    pending), limit (default 500, capped at 1000). Key/value come from the
    mapped legacy projection row — the shape the console user recognises —
    with the Context identity key and l0 layer as fallback for unmapped rows.
    Evidence is the resolver's bounded public projection only.
    """
    state = params.get("state", "pending")
    try:
        limit = int(params.get("limit", 500))
    except (TypeError, ValueError):
        limit = 500
    limit = max(1, min(limit, 1000))

    where = "WHERE r.review_state=?" if state in _REVIEW_STATES else ""
    args: tuple = (state,) if state in _REVIEW_STATES else ()
    rows = store._connection().execute(
        "SELECT r.item_id, r.resolution_state, r.review_state,"
        " r.proposed_project, r.resolved_project, r.confidence, r.method,"
        " r.evidence_json, r.revision, r.reviewed_at, r.created_at,"
        " i.identity_key, i.status AS item_status, i.project AS current_project,"
        " m.legacy_memory_id AS legacy_id "
        "FROM context_project_resolutions r "
        "JOIN context_items i ON i.id = r.item_id "
        "LEFT JOIN legacy_memory_migrations m ON m.context_item_id = r.item_id "
        f"{where} ORDER BY r.item_id LIMIT ?",
        (*args, limit),
    ).fetchall()

    legacy_ids = [row["legacy_id"] for row in rows if row["legacy_id"] is not None]
    legacy_by_id = {r["id"]: r for r in facade.get_by_ids(legacy_ids)}

    # 没有可用 legacy 行（未映射或已删除）的条目回退到 l0 层内容做预览
    missing = [
        row["item_id"]
        for row in rows
        if row["legacy_id"] is None
        or legacy_by_id.get(row["legacy_id"]) is None
    ]
    l0_by_item: dict[int, str] = {}
    if missing:
        marks = ",".join("?" for _ in missing)
        for layer_row in store._connection().execute(
            "SELECT item_id, content FROM context_layers "
            f"WHERE layer='l0' AND item_id IN ({marks})",
            tuple(missing),
        ).fetchall():
            l0_by_item[layer_row["item_id"]] = layer_row["content"]

    out = []
    for row in rows:
        legacy = (
            legacy_by_id.get(row["legacy_id"])
            if row["legacy_id"] is not None
            else None
        )
        try:
            evidence = json.loads(row["evidence_json"] or "[]")
        except ValueError:
            evidence = []
        out.append({
            "item_id": row["item_id"],
            "legacy_id": row["legacy_id"],
            "key": legacy["key"] if legacy else row["identity_key"],
            "value": legacy["value"] if legacy else l0_by_item.get(row["item_id"], ""),
            "item_status": row["item_status"],
            "current_project": row["current_project"],
            "resolution_state": row["resolution_state"],
            "review_state": row["review_state"],
            "proposed_project": row["proposed_project"],
            "resolved_project": row["resolved_project"],
            "confidence": row["confidence"],
            "method": row["method"],
            "evidence": evidence,
            "revision": row["revision"],
            "reviewed_at": row["reviewed_at"],
            "created_at": row["created_at"],
        })
    return out


def _parse_expected_revision(body: dict) -> int | None:
    expected = body.get("expected_revision")
    if isinstance(expected, bool) or not isinstance(expected, int) or expected < 1:
        return None
    return expected


def api_resolution_accept(
    service: ContextService, item_id: int, body: dict
) -> dict:
    """Human-accept one pending resolution into a registered project (CAS)."""
    project = str(body.get("project", "")).strip()
    expected = _parse_expected_revision(body)
    if not project:
        return {"ok": False, "error": "invalid_project"}
    if expected is None:
        return {"ok": False, "error": "invalid_revision"}
    try:
        with service.store.transaction():
            _project_store(service).accept_resolution(
                item_id, project, expected_revision=expected
            )
    except ProjectStoreError as exc:
        return {"ok": False, "error": exc.code}
    return {"ok": True, "item_id": item_id, "resolved_project": project}


def api_resolution_reject(
    service: ContextService, item_id: int, body: dict
) -> dict:
    """Human-reject one pending resolution; the item's project stays put."""
    expected = _parse_expected_revision(body)
    if expected is None:
        return {"ok": False, "error": "invalid_revision"}
    try:
        with service.store.transaction():
            _project_store(service).reject_resolution(
                item_id, expected_revision=expected
            )
    except ProjectStoreError as exc:
        return {"ok": False, "error": exc.code}
    return {"ok": True, "item_id": item_id, "review_state": "rejected"}


def api_resolutions_batch_accept(service: ContextService, body: dict) -> dict:
    """Accept many pending resolutions into one project, per-item CAS.

    Each item commits in its own transaction so one stale revision does not
    roll back neighbours; the per-item results tell the UI exactly which rows
    to keep showing as pending.
    """
    project = str(body.get("project", "")).strip()
    items = body.get("items")
    if not project:
        return {"ok": False, "error": "invalid_project"}
    if (
        not isinstance(items, list)
        or not 1 <= len(items) <= _BATCH_ACCEPT_MAX
    ):
        return {"ok": False, "error": "invalid_items"}
    parsed = []
    for entry in items:
        if not isinstance(entry, dict):
            return {"ok": False, "error": "invalid_items"}
        item_id = entry.get("item_id")
        expected = _parse_expected_revision(entry)
        if (
            isinstance(item_id, bool)
            or not isinstance(item_id, int)
            or item_id < 1
            or expected is None
        ):
            return {"ok": False, "error": "invalid_items"}
        parsed.append((item_id, expected))

    store = service.store
    known = store._connection().execute(
        "SELECT 1 FROM context_project_registry WHERE project=?", (project,)
    ).fetchone()
    if known is None:
        return {"ok": False, "error": "project_not_found"}

    results = []
    for item_id, expected in parsed:
        try:
            with store.transaction():
                _project_store(service).accept_resolution(
                    item_id, project, expected_revision=expected
                )
            results.append({"item_id": item_id, "ok": True})
        except ProjectStoreError as exc:
            results.append(
                {"item_id": item_id, "ok": False, "error": exc.code}
            )
    accepted = sum(1 for r in results if r["ok"])
    return {
        "ok": True,
        "project": project,
        "accepted": accepted,
        "failed": len(results) - accepted,
        "results": results,
    }


def _valid_display_name(name: object) -> bool:
    """Display labels: any printable text up to 32 chars, no control chars."""
    return (
        isinstance(name, str)
        and len(name) <= 32
        and not any(ord(c) < 32 for c in name)
    )


def api_project_register(service: ContextService, body: dict) -> dict:
    """Register a project from the review UI (idempotent)."""
    name = str(body.get("name", "")).strip()
    if not name or len(name) > 64 or any(ord(c) < 32 for c in name):
        return {"ok": False, "error": "invalid_project"}
    display_name = str(body.get("display_name", "")).strip()
    if not _valid_display_name(display_name):
        return {"ok": False, "error": "invalid_display_name"}
    with service.store.transaction():
        _project_store(service).register_project(name, display_name)
    return {"ok": True, "project": name, "display_name": display_name}


def api_project_set_display_name(service: ContextService, body: dict) -> dict:
    """Set or clear a project's Chinese display name (revision CAS)."""
    project = str(body.get("project", "")).strip()
    display_name = str(body.get("display_name", "")).strip()
    expected = _parse_expected_revision(body)
    if not project:
        return {"ok": False, "error": "invalid_project"}
    if not _valid_display_name(display_name):
        return {"ok": False, "error": "invalid_display_name"}
    if expected is None:
        return {"ok": False, "error": "invalid_revision"}
    try:
        with service.store.transaction():
            _project_store(service).set_display_name(
                project, display_name, expected_revision=expected
            )
    except ProjectStoreError as exc:
        return {"ok": False, "error": exc.code}
    return {"ok": True, "project": project, "display_name": display_name}


def api_memories(
    facade: LegacyCompatibilityFacade,
    params: dict,
    store: ContextStore | None = None,
) -> list[dict]:
    """List memories with filtering and sorting.

    params keys (from query string): status, tier, attribute, project, q,
    sort, order, plus attribution (project attribution filter; ``__none__``
    selects unattributed rows). Every row carries a ``project`` field with
    its Context-side attribution when the store is available.

    Reads go through the facade, whose surface excludes deleted rows:
    status='deleted'/'all' therefore no longer list soft-deleted memories.
    """
    status = params.get("status", "active")
    tier = params.get("tier", "")
    attribute = params.get("attribute", "")
    project = params.get("project", "").strip()
    attribution = params.get("attribution", "").strip()
    q = params.get("q", "").strip()
    sort = _SORT_COLUMNS.get(params.get("sort", "access_count"),
                             "access_count")
    desc = params.get("order", "desc").lower() != "asc"

    rows = facade.get_by_ids(facade.all_ids())
    if status in _VALID_STATUSES:
        rows = [r for r in rows if r["status"] == status]
    elif status != "all":
        rows = [r for r in rows if r["status"] == "active"]
    if tier in _VALID_TIERS:
        rows = [r for r in rows if r["tier"] == tier]
    if attribute:
        rows = [r for r in rows if r["attribute"] == attribute]
    if project:
        # tags 是逗号拼接串，用 ",tags," 形式精确匹配单个标签
        needle = f",{project},"
        rows = [r for r in rows if needle in f",{r['tags'] or ''},"]
    if q:
        # 恢复 SQL LIKE 时代的大小写不敏感语义
        needle = q.casefold()
        rows = [
            r for r in rows
            if needle in r["key"].casefold() or needle in r["value"].casefold()
        ]

    attr_map = _legacy_attribution_map(store) if store is not None else {}
    proj_of = {legacy_id: meta["project"] for legacy_id, meta in attr_map.items()}
    if attribution == "__none__":
        rows = [r for r in rows if not proj_of.get(r["id"], "")]
    elif attribution:
        rows = [r for r in rows if proj_of.get(r["id"], "") == attribution]

    rows = _sort_rows(rows, sort, desc)
    return [
        {**{field: r.get(field) for field in _MEMORY_FIELDS},
         "project": proj_of.get(r["id"], ""),
         "item_id": attr_map.get(r["id"], {}).get("item_id"),
         "resolution_revision": attr_map.get(r["id"], {}).get(
             "resolution_revision")}
        for r in rows[:500]
    ]


def _sort_rows(rows: list[dict], sort: str, desc: bool) -> list[dict]:
    """Mirror `ORDER BY {sort} IS NULL, {sort} {order}, id {order}` — NULLs last."""
    present = [r for r in rows if r[sort] is not None]
    missing = [r for r in rows if r[sort] is None]
    present.sort(key=lambda r: (r[sort], r["id"]), reverse=desc)
    missing.sort(key=lambda r: r["id"], reverse=desc)
    return present + missing


def api_update(facade: LegacyCompatibilityFacade, mem_id: int, body: dict) -> dict:
    """Update importance/tier/attribute/tags in place through the typed facade.

    All four fields share the service's single transaction, which also mirrors
    the edit onto the mapped ContextItem (content_type/scope/tags re-derive
    from the new projection row through the migrator's public policy). The
    legacy row keeps its id/history; the HTTP request/response shape is
    unchanged.
    """
    if facade.get_by_id(mem_id) is None:
        return {"ok": False, "error": "not found"}

    importance = body.get("importance")
    tier = body.get("tier")
    if importance is not None:
        importance = float(importance)
        if not 0 <= importance <= 10:
            return {"ok": False, "error": "importance must be 0-10"}
    if tier is not None and tier not in _VALID_TIERS:
        return {"ok": False, "error": "invalid tier"}

    # Web 侧把 JSON 形状换算成类型化边界形状（legacy_models 的边界只进不出）
    attribute = str(body["attribute"]) if "attribute" in body else None
    tags = None
    if "tags" in body:
        raw_tags = body["tags"]
        if isinstance(raw_tags, list):
            tags = tuple(str(tag) for tag in raw_tags)
        else:
            tags = tuple(str(raw_tags).split(","))

    if (
        importance is not None
        or tier is not None
        or attribute is not None
        or tags is not None
    ):
        facade.update_metadata(
            mem_id,
            importance=importance,
            tier=tier,
            attribute=attribute,
            tags=tags,
        )

    return {"ok": True, "memory": facade.get_by_id(mem_id)}


def api_archive(facade: LegacyCompatibilityFacade, mem_id: int) -> dict:
    if facade.get_by_id(mem_id) is None:
        return {"ok": False, "error": "not found"}
    facade.archive(mem_id)
    return {"ok": True}


def api_restore(facade: LegacyCompatibilityFacade, mem_id: int) -> dict:
    """archived -> active."""
    if facade.get_by_id(mem_id) is None:
        return {"ok": False, "error": "not found"}
    facade.restore(mem_id)
    return {"ok": True}


def api_delete(facade: LegacyCompatibilityFacade, mem_id: int) -> dict:
    """Soft delete (status -> deleted)."""
    if facade.get_by_id(mem_id) is None:
        return {"ok": False, "error": "not found"}
    facade.remove(mem_id)
    return {"ok": True}


def api_hard_delete(facade: LegacyCompatibilityFacade, mem_id: int) -> dict:
    """Physically remove the exact mapping/projection/ContextItem triple —
    irreversible. Only this explicit console action reaches hard delete;
    forgetting/consolidation never trigger it. The stale vector entry is
    dropped on the next index consistency rebuild.
    """
    if facade.get_by_id(mem_id) is None:
        return {"ok": False, "error": "not found"}
    facade.hard_delete(mem_id)
    return {"ok": True}


# ---- HTTP layer ----

_MEM_ACTION_RE = re.compile(r"^/api/memory/(\d+)/(update|archive|restore|delete|hard_delete)$")
_RES_ACTION_RE = re.compile(r"^/api/resolution/(\d+)/(accept|reject)$")

_BODY_LIMIT = 64 * 1024


def make_handler(service: ContextService):
    """Build the handler owning a ContextService compatibility facade."""
    facade = service.legacy_facade()
    store = service.store

    class MemoryWebHandler(BaseHTTPRequestHandler):
        server_version = "EvolvMemWeb/1.0"

        def _send_json(self, payload, status=200):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _send_html(self, html: str, status=200):
            data = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _read_body(self):
            """Parse a bounded JSON body; None means no body was sent.

            A failure response is already sent when the second element is
            True, so callers just ``return``.
            """
            length = int(self.headers.get("Content-Length") or 0)
            if length > _BODY_LIMIT:
                self._send_json({"ok": False, "error": "body too large"}, 413)
                return None, True
            if not length:
                return {}, False
            try:
                return json.loads(self.rfile.read(length).decode("utf-8")), False
            except (ValueError, UnicodeDecodeError):
                self._send_json({"ok": False, "error": "invalid JSON"}, 400)
                return None, True

        def log_message(self, fmt, *args):  # keep console quiet
            pass

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/":
                try:
                    self._send_html(
                        _STATIC_INDEX.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    self._send_json({"ok": False,
                                     "error": "index.html missing"}, 404)
                return
            if path == "/api/stats":
                self._send_json(api_stats(facade, store))
                return
            if path == "/api/memories":
                qs = parse_qs(parsed.query)
                params = {k: v[0] for k, v in qs.items()}
                self._send_json(api_memories(facade, params, store))
                return
            if path == "/api/projects":
                self._send_json(api_projects(facade, store))
                return
            if path == "/api/resolutions":
                qs = parse_qs(parsed.query)
                params = {k: v[0] for k, v in qs.items()}
                self._send_json(api_resolutions(facade, store, params))
                return
            self._send_json({"ok": False, "error": "unknown endpoint"}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            m = _MEM_ACTION_RE.match(path)
            if m:
                self._handle_memory_action(int(m.group(1)), m.group(2))
                return
            m = _RES_ACTION_RE.match(path)
            if m:
                self._handle_resolution_action(int(m.group(1)), m.group(2))
                return
            if path in ("/api/resolutions/batch_accept",
                        "/api/projects/register",
                        "/api/projects/display_name"):
                body, failed = self._read_body()
                if failed:
                    return
                try:
                    if path == "/api/resolutions/batch_accept":
                        result = api_resolutions_batch_accept(service, body)
                    elif path == "/api/projects/register":
                        result = api_project_register(service, body)
                    else:
                        result = api_project_set_display_name(service, body)
                except Exception as exc:  # bounded surface; writes roll back
                    self._send_json({"ok": False, "error": _bounded_error(exc)},
                                    500)
                    return
                self._send_json(result, 200 if result.get("ok") else 400)
                return
            self._send_json({"ok": False,
                             "error": "unknown endpoint"}, 404)

        def _handle_memory_action(self, mem_id: int, action: str):
            body = {}
            if action == "update":
                body, failed = self._read_body()
                if failed:
                    return

            try:
                if action == "update":
                    result = api_update(facade, mem_id, body)
                elif action == "archive":
                    result = api_archive(facade, mem_id)
                elif action == "restore":
                    result = api_restore(facade, mem_id)
                elif action == "hard_delete":
                    result = api_hard_delete(facade, mem_id)
                else:
                    result = api_delete(facade, mem_id)
            except Exception as exc:  # bounded surface; writes roll back whole
                self._send_json({"ok": False, "error": _bounded_error(exc)},
                                500)
                return
            self._send_json(result,
                            200 if result.get("ok") else 400)

        def _handle_resolution_action(self, item_id: int, action: str):
            body, failed = self._read_body()
            if failed:
                return
            try:
                if action == "accept":
                    result = api_resolution_accept(service, item_id, body)
                else:
                    result = api_resolution_reject(service, item_id, body)
            except Exception as exc:  # bounded surface; writes roll back whole
                self._send_json({"ok": False, "error": _bounded_error(exc)},
                                500)
                return
            self._send_json(result,
                            200 if result.get("ok") else 400)

    return MemoryWebHandler


def run(port: int = 9377, data_dir: str | None = None,
        host: str = "127.0.0.1") -> None:
    config = Config.from_file()
    if data_dir:
        config.data_dir = Path(data_dir)
    # 生产写入口：ContextService（按配置 mode，非法值 fail-closed 到 legacy）
    # + 兼容门面；legacy 投影 schema 经服务自有后端幂等引导（与 mcp_server
    # 相同），本模块不再持有裸 MemoryStore
    service = ContextService(config)
    service.initialize(mode=_context_mode(config), adapter=config.adapter or "web")
    service._legacy_backend()
    # 单线程服务：sqlite 连接不支持跨线程使用；本地单用户控制台无需并发
    server = HTTPServer((host, port), make_handler(service))
    print(f"EvolvMem web console: http://{host}:{port} "
          f"(data: {config.db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="EvolvMem web console")
    parser.add_argument("--port", type=int, default=9377)
    parser.add_argument("--host", default="127.0.0.1",
                        help="bind address (default: 127.0.0.1; use 0.0.0.0 for LAN)")
    parser.add_argument("--data-dir", default=None,
                        help="override data dir (default: ~/.claude/evolvmem)")
    args = parser.parse_args()
    run(port=args.port, data_dir=args.data_dir, host=args.host)


if __name__ == "__main__":
    main()

"""Local web memory management console: stdlib-only HTTP server + JSON API.

Serves a single-page UI (evolvmem/web_static/index.html) and a JSON API on
top of a ContextService + legacy compatibility facade — no third-party
dependencies. Every lifecycle mutation (importance/tier/attribute/tags
update, archive, restore, soft delete, hard delete) routes through the typed
facade so compat/shadow/primary modes mirror it onto the mapped Context side
in one transaction; list/read keep returning legacy rows through the facade.

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
from evolvmem.legacy_compat import LegacyCompatibilityFacade

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

def api_stats(facade: LegacyCompatibilityFacade) -> dict:
    """Aggregate stats over active memories.

    Semantics preserved from the SQL version: total_active counts active and
    unexpired rows; the breakdowns scan status='active' rows (including
    not-yet-archived expired ones).
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

    return {
        "total_active": total_active,
        "by_tier": by_tier,
        "by_attribute": by_attribute,
        "by_project": by_project,
        "never_accessed": never_accessed,
        "top_accessed": top_accessed,
    }


def api_memories(facade: LegacyCompatibilityFacade, params: dict) -> list[dict]:
    """List memories with filtering and sorting.

    params keys (from query string): status, tier, attribute, project, q, sort, order.

    Reads go through the facade, whose surface excludes deleted rows:
    status='deleted'/'all' therefore no longer list soft-deleted memories.
    """
    status = params.get("status", "active")
    tier = params.get("tier", "")
    attribute = params.get("attribute", "")
    project = params.get("project", "").strip()
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

    rows = _sort_rows(rows, sort, desc)
    return [{field: r.get(field) for field in _MEMORY_FIELDS}
            for r in rows[:500]]


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


def make_handler(service: ContextService):
    """Build the handler owning a ContextService compatibility facade."""
    facade = service.legacy_facade()

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
                self._send_json(api_stats(facade))
                return
            if path == "/api/memories":
                qs = parse_qs(parsed.query)
                params = {k: v[0] for k, v in qs.items()}
                self._send_json(api_memories(facade, params))
                return
            self._send_json({"ok": False, "error": "unknown endpoint"}, 404)

        def do_POST(self):
            m = _MEM_ACTION_RE.match(urlparse(self.path).path)
            if not m:
                self._send_json({"ok": False,
                                 "error": "unknown endpoint"}, 404)
                return
            mem_id, action = int(m.group(1)), m.group(2)
            body = {}
            if action == "update":
                length = int(self.headers.get("Content-Length") or 0)
                if length > 64 * 1024:
                    self._send_json({"ok": False,
                                     "error": "body too large"}, 413)
                    return
                if length:
                    try:
                        body = json.loads(
                            self.rfile.read(length).decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        self._send_json({"ok": False,
                                         "error": "invalid JSON"}, 400)
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

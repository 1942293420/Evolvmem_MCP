"""Local web memory management console: stdlib-only HTTP server + JSON API.

Serves a single-page UI (evolvmem/web_static/index.html) and a JSON API on
top of the existing MemoryStore — no third-party dependencies.

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
from evolvmem.memory_store import MemoryStore, _now_iso

_STATIC_INDEX = Path(__file__).parent / "web_static" / "index.html"

_SORT_COLUMNS = {
    "access_count": "access_count",
    "last_accessed": "last_accessed",
    "importance": "importance",
    "created_at": "created_at",
}
_VALID_TIERS = ("pinned", "normal", "reference")
_VALID_STATUSES = ("active", "archived", "superseded", "deleted")


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---- API logic (plain functions over a MemoryStore, unit-testable) ----

def api_stats(store: MemoryStore) -> dict:
    """Aggregate stats over active memories."""
    total_active = store.count_active()

    by_tier: dict[str, int] = {}
    for r in store._execute(
        "SELECT tier, COUNT(*) AS cnt FROM memories "
        "WHERE status='active' GROUP BY tier"
    ):
        by_tier[r["tier"]] = r["cnt"]
    for t in _VALID_TIERS:
        by_tier.setdefault(t, 0)

    by_attribute: dict[str, int] = {}
    for r in store._execute(
        "SELECT COALESCE(NULLIF(attribute, ''), '(未分类)') AS cat, "
        "COUNT(*) AS cnt FROM memories WHERE status='active' GROUP BY cat"
    ):
        by_attribute[r["cat"]] = r["cnt"]

    never_accessed = store._execute(
        "SELECT COUNT(*) AS cnt FROM memories "
        "WHERE status='active' AND access_count = 0"
    )[0]["cnt"]

    # 分类分布：tags 里以 "分类:" 开头的标签
    by_project: dict[str, int] = {}
    for r in store._execute(
        "SELECT tags FROM memories WHERE status='active' "
        "AND tags LIKE '%分类:%'"
    ):
        for t in (r["tags"] or "").split(","):
            t = t.strip()
            if t.startswith("分类:"):
                by_project[t] = by_project.get(t, 0) + 1

    top_accessed = [
        {"id": r["id"], "key": r["key"], "access_count": r["access_count"]}
        for r in store._execute(
            "SELECT id, key, access_count FROM memories "
            "WHERE status='active' ORDER BY access_count DESC, id ASC LIMIT 10"
        )
    ]

    return {
        "total_active": total_active,
        "by_tier": by_tier,
        "by_attribute": by_attribute,
        "by_project": by_project,
        "never_accessed": never_accessed,
        "top_accessed": top_accessed,
    }


def api_memories(store: MemoryStore, params: dict) -> list[dict]:
    """List memories with filtering and sorting.

    params keys (from query string): status, tier, attribute, project, q, sort, order.
    """
    status = params.get("status", "active")
    tier = params.get("tier", "")
    attribute = params.get("attribute", "")
    project = params.get("project", "").strip()
    q = params.get("q", "").strip()
    sort = _SORT_COLUMNS.get(params.get("sort", "access_count"),
                             "access_count")
    order = "ASC" if params.get("order", "desc").lower() == "asc" else "DESC"

    where, args = [], []
    if status in _VALID_STATUSES:
        where.append("status = ?")
        args.append(status)
    elif status != "all":
        where.append("status = 'active'")
    if tier in _VALID_TIERS:
        where.append("tier = ?")
        args.append(tier)
    if attribute:
        where.append("attribute = ?")
        args.append(attribute)
    if project:
        # tags 是逗号拼接串，用 ",tags," 形式精确匹配单个标签
        where.append("(',' || tags || ',') LIKE ? ESCAPE '\\'")
        args.append(f"%,{_escape_like(project)},%")
    if q:
        pattern = f"%{_escape_like(q)}%"
        where.append("(key LIKE ? ESCAPE '\\' OR value LIKE ? ESCAPE '\\')")
        args.extend([pattern, pattern])

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    # NULLs last for both directions
    order_sql = f"ORDER BY {sort} IS NULL, {sort} {order}, id {order}"

    rows = store._execute(
        "SELECT id, key, value, attribute, tags, tier, importance, "
        "access_count, last_accessed, created_at, updated_at, expires_at, "
        "status FROM memories "
        f"{where_sql} {order_sql} LIMIT 500",
        tuple(args),
    )
    return [dict(r) for r in rows]


def api_update(store: MemoryStore, mem_id: int, body: dict) -> dict:
    """Update importance/tier via update_metadata; attribute/tags via SQL."""
    if store.get_by_id(mem_id) is None:
        return {"ok": False, "error": "not found"}

    importance = body.get("importance")
    tier = body.get("tier")
    if importance is not None:
        importance = float(importance)
        if not 0 <= importance <= 10:
            return {"ok": False, "error": "importance must be 0-10"}
    if tier is not None and tier not in _VALID_TIERS:
        return {"ok": False, "error": "invalid tier"}
    if importance is not None or tier is not None:
        store.update_metadata(mem_id, importance=importance, tier=tier)

    sets, args = [], []
    if "attribute" in body:
        sets.append("attribute = ?")
        args.append(str(body["attribute"]))
    if "tags" in body:
        tags = body["tags"]
        if isinstance(tags, list):
            tags = ",".join(str(t) for t in tags)
        sets.append("tags = ?")
        args.append(str(tags))
    if sets:
        sets.append("updated_at = ?")
        args.append(_now_iso())
        args.append(mem_id)
        store._execute(
            f"UPDATE memories SET {', '.join(sets)} WHERE id = ?",
            tuple(args),
        )
        store._conn.commit()

    return {"ok": True, "memory": store.get_by_id(mem_id)}


def api_archive(store: MemoryStore, mem_id: int) -> dict:
    if store.get_by_id(mem_id) is None:
        return {"ok": False, "error": "not found"}
    store.archive(mem_id)
    return {"ok": True}


def api_restore(store: MemoryStore, mem_id: int) -> dict:
    """archived -> active."""
    if store.get_by_id(mem_id) is None:
        return {"ok": False, "error": "not found"}
    store._execute(
        "UPDATE memories SET status='active', updated_at=? WHERE id=?",
        (_now_iso(), mem_id),
    )
    store._conn.commit()
    return {"ok": True}


def api_delete(store: MemoryStore, mem_id: int) -> dict:
    """Soft delete (status -> deleted)."""
    if store.get_by_id(mem_id) is None:
        return {"ok": False, "error": "not found"}
    store.remove(mem_id)
    return {"ok": True}


# ---- HTTP layer ----

_MEM_ACTION_RE = re.compile(r"^/api/memory/(\d+)/(update|archive|restore|delete)$")


def make_handler(store: MemoryStore):
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
                self._send_json(api_stats(store))
                return
            if path == "/api/memories":
                qs = parse_qs(parsed.query)
                params = {k: v[0] for k, v in qs.items()}
                self._send_json(api_memories(store, params))
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
                    result = api_update(store, mem_id, body)
                elif action == "archive":
                    result = api_archive(store, mem_id)
                elif action == "restore":
                    result = api_restore(store, mem_id)
                else:
                    result = api_delete(store, mem_id)
            except Exception as exc:  # surface store errors as JSON
                self._send_json({"ok": False, "error": str(exc)}, 500)
                return
            self._send_json(result,
                            200 if result.get("ok") else 400)

    return MemoryWebHandler


def run(port: int = 9377, data_dir: str | None = None,
        host: str = "127.0.0.1") -> None:
    config = Config.from_file()
    if data_dir:
        config.data_dir = Path(data_dir)
    store = MemoryStore(config)
    store.initialize()
    # 单线程服务：sqlite 连接不支持跨线程使用；本地单用户控制台无需并发
    server = HTTPServer((host, port), make_handler(store))
    print(f"EvolvMem web console: http://{host}:{port} "
          f"(data: {config.db_path})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


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

"""HTTP API & View dispatchers routing history console requests to PostgreSQL."""
from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any

from evolvmem.history_pg.candidate_reader import PgHistoryCandidateReader
from evolvmem.history_pg.config import PgHistoryConfig, load_pg_history_config

_PROJ_RE = re.compile(r"^/api/history-candidate/project/([^/]+)/?$")
_BLK_RE = re.compile(r"^/api/history-candidate/block/([^/]+)/?$")
_BLK_TURNS_RE = re.compile(r"^/api/history-candidate/block/([^/]+)/turns/?$")
_BLK_CTX_RE = re.compile(r"^/api/history-candidate/block/([^/]+)/context/?$")
_COV_RE = re.compile(r"^/api/history-candidate/coverage/([^/]+)/?$")
_ATT_RE = re.compile(r"^/history/attachment/([0-9a-f]{16,128})/?$")


def _reader_from_env(environ: dict[str, Any] | None, pg_config: PgHistoryConfig | None) -> PgHistoryCandidateReader:
    env = dict(environ or {})
    cfg = pg_config if pg_config is not None else load_pg_history_config()
    dbname = str(env.get("EVOLVMEM_HISTORY_PG_DB") or "").strip() or None
    owner = str(env.get("EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE") or "jiangli")
    user_id = "jiangli" if owner == "personal" else owner
    return PgHistoryCandidateReader(cfg, dbname=dbname, user_id=user_id)


def dispatch_pg_history_api(
    method: str,
    path: str,
    params: dict[str, Any],
    headers: dict[str, Any],
    environ: dict[str, Any] | None = None,
    owner_verified: bool = False,
    pg_config: PgHistoryConfig | None = None,
) -> tuple[int, dict[str, Any]]:
    clean = path.rstrip("/") or "/"
    upper = str(method).upper()
    with _reader_from_env(environ, pg_config) as reader:
        if upper == "POST" and clean in ("/api/history-candidate/management/delete", "/api/history-candidate/management/restore"):
            if not owner_verified or headers.get("X-EvolvMem-Action") != "history-manage":
                return 403, {"ok": False, "error": "history_manage_forbidden"}
            act = "delete" if clean.endswith("/delete") else "restore"
            res = reader.manage(
                act,
                kind=str(params.get("kind") or "block"),
                project=str(params.get("project") or ""),
                key=str(params.get("key") or ""),
                title=str(params.get("title") or ""),
                request_id=str(params.get("request_id") or ""),
            )
            return 200, res

        if upper != "GET":
            return 405, {"ok": False, "error": "method_not_allowed"}

        if clean == "/api/history-candidate/projects":
            return 200, reader.projects()
        if clean == "/api/history-candidate/unassigned":
            return 200, reader.unassigned()
        if clean == "/api/history-candidate/management/trash":
            return 200, reader.manage("trash")
        if clean == "/api/history-candidate/search":
            q = str(params.get("q") or params.get("query") or "")
            proj = str(params.get("project") or "") or None
            limit = int(params.get("limit") or 20)
            items = reader.hybrid_search(q, project=proj, limit=limit)
            return 200, {"ok": True, "items": items, "total": len(items), "query": q}
        if m := _PROJ_RE.match(clean):
            res = reader.project_overview(m.group(1))
            return (200 if res.get("ok") else 404), res
        if m := _BLK_TURNS_RE.match(clean):
            off = int(params.get("offset") or 0)
            lim = int(params.get("limit") or 50)
            res = reader.block_turn_page(m.group(1), offset=off, limit=lim)
            return (200 if res.get("ok") else 404), res
        if m := _BLK_CTX_RE.match(clean):
            res = reader.block_page(m.group(1))
            return (200 if res.get("ok") else 404), res
        if m := _BLK_RE.match(clean):
            res = reader.block_page(m.group(1))
            return (200 if res.get("ok") else 404), res
        if m := _COV_RE.match(clean):
            key = "gap_audit" if m.group(1) == "summary" else m.group(1)
            res = reader.coverage_report(key)
            return (200 if res.get("ok") else 404), res
    return 404, {"ok": False, "error": "not_found"}


def handle_pg_history_view(
    method: str,
    path: str,
    params: dict[str, Any],
    *,
    owner_verified: bool,
    auth_enabled: bool = True,
    environ: dict[str, Any] | None = None,
    pg_config: PgHistoryConfig | None = None,
) -> tuple[int, str, Any]:
    if auth_enabled and not owner_verified:
        return 403, "application/json", {"ok": False, "error": "forbidden"}
    clean = path.rstrip("/") or "/"
    if m := _ATT_RE.match(clean):
        with _reader_from_env(environ, pg_config) as reader:
            meta = reader.attachment(m.group(1))
        if not meta or not meta.get("path") or not Path(meta["path"]).is_file():
            return 404, "text/plain", b"attachment_not_found"
        raw = Path(meta["path"]).read_bytes()
        return 200, str(meta["mime_type"]), raw
    status, payload = dispatch_pg_history_api(
        method, "/api/history-candidate" + clean.removeprefix("/history"),
        params, {}, environ=environ, owner_verified=owner_verified, pg_config=pg_config,
    )
    return status, "application/json", json.dumps(payload, ensure_ascii=False)

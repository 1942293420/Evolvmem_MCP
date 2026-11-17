"""HTTP API & View dispatchers routing history console requests to PostgreSQL."""
from __future__ import annotations

import hmac
import json
import os
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


def _read_token_file(path: Path | None) -> str:
    if path is None or not path.is_file():
        return ""
    try:
        if path.stat().st_size > 4096:
            return ""
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _authorize_personal_get(hdr_lc: dict[str, Any], environ: dict[str, Any] | None) -> tuple[int, str] | None:
    env = os.environ if environ is None else environ
    token_path_str = str(env.get("EVOLVMEM_HISTORY_CANDIDATE_TOKEN_FILE") or "").strip()
    if not token_path_str:
        return 403, "history_forbidden"
    expected = _read_token_file(Path(token_path_str).expanduser())
    supplied = hdr_lc.get("x-evolvmem-history-token")
    if not expected or not supplied or not hmac.compare_digest(str(supplied), expected):
        return 401, "history_candidate_unauthorized"
    expected_ns = str(env.get("EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE") or "personal").strip() or "personal"
    supplied_ns = hdr_lc.get("x-evolvmem-namespace")
    if not supplied_ns or not hmac.compare_digest(str(supplied_ns), expected_ns):
        return 403, "history_candidate_namespace_mismatch"
    return None


def _reader_from_env(environ: dict[str, Any] | None, pg_config: PgHistoryConfig | None, *, grouping: str = '') -> PgHistoryCandidateReader:
    env = os.environ if environ is None else environ
    cfg = pg_config if pg_config is not None else load_pg_history_config()
    dbname = str(env.get("EVOLVMEM_HISTORY_PG_DB") or "").strip() or None
    ns = str(env.get("EVOLVMEM_HISTORY_CANDIDATE_NAMESPACE") or "").strip()
    owner_uid = str(env.get("EVOLVMEM_CONSOLE_OWNER_ID") or os.environ.get("EVOLVMEM_CONSOLE_OWNER_ID") or "").strip()
    if dbname:
        user_id = owner_uid or ("" if ns in ("", "personal") else ns)
    elif ns == "personal":
        if not owner_uid:
            raise ValueError("missing_console_owner_id")
        user_id = owner_uid
    elif ns:
        user_id = ns
    else:
        raise ValueError("missing_history_pg_target")
    att_dir = str(env.get("EVOLVMEM_HISTORY_ATTACHMENTS_DIR") or os.environ.get("EVOLVMEM_HISTORY_ATTACHMENTS_DIR") or "").strip() or None
    reader_class = PgHistoryCandidateReader
    if grouping == 'workspace':
        from evolvmem.history_pg.workspace_views import WorkspaceHistoryReader
        reader_class = WorkspaceHistoryReader
    return reader_class(cfg, dbname=dbname, user_id=user_id, attachments_dir=att_dir)


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
    hdr_lc = {str(k).lower(): v for k, v in (headers or {}).items()}
    manage_paths = ("/api/history-candidate/management/delete", "/api/history-candidate/management/restore")

    if upper == "POST":
        if clean not in manage_paths:
            return 405, {"ok": False, "error": "method_not_allowed"}
        if not owner_verified or str(hdr_lc.get("x-evolvmem-action") or "") != "history-manage":
            return 403, {"ok": False, "error": "history_manage_forbidden"}
    elif upper == "GET":
        if not owner_verified:
            auth_err = _authorize_personal_get(hdr_lc, environ)
            if auth_err is not None:
                return auth_err[0], {"ok": False, "error": auth_err[1]}
    else:
        return 405, {"ok": False, "error": "method_not_allowed"}

    try:
        grouping = str(params.get('grouping') or '')
        if (upper == 'POST' and params.get('kind') == 'workspace') or clean == '/api/history-candidate/management/trash' or _BLK_TURNS_RE.match(clean):
            grouping = 'workspace'
        if grouping not in ('', 'workspace'):
            return 400, {'ok': False, 'error': 'invalid_history_grouping'}
        with _reader_from_env(environ, pg_config, grouping=grouping) as reader:
            if upper == "POST":
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
                extra = {'scope': str(params.get('scope') or '')} if grouping == 'workspace' else {}
                items = reader.hybrid_search(q, project=proj, limit=limit, **extra)
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
    except ValueError as exc:
        msg = str(exc)
        status = 409 if "conflict" in msg.lower() else 400
        return status, {"ok": False, "error": msg}
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
    del auth_enabled
    if not owner_verified:
        return 403, "application/json", json.dumps({"ok": False, "error": "forbidden"}, ensure_ascii=False)
    clean = path.rstrip("/") or "/"
    if m := _ATT_RE.match(clean):
        try:
            with _reader_from_env(environ, pg_config) as reader:
                meta = reader.attachment(m.group(1))
        except ValueError as exc:
            msg = str(exc)
            status = 409 if "conflict" in msg.lower() else 400
            return status, "application/json", json.dumps({"ok": False, "error": msg}, ensure_ascii=False)
        if not meta or not meta.get("path") or not Path(meta["path"]).is_file():
            return 404, "text/plain", b"attachment_not_found"
        raw = Path(meta["path"]).read_bytes()
        return 200, str(meta["mime_type"]), raw
    subpath = clean.removeprefix("/history")
    if subpath in ("", "/"):
        subpath = "/projects"
    status, payload = dispatch_pg_history_api(
        method, "/api/history-candidate" + subpath,
        params, {}, environ=environ, owner_verified=owner_verified, pg_config=pg_config,
    )
    return status, "application/json", json.dumps(payload, ensure_ascii=False)

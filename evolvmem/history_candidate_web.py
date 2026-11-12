"""Token-gated HTTP surface for the read-only history candidate.

Wired into the existing web console (``evolvmem.web_server``) under
``/api/history-candidate/...``. The surface is additive and fail-closed:

* disabled unless both the candidate DB path and the token file are configured;
* every request must carry the personal token and namespace headers;
* only GET is accepted;
* no write path exists in the reader (``mode=ro`` + deny-write authorizer).

Nothing here changes the existing project-recall, knowledge or memory routes.
Removing the two environment variables and restarting the web console disables
the surface and restores the previous behaviour exactly.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from urllib.parse import unquote

from evolvmem.history_candidate_read import (
    HistoryCandidateError,
    authorize,
    open_reader,
)

PREFIX = "/api/history-candidate"
_PROJECT = re.compile(re.escape(PREFIX) + r"/project/([^/]+)/?")
_BLOCK = re.compile(re.escape(PREFIX) + r"/block/([^/]+)/?")
_TURNS = re.compile(re.escape(PREFIX) + r"/block/([^/]+)/turns/?")
_CONTEXT = re.compile(re.escape(PREFIX) + r"/block/([^/]+)/context/?")
_COVERAGE_SCHEMA = "evolvmem.gap_audit.v1"
_COVERAGE_SOURCE = re.compile(re.escape(PREFIX) + r"/coverage/source/([^/]+)/?")
_DEFAULT_ACCEPTED_AUDIT = ''
_AUDIT_STATUS_LABELS = {
    "found_original": "已找到原文，待接入",
    "found_original_partial": "找到部分原文",
    "linked_elsewhere": "原文已在其他项目",
    "not_found_in_checked_scope": "已查范围内未找到",
    "unable": "尚无法核实",
    "unresolved": "尚无法核实",
}
_AUDIT_PROJECT = re.compile(re.escape(PREFIX) + r"/coverage/audit/(?:project|projects)/([^/]+)/?")
_AUDIT_SOURCE = re.compile(re.escape(PREFIX) + r"/coverage/audit/(?:source|sources)/([^/]+)/?")


def _status_label(status):
    return _AUDIT_STATUS_LABELS.get(str(status or "").casefold(), "尚无法核实")


def _accepted_audit(environ):
    """Read only the root-accepted audit file at request time; never raw drafts."""
    value = (environ or os.environ).get("EVOLVMEM_HISTORY_ACCEPTED_AUDIT_FILE", _DEFAULT_ACCEPTED_AUDIT)
    path = pathlib.Path(value)
    base = {"status": "pending_acceptance", "message": "备份核查结果待验收",
            "items": {}, "project_items": {}, "block_items": {}, "summary": {},
            "scope": None, "limitations": [], "checked_at": None}
    if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
        return base
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {**base, "status": "invalid", "message": "备份核查结果格式无效"}
    if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
        return {**base, "status": "invalid", "message": "备份核查结果格式无效"}
    items = []
    project_items = {}
    block_items = {}
    for item in raw["items"]:
        if not isinstance(item, dict):
            continue
        identity = item.get("identity") if isinstance(item.get("identity"), dict) else {}
        project = identity.get("project")
        block_id = identity.get("block_id")
        if project:
            project_items[str(project)] = item
        if block_id:
            block_items[str(block_id)] = item
        items.append(item)
    counts = {}
    for item in items:
        key = str(item.get("status") or "unresolved")
        counts[key] = counts.get(key, 0) + 1
    return {
        "status": "accepted",
        "message": "备份核查结果已验收",
        "generated_at_local": raw.get("generated_at_local"),
        "checked_at": raw.get("generated_at_local") or raw.get("checked_at"),
        "summary": raw.get("summary") if isinstance(raw.get("summary"), dict) else {},
        "status_counts": counts,
        "item_count": len(items),
        "scope": raw.get("scope"),
        "limitations": raw.get("limitations") if isinstance(raw.get("limitations"), list) else [],
        "items": {str(i): item for i, item in enumerate(items)},
        "project_items": project_items,
        "block_items": block_items,
    }


def _audit_row_context(ctx, kind, key):
    item = (ctx.get("project_items") if kind == "projects" else ctx.get("block_items") or {}).get(str(key))
    if not ctx.get("status") == "accepted" or not item:
        return None
    evidence = str(item.get("conclusion") or item.get("evidence") or "").strip()
    short = evidence[:160] + ("…" if len(evidence) > 160 else "")
    return {
        "status": item.get("status"),
        "status_label": _status_label(item.get("status")),
        "short_explanation": short,
        "checked_at": item.get("checked_at") or ctx.get("checked_at"),
        "scope_summary": ctx.get("summary") or {},
    }


def _audit_detail(ctx, kind, key):
    item = (ctx.get("project_items") if kind == "projects" else ctx.get("block_items") or {}).get(str(key))
    if ctx.get("status") != "accepted" or not item:
        return {"status": ctx.get("status") or "pending_acceptance",
                "status_label": _status_label(None),
                "message": ctx.get("message") or "备份核查结果待验收"}
    return {
        "status": item.get("status"),
        "status_label": _status_label(item.get("status")),
        "checked_at": item.get("checked_at") or ctx.get("checked_at"),
        "conclusion": item.get("conclusion"),
        "evidence": item.get("evidence"),
        "scope": item.get("scope") or ctx.get("scope"),
        "limitations": item.get("limitations") or ctx.get("limitations") or [],
    }


def _backup_status(environ):
    ctx = _accepted_audit(environ)
    return {k: ctx.get(k) for k in ("status", "message", "generated_at_local", "checked_at",
                                    "summary", "status_counts", "item_count", "scope", "limitations")}


def _coverage_summary(audit, environ):
    return {
        "ok": True,
        "snapshot": {
            "generated_at_local": audit.get("generated_at_local"),
            "candidate_db_sha256": (audit.get("scope") or {}).get("candidate_db_sha256"),
            "catalog_generated_at_utc": (audit.get("scope") or {}).get("catalog_generated_at_utc"),
            "is_snapshot_not_live": bool((audit.get("scope") or {}).get("audit_is_snapshot_not_live")),
        },
        "counts": {
            "projects": len(audit.get("projects") or []),
            "sources": len(audit.get("empty_sources") or []),
        },
        "backup_audit": _backup_status(environ),
    }


def _coverage_page(audit, group, params, environ):
    q = str((params or {}).get("q") or "").strip()[:200]
    needle = q.casefold()
    try:
        page = max(1, int((params or {}).get("page") or 1))
        size = min(50, max(1, int((params or {}).get("page_size") or 10)))
    except (TypeError, ValueError):
        raise HistoryCandidateError("invalid_pagination", 400)
    if group == "projects":
        source = audit.get("projects") or []
        items = [x for x in source if not needle or needle in " ".join(str(x.get(k) or "") for k in
                 ("project", "display_name", "reason", "reason_code", "next_step")).casefold()]
    else:
        source = audit.get("empty_sources") or []
        items = [x for x in source if not needle or needle in " ".join(str(x.get(k) or "") for k in
                 ("block_id", "title", "reason", "reason_code", "next_step")).casefold()]
    total = len(items)
    pages = max(1, (total + size - 1) // size)
    if page > pages:
        page = pages
    offset = (page - 1) * size
    ctx = _accepted_audit(environ)
    page_items = []
    for row in items[offset:offset + size]:
        enriched = dict(row)
        key = row.get("project") if group == "projects" else row.get("block_id")
        audit_row = _audit_row_context(ctx, group, key)
        if audit_row:
            enriched["backup_audit"] = audit_row
        page_items.append(enriched)
    return {
        "ok": True, "group": group, "q": q, "page": page, "page_size": size,
        "pages": pages, "total": total, "items": page_items,
        "snapshot": _coverage_summary(audit, environ).get("snapshot"),
    }


def _coverage_audit(environ):
    path_value = (environ or os.environ).get("EVOLVMEM_HISTORY_COVERAGE_FILE", "")
    if not path_value:
        raise HistoryCandidateError("history_candidate_coverage_unavailable", 404)
    path = pathlib.Path(path_value)
    if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
        raise HistoryCandidateError("history_candidate_coverage_unavailable", 404)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        raise HistoryCandidateError("history_candidate_coverage_invalid", 500)
    if not isinstance(payload, dict) or payload.get("schema") != _COVERAGE_SCHEMA:
        if isinstance(payload, dict) and payload.get("schema") == "evolvmem.recovery.gap_audit.v3":
            payload = {
                "schema": _COVERAGE_SCHEMA,
                "generated_at_local": payload.get("generated_at"),
                "scope": {
                    "candidate_db_sha256": payload.get("candidate_sha256"),
                    "audit_is_snapshot_not_live": True,
                    "source_schema": "evolvmem.recovery.gap_audit.v3",
                },
                "projects": payload.get("zero_history_projects") or [],
                "empty_sources": payload.get("empty_sources") or [],
            }
        else:
            raise HistoryCandidateError("history_candidate_coverage_invalid", 500)
    if not isinstance(payload.get("projects"), list) or not isinstance(payload.get("empty_sources"), list):
        raise HistoryCandidateError("history_candidate_coverage_invalid", 500)
    return payload


def dispatch(method: str, path: str, params: dict, headers, environ: dict | None = None,
             owner_verified: bool = False):
    """Return ``(status, payload)``; never raises for expected failures.

    ``owner_verified`` is set by the Web layer only after the existing Feishu
    session passed the owner identity rule. The personal token remains a
    headless-verification fallback and is never required by the normal UI.
    """
    try:
        clean = path.rstrip("/") or "/"
        if str(method).upper() == "POST" and clean in (PREFIX + "/management/delete", PREFIX + "/management/restore"):
            # Write access is owner-session only; the read token never grants it.
            if not owner_verified or headers.get('X-EvolvMem-Action') != 'history-manage':
                raise HistoryCandidateError('history_management_forbidden', 403)
            from evolvmem.history_management import manage, management_path
            with open_reader(environ) as reader:
                return 200, manage(reader, management_path(environ), clean.rsplit('/', 1)[1], params)
        if str(method).upper() != "GET":
            raise HistoryCandidateError("history_candidate_method_not_allowed", 405)
        if not owner_verified:
            authorize(headers, environ)
        clean = path.rstrip("/") or "/"
        with open_reader(environ) as reader:
            def coverage_audit():
                audit = dict(_coverage_audit(environ))
                audit['projects'] = [r for r in audit['projects'] if reader.project_visible(r['project'])]
                audit['empty_sources'] = [r for r in audit['empty_sources'] if reader.block_visible(r['block_id'])]
                return audit
            if clean == PREFIX + '/management/trash':
                from evolvmem.history_management import management_path
                state = reader.management
                return 200, {'ok': True, 'items': sorted(state.items.values(), key=lambda r: r['deleted_at'], reverse=True),
                             'enabled': bool(management_path(environ))}
            if clean == PREFIX + '/search':
                from evolvmem.history_management import search
                return 200, search(reader, params)
            if clean == PREFIX + "/coverage/summary":
                return 200, _coverage_summary(coverage_audit(), environ)
            if clean == PREFIX + "/coverage/projects":
                return 200, _coverage_page(coverage_audit(), "projects", params, environ)
            if clean == PREFIX + "/coverage/sources":
                return 200, _coverage_page(coverage_audit(), "sources", params, environ)
            source_match = _COVERAGE_SOURCE.fullmatch(path)
            if source_match:
                audit = coverage_audit()
                block_id = unquote(source_match.group(1))
                if not reader.block_visible(block_id.strip()):
                    raise HistoryCandidateError('history_item_deleted', 410)
                item = next((x for x in audit["empty_sources"] if x.get("block_id") == block_id), None)
                if item is None:
                    raise HistoryCandidateError("history_candidate_coverage_source_not_found", 404)
                return 200, {"ok": True, "item": item,
                             "backup_audit": _audit_detail(_accepted_audit(environ), "sources", block_id),
                             "snapshot": _coverage_summary(audit, environ)["snapshot"]}
            audit_match = _AUDIT_PROJECT.fullmatch(path)
            if audit_match:
                if not reader.project_visible(unquote(audit_match.group(1))):
                    raise HistoryCandidateError('history_item_deleted', 410)
                return 200, {"ok": True, "kind": "projects", "key": unquote(audit_match.group(1)),
                             "backup_audit": _audit_detail(_accepted_audit(environ), "projects",
                                                          unquote(audit_match.group(1)))}
            audit_match = _AUDIT_SOURCE.fullmatch(path)
            if audit_match:
                if not reader.block_visible(unquote(audit_match.group(1)).strip()):
                    raise HistoryCandidateError('history_item_deleted', 410)
                return 200, {"ok": True, "kind": "sources", "key": unquote(audit_match.group(1)),
                             "backup_audit": _audit_detail(_accepted_audit(environ), "sources",
                                                          unquote(audit_match.group(1)))}
            if clean == PREFIX + "/coverage":
                # Compatibility for the previous combined endpoint: no bulk dump.
                summary = _coverage_summary(coverage_audit(), environ)
                summary["links"] = {"projects": PREFIX + "/coverage/projects",
                                    "sources": PREFIX + "/coverage/sources"}
                summary["deprecated"] = True
                return 200, summary
            if clean == PREFIX + "/projects":
                return 200, {"ok": True, "projects": reader.projects()}
            if clean == PREFIX + "/unassigned":
                payload = reader.unassigned()
                ctx = _accepted_audit(environ)
                for row in payload.get("empty") or []:
                    audit_row = _audit_row_context(ctx, "sources", row.get("block_id"))
                    if audit_row:
                        row["backup_audit"] = audit_row
                return 200, payload
            match = _CONTEXT.fullmatch(path)
            if match:
                return 200, reader.block_projects(unquote(match.group(1)))
            match = _TURNS.fullmatch(path)
            if match:
                return 200, reader.block_turn_page(unquote(match.group(1)),
                                                   params.get("page", 1),
                                                   params.get("page_size", 50))
            match = _PROJECT.fullmatch(path)
            if match:
                return 200, reader.project_overview(unquote(match.group(1)))
            match = _BLOCK.fullmatch(path)
            if match:
                return 200, reader.block_page(unquote(match.group(1)),
                                              params.get("page", 1),
                                              params.get("page_size", 50))
        raise HistoryCandidateError("history_candidate_not_found", 404)
    except HistoryCandidateError as exc:
        return exc.status, {"ok": False, "error": exc.code}
    except Exception:
        # Bounded surface: no paths, SQL or exception text leave the process.
        return 500, {"ok": False, "error": "history_candidate_internal_error"}

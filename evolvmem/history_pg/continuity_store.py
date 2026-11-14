"""PG-backed continuity, context_session_start, project recall, and status store."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any
import uuid

import psycopg
from psycopg.rows import dict_row

from evolvmem.history_pg.config import PgHistoryConfig, PgHistoryPool
from evolvmem.lan_sharing import LanError, validate_request_id


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ws_fp(workspace_path: str, device_id: str) -> str:
    base = f"{device_id.strip()}::{workspace_path.strip()}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()[:32]


def _layer_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _escape_like(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class PgContinuityStore:
    """PostgreSQL runtime continuity and session-start context operations."""

    def __init__(self, config: PgHistoryConfig, *, pool: PgHistoryPool | None = None) -> None:
        self.config = config
        self.pool = pool or PgHistoryPool(config)

    @staticmethod
    def _memory_revision(conn: psycopg.Connection) -> int:
        row = conn.execute("SELECT revision FROM em_runtime.lan_memory_revision WHERE id = 1").fetchone()
        return int(row["revision"]) if row else 0

    @staticmethod
    def _next_item_id(conn: psycopg.Connection) -> int:
        row = conn.execute(
            "SELECT nextval(pg_get_serial_sequence('em_runtime.context_items', 'id')) AS id"
        ).fetchone()
        return int(row["id"])

    @staticmethod
    def _upsert_project(conn: psycopg.Connection, project: str, now_utc: str) -> None:
        if not project:
            return
        conn.execute(
            "INSERT INTO em_runtime.context_project_registry (project, status, revision, created_at, updated_at, display_name) "
            "VALUES (%s, 'active', 1, %s, %s, %s) ON CONFLICT (project) DO NOTHING",
            (project, now_utc, now_utc, project),
        )

    def _upsert_checkpoint_item(
        self,
        conn: psycopg.Connection,
        *,
        project: str,
        ws_id: str,
        l0: str,
        l1: str,
        l2: str,
        now_utc: str,
    ) -> int:
        ident_key = f"continuity:{ws_id}"
        row = conn.execute(
            "SELECT id FROM em_runtime.context_items WHERE identity_key = %s",
            (ident_key,),
        ).fetchone()
        if row is None:
            item_id = int(
                conn.execute(
                    "INSERT INTO em_runtime.context_items "
                    "(identity_key, content_type, project, scope, status, tier, tags, importance, confidence, "
                    "source_state, source_count, created_at, updated_at, experience_payload, projects, personal_general) "
                    "VALUES (%s,'continuity',%s,'project','active','high','[]',8.0,0.9,'verified',1,%s,%s,'','[]',0) "
                    "RETURNING id",
                    (ident_key, project, now_utc, now_utc),
                ).fetchone()["id"]
            )
        else:
            item_id = int(row["id"])
            conn.execute(
                "UPDATE em_runtime.context_items SET updated_at=%s, project=COALESCE(NULLIF(%s,''), project), status='active' WHERE id=%s",
                (now_utc, project, item_id),
            )
        for layer_name, text in (("l0", l0), ("l1", l1), ("l2", l2)):
            if not text:
                continue
            existing = conn.execute(
                "SELECT id FROM em_runtime.context_layers WHERE item_id=%s AND layer=%s LIMIT 1",
                (item_id, layer_name),
            ).fetchone()
            h = _layer_hash(text)
            if existing is None:
                conn.execute(
                    "INSERT INTO em_runtime.context_layers (item_id, layer, content, content_hash, generator, created_at, updated_at) "
                    "VALUES (%s,%s,%s,%s,'pg_continuity',%s,%s)",
                    (item_id, layer_name, text, h, now_utc, now_utc),
                )
            else:
                conn.execute(
                    "UPDATE em_runtime.context_layers SET content=%s, content_hash=%s, updated_at=%s WHERE id=%s",
                    (text, h, now_utc, existing["id"]),
                )
        return item_id

    def memory_status(self, user_id: str, args: dict[str, Any], *, statement_timeout_ms: int | None = None) -> dict[str, Any]:
        dbname = self.config.resolve_user_history_db(user_id)
        with self.pool.connection(dbname, role_kind="reader", statement_timeout_ms=statement_timeout_ms) as conn:
            conn.row_factory = dict_row
            rev = self._memory_revision(conn)
            if args.get("detail") == "revision":
                return {"detail": "revision", "memory_revision": f"{rev}.0", "pg_backed": True}
            ctx_cnt = int(conn.execute("SELECT count(*) AS c FROM em_runtime.context_items WHERE status <> 'deleted'").fetchone()["c"])
            arch_cnt = int(conn.execute("SELECT count(*) AS c FROM em_runtime.session_archives").fetchone()["c"])
            ws_cnt = int(conn.execute("SELECT count(*) AS c FROM em_runtime.continuity_workstreams WHERE status='open'").fetchone()["c"])
            return {
                "available": True,
                "backend": "postgres",
                "memory_revision": f"{rev}.0",
                "active_memories": ctx_cnt,
                "session_archives": arch_cnt,
                "active_workstreams": ws_cnt,
                "context_ready": True,
                "embedding_dim": 768,
            }

    def context_session_start(self, user_id: str, args: dict[str, Any], *, statement_timeout_ms: int | None = None) -> dict[str, Any]:
        project = str(args.get("project") or "").strip()
        ws_path = str(args.get("workspace_path") or "").strip()
        dev_id = str(args.get("device_id") or "").strip()
        query = str(args.get("query") or "").strip()
        try:
            max_chars = max(200, min(int(args.get("max_chars") or 3200), 16000))
        except (TypeError, ValueError) as exc:
            raise LanError("invalid_arguments") from exc
        fp = _ws_fp(ws_path, dev_id) if ws_path and dev_id else ""

        dbname = self.config.resolve_user_history_db(user_id)
        with self.pool.connection(dbname, role_kind="reader", statement_timeout_ms=statement_timeout_ms) as conn:
            conn.row_factory = dict_row
            rev = self._memory_revision(conn)
            ws_row = None
            if fp and project:
                ws_row = conn.execute(
                    "SELECT w.* FROM em_runtime.continuity_focus f "
                    "JOIN em_runtime.continuity_workstreams w ON w.id = f.workstream_id "
                    "WHERE f.project=%s AND f.workspace_fingerprint=%s "
                    "AND NOT EXISTS (SELECT 1 FROM em_history.management_trash tp WHERE tp.kind='project' AND COALESCE(NULLIF(tp.project,''),tp.key)=w.project) "
                    "LIMIT 1",
                    (project, fp),
                ).fetchone()
            if ws_row is None and project:
                ws_row = conn.execute(
                    "SELECT * FROM em_runtime.continuity_workstreams w WHERE w.project=%s AND w.status='open' "
                    "AND NOT EXISTS (SELECT 1 FROM em_history.management_trash tp WHERE tp.kind='project' AND COALESCE(NULLIF(tp.project,''),tp.key)=w.project) "
                    "ORDER BY w.updated_at DESC LIMIT 1",
                    (project,),
                ).fetchone()

            lines: list[str] = []
            selected_ids: list[int] = []
            if ws_row and ws_row.get("current_context_id"):
                cid = int(ws_row["current_context_id"])
                layers = conn.execute(
                    "SELECT layer, content FROM em_runtime.context_layers WHERE item_id=%s ORDER BY layer",
                    (cid,),
                ).fetchall()
                layer_text = {r["layer"]: r["content"] for r in layers}
                l1 = layer_text.get("l1") or layer_text.get("l0") or ""
                if l1:
                    lines.append(f"[Continuity workstream {ws_row['id']} rev={ws_row['checkpoint_revision']}]\n{l1}")
                    selected_ids.append(cid)

            ctx_rows = conn.execute(
                "SELECT ci.id, ci.project, COALESCE(cl.content, '') AS body "
                "FROM em_runtime.context_items ci "
                "LEFT JOIN em_runtime.context_layers cl ON cl.item_id = ci.id AND cl.layer = 'l1' "
                "WHERE ci.status='active' AND (%s = '' OR ci.project = %s) "
                "AND NOT EXISTS (SELECT 1 FROM em_history.management_trash tp WHERE tp.kind='project' AND COALESCE(NULLIF(tp.project,''),tp.key)=ci.project) "
                "ORDER BY ci.updated_at DESC LIMIT 8",
                (project, project),
            ).fetchall()
            for r in ctx_rows:
                cid = int(r["id"])
                if cid in selected_ids:
                    continue
                body = (r["body"] or "").strip()
                if body:
                    lines.append(f"- [{r['project'] or 'general'}] {body[:280]}")
                    selected_ids.append(cid)

            if not lines and query:
                hits = conn.execute(
                    "SELECT b.block_id, b.title, b.projects FROM em_history.blocks b "
                    "WHERE NOT EXISTS (SELECT 1 FROM em_history.management_trash t WHERE t.kind = 'block' AND t.key = b.block_id) "
                    "AND NOT (EXISTS (SELECT 1 FROM em_history.block_projects bp WHERE bp.block_id = b.block_id) "
                    "AND NOT EXISTS (SELECT 1 FROM em_history.block_projects bp WHERE bp.block_id = b.block_id "
                    "AND NOT EXISTS (SELECT 1 FROM em_history.management_trash tp WHERE tp.kind='project' AND COALESCE(NULLIF(tp.project,''),tp.key)=bp.project))) "
                    "ORDER BY b.observed_last_at DESC LIMIT 3"
                ).fetchall()
                for h in hits:
                    lines.append(f"- [history:{h['block_id']}] {h['title']}")

            block = "\n".join(lines)[:max_chars]
            if not block:
                block = f"[EvolvMem PG ready] project={project or 'general'} revision={rev}"
            return {
                "project": project,
                "block": block,
                "selected_ids": selected_ids,
                "used_chars": len(block),
                "memory_revision": f"{rev}.0",
                "workstream_id": ws_row["id"] if ws_row else "",
                "checkpoint_revision": int(ws_row["checkpoint_revision"]) if ws_row else 0,
                "continuation": {"workstream_id": ws_row["id"], "staleness": "fresh"} if ws_row else None,
                "continuation_code": "ok" if ws_row else "no_active_workstream",
            }

    def context_project_recall(self, user_id: str, args: dict[str, Any], *, statement_timeout_ms: int | None = None) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        try:
            max_chars = max(200, min(int(args.get("max_chars") or 1200), 8000))
        except (TypeError, ValueError) as exc:
            raise LanError("invalid_arguments") from exc
        pat = f"%{_escape_like(query)}%"
        dbname = self.config.resolve_user_history_db(user_id)
        with self.pool.connection(dbname, role_kind="reader", statement_timeout_ms=statement_timeout_ms) as conn:
            conn.row_factory = dict_row
            rows = conn.execute(
                "SELECT p.project, p.display_name, p.summary "
                "FROM em_history.projects p "
                "WHERE (%s = '' OR p.project ILIKE %s ESCAPE '\\' OR p.display_name ILIKE %s ESCAPE '\\' OR p.summary ILIKE %s ESCAPE '\\') "
                "AND NOT EXISTS (SELECT 1 FROM em_history.management_trash tp WHERE tp.kind='project' AND COALESCE(NULLIF(tp.project,''),tp.key)=p.project) "
                "ORDER BY p.block_count DESC LIMIT 5",
                (query, pat, pat, pat),
            ).fetchall()
            lines = [f"{r['project']}: {r['display_name']} - {(r['summary'] or '')[:160]}" for r in rows]
            return {"block": "\n".join(lines)[:max_chars], "matched_projects": [r["project"] for r in rows]}

    def context_search(self, user_id: str, args: dict[str, Any], *, statement_timeout_ms: int | None = None) -> dict[str, Any]:
        query = str(args.get("query") or "").strip()
        project = str(args.get("project") or "").strip()
        try:
            top_k = max(1, min(int(args.get("top_k") or 10), 20))
        except (TypeError, ValueError) as exc:
            raise LanError("invalid_arguments") from exc
        pat = f"%{_escape_like(query)}%"
        dbname = self.config.resolve_user_history_db(user_id)
        with self.pool.connection(dbname, role_kind="reader", statement_timeout_ms=statement_timeout_ms) as conn:
            conn.row_factory = dict_row
            rows = conn.execute(
                "SELECT ci.id, ci.project, ci.content_type, LEFT(cl.content, 300) AS snippet "
                "FROM em_runtime.context_items ci JOIN em_runtime.context_layers cl ON cl.item_id = ci.id "
                "WHERE ci.status='active' AND cl.layer='l1' AND (%s='' OR ci.project=%s) AND (%s='' OR cl.content ILIKE %s ESCAPE '\\') "
                "ORDER BY ci.updated_at DESC LIMIT %s",
                (project, project, query, pat, top_k),
            ).fetchall()
            results = [{"id": int(r["id"]), "project": r["project"], "content_type": r["content_type"], "snippet": r["snippet"], "score": 1.0} for r in rows]
            return {"results": results, "count": len(results)}

    def context_read(self, user_id: str, args: dict[str, Any], *, statement_timeout_ms: int | None = None) -> dict[str, Any]:
        try:
            item_id = int(args.get("id") or 0)
        except (TypeError, ValueError) as exc:
            raise LanError("invalid_arguments") from exc
        layer = str(args.get("layer") or "L1").lower()
        if item_id <= 0 or layer not in ("l0", "l1", "l2"):
            raise LanError("invalid_arguments")
        dbname = self.config.resolve_user_history_db(user_id)
        with self.pool.connection(dbname, role_kind="reader", statement_timeout_ms=statement_timeout_ms) as conn:
            conn.row_factory = dict_row
            row = conn.execute(
                "SELECT ci.id, ci.project, cl.layer, cl.content FROM em_runtime.context_items ci "
                "JOIN em_runtime.context_layers cl ON cl.item_id = ci.id "
                "WHERE ci.id=%s AND cl.layer=%s AND ci.status <> 'deleted' LIMIT 1",
                (item_id, layer),
            ).fetchone()
            if row is None:
                raise LanError("context_not_found")
            return {"id": int(row["id"]), "project": row["project"], "layer": row["layer"], "content": row["content"]}

    def continuity_checkpoint(self, user_id: str, args: dict[str, Any], *, op_name: str = "continuity_checkpoint", statement_timeout_ms: int | None = None) -> dict[str, Any]:
        req_id = str(args.get("request_id") or "")
        validate_request_id(req_id)
        project = str(args.get("project") or "general").strip() or "general"
        ws_path = str(args.get("workspace_path") or "").strip()
        dev_id = str(args.get("device_id") or "default").strip() or "default"
        summary = str(args.get("summary") or args.get("state") or "Checkpoint saved").strip()
        next_step = str(args.get("next_step") or "").strip()
        fp = _ws_fp(ws_path or project, dev_id)
        now_utc = _utc_now()
        payload_hash = hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()

        raw_repo = args.get("repo_snapshot")
        if raw_repo is None:
            repo: dict[str, Any] = {}
        elif isinstance(raw_repo, dict):
            repo = raw_repo
        else:
            raise LanError("invalid_arguments")

        dbname = self.config.resolve_user_history_db(user_id)
        with self.pool.connection(dbname, role_kind="writer", statement_timeout_ms=statement_timeout_ms) as conn:
            conn.row_factory = dict_row
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))", (project, fp))
            prev = conn.execute(
                "SELECT payload_hash, result FROM em_runtime.lan_write_requests WHERE user_id=%s AND tool=%s AND request_id=%s",
                (user_id, op_name, req_id),
            ).fetchone()
            if prev is not None:
                if prev["payload_hash"] != payload_hash:
                    raise LanError("request_id_conflict")
                return json.loads(prev["result"])

            self._upsert_project(conn, project, now_utc)
            ws = conn.execute(
                "SELECT * FROM em_runtime.continuity_workstreams WHERE project=%s AND workspace_fingerprint=%s AND status='open' ORDER BY updated_at DESC LIMIT 1 FOR UPDATE",
                (project, fp),
            ).fetchone()
            ws_id = ws["id"] if ws else f"ws-{uuid.uuid4().hex[:16]}"
            before_rev = int(ws["checkpoint_revision"]) if ws else 0
            after_rev = before_rev + 1
            before_sv = int(ws["state_version"]) if ws else 0
            after_sv = before_sv + 1
            l0 = summary[:180]
            l1 = f"{summary}\nNext: {next_step}".strip() if next_step else summary
            l2 = json.dumps({"summary": summary, "next_step": next_step, "device_id": dev_id}, ensure_ascii=False)
            cid = self._upsert_checkpoint_item(conn, project=project, ws_id=ws_id, l0=l0, l1=l1, l2=l2, now_utc=now_utc)
            if ws is None:
                conn.execute(
                    "INSERT INTO em_runtime.continuity_workstreams "
                    "(id, project, workspace_fingerprint, current_context_id, checkpoint_revision, state_version, status, "
                    "repo_kind, repo_branch, repo_root_commit, repo_head_commit, created_at, updated_at) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'open',%s,%s,%s,%s,%s,%s)",
                    (ws_id, project, fp, cid, after_rev, after_sv, ("git" if str(repo.get("kind") or "") == "git" else "non_git"), str(repo.get("branch") or ""), str(repo.get("root_commit") or ""), str(repo.get("head_commit") or ""), now_utc, now_utc),
                )
            else:
                conn.execute(
                    "UPDATE em_runtime.continuity_workstreams SET current_context_id=%s, checkpoint_revision=%s, state_version=%s, updated_at=%s WHERE id=%s",
                    (cid, after_rev, after_sv, now_utc, ws_id),
                )
            conn.execute(
                "INSERT INTO em_runtime.continuity_focus (project, workspace_fingerprint, workstream_id, revision, updated_at) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (project, workspace_fingerprint) DO UPDATE SET workstream_id=EXCLUDED.workstream_id, revision=EXCLUDED.revision, updated_at=EXCLUDED.updated_at",
                (project, fp, ws_id, after_rev, now_utc),
            )
            conn.execute(
                "INSERT INTO em_runtime.continuity_events (workstream_id, event_type, before_revision, after_revision, before_state_version, after_state_version, error_code, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,'',%s)",
                (ws_id, op_name, before_rev, after_rev, before_sv, after_sv, now_utc),
            )
            conn.execute(
                "INSERT INTO em_runtime.lan_memory_revision (id, revision) VALUES (1, 1) "
                "ON CONFLICT (id) DO UPDATE SET revision = em_runtime.lan_memory_revision.revision + 1"
            )
            result = {"ok": True, "workstream_id": ws_id, "checkpoint_revision": after_rev, "context_id": cid, "project": project}
            conn.execute(
                "INSERT INTO em_runtime.cutover_write_journal (library_key, request_id, actor_id, op_type, target_table, target_key, payload) "
                "VALUES (%s,%s,%s,%s,'em_runtime.continuity_workstreams',%s,%s::jsonb) ON CONFLICT DO NOTHING",
                (user_id, req_id, user_id, op_name, ws_id, json.dumps({
                    "workstream_id": ws_id, "project": project, "workspace_fingerprint": fp, "context_id": cid,
                    "checkpoint_revision": after_rev, "l0": l0, "l1": l1, "l2": l2, "now_utc": now_utc,
                }, ensure_ascii=False)),
            )
            conn.execute(
                "INSERT INTO em_runtime.lan_write_requests (user_id, tool, request_id, payload_hash, result) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (user_id, op_name, req_id, payload_hash, json.dumps(result, ensure_ascii=False)),
            )
            return result

    def continuity_resume(self, user_id: str, args: dict[str, Any], *, statement_timeout_ms: int | None = None) -> dict[str, Any]:
        project = str(args.get("project") or args.get("project_hint") or "").strip()
        ws_path = str(args.get("workspace_path") or "").strip()
        dev_id = str(args.get("device_id") or "").strip()
        fp = _ws_fp(ws_path, dev_id) if ws_path and dev_id else ""
        dbname = self.config.resolve_user_history_db(user_id)
        with self.pool.connection(dbname, role_kind="reader", statement_timeout_ms=statement_timeout_ms) as conn:
            conn.row_factory = dict_row
            ws = None
            staleness = "fresh"
            if fp and project:
                ws = conn.execute(
                    "SELECT * FROM em_runtime.continuity_workstreams WHERE project=%s AND workspace_fingerprint=%s ORDER BY updated_at DESC LIMIT 1",
                    (project, fp),
                ).fetchone()
            if ws is None and project:
                ws = conn.execute(
                    "SELECT * FROM em_runtime.continuity_workstreams WHERE project=%s ORDER BY updated_at DESC LIMIT 1",
                    (project,),
                ).fetchone()
                if ws is not None and fp:
                    staleness = "workspace_mismatch"
            if ws is None:
                return {"found": False, "staleness": "none", "checkpoint": {}}
            layers = conn.execute("SELECT layer, content FROM em_runtime.context_layers WHERE item_id=%s", (ws["current_context_id"],)).fetchall()
            ckpt = {r["layer"].lower(): r["content"] for r in layers}
            return {
                "found": True,
                "workstream_id": ws["id"],
                "project": ws["project"],
                "checkpoint_revision": int(ws["checkpoint_revision"]),
                "staleness": staleness,
                "checkpoint": ckpt,
            }

    def continuity_list(self, user_id: str, args: dict[str, Any], *, statement_timeout_ms: int | None = None) -> dict[str, Any]:
        project = str(args.get("project") or "").strip()
        dbname = self.config.resolve_user_history_db(user_id)
        with self.pool.connection(dbname, role_kind="reader", statement_timeout_ms=statement_timeout_ms) as conn:
            conn.row_factory = dict_row
            rows = conn.execute(
                "SELECT id, project, checkpoint_revision, status, updated_at FROM em_runtime.continuity_workstreams "
                "WHERE (%s='' OR project=%s) ORDER BY updated_at DESC LIMIT 20",
                (project, project),
            ).fetchall()
            return {"workstreams": [dict(r) for r in rows]}

    def dispatch(self, user_id: str, name: str, args: dict[str, Any], *, statement_timeout_ms: int | None = None) -> dict[str, Any]:
        if name in ("memory_status", "context_status"):
            return self.memory_status(user_id, args, statement_timeout_ms=statement_timeout_ms)
        if name == "context_session_start":
            return self.context_session_start(user_id, args, statement_timeout_ms=statement_timeout_ms)
        if name == "context_project_recall":
            return self.context_project_recall(user_id, args, statement_timeout_ms=statement_timeout_ms)
        if name == "context_search":
            return self.context_search(user_id, args, statement_timeout_ms=statement_timeout_ms)
        if name == "context_read":
            return self.context_read(user_id, args, statement_timeout_ms=statement_timeout_ms)
        if name in ("continuity_begin", "continuity_checkpoint"):
            return self.continuity_checkpoint(user_id, args, op_name=name, statement_timeout_ms=statement_timeout_ms)
        if name in ("continuity_resume", "continuity_find"):
            return self.continuity_resume(user_id, args, statement_timeout_ms=statement_timeout_ms)
        if name == "continuity_list":
            return self.continuity_list(user_id, args, statement_timeout_ms=statement_timeout_ms)
        raise LanError("unsupported_tool")

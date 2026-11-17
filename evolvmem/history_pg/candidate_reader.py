"""PG-backed candidate history reader, hybrid FTS+trigram+vector search, and delete/restore state."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote
import uuid

from evolvmem.history_pg.config import PgHistoryConfig, load_pg_history_config

_TRASH_PROJ_SQL = "COALESCE(NULLIF(tp.project, ''), tp.key)"
_LEGACY_VISIBLE_BLOCK_SQL = (
    "NOT EXISTS (SELECT 1 FROM em_history.management_trash t WHERE t.kind = 'block' AND t.key = b.block_id) "
    "AND NOT ("
    "  EXISTS (SELECT 1 FROM em_history.block_projects bp WHERE bp.block_id = b.block_id) "
    "  AND NOT EXISTS ("
    "    SELECT 1 FROM em_history.block_projects bp "
    "    WHERE bp.block_id = b.block_id "
    "      AND NOT EXISTS (SELECT 1 FROM em_history.management_trash tp WHERE tp.kind = 'project' AND " + _TRASH_PROJ_SQL + " = bp.project) "
    "      AND NOT EXISTS (SELECT 1 FROM em_history.management_trash tpb WHERE tpb.kind = 'project_block' AND tpb.project = bp.project AND tpb.key = b.block_id)"
    "  )"
    ")"
)
_VISIBLE_BLOCK_SQL = (
    _LEGACY_VISIBLE_BLOCK_SQL +
    " AND NOT EXISTS (SELECT 1 FROM em_history.candidate_meta wf "
    "JOIN em_history.management_trash wt ON wt.kind = 'workspace' "
    "AND wt.key = wf.value WHERE wf.key = 'workspace_projection:v1:folder:' || b.block_id "
    "AND wf.value <> '')"
)


def _vec_literal(vec: list[float] | tuple[float, ...]) -> str:
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"


def _escape_like(q: str) -> str:
    return q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class PgHistoryCandidateReader:
    """Drop-in PostgreSQL replacement for SQLite HistoryCandidateReader & ManagedReader."""

    def __init__(
        self,
        pg_config: PgHistoryConfig | None = None,
        *,
        dbname: str | None = None,
        user_id: str = "",
        embedder: Any = None,
        attachments_dir: Path | str | None = None,
    ) -> None:
        self._cfg = pg_config if pg_config is not None else load_pg_history_config()
        self._dbname = dbname or self._cfg.resolve_user_history_db(user_id)
        self._embedder = embedder
        env_att = os.environ.get("EVOLVMEM_HISTORY_ATTACHMENTS_DIR", "").strip()
        raw_att = attachments_dir or (Path(env_att) if env_att else (self._cfg.cas_root / "attachments"))
        self._attachments_dir = Path(raw_att).expanduser()

    def __enter__(self) -> PgHistoryCandidateReader:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        return None

    def block_visible(self, block_id: str) -> bool:
        with self._cfg.connect(self._dbname, role_kind="reader") as conn:
            row = conn.execute(
                f"SELECT 1 FROM em_history.blocks b WHERE b.block_id = %s AND {_VISIBLE_BLOCK_SQL} LIMIT 1",
                (block_id,),
            ).fetchone()
            return row is not None

    def visible_block_count(self) -> int:
        with self._cfg.connect(self._dbname, role_kind="reader") as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM em_history.blocks b WHERE b.message_count > 0 AND {_VISIBLE_BLOCK_SQL}"
            ).fetchone()
            return int(row[0] or 0) if row else 0

    def projects(self) -> dict[str, Any]:
        with self._cfg.connect(self._dbname, role_kind="reader") as conn:
            rows = conn.execute(
                "SELECT p.project, p.display_name, p.status, p.attachment_count, p.first_at, p.last_at, p.summary "
                "FROM em_history.projects p "
                f"WHERE NOT EXISTS (SELECT 1 FROM em_history.management_trash tp WHERE tp.kind = 'project' AND {_TRASH_PROJ_SQL} = p.project) "
                "ORDER BY p.last_at DESC, p.project"
            ).fetchall()
            vis_rows = conn.execute(
                "SELECT bp.project, COUNT(*), COALESCE(SUM(b.message_count), 0), COALESCE(SUM(b.turn_count), 0) "
                "FROM em_history.block_projects bp JOIN em_history.blocks b ON b.block_id = bp.block_id "
                f"WHERE {_VISIBLE_BLOCK_SQL} "
                "AND NOT EXISTS (SELECT 1 FROM em_history.management_trash tpb WHERE tpb.kind = 'project_block' AND tpb.project = bp.project AND tpb.key = b.block_id) "
                "GROUP BY bp.project"
            ).fetchall()

        vis_by_proj = {str(r[0]): (int(r[1] or 0), int(r[2] or 0), int(r[3] or 0)) for r in vis_rows}
        out = []
        for r in rows:
            proj = str(r[0])
            bcnt, mcnt, tcnt = vis_by_proj.get(proj, (0, 0, 0))
            out.append({
                "project": proj,
                "display_name": r[1] or proj,
                "status": r[2],
                "block_count": bcnt,
                "message_count": mcnt,
                "turn_count": tcnt,
                "attachment_count": int(r[3] or 0),
                "first_at": r[4],
                "last_at": r[5],
                "summary": r[6],
            })
        return {"ok": True, "projects": out}

    def unassigned(self) -> dict[str, Any]:
        with self._cfg.connect(self._dbname, role_kind="reader") as conn:
            rows = conn.execute(
                "SELECT b.block_id, b.title, b.adapter, b.message_count, b.observed_first_at, b.observed_last_at "
                "FROM em_history.blocks b WHERE NOT EXISTS ("
                "SELECT 1 FROM em_history.block_projects bp WHERE bp.block_id = b.block_id) "
                f"AND {_VISIBLE_BLOCK_SQL} "
                "ORDER BY b.observed_last_at DESC LIMIT 200"
            ).fetchall()
        blocks = [
            {
                "block_id": r[0], "title": r[1], "adapter": r[2],
                "message_count": int(r[3] or 0), "observed_first_at": r[4], "observed_last_at": r[5],
            }
            for r in rows
        ]
        return {"ok": True, "blocks": blocks}

    def project_overview(self, project: str) -> dict[str, Any]:
        proj = unquote(project)
        with self._cfg.connect(self._dbname, role_kind="reader") as conn:
            prow = conn.execute(
                "SELECT p.project, p.display_name, p.status, p.summary, p.first_at, p.last_at "
                "FROM em_history.projects p WHERE p.project = %s "
                f"AND NOT EXISTS (SELECT 1 FROM em_history.management_trash tp WHERE tp.kind = 'project' AND {_TRASH_PROJ_SQL} = p.project)",
                (proj,),
            ).fetchone()
            if not prow:
                return {"ok": False, "error": "project_not_found"}
            brows = conn.execute(
                "SELECT b.block_id, b.title, b.adapter, b.message_count, b.turn_count, b.observed_first_at, b.observed_last_at "
                "FROM em_history.blocks b JOIN em_history.block_projects bp ON bp.block_id = b.block_id "
                "WHERE bp.project = %s "
                f"AND {_VISIBLE_BLOCK_SQL} "
                "AND NOT EXISTS (SELECT 1 FROM em_history.management_trash tpb WHERE tpb.kind = 'project_block' AND tpb.project = bp.project AND tpb.key = b.block_id) "
                "ORDER BY b.observed_last_at DESC LIMIT 500",
                (proj,),
            ).fetchall()
        return {
            "ok": True,
            "project": prow[0],
            "display_name": prow[1] or prow[0],
            "status": prow[2],
            "summary": prow[3],
            "first_at": prow[4],
            "last_at": prow[5],
            "blocks": [
                {
                    "block_id": r[0], "title": r[1], "adapter": r[2],
                    "message_count": int(r[3] or 0), "turn_count": int(r[4] or 0),
                    "observed_first_at": r[5], "observed_last_at": r[6],
                }
                for r in brows
            ],
        }

    def block_page(self, block_id: str) -> dict[str, Any]:
        bid = unquote(block_id)
        if not self.block_visible(bid):
            return {"ok": False, "error": "block_not_found"}
        with self._cfg.connect(self._dbname, role_kind="reader") as conn:
            row = conn.execute(
                "SELECT block_id, adapter, kind, title, message_count, turn_count, "
                "observed_first_at, observed_last_at, projects "
                "FROM em_history.blocks WHERE block_id = %s",
                (bid,),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "block_not_found"}
            total_messages = int(
                conn.execute("SELECT COUNT(*) FROM em_history.messages WHERE block_id = %s", (bid,)).fetchone()[0] or 0
            )
            msgs = conn.execute(
                "SELECT ordinal, role, content, recorded_at, attachments "
                "FROM em_history.messages WHERE block_id = %s ORDER BY ordinal LIMIT 200",
                (bid,),
            ).fetchall()
        return {
            "ok": True,
            "block": {
                "block_id": row[0], "adapter": row[1], "kind": row[2], "title": row[3],
                "message_count": int(row[4] or 0), "turn_count": int(row[5] or 0),
                "observed_first_at": row[6], "observed_last_at": row[7],
                "projects": json.loads(row[8] or "[]"),
            },
            "total_messages": total_messages,
            "has_more": total_messages > len(msgs),
            "messages": [
                {
                    "ordinal": m[0], "role": m[1], "content": m[2],
                    "recorded_at": m[3], "attachments": json.loads(m[4] or "[]"),
                }
                for m in msgs
            ],
        }

    def block_turn_page(self, block_id: str, *, offset: int = 0, limit: int = 50) -> dict[str, Any]:
        bid = unquote(block_id)
        if not self.block_visible(bid):
            return {"ok": False, "error": "block_not_found"}
        page_size, page_offset = min(200, max(1, int(limit))), max(0, int(offset))
        with self._cfg.connect(self._dbname, role_kind="reader") as conn:
            msgs = conn.execute(
                "SELECT ordinal, role, content, recorded_at, attachments "
                "FROM em_history.messages WHERE block_id = %s ORDER BY ordinal LIMIT %s OFFSET %s",
                (bid, page_size + 1, page_offset),
            ).fetchall()
        return {
            "ok": True,
            "block_id": bid,
            "has_more": len(msgs) > page_size,
            "next_offset": page_offset + min(len(msgs), page_size),
            "messages": [
                {"ordinal": m[0], "role": m[1], "content": m[2], "recorded_at": m[3], "attachments": json.loads(m[4] or "[]")}
                for m in msgs[:page_size]
            ],
        }

    def _resolve_attachment_file(self, raw_path: str | None) -> Path | None:
        if not raw_path:
            return None
        p = Path(raw_path)
        if ".." in p.parts:
            return None
        try:
            if p.is_absolute():
                cand = p.resolve()
                return cand if cand.is_file() and ".secrets" not in cand.parts else None
            root = self._attachments_dir.resolve()
            cand = (root / p).resolve()
            return cand if cand.is_relative_to(root) and cand.is_file() else None
        except OSError:
            return None

    def attachment(self, sha256: str) -> dict[str, Any] | None:
        if not isinstance(sha256, str) or not (16 <= len(sha256) <= 128) or not all(c in "0123456789abcdef" for c in sha256):
            return None
        with self._cfg.connect(self._dbname, role_kind="reader") as conn:
            row = conn.execute(
                "SELECT sha256, mime_type, bytes, path, format_ok, width, height FROM em_history.attachments WHERE sha256 = %s",
                (sha256,),
            ).fetchone()
            if not row:
                return None
            msg_rows = conn.execute(
                "SELECT m.block_id, m.attachments, (" + _VISIBLE_BLOCK_SQL + ") AS vis "
                "FROM em_history.messages m JOIN em_history.blocks b ON b.block_id = m.block_id "
                "WHERE m.attachments LIKE %s ESCAPE '\\'",
                ("%" + _escape_like(sha256) + "%",),
            ).fetchall()
        matching = []
        for mr in msg_rows:
            try:
                parsed = json.loads(mr[1] or "[]")
            except (TypeError, ValueError):
                parsed = []
            if isinstance(parsed, list) and any(
                (a == sha256 or (isinstance(a, dict) and a.get("sha256") == sha256))
                for a in parsed
            ):
                matching.append(mr)
        if matching and not any(bool(mr[2]) for mr in matching):
            return None
        resolved = self._resolve_attachment_file(row[3])
        if resolved is None:
            return None
        digest = hashlib.sha256()
        size = 0
        with resolved.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1 << 20), b""):
                digest.update(chunk)
                size += len(chunk)
        if digest.hexdigest() != row[0] or (row[2] and size != int(row[2])):
            raise ValueError("attachment_conflict")
        mime = row[1] or (mimetypes.guess_type(str(resolved))[0] or "application/octet-stream")
        return {"sha256": row[0], "mime_type": mime, "bytes": size, "path": str(resolved)}

    def coverage_report(self, report_key: str) -> dict[str, Any]:
        with self._cfg.connect(self._dbname, role_kind="reader") as conn:
            row = conn.execute(
                "SELECT payload FROM em_history.coverage_reports WHERE report_key = %s",
                (report_key,),
            ).fetchone()
        if not row:
            return {"ok": False, "error": "coverage_report_not_found"}
        payload = row[0] if isinstance(row[0], dict) else json.loads(str(row[0]))
        return {"ok": True, **payload} if "ok" not in payload else payload

    def hybrid_search(
        self,
        query: str,
        *,
        query_vec: list[float] | None = None,
        project: str | None = None,
        limit: int = 20,
        block_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Hybrid search fusing PG tsvector + pg_trgm + pgvector cosine via RRF."""
        q = (query or "").strip()
        if block_ids is not None and not block_ids:
            return []
        if query_vec is None and q and self._embedder is not None:
            query_vec = self._embedder.encode_query(q)
        if not q and not query_vec:
            return []
        top_n = max(1, min(100, int(limit)))
        scores: dict[str, dict[str, Any]] = {}

        def _acc(bid: str, title: str, snippet: str, projects_json: str, rank_idx: int, weight: float = 1.0) -> None:
            entry = scores.setdefault(bid, {
                "block_id": bid, "title": title, "snippet": snippet,
                "projects": json.loads(projects_json or "[]"), "score": 0.0,
            })
            entry["score"] += weight / (60.0 + rank_idx)
            if snippet and not entry["snippet"]:
                entry["snippet"] = snippet

        with self._cfg.connect(self._dbname, role_kind="reader") as conn:
            block_sql = "AND b.block_id = ANY(%s) " if block_ids is not None else ""
            block_params = (block_ids,) if block_ids is not None else ()
            proj_sql = (
                "AND EXISTS (SELECT 1 FROM em_history.block_projects bp WHERE bp.block_id = b.block_id AND bp.project = %s "
                f"AND NOT EXISTS (SELECT 1 FROM em_history.management_trash tp WHERE tp.kind = 'project' AND {_TRASH_PROJ_SQL} = bp.project) "
                "AND NOT EXISTS (SELECT 1 FROM em_history.management_trash tpb WHERE tpb.kind = 'project_block' AND tpb.project = bp.project AND tpb.key = b.block_id)) "
                if project else ""
            )
            if q:
                like_pat = f"%{_escape_like(q)}%"
                lex_params: tuple[Any, ...] = (q, q, like_pat, like_pat, q, q, like_pat, like_pat, q, q, q, q)
                if project:
                    lex_params += (project,)
                lex_params += block_params
                lex_params += (top_n * 3,)
                lex_rows = conn.execute(
                    "SELECT sub.block_id, sub.title, sub.snip, sub.projects, sub.lex_score FROM ("
                    "SELECT DISTINCT ON (b.block_id) b.block_id, b.title, COALESCE(m.content, b.title) AS snip, b.projects, "
                    "GREATEST(similarity(b.title, %s), COALESCE(similarity(m.content, %s), 0)) "
                    "+ CASE WHEN b.title ILIKE %s ESCAPE '\\' OR COALESCE(m.content, '') ILIKE %s ESCAPE '\\' THEN 1.0 ELSE 0.0 END "
                    "+ ts_rank(b.search_tsv, plainto_tsquery('simple', %s)) "
                    "+ COALESCE(ts_rank(m.search_tsv, plainto_tsquery('simple', %s)), 0) AS lex_score "
                    "FROM em_history.blocks b "
                    "LEFT JOIN em_history.messages m ON m.block_id = b.block_id "
                    f"WHERE {_VISIBLE_BLOCK_SQL} "
                    "AND (b.title ILIKE %s ESCAPE '\\' OR m.content ILIKE %s ESCAPE '\\' "
                    "OR b.title %% %s OR m.content %% %s "
                    "OR b.search_tsv @@ plainto_tsquery('simple', %s) "
                    "OR m.search_tsv @@ plainto_tsquery('simple', %s)) "
                    + proj_sql
                    + block_sql
                    + "ORDER BY b.block_id, lex_score DESC"
                    ") sub ORDER BY sub.lex_score DESC LIMIT %s",
                    lex_params,
                ).fetchall()
                for r_idx, (bid, title, snip, pjson, _) in enumerate(lex_rows, start=1):
                    _acc(str(bid), str(title or ""), str(snip or "")[:200], str(pjson or "[]"), r_idx, weight=1.2)

            if query_vec:
                vlit = _vec_literal(query_vec)
                vec_params: tuple[Any, ...] = (vlit,) + ((project,) if project else ()) + block_params + (vlit, top_n * 3)
                vec_rows = conn.execute(
                    "SELECT b.block_id, b.title, b.projects, 1.0 - (b.embedding <=> %s::vector) AS vsim "
                    "FROM em_history.blocks b "
                    f"WHERE b.embedding IS NOT NULL AND {_VISIBLE_BLOCK_SQL} "
                    + proj_sql
                    + block_sql
                    + "ORDER BY b.embedding <=> %s::vector LIMIT %s",
                    vec_params,
                ).fetchall()
                for r_idx, (bid, title, pjson, _) in enumerate(vec_rows, start=1):
                    _acc(str(bid), str(title or ""), "", str(pjson or "[]"), r_idx, weight=1.0)
                mvec_params: tuple[Any, ...] = (vlit,) + ((project,) if project else ()) + block_params + (vlit, top_n * 3)
                mvec_rows = conn.execute(
                    "SELECT sub.block_id, sub.title, sub.content, sub.projects, sub.vsim FROM ("
                    "SELECT DISTINCT ON (b.block_id) b.block_id, b.title, m.content, b.projects, 1.0 - (m.embedding <=> %s::vector) AS vsim "
                    "FROM em_history.messages m JOIN em_history.blocks b ON b.block_id = m.block_id "
                    f"WHERE m.embedding IS NOT NULL AND {_VISIBLE_BLOCK_SQL} "
                    + proj_sql
                    + block_sql
                    + "ORDER BY b.block_id, m.embedding <=> %s::vector"
                    ") sub ORDER BY sub.vsim DESC LIMIT %s",
                    mvec_params,
                ).fetchall()
                for r_idx, (bid, title, content, pjson, _) in enumerate(mvec_rows, start=1):
                    _acc(str(bid), str(title or ""), str(content or "")[:200], str(pjson or "[]"), r_idx, weight=0.9)

        ranked = sorted(scores.values(), key=lambda x: (-x["score"], x["block_id"]))
        return ranked[:top_n]

    def manage(
        self,
        action: str,
        *,
        kind: str = "block",
        project: str = "",
        key: str = "",
        title: str = "",
        request_id: str = "",
    ) -> dict[str, Any]:
        act = action.strip().lower()
        if act == "trash":
            with self._cfg.connect(self._dbname, role_kind="reader") as conn:
                rows = conn.execute(
                    "SELECT kind, project, key, title, deleted_at FROM em_history.management_trash ORDER BY deleted_at DESC"
                ).fetchall()
            return {
                "ok": True,
                "enabled": True,
                "items": [{"kind": r[0], "project": r[1], "key": r[2], "title": r[3], "deleted_at": r[4]} for r in rows],
            }
        if act not in ("delete", "restore"):
            raise ValueError(f"unsupported_manage_action:{action!r}")

        req_id = request_id.strip() if request_id and request_id.strip() else f"mgmt-{uuid.uuid4().hex}"
        fp = hashlib.sha256(json.dumps({"action": act, "kind": kind, "project": project, "key": key}, sort_keys=True).encode()).hexdigest()
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._cfg.connect(self._dbname, role_kind="writer") as conn:
            prev = conn.execute(
                "SELECT fingerprint, result FROM em_history.management_operations WHERE request_id = %s",
                (req_id,),
            ).fetchone()
            if prev:
                if prev[0] != fp:
                    raise ValueError("management_request_id_conflict")
                return json.loads(prev[1])

            if act == "delete":
                conn.execute(
                    "INSERT INTO em_history.management_trash(kind, project, key, title, deleted_at) "
                    "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (kind, project, key) DO NOTHING",
                    (kind, project, key, title, now_iso),
                )
            else:
                conn.execute(
                    "DELETE FROM em_history.management_trash WHERE kind = %s AND project = %s AND key = %s",
                    (kind, project, key),
                )
            res = {"ok": True, "action": act, "kind": kind, "project": project, "key": key, "request_id": req_id}
            res_json = json.dumps(res, ensure_ascii=False)
            conn.execute(
                "INSERT INTO em_history.management_operations(request_id, fingerprint, result, created_at) "
                "VALUES (%s, %s, %s, %s)",
                (req_id, fp, res_json, now_iso),
            )
            conn.execute(
                "INSERT INTO em_runtime.cutover_write_journal(library_key, request_id, actor_id, op_type, target_table, target_key, payload) "
                "VALUES (%s, %s, 'console', %s, 'management_trash', %s, %s::jsonb) ON CONFLICT DO NOTHING",
                (
                    self._dbname, req_id, f"history_manage_{act}",
                    f"{kind}:{project}:{key}",
                    json.dumps({"kind": kind, "project": project, "key": key, "title": title, "deleted_at": now_iso}),
                ),
            )
            conn.commit()
        return res

"""PG-backed candidate history reader, hybrid FTS+trigram+vector search, and delete/restore state."""
from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from evolvmem.history_pg.config import PgHistoryConfig, load_pg_history_config


def _vec_literal(vec: list[float] | tuple[float, ...]) -> str:
    return "[" + ",".join(f"{float(x):.6f}" for x in vec) + "]"


class PgHistoryCandidateReader:
    """Drop-in PostgreSQL replacement for SQLite HistoryCandidateReader & ManagedReader."""

    def __init__(self, pg_config: PgHistoryConfig | None = None, *, dbname: str | None = None, user_id: str = "jiangli", embedder: Any = None) -> None:
        self._cfg = pg_config if pg_config is not None else load_pg_history_config()
        self._dbname = dbname or self._cfg.resolve_user_history_db(user_id)
        self._embedder = embedder

    def __enter__(self) -> PgHistoryCandidateReader:
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def close(self) -> None:
        return None

    def block_visible(self, block_id: str) -> bool:
        with self._cfg.connect(self._dbname) as conn:
            row = conn.execute(
                "SELECT 1 FROM em_history.management_trash WHERE kind = 'block' AND key = %s LIMIT 1",
                (block_id,),
            ).fetchone()
            return row is None

    def visible_block_count(self) -> int:
        with self._cfg.connect(self._dbname) as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM em_history.blocks b "
                "WHERE b.message_count > 0 AND NOT EXISTS ("
                "SELECT 1 FROM em_history.management_trash t WHERE t.kind = 'block' AND t.key = b.block_id)"
            ).fetchone()
            return int(row[0] or 0) if row else 0

    def projects(self) -> dict[str, Any]:
        with self._cfg.connect(self._dbname) as conn:
            trashed_proj = {
                r[0] for r in conn.execute("SELECT project FROM em_history.management_trash WHERE kind = 'project'").fetchall()
            }
            trashed_blk = {
                r[0] for r in conn.execute("SELECT key FROM em_history.management_trash WHERE kind = 'block'").fetchall()
            }
            rows = conn.execute(
                "SELECT p.project, p.display_name, p.status, p.block_count, p.message_count, "
                "p.turn_count, p.attachment_count, p.first_at, p.last_at, p.summary "
                "FROM em_history.projects p ORDER BY p.last_at DESC, p.project"
            ).fetchall()
            bp_rows = conn.execute(
                "SELECT bp.project, bp.block_id FROM em_history.block_projects bp"
            ).fetchall()

        vis_by_proj: dict[str, int] = {}
        for proj, bid in bp_rows:
            if bid not in trashed_blk:
                vis_by_proj[str(proj)] = vis_by_proj.get(str(proj), 0) + 1

        out = []
        for r in rows:
            proj = str(r[0])
            if proj in trashed_proj:
                continue
            out.append({
                "project": proj,
                "display_name": r[1] or proj,
                "status": r[2],
                "block_count": vis_by_proj.get(proj, int(r[3] or 0)),
                "message_count": int(r[4] or 0),
                "turn_count": int(r[5] or 0),
                "attachment_count": int(r[6] or 0),
                "first_at": r[7],
                "last_at": r[8],
                "summary": r[9],
            })
        return {"ok": True, "projects": out}

    def unassigned(self) -> dict[str, Any]:
        with self._cfg.connect(self._dbname) as conn:
            rows = conn.execute(
                "SELECT b.block_id, b.title, b.adapter, b.message_count, b.observed_first_at, b.observed_last_at "
                "FROM em_history.blocks b WHERE NOT EXISTS ("
                "SELECT 1 FROM em_history.block_projects bp WHERE bp.block_id = b.block_id) "
                "AND NOT EXISTS ("
                "SELECT 1 FROM em_history.management_trash t WHERE t.kind = 'block' AND t.key = b.block_id) "
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
        with self._cfg.connect(self._dbname) as conn:
            prow = conn.execute(
                "SELECT project, display_name, status, summary, first_at, last_at, intro_json "
                "FROM em_history.projects WHERE project = %s",
                (proj,),
            ).fetchone()
            if not prow:
                return {"ok": False, "error": "project_not_found"}
            brows = conn.execute(
                "SELECT b.block_id, b.title, b.adapter, b.message_count, b.turn_count, b.observed_first_at, b.observed_last_at "
                "FROM em_history.blocks b JOIN em_history.block_projects bp ON bp.block_id = b.block_id "
                "WHERE bp.project = %s AND NOT EXISTS ("
                "SELECT 1 FROM em_history.management_trash t WHERE t.kind = 'block' AND t.key = b.block_id) "
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
        with self._cfg.connect(self._dbname) as conn:
            row = conn.execute(
                "SELECT block_id, adapter, kind, title, message_count, turn_count, "
                "observed_first_at, observed_last_at, projects, block_json "
                "FROM em_history.blocks WHERE block_id = %s",
                (bid,),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "block_not_found"}
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
        with self._cfg.connect(self._dbname) as conn:
            msgs = conn.execute(
                "SELECT ordinal, role, content, recorded_at, attachments "
                "FROM em_history.messages WHERE block_id = %s ORDER BY ordinal LIMIT %s OFFSET %s",
                (bid, max(1, int(limit)), max(0, int(offset))),
            ).fetchall()
        return {
            "ok": True,
            "block_id": bid,
            "messages": [
                {"ordinal": m[0], "role": m[1], "content": m[2], "recorded_at": m[3], "attachments": json.loads(m[4] or "[]")}
                for m in msgs
            ],
        }

    def attachment(self, sha256: str) -> dict[str, Any] | None:
        with self._cfg.connect(self._dbname) as conn:
            row = conn.execute(
                "SELECT sha256, mime_type, bytes, path, format_ok, width, height FROM em_history.attachments WHERE sha256 = %s",
                (sha256,),
            ).fetchone()
        if not row:
            return None
        p = Path(row[3]) if row[3] else None
        if p is not None and p.is_file():
            mime = row[1] or (mimetypes.guess_type(str(p))[0] or "application/octet-stream")
            return {"sha256": row[0], "mime_type": mime, "bytes": int(row[2] or p.stat().st_size), "path": str(p)}
        return {"sha256": row[0], "mime_type": row[1] or "application/octet-stream", "bytes": int(row[2] or 0), "path": row[3]}

    def coverage_report(self, report_key: str) -> dict[str, Any]:
        with self._cfg.connect(self._dbname) as conn:
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
    ) -> list[dict[str, Any]]:
        """Hybrid search fusing PG tsvector + pg_trgm + pgvector cosine via RRF."""
        q = (query or "").strip()
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

        with self._cfg.connect(self._dbname) as conn:
            if q:
                like_pat = f"%{q}%"
                proj_sql = "AND EXISTS (SELECT 1 FROM em_history.block_projects bp WHERE bp.block_id = b.block_id AND bp.project = %s) " if project else ""
                lex_params: tuple[Any, ...] = (q, q, like_pat, like_pat, q, q, like_pat, like_pat, q, q, q, q)
                if project:
                    lex_params += (project,)
                lex_params += (top_n * 3,)
                lex_rows = conn.execute(
                    "SELECT b.block_id, b.title, COALESCE(m.content, b.title) AS snip, b.projects, "
                    "GREATEST(similarity(b.title, %s), COALESCE(similarity(m.content, %s), 0)) "
                    "+ CASE WHEN b.title ILIKE %s OR COALESCE(m.content, '') ILIKE %s THEN 1.0 ELSE 0.0 END "
                    "+ ts_rank(b.search_tsv, plainto_tsquery('simple', %s)) "
                    "+ COALESCE(ts_rank(m.search_tsv, plainto_tsquery('simple', %s)), 0) AS lex_score "
                    "FROM em_history.blocks b "
                    "LEFT JOIN em_history.messages m ON m.block_id = b.block_id "
                    "WHERE NOT EXISTS (SELECT 1 FROM em_history.management_trash t WHERE t.kind = 'block' AND t.key = b.block_id) "
                    "AND (b.title ILIKE %s OR m.content ILIKE %s "
                    "OR b.title %% %s OR m.content %% %s "
                    "OR b.search_tsv @@ plainto_tsquery('simple', %s) "
                    "OR m.search_tsv @@ plainto_tsquery('simple', %s)) "
                    + proj_sql
                    + "ORDER BY lex_score DESC LIMIT %s",
                    lex_params,
                ).fetchall()
                seen_lex: set[str] = set()
                r_idx = 1
                for bid, title, snip, pjson, _ in lex_rows:
                    if bid in seen_lex:
                        continue
                    seen_lex.add(bid)
                    _acc(str(bid), str(title or ""), str(snip or "")[:200], str(pjson or "[]"), r_idx, weight=1.2)
                    r_idx += 1

            if query_vec:
                vlit = _vec_literal(query_vec)
                proj_sql = "AND EXISTS (SELECT 1 FROM em_history.block_projects bp WHERE bp.block_id = b.block_id AND bp.project = %s) " if project else ""
                vec_params: tuple[Any, ...] = (vlit,) + ((project,) if project else ()) + (vlit, top_n * 3)
                vec_rows = conn.execute(
                    "SELECT b.block_id, b.title, b.projects, 1.0 - (b.embedding <=> %s::vector) AS vsim "
                    "FROM em_history.blocks b "
                    "WHERE b.embedding IS NOT NULL "
                    "AND NOT EXISTS (SELECT 1 FROM em_history.management_trash t WHERE t.kind = 'block' AND t.key = b.block_id) "
                    + proj_sql
                    + "ORDER BY b.embedding <=> %s::vector LIMIT %s",
                    vec_params,
                ).fetchall()
                for r_idx, (bid, title, pjson, _) in enumerate(vec_rows, start=1):
                    _acc(str(bid), str(title or ""), "", str(pjson or "[]"), r_idx, weight=1.0)
                mvec_rows = conn.execute(
                    "SELECT b.block_id, b.title, m.content, b.projects, 1.0 - (m.embedding <=> %s::vector) AS vsim "
                    "FROM em_history.messages m JOIN em_history.blocks b ON b.block_id = m.block_id "
                    "WHERE m.embedding IS NOT NULL "
                    "AND NOT EXISTS (SELECT 1 FROM em_history.management_trash t WHERE t.kind = 'block' AND t.key = b.block_id) "
                    + proj_sql
                    + "ORDER BY m.embedding <=> %s::vector LIMIT %s",
                    vec_params,
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
            with self._cfg.connect(self._dbname) as conn:
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

        req_id = request_id or hashlib.sha256(f"{act}:{kind}:{project}:{key}".encode()).hexdigest()[:24]
        fp = hashlib.sha256(json.dumps({"action": act, "kind": kind, "project": project, "key": key}, sort_keys=True).encode()).hexdigest()
        now_iso = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        with self._cfg.connect(self._dbname) as conn:
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

"""PostgreSQL-backed HistoryReader and EvidenceVerifier (zero sqlite3 imports)."""
from __future__ import annotations

from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import re
from typing import Any

from evolvmem.experience_v2.pipeline.history_reader import (
    HistoryRawRecord,
    HistorySession,
    HistorySessionSummary,
)
from evolvmem.history_pg.config import PgHistoryConfig, load_pg_history_config

_PTR_SRC_RE = re.compile(r"^(archive|session_archive|block):([^\s#]+)(?:#(?:msg|L)=?(\d+))?$")


def _parse_iso(ts: Any) -> datetime | None:
    if not ts:
        return None
    s = str(ts).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _load_jsonl_records(path: Path) -> tuple[list[HistoryRawRecord], bytes]:
    if not path.is_file():
        return [], b""
    raw_bytes = path.read_bytes()
    opener = gzip.open if path.name.endswith(".gz") else open
    records: list[HistoryRawRecord] = []
    with opener(path, "rt", encoding="utf-8", errors="replace") as fh:
        idx = 0
        for line in fh:
            raw = line.strip()
            if not raw:
                continue
            idx += 1
            try:
                ev = json.loads(raw)
            except ValueError:
                ev = {"text": raw}
            if not isinstance(ev, dict):
                ev = {"value": ev}
            role = str(ev.get("role") or ev.get("type") or "unknown")
            text = str(ev.get("content") or ev.get("text") or "")
            records.append(HistoryRawRecord(event_index=idx, role=role, content=text, raw_event=ev))
    return records, raw_bytes


class PgHistoryReader:
    """HistoryReader Protocol implementation over PG em_runtime + em_history + CAS blobs."""

    def __init__(self, pg_config: PgHistoryConfig | None = None, *, dbname: str | None = None, user_id: str = "jiangli", library: str = "personal") -> None:
        self._cfg = pg_config if pg_config is not None else load_pg_history_config()
        self._dbname = dbname or self._cfg.resolve_user_history_db(user_id)
        self._library = library

    def _resolve_blob_path(self, cas_sha: str, payload_path: str) -> Path | None:
        if cas_sha:
            for suffix in (".jsonl", ".jsonl.gz"):
                cand = self._cfg.cas_root / cas_sha[:2] / f"{cas_sha}{suffix}"
                if cand.is_file():
                    return cand
        if payload_path:
            p = Path(payload_path)
            if p.is_file():
                return p
        return None

    def list_sessions(
        self,
        *,
        project: str | None = None,
        adapter: str | None = None,
        since: str | None = None,
        limit: int = 100,
        offset: int = 0,
        dedupe_logical_sessions: bool = True,
    ) -> list[HistorySessionSummary]:
        clauses = ["a.state = 'available'"]
        params: list[Any] = []
        if project:
            clauses.append("a.project = %s")
            params.append(project)
        if adapter:
            clauses.append("a.adapter = %s")
            params.append(adapter)
        since_dt = _parse_iso(since)
        where_sql = " AND ".join(clauses)
        params.extend([max(1, int(limit)), max(0, int(offset))])
        sql_txt = (
            "SELECT a.id, a.project, a.adapter, a.external_session_id, a.created_at, a.payload_path, a.cas_blob_sha256, "
            "COALESCE(u.source_format, CASE WHEN a.adapter='dsh' THEN 'dsh-jsonl' ELSE a.adapter || '-jsonl' END) "
            "FROM em_runtime.session_archives a "
            "LEFT JOIN em_runtime.lan_session_uploads u ON u.archive_id = a.id "
            f"WHERE {where_sql} ORDER BY a.created_at DESC, a.id DESC LIMIT %s OFFSET %s"
        )
        out: list[HistorySessionSummary] = []
        seen_logical: set[tuple[str, str]] = set()
        with self._cfg.connect(self._dbname) as conn:
            for row in conn.execute(sql_txt, params).fetchall():
                aid, proj, adp, ext_id, created_at, pay_path, cas_sha, src_fmt = row
                if since_dt is not None:
                    cdt = _parse_iso(created_at)
                    if cdt is not None and cdt < since_dt:
                        continue
                if dedupe_logical_sessions and (adp, ext_id) in seen_logical:
                    continue
                seen_logical.add((adp, ext_id))
                has_blob = self._resolve_blob_path(str(cas_sha or ""), str(pay_path or "")) is not None
                out.append(HistorySessionSummary(
                    session_id=str(ext_id),
                    adapter=str(adp),
                    source_format=str(src_fmt or "codex-jsonl"),
                    project=str(proj),
                    source_ref=f"archive:{aid}",
                    library=self._library,
                    external_session_id=str(ext_id),
                    created_at=str(created_at),
                    has_raw_bytes=has_blob,
                ))
        return out

    def get_session(self, source_ref: str) -> HistorySession | None:
        if source_ref.startswith(("archive:", "session_archive:")):
            aid_str = source_ref.split(":", 1)[1].split("#", 1)[0]
            try:
                aid = int(aid_str)
            except ValueError:
                return None
            with self._cfg.connect(self._dbname) as conn:
                row = conn.execute(
                    "SELECT id, project, adapter, external_session_id, payload_path, cas_blob_sha256, created_at "
                    "FROM em_runtime.session_archives WHERE id = %s AND state = 'available'",
                    (aid,),
                ).fetchone()
                if not row:
                    return None
                _, proj, adp, ext_id, pay_path, cas_sha, created_at = row
                blob = self._resolve_blob_path(str(cas_sha or ""), str(pay_path or ""))
                records, raw_bytes = _load_jsonl_records(blob) if blob is not None else ([], b"")
                if not records:
                    ch_row = conn.execute(
                        "SELECT messages FROM em_runtime.conversation_history WHERE archive_id = %s",
                        (aid,),
                    ).fetchone()
                    if ch_row and ch_row[0]:
                        try:
                            msgs = json.loads(ch_row[0])
                            if isinstance(msgs, list):
                                records = [
                                    HistoryRawRecord(
                                        event_index=i,
                                        role=str(m.get("role") or "unknown") if isinstance(m, dict) else "unknown",
                                        content=str(m.get("content") or "") if isinstance(m, dict) else str(m),
                                        raw_event=m if isinstance(m, dict) else {"content": m},
                                    )
                                    for i, m in enumerate(msgs, start=1)
                                ]
                        except ValueError:
                            records = []
            summ = HistorySessionSummary(
                session_id=str(ext_id),
                adapter=str(adp),
                source_format=f"{adp}-jsonl",
                project=str(proj),
                source_ref=f"archive:{aid}",
                library=self._library,
                external_session_id=str(ext_id),
                created_at=str(created_at),
                has_raw_bytes=bool(raw_bytes),
            )
            return HistorySession(summary=summ, records=tuple(records), raw_bytes=raw_bytes)

        if source_ref.startswith("block:"):
            bid = source_ref.split(":", 1)[1].split("#", 1)[0]
            with self._cfg.connect(self._dbname) as conn:
                brow = conn.execute(
                    "SELECT block_id, adapter, native_session_id, observed_first_at, projects "
                    "FROM em_history.blocks WHERE block_id = %s",
                    (bid,),
                ).fetchone()
                if not brow:
                    return None
                mrows = conn.execute(
                    "SELECT ordinal, role, content, recorded_at FROM em_history.messages WHERE block_id = %s ORDER BY ordinal",
                    (bid,),
                ).fetchall()
            projs = json.loads(brow[4] or "[]") if brow[4] else []
            records = [
                HistoryRawRecord(
                    event_index=int(ord_i) + 1,
                    role=str(role),
                    content=str(content),
                    raw_event={"ordinal": ord_i, "role": role, "content": content, "recorded_at": rec_at},
                )
                for ord_i, role, content, rec_at in mrows
            ]
            summ = HistorySessionSummary(
                session_id=str(brow[2] or bid),
                adapter=str(brow[1] or ""),
                source_format="candidate-block",
                project=str(projs[0]) if projs else "",
                source_ref=f"block:{bid}",
                library=self._library,
                external_session_id=str(brow[2] or bid),
                created_at=str(brow[3] or ""),
                has_raw_bytes=False,
            )
            return HistorySession(summary=summ, records=tuple(records), raw_bytes=b"")
        return None

    def check_pointer_with_reason(self, pointer: dict[str, Any]) -> tuple[bool, str]:
        if not isinstance(pointer, dict):
            return False, "invalid_evidence_pointer"
        src_ref = str(pointer.get("source_ref") or "").strip()
        line_no = pointer.get("line_no")
        if not src_ref and pointer.get("source"):
            m = _PTR_SRC_RE.match(str(pointer["source"]).strip())
            if m:
                kind, ident, idx = m.groups()
                src_ref = f"{'archive' if kind in ('archive', 'session_archive') else 'block'}:{ident}"
                if line_no is None and idx is not None:
                    line_no = int(idx)
        if not src_ref:
            return False, "missing_source_ref"
        sess = self.get_session(src_ref)
        if sess is None:
            return False, f"session_not_found:{src_ref}"
        if line_no is not None:
            try:
                num = int(line_no)
            except (TypeError, ValueError):
                return False, "invalid_line_no"
            if num < 1 or num > len(sess.records):
                return False, f"evidence_line_out_of_range:{num}>{len(sess.records)}"
        return True, "ok"

    def verify_evidence_pointer(self, pointer: dict[str, Any]) -> bool:
        ok, _ = self.check_pointer_with_reason(pointer)
        return ok


class PgHistoryEvidenceVerifier:
    """EvidenceVerifier Protocol over PgHistoryReader with detailed mismatch reasons."""

    def __init__(self, reader: PgHistoryReader | None = None) -> None:
        self._reader = reader if reader is not None else PgHistoryReader()

    def verify_all(self, evidence: list[dict[str, Any]]) -> tuple[bool, str]:
        if not isinstance(evidence, list) or not evidence:
            return False, "missing_history_evidence"
        for ptr in evidence:
            ok, reason = self._reader.check_pointer_with_reason(ptr)
            if not ok:
                return False, reason
        return True, "ok"

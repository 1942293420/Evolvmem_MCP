"""PostgreSQL-backed HistoryReader and EvidenceVerifier (zero sqlite3 imports)."""
from __future__ import annotations

from datetime import datetime, timezone
import gzip
import json
import os
from pathlib import Path
import re
from typing import Any

from evolvmem.experience_v2.pipeline.history_reader import (
    HistoryRawRecord,
    HistorySession,
    HistorySessionSummary,
)
from evolvmem.archive_storage import ArchiveStorage, MAGIC as _ARCHIVE_MAGIC
from evolvmem.config import Config
from evolvmem.history_pg.config import PgHistoryConfig, load_pg_history_config

_PTR_SRC_RE = re.compile(r"^(archive|session_archive|block):([^\s#]+)(?:#(?:msg|L)=?(\d+))?$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_TRASH_PROJ_SQL = "COALESCE(NULLIF(tp.project, ''), tp.key)"
_VISIBLE_BLOCK_SQL = (
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


def _flatten_content(val: Any) -> str:
    if isinstance(val, str):
        return val
    if isinstance(val, list):
        parts: list[str] = []
        for item in val:
            if isinstance(item, dict):
                txt = item.get("text") or item.get("content") or item.get("input_text") or ""
                if txt:
                    parts.append(str(txt))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    if isinstance(val, dict):
        return str(val.get("text") or val.get("content") or "")
    return "" if val is None else str(val)


def _extract_role_and_text(ev: dict[str, Any]) -> tuple[str, str]:
    role = str(ev.get("role") or "")
    text = _flatten_content(ev.get("content") if ev.get("content") is not None else ev.get("text"))
    payload = ev.get("payload")
    if isinstance(payload, dict):
        msg = payload.get("message") if isinstance(payload.get("message"), dict) else None
        if not role:
            role = str((msg or {}).get("role") or payload.get("role") or payload.get("type") or "")
        if not text:
            src = (msg or {}).get("content") if msg is not None else (
                payload.get("content") if payload.get("content") is not None else (payload.get("text") or payload.get("summary"))
            )
            text = _flatten_content(src)
    for container_key in ("data", "item", "message"):
        sub = ev.get(container_key)
        if isinstance(sub, dict):
            if not role:
                role = str(sub.get("role") or sub.get("type") or "")
            if not text:
                text = _flatten_content(sub.get("content") if sub.get("content") is not None else sub.get("text"))
    if not role:
        role = str(ev.get("type") or "unknown")
    return role, text


def _find_archive_key_dir(path: Path, data_dir: Path | None) -> Path | None:
    if data_dir is not None and (data_dir / "archive.key").is_file():
        return data_dir
    for anc in path.parents:
        if (anc / "archive.key").is_file():
            return anc
    return None


def _parse_lines_to_records(text: str) -> list[HistoryRawRecord]:
    records: list[HistoryRawRecord] = []
    idx = 0
    for line in text.splitlines():
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
        role, msg_text = _extract_role_and_text(ev)
        records.append(HistoryRawRecord(event_index=idx, role=role, content=msg_text, raw_event=ev))
    return records


def _load_jsonl_records(path: Path, *, data_dir: Path | None = None) -> tuple[list[HistoryRawRecord], bytes]:
    if not path.is_file():
        return [], b""
    raw_bytes = path.read_bytes()
    envelope_conv: Any = None
    if raw_bytes.startswith(_ARCHIVE_MAGIC):
        key_dir = _find_archive_key_dir(path, data_dir)
        if key_dir is None:
            return [], raw_bytes
        key = (key_dir / "archive.key").read_bytes()
        if len(key) != 32:
            return [], raw_bytes
        unpacked = ArchiveStorage(Config(data_dir=key_dir, apply_environment=False), key).unpack(raw_bytes)
        text = unpacked
        raw_bytes = unpacked.encode("utf-8")
        stripped = unpacked.lstrip()
        if stripped.startswith("{") and "\n" not in unpacked.strip():
            try:
                env = json.loads(unpacked)
                if isinstance(env, dict) and isinstance(env.get("transcript"), str):
                    envelope_conv = env.get("conversation") or env.get("messages")
                    text = env["transcript"]
            except ValueError:
                pass
    elif raw_bytes[:2] == b"\x1f\x8b" or path.name.endswith(".gz"):
        text = gzip.decompress(raw_bytes).decode("utf-8", errors="replace")
    else:
        text = raw_bytes.decode("utf-8", errors="replace")

    records = _parse_lines_to_records(text)
    if not records and isinstance(envelope_conv, list):
        records = [
            HistoryRawRecord(
                event_index=i, role=str(m.get("role") or "unknown") if isinstance(m, dict) else "unknown",
                content=str(m.get("content") or "") if isinstance(m, dict) else str(m),
                raw_event=m if isinstance(m, dict) else {"content": m},
            )
            for i, m in enumerate(envelope_conv, start=1)
        ]
    return records, raw_bytes


class PgHistoryReader:
    """HistoryReader Protocol implementation over PG em_runtime + em_history + CAS blobs."""

    def __init__(
        self,
        pg_config: PgHistoryConfig | None = None,
        *,
        dbname: str | None = None,
        user_id: str = "",
        library: str = "personal",
        data_dir: Path | str | None = None,
    ) -> None:
        self._cfg = pg_config if pg_config is not None else load_pg_history_config()
        self._dbname = dbname or self._cfg.resolve_user_history_db(user_id)
        self._library = library
        dd_raw = data_dir if data_dir is not None else os.environ.get("EVOLVMEM_DATA_DIR")
        self._data_dir = Path(dd_raw).expanduser().resolve() if dd_raw else None

    def _resolve_blob_path(self, cas_sha: str, payload_path: str) -> Path | None:
        sha = (cas_sha or "").strip().lower()
        if sha and _HEX64_RE.match(sha):
            for base_dir in (
                self._cfg.cas_root / sha[:2] / sha[2:4],
                self._cfg.cas_root / sha[:2],
            ):
                for suffix in (".jsonl", ".jsonl.gz", "", ".gz"):
                    cand = base_dir / f"{sha}{suffix}"
                    if cand.is_file():
                        return cand
        if payload_path:
            p = Path(payload_path)
            if p.is_file():
                return p
            if not p.is_absolute() and self._data_dir is not None:
                rel_p = (self._data_dir / p).resolve()
                if rel_p.is_relative_to(self._data_dir) and rel_p.is_file():
                    return rel_p
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
        clauses = [
            "a.state = 'available'",
            f"NOT EXISTS (SELECT 1 FROM em_history.management_trash tp WHERE tp.kind = 'project' AND {_TRASH_PROJ_SQL} = a.project)",
            "NOT EXISTS (SELECT 1 FROM em_history.management_trash tb WHERE tb.kind = 'block' AND tb.key = ('archive:' || a.id::text))",
        ]
        params: list[Any] = []
        if project:
            clauses.append("a.project = %s")
            params.append(project)
        if adapter:
            clauses.append("a.adapter = %s")
            params.append(adapter)
        since_dt = _parse_iso(since)
        if since_dt is not None:
            clauses.append("(a.created_at <> '' AND a.created_at::timestamptz >= %s)")
            params.append(since_dt)
        where_sql = " AND ".join(clauses)
        base_select = (
            "SELECT a.id, a.project, a.adapter, a.external_session_id, a.created_at, a.payload_path, a.cas_blob_sha256, "
            "COALESCE(u.source_format, CASE WHEN a.adapter='dsh' THEN 'dsh-jsonl' ELSE a.adapter || '-jsonl' END) AS src_fmt "
            "FROM em_runtime.session_archives a "
            "LEFT JOIN LATERAL ("
            "  SELECT lu.source_format FROM em_runtime.lan_session_uploads lu "
            "  WHERE lu.archive_id = a.id ORDER BY lu.received_at DESC LIMIT 1"
            ") u ON true "
            f"WHERE {where_sql}"
        )
        if dedupe_logical_sessions:
            dedup_inner = (
                "SELECT DISTINCT ON (sub.adapter, sub.external_session_id) * FROM ("
                + base_select +
                ") sub ORDER BY sub.adapter, sub.external_session_id, sub.created_at DESC, sub.id DESC"
            )
            sql_txt = (
                "SELECT * FROM (" + dedup_inner + ") d "
                "ORDER BY d.created_at DESC, d.id DESC LIMIT %s OFFSET %s"
            )
        else:
            sql_txt = base_select + " ORDER BY a.created_at DESC, a.id DESC LIMIT %s OFFSET %s"
        params.extend([max(1, int(limit)), max(0, int(offset))])

        out: list[HistorySessionSummary] = []
        with self._cfg.connect(self._dbname, role_kind="reader") as conn:
            for row in conn.execute(sql_txt, params).fetchall():
                aid, proj, adp, ext_id, created_at, pay_path, cas_sha, src_fmt = row
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
            with self._cfg.connect(self._dbname, role_kind="reader") as conn:
                row = conn.execute(
                    "SELECT a.id, a.project, a.adapter, a.external_session_id, a.payload_path, a.cas_blob_sha256, a.created_at "
                    "FROM em_runtime.session_archives a WHERE a.id = %s AND a.state = 'available' "
                    f"AND NOT EXISTS (SELECT 1 FROM em_history.management_trash tp WHERE tp.kind = 'project' AND {_TRASH_PROJ_SQL} = a.project) "
                    "AND NOT EXISTS (SELECT 1 FROM em_history.management_trash tb WHERE tb.kind = 'block' AND tb.key = ('archive:' || a.id::text))",
                    (aid,),
                ).fetchone()
                if not row:
                    return None
                _, proj, adp, ext_id, pay_path, cas_sha, created_at = row
                blob = self._resolve_blob_path(str(cas_sha or ""), str(pay_path or ""))
                records, raw_bytes = _load_jsonl_records(blob, data_dir=self._data_dir) if blob is not None else ([], b"")
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
            with self._cfg.connect(self._dbname, role_kind="reader") as conn:
                brow = conn.execute(
                    "SELECT b.block_id, b.adapter, b.native_session_id, b.observed_first_at, b.projects "
                    f"FROM em_history.blocks b WHERE b.block_id = %s AND {_VISIBLE_BLOCK_SQL}",
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
        if reader is not None:
            self._reader = reader
        else:
            dbname = str(os.environ.get("EVOLVMEM_HISTORY_PG_DB") or "").strip() or None
            uid = str(os.environ.get("EVOLVMEM_CONSOLE_OWNER_ID") or "").strip()
            self._reader = PgHistoryReader(dbname=dbname, user_id=uid)

    def verify_all(self, evidence: list[dict[str, Any]]) -> tuple[bool, str]:
        if not isinstance(evidence, list) or not evidence:
            return False, "missing_history_evidence"
        for ptr in evidence:
            ok, reason = self._reader.check_pointer_with_reason(ptr)
            if not ok:
                return False, reason
        return True, "ok"

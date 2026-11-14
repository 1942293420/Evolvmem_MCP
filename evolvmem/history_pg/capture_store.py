"""PG-backed chunked capture intake, 0600 atomic CAS storage, and status/assign/retry."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from types import SimpleNamespace
from typing import Any
import uuid

import psycopg
from psycopg.rows import dict_row

from evolvmem.archive_storage import ArchiveStorage, MAGIC as ARCHIVE_MAGIC
from evolvmem.history_pg.config import PgHistoryConfig, PgHistoryPool
from evolvmem.lan_sharing import LanError, validate_request_id
from evolvmem.product_policy import load_policy
from evolvmem.transcript_formats import CODEX_FORMAT, adapter_name, parse_transcript, source_format, workspace_details

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_EXCLUDED_REASONS = frozenset({"subagent_session"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_0600(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        tmp.unlink(missing_ok=True)


def _sync_dir(path: Path) -> None:
    try:
        dfd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


class PgCaptureStore:
    """Handles session archive intake and status strictly via PostgreSQL + CAS."""

    def __init__(self, config: PgHistoryConfig, *, pool: PgHistoryPool | None = None, data_dir: Path | str | None = None) -> None:
        self.config = config
        self.pool = pool or PgHistoryPool(config)
        self.cas_root = Path(config.cas_root)
        self.cas_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        raw_dir = str(data_dir).strip() if data_dir is not None else ""
        if not raw_dir:
            raw_dir = str(os.environ.get("EVOLVMEM_DATA_DIR") or "").strip()
        self.data_dir: Path | None = Path(raw_dir).expanduser() if raw_dir else None

    def _read_existing_archive(self, raw_path: str) -> bytes:
        raw_str = str(raw_path or "").strip()
        if not raw_str:
            raise LanError("archive_payload_unavailable")
        cand = Path(raw_str).expanduser()
        if ".." in cand.parts:
            raise LanError("archive_payload_unavailable")
        if cand.is_absolute():
            if cand.is_symlink():
                raise LanError("archive_payload_unavailable")
            allowed = [self.cas_root.resolve()] + ([self.data_dir.resolve()] if self.data_dir else [])
            target = cand.resolve()
            if not target.is_file() or not any(target.is_relative_to(root) for root in allowed):
                raise LanError("archive_payload_unavailable")
        else:
            if self.data_dir is None:
                raise LanError("archive_payload_unavailable")
            cur = self.data_dir
            for part in cand.parts:
                cur = cur / part
                if cur.is_symlink():
                    raise LanError("archive_payload_unavailable")
            data_root = self.data_dir.resolve()
            target = (self.data_dir / cand).resolve()
            if not target.is_relative_to(data_root) or not target.is_file():
                raise LanError("archive_payload_unavailable")
        blob = target.read_bytes()
        if not blob.startswith(ARCHIVE_MAGIC):
            return blob
        key_dir = self.data_dir if (self.data_dir is not None and (self.data_dir / "archive.key").is_file()) else None
        if key_dir is None:
            key_dir = next((p for p in target.parents if (p / "archive.key").is_file()), None)
        if key_dir is None:
            raise LanError("archive_payload_unavailable")
        try:
            key = (key_dir / "archive.key").read_bytes()
            if len(key) != 32:
                raise LanError("archive_payload_unavailable")
            unpacked = ArchiveStorage(SimpleNamespace(data_dir=key_dir), key).unpack(blob)
        except (ValueError, OSError) as exc:
            raise LanError("archive_payload_unavailable") from exc
        if unpacked.lstrip().startswith("{"):
            try:
                env = json.loads(unpacked)
                if isinstance(env, dict) and isinstance(env.get("transcript"), str):
                    return env["transcript"].encode("utf-8")
            except ValueError:
                pass
        return unpacked.encode("utf-8")

    def _stage_path(self, device_id: str, session_id: str, sha256: str) -> Path:
        key_hash = hashlib.sha256(f"{device_id}|{session_id}|{sha256}".encode("utf-8")).hexdigest()
        return self.cas_root / "staging" / f"{key_hash}.part"

    def _cas_path(self, sha256: str) -> Path:
        return self.cas_root / sha256[:2] / sha256[2:4] / f"{sha256}.jsonl"

    @staticmethod
    def _identity(args: dict[str, Any]) -> tuple[str, str]:
        dev = str(args.get("device_id") or "").strip()
        sid = str(args.get("session_id") or "").strip()
        if not dev or len(dev) > 64 or not sid or len(sid) > 128:
            raise LanError("invalid_arguments")
        return dev, sid

    @staticmethod
    def _receipt(row: dict[str, Any], head: dict[str, Any] | None, *, status: str | None = None) -> dict[str, Any]:
        policy = load_policy()
        is_curr = False
        if row.get("archive_id") and head is not None:
            is_curr = head.get("sha256") == row["sha256"]
        res: dict[str, Any] = {
            "status": status or ("archived" if row.get("archive_id") else "receiving"),
            "next_offset": int(row["received_bytes"]),
            "total_bytes": int(row["total_bytes"]),
            "source_sha256": row["sha256"],
            "source_order": row["source_order"],
            "source_format": row["source_format"],
            "current": is_curr,
            "extraction_status": row["extraction_status"],
            "project": row["project"],
            "attribution_reason": row["attribution_reason"],
            "backfill_status": row["backfill_status"],
            "ai_processing_enabled": bool(policy.ai_processing_enabled),
        }
        if not res["ai_processing_enabled"] and row["extraction_status"] in ("pending", "processing", "not_requested", "unassigned"):
            res["processing_state"] = "paused"
            res["processing_reason"] = "ai_processing_disabled"
        if row.get("archive_id"):
            res["archive_id"] = int(row["archive_id"])
            res["source_revision"] = row["sha256"]
        if row.get("error"):
            res["processing_error"] = row["error"]
        return res

    @staticmethod
    def _record_journal(
        conn: psycopg.Connection,
        *,
        library_key: str,
        request_id: str,
        actor_id: str,
        op_type: str,
        target_table: str,
        target_key: str,
        payload: dict[str, Any],
    ) -> None:
        conn.execute(
            "INSERT INTO em_runtime.cutover_write_journal (library_key, request_id, actor_id, op_type, target_table, target_key, payload) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb) ON CONFLICT (library_key, request_id, op_type, target_key) DO UPDATE SET "
            "payload=EXCLUDED.payload, replayed_to_sqlite=false, created_at=clock_timestamp()",
            (library_key, request_id, actor_id, op_type, target_table, target_key, json.dumps(payload, ensure_ascii=False)),
        )

    def upload(self, user_id: str, args: dict[str, Any], *, statement_timeout_ms: int | None = None) -> dict[str, Any]:
        dev_id, sess_id = self._identity(args)
        digest = str(args.get("sha256") or "")
        if not _SHA256_RE.match(digest):
            raise LanError("invalid_transcript_hash")
        fmt = source_format(args.get("source_format", CODEX_FORMAT))
        total = args.get("total_bytes")
        offset = args.get("offset")
        if type(total) is not int or total < 1 or type(offset) is not int or offset < 0:
            raise LanError("invalid_chunk")
        req_id = str(args.get("request_id") or "")
        validate_request_id(req_id)
        order = args.get("source_order")
        decl_curr = args.get("current_sha256")
        if order is None and decl_curr is not None:
            raise LanError("invalid_source_order")
        if order is not None and (type(order) is not int or order < 1):
            raise LanError("invalid_source_order")
        if decl_curr is not None and (not isinstance(decl_curr, str) or not _SHA256_RE.match(decl_curr)):
            raise LanError("invalid_current_sha256")
        try:
            chunk = base64.b64decode(args.get("content_b64", ""), validate=True)
        except (ValueError, TypeError):
            raise LanError("invalid_chunk") from None
        if not chunk or len(chunk) > 262144 or offset + len(chunk) > total:
            raise LanError("invalid_chunk")

        payload_hash = hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        dbname = self.config.resolve_user_history_db(user_id)
        unlink_stage: Path | None = None
        with self.pool.connection(dbname, role_kind="writer", statement_timeout_ms=statement_timeout_ms) as conn:
            conn.row_factory = dict_row
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))", (dev_id, sess_id))
            prev_req = conn.execute(
                "SELECT payload_hash, result FROM em_runtime.lan_write_requests WHERE user_id=%s AND tool='session_archive_upload' AND request_id=%s",
                (user_id, req_id),
            ).fetchone()
            if prev_req is not None and prev_req["payload_hash"] == payload_hash:
                return json.loads(prev_req["result"])

            row = conn.execute(
                "SELECT * FROM em_runtime.lan_session_uploads WHERE device_id=%s AND session_id=%s AND sha256=%s FOR UPDATE",
                (dev_id, sess_id, digest),
            ).fetchone()
            head = conn.execute(
                "SELECT * FROM em_runtime.lan_session_heads WHERE device_id=%s AND session_id=%s FOR UPDATE",
                (dev_id, sess_id),
            ).fetchone()
            declared_proj = str(args.get("project") or "")
            if row is not None:
                if (
                    int(row["total_bytes"]) != total
                    or row["declared_project"] != declared_proj
                    or row["source_format"] != fmt
                ):
                    raise LanError("upload_metadata_conflict")
                if order is not None and row.get("source_order") is not None and int(row["source_order"]) != order:
                    floor = int(head["source_order"]) if (head is not None and head.get("source_order") is not None) else None
                    if not (decl_curr == digest and order > int(row["source_order"]) and (floor is None or order > floor)):
                        raise LanError("upload_metadata_conflict")

            stage = self._stage_path(dev_id, sess_id, digest)
            existing_payload_path = ""
            if row is not None and row.get("archive_id"):
                arch = conn.execute("SELECT payload_path FROM em_runtime.session_archives WHERE id=%s", (row["archive_id"],)).fetchone()
                if arch is None or not arch.get("payload_path"):
                    raise LanError("archive_payload_unavailable")
                existing_payload_path = str(arch["payload_path"])
                raw = self._read_existing_archive(existing_payload_path)
            else:
                raw = stage.read_bytes() if stage.exists() else b""

            if offset > len(raw) or (offset < len(raw) and raw[offset:offset + len(chunk)] != chunk):
                raise LanError("invalid_chunk")
            if offset == len(raw):
                raw = raw + chunk
            received = len(raw)
            complete = received == total
            if not complete:
                _atomic_write_0600(stage, raw)
                _sync_dir(stage.parent)

            stale = head is not None and head.get("source_order") is not None and order is not None and order < int(head["source_order"])
            archive_id = int(row["archive_id"]) if (row is not None and row.get("archive_id")) else None
            project = str(row["project"]) if row is not None else declared_proj
            reason = str(row["attribution_reason"]) if row is not None else ""
            cas_path_str = ""

            if complete and archive_id is None:
                if hashlib.sha256(raw).hexdigest() != digest:
                    raise LanError("transcript_hash_mismatch")
                rows, _ = parse_transcript(raw, sess_id, fmt)
                det = workspace_details(rows, fmt)
                reason = str(det.get("attribution_reason") or "")
                excluded = reason in _EXCLUDED_REASONS
                if excluded:
                    project = ""
                cas_file = self._cas_path(digest)
                _atomic_write_0600(cas_file, raw)
                _sync_dir(cas_file.parent)
                cas_path_str = str(cas_file)
                ext_id = hashlib.sha256(json.dumps([dev_id, sess_id]).encode("utf-8")).hexdigest() + ":" + digest
                now_utc = _utc_now()
                arch_row = conn.execute(
                    "INSERT INTO em_runtime.session_archives "
                    "(project, adapter, external_session_id, payload_path, payload_sha256, state, expires_at, created_at, device_id, cas_blob_sha256) "
                    "VALUES (%s,%s,%s,%s,%s,'available','',%s,%s,%s) RETURNING id",
                    (project, adapter_name(fmt), ext_id, cas_path_str, digest, now_utc, dev_id, digest),
                ).fetchone()
                archive_id = int(arch_row["id"])
                conn.execute(
                    "INSERT INTO em_runtime.session_archive_storage (archive_id, manifest_sha256, content_sha256, logical_bytes) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT (archive_id) DO NOTHING",
                    (archive_id, digest, digest, total),
                )
                unlink_stage = stage

            extraction = str(row["extraction_status"]) if row is not None else "not_requested"
            backfill = str(row["backfill_status"]) if row is not None else "pending"
            if complete:
                if reason in _EXCLUDED_REASONS:
                    extraction, backfill = "excluded", "excluded"
                elif stale:
                    extraction = "not_requested" if extraction in ("pending", "superseded") else extraction
                    backfill = "superseded" if backfill == "pending" else backfill
                elif args.get("extract") and extraction == "not_requested":
                    extraction = "pending" if project else "unassigned"

            now_ts = time.time()
            conn.execute(
                "INSERT INTO em_runtime.lan_session_uploads "
                "(device_id, session_id, sha256, project, declared_project, attribution_reason, received_at, "
                "total_bytes, received_bytes, archive_id, extraction_status, source_order, backfill_status, source_format) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (device_id, session_id, sha256) DO UPDATE SET "
                "received_bytes=EXCLUDED.received_bytes, archive_id=COALESCE(em_runtime.lan_session_uploads.archive_id, EXCLUDED.archive_id), "
                "project=EXCLUDED.project, attribution_reason=EXCLUDED.attribution_reason, "
                "source_order=COALESCE(EXCLUDED.source_order, em_runtime.lan_session_uploads.source_order), "
                "backfill_status=EXCLUDED.backfill_status, extraction_status=EXCLUDED.extraction_status",
                (dev_id, sess_id, digest, project, declared_proj, reason, now_ts, total, received, archive_id, extraction, order, backfill, fmt),
            )

            promote = complete and not stale and (
                head is None or decl_curr == digest or order is None or head.get("source_order") is None or order >= int(head["source_order"])
            )
            if promote:
                keep_order = order if order is not None else (head.get("source_order") if head else None)
                conn.execute(
                    "INSERT INTO em_runtime.lan_session_heads (device_id, session_id, sha256, source_order) VALUES (%s,%s,%s,%s) "
                    "ON CONFLICT (device_id, session_id) DO UPDATE SET sha256=EXCLUDED.sha256, source_order=COALESCE(EXCLUDED.source_order, em_runtime.lan_session_heads.source_order)",
                    (dev_id, sess_id, digest, keep_order),
                )
                head = {"device_id": dev_id, "session_id": sess_id, "sha256": digest, "source_order": keep_order}

            if complete:
                conn.execute(
                    "INSERT INTO em_runtime.lan_memory_revision (id, revision) VALUES (1, 1) "
                    "ON CONFLICT (id) DO UPDATE SET revision = em_runtime.lan_memory_revision.revision + 1"
                )

            saved = conn.execute(
                "SELECT * FROM em_runtime.lan_session_uploads WHERE device_id=%s AND session_id=%s AND sha256=%s",
                (dev_id, sess_id, digest),
            ).fetchone()
            if stale and head is not None:
                head_up = conn.execute(
                    "SELECT * FROM em_runtime.lan_session_uploads WHERE device_id=%s AND session_id=%s AND sha256=%s",
                    (dev_id, sess_id, head["sha256"]),
                ).fetchone()
                receipt = self._receipt(head_up or saved, head, status="stale")
                receipt["submitted_sha256"] = digest
                receipt["submitted_total_bytes"] = total
            else:
                receipt = self._receipt(saved, head)

            if complete and archive_id is not None:
                self._record_journal(
                    conn,
                    library_key=user_id,
                    request_id=req_id,
                    actor_id=user_id,
                    op_type="session_archive_upload",
                    target_table="em_runtime.session_archives",
                    target_key=str(archive_id),
                    payload={
                        "archive": {
                            "id": archive_id, "project": project, "adapter": adapter_name(fmt),
                            "payload_path": cas_path_str or existing_payload_path or str(self._cas_path(digest)),
                            "payload_sha256": digest, "created_at": _utc_now(),
                        },
                        "storage": {"archive_id": archive_id, "manifest_sha256": digest, "content_sha256": digest, "logical_bytes": total},
                        "upload": {
                            "device_id": dev_id, "session_id": sess_id, "sha256": digest, "project": project,
                            "declared_project": declared_proj, "attribution_reason": reason,
                            "total_bytes": total, "received_bytes": received, "archive_id": archive_id,
                            "extraction_status": extraction, "source_order": order,
                            "backfill_status": backfill, "source_format": fmt,
                        },
                        "head": head if promote else None,
                    },
                )
            conn.execute(
                "INSERT INTO em_runtime.lan_write_requests (user_id, tool, request_id, payload_hash, result) "
                "VALUES (%s,%s,%s,%s,%s) ON CONFLICT (user_id, tool, request_id) DO UPDATE SET "
                "payload_hash=EXCLUDED.payload_hash, result=EXCLUDED.result, created_at=clock_timestamp()",
                (user_id, "session_archive_upload", req_id, payload_hash, json.dumps(receipt, ensure_ascii=False)),
            )
        if unlink_stage is not None:
            unlink_stage.unlink(missing_ok=True)
        return receipt

    def _build_status(self, conn: psycopg.Connection, dev_id: str, sess_id: str, handoff: Any = None) -> dict[str, Any]:
        head = conn.execute("SELECT * FROM em_runtime.lan_session_heads WHERE device_id=%s AND session_id=%s", (dev_id, sess_id)).fetchone()
        row = None
        if head is not None and head.get("sha256"):
            row = conn.execute(
                "SELECT * FROM em_runtime.lan_session_uploads WHERE device_id=%s AND session_id=%s AND sha256=%s",
                (dev_id, sess_id, head["sha256"]),
            ).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM em_runtime.lan_session_uploads WHERE device_id=%s AND session_id=%s ORDER BY received_at DESC LIMIT 1",
                (dev_id, sess_id),
            ).fetchone()
        if row is None:
            result: dict[str, Any] = {"status": "not_found"}
        else:
            result = self._receipt(row, head)
            if row.get("archive_id"):
                arch = conn.execute("SELECT state FROM em_runtime.session_archives WHERE id=%s", (row["archive_id"],)).fetchone()
                result["payload_state"] = arch["state"] if arch else "unavailable"
        if isinstance(handoff, dict):
            result["handoff"] = {
                "schema_version": 1,
                "project": handoff.get("project", ""),
                "source_sha256": handoff.get("sha256", ""),
                "source_format": handoff.get("source_format", CODEX_FORMAT),
                "covers_source": False,
                "state": "blocked" if not load_policy().ai_processing_enabled else "pending",
                "reason": "ai_processing_disabled" if not load_policy().ai_processing_enabled else "organization_task_missing",
            }
        return result

    def status(self, user_id: str, args: dict[str, Any], *, statement_timeout_ms: int | None = None) -> dict[str, Any]:
        dev_id, sess_id = self._identity(args)
        handoff = args.get("handoff")
        dbname = self.config.resolve_user_history_db(user_id)
        with self.pool.connection(dbname, role_kind="reader", statement_timeout_ms=statement_timeout_ms) as conn:
            conn.row_factory = dict_row
            return self._build_status(conn, dev_id, sess_id, handoff)

    def assign(self, user_id: str, args: dict[str, Any], *, statement_timeout_ms: int | None = None) -> dict[str, Any]:
        dev_id, sess_id = self._identity(args)
        project = str(args.get("project") or "").strip()
        req_id = str(args.get("request_id") or "")
        validate_request_id(req_id)
        if not project or len(project) > 128:
            raise LanError("invalid_arguments")
        payload_hash = hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        dbname = self.config.resolve_user_history_db(user_id)
        with self.pool.connection(dbname, role_kind="writer", statement_timeout_ms=statement_timeout_ms) as conn:
            conn.row_factory = dict_row
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))", (dev_id, sess_id))
            prev_req = conn.execute(
                "SELECT payload_hash, result FROM em_runtime.lan_write_requests WHERE user_id=%s AND tool='session_archive_assign' AND request_id=%s",
                (user_id, req_id),
            ).fetchone()
            if prev_req is not None:
                if prev_req["payload_hash"] != payload_hash:
                    raise LanError("request_id_conflict")
                return json.loads(prev_req["result"])
            known = conn.execute("SELECT 1 FROM em_runtime.context_project_registry WHERE project=%s AND status='active'", (project,)).fetchone()
            if known is None:
                raise LanError("project_not_registered")
            row = conn.execute(
                "SELECT * FROM em_runtime.lan_session_uploads WHERE device_id=%s AND session_id=%s ORDER BY received_at DESC LIMIT 1 FOR UPDATE",
                (dev_id, sess_id),
            ).fetchone()
            if row is None:
                raise LanError("archive_not_found")
            conn.execute(
                "UPDATE em_runtime.lan_session_uploads SET project=%s WHERE device_id=%s AND session_id=%s AND sha256=%s",
                (project, dev_id, sess_id, row["sha256"]),
            )
            if row.get("archive_id"):
                conn.execute("UPDATE em_runtime.session_archives SET project=%s WHERE id=%s", (project, row["archive_id"]))
            self._record_journal(
                conn, library_key=user_id, request_id=req_id, actor_id=user_id,
                op_type="session_archive_assign", target_table="em_runtime.lan_session_uploads",
                target_key=f"{dev_id}:{sess_id}:{row['sha256']}",
                payload={"device_id": dev_id, "session_id": sess_id, "sha256": row["sha256"], "project": project},
            )
            receipt = self._build_status(conn, dev_id, sess_id, args.get("handoff"))
            conn.execute(
                "INSERT INTO em_runtime.lan_write_requests (user_id, tool, request_id, payload_hash, result) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (user_id, "session_archive_assign", req_id, payload_hash, json.dumps(receipt, ensure_ascii=False)),
            )
            return receipt

    def retry(self, user_id: str, args: dict[str, Any], *, statement_timeout_ms: int | None = None) -> dict[str, Any]:
        dev_id, sess_id = self._identity(args)
        req_id = str(args.get("request_id") or "")
        validate_request_id(req_id)
        payload_hash = hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        dbname = self.config.resolve_user_history_db(user_id)
        with self.pool.connection(dbname, role_kind="writer", statement_timeout_ms=statement_timeout_ms) as conn:
            conn.row_factory = dict_row
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))", (dev_id, sess_id))
            prev_req = conn.execute(
                "SELECT payload_hash, result FROM em_runtime.lan_write_requests WHERE user_id=%s AND tool='session_archive_retry' AND request_id=%s",
                (user_id, req_id),
            ).fetchone()
            if prev_req is not None:
                if prev_req["payload_hash"] != payload_hash:
                    raise LanError("request_id_conflict")
                return json.loads(prev_req["result"])
            conn.execute(
                "UPDATE em_runtime.lan_session_uploads SET error='' WHERE device_id=%s AND session_id=%s",
                (dev_id, sess_id),
            )
            receipt = self._build_status(conn, dev_id, sess_id, args.get("handoff"))
            conn.execute(
                "INSERT INTO em_runtime.lan_write_requests (user_id, tool, request_id, payload_hash, result) VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (user_id, "session_archive_retry", req_id, payload_hash, json.dumps(receipt, ensure_ascii=False)),
            )
            return receipt

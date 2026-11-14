"""PG-only unified MCP host compatible with lan_server.make_http_server."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from typing import Any
import uuid

import psycopg.errors

from evolvmem.experience_v2.control.pg_console_store import PgConsoleStore, PgControlConfig
from evolvmem.history_pg.candidate_reader import PgHistoryCandidateReader
from evolvmem.history_pg.capture_store import PgCaptureStore
from evolvmem.history_pg.config import PgHistoryConfig, PgHistoryPool, PoolLimits
from evolvmem.history_pg.continuity_store import PgContinuityStore
from evolvmem.lan_sharing import LanError
from evolvmem.library_foundation import FoundationError, RequestIdentity

_TOOL_NAMES = (
    "memory_status", "context_session_start", "context_project_recall", "context_search", "context_read", "context_status",
    "continuity_begin", "continuity_find", "continuity_resume", "continuity_checkpoint", "continuity_list",
    "experience_recall", "session_archive_upload", "session_archive_status", "session_archive_assign", "session_archive_retry",
    "history_projects", "history_search", "history_block",
)


def _uuid_for(prefix: str, val: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"evolvmem:{prefix}:{val}"))


def _build_pg_cfg(raw: dict[str, Any]) -> PgHistoryConfig:
    lim_raw = dict(raw.get("pool_limits") or {})
    limits = PoolLimits(
        max_connections=int(lim_raw.get("max_connections", 24)),
        acquire_timeout_s=float(lim_raw.get("acquire_timeout_s", 2.5)),
        statement_timeout_ms=int(lim_raw.get("statement_timeout_ms", 3000)),
        lock_timeout_ms=int(lim_raw.get("lock_timeout_ms", 500)),
    )
    return PgHistoryConfig(
        pool_limits=limits,
        host=str(raw.get("host", "127.0.0.1")),
        port=int(raw.get("port", 55434)),
        admin_user=str(raw.get("admin_user", "postgres")),
        admin_password=str(raw.get("admin_password", "")),
        control_db=str(raw.get("control_db", "evolvmem_control")),
        history_migration_db=str(raw.get("history_migration_db", "evolvmem_history_migration")),
        shared_public_db=str(raw.get("shared_public_db", "evolvmem_shared_public")),
        user_history_dbs=dict(raw.get("user_history_dbs") or {"jiangli": "evolvmem_history_migration"}),
        device_dbs=dict(raw.get("device_dbs") or {}),
        cas_root=Path(raw.get("cas_root", "~/.local/share/evolvmem-pg-cas")).expanduser(),
        allow_loopback_disable_ssl=bool(raw.get("allow_loopback_disable_ssl", True)),
        control_coordinator_password=str(raw.get("control_coordinator_password", raw.get("admin_password", ""))),
        container_name=str(raw.get("container_name", "")),
        allow_prod=bool(raw.get("allow_prod", False)),
    )


class UnifiedMcpHost:
    """Stateless-HTTP PG MCP adapter with live em_control enforcement and deadlines."""

    deadline_guard = None  # Managed per-request inside serve_rpc

    def __init__(
        self,
        pg_config: PgHistoryConfig,
        control_config: PgControlConfig,
        *,
        owner_id: str = "jiangli",
        tokens: dict[str, dict[str, str]] | None = None,
        request_timeout_seconds: float = 3.0,
        pool: PgHistoryPool | None = None,
        allow_sleep_probe: bool = False,
    ) -> None:
        self.pg_config = pg_config
        self.control_store = PgConsoleStore(control_config)
        self.owner_id = owner_id
        self.tokens = dict(tokens or {})
        self.request_timeout_seconds = max(0.1, float(request_timeout_seconds))
        self.pool = pool or PgHistoryPool(pg_config)
        self.capture = PgCaptureStore(pg_config, pool=self.pool)
        self.continuity = PgContinuityStore(pg_config, pool=self.pool)
        self.allow_sleep_probe = bool(allow_sleep_probe or os.environ.get("EVOLVMEM_ALLOW_STAGE5_PROBES") == "1")
        self._lock = threading.Lock()
        self._principal_to_user: dict[str, str] = {}

    def authenticate(self, token: str) -> RequestIdentity | None:
        if not token:
            return None
        tok_sha = hashlib.sha256(token.encode("utf-8")).hexdigest()
        entry = self.tokens.get(tok_sha)
        if not entry:
            return None
        user_id = str(entry.get("user_id") or "")
        dev_label = str(entry.get("device_id") or "default")
        try:
            user_row = self.control_store.get_user_source(self.owner_id, user_id)
        except Exception as exc:
            raise FoundationError("catalog_unavailable") from exc
        if user_row is None or not user_row.get("enabled"):
            return None
        principal_id = str(user_row["principal_id"])
        with self._lock:
            self._principal_to_user[principal_id] = user_id
        return RequestIdentity(
            principal_id=principal_id,
            credential_id=_uuid_for("cred", tok_sha),
            device_id=_uuid_for("dev", dev_label),
            credential_epoch=1,
        )

    def _resolve_user_id(self, identity: RequestIdentity) -> str:
        with self._lock:
            uid = self._principal_to_user.get(identity.principal_id)
        if uid is None:
            for row in self.control_store.list_users(self.owner_id):
                if row["principal_id"] == identity.principal_id:
                    uid = row["id"]
                    with self._lock:
                        self._principal_to_user[identity.principal_id] = uid
                    break
        if uid is None or not self.control_store.user_enabled(self.owner_id, uid):
            raise FoundationError("unauthorized")
        return uid

    def _tool_specs(self) -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "description": f"PG-backed EvolvMem tool: {name}",
                "inputSchema": {"type": "object", "properties": {}, "additionalProperties": True},
                "annotations": {"readOnlyHint": name in (
                    "memory_status", "context_session_start", "context_project_recall", "context_search",
                    "context_read", "context_status", "continuity_resume", "continuity_find", "continuity_list",
                    "experience_recall", "session_archive_status", "history_projects", "history_search", "history_block",
                )},
            }
            for name in _TOOL_NAMES
        ]

    def _handle_tool(self, user_id: str, identity: RequestIdentity, name: str, args: dict[str, Any], timeout_ms: int) -> dict[str, Any]:
        if name == "memory_status" and self.allow_sleep_probe and isinstance(args.get("_sleep_seconds"), (int, float)):
            sec = float(args["_sleep_seconds"])
            with self.pool.connection(self.pg_config.resolve_user_history_db(user_id), statement_timeout_ms=timeout_ms) as conn:
                conn.execute("SELECT pg_sleep(%s)", (sec,))
        if name in ("session_archive_upload", "session_archive_status", "session_archive_assign", "session_archive_retry"):
            fn = {
                "session_archive_upload": self.capture.upload,
                "session_archive_status": self.capture.status,
                "session_archive_assign": self.capture.assign,
                "session_archive_retry": self.capture.retry,
            }[name]
            return fn(user_id, args, statement_timeout_ms=timeout_ms)
        if name == "experience_recall":
            return {"results": [], "cases": [], "methods": [], "count": 0, "llm_calls": 0}
        if name in ("history_projects", "history_search", "history_block"):
            reader = PgHistoryCandidateReader(self.pg_config, dbname=self.pg_config.resolve_user_history_db(user_id))
            if name == "history_projects":
                return {"projects": reader.projects()}
            if name == "history_search":
                return {"items": reader.hybrid_search(str(args.get("query") or ""), limit=int(args.get("limit") or 10))}
            return {"turns": reader.block_turn_page(str(args.get("block_id") or ""))}
        return self.continuity.dispatch(user_id, name, args, statement_timeout_ms=timeout_ms)

    def serve_rpc(self, identity: RequestIdentity, request: dict[str, Any], *, publish, session_id=None, client_hint=None) -> None:
        method = request.get("method")
        req_id = request.get("id")
        if method and str(method).startswith("notifications/"):
            publish(None)
            return
        if method == "initialize":
            params = request.get("params") or {}
            proto = params.get("protocolVersion") or "2025-11-25"
            publish({
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "protocolVersion": proto,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "evolvmem-pg-unified", "version": "0.1"},
                    "instructions": "PG-backed unified EvolvMem MCP service.",
                },
            })
            return
        if method == "ping":
            publish({"jsonrpc": "2.0", "id": req_id, "result": {}})
            return
        if method == "tools/list":
            publish({"jsonrpc": "2.0", "id": req_id, "result": {"tools": self._tool_specs()}})
            return
        if method != "tools/call":
            publish({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "method_not_found"}})
            return

        params = request.get("params") or {}
        tool_name = str(params.get("name") or "")
        args = params.get("arguments") or {}
        if tool_name not in _TOOL_NAMES or not isinstance(args, dict):
            payload = {"error": "unknown_tool", "authenticated_user": identity.principal_id}
            publish({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": True}})
            return

        timeout_s = float(args.get("_timeout_seconds") or self.request_timeout_seconds) if self.allow_sleep_probe else self.request_timeout_seconds
        timeout_ms = max(50, int(timeout_s * 1000))
        t0 = time.monotonic()
        try:
            user_id = self._resolve_user_id(identity)
            res = self._handle_tool(user_id, identity, tool_name, args, timeout_ms)
            if time.monotonic() - t0 > timeout_s + 0.25:
                raise TimeoutError("request_deadline_exceeded")
            out = dict(res)
            out["authenticated_user"] = identity.principal_id
            publish({
                "jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": json.dumps(out, ensure_ascii=False)}], "isError": False},
            })
        except (LanError, FoundationError) as exc:
            code = exc.code if isinstance(exc, FoundationError) else str(exc)
            err = {"error": code, "authenticated_user": identity.principal_id}
            publish({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(err)}], "isError": True}})
        except (psycopg.errors.QueryCanceled, TimeoutError):
            err = {"error": "request_timeout", "authenticated_user": identity.principal_id}
            publish({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": json.dumps(err)}], "isError": True}})


def main() -> int:
    from evolvmem import lan_server
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=19378)
    ap.add_argument("--config", required=True)
    args = ap.parse_args()
    raw = json.loads(Path(args.config).read_text(encoding="utf-8"))
    pg_cfg = _build_pg_cfg(raw["pg_config"])
    ctrl_cfg = PgControlConfig.from_file(Path(raw["control_config_path"]))
    host = UnifiedMcpHost(
        pg_cfg, ctrl_cfg,
        owner_id=str(raw.get("owner_id", "jiangli")),
        tokens=raw.get("tokens") or {},
        request_timeout_seconds=float(raw.get("request_timeout_seconds", 3.0)),
        allow_sleep_probe=bool(raw.get("allow_sleep_probe", False)),
    )
    server = lan_server.make_http_server(None, host=args.host, port=args.port, library_host=host, allow_legacy=False)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

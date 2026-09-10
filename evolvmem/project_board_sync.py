"""Synchronize committed continuity checkpoints to an existing project board.

The adapter is deliberately small and local: SQLite remains authoritative,
the private target configuration is read only at call time, and transport
failures leave the newest snapshot pending without changing checkpoint writes.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
from typing import Iterable
from urllib.parse import quote, urlsplit

from evolvmem.config import Config
from evolvmem.context_models import ContextLayer
from evolvmem.context_store import ContextStore
from evolvmem.continuity_service import _resolve_bound_project
from evolvmem.cutover_cli import _scrubbed_environment
from evolvmem.workspace_identity import (
    WorkspaceIdentityError,
    WorkspaceIdentityProvider,
)


_CONFIG_ENV = "EVOLVMEM_PROJECT_BOARD_CONFIG"
_CONFIG_NAME = "project_board.json"
_STATE_NAME = "project_board_sync.db"
_MAX_RESPONSE_BYTES = 256 * 1024
_REMOTE_RESULTS = frozenset({
    "synced", "unchanged", "stale", "not_bound", "project_unavailable",
})
_SUCCESS_RESULTS = frozenset({"synced", "unchanged", "stale"})
_LOOKUP_FAILED = object()


def _is_loopback_host(hostname: str) -> bool:
    if hostname.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class _BoardConfig:
    base_url: str
    api_key: str


@dataclass(frozen=True, slots=True)
class _Scope:
    project: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class _Snapshot:
    project: str
    fingerprint: str
    workstream_id: str
    checkpoint_revision: int
    state_version: int
    payload: dict
    content_hash: str


class ProjectBoardSync:
    """Best-effort one-way adapter for one borrowed continuity store."""

    def __init__(
        self,
        config: Config,
        store: ContextStore,
        workspace_identity: WorkspaceIdentityProvider,
        *,
        timeout_seconds: float = 3.0,
    ) -> None:
        if not isinstance(config, Config):
            raise TypeError("config must be a Config instance")
        if not isinstance(store, ContextStore):
            raise TypeError("store must be a ContextStore instance")
        if not isinstance(workspace_identity, WorkspaceIdentityProvider):
            raise TypeError("workspace_identity must be a WorkspaceIdentityProvider instance")
        if type(timeout_seconds) not in (int, float) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.config = config
        self.store = store
        self.workspace_identity = workspace_identity
        self.timeout_seconds = float(timeout_seconds)

    @property
    def config_path(self) -> Path:
        override = os.environ.get(_CONFIG_ENV)
        if override and override.strip():
            return Path(override).expanduser()
        return self.config.data_dir / _CONFIG_NAME

    @property
    def state_path(self) -> Path:
        return self.config.data_dir / _STATE_NAME

    def sync(
        self,
        workspace_path: str,
        project_hint: str = "",
        workstream_id: str = "",
    ) -> dict:
        """Push latest committed checkpoints in one exact project/workspace.

        With ``workstream_id`` only that changed workstream plus previously
        pending peers is attempted. Without it every latest checkpoint in the
        scope is considered, including terminal workstreams.
        """
        board = self._load_config()
        if board is None:
            return self._disabled()
        scope = self._resolve_scope(workspace_path, project_hint)
        if scope is None:
            return self._not_bound()
        snapshots = self._snapshots(scope, workstream_id)
        if workstream_id and not snapshots:
            return self._not_bound(project=scope.project)
        if not snapshots:
            return self._aggregate(scope.project, ())
        state = self._open_state()
        try:
            for snapshot in snapshots:
                self._queue(state, snapshot)
            target = self._lookup_target(board, scope.project)
            if target is _LOOKUP_FAILED:
                results = tuple(
                    self._receipt(snapshot, "pending") for snapshot in snapshots
                )
                for snapshot in snapshots:
                    self._mark_attempt(state, snapshot, "pending")
                return self._aggregate(scope.project, results)
            if target is None:
                results = tuple(
                    self._receipt(snapshot, "not_bound") for snapshot in snapshots
                )
                for snapshot in snapshots:
                    self._mark_attempt(state, snapshot, "not_bound")
                return self._aggregate(scope.project, results)
            results = []
            for snapshot in snapshots:
                prior = self._state_row(state, snapshot)
                same_content = (
                    prior is not None
                    and prior["sent_hash"] == snapshot.content_hash
                    and prior["target_record_id"] == target["projectRecordId"]
                    and prior["binding_id"] == target["bindingId"]
                )
                if same_content:
                    self._mark_sent(state, snapshot, target, "unchanged")
                    results.append(self._receipt(snapshot, "unchanged"))
                    continue
                payload = {
                    "projectRecordId": target["projectRecordId"],
                    "bindingId": target["bindingId"],
                    **snapshot.payload,
                }
                remote_status = self._post_snapshot(board, payload)
                if remote_status in _SUCCESS_RESULTS:
                    public_status = (
                        "synced" if remote_status == "synced" else "unchanged"
                    )
                    self._mark_sent(state, snapshot, target, public_status)
                elif remote_status in {"not_bound", "project_unavailable"}:
                    public_status = "not_bound"
                    self._mark_attempt(state, snapshot, public_status)
                else:
                    public_status = "pending"
                    self._mark_attempt(state, snapshot, public_status)
                results.append(self._receipt(snapshot, public_status))
            return self._aggregate(scope.project, tuple(results))
        finally:
            state.close()

    def status(
        self,
        workspace_path: str,
        project_hint: str = "",
        workstream_id: str = "",
    ) -> dict:
        """Return content-free local delivery status without network activity."""
        if self._load_config() is None:
            return self._disabled()
        scope = self._resolve_scope(workspace_path, project_hint)
        if scope is None:
            return self._not_bound()
        snapshots = self._snapshots(scope, workstream_id, include_pending=False)
        if workstream_id and not snapshots:
            return self._not_bound(project=scope.project)
        if not self.state_path.exists():
            return self._aggregate(
                scope.project,
                tuple(self._receipt(snapshot, "pending") for snapshot in snapshots),
            )
        state = self._open_state()
        try:
            results = []
            for snapshot in snapshots:
                row = self._state_row(state, snapshot)
                if row is None:
                    delivery = "pending"
                elif row["sent_hash"] == snapshot.content_hash:
                    delivery = row["last_status"] or "unchanged"
                elif (
                    row["content_hash"] == snapshot.content_hash
                    and row["last_status"] == "not_bound"
                ):
                    delivery = "not_bound"
                else:
                    delivery = "pending"
                results.append(self._receipt(snapshot, delivery))
            return self._aggregate(scope.project, tuple(results))
        finally:
            state.close()

    def _load_config(self) -> _BoardConfig | None:
        path = self.config_path
        try:
            metadata = path.stat()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_uid != os.geteuid()
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                return None
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(raw, dict) or set(raw) != {"base_url", "api_key", "enabled"}:
            return None
        if raw.get("enabled") is not True:
            return None
        base_url = raw.get("base_url")
        api_key = raw.get("api_key")
        if (
            not isinstance(base_url, str)
            or not isinstance(api_key, str)
            or not base_url.strip()
            or not api_key.strip()
            or len(api_key) > 4096
        ):
            return None
        normalized = base_url.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or (
                parsed.scheme == "http"
                and not _is_loopback_host(parsed.hostname)
            )
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return None
        return _BoardConfig(base_url=normalized, api_key=api_key.strip())

    def _resolve_scope(self, workspace_path: str, project_hint: str) -> _Scope | None:
        try:
            identity = self.workspace_identity.resolve(workspace_path)
        except (WorkspaceIdentityError, TypeError):
            return None
        project = _resolve_bound_project(
            self.store, identity.fingerprint, project_hint
        )
        if project is None:
            return None
        return _Scope(project, identity.fingerprint)

    def _snapshots(
        self,
        scope: _Scope,
        workstream_id: str,
        *,
        include_pending: bool = True,
    ) -> tuple[_Snapshot, ...]:
        conn = self.store._connection()
        ids: set[str] | None = None
        if workstream_id:
            requested = conn.execute(
                "SELECT 1 FROM continuity_workstreams WHERE id=? AND project=? "
                "AND workspace_fingerprint=?",
                (workstream_id, scope.project, scope.fingerprint),
            ).fetchone()
            if requested is None:
                return ()
            ids = {workstream_id}
            if include_pending and self.state_path.exists():
                state = self._open_state()
                try:
                    ids.update(
                        row["workstream_id"]
                        for row in state.execute(
                            "SELECT workstream_id FROM project_board_sync_state "
                            "WHERE project=? AND workspace_fingerprint=? AND pending=1",
                            (scope.project, scope.fingerprint),
                        )
                    )
                finally:
                    state.close()
        query = (
            "SELECT * FROM continuity_workstreams WHERE project=? "
            "AND workspace_fingerprint=?"
        )
        params: list[object] = [scope.project, scope.fingerprint]
        if ids is not None:
            placeholders = ",".join("?" for _ in ids)
            query += f" AND id IN ({placeholders})"
            params.extend(sorted(ids))
        query += " ORDER BY updated_at, id"
        snapshots = []
        for row in conn.execute(query, params).fetchall():
            content = self._checkpoint_content(int(row["current_context_id"]))
            if content is None:
                continue
            source_updated_at = self._utc_timestamp(row["updated_at"])
            if source_updated_at is None:
                continue
            payload = {
                "sourceProject": scope.project,
                "workstreamId": row["id"],
                "checkpointRevision": int(row["checkpoint_revision"]),
                "stateVersion": int(row["state_version"]),
                "status": row["status"],
                "objective": content["objective"],
                "completedSteps": content["completed_steps"],
                "currentStep": content["current_step"],
                "nextAction": content["next_action"],
                "blockers": content["blockers"],
                "sourceUpdatedAt": source_updated_at,
            }
            business = {
                key: payload[key]
                for key in (
                    "sourceProject", "workstreamId", "status", "objective",
                    "completedSteps", "currentStep", "nextAction", "blockers",
                )
            }
            digest = hashlib.sha256(
                json.dumps(
                    business, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            snapshots.append(_Snapshot(
                project=scope.project,
                fingerprint=scope.fingerprint,
                workstream_id=row["id"],
                checkpoint_revision=int(row["checkpoint_revision"]),
                state_version=int(row["state_version"]),
                payload=payload,
                content_hash=digest,
            ))
        return tuple(snapshots)

    def _checkpoint_content(self, context_id: int) -> dict | None:
        raw = self.store.get_layer(context_id, ContextLayer.L2)
        try:
            content = json.loads(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None
        if not isinstance(content, dict):
            return None
        values = {}
        for key in ("objective", "current_step", "next_action"):
            value = content.get(key)
            if not isinstance(value, str):
                return None
            values[key] = value
        for key in ("completed_steps", "blockers"):
            value = content.get(key)
            if (
                not isinstance(value, list)
                or len(value) > 100
                or any(not isinstance(item, str) for item in value)
            ):
                return None
            values[key] = value
        return values

    def _open_state(self) -> sqlite3.Connection:
        self.config.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        conn = sqlite3.connect(self.state_path)
        conn.row_factory = sqlite3.Row
        os.chmod(self.state_path, 0o600)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS project_board_sync_state("
            "project TEXT NOT NULL, workspace_fingerprint TEXT NOT NULL,"
            "workstream_id TEXT NOT NULL, checkpoint_revision INTEGER NOT NULL,"
            "state_version INTEGER NOT NULL, payload_json TEXT NOT NULL,"
            "content_hash TEXT NOT NULL, pending INTEGER NOT NULL DEFAULT 1,"
            "target_record_id TEXT NOT NULL DEFAULT '',"
            "binding_id TEXT NOT NULL DEFAULT '', sent_hash TEXT NOT NULL DEFAULT '',"
            "last_status TEXT NOT NULL DEFAULT 'pending', updated_at TEXT NOT NULL,"
            "PRIMARY KEY(project, workspace_fingerprint, workstream_id))"
        )
        conn.commit()
        return conn

    def _queue(self, state: sqlite3.Connection, snapshot: _Snapshot) -> None:
        state.execute(
            "INSERT INTO project_board_sync_state("
            "project, workspace_fingerprint, workstream_id, checkpoint_revision,"
            "state_version, payload_json, content_hash, pending, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?) "
            "ON CONFLICT(project, workspace_fingerprint, workstream_id) DO UPDATE SET "
            "checkpoint_revision=excluded.checkpoint_revision,"
            "state_version=excluded.state_version,payload_json=excluded.payload_json,"
            "content_hash=excluded.content_hash,pending=1,updated_at=excluded.updated_at",
            (
                snapshot.project, snapshot.fingerprint, snapshot.workstream_id,
                snapshot.checkpoint_revision, snapshot.state_version,
                json.dumps(snapshot.payload, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")),
                snapshot.content_hash, snapshot.payload["sourceUpdatedAt"],
            ),
        )
        state.commit()

    @staticmethod
    def _state_row(state: sqlite3.Connection, snapshot: _Snapshot):
        return state.execute(
            "SELECT * FROM project_board_sync_state WHERE project=? "
            "AND workspace_fingerprint=? AND workstream_id=?",
            (snapshot.project, snapshot.fingerprint, snapshot.workstream_id),
        ).fetchone()

    @staticmethod
    def _utc_timestamp(value: object) -> str | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        else:
            parsed = parsed.astimezone(timezone.utc)
        return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _lookup_target(self, board: _BoardConfig, project: str):
        response = self._request_json(
            board, "GET",
            "/openapi/rd-progress/bindings?sourceProject=" + quote(project, safe=""),
        )
        if not isinstance(response, dict) or not isinstance(response.get("items"), list):
            return _LOOKUP_FAILED
        matches = []
        required = (
            "projectRecordId", "projectName", "projectNumber", "bindingId",
            "sourceProject",
        )
        for item in response["items"]:
            if not isinstance(item, dict) or not all(
                isinstance(item.get(key), str) and item[key] for key in required
            ):
                return _LOOKUP_FAILED
            if item["sourceProject"] == project:
                matches.append(item)
        return matches[0] if len(matches) == 1 else None

    def _post_snapshot(self, board: _BoardConfig, payload: dict) -> str:
        response = self._request_json(
            board, "POST", "/openapi/rd-progress/sync", payload
        )
        if not isinstance(response, dict) or response.get("status") not in _REMOTE_RESULTS:
            return "pending"
        if (
            response.get("projectRecordId") != payload["projectRecordId"]
            or response.get("workstreamId") != payload["workstreamId"]
            or response.get("checkpointRevision") != payload["checkpointRevision"]
        ):
            return "pending"
        return response["status"]

    def _request_json(
        self,
        board: _BoardConfig,
        method: str,
        route: str,
        payload: dict | None = None,
    ) -> object | None:
        parsed = urlsplit(board.base_url)
        path = parsed.path.rstrip("/") + route
        body = None
        headers = {"Accept": "application/json", "X-Api-Key": board.api_key}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        connection_class = (
            http.client.HTTPSConnection
            if parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_class(
            parsed.hostname, parsed.port, timeout=self.timeout_seconds
        )
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            data = response.read(_MAX_RESPONSE_BYTES + 1)
            if (
                response.status < 200
                or response.status >= 300
                or len(data) > _MAX_RESPONSE_BYTES
            ):
                return None
            return json.loads(data.decode("utf-8"))
        except (OSError, ValueError, UnicodeError, http.client.HTTPException):
            return None
        finally:
            connection.close()

    def _mark_sent(
        self,
        state: sqlite3.Connection,
        snapshot: _Snapshot,
        target: dict,
        status: str,
    ) -> None:
        state.execute(
            "UPDATE project_board_sync_state SET pending=0,target_record_id=?,"
            "binding_id=?,sent_hash=?,last_status=? WHERE project=? "
            "AND workspace_fingerprint=? AND workstream_id=?",
            (
                target["projectRecordId"], target["bindingId"],
                snapshot.content_hash, status, snapshot.project,
                snapshot.fingerprint, snapshot.workstream_id,
            ),
        )
        state.commit()

    @staticmethod
    def _mark_attempt(
        state: sqlite3.Connection, snapshot: _Snapshot, status: str
    ) -> None:
        state.execute(
            "UPDATE project_board_sync_state SET pending=1,last_status=? "
            "WHERE project=? AND workspace_fingerprint=? AND workstream_id=?",
            (status, snapshot.project, snapshot.fingerprint, snapshot.workstream_id),
        )
        state.commit()

    @staticmethod
    def _receipt(snapshot: _Snapshot, status: str) -> dict:
        return {
            "workstreamId": snapshot.workstream_id,
            "checkpointRevision": snapshot.checkpoint_revision,
            "stateVersion": snapshot.state_version,
            "status": status,
        }

    def _aggregate(
        self,
        project: str,
        results: Iterable[dict],
    ) -> dict:
        rows = tuple(results)
        pending_count = sum(item["status"] in {"pending", "not_bound"} for item in rows)
        synced_count = sum(item["status"] == "synced" for item in rows)
        statuses = {item["status"] for item in rows}
        if "pending" in statuses:
            status = "pending"
        elif "not_bound" in statuses:
            status = "not_bound"
        elif "synced" in statuses:
            status = "synced"
        else:
            status = "unchanged"
        return {
            "status": status,
            "project": project,
            "workstreamCount": len(rows),
            "syncedCount": synced_count,
            "pendingCount": pending_count,
            "workstreams": list(rows),
        }

    @staticmethod
    def _disabled() -> dict:
        return {"status": "disabled", "message": "project board sync is disabled"}

    @staticmethod
    def _not_bound(*, project: str = "") -> dict:
        result = {
            "status": "not_bound", "workstreamCount": 0,
            "syncedCount": 0, "pendingCount": 0, "workstreams": [],
        }
        if project:
            result["project"] = project
        return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="evolvmem.project_board_sync")
    parser.add_argument("--data-dir", type=Path, default=Config().data_dir)
    sub = parser.add_subparsers(dest="action", required=True)
    for action in ("status", "sync"):
        command = sub.add_parser(action)
        command.add_argument("workspace_path")
        command.add_argument("--project-hint", default="")
        command.add_argument("--workstream-id", default="")
    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    with _scrubbed_environment():
        config = Config(data_dir=args.data_dir)
        provider = WorkspaceIdentityProvider(config.data_dir / "workspace.key")
        try:
            with ContextStore(config) as store:
                adapter = ProjectBoardSync(config, store, provider)
                result = getattr(adapter, args.action)(
                    args.workspace_path,
                    project_hint=args.project_hint,
                    workstream_id=args.workstream_id,
                )
        except Exception:
            result = {
                "status": "pending",
                "message": "project board sync remains pending",
            }
    print(json.dumps(result, ensure_ascii=False))
    return 1 if args.action == "sync" and result.get("status") == "pending" else 0


if __name__ == "__main__":
    sys.exit(main())

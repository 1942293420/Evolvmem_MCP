"""Behavioral coverage for the optional project-board progress adapter."""

from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading

import pytest

from evolvmem.context_store import ContextStore
from evolvmem.context_models import ContextMode
from evolvmem.continuity_models import ContinuityCheckpointRequest
from evolvmem.continuity_service import ContinuityService
from evolvmem.mcp_contract import tool_specs
from evolvmem.mcp_server import MemoryMCPServer
from evolvmem.project_board_sync import ProjectBoardSync, main
from evolvmem.project_store import ProjectStore
from evolvmem.workspace_identity import WorkspaceIdentityProvider


class _BoardHandler(BaseHTTPRequestHandler):
    binding_items = []
    posts = []
    post_status = 200
    get_status = 200
    post_payload = {"status": "synced", "projectRecordId": "record-1",
                    "workstreamId": "", "checkpointRevision": 1,
                    "syncedAt": "2026-09-10T12:00:00Z"}
    api_key = ""
    get_paths = []
    authorization_headers = []

    def do_GET(self):  # noqa: N802 - stdlib callback name
        type(self).get_paths.append(self.path)
        auth = self.headers.get("Authorization")
        legacy = self.headers.get("X-Api-Key")
        type(self).authorization_headers.append((auth, legacy))
        if auth != f"Bearer {type(self).api_key}" or legacy is not None:
            self.send_response(401)
            self.end_headers()
            return
        if type(self).get_status != 200:
            self.send_response(type(self).get_status)
            self.end_headers()
            return
        body = json.dumps({"items": type(self).binding_items}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - stdlib callback name
        auth = self.headers.get("Authorization")
        legacy = self.headers.get("X-Api-Key")
        type(self).authorization_headers.append((auth, legacy))
        if auth != f"Bearer {type(self).api_key}" or legacy is not None:
            self.send_response(401)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        type(self).posts.append(payload)
        response = dict(type(self).post_payload)
        response["projectRecordId"] = payload["projectRecordId"]
        response["workstreamId"] = payload["workstreamId"]
        response["checkpointRevision"] = payload["checkpointRevision"]
        body = json.dumps(response).encode()
        self.send_response(type(self).post_status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@contextmanager
def _board_server(*, source_project="proj", get_status=200, post_status=200):
    handler = type("BoardHandler", (_BoardHandler,), {})
    handler.api_key = "temporary-secret-key"
    handler.binding_items = [{
        "projectRecordId": "record-1",
        "projectName": "Existing project",
        "projectNumber": "RD-001",
        "bindingId": "binding-1",
        "sourceProject": source_project,
    }]
    handler.posts = []
    handler.get_paths = []
    handler.authorization_headers = []
    handler.post_status = post_status
    handler.get_status = get_status
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield handler, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _configure(test_config, base_url: str, *, enabled=True):
    path = test_config.data_dir / "project_board.json"
    path.write_text(json.dumps({
        "base_url": base_url,
        "api_key": "temporary-secret-key",
        "enabled": enabled,
    }), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def _services(test_config, workspace: Path, *, project="proj"):
    provider = WorkspaceIdentityProvider(test_config.data_dir / "workspace.key")
    provider.bootstrap_key()
    store = ContextStore(test_config)
    store.initialize()
    fingerprint = provider.resolve(str(workspace)).fingerprint
    with store.transaction():
        projects = ProjectStore(
            store._connection(), store._require_transaction, generic_names=()
        )
        projects.register_project(project)
        projects.bind_workspace(
            fingerprint, project, method="test", make_default=True
        )
    continuity = ContinuityService(test_config, store, provider)
    sync = ProjectBoardSync(test_config, store, provider)
    return store, continuity, sync


def _create(continuity, workspace: Path, *, objective="Ship adapter"):
    return continuity.checkpoint(ContinuityCheckpointRequest(
        action="create",
        workspace_path=str(workspace),
        objective=objective,
        completed_steps=("Designed",),
        current_step="Implement",
        next_action="Verify",
        blockers=(),
    ))


@pytest.fixture
def workspace(tmp_path):
    path = tmp_path / "workspace"
    path.mkdir()
    return path


def test_manual_sync_posts_authoritative_checkpoint_contract(test_config, workspace):
    """Wrong payload fields, types, or client-supplied state must fail."""
    with _board_server() as (http, base_url):
        _configure(test_config, base_url)
        store, continuity, sync = _services(test_config, workspace)
        created = _create(continuity, workspace)

        result = sync.sync(str(workspace), workstream_id=created.workstream_id)

        assert result == {
            "status": "synced",
            "project": "proj",
            "workstreamCount": 1,
            "syncedCount": 1,
            "pendingCount": 0,
            "workstreams": [{
                "workstreamId": created.workstream_id,
                "checkpointRevision": 1,
                "stateVersion": 1,
                "status": "synced",
            }],
        }
        assert len(http.posts) == 1
        payload = http.posts[0]
        assert payload == {
            "projectRecordId": "record-1",
            "bindingId": "binding-1",
            "sourceProject": "proj",
            "workstreamId": created.workstream_id,
            "checkpointRevision": 1,
            "stateVersion": 1,
            "status": "open",
            "objective": "Ship adapter",
            "completedSteps": ["Designed"],
            "currentStep": "Implement",
            "nextAction": "Verify",
            "blockers": [],
            "sourceUpdatedAt": payload["sourceUpdatedAt"],
        }
        assert isinstance(payload["stateVersion"], int)
        assert payload["stateVersion"] > 0
        stored_updated_at = store._connection().execute(
            "SELECT updated_at FROM continuity_workstreams WHERE id=?",
            (created.workstream_id,),
        ).fetchone()["updated_at"]
        assert " " in stored_updated_at  # real SQLite UTC representation
        assert payload["sourceUpdatedAt"] == (
            stored_updated_at.replace(" ", "T") + ".000Z"
        )
        assert http.get_paths == [
            "/openapi/rd-progress/bindings?sourceProject=proj"
        ]
        store.close()


def test_gateway_authentication_uses_only_authorization_bearer(
    test_config, workspace
):
    """Both gateway calls must use the published Bearer authentication."""
    with _board_server() as (http, base_url):
        _configure(test_config, base_url)
        store, continuity, sync = _services(test_config, workspace)
        created = _create(continuity, workspace)

        result = sync.sync(
            str(workspace), workstream_id=created.workstream_id
        )

        assert result["status"] == "synced"
        assert http.authorization_headers == [
            ("Bearer temporary-secret-key", None),
            ("Bearer temporary-secret-key", None),
        ]
        store.close()


def test_plain_http_config_is_limited_to_loopback(test_config, workspace):
    """Credential-bearing HTTP must never be enabled for a remote host."""
    store, _continuity, sync = _services(test_config, workspace)

    for base_url in (
        "http://127.0.0.1:5189",
        "http://[::1]:5189",
        "http://localhost:5189",
        "https://example.com/project-board",
    ):
        _configure(test_config, base_url)
        assert sync._load_config() is not None

    _configure(test_config, "http://example.com/project-board")
    assert sync._load_config() is None
    assert sync.sync(str(workspace)) == {
        "status": "disabled", "message": "project board sync is disabled",
    }
    store.close()


def test_sync_uses_continuity_unicode_casefold_alias_resolution(
    test_config, workspace
):
    """Board scope must resolve aliases exactly as continuity does."""
    with _board_server() as (http, base_url):
        _configure(test_config, base_url)
        store, continuity, sync = _services(test_config, workspace)
        with store.transaction():
            ProjectStore(
                store._connection(), store._require_transaction, generic_names=()
            ).add_alias("Straße", "proj")
        created = _create(continuity, workspace)
        fingerprint = sync.workspace_identity.resolve(str(workspace)).fingerprint
        assert continuity._resolve_project(fingerprint, "STRASSE") == "proj"

        result = sync.sync(
            str(workspace), project_hint="STRASSE",
            workstream_id=created.workstream_id,
        )

        assert result["status"] == "synced"
        assert len(http.posts) == 1
        store.close()


def test_manual_sync_covers_all_latest_workstreams_including_completed(
    test_config, workspace
):
    """Reading only the focused or unfinished task must fail this test."""
    with _board_server() as (http, base_url):
        _configure(test_config, base_url)
        store, continuity, sync = _services(test_config, workspace)
        first = _create(continuity, workspace, objective="First")
        continuity.checkpoint(ContinuityCheckpointRequest(
            action="complete", workspace_path=str(workspace),
            workstream_id=first.workstream_id,
            completed_steps=("Done",),
            expected_checkpoint_revision=1, expected_state_version=1,
        ))
        second = _create(continuity, workspace, objective="Second")

        result = sync.sync(str(workspace))

        assert result["status"] == "synced"
        assert result["workstreamCount"] == 2
        assert {item["workstreamId"] for item in result["workstreams"]} == {
            first.workstream_id, second.workstream_id,
        }
        assert {payload["status"] for payload in http.posts} == {
            "completed", "open",
        }
        store.close()


def test_no_exact_unique_remote_binding_does_not_post(test_config, workspace):
    """Missing and ambiguous source bindings must never guess a target."""
    with _board_server(source_project="other") as (http, base_url):
        http.binding_items.append({
            "projectRecordId": "record-2", "projectName": "Duplicate",
            "projectNumber": "RD-002", "bindingId": "binding-2",
            "sourceProject": "proj",
        })
        http.binding_items.append({
            "projectRecordId": "record-3", "projectName": "Duplicate 2",
            "projectNumber": "RD-003", "bindingId": "binding-3",
            "sourceProject": "proj",
        })
        _configure(test_config, base_url)
        store, continuity, sync = _services(test_config, workspace)
        _create(continuity, workspace)

        result = sync.sync(str(workspace))

        assert result["status"] == "not_bound"
        assert result["pendingCount"] == 1
        assert http.posts == []
        assert sync.status(str(workspace))["status"] == "not_bound"
        store.close()


def test_binding_lookup_failure_is_pending_not_not_bound(test_config, workspace):
    """Conflating an HTTP lookup failure with a successful empty result must fail."""
    with _board_server(get_status=503) as (http, base_url):
        _configure(test_config, base_url)
        store, continuity, sync = _services(test_config, workspace)
        _create(continuity, workspace)

        result = sync.sync(str(workspace))

        assert result["status"] == "pending"
        assert result["pendingCount"] == 1
        assert http.posts == []
        assert sync.status(str(workspace))["status"] == "pending"
        store.close()


def test_malformed_binding_item_is_pending_not_not_bound(test_config, workspace):
    """A malformed success response must not be mistaken for a verified absence."""
    with _board_server() as (http, base_url):
        http.binding_items = [{"sourceProject": "proj"}]
        _configure(test_config, base_url)
        store, continuity, sync = _services(test_config, workspace)
        _create(continuity, workspace)

        result = sync.sync(str(workspace))

        assert result["status"] == "pending"
        assert result["pendingCount"] == 1
        assert http.posts == []
        store.close()


def test_failed_post_stays_pending_and_retry_sends_latest_snapshot(
    test_config, workspace
):
    """Clearing pending state on transport failure must fail this test."""
    with _board_server(post_status=503) as (http, base_url):
        _configure(test_config, base_url)
        store, continuity, sync = _services(test_config, workspace)
        created = _create(continuity, workspace)

        failed = sync.sync(str(workspace), workstream_id=created.workstream_id)
        status = sync.status(str(workspace))

        assert failed["status"] == "pending"
        assert failed["pendingCount"] == 1
        assert status["status"] == "pending"
        rendered = json.dumps(failed) + json.dumps(status)
        assert "temporary-secret-key" not in rendered
        assert base_url not in rendered
        http.post_status = 200
        retried = sync.sync(str(workspace))
        assert retried["status"] == "synced"
        assert retried["pendingCount"] == 0
        assert len(http.posts) == 2
        store.close()


def test_same_business_content_skips_post_but_advances_local_version(
    test_config, workspace
):
    """Including revisions in the business fingerprint must fail this test."""
    with _board_server() as (http, base_url):
        _configure(test_config, base_url)
        store, continuity, sync = _services(test_config, workspace)
        created = _create(continuity, workspace)
        assert sync.sync(str(workspace))["status"] == "synced"
        continuity.checkpoint(ContinuityCheckpointRequest(
            action="update", workspace_path=str(workspace),
            workstream_id=created.workstream_id,
            expected_checkpoint_revision=1, expected_state_version=1,
        ))

        repeated = sync.sync(str(workspace))

        assert repeated["status"] == "unchanged"
        assert repeated["workstreams"][0]["checkpointRevision"] == 2
        assert repeated["workstreams"][0]["stateVersion"] == 2
        assert len(http.posts) == 1
        assert sync.status(str(workspace))["pendingCount"] == 0
        store.close()


def test_status_detects_new_authoritative_content_not_yet_queued(
    test_config, workspace
):
    """A stale sent row must not hide a newer committed business snapshot."""
    with _board_server() as (http, base_url):
        _configure(test_config, base_url)
        store, continuity, sync = _services(test_config, workspace)
        created = _create(continuity, workspace)
        assert sync.sync(str(workspace))["status"] == "synced"
        continuity.checkpoint(ContinuityCheckpointRequest(
            action="update", workspace_path=str(workspace),
            workstream_id=created.workstream_id,
            current_step="A newer unsent step",
            expected_checkpoint_revision=1, expected_state_version=1,
        ))

        status = sync.status(str(workspace))

        assert status["status"] == "pending"
        assert status["pendingCount"] == 1
        assert status["workstreams"][0]["checkpointRevision"] == 2
        assert len(http.posts) == 1
        store.close()


def test_changed_binding_identity_forces_post_of_same_content(
    test_config, workspace
):
    """Remembering only the content hash must fail after a remote rebind."""
    with _board_server() as (http, base_url):
        _configure(test_config, base_url)
        store, continuity, sync = _services(test_config, workspace)
        _create(continuity, workspace)
        sync.sync(str(workspace))
        http.binding_items = [{**http.binding_items[0],
                               "projectRecordId": "record-2",
                               "bindingId": "binding-2"}]

        result = sync.sync(str(workspace))

        assert result["status"] == "synced"
        assert len(http.posts) == 2
        assert http.posts[-1]["projectRecordId"] == "record-2"
        assert http.posts[-1]["bindingId"] == "binding-2"
        store.close()


def test_foreign_workstream_is_rejected_before_any_http_request(
    test_config, workspace, tmp_path
):
    """A workstream ID from another bound workspace must not leave the host."""
    with _board_server() as (http, base_url):
        _configure(test_config, base_url)
        store, continuity, sync = _services(test_config, workspace)
        other = tmp_path / "other"
        other.mkdir()
        provider = sync.workspace_identity
        other_fingerprint = provider.resolve(str(other)).fingerprint
        with store.transaction():
            ProjectStore(store._connection(), store._require_transaction,
                         generic_names=()).bind_workspace(
                other_fingerprint, "proj", method="test", make_default=True
            )
        foreign = _create(continuity, other)

        result = sync.sync(str(workspace), workstream_id=foreign.workstream_id)

        assert result["status"] == "not_bound"
        assert result["workstreamCount"] == 0
        assert http.get_paths == []
        assert http.posts == []
        store.close()


def test_foreign_workstream_rejection_does_not_flush_pending_peer(
    test_config, workspace, tmp_path
):
    """Pending peers must not bypass validation of an explicitly requested ID."""
    with _board_server(post_status=503) as (http, base_url):
        _configure(test_config, base_url)
        store, continuity, sync = _services(test_config, workspace)
        local = _create(continuity, workspace, objective="Pending local")
        assert sync.sync(str(workspace), workstream_id=local.workstream_id)[
            "status"
        ] == "pending"
        other = tmp_path / "other-with-pending"
        other.mkdir()
        provider = sync.workspace_identity
        other_fingerprint = provider.resolve(str(other)).fingerprint
        with store.transaction():
            ProjectStore(store._connection(), store._require_transaction,
                         generic_names=()).bind_workspace(
                other_fingerprint, "proj", method="test", make_default=True
            )
        foreign = _create(continuity, other, objective="Foreign")
        http.get_paths.clear()
        http.posts.clear()

        result = sync.sync(str(workspace), workstream_id=foreign.workstream_id)

        assert result["status"] == "not_bound"
        assert result["workstreamCount"] == 0
        assert http.get_paths == []
        assert http.posts == []
        store.close()


def test_disabled_or_unsafe_config_returns_stable_redacted_status(
    test_config, workspace
):
    """Unsafe config permissions or disabled=false must fail closed."""
    store, continuity, sync = _services(test_config, workspace)
    _create(continuity, workspace)
    path = _configure(test_config, "http://127.0.0.1:9", enabled=False)
    assert sync.sync(str(workspace))["status"] == "disabled"
    _configure(test_config, "http://127.0.0.1:9")
    os.chmod(path, 0o644)
    result = sync.sync(str(workspace))
    assert result == {"status": "disabled", "message": "project board sync is disabled"}
    assert "temporary-secret-key" not in json.dumps(result)
    store.close()


def _mcp_server(test_config, store, provider):
    test_config.context_mode = "compat"
    test_config.adapter = "kimi"
    server = MemoryMCPServer(config=test_config)
    server._init_done.set()
    server.context_service = type("Context", (), {"store": store})()
    server._workspace_identity_provider = provider
    return server


def _mcp_call(server, name, args):
    response = server._handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": name, "arguments": args},
    })["result"]
    return response, json.loads(response["content"][0]["text"])


def test_mcp_contract_exposes_manual_sync_and_readonly_status():
    """Omitting either public tool or loosening its schema must fail."""
    specs = {spec.name: spec for spec in tool_specs(
        adapter="kimi", mode=ContextMode.COMPAT, health=None
    )}
    assert {"project_board_sync", "project_board_status"} <= set(specs)
    for name in ("project_board_sync", "project_board_status"):
        schema = specs[name].input_schema
        assert schema["required"] == ["workspace_path"]
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == {
            "workspace_path", "project_hint", "workstream_id",
        }
    assert specs["project_board_status"].annotations["readOnlyHint"] is True
    assert specs["project_board_sync"].annotations.get("readOnlyHint") is not True


def test_mcp_checkpoint_auto_syncs_only_after_committed_progress_change(
    test_config, workspace
):
    """Auto-sync before persistence, on create, or outside MCP must fail."""
    with _board_server(post_status=503) as (http, base_url):
        _configure(test_config, base_url)
        store, continuity, _sync = _services(test_config, workspace)
        server = _mcp_server(
            test_config, store, continuity._workspace_identity
        )
        created_response, created = _mcp_call(server, "continuity_checkpoint", {
            "action": "create", "workspace_path": str(workspace),
            "objective": "Automatic", "current_step": "Build",
            "next_action": "Test",
        })
        assert "isError" not in created_response
        assert http.posts == []

        update_response, updated = _mcp_call(server, "continuity_checkpoint", {
            "action": "update", "workspace_path": str(workspace),
            "workstream_id": created["workstream_id"],
            "current_step": "Test",
            "expected_checkpoint_revision": 1,
            "expected_state_version": 1,
        })

        assert "isError" not in update_response
        assert updated["checkpoint_revision"] == 2
        assert updated["status"] == "open"
        assert updated["project_board_sync"]["status"] == "pending"
        assert updated["project_board_sync"]["workstreams"][0][
            "checkpointRevision"
        ] == 2
        assert http.posts[0]["checkpointRevision"] == 2
        status_response, status = _mcp_call(server, "project_board_status", {
            "workspace_path": str(workspace),
            "workstream_id": created["workstream_id"],
        })
        assert "isError" not in status_response
        assert status["status"] == "pending"
        get_count = len(http.get_paths)
        assert len(http.posts) == 1

        http.post_status = 200
        sync_response, synced = _mcp_call(server, "project_board_sync", {
            "workspace_path": str(workspace),
            "workstream_id": created["workstream_id"],
        })
        assert "isError" not in sync_response
        assert synced["status"] == "synced"
        assert len(http.get_paths) == get_count + 1
        assert len(http.posts) == 2
        store.close()


def test_all_progress_actions_trigger_sync_while_create_and_focus_do_not(
    test_config, workspace
):
    """Dropping any required action from the automatic trigger set must fail."""
    with _board_server() as (http, base_url):
        _configure(test_config, base_url)
        store, continuity, _sync = _services(test_config, workspace)
        server = _mcp_server(test_config, store, continuity._workspace_identity)
        _response, first = _mcp_call(server, "continuity_checkpoint", {
            "action": "create", "workspace_path": str(workspace),
            "objective": "Lifecycle", "current_step": "Open",
        })
        assert http.posts == []
        revision = 1
        for action, expected_status in (
            ("update", "open"), ("pause", "paused"), ("resume", "open"),
            ("block", "blocked"), ("unblock", "open"),
            ("complete", "completed"),
        ):
            _response, result = _mcp_call(server, "continuity_checkpoint", {
                "action": action, "workspace_path": str(workspace),
                "workstream_id": first["workstream_id"],
                "expected_checkpoint_revision": revision,
                "expected_state_version": revision,
            })
            revision += 1
            assert result["status"] == expected_status

        _response, second = _mcp_call(server, "continuity_checkpoint", {
            "action": "create", "workspace_path": str(workspace),
            "objective": "Cancelled lifecycle",
        })
        _mcp_call(server, "continuity_checkpoint", {
            "action": "cancel", "workspace_path": str(workspace),
            "workstream_id": second["workstream_id"],
            "expected_checkpoint_revision": 1,
            "expected_state_version": 1,
        })
        assert [payload["status"] for payload in http.posts] == [
            "open", "paused", "open", "blocked", "open", "completed",
            "cancelled",
        ]
        store.close()


def test_project_board_cli_status_uses_explicit_data_dir(
    test_config, workspace, capsys, monkeypatch, tmp_path
):
    """A process EVOLVMEM_DATA_DIR must not redirect explicit CLI scope."""
    store, continuity, _sync = _services(test_config, workspace)
    _create(continuity, workspace)
    _configure(test_config, "http://127.0.0.1:9")
    monkeypatch.setenv("EVOLVMEM_DATA_DIR", str(tmp_path / "wrong-data"))

    code = main([
        "--data-dir", str(test_config.data_dir), "status", str(workspace),
    ])

    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["status"] == "pending"
    assert output["project"] == "proj"
    assert output["workstreamCount"] == 1
    assert "temporary-secret-key" not in json.dumps(output)
    store.close()


@pytest.mark.parametrize("action, expected_code", (("sync", 1), ("status", 0)))
def test_project_board_cli_local_state_error_is_pending(
    test_config, workspace, capsys, action, expected_code
):
    """An operational local-state failure must keep delivery retryable."""
    store, continuity, sync = _services(test_config, workspace)
    _create(continuity, workspace)
    _configure(test_config, "http://127.0.0.1:9")
    store.close()
    sync.state_path.mkdir()

    code = main([
        "--data-dir", str(test_config.data_dir), action, str(workspace),
    ])

    output = json.loads(capsys.readouterr().out)
    assert code == expected_code
    assert output == {
        "status": "pending",
        "message": "project board sync remains pending",
    }
    assert "temporary-secret-key" not in json.dumps(output)


def test_status_without_prior_delivery_is_read_only(test_config, workspace):
    """A status read must not create the delivery-state database."""
    _configure(test_config, "http://127.0.0.1:9")
    store, continuity, sync = _services(test_config, workspace)
    _create(continuity, workspace)
    assert not sync.state_path.exists()

    result = sync.status(str(workspace))

    assert result["status"] == "pending"
    assert result["pendingCount"] == 1
    assert not sync.state_path.exists()
    store.close()

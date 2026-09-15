"""Behavioral tests for the native Windows Codex PowerShell client.

The tests execute the script against a synthetic Streamable HTTP endpoint.
They intentionally do not inspect the PowerShell source text.
"""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import tomllib

import pytest


ROOT = Path(__file__).parents[1]
CLIENT = ROOT / "scripts/windows/evolvmem-codex.ps1"
INSTALLER = ROOT / "scripts/windows/install-evolvmem.ps1"
PWSH = Path(os.environ.get("EVOLVMEM_TEST_PWSH", "/tmp/pwsh-7.6.6/pwsh"))


@pytest.fixture(scope="module", autouse=True)
def require_pwsh():
    if not PWSH.exists():
        pytest.skip("portable PowerShell is not available")


class FakeMcp:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers["Content-Length"]))
                request = json.loads(raw)
                outer.requests.append(
                    {
                        "request": request,
                        "authorization": self.headers.get("Authorization"),
                        "expected_user": self.headers.get("X-EvolvMem-Expected-User"),
                        "content_type": self.headers.get("Content-Type"),
                        "raw": raw,
                    }
                )
                name = request.get("params", {}).get("name")
                next_item = outer.responses[0] if outer.responses else None
                configured_status = (
                    name == "memory_status"
                    and isinstance(next_item, dict)
                    and "active_memories" in next_item
                )
                if name == "memory_status" and not configured_status:
                    item = {
                        "authenticated_user": "alice",
                        "active_memories": 0,
                        "memory_revision": 0,
                    }
                else:
                    item = outer.responses.pop(0)
                if isinstance(item, int):
                    self.send_response(item)
                    body = b'{"error":"synthetic"}'
                else:
                    tool_result = item.get("_rpc_result") if "_rpc_result" in item else {
                            "content": [{"type": "text", "text": json.dumps(item)}],
                            "isError": False,
                        }
                    body = json.dumps(
                        {"jsonrpc": "2.0", "id": request["id"], "result": tool_result}
                    ).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/mcp"

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=3)
        self.server.server_close()


@pytest.fixture
def client_home(tmp_path):
    home = tmp_path / "client"
    home.mkdir()
    return home


def write_config(home: Path, url: str, *, strict=False, projects=None, user="alice"):
    (home / "config.json").write_text(
        json.dumps(
            {
                "url": url,
                "token_env_var": "EVOLVMEM_TEST_TOKEN",
                "expected_user": user,
                "device_id": "device-00000000-0000-0000-0000-000000000001",
                "projects": projects or {},
                "strict_injection": strict,
            }
        ),
        encoding="utf-8",
    )


def run_script(
    home: Path, action: str, payload=None, *, token="top-secret-token", extra_env=None
):
    env = os.environ.copy()
    env.update(
        EVOLVMEM_CLIENT_HOME=str(home),
        EVOLVMEM_TEST_TOKEN=token,
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [str(PWSH), "-NoLogo", "-NoProfile", "-File", str(CLIENT), "-Action", action],
        input=json.dumps(payload) if payload is not None else "",
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
    )


def call_args(fake: FakeMcp, index=0):
    return fake.requests[index]["request"]["params"]["arguments"]


def call_name(fake: FakeMcp, index=0):
    return fake.requests[index]["request"]["params"]["name"]


def queue_plaintext_upload(home: Path, content: bytes, *, session="queued-session"):
    queue = home / "queue"
    queue.mkdir(exist_ok=True)
    sha = hashlib.sha256(content).hexdigest()
    stem = f"fixture-{sha}"
    (queue / f"{stem}.bin").write_bytes(content)
    (queue / f"{stem}.json").write_text(
        json.dumps(
            {
                "version": 1,
                "session_id": session,
                "project": "demo",
                "sha256": sha,
                "total_bytes": len(content),
                "next_offset": 0,
                "extract": True,
            }
        ),
        encoding="utf-8",
    )
    return sha, queue / f"{stem}.bin", queue / f"{stem}.json"


def run_portable_upload_harness(home: Path, *, token="top-secret-token"):
    # The portable test replaces only Windows DPAPI decryption. It dot-sources
    # the real client and exercises the real queue/RPC/receipt implementation.
    command = (
        f". '{CLIENT}' -Action snapshot; "
        "function Unprotect-Bytes([byte[]]$Bytes) { return $Bytes }; "
        "$n = Invoke-WithClientLock { Invoke-UploadQueue (Get-Config) } 100 'upload'; "
        "Write-OutputJson @{ acknowledged_versions = $n }"
    )
    env = os.environ.copy()
    env.update(EVOLVMEM_CLIENT_HOME=str(home), EVOLVMEM_TEST_TOKEN=token)
    return subprocess.run(
        [str(PWSH), "-NoLogo", "-NoProfile", "-Command", command],
        input="",
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
    )


def test_session_start_injects_bounded_context_and_reports_unbound(client_home):
    fake = FakeMcp(
        [
            {
                "authenticated_user": "alice",
                    "block": "memory " * 2000,
                    "selected_ids": [],
                    "memory_revision": 7,
                    "continuation": {"status": "no_focus", "checkpoint_revision": 0},
                    "continuation_code": "NO_FOCUS",
            }
        ]
    )
    try:
        write_config(client_home, fake.url)
        result = run_script(
            client_home,
            "session-start",
            {
                "session_id": "session-a",
                "cwd": "/unmapped/workspace",
                "source": "resume",
                "hook_event_name": "SessionStart",
            },
        )

        assert result.returncode == 0, result.stderr
        output = json.loads(result.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
        assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "workspace is not bound" in context
        assert len(context) <= 8000
        assert [call_name(fake, i) for i in range(2)] == [
            "memory_status", "context_session_start"
        ]
        assert call_args(fake, 1)["project"] == ""
        assert call_args(fake, 1)["workspace_path"] == "/unmapped/workspace"
        assert fake.requests[0]["authorization"] == "Bearer top-secret-token"
        assert fake.requests[0]["expected_user"] == "alice"
        assert "top-secret-token" not in result.stdout + result.stderr
        receipt = json.loads(next((client_home / "receipts").glob("*.json")).read_text())
        assert receipt["status"] == "success"
        assert receipt["first_prompt_pending"] is True
        assert receipt["source"] == "resume"
        assert receipt["memory_revision"] == 7
        assert receipt["continuation"]["status"] == "no_focus"
        assert receipt["continuation_code"] == "NO_FOCUS"
    finally:
        fake.close()


def test_session_context_carries_stable_connection_identity_within_budget(client_home):
    fake = FakeMcp(
        [{"authenticated_user": "alice", "block": "history " * 2000,
          "selected_ids": [], "memory_revision": 1}]
    )
    try:
        write_config(
            client_home,
            fake.url,
            projects={r"C:\work\项目": "demo-project"},
        )
        result = run_script(
            client_home,
            "session-start",
            {"session_id": "session-meta", "cwd": r"C:\work\项目", "source": "startup"},
        )

        assert result.returncode == 0, result.stderr
        context = json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
        marker = "[EvolvMem connection metadata]\n"
        assert marker in context
        metadata_line = context.split(marker, 1)[1].splitlines()[0]
        metadata = json.loads(metadata_line)
        assert metadata["device_id"] == "device-00000000-0000-0000-0000-000000000001"
        assert metadata["workspace_path"] == r"C:\work\项目"
        assert metadata["project"] == "demo-project"
        assert metadata["session_id"] == "session-meta"
        assert "do not replace device_id with a hostname" in context
        assert call_args(fake, 1)["max_chars"] == 6400
        assert len(context) <= 8000
    finally:
        fake.close()


def test_portable_pwsh_sends_unicode_json_as_explicit_utf8_bytes(client_home):
    fake = FakeMcp(
        [{"authenticated_user": "alice", "block": "ok", "selected_ids": [], "memory_revision": 1}]
    )
    try:
        write_config(client_home, fake.url, projects={r"C:\研发": "橙园"})
        result = run_script(
            client_home,
            "session-start",
            {"session_id": "unicode-wire", "cwd": r"C:\研发", "source": "startup"},
        )

        assert result.returncode == 0, result.stderr
        assert fake.requests[1]["content_type"].lower() == "application/json; charset=utf-8"
        assert "橙园".encode("utf-8") in fake.requests[1]["raw"]
        assert call_args(fake, 1)["project"] == "橙园"
    finally:
        fake.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell 5.1 proof is Windows-only")
def test_windows_powershell_51_sends_unicode_json_as_utf8(client_home):
    fake = FakeMcp(
        [{"authenticated_user": "alice", "block": "ok", "selected_ids": [], "memory_revision": 1}]
    )
    try:
        write_config(client_home, fake.url, projects={r"C:\研发": "橙园"})
        env = os.environ.copy()
        env.update(EVOLVMEM_CLIENT_HOME=str(client_home), EVOLVMEM_TEST_TOKEN="top-secret-token")
        payload = json.dumps(
            {"session_id": "unicode-ps51", "cwd": r"C:\研发", "source": "startup"},
            ensure_ascii=False,
        )
        result = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-File", str(CLIENT),
             "-Action", "session-start"],
            input=payload.encode("utf-8"), capture_output=True, env=env, timeout=15,
        )
        assert result.returncode == 0, result.stderr.decode(errors="replace")
        assert call_args(fake, 1)["project"] == "橙园"
        assert fake.requests[1]["content_type"].lower() == "application/json; charset=utf-8"
    finally:
        fake.close()


def test_upload_preflights_identity_and_never_sends_snapshot_to_wrong_user(client_home):
    fake = FakeMcp([{"authenticated_user": "mallory", "active_memories": 0}])
    try:
        write_config(client_home, fake.url, user="alice")
        _, data_path, manifest_path = queue_plaintext_upload(
            client_home, b'{"type":"event_msg","payload":{"message":"private"}}\n'
        )
        result = run_portable_upload_harness(client_home)

        assert result.returncode == 0, result.stderr
        assert [call_name(fake, i) for i in range(len(fake.requests))] == ["memory_status"]
        assert data_path.exists() and manifest_path.exists()
        assert "top-secret-token" not in result.stdout + result.stderr
    finally:
        fake.close()


@pytest.mark.parametrize("bad_field", [None, "sha256", "total_bytes"])
def test_stale_upload_clears_queue_only_for_exact_submitted_snapshot_ack(
    client_home, bad_field
):
    old = b'{"type":"event_msg","payload":{"message":"old"}}\n'
    old_sha, data_path, manifest_path = queue_plaintext_upload(client_home, old)
    submitted_sha = "0" * 64 if bad_field == "sha256" else old_sha
    submitted_total = len(old) + 1 if bad_field == "total_bytes" else len(old)
    fake = FakeMcp(
        [
            {"authenticated_user": "alice", "active_memories": 0},
            {
                "authenticated_user": "alice",
                "status": "stale",
                "next_offset": 610,
                "total_bytes": 610,
                "source_sha256": "b" * 64,
                "submitted_total_bytes": submitted_total,
                "submitted_sha256": submitted_sha,
                "archive_id": "archive-newer",
                "extraction_status": "pending",
            },
        ]
    )
    try:
        write_config(client_home, fake.url)
        result = run_portable_upload_harness(client_home)

        assert result.returncode == 0, result.stderr
        assert [call_name(fake, i) for i in range(2)] == [
            "memory_status", "session_archive_upload"
        ]
        assert data_path.exists() is (bad_field is not None)
        assert manifest_path.exists() is (bad_field is not None)
    finally:
        fake.close()


def test_prompt_gate_retries_failed_current_start_and_injects_result(client_home):
    fake = FakeMcp(
        [
            503,
            {"authenticated_user": "alice", "block": "fresh prompt memory", "selected_ids": [4], "memory_revision": 2},
            {"authenticated_user": "alice", "results": []},
        ]
    )
    try:
        write_config(client_home, fake.url)
        event = {
            "session_id": "session-retry",
            "cwd": "/work",
            "source": "startup",
            "hook_event_name": "SessionStart",
        }
        first = run_script(client_home, "session-start", event)
        assert first.returncode == 0
        gate = run_script(
            client_home,
            "prompt-submit",
            {
                "session_id": "session-retry",
                "cwd": "/work",
                "prompt": "continue the migration",
                "hook_event_name": "UserPromptSubmit",
            },
        )

        assert gate.returncode == 0, gate.stderr
        output = json.loads(gate.stdout)
        assert output["hookSpecificOutput"]["additionalContext"].endswith("fresh prompt memory")
        assert "workspace is not bound" in output["hookSpecificOutput"]["additionalContext"]
        assert call_args(fake, 3)["query"] == "continue the migration"
        receipt = json.loads(next((client_home / "receipts").glob("*.json")).read_text())
        assert receipt["first_prompt_pending"] is False
        assert receipt["status"] == "success"
    finally:
        fake.close()


def test_prompt_gate_keeps_recovered_context_when_experience_lookup_fails(client_home):
    fake = FakeMcp(
        [
            503,
            {
                "authenticated_user": "alice",
                "block": "recovered memory",
                "selected_ids": [9],
                "memory_revision": 4,
                "continuation": {"status": "active", "checkpoint_revision": 2},
                "continuation_code": "ACTIVE",
            },
            503,
        ]
    )
    try:
        write_config(client_home, fake.url)
        start = run_script(
            client_home,
            "session-start",
            {"session_id": "session-experience-fail", "cwd": "/work", "source": "startup"},
        )
        gate = run_script(
            client_home,
            "prompt-submit",
            {"session_id": "session-experience-fail", "cwd": "/work", "prompt": "resume task"},
        )

        assert start.returncode == gate.returncode == 0
        output = json.loads(gate.stdout)
        assert output["hookSpecificOutput"]["additionalContext"].endswith("recovered memory")
        assert "experience" in output["systemMessage"].lower()
        receipt = json.loads(next((client_home / "receipts").glob("*.json")).read_text())
        assert receipt["status"] == "success"
        assert receipt["continuation_code"] == "ACTIVE"
        assert receipt["first_prompt_pending"] is False
    finally:
        fake.close()


def test_prompt_gate_never_accepts_previous_start_receipt(client_home):
    fake = FakeMcp([503, 503])
    try:
        write_config(client_home, fake.url, strict=True)
        receipts = client_home / "receipts"
        receipts.mkdir()
        old = {
            "session_id": "same-session",
            "start_id": "previous-start",
            "source": "startup",
            "status": "success",
            "first_prompt_pending": False,
        }
        (receipts / "same-session.json").write_text(json.dumps(old))

        start = run_script(
            client_home,
            "session-start",
            {
                "session_id": "same-session",
                "cwd": "/work",
                "source": "resume",
                "hook_event_name": "SessionStart",
            },
        )
        gate = run_script(
            client_home,
            "prompt-submit",
            {
                "session_id": "same-session",
                "cwd": "/work",
                "prompt": "first prompt after resume",
                "hook_event_name": "UserPromptSubmit",
            },
        )

        assert start.returncode == 0
        assert gate.returncode == 0
        assert json.loads(gate.stdout) == {
            "decision": "block",
            "reason": "EvolvMem memory injection is unavailable for this session start. Retry the prompt after connectivity is restored.",
        }
        assert "top-secret-token" not in gate.stdout + gate.stderr
    finally:
        fake.close()


def test_default_prompt_gate_warns_and_continues_when_retry_fails(client_home):
    fake = FakeMcp([503, 503])
    try:
        write_config(client_home, fake.url)
        start = run_script(
            client_home,
            "session-start",
            {"session_id": "session-open", "cwd": "/work", "source": "clear"},
        )
        gate = run_script(
            client_home,
            "prompt-submit",
            {"session_id": "session-open", "cwd": "/work", "prompt": "keep going"},
        )

        assert start.returncode == gate.returncode == 0
        output = json.loads(gate.stdout)
        assert "decision" not in output
        assert "unavailable" in output["systemMessage"]
        assert "top-secret-token" not in gate.stdout + gate.stderr
    finally:
        fake.close()


def test_snapshot_does_not_wait_for_background_upload_lock(client_home, tmp_path):
    write_config(client_home, "http://127.0.0.1:1/mcp")
    legacy_name = "EvolvMemCodex-" + hashlib.sha256(
        str(client_home).encode()
    ).hexdigest()[:20]
    upload_name = "EvolvMemCodex-" + hashlib.sha256(
        (str(client_home) + "|upload").encode()
    ).hexdigest()[:20]
    ready = tmp_path / "mutex-ready"
    holder_code = (
        "$a=New-Object Threading.Mutex($false,$env:EVOLVMEM_TEST_LEGACY_LOCK);"
        "$b=New-Object Threading.Mutex($false,$env:EVOLVMEM_TEST_UPLOAD_LOCK);"
        "[void]$a.WaitOne();[void]$b.WaitOne();"
        "[IO.File]::WriteAllText($env:EVOLVMEM_TEST_READY,'ready');"
        "Start-Sleep -Milliseconds 3000;"
        "$b.ReleaseMutex();$a.ReleaseMutex();$b.Dispose();$a.Dispose()"
    )
    holder_env = os.environ.copy()
    holder_env.update(
        EVOLVMEM_TEST_LEGACY_LOCK=legacy_name,
        EVOLVMEM_TEST_UPLOAD_LOCK=upload_name,
        EVOLVMEM_TEST_READY=str(ready),
    )
    holder = subprocess.Popen(
        [str(PWSH), "-NoLogo", "-NoProfile", "-Command", holder_code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=holder_env,
    )
    try:
        deadline = time.monotonic() + 2
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), holder.stderr.read().decode(errors="replace")
        started = time.monotonic()
        result = run_script(
            client_home,
            "snapshot",
            {
                "session_id": "independent-session",
                "cwd": str(tmp_path),
                "transcript_path": str(tmp_path / "not-yet-created.jsonl"),
            },
        )
        elapsed = time.monotonic() - started
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == {}
        assert elapsed < 1.5
    finally:
        holder.wait(timeout=5)


def test_prompt_refreshes_context_when_server_memory_revision_changed(client_home):
    fake = FakeMcp(
        [
            {"authenticated_user": "alice", "block": "startup memory", "selected_ids": [1], "memory_revision": 1},
            {"authenticated_user": "alice", "active_memories": 2, "memory_revision": 2},
            {"authenticated_user": "alice", "block": "new cross-device memory", "selected_ids": [1, 2], "memory_revision": 2},
            {"authenticated_user": "alice", "results": []},
        ]
    )
    try:
        write_config(client_home, fake.url)
        start = run_script(
            client_home,
            "session-start",
            {"session_id": "session-version", "cwd": "/work", "source": "startup"},
        )
        assert start.returncode == 0
        gate = run_script(
            client_home,
            "prompt-submit",
            {"session_id": "session-version", "cwd": "/work", "prompt": "use the latest decision"},
        )

        assert gate.returncode == 0, gate.stderr
        context = json.loads(gate.stdout)["hookSpecificOutput"]["additionalContext"]
        assert context.endswith("new cross-device memory")
        assert "workspace is not bound" in context
        assert [call_name(fake, i) for i in range(5)] == [
            "memory_status", "context_session_start", "memory_status",
            "context_session_start", "experience_recall"
        ]
        assert call_args(fake, 3)["query"] == "use the latest decision"
    finally:
        fake.close()


def test_session_start_uses_longest_explicit_mapping_and_real_git_observation(
    client_home, tmp_path
):
    repo = tmp_path / "parent" / "nested"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.invalid"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "tracked.txt").write_text("fixture")
    subprocess.run(["git", "-C", str(repo), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture"], check=True)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    branch = subprocess.check_output(
        ["git", "-C", str(repo), "symbolic-ref", "--short", "HEAD"], text=True
    ).strip()
    fake = FakeMcp(
        [{"authenticated_user": "alice", "block": "bound", "selected_ids": [], "memory_revision": 1}]
    )
    try:
        write_config(
            client_home,
            fake.url,
            projects={str(tmp_path / "parent"): "parent-project", str(repo): "nested-project"},
        )
        result = run_script(
            client_home,
            "session-start",
            {"session_id": "git-session", "cwd": str(repo), "source": "startup"},
        )
        assert result.returncode == 0, result.stderr
        args = call_args(fake, 1)
        assert args["project"] == "nested-project"
        assert args["device_id"].startswith("device-")
        assert args["repo_snapshot"] == {
            "kind": "git",
            "branch": branch,
            "root_commit": head,
            "head_commit": head,
        }
        assert "workspace is not bound" not in json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]
    finally:
        fake.close()


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI CurrentUser is Windows-only")
def test_snapshot_copies_only_complete_jsonl_and_keeps_every_unacknowledged_version(
    client_home, tmp_path
):
    write_config(client_home, "http://127.0.0.1:1/mcp", projects={str(tmp_path): "demo"})
    transcript = tmp_path / "rollout.jsonl"
    first_complete = b'{"type":"one"}\r\n{"type":"two"}\n'
    transcript.write_bytes(first_complete + b'{"type":"partial"')
    payload = {
        "session_id": "session-snapshot",
        "cwd": str(tmp_path),
        "transcript_path": str(transcript),
        "hook_event_name": "SessionEnd",
    }

    first = run_script(client_home, "snapshot", payload)
    assert first.returncode == 0, first.stderr
    assert json.loads(first.stdout) == {}
    queued = sorted((client_home / "queue").glob("*.bin"))
    assert len(queued) == 1
    assert unprotect_dpapi(queued[0]) == first_complete
    assert transcript.read_bytes().endswith(b'{"type":"partial"')

    transcript.write_bytes(first_complete + b'{"type":"three"}\npartial')
    second = run_script(client_home, "snapshot", payload)
    assert second.returncode == 0, second.stderr
    queued = sorted((client_home / "queue").glob("*.bin"))
    assert len(queued) == 2
    manifests = [json.loads(path.read_text()) for path in (client_home / "queue").glob("*.json")]
    assert {item["sha256"] for item in manifests} == {
        hashlib.sha256(unprotect_dpapi(path)).hexdigest() for path in queued
    }
    assert all(item["next_offset"] == 0 for item in manifests)
    assert all(item["project"] == "demo" for item in manifests)

    rewritten = b'{"type":"rewritten-prefix-with-padding"}\n{"type":"new-tail"}\n'
    assert len(rewritten) >= len(first_complete + b'{"type":"three"}\n')
    transcript.write_bytes(rewritten)
    third = run_script(client_home, "snapshot", payload)
    assert third.returncode == 0, third.stderr
    queued = sorted((client_home / "queue").glob("*.bin"))
    assert len(queued) == 3
    plaintext_versions = [unprotect_dpapi(path) for path in queued]
    assert rewritten in plaintext_versions
    assert first_complete in plaintext_versions


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI CurrentUser is Windows-only")
def test_upload_resumes_at_acknowledged_offset_with_stable_request_ids(client_home):
    first = b'{"value":"' + (b"a" * 262200) + b'"}\n'
    second = b'{"value":"' + (b"b" * 1000) + b'"}\n'
    content = first + second
    sha = hashlib.sha256(content).hexdigest()
    fake = FakeMcp(
        [
            {"authenticated_user": "alice", "status": "receiving", "next_offset": 262144},
            503,
            {
                "authenticated_user": "alice",
                "status": "archived",
                "next_offset": len(content),
                "archive_id": "archive-1",
                "source_sha256": sha,
                "extraction_status": "pending",
            },
        ]
    )
    try:
        transcript = client_home / "rollout.jsonl"
        transcript.write_bytes(content)
        write_config(client_home, fake.url, projects={str(client_home): "demo"})
        snap = run_script(
            client_home,
            "snapshot",
            {
                "session_id": "session-upload",
                "cwd": str(client_home),
                "transcript_path": str(transcript),
            },
        )
        assert snap.returncode == 0
        queue = client_home / "queue"
        manifest_path = next(queue.glob("*.json"))
        snapshot = next(queue.glob("*.bin"))
        failed = run_script(client_home, "upload")
        assert failed.returncode == 0
        saved = json.loads(manifest_path.read_text())
        assert saved["next_offset"] == 262144
        first_id = call_args(fake, 0)["request_id"]
        failed_id = call_args(fake, 1)["request_id"]
        assert len(os.path.commonprefix([first_id, failed_id])) > 10
        assert len(__import__("base64").b64decode(call_args(fake, 0)["content_b64"])) == 262144

        completed = run_script(client_home, "upload")
        assert completed.returncode == 0, completed.stderr
        assert not snapshot.exists()
        assert not manifest_path.exists()
        assert call_args(fake, 2)["offset"] == 262144
        assert call_args(fake, 2)["request_id"] == failed_id
        receipt = json.loads((client_home / "archive-status" / f"{sha}.json").read_text())
        assert receipt["archive_status"] == "archived"
        assert receipt["extraction_status"] == "pending"
    finally:
        fake.close()


def unprotect_dpapi(path: Path) -> bytes:
    command = (
        "$b=[IO.File]::ReadAllBytes($args[0]);"
        "$e=[Text.Encoding]::UTF8.GetBytes('evolvmem-codex-archive-v1');"
        "$p=[Security.Cryptography.ProtectedData]::Unprotect($b,$e,[Security.Cryptography.DataProtectionScope]::CurrentUser);"
        "[Console]::OpenStandardOutput().Write($p,0,$p.Length)"
    )
    result = subprocess.run(
        [str(PWSH), "-NoLogo", "-NoProfile", "-Command", command, str(path)],
        capture_output=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


def test_status_treats_authenticated_empty_memory_as_success(client_home):
    fake = FakeMcp(
        [{"authenticated_user": "alice", "active_memories": 0, "status": "ready"}]
    )
    try:
        write_config(client_home, fake.url)
        result = run_script(client_home, "status")
        assert result.returncode == 0, result.stderr
        status = json.loads(result.stdout)
        assert status["connected"] is True
        assert status["authenticated_user"] == "alice"
        assert status["active_memories"] == 0
        assert status["memory_state"] == "empty"
        assert "top-secret-token" not in result.stdout + result.stderr
    finally:
        fake.close()


def test_status_rejects_mismatched_authenticated_user_without_disclosing_token(client_home):
    fake = FakeMcp([{"authenticated_user": "mallory", "active_memories": 99}])
    try:
        write_config(client_home, fake.url)
        result = run_script(client_home, "status")
        assert result.returncode == 0
        status = json.loads(result.stdout)
        assert status["connected"] is False
        assert status["memory_state"] == "unavailable"
        assert "top-secret-token" not in result.stdout + result.stderr
    finally:
        fake.close()


def test_status_queries_registered_archive_without_returning_transcript_content(client_home, tmp_path):
    fake = FakeMcp(
        [
            {"authenticated_user": "alice", "active_memories": 3, "memory_revision": 8},
            {
                "authenticated_user": "alice",
                "status": "archived",
                "archive_id": "archive-visible",
                "source_sha256": "a" * 64,
                "extraction_status": "pending",
                "processing_error": "",
            },
        ]
    )
    try:
        write_config(client_home, fake.url)
        sessions = client_home / "sessions"
        sessions.mkdir()
        (sessions / "registered.json").write_text(
            json.dumps(
                {
                    "session_id": "registered-session",
                    "transcript_path": str(tmp_path / "secret.jsonl"),
                    "workspace_path": str(tmp_path),
                    "source_bytes": 123,
                }
            )
        )
        result = run_script(client_home, "status")
        assert result.returncode == 0, result.stderr
        output = json.loads(result.stdout)
        assert output["archive_sessions"] == [
            {
                "session_id": "registered-session",
                "archive_status": "archived",
                "archive_id": "archive-visible",
                "source_sha256": "a" * 64,
                "extraction_status": "pending",
                "processing_error": "",
            }
        ]
        assert "secret.jsonl" not in result.stdout
        assert call_name(fake, 1) == "session_archive_status"
        assert call_args(fake, 1) == {
            "device_id": "device-00000000-0000-0000-0000-000000000001",
            "session_id": "registered-session",
        }
    finally:
        fake.close()


def write_native_codex_config(codex: Path, url: str):
    codex.mkdir(parents=True)
    (codex / "config.toml").write_text(
        '[mcp_servers.evolvmem]\n'
        f'url = "{url}"\n'
        'bearer_token_env_var = "EVOLVMEM_TEST_TOKEN"\n'
        'http_headers = { "X-EvolvMem-Expected-User" = "alice" }\n',
        encoding="utf-8",
    )
    actions = {
        "SessionStart": "session-start",
        "UserPromptSubmit": "prompt-submit",
        "Stop": "snapshot",
        "PreCompact": "snapshot",
        "SessionEnd": "snapshot",
        "Interrupt": "snapshot",
    }
    hooks = {"hooks": {}}
    for event, action in actions.items():
        hooks["hooks"][event] = [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": (
                            f'powershell.exe -NoProfile -File "{CLIENT}" -Action {action}'
                        ),
                    }
                ]
            }
        ]
    (codex / "hooks.json").write_text(json.dumps(hooks), encoding="utf-8")


def test_self_test_reports_full_remote_and_native_config_parity(client_home, tmp_path):
    expected = {
        "memory_search", "memory_status", "memory_add", "memory_replace", "memory_remove",
        "memory_consolidate", "memory_publish", "memory_update_public", "memory_unpublish",
        "context_session_start", "context_search", "context_read",
        "context_status", "context_confirm", "context_record_outcome", "context_archive_project",
        "context_sweep", "experience_recall", "experience_record", "continuity_begin",
        "continuity_resume", "continuity_find", "continuity_bind", "continuity_checkpoint",
        "continuity_list", "project_board_sync", "project_board_status", "session_archive_upload",
        "session_archive_status", "session_archive_retry", "session_archive_assign",
    }
    fake = FakeMcp(
        [
            {"authenticated_user": "alice", "active_memories": 0},
            {"_rpc_result": {"tools": [{"name": name} for name in sorted(expected)]}},
        ]
    )
    try:
        write_config(client_home, fake.url)
        profile = tmp_path / "profile"
        codex = tmp_path / "custom-codex-home"
        write_native_codex_config(codex, fake.url)
        result = run_script(
            client_home,
            "self-test",
            extra_env={"USERPROFILE": str(profile), "CODEX_HOME": str(codex)},
        )
        assert result.returncode == 0, result.stderr
        status = json.loads(result.stdout)
        assert status["healthy"] is True
        assert status["remote_connected"] is True
        assert status["missing_tools"] == []
        assert status["tool_count"] == len(expected)
        assert status["mcp_configured"] is True
        assert status["hooks_configured"] is True
        assert status["worker_task_configured"] is None
        assert status["native_event_test_required"] is True
    finally:
        fake.close()


@pytest.mark.parametrize(
    "header_config",
    [
        'http_headers = { "X-Keep" = "yes", "X-EvolvMem-Expected-User" = "alice" } # keep\n',
        '[mcp_servers.evolvmem.http_headers]\nX-Keep = "yes"\nX-EvolvMem-Expected-User = "alice" # keep\n',
    ],
    ids=["inline-comment", "subtable-unquoted-key"],
)
@pytest.mark.parametrize(
    "root_header",
    [
        '[mcp_servers.evolvmem] # memory\n',
        '  [mcp_servers.evolvmem]\n',
    ],
    ids=["commented-root", "indented-root"],
)
def test_self_test_accepts_supported_native_header_forms(
    client_home, tmp_path, header_config, root_header
):
    expected = {
        "memory_search", "memory_status", "memory_add", "memory_replace", "memory_remove",
        "memory_consolidate", "memory_publish", "memory_update_public", "memory_unpublish",
        "context_session_start", "context_search", "context_read", "context_status",
        "context_confirm", "context_record_outcome", "context_archive_project", "context_sweep",
        "experience_recall", "experience_record", "continuity_begin", "continuity_resume",
        "continuity_find", "continuity_bind", "continuity_checkpoint", "continuity_list",
        "project_board_sync", "project_board_status", "session_archive_upload",
        "session_archive_status", "session_archive_retry", "session_archive_assign",
    }
    fake = FakeMcp([
        {"authenticated_user": "alice", "active_memories": 0},
        {"_rpc_result": {"tools": [{"name": name} for name in sorted(expected)]}},
    ])
    try:
        write_config(client_home, fake.url)
        codex = tmp_path / "codex-supported-headers"
        write_native_codex_config(codex, fake.url)
        config_path = codex / "config.toml"
        native = config_path.read_text(encoding="utf-8")
        native = native.replace('[mcp_servers.evolvmem]\n', root_header)
        native = native.replace(
            'http_headers = { "X-EvolvMem-Expected-User" = "alice" }\n',
            header_config,
        )
        config_path.write_text(native, encoding="utf-8")

        result = run_script(
            client_home,
            "self-test",
            extra_env={"USERPROFILE": str(tmp_path / "profile"), "CODEX_HOME": str(codex)},
        )

        assert result.returncode == 0, result.stderr
        status = json.loads(result.stdout)
        assert status["mcp_configured"] is True
        assert status["healthy"] is True
    finally:
        fake.close()


def test_self_test_reports_missing_native_hook_as_unhealthy(client_home, tmp_path):
    expected = {
        "memory_search", "memory_status", "memory_add", "memory_replace", "memory_remove",
        "memory_consolidate", "memory_publish", "memory_update_public", "memory_unpublish",
        "context_session_start", "context_search", "context_read",
        "context_status", "context_confirm", "context_record_outcome", "context_archive_project",
        "context_sweep", "experience_recall", "experience_record", "continuity_begin",
        "continuity_resume", "continuity_find", "continuity_bind", "continuity_checkpoint",
        "continuity_list", "project_board_sync", "project_board_status", "session_archive_upload",
        "session_archive_status", "session_archive_retry", "session_archive_assign",
    }
    fake = FakeMcp(
        [
            {"authenticated_user": "alice", "active_memories": 0},
            {"_rpc_result": {"tools": [{"name": name} for name in sorted(expected)]}},
        ]
    )
    try:
        write_config(client_home, fake.url)
        profile = tmp_path / "profile"
        codex = tmp_path / "custom-codex-home"
        write_native_codex_config(codex, fake.url)
        hooks_path = codex / "hooks.json"
        hooks = json.loads(hooks_path.read_text())
        del hooks["hooks"]["SessionEnd"]
        hooks_path.write_text(json.dumps(hooks))

        result = run_script(
            client_home,
            "self-test",
            extra_env={"USERPROFILE": str(profile), "CODEX_HOME": str(codex)},
        )
        assert result.returncode == 0, result.stderr
        status = json.loads(result.stdout)
        assert status["healthy"] is False
        assert status["mcp_configured"] is True
        assert status["hooks_configured"] is False
        assert status["invalid_hooks"] == ["SessionEnd:snapshot"]
    finally:
        fake.close()


def test_self_test_reports_explicitly_disabled_codex_hooks_feature(client_home, tmp_path):
    expected = {
        "memory_search", "memory_status", "memory_add", "memory_replace", "memory_remove",
        "memory_consolidate", "memory_publish", "memory_update_public", "memory_unpublish",
        "context_session_start", "context_search", "context_read", "context_status",
        "context_confirm", "context_record_outcome", "context_archive_project", "context_sweep",
        "experience_recall", "experience_record", "continuity_begin", "continuity_resume",
        "continuity_find", "continuity_bind", "continuity_checkpoint", "continuity_list",
        "project_board_sync", "project_board_status", "session_archive_upload",
        "session_archive_status", "session_archive_retry", "session_archive_assign",
    }
    fake = FakeMcp([
        {"authenticated_user": "alice", "active_memories": 0},
        {"_rpc_result": {"tools": [{"name": name} for name in sorted(expected)]}},
    ])
    try:
        write_config(client_home, fake.url)
        codex = tmp_path / "codex-hooks-disabled"
        write_native_codex_config(codex, fake.url)
        with (codex / "config.toml").open("a", encoding="utf-8") as handle:
            handle.write("\n[features]\nhooks = false\n")
        result = run_script(
            client_home,
            "self-test",
            extra_env={"USERPROFILE": str(tmp_path / "profile"), "CODEX_HOME": str(codex)},
        )

        assert result.returncode == 0, result.stderr
        status = json.loads(result.stdout)
        assert status["remote_connected"] is True
        assert status["mcp_configured"] is True
        assert status["hooks_configured"] is True
        assert status["hooks_feature_enabled"] is False
        assert status["healthy"] is False
    finally:
        fake.close()


@pytest.mark.parametrize("feature_name", ["hooks", "codex_hooks"])
def test_self_test_detects_disabled_hooks_feature_in_crlf_config(
    client_home, tmp_path, feature_name
):
    expected = {
        "memory_search", "memory_status", "memory_add", "memory_replace", "memory_remove",
        "memory_consolidate", "memory_publish", "memory_update_public", "memory_unpublish",
        "context_session_start", "context_search", "context_read", "context_status",
        "context_confirm", "context_record_outcome", "context_archive_project", "context_sweep",
        "experience_recall", "experience_record", "continuity_begin", "continuity_resume",
        "continuity_find", "continuity_bind", "continuity_checkpoint", "continuity_list",
        "project_board_sync", "project_board_status", "session_archive_upload",
        "session_archive_status", "session_archive_retry", "session_archive_assign",
    }
    fake = FakeMcp([
        {"authenticated_user": "alice", "active_memories": 0},
        {"_rpc_result": {"tools": [{"name": name} for name in sorted(expected)]}},
    ])
    try:
        write_config(client_home, fake.url)
        codex = tmp_path / f"codex-{feature_name}-disabled-crlf"
        write_native_codex_config(codex, fake.url)
        config_path = codex / "config.toml"
        native_config = config_path.read_text(encoding="utf-8").replace("\n", "\r\n")
        config_path.write_bytes(
            (native_config + f"\r\n[features]\r\n{feature_name} = false\r\n").encode()
        )

        result = run_script(
            client_home,
            "self-test",
            extra_env={"USERPROFILE": str(tmp_path / "profile"), "CODEX_HOME": str(codex)},
        )

        assert result.returncode == 0, result.stderr
        status = json.loads(result.stdout)
        assert status["hooks_feature_enabled"] is False
        assert status["healthy"] is False
    finally:
        fake.close()


def test_self_test_distinguishes_invalid_local_config_from_remote_failure(
    client_home, tmp_path
):
    expected = {
        "memory_search", "memory_status", "memory_add", "memory_replace", "memory_remove",
        "memory_consolidate", "memory_publish", "memory_update_public", "memory_unpublish",
        "context_session_start", "context_search", "context_read",
        "context_status", "context_confirm", "context_record_outcome", "context_archive_project",
        "context_sweep", "experience_recall", "experience_record", "continuity_begin",
        "continuity_resume", "continuity_find", "continuity_bind", "continuity_checkpoint",
        "continuity_list", "project_board_sync", "project_board_status", "session_archive_upload",
        "session_archive_status", "session_archive_retry", "session_archive_assign",
    }
    fake = FakeMcp(
        [
            {"authenticated_user": "alice", "active_memories": 0},
            {"_rpc_result": {"tools": [{"name": name} for name in sorted(expected)]}},
        ]
    )
    try:
        write_config(client_home, fake.url)
        profile = tmp_path / "profile"
        codex = tmp_path / "custom-codex-home"
        write_native_codex_config(codex, fake.url)
        (codex / "hooks.json").write_text("{invalid-json", encoding="utf-8")

        result = run_script(
            client_home,
            "self-test",
            extra_env={"USERPROFILE": str(profile), "CODEX_HOME": str(codex)},
        )
        assert result.returncode == 0, result.stderr
        status = json.loads(result.stdout)
        assert status["healthy"] is False
        assert status["remote_connected"] is True
        assert status["mcp_configured"] is True
        assert status["hooks_configured"] is False
        assert status["invalid_hooks"] == ["hooks-config-unreadable"]
        assert "could not reach" not in status["note"]
    finally:
        fake.close()


def test_installer_merges_hooks_and_toml_without_duplicate_or_identity_overwrite(tmp_path):
    appdata = tmp_path / "local"
    userprofile = tmp_path / "profile"
    codex = tmp_path / "custom-codex-home"
    codex.mkdir(parents=True)
    (codex / "config.toml").write_text('model = "keep-me"\n[projects."C:\\\\work"]\ntrust_level = "trusted"\n')
    (codex / "hooks.json").write_text(
        json.dumps(
            {
                "description": "keep this",
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {"type": "command", "command": "existing-stop.cmd"},
                                {
                                    "type": "command",
                                    "command": (
                                        'powershell.exe -File "D:\\other\\evolvmem-codex.ps1" '
                                        '-Action snapshot'
                                    ),
                                },
                            ]
                        }
                    ]
                },
            }
        )
    )
    env = os.environ.copy()
    env.update(
        LOCALAPPDATA=str(appdata), USERPROFILE=str(userprofile), CODEX_HOME=str(codex)
    )
    command = [
        str(PWSH),
        "-NoLogo",
        "-NoProfile",
        "-File",
        str(INSTALLER),
        "-Url",
        "http://memory.test/mcp",
        "-ExpectedUser",
        "alice",
        "-TokenEnvVar",
        "EVOLVMEM_TEST_TOKEN",
    ]

    first = subprocess.run(command, text=True, capture_output=True, env=env, timeout=15)
    second = subprocess.run(command, text=True, capture_output=True, env=env, timeout=15)
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    toml = (codex / "config.toml").read_text()
    assert toml.count("[mcp_servers.evolvmem]") == 1
    assert toml.count('"X-EvolvMem-Expected-User" = "alice"') == 1
    assert 'model = "keep-me"' in toml
    hooks = json.loads((codex / "hooks.json").read_text())
    assert hooks["description"] == "keep this"
    assert hooks["hooks"]["Stop"][0]["hooks"][0]["command"] == "existing-stop.cmd"
    assert hooks["hooks"]["Stop"][0]["hooks"][1]["command"].startswith(
        'powershell.exe -File "D:\\other\\evolvmem-codex.ps1"'
    )
    installed_client = str(appdata / "EvolvMem/Codex/evolvmem-codex.ps1")
    for event in (
        "SessionStart", "UserPromptSubmit", "Stop", "PreCompact", "SessionEnd", "Interrupt"
    ):
        commands = [
            handler["command"]
            for group in hooks["hooks"][event]
            for handler in group["hooks"]
            if installed_client in handler.get("command", "")
        ]
        assert len(commands) == 1
    session_end_group = next(
        group
        for group in hooks["hooks"]["SessionEnd"]
        if any(installed_client in hook.get("command", "") for hook in group["hooks"])
    )
    assert "matcher" not in session_end_group
    config = json.loads((appdata / "EvolvMem/Codex/config.json").read_text())
    device_id = config["device_id"]
    assert config["strict_injection"] is False
    assert json.loads((appdata / "EvolvMem/Codex/config.json").read_text())["device_id"] == device_id

    conflict = subprocess.run(
        [
            str(PWSH), "-NoLogo", "-NoProfile", "-File", str(INSTALLER),
            "-Url", "http://different.test/mcp", "-ExpectedUser", "mallory",
            "-TokenEnvVar", "OTHER_TOKEN",
        ],
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
    )
    assert conflict.returncode != 0
    preserved = json.loads((appdata / "EvolvMem/Codex/config.json").read_text())
    assert preserved["expected_user"] == "alice"
    assert preserved["device_id"] == device_id

    queue = appdata / "EvolvMem/Codex/queue"
    queue.mkdir()
    pending = queue / "pending.bin"
    pending.write_bytes(b"encrypted-placeholder")
    uninstall = subprocess.run(
        [
            str(PWSH), "-NoLogo", "-NoProfile", "-File", str(INSTALLER),
            "-Uninstall",
        ],
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
    )
    assert uninstall.returncode == 0, uninstall.stderr
    assert pending.read_bytes() == b"encrypted-placeholder"
    assert not (appdata / "EvolvMem/Codex/evolvmem-codex.ps1").exists()
    hooks = json.loads((codex / "hooks.json").read_text())
    stop_commands = [
        handler["command"]
        for group in hooks["hooks"]["Stop"]
        for handler in group["hooks"]
    ]
    assert "existing-stop.cmd" in stop_commands
    assert any("D:\\other\\evolvmem-codex.ps1" in command for command in stop_commands)
    assert all(str(appdata / "EvolvMem/Codex") not in command for command in stop_commands)


def test_installer_adds_identity_guard_to_matching_unmanaged_mcp_without_replacing_settings(
    tmp_path,
):
    appdata = tmp_path / "local"
    profile = tmp_path / "profile"
    codex = tmp_path / "codex"
    codex.mkdir()
    original = (
        '[mcp_servers.evolvmem]\n'
        'url = "http://memory.test/mcp"\n'
        'bearer_token_env_var = "EVOLVMEM_TEST_TOKEN"\n'
        'http_headers = { "X-Keep" = "yes" }\n'
        'enabled_tools = ["memory_status"]\n\n'
        '[projects."C:\\\\work"]\n'
        'trust_level = "trusted"\n'
    )
    (codex / "config.toml").write_text(original, encoding="utf-8")
    env = os.environ.copy()
    env.update(LOCALAPPDATA=str(appdata), USERPROFILE=str(profile), CODEX_HOME=str(codex))
    command = [
        str(PWSH), "-NoLogo", "-NoProfile", "-File", str(INSTALLER),
        "-Url", "http://memory.test/mcp", "-ExpectedUser", "alice",
        "-TokenEnvVar", "EVOLVMEM_TEST_TOKEN",
    ]

    first = subprocess.run(command, text=True, capture_output=True, env=env, timeout=15)
    second = subprocess.run(command, text=True, capture_output=True, env=env, timeout=15)

    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    updated = (codex / "config.toml").read_text()
    assert updated.count("[mcp_servers.evolvmem]") == 1
    assert updated.count('"X-EvolvMem-Expected-User" = "alice"') == 1
    assert '"X-Keep" = "yes"' in updated
    assert 'enabled_tools = ["memory_status"]' in updated
    assert '[projects."C:\\\\work"]' in updated
    assert 'trust_level = "trusted"' in updated


@pytest.mark.parametrize(
    "header_config",
    [
        'http_headers = { "X-Keep" = "yes" } # keep\n',
        '[mcp_servers.evolvmem.http_headers] # header keep\nX-Keep = "yes" # keep\n',
    ],
    ids=["inline-comment", "subtable-unquoted-key"],
)
@pytest.mark.parametrize(
    "root_header",
    [
        '[mcp_servers.evolvmem] # memory\n',
        '  [mcp_servers.evolvmem]\n',
    ],
    ids=["commented-root", "indented-root"],
)
def test_installer_adds_guard_to_supported_unmanaged_header_forms(
    tmp_path, header_config, root_header
):
    appdata = tmp_path / "local"
    profile = tmp_path / "profile"
    codex = tmp_path / "codex"
    codex.mkdir()
    stanza_tail = (
        'enabled_tools = ["memory_status"]\n\n'
        if header_config.startswith("http_headers")
        else 'enabled_tools = ["memory_status"]\n'
    )
    original = (
        root_header + 'url = "http://memory.test/mcp"\n'
        'bearer_token_env_var = "EVOLVMEM_TEST_TOKEN"\n'
        + (header_config if header_config.startswith("http_headers") else stanza_tail)
        + (stanza_tail if header_config.startswith("http_headers") else header_config)
        + '\n[projects."C:\\\\work"]\n'
        'trust_level = "trusted"\n'
    )
    config_path = codex / "config.toml"
    config_path.write_text(original, encoding="utf-8")
    env = os.environ.copy()
    env.update(LOCALAPPDATA=str(appdata), USERPROFILE=str(profile), CODEX_HOME=str(codex))
    command = [
        str(PWSH), "-NoLogo", "-NoProfile", "-File", str(INSTALLER),
        "-Url", "http://memory.test/mcp", "-ExpectedUser", "alice",
        "-TokenEnvVar", "EVOLVMEM_TEST_TOKEN",
    ]

    first = subprocess.run(command, text=True, capture_output=True, env=env, timeout=15)
    second = subprocess.run(command, text=True, capture_output=True, env=env, timeout=15)

    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    updated = config_path.read_text(encoding="utf-8")
    parsed = tomllib.loads(updated)
    headers = parsed["mcp_servers"]["evolvmem"]["http_headers"]
    assert headers == {"X-Keep": "yes", "X-EvolvMem-Expected-User": "alice"}
    assert root_header.rstrip("\n") in updated
    assert updated.count("X-EvolvMem-Expected-User") == 1
    assert '# keep' in updated
    assert 'enabled_tools = ["memory_status"]' in updated
    assert parsed["projects"][r"C:\work"]["trust_level"] == "trusted"


def test_installer_requires_force_for_unquoted_expected_user_header_mismatch(tmp_path):
    appdata = tmp_path / "local"
    codex = tmp_path / "codex"
    codex.mkdir()
    original = (
        '[mcp_servers.evolvmem]\n'
        'url = "http://memory.test/mcp"\n'
        'bearer_token_env_var = "EVOLVMEM_TEST_TOKEN"\n'
        '[mcp_servers.evolvmem.http_headers]\n'
        'X-EvolvMem-Expected-User = "mallory"\n'
    )
    config_path = codex / "config.toml"
    config_path.write_text(original, encoding="utf-8")
    env = os.environ.copy()
    env.update(LOCALAPPDATA=str(appdata), USERPROFILE=str(tmp_path / "profile"), CODEX_HOME=str(codex))
    command = [
        str(PWSH), "-NoLogo", "-NoProfile", "-File", str(INSTALLER),
        "-Url", "http://memory.test/mcp", "-ExpectedUser", "alice",
        "-TokenEnvVar", "EVOLVMEM_TEST_TOKEN",
    ]

    rejected = subprocess.run(command, text=True, capture_output=True, env=env, timeout=15)

    assert rejected.returncode != 0
    assert config_path.read_text(encoding="utf-8") == original
    assert not appdata.exists()

    forced = subprocess.run(
        command + ["-ForceIdentity"], text=True, capture_output=True, env=env, timeout=15
    )
    assert forced.returncode == 0, forced.stderr
    headers = tomllib.loads(config_path.read_text(encoding="utf-8"))["mcp_servers"][
        "evolvmem"
    ]["http_headers"]
    assert headers["X-EvolvMem-Expected-User"] == "alice"


def test_installer_rejects_unsupported_header_form_before_any_file_change(tmp_path):
    appdata = tmp_path / "local"
    codex = tmp_path / "codex"
    codex.mkdir()
    original = (
        '[mcp_servers.evolvmem]\n'
        'url = "http://memory.test/mcp"\n'
        'bearer_token_env_var = "EVOLVMEM_TEST_TOKEN"\n'
        'http_headers."X-Keep" = "yes"\n'
    )
    assert tomllib.loads(original)["mcp_servers"]["evolvmem"]["http_headers"] == {
        "X-Keep": "yes"
    }
    config_path = codex / "config.toml"
    config_path.write_text(original, encoding="utf-8")
    env = os.environ.copy()
    env.update(LOCALAPPDATA=str(appdata), USERPROFILE=str(tmp_path / "profile"), CODEX_HOME=str(codex))

    result = subprocess.run(
        [
            str(PWSH), "-NoLogo", "-NoProfile", "-File", str(INSTALLER),
            "-Url", "http://memory.test/mcp", "-ExpectedUser", "alice",
            "-TokenEnvVar", "EVOLVMEM_TEST_TOKEN", "-ForceIdentity",
        ],
        text=True,
        capture_output=True,
        env=env,
        timeout=15,
    )

    assert result.returncode != 0
    assert config_path.read_text(encoding="utf-8") == original
    assert not appdata.exists()

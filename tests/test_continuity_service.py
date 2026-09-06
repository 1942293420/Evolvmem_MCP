"""Behavioral contracts for the continuity domain service (Task 7).

Pinned here: the closed action whitelist and status-transition matrix
(terminal states reject everything; ``update`` never sneaks a status change);
per-row revision CAS on workstreams plus focus-pointer CAS (any rowcount!=1
rolls the whole mutation back); the unique single-focus guarantee for
concurrent creates; focus rows that are pre-built empty and never deleted;
source/parent validation; the five staleness codes; the resume short-circuit
matrix; and server-authoritative L2 write-back with client-forgery rejection.

Tests never touch the real database: everything runs on the ``test_config``
temp roots, and git fixtures live under ``tmp_path``.
"""

import json
import secrets
import shutil
import subprocess

import pytest

from evolvmem.context_models import (
    ContextItemDraft,
    ContextLayer,
    ContextLayers,
    ContextScope,
    ContextStatus,
)
from evolvmem.context_models import ContextContentType
from evolvmem.context_store import ContextStore
from evolvmem.continuity_models import (
    ContinuityCheckpointRequest,
    ContinuityCheckpointResult,
    ContinuityError,
    ContinuityResumeRequest,
    ContinuityResumeResult,
    WorkstreamSummary,
)
from evolvmem.continuity_service import ContinuityService
from evolvmem.project_store import ProjectStore
from evolvmem.workspace_identity import WorkspaceIdentityProvider


# ---- fixtures and helpers ----


def _git(repo, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def git_workspace(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git binary not available")
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", str(repo)],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _git(repo, "config", "user.email", "continuity@example.invalid")
    _git(repo, "config", "user.name", "continuity-test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return repo


@pytest.fixture
def plain_workspace(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    return plain


@pytest.fixture
def identity_provider(test_config):
    provider = WorkspaceIdentityProvider(
        key_path=test_config.data_dir / "workspace.key"
    )
    provider.bootstrap_key()
    return provider


@pytest.fixture
def store(test_config):
    with ContextStore(test_config) as instance:
        yield instance


def _project_store(store) -> ProjectStore:
    return ProjectStore(
        store._connection(), store._require_transaction, generic_names=()
    )


def _bind(store, provider, workspace, project, *, make_default=True) -> str:
    """Register + bind in one transaction; pre-builds the empty focus row."""
    fingerprint = provider.resolve(str(workspace)).fingerprint
    with store.transaction():
        ps = _project_store(store)
        ps.register_project(project)
        ps.bind_workspace(
            fingerprint, project, method="test", make_default=make_default
        )
    return fingerprint


@pytest.fixture
def continuity(test_config, store, identity_provider, git_workspace):
    _bind(store, identity_provider, git_workspace, "proj")
    return ContinuityService(test_config, store, identity_provider)


def _create(continuity, workspace, **overrides) -> ContinuityCheckpointResult:
    params = {
        "action": "create",
        "workspace_path": str(workspace),
        "objective": "交付续接域层",
        "next_action": "写测试",
    }
    params.update(overrides)
    return continuity.checkpoint(ContinuityCheckpointRequest(**params))


def _workstream_row(store, workstream_id: str):
    return store._connection().execute(
        "SELECT * FROM continuity_workstreams WHERE id=?", (workstream_id,)
    ).fetchone()


def _focus_row(store, project: str, fingerprint: str):
    return store._connection().execute(
        "SELECT * FROM continuity_focus WHERE project=? AND workspace_fingerprint=?",
        (project, fingerprint),
    ).fetchone()


def _events(store, workstream_id: str | None = None) -> list[dict]:
    if workstream_id is None:
        rows = store._connection().execute(
            "SELECT * FROM continuity_events ORDER BY id"
        ).fetchall()
    else:
        rows = store._connection().execute(
            "SELECT * FROM continuity_events WHERE workstream_id=? ORDER BY id",
            (workstream_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _act(continuity, store, workspace, workstream_id: str, action: str, **overrides):
    """Apply an action with the workstream's current revisions as CAS tokens."""
    row = _workstream_row(store, workstream_id)
    params = {
        "action": action,
        "workspace_path": str(workspace),
        "workstream_id": workstream_id,
        "expected_checkpoint_revision": row["checkpoint_revision"],
        "expected_state_version": row["state_version"],
    }
    params.update(overrides)
    return continuity.checkpoint(ContinuityCheckpointRequest(**params))


def _make_item(store, project: str, *, scope=ContextScope.PROJECT,
               status=ContextStatus.ACTIVE) -> int:
    item = store.create_item(
        ContextItemDraft(
            identity_key=f"project:{project}:fact:{secrets.token_hex(4)}",
            content_type=ContextContentType.FACT,
            layers=ContextLayers(
                l0=f"{project} 要点",
                l1=f"{project} 细节",
                l2=f"{project} 来源",
                generator="test-suite",
            ),
            project=project,
            scope=scope,
            status=status,
        )
    )
    return item.id


def _l2_of(store, context_id: int) -> dict:
    raw = store.get_layer(context_id, ContextLayer.L2)
    assert raw is not None
    return json.loads(raw)


# ---- create + resume round trip ----


def test_create_then_resume_roundtrip(
    continuity, store, identity_provider, git_workspace
):
    created = _create(
        continuity, git_workspace, make_focus=True, expected_focus_revision=0
    )
    assert created.workstream_id.startswith("ws_")
    assert created.checkpoint_revision == 1
    assert created.state_version == 1
    assert created.focus_revision == 1
    assert created.status == "open"
    assert created.context_id > 0

    row = _workstream_row(store, created.workstream_id)
    assert row["project"] == "proj"
    assert row["status"] == "open"
    assert row["repo_kind"] == "git"
    assert row["repo_branch"] == "main"
    assert row["repo_head_commit"] == row["repo_root_commit"]

    resumed = continuity.resume(
        ContinuityResumeRequest(workspace_path=str(git_workspace))
    )
    assert resumed.code == "ok"
    assert resumed.workstream_id == created.workstream_id
    assert resumed.context_id == created.context_id
    assert resumed.checkpoint_revision == 1
    assert resumed.state_version == 1
    assert resumed.focus_revision == 1
    assert resumed.status == "open"
    assert resumed.staleness == "fresh"
    assert resumed.candidates == ()
    checkpoint = resumed.checkpoint
    assert checkpoint is not None
    assert checkpoint["workstream_id"] == created.workstream_id
    assert checkpoint["project"] == "proj"
    assert checkpoint["status"] == "open"
    assert checkpoint["repo"]["kind"] == "git"
    assert checkpoint["l0"]
    assert "交付续接域层" in checkpoint["l1"]
    assert "l2" not in checkpoint  # L2 原文不外泄


def test_l2_canonical_writeback_with_server_authority(
    continuity, store, identity_provider, git_workspace
):
    created = _create(continuity, git_workspace)
    raw = store.get_layer(created.context_id, ContextLayer.L2)
    payload = json.loads(raw)
    fingerprint = identity_provider.resolve(str(git_workspace)).fingerprint
    assert payload["schema_version"] == 1
    assert payload["workstream_id"] == created.workstream_id
    assert payload["project"] == "proj"
    assert payload["workspace_fingerprint"] == fingerprint
    assert payload["checkpoint_revision"] == 1
    assert payload["state_version"] == 1
    assert payload["status"] == "open"
    assert payload["objective"] == "交付续接域层"
    assert payload["next_action"] == "写测试"
    assert payload["parent_workstream_id"] is None
    assert payload["source_context_ids"] == []
    assert payload["repo"]["kind"] == "git"
    assert payload["repo"]["branch"] == "main"
    assert payload["repo"]["head_commit"] == _git(git_workspace, "rev-parse", "HEAD")
    # canonical form: sorted keys, tight separators
    assert raw == json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"))


def test_client_forged_authority_fields_rejected():
    from evolvmem.continuity_service import _build_l2_payload

    authority = {
        "schema_version": 1,
        "workstream_id": "ws_real",
        "project": "proj",
        "workspace_fingerprint": "hmac-sha256:" + "a" * 64,
        "checkpoint_revision": 3,
        "status": "open",
        "repo": {"kind": "non_git", "branch": "", "root_commit": "",
                 "head_commit": ""},
    }
    with pytest.raises(ContinuityError, match="content_rejected"):
        _build_l2_payload(
            client={"objective": "x", "workstream_id": "ws_forged"},
            authority=authority,
        )
    with pytest.raises(ContinuityError, match="content_rejected"):
        _build_l2_payload(
            client={"objective": "x", "checkpoint_revision": 1},
            authority=authority,
        )
    # 一致重复的权威字段允许，服务端值仍然胜出
    ok = _build_l2_payload(
        client={"objective": "x", "workstream_id": "ws_real"},
        authority=authority,
    )
    assert json.loads(ok)["workstream_id"] == "ws_real"


# ---- action whitelist and transition matrix ----


def test_invalid_action_rejected(continuity, store, git_workspace):
    with pytest.raises(ContinuityError, match="invalid_action"):
        continuity.checkpoint(
            ContinuityCheckpointRequest(
                action="explode", workspace_path=str(git_workspace)
            )
        )
    assert _events(store) == []


@pytest.mark.parametrize(
    "path, action, expected",
    [
        ((), "update", "open"),
        ((), "pause", "paused"),
        ((), "block", "blocked"),
        ((), "complete", "completed"),
        ((), "cancel", "cancelled"),
        (("pause",), "update", "paused"),
        (("pause",), "resume", "open"),
        (("pause",), "complete", "completed"),
        (("pause",), "cancel", "cancelled"),
        (("block",), "update", "blocked"),
        (("block",), "unblock", "open"),
        (("block",), "pause", "paused"),
        (("block",), "complete", "completed"),
        (("block",), "cancel", "cancelled"),
    ],
)
def test_transition_matrix_allows(
    continuity, store, git_workspace, path, action, expected
):
    created = _create(continuity, git_workspace)
    for step in path:
        _act(continuity, store, git_workspace, created.workstream_id, step)
    result = _act(
        continuity, store, git_workspace, created.workstream_id, action
    )
    assert result.status == expected
    row = _workstream_row(store, created.workstream_id)
    assert row["status"] == expected


@pytest.mark.parametrize(
    "path, action",
    [
        ((), "resume"),
        ((), "unblock"),
        (("pause",), "pause"),
        (("pause",), "block"),
        (("block",), "resume"),
        (("block",), "block"),
        (("complete",), "update"),
        (("complete",), "resume"),
        (("cancel",), "update"),
        (("cancel",), "pause"),
    ],
)
def test_transition_matrix_rejects(
    continuity, store, git_workspace, path, action
):
    created = _create(continuity, git_workspace)
    for step in path:
        _act(continuity, store, git_workspace, created.workstream_id, step)
    before = _workstream_row(store, created.workstream_id)
    with pytest.raises(ContinuityError, match="invalid_transition"):
        _act(continuity, store, git_workspace, created.workstream_id, action)
    after = _workstream_row(store, created.workstream_id)
    # 整体回滚：revision/version/status/current_context_id 全部不变
    assert after["checkpoint_revision"] == before["checkpoint_revision"]
    assert after["state_version"] == before["state_version"]
    assert after["status"] == before["status"]
    assert after["current_context_id"] == before["current_context_id"]


def test_update_never_changes_status_implicitly(
    continuity, store, git_workspace
):
    created = _create(continuity, git_workspace)
    _act(continuity, store, git_workspace, created.workstream_id, "pause")
    result = _act(
        continuity, store, git_workspace, created.workstream_id, "update",
        current_step="复盘",
    )
    assert result.status == "paused"


# ---- revision CAS ----


def test_update_with_stale_revision_conflicts(continuity, git_workspace):
    created = continuity.checkpoint(
        ContinuityCheckpointRequest(action="create", workspace_path=str(git_workspace),
                                    objective="goal", next_action="step1", make_focus=True,
                                    expected_focus_revision=0)
    )
    with pytest.raises(ContinuityError, match="revision_conflict"):
        continuity.checkpoint(
            ContinuityCheckpointRequest(action="update", workspace_path=str(git_workspace),
                                        workstream_id=created.workstream_id,
                                        current_step="step1", next_action="step2",
                                        expected_checkpoint_revision=0,
                                        expected_state_version=0)
        )


def test_update_happy_path_supersedes_and_merges(
    continuity, store, git_workspace
):
    created = _create(
        continuity,
        git_workspace,
        accepted_decisions=("采用 CAS",),
        completed_steps=("设计冻结",),
    )
    updated = _act(
        continuity,
        store,
        git_workspace,
        created.workstream_id,
        "update",
        current_step="实现域层",
        next_action="跑回归",
        completed_steps=("设计冻结", "测试落地"),
    )
    assert updated.checkpoint_revision == 2
    assert updated.state_version == 2
    assert updated.context_id != created.context_id

    old_item = store.get_item(created.context_id, include_layers=False)
    assert old_item.status is ContextStatus.SUPERSEDED
    new_item = store.get_item(updated.context_id)
    assert new_item.status is ContextStatus.ACTIVE
    identity = f"project:proj:workstream:{created.workstream_id}:checkpoint"
    assert new_item.identity_key == identity
    assert new_item.content_type is ContextContentType.WORKSTREAM_CHECKPOINT
    chain = store.get_by_identity(identity, project="proj")
    assert len(chain) == 2
    assert sum(1 for item in chain if item.status is ContextStatus.ACTIVE) == 1

    payload = _l2_of(store, updated.context_id)
    assert payload["objective"] == "交付续接域层"  # 未提供的字段沿旧值
    assert payload["accepted_decisions"] == ["采用 CAS"]
    assert payload["completed_steps"] == ["设计冻结", "测试落地"]
    assert payload["current_step"] == "实现域层"
    assert payload["checkpoint_revision"] == 2


def test_create_with_nonzero_expected_revisions_conflicts(
    continuity, git_workspace
):
    with pytest.raises(ContinuityError, match="revision_conflict"):
        _create(continuity, git_workspace, expected_checkpoint_revision=1)
    with pytest.raises(ContinuityError, match="revision_conflict"):
        _create(continuity, git_workspace, expected_state_version=2)


def test_concurrent_create_make_focus_single_winner(
    continuity, store, git_workspace
):
    first = _create(
        continuity, git_workspace, make_focus=True, expected_focus_revision=0
    )
    with pytest.raises(ContinuityError, match="revision_conflict"):
        _create(
            continuity, git_workspace, make_focus=True,
            expected_focus_revision=0,
        )
    rows = store._connection().execute(
        "SELECT id FROM continuity_workstreams"
    ).fetchall()
    assert [row["id"] for row in rows] == [first.workstream_id]
    focus = _focus_row(
        store, "proj",
        continuity._workspace_identity.resolve(str(git_workspace)).fingerprint,
    )
    assert focus["workstream_id"] == first.workstream_id
    assert focus["revision"] == 1


def test_events_written_for_mutation_and_failure(
    continuity, store, git_workspace
):
    created = _create(continuity, git_workspace)
    _act(continuity, store, git_workspace, created.workstream_id, "update",
         current_step="s1")
    with pytest.raises(ContinuityError, match="revision_conflict"):
        _act(continuity, store, git_workspace, created.workstream_id, "update",
             expected_checkpoint_revision=1, expected_state_version=1)
    events = _events(store, created.workstream_id)
    types = [event["event_type"] for event in events]
    assert types == ["create", "update", "revision_conflict"]
    assert events[0]["after_revision"] == 1
    assert events[1]["before_revision"] == 1
    assert events[1]["after_revision"] == 2
    assert events[2]["error_code"] == "revision_conflict"


# ---- focus pointer semantics ----


def test_make_focus_requires_expected_focus_revision(continuity, git_workspace):
    with pytest.raises(ContinuityError, match="invalid_action"):
        _create(continuity, git_workspace, make_focus=True)


def test_switch_and_clear_focus(
    continuity, store, identity_provider, git_workspace
):
    first = _create(
        continuity, git_workspace, objective="第一", make_focus=True,
        expected_focus_revision=0,
    )
    second = _create(continuity, git_workspace, objective="第二")

    switched = continuity.checkpoint(
        ContinuityCheckpointRequest(
            action="switch_focus",
            workspace_path=str(git_workspace),
            workstream_id=second.workstream_id,
            expected_focus_revision=1,
        )
    )
    assert switched.workstream_id == second.workstream_id
    assert switched.focus_revision == 2
    assert switched.status == "open"
    assert switched.checkpoint_revision == 1  # 目标行本身未被改写

    resumed = continuity.resume(
        ContinuityResumeRequest(workspace_path=str(git_workspace))
    )
    assert resumed.code == "ok"
    assert resumed.workstream_id == second.workstream_id

    # 旧指针 revision 过期 → focus_conflict
    with pytest.raises(ContinuityError, match="focus_conflict"):
        continuity.checkpoint(
            ContinuityCheckpointRequest(
                action="switch_focus",
                workspace_path=str(git_workspace),
                workstream_id=first.workstream_id,
                expected_focus_revision=1,
            )
        )

    cleared = continuity.checkpoint(
        ContinuityCheckpointRequest(
            action="clear_focus",
            workspace_path=str(git_workspace),
            expected_focus_revision=2,
        )
    )
    assert cleared.workstream_id == ""
    assert cleared.focus_revision == 3

    fingerprint = identity_provider.resolve(str(git_workspace)).fingerprint
    focus = _focus_row(store, "proj", fingerprint)
    assert focus is not None  # 行永不删除
    assert focus["workstream_id"] is None
    assert focus["revision"] == 3

    resumed = continuity.resume(
        ContinuityResumeRequest(workspace_path=str(git_workspace))
    )
    assert resumed.code == "ambiguous"  # clear 后两个未完成候选
    assert resumed.focus_revision == 3
    assert len(resumed.candidates) == 2


def test_switch_focus_target_validation(continuity, store, git_workspace):
    created = _create(continuity, git_workspace)
    # 目标不存在
    with pytest.raises(ContinuityError, match="workstream_not_found"):
        continuity.checkpoint(
            ContinuityCheckpointRequest(
                action="switch_focus",
                workspace_path=str(git_workspace),
                workstream_id="ws_ghost",
                expected_focus_revision=0,
            )
        )
    # 目标已终态
    _act(continuity, store, git_workspace, created.workstream_id, "complete")
    with pytest.raises(ContinuityError, match="focus_conflict"):
        continuity.checkpoint(
            ContinuityCheckpointRequest(
                action="switch_focus",
                workspace_path=str(git_workspace),
                workstream_id=created.workstream_id,
                expected_focus_revision=0,
            )
        )


def test_clear_focus_revision_cas(continuity, git_workspace):
    _create(continuity, git_workspace)
    with pytest.raises(ContinuityError, match="focus_conflict"):
        continuity.checkpoint(
            ContinuityCheckpointRequest(
                action="clear_focus",
                workspace_path=str(git_workspace),
                expected_focus_revision=9,
            )
        )


# ---- source_context_ids ----


def test_source_context_ids_linked(continuity, store, git_workspace):
    source_id = _make_item(store, "proj")
    global_id = _make_item(store, "", scope=ContextScope.GLOBAL)
    created = _create(
        continuity, git_workspace,
        source_context_ids=(source_id, global_id),
    )
    rows = store._connection().execute(
        "SELECT source_kind, source_ref FROM context_sources "
        "WHERE item_id=? AND source_kind='context_reference' ORDER BY id",
        (created.context_id,),
    ).fetchall()
    assert [(row["source_kind"], row["source_ref"]) for row in rows] == [
        ("context_reference", str(source_id)),
        ("context_reference", str(global_id)),
    ]
    payload = _l2_of(store, created.context_id)
    assert payload["source_context_ids"] == [source_id, global_id]


@pytest.mark.parametrize("bad", ["missing", "deleted", "cross_project"])
def test_source_context_ids_rejected(
    continuity, store, git_workspace, bad
):
    if bad == "missing":
        bad_id = 999999
    elif bad == "deleted":
        bad_id = _make_item(store, "proj", status=ContextStatus.DELETED)
    else:
        other = "other"
        with store.transaction():
            _project_store(store).register_project(other)
        bad_id = _make_item(store, other)
    with pytest.raises(ContinuityError, match="invalid_source"):
        _create(continuity, git_workspace, source_context_ids=(bad_id,))
    # 整体回滚：无 workstream、无 checkpoint item
    assert store._connection().execute(
        "SELECT COUNT(*) AS c FROM continuity_workstreams"
    ).fetchone()["c"] == 0


# ---- parent validation ----


def test_parent_happy_path_and_cycle_rejection(
    continuity, store, git_workspace
):
    parent = _create(continuity, git_workspace, objective="父")
    child = _create(
        continuity, git_workspace, objective="子",
        parent_workstream_id=parent.workstream_id,
    )
    row = _workstream_row(store, child.workstream_id)
    assert row["parent_id"] == parent.workstream_id
    payload = _l2_of(store, child.context_id)
    assert payload["parent_workstream_id"] == parent.workstream_id

    # self-parent
    with pytest.raises(ContinuityError, match="invalid_parent"):
        _act(
            continuity, store, git_workspace, child.workstream_id, "update",
            parent_workstream_id=child.workstream_id,
        )
    # 父链循环：把 parent 的 parent 指到 child
    with pytest.raises(ContinuityError, match="invalid_parent"):
        _act(
            continuity, store, git_workspace, parent.workstream_id, "update",
            parent_workstream_id=child.workstream_id,
        )


def test_parent_must_be_same_project_and_unfinished(
    continuity, store, identity_provider, git_workspace
):
    # 同 workspace 第二项目（非默认）下的 workstream 不能当 proj 的 parent
    _bind(store, identity_provider, git_workspace, "other", make_default=False)
    foreign = _create(
        continuity, git_workspace, objective="外部", project_hint="other"
    )
    assert foreign.workstream_id
    with pytest.raises(ContinuityError, match="invalid_parent"):
        _create(
            continuity, git_workspace, objective="子",
            parent_workstream_id=foreign.workstream_id,
        )

    parent = _create(continuity, git_workspace, objective="父")
    _act(continuity, store, git_workspace, parent.workstream_id, "complete")
    with pytest.raises(ContinuityError, match="invalid_parent"):
        _create(
            continuity, git_workspace, objective="子",
            parent_workstream_id=parent.workstream_id,
        )
    with pytest.raises(ContinuityError, match="invalid_parent"):
        _create(
            continuity, git_workspace, objective="子",
            parent_workstream_id="ws_ghost",
        )


def test_cross_project_mutation_is_not_found(
    continuity, store, identity_provider, git_workspace
):
    _bind(store, identity_provider, git_workspace, "other", make_default=False)
    mine = _create(continuity, git_workspace, objective="我的")
    with pytest.raises(ContinuityError, match="workstream_not_found"):
        continuity.checkpoint(
            ContinuityCheckpointRequest(
                action="update",
                workspace_path=str(git_workspace),
                project_hint="other",
                workstream_id=mine.workstream_id,
                expected_checkpoint_revision=1,
                expected_state_version=1,
            )
        )


# ---- content gates ----


@pytest.mark.parametrize(
    "field,value",
    [
        ("objective", "读一下 /etc/passwd 的内容"),
        ("objective", "配置在 ~/secret 下面"),
        ("objective", "调用参数 token=abc123 已确认"),
        ("objective", "diff --git a/x.py b/x.py 之后改了三行"),
        ("objective", "x" * 2001),
    ],
)
def test_forbidden_content_rejected(continuity, store, git_workspace, field, value):
    with pytest.raises(ContinuityError, match="content_rejected"):
        _create(continuity, git_workspace, **{field: value})
    assert store._connection().execute(
        "SELECT COUNT(*) AS c FROM continuity_workstreams"
    ).fetchone()["c"] == 0


def test_oversized_array_rejected(continuity, git_workspace):
    with pytest.raises(ContinuityError, match="content_rejected"):
        _create(
            continuity, git_workspace,
            accepted_decisions=tuple(f"决定{i}" for i in range(51)),
        )


# ---- project resolution ----


def test_project_unresolved_for_unbound_workspace(
    continuity, identity_provider, plain_workspace
):
    with pytest.raises(ContinuityError, match="project_unresolved"):
        _create(continuity, plain_workspace)
    resumed = continuity.resume(
        ContinuityResumeRequest(workspace_path=str(plain_workspace))
    )
    assert resumed.code == "no_continuation"


def test_hint_alias_normalization_and_binding_gate(
    continuity, store, identity_provider, git_workspace
):
    with store.transaction():
        _project_store(store).add_alias("pj", "proj")
    created = _create(continuity, git_workspace, project_hint="PJ")
    assert _workstream_row(store, created.workstream_id)["project"] == "proj"

    with pytest.raises(ContinuityError, match="project_unresolved"):
        _create(continuity, git_workspace, project_hint="ghost")


def test_multiple_bindings_without_default_require_hint(
    test_config, store, identity_provider, git_workspace
):
    _bind(store, identity_provider, git_workspace, "alpha", make_default=False)
    _bind(store, identity_provider, git_workspace, "beta", make_default=False)
    service = ContinuityService(test_config, store, identity_provider)
    with pytest.raises(ContinuityError, match="project_unresolved"):
        _create(service, git_workspace)
    created = _create(service, git_workspace, project_hint="beta")
    assert _workstream_row(store, created.workstream_id)["project"] == "beta"


def test_workspace_key_missing_fails_closed(test_config, store, git_workspace):
    provider = WorkspaceIdentityProvider(
        key_path=test_config.data_dir / "workspace.key"
    )  # 未 bootstrap
    service = ContinuityService(test_config, store, provider)
    with pytest.raises(ContinuityError, match="workspace_key_missing"):
        _create(service, git_workspace)
    resumed = service.resume(
        ContinuityResumeRequest(workspace_path=str(git_workspace))
    )
    assert resumed.code == "continuity_not_ready"


def test_resume_continuity_not_ready_without_schema(test_config, tmp_path):
    # 旧库无 continuity 表：只读打开，resume 返回稳定 code 而非抛错
    test_config.db_path.touch()
    store = ContextStore(test_config)
    store.initialize(create_schema=False)
    try:
        provider = WorkspaceIdentityProvider(
            key_path=test_config.data_dir / "workspace.key"
        )
        provider.bootstrap_key()
        service = ContinuityService(test_config, store, provider)
        resumed = service.resume(
            ContinuityResumeRequest(workspace_path=str(tmp_path))
        )
        assert resumed.code == "continuity_not_ready"
    finally:
        store.close()


# ---- resume matrix ----


def test_resume_no_continuation_when_empty(continuity, git_workspace):
    resumed = continuity.resume(
        ContinuityResumeRequest(workspace_path=str(git_workspace))
    )
    assert resumed.code == "no_continuation"
    assert resumed.candidates == ()
    assert resumed.checkpoint is None
    assert resumed.focus_revision == 0  # 预建空行 revision=0


def test_resume_needs_focus_confirmation_single_unfinished(
    continuity, git_workspace
):
    created = _create(continuity, git_workspace)
    resumed = continuity.resume(
        ContinuityResumeRequest(workspace_path=str(git_workspace))
    )
    assert resumed.code == "needs_focus_confirmation"
    assert resumed.checkpoint is None
    assert len(resumed.candidates) == 1
    candidate = resumed.candidates[0]
    assert isinstance(candidate, WorkstreamSummary)
    assert candidate.workstream_id == created.workstream_id
    assert candidate.project == "proj"
    assert candidate.status == "open"
    assert candidate.checkpoint_revision == 1
    assert candidate.state_version == 1
    assert candidate.l0
    assert candidate.updated_at


def test_resume_ambiguous_lists_only_l0_candidates(
    continuity, git_workspace
):
    _create(continuity, git_workspace, objective="甲")
    _create(continuity, git_workspace, objective="乙")
    resumed = continuity.resume(
        ContinuityResumeRequest(workspace_path=str(git_workspace))
    )
    assert resumed.code == "ambiguous"
    assert len(resumed.candidates) == 2
    for candidate in resumed.candidates:
        assert candidate.l0
        assert not hasattr(candidate, "l1")


def test_resume_focus_terminal_is_dangling(continuity, store, git_workspace):
    created = _create(
        continuity, git_workspace, make_focus=True, expected_focus_revision=0
    )
    _act(continuity, store, git_workspace, created.workstream_id, "complete")
    resumed = continuity.resume(
        ContinuityResumeRequest(workspace_path=str(git_workspace))
    )
    assert resumed.code == "dangling_focus"
    assert resumed.checkpoint is None


def test_resume_focus_missing_workstream_is_dangling(
    continuity, store, git_workspace
):
    created = _create(
        continuity, git_workspace, make_focus=True, expected_focus_revision=0
    )
    conn = store._connection()
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "DELETE FROM continuity_workstreams WHERE id=?",
        (created.workstream_id,),
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    resumed = continuity.resume(
        ContinuityResumeRequest(workspace_path=str(git_workspace))
    )
    assert resumed.code == "dangling_focus"


def test_resume_paused_focus_returns_checkpoint(
    continuity, store, git_workspace
):
    created = _create(
        continuity, git_workspace, make_focus=True, expected_focus_revision=0
    )
    _act(continuity, store, git_workspace, created.workstream_id, "pause")
    resumed = continuity.resume(
        ContinuityResumeRequest(workspace_path=str(git_workspace))
    )
    assert resumed.code == "ok"
    assert resumed.status == "paused"
    assert resumed.staleness == "fresh"
    assert resumed.checkpoint is not None


# ---- staleness ----


def test_staleness_head_advanced(continuity, git_workspace):
    _create(continuity, git_workspace, make_focus=True, expected_focus_revision=0)
    (git_workspace / "seed.txt").write_text("more\n", encoding="utf-8")
    _git(git_workspace, "add", ".")
    _git(git_workspace, "commit", "-m", "advance")
    resumed = continuity.resume(
        ContinuityResumeRequest(workspace_path=str(git_workspace))
    )
    assert resumed.code == "ok"
    assert resumed.staleness == "head_advanced"
    assert resumed.checkpoint is not None


def test_staleness_branch_changed(continuity, git_workspace):
    _create(continuity, git_workspace, make_focus=True, expected_focus_revision=0)
    _git(git_workspace, "checkout", "-b", "feature")
    resumed = continuity.resume(
        ContinuityResumeRequest(workspace_path=str(git_workspace))
    )
    assert resumed.staleness == "branch_changed"


def test_staleness_head_diverged(continuity, git_workspace):
    _create(continuity, git_workspace, make_focus=True, expected_focus_revision=0)
    # amend 换 message 保证得到不同的 commit hash（同秒 amend 可能同 hash）
    _git(git_workspace, "commit", "--amend", "--no-edit", "-m", "init amended")
    resumed = continuity.resume(
        ContinuityResumeRequest(workspace_path=str(git_workspace))
    )
    assert resumed.staleness == "head_diverged"


def test_staleness_unknown_for_non_git(
    test_config, store, identity_provider, plain_workspace
):
    _bind(store, identity_provider, plain_workspace, "plain")
    service = ContinuityService(test_config, store, identity_provider)
    _create(service, plain_workspace, make_focus=True, expected_focus_revision=0)
    row = store._connection().execute(
        "SELECT repo_kind FROM continuity_workstreams"
    ).fetchone()
    assert row["repo_kind"] == "non_git"
    resumed = service.resume(
        ContinuityResumeRequest(workspace_path=str(plain_workspace))
    )
    assert resumed.code == "ok"
    assert resumed.staleness == "unknown"


def test_staleness_wrong_workspace_hides_checkpoint(
    continuity, store, identity_provider, git_workspace
):
    created = _create(
        continuity, git_workspace, make_focus=True, expected_focus_revision=0
    )
    store._connection().execute(
        "UPDATE continuity_workstreams SET workspace_fingerprint=? WHERE id=?",
        ("hmac-sha256:" + "f" * 64, created.workstream_id),
    )
    store._connection().commit()
    resumed = continuity.resume(
        ContinuityResumeRequest(workspace_path=str(git_workspace))
    )
    assert resumed.staleness == "wrong_workspace"
    assert resumed.checkpoint is None  # 不泄露 L1/L2 派生内容


# ---- list_open ----


def test_list_open_returns_unfinished_only(
    continuity, store, identity_provider, git_workspace, plain_workspace, test_config
):
    first = _create(continuity, git_workspace, objective="甲")
    second = _create(continuity, git_workspace, objective="乙")
    _act(continuity, store, git_workspace, second.workstream_id, "cancel")
    _bind(store, identity_provider, plain_workspace, "plain")
    _create(
        ContinuityService(test_config, store, identity_provider),
        plain_workspace, objective="别处",
    )

    listed = continuity.list_open(
        ContinuityResumeRequest(workspace_path=str(git_workspace))
    )
    assert isinstance(listed, tuple)
    assert [item.workstream_id for item in listed] == [first.workstream_id]
    assert listed[0].l0
    assert listed[0].status == "open"

    # 未绑定 workspace → 空
    stranger = plain_workspace / "stranger"
    stranger.mkdir()
    assert continuity.list_open(
        ContinuityResumeRequest(workspace_path=str(stranger))
    ) == ()

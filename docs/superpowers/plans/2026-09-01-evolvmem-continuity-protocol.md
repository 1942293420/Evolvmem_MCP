# EvolvMem Cross-Agent Continuity Protocol Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-subagent-driven-development (recommended) or superpowers-executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a fresh supported Agent receive only “继续原任务”, recover the exact current project, workstream, and checkpoint from EvolvMem without Codex transcript memory or semantic guessing, validate repository and lease safety, and continue from the recorded next action with auditable optimistic concurrency.

**Architecture:** Each canonical `(project, workspace_fingerprint)` owns one permanent versioned focus row that points to zero or one unfinished workstream. The relational workstream row is authoritative and points to a versioned `WORKSTREAM_CHECKPOINT` Context item; checkpoint revision, state version, focus revision, lease epoch, semantic mutation epoch, and journal entry have explicit independent rules. Lease capabilities live only in one transport session, while exact resume validates pointer integrity, lease ownership, and repository staleness in a fixed order. Continuation phrases are control intent and never become FTS or HNSW queries, and recovery candidates cannot overwrite checkpoints or focus without explicit CAS confirmation.

**Tech Stack:** Python 3.10+, frozen dataclasses and enums, SQLite foreign keys/CAS/semantic journal, HMAC-SHA-256, read-only Git subprocesses, the existing Context Core and MCP stdio JSON-RPC server, vanilla Web v2, pytest, uv, Codex CLI `--ephemeral`, bubblewrap, and a generic MCP harness.

**Design source:** `docs/superpowers/specs/2026-09-01-evolvmem-project-continuity-design.md`

**Prerequisites:** Complete and review `2026-09-01-evolvmem-project-memory-cleanup.md` and `2026-09-01-evolvmem-context-web-v2.md` on the same isolated feature branch. The real-data wrapper and old-schema read-only planner are produced by cleanup-plan Task 10. No real mutation occurs before Task 12 renders an apply command from the actual stable digest and the user approves that exact command.

## Global Constraints

- Execute in `/home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity` on `feat/evolvmem-project-continuity`. Keep the dirty main checkout and its current skill/session-miner WIP untouched unless the user separately authorizes an integration snapshot.
- Use `superpowers-test-driven-development` for every code task. Each code task below separates test writing, observed red, implementation, observed green, and commit. Unexpected failures require `superpowers-systematic-debugging`; completion claims require `superpowers-verification-before-completion` with fresh output.
- Continuation is exact lookup, never similarity search. `ContinuationIntentDetector` classifies before the ordinary Context serving gate; a continuation branch makes zero FTS, HNSW, `memory_search`, and `context_search` calls.
- Workspace/project resolution uses `ProjectService.resolve_workspace(ResolveWorkspaceRequest) -> WorkspaceProjectResolution` and the transient caller cwd. Never use the MCP process cwd. Persist only the HMAC workspace fingerprint and a server-generated repository anchor.
- Never return absolute paths, remotes, HMAC keys, lease tokens, writer hashes, archive text, command text, patch text, environment values, hidden reasoning, or complete terminal output in protocol/runtime results. The sole operator-output exception is Task 12’s user-approval packet, which must show the exact absolute reviewed-worktree wrapper command plus externally managed writer stop/restart commands; it still excludes live data/config/key/auth paths and private values.
- Workstream table fields are authoritative for ID, project, workspace fingerprint, parent, status, current Context ID, checkpoint/state revisions, and repository anchor. The server constructs those L2 fields; client-supplied authority fields reject the whole request.
- `checkpoint_revision` advances only for a new checkpoint Context item. `state_version` advances for checkpoint/status/lease mutations. `lease_epoch` advances on claim, renew, and release. `focus.revision` advances only on focus set, switch, and clear.
- Every successful typed continuity mutation runs in exactly one outer `ContextStore.semantic_transaction`, advances the semantic mutation epoch exactly once, and appends exactly one journal entry. This includes create/update/status, claim/renew/release, switch/clear, recovery create/expire/accept/reject, and binding-to-focus integration. A rejected or rolled-back mutation advances neither epoch nor journal.
- Focus rows are permanent. Every active workspace binding owns exactly one row, initially `workstream_id=NULL, revision=0`; clear writes NULL and increments. Every focus mutation is exact CAS.
- A checkpoint identity is exactly `project:{project}:workstream:{workstream_id}:checkpoint`. A new active item, L0/L1/L2, sources, supersession, workstream CAS, optional focus CAS, event, semantic epoch, and journal commit or roll back together. A failed CAS leaves the old checkpoint authoritative and active with no new item, layer, source, event, epoch, or journal residue.
- Default Context retrieval and injection exclude `WORKSTREAM_CHECKPOINT` and `WORKSTREAM_RECOVERY` even when FTS or HNSW ranks them first. Only continuity exact reads and explicit admin content-type reads may access them automatically.
- Lease secrets are server-side capabilities: 256 random bits, `repr=False`, held in owned `bytearray` buffers inside one transport handler, compared directly in constant time, zeroed at shutdown, and absent from schema/results/logs/checkpoints/model input.
- One stdio `MemoryMCPServer` instance is one transport session. A multiplexed network transport without isolated authenticated session storage must not advertise `evolvmem.continuity.v1`.
- A lease is active exactly when `lease_until > server_utc_now`. Equality is expired. A content mutation without the caller’s current active capability returns a stable refusal and never claims while writing.
- `continuity_resume` chooses one primary result in this order: project/binding resolution; no-focus branch; pointer/Context/L2/terminal integrity; foreign active lease; repository staleness; workflow status.
- Repository priority is exactly `wrong_workspace > unknown > head_diverged > branch_changed > head_advanced > worktree_changed > fresh`. Only `fresh` permits immediate execution. `wrong_workspace` returns no L1 or L2.
- Checkpoint/recovery strings, arrays, artifacts, and verification summaries have explicit Config caps. Artifacts are normalized repository-relative paths. Source Context IDs must exist, be visible, not deleted, and be same-project or allowed global.
- Recovered checkpoint data is untrusted history. Current system/developer/user instructions, current code, and current tests always win, and every rendered continuation block states that boundary.
- A single no-focus unfinished candidate is confirmation-only. After explicit confirmation the caller must first claim that candidate using current `state_version`, then CAS switch focus using the returned target state version and current `focus.revision`.
- An explicit new goal leaves an existing focus untouched. Creating a new non-focus workstream does not pause, replace, or clear the old focus; only an explicit, separately authorized focus switch may change it.
- If a checkpoint write is denied by filesystem/database permission, the adapter reports `checkpoint_not_saved` and states that the old checkpoint remains authoritative. It must not imply that a handoff was persisted.
- All automated tests and initial two-Agent acceptance use unique owned temporary data/config/key/vector/history roots. Task 12 is the sole pre-approval exception: it may discover live Config read-only and open the live database only with SQLite `mode=ro` plus `PRAGMA query_only=ON` to render the bounded approval packet. It remains a hard stop before any live DB/config/key/vector mutation, writer stop/restart, deployment, or service restart.
- Codex `--ephemeral` alone is insufficient. The handoff runner also uses `--ignore-user-config`, `--ignore-rules`, `-c memories.use_memories=false`, `-c memories.generate_memories=false`, and `-c history.persistence=none`, and bubblewrap hides the normal `.codex` tree behind an empty mount while binding only the required authentication file read-only. Do not override `HOME` or `CODEX_HOME`. Event audit must reject `memory_search`, `context_search`, and every shell/file/database read that directly targets the canary EvolvMem store or any Codex memory/session/history store; inability to prove this isolation is a hard refusal with no unisolated fallback.
- A real apply is successful only at `maintenance stage=verified,status=completed` with project, rollup, vector, focus, pointer, checkpoint, mutation-journal, and continuity gates passing.
- Before Task 12, record a user-selected deployment candidate: either the reviewed feature-worktree commit with dirty WIP explicitly excluded, or an owner-authorized integration commit that combines and verifies the WIP. Never silently merge, reset, stash, or deploy the dirty checkout.
- Any code, config, or migration change prompted by review after Task 11 or after deployment returns to focused red/green tests, both temporary acceptance gates, the Python 3.10 full-suite gate, independent review, and a newly generated Task 12 digest/approval. The prior approval cannot authorize changed bits or a changed plan.
- After actual diagnosis/fix verification, update `/home/jiangli/fix-records/records/2026-09-01-evolvmem-project-continuity.md` using the six literal headings `症状 / 排查过程 / 根因 / 修复内容 / 验证 / 遗留事项`. Never describe an unrun migration or handoff as fixed.

---

### Task 1: Define bounded continuity models and the canonical checkpoint codec

**Files:**

- Create: `evolvmem/continuity_models.py`
- Create: `evolvmem/continuity_checkpoint.py`
- Create: `tests/test_continuity_models.py`
- Create: `tests/test_continuity_checkpoint.py`
- Modify: `evolvmem/config.py:18` (`Config`)

**Interfaces:**

- Consumes: `ProjectService.resolve_workspace(request: ResolveWorkspaceRequest) -> WorkspaceProjectResolution`, `WorkspaceIdentityProvider.digest_private(domain: str, payload: bytes) -> str`, and existing `ContextContentType`/`ContextLayer` values.
- Produces: `ContinuityCheckpointRequest`, `ContinuityResumeRequest`, `ContinuityListRequest`, `CheckpointPayload`, `CheckpointAuthority`, `ContinuityMutationResult`, `RepoAnchor`, `RepoAnchor.unknown(workspace_fingerprint: str) -> RepoAnchor`, `RepoStaleness`, `LeaseGrant`, `LeaseSessionPort`, stable public result/error enums, `ContinuityBounds.defaults() -> ContinuityBounds`, `validate_checkpoint_payload(payload: CheckpointPayload, bounds: ContinuityBounds) -> CheckpointPayload`, and `encode_checkpoint_v1(authority: CheckpointAuthority, payload: CheckpointPayload, bounds: ContinuityBounds) -> str`.

- [ ] **Step 1: Verify prerequisites and the clean isolated baseline**

Run:

```bash
git branch --show-current
git status --short
git log --oneline -24
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q
```

Expected: branch `feat/evolvmem-project-continuity`, clean worktree, reviewed cleanup/Web commits, and zero test failures. Confirm all Config instances used below point to pytest temp roots.

- [ ] **Step 2: Write the failing model and codec tests**

Add these representative tests, then extend the same tables with every action/result code, every bounded field, invalid action/field combinations, boolean-as-revision rejection, escaping/absolute artifacts, duplicate sources, authority-field injection, sensitive content, non-canonical JSON, and missing/unknown fields:

```python
import json

import pytest

from evolvmem.continuity_checkpoint import encode_checkpoint_v1
from evolvmem.continuity_models import (
    CheckpointAuthority,
    CheckpointPayload,
    ContinuityAction,
    ContinuityBounds,
    ContinuityCheckpointRequest,
    ContinuityValidationError,
    RepoAnchor,
    WorkstreamStatus,
)


def test_checkpoint_request_rejects_boolean_revision() -> None:
    with pytest.raises(
        ContinuityValidationError,
        match="expected_state_version must be an integer",
    ):
        ContinuityCheckpointRequest(
            action=ContinuityAction.CLAIM,
            workspace_path="/tmp/caller-workspace",
            workstream_id="ws_unit",
            expected_state_version=False,
        )


def test_checkpoint_v1_is_canonical_and_server_authoritative() -> None:
    authority = CheckpointAuthority(
        workstream_id="ws_unit",
        parent_workstream_id=None,
        project="evolvmem",
        workspace_fingerprint="hmac-sha256:" + "a" * 64,
        checkpoint_revision=3,
        status=WorkstreamStatus.OPEN,
        repo=RepoAnchor(
            kind="git",
            branch="feat/unit",
            root_commit="1" * 40,
            head_commit="2" * 40,
            worktree_state_hash="hmac-sha256:" + "b" * 64,
        ),
    )
    payload = CheckpointPayload(
        objective="Finish exact continuity",
        accepted_decisions=("Use exact focus",),
        completed_steps=("Defined the schema",),
        current_step="Write codec tests",
        next_action="Implement canonical encoding",
        blockers=(),
        artifacts=(),
        verification=(),
        source_context_ids=(17,),
    )

    encoded = encode_checkpoint_v1(authority, payload, ContinuityBounds.defaults())

    assert encoded == encode_checkpoint_v1(
        authority, payload, ContinuityBounds.defaults()
    )
    assert encoded == json.dumps(
        json.loads(encoded),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert json.loads(encoded)["checkpoint_revision"] == 3
    assert json.loads(encoded)["source_context_ids"] == [17]
```

- [ ] **Step 3: Run the focused tests and observe red**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_models.py tests/test_continuity_checkpoint.py
```

Expected: FAIL during import because `continuity_models` and `continuity_checkpoint` do not exist. A validation assertion failure after those modules exist is also a valid red; an unrelated failure is not.

- [ ] **Step 4: Implement immutable models, the lease port, bounds, and codec**

Use `collections.abc.Sequence` in public annotations where a variadic immutable sequence is needed, normalize it to a tuple in `__post_init__`, and reject booleans before integer range checks. Define `LeaseSessionPort` before `ContinuityService` exists:

```python
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class WorkstreamStatus(str, Enum):
    OPEN = "open"
    PAUSED = "paused"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ContinuityAction(str, Enum):
    CREATE = "create"
    UPDATE = "update"
    PAUSE = "pause"
    BLOCK = "block"
    RESUME = "resume"
    UNBLOCK = "unblock"
    COMPLETE = "complete"
    CANCEL = "cancel"
    CLAIM = "claim"
    RENEW = "renew"
    RELEASE = "release"
    SWITCH_FOCUS = "switch_focus"
    CLEAR_FOCUS = "clear_focus"
    ACCEPT_RECOVERY_NEW = "accept_recovery_new"
    ACCEPT_RECOVERY_EXISTING = "accept_recovery_existing"
    REJECT_RECOVERY = "reject_recovery"


@dataclass(frozen=True, slots=True)
class LeaseGrant:
    token_hash: str
    writer_hash: str


class LeaseSessionPort(Protocol):
    @property
    def writer_hash(self) -> str:
        raise NotImplementedError

    def issue(self, workstream_id: str, lease_epoch: int) -> LeaseGrant:
        raise NotImplementedError

    def owns(
        self,
        workstream_id: str,
        lease_epoch: int,
        token_hash: str,
    ) -> bool:
        raise NotImplementedError

    def discard(self, workstream_id: str) -> None:
        raise NotImplementedError
```

Freeze request fields exactly:

```python
@dataclass(frozen=True, slots=True)
class ContinuityCheckpointRequest:
    action: ContinuityAction
    workspace_path: str
    project_hint: str = ""
    workstream_id: str = ""
    recovery_candidate_id: str = ""
    parent_workstream_id: str = ""
    payload: CheckpointPayload | None = None
    expected_checkpoint_revision: int = 0
    expected_state_version: int = 0
    expected_focus_revision: int = 0
    expected_candidate_revision: int = 0
    make_focus: bool = False
    confirm_project_binding: bool = False
    expected_registry_revision: int = 0
    expected_binding_revision: int = 0
```

`ContinuityResumeRequest(workspace_path: str, project_hint: str = "", max_chars: int | None = None)` and `ContinuityListRequest(workspace_path: str, project_hint: str = "", limit: int = 20)` use the same transient-path validation. `CheckpointPayload` has exact fields `objective`, `accepted_decisions`, `completed_steps`, `current_step`, `next_action`, `blockers`, `artifacts`, `verification`, and `source_context_ids`. `CheckpointAuthority` contains only server-owned workstream/project/fingerprint/revision/status/repo fields.

Implement canonical encoding with no client authority merge:

```python
def encode_checkpoint_v1(
    authority: CheckpointAuthority,
    payload: CheckpointPayload,
    bounds: ContinuityBounds,
) -> str:
    checked = validate_checkpoint_payload(payload, bounds)
    body = {
        "accepted_decisions": list(checked.accepted_decisions),
        "artifacts": [
            {"path": item.path, "role": item.role}
            for item in checked.artifacts
        ],
        "blockers": list(checked.blockers),
        "checkpoint_revision": authority.checkpoint_revision,
        "completed_steps": list(checked.completed_steps),
        "current_step": checked.current_step,
        "next_action": checked.next_action,
        "objective": checked.objective,
        "parent_workstream_id": authority.parent_workstream_id,
        "project": authority.project,
        "repo": {
            "branch": authority.repo.branch,
            "head_commit": authority.repo.head_commit,
            "kind": authority.repo.kind,
            "root_commit": authority.repo.root_commit,
            "worktree_state_hash": authority.repo.worktree_state_hash,
        },
        "schema_version": 1,
        "source_context_ids": sorted(set(checked.source_context_ids)),
        "status": authority.status.value,
        "verification": [
            {
                "args_digest": item.args_digest,
                "at": item.at,
                "outcome": item.outcome.value,
                "program": item.program,
                "summary": item.summary,
            }
            for item in checked.verification
        ],
        "workspace_fingerprint": authority.workspace_fingerprint,
        "workstream_id": authority.workstream_id,
    }
    encoded = json.dumps(
        body,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if len(encoded) > bounds.l2_max_chars:
        raise ContinuityValidationError("checkpoint L2 exceeds configured limit")
    return encoded
```

Add Config defaults for 15-minute leases, 7-day recovery expiry, and explicit objective/decision/step/blocker/artifact/verification/source/list/total L2 caps. Preserve every existing config field, including the dirty-WIP skill/session-mining fields.

- [ ] **Step 5: Run model, codec, and Config tests and observe green**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_models.py tests/test_continuity_checkpoint.py tests/test_runtime_contract.py
git diff --check
```

Expected: PASS with no undeclared model fields, non-canonical serialization, or Config round-trip regression.

- [ ] **Step 6: Commit Task 1**

Run:

```bash
git add evolvmem/continuity_models.py evolvmem/continuity_checkpoint.py evolvmem/config.py tests/test_continuity_models.py tests/test_continuity_checkpoint.py
git commit -m "feat: define continuity checkpoint contracts"
```

---

### Task 2: Persist workstreams, permanent focus rows, and append-only events

**Files:**

- Create: `evolvmem/continuity_store.py`
- Create: `tests/test_continuity_store.py`
- Modify: `evolvmem/continuity_models.py: WorkstreamRecord, WorkstreamMutation, FocusRecord, FocusMutation, and ContinuityEvent symbols`
- Modify: `evolvmem/context_store.py:257` (`ContextStore`) and `evolvmem/context_store.py:317` (`ContextStore.transaction`; prerequisite plan adds `semantic_transaction`)
- Read: `evolvmem/project_store.py: ProjectBindingLifecyclePort and ProjectStore binding lifecycle call sites`
- Modify: `evolvmem/mutation_state.py: SEMANTIC_PROJECTIONS`
- Modify: `tests/test_context_store.py: current ContextStore transaction/bootstrap tests`
- Modify: `tests/test_project_store.py: prerequisite binding CAS tests`
- Modify: `tests/test_mutation_state.py: semantic projection inventory and direct-write drift tests`

**Interfaces:**

- Consumes: `ContextStore.semantic_transaction(kind: str, owner_run_id: str | None = None) -> ContextManager[ContextStore]`, `ContextStore.require_semantic_transaction(operation: str) -> None`, `ContextStore.current_mutation_epoch() -> int`, cleanup Task 3 `ProjectBindingLifecyclePort.ensure_focus_for_active_binding(*, project: str, workspace_fingerprint: str) -> None`, `ProjectBindingLifecyclePort.clear_focus_for_inactive_binding(*, project: str, workspace_fingerprint: str) -> None`, `ProjectStore(context_store: ContextStore, *, binding_lifecycle: ProjectBindingLifecyclePort | None = None)`, active `ProjectWorkspaceBindingRecord` values, and Task 1 continuity records.
- Produces: `WorkstreamRecord`, `WorkstreamMutation`, `FocusRecord`, `FocusMutation`, `ContinuityEvent`, `ContinuityBindingLifecycle(context_store: ContextStore, store: ContinuityStore)` implementing the prerequisite port, and `build_continuity_project_store(context_store: ContextStore) -> tuple[ProjectStore, ContinuityStore]`; factory `ContinuityEvent.from_result(action: ContinuityAction, result: ContinuityMutationResult, writer_hash: str, at: datetime) -> ContinuityEvent`; repository methods `ContinuityStore.ensure_focus_row(project: str, workspace_fingerprint: str) -> FocusRecord`, `get_focus(project: str, workspace_fingerprint: str) -> FocusRecord`, `list_unfinished(project: str, workspace_fingerprint: str, limit: int) -> Sequence[WorkstreamRecord]`, `get_workstream(workstream_id: str) -> WorkstreamRecord | None`, `insert_workstream(record: WorkstreamRecord) -> None`, `cas_workstream(mutation: WorkstreamMutation) -> WorkstreamRecord`, `cas_focus(mutation: FocusMutation) -> FocusRecord`, `append_event(event: ContinuityEvent) -> None`; module helper `utc_now_text() -> str`; and complete `SEMANTIC_PROJECTIONS` entries for `continuity_workstreams`, `continuity_focus`, and `continuity_events`.

- [ ] **Step 1: Write failing schema, focus-CAS, and semantic-epoch tests**

Add complete DDL assertions for `continuity_workstreams`, `continuity_focus`, and `continuity_events`, plus this representative permanent-row test:

```python
import pytest

from evolvmem.continuity_models import FocusMutation
from evolvmem.continuity_store import ContinuityConflict


def _journal_count(context_store) -> int:
    row = context_store._connection().execute(
        "SELECT COUNT(*) FROM context_mutation_journal"
    ).fetchone()
    return int(row[0])


def _focus_row_count(context_store, project: str, fingerprint: str) -> int:
    row = context_store._connection().execute(
        """
        SELECT COUNT(*)
          FROM continuity_focus
         WHERE project = ? AND workspace_fingerprint = ?
        """,
        (project, fingerprint),
    ).fetchone()
    return int(row[0])


def test_focus_clear_keeps_row_and_commits_one_semantic_epoch(
    context_store,
    continuity_store,
) -> None:
    with context_store.semantic_transaction("continuity:ensure_focus"):
        continuity_store.ensure_focus_row(
            project="evolvmem",
            workspace_fingerprint="hmac-sha256:" + "c" * 64,
        )
    initial = continuity_store.get_focus(
        "evolvmem", "hmac-sha256:" + "c" * 64
    )
    before_epoch = context_store.current_mutation_epoch()
    before_journal = _journal_count(context_store)

    with context_store.semantic_transaction("continuity:clear_focus"):
        cleared = continuity_store.cas_focus(
            FocusMutation(
                project=initial.project,
                workspace_fingerprint=initial.workspace_fingerprint,
                expected_revision=initial.revision,
                expected_workstream_id=initial.workstream_id,
                next_workstream_id=None,
            )
        )

    assert cleared.workstream_id is None
    assert cleared.revision == initial.revision + 1
    assert _focus_row_count(
        context_store,
        initial.project,
        initial.workspace_fingerprint,
    ) == 1
    assert context_store.current_mutation_epoch() == before_epoch + 1
    assert _journal_count(context_store) == before_journal + 1

    with pytest.raises(ContinuityConflict, match="focus_revision_conflict"):
        with context_store.semantic_transaction("continuity:clear_focus"):
            continuity_store.cas_focus(
                FocusMutation(
                    project=initial.project,
                    workspace_fingerprint=initial.workspace_fingerprint,
                    expected_revision=initial.revision,
                    expected_workstream_id=initial.workstream_id,
                    next_workstream_id=None,
                )
            )
    assert context_store.current_mutation_epoch() == before_epoch + 1


def test_focus_cas_refuses_a_direct_nonsemantic_call(
    continuity_store,
    seeded_focus,
) -> None:
    with pytest.raises(
        RuntimeError,
        match="cas_focus requires an active semantic transaction",
    ):
        continuity_store.cas_focus(
            FocusMutation(
                project=seeded_focus.project,
                workspace_fingerprint=seeded_focus.workspace_fingerprint,
                expected_revision=seeded_focus.revision,
                expected_workstream_id=seeded_focus.workstream_id,
                next_workstream_id=None,
            )
        )
```

Also test direct-call refusal for `ensure_focus_row`, `insert_workstream`, `cas_workstream`, `cas_focus`, and `append_event`; initial NULL/revision 0; idempotent old-schema bootstrap; active-binding coverage; target same-project/workspace/nonterminal checks; two concurrent switches yielding one success; independent checkpoint/state/lease/focus values; append-only event constraints; and binding revoke clearing but retaining the empty focus row. Construct integration fixtures as `ProjectStore(context_store, binding_lifecycle=ContinuityBindingLifecycle(context_store, continuity_store))`. Assert activation invokes ensure after the binding row CAS but before its audit event; revoke/archive invokes clear before its audit event; lifecycle failure rolls back the binding row, registry revision, focus row, event, epoch, and journal. A NULL clear is a no-op, while a non-NULL clear increments the permanent row exactly once. In `tests/test_mutation_state.py`, add the exact inventory and direct-bypass regression:

```python
from evolvmem.mutation_state import SEMANTIC_PROJECTIONS


def test_continuity_tables_are_complete_semantic_projections() -> None:
    assert SEMANTIC_PROJECTIONS["continuity_workstreams"] == (
        "id", "project", "workspace_fingerprint", "parent_id",
        "current_context_id", "checkpoint_revision", "state_version",
        "status", "repo_anchor_json", "lease_token_hash",
        "lease_writer_hash", "lease_until", "lease_epoch", "created_at",
        "updated_at", "completed_at",
    )
    assert SEMANTIC_PROJECTIONS["continuity_focus"] == (
        "project", "workspace_fingerprint", "workstream_id", "revision",
        "updated_at",
    )
    assert SEMANTIC_PROJECTIONS["continuity_events"] == (
        "id", "workstream_id", "candidate_id", "event_type",
        "before_checkpoint_revision", "after_checkpoint_revision",
        "before_state_version", "after_state_version",
        "before_focus_revision", "after_focus_revision",
        "before_lease_epoch", "after_lease_epoch", "writer_hash",
        "error_code", "created_at",
    )


def test_direct_continuity_write_without_epoch_is_detected(
    context_store,
    continuity_store,
    open_workstream_record,
) -> None:
    with context_store.semantic_transaction("continuity:test_seed"):
        continuity_store.insert_workstream(open_workstream_record)
    expected_epoch = context_store.current_mutation_epoch()
    expected_digest = context_store.canonical_state_digest()

    context_store._connection().execute(
        "UPDATE continuity_workstreams SET state_version=state_version+1 WHERE id=?",
        (open_workstream_record.id,),
    )
    context_store._connection().commit()

    report = context_store.detect_unjournaled_mutation(
        expected_epoch,
        expected_digest,
    )
    assert report.reason_codes == ("semantic_digest_changed_without_epoch",)
```

- [ ] **Step 2: Run the focused store tests and observe red**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_store.py tests/test_project_store.py -k "continuity or focus or binding"
```

Expected: FAIL because the continuity schema, repository, and semantic projections are absent.

- [ ] **Step 3: Implement the schema and narrow same-connection repository**

`ContinuityStore` receives an initialized `ContextStore` and never opens or commits another connection. Use parameterized operations and exact rowcount CAS:

```python
class ContinuityStore:
    def __init__(self, context_store: ContextStore) -> None:
        self.context_store = context_store

    def ensure_focus_row(
        self,
        project: str,
        workspace_fingerprint: str,
    ) -> FocusRecord:
        self.context_store.require_semantic_transaction("ensure_focus_row")
        self.context_store._connection().execute(
            """
            INSERT INTO continuity_focus(
                project, workspace_fingerprint, workstream_id, revision, updated_at
            )
            VALUES (?, ?, NULL, 0, ?)
            ON CONFLICT(project, workspace_fingerprint) DO NOTHING
            """,
            (project, workspace_fingerprint, utc_now_text()),
        )
        return self.get_focus(project, workspace_fingerprint)

    def cas_focus(self, mutation: FocusMutation) -> FocusRecord:
        self.context_store.require_semantic_transaction("cas_focus")
        cursor = self.context_store._connection().execute(
            """
            UPDATE continuity_focus
               SET workstream_id = ?,
                   revision = revision + 1,
                   updated_at = ?
             WHERE project = ?
               AND workspace_fingerprint = ?
               AND revision = ?
               AND workstream_id IS ?
            """,
            (
                mutation.next_workstream_id,
                utc_now_text(),
                mutation.project,
                mutation.workspace_fingerprint,
                mutation.expected_revision,
                mutation.expected_workstream_id,
            ),
        )
        if cursor.rowcount != 1:
            raise ContinuityConflict("focus_revision_conflict")
        return self.get_focus(
            mutation.project,
            mutation.workspace_fingerprint,
        )
```

The DDL must include all design columns, foreign keys, status checks, unfinished/project-workspace indexes, and no UPDATE/DELETE path for `continuity_events`. Every mutator begins with `self.context_store.require_semantic_transaction` using its literal operation name: `"ensure_focus_row"`, `"insert_workstream"`, `"cas_workstream"`, `"cas_focus"`, `"append_event"`, `"insert_recovery_context"`, `"insert_recovery_candidate"`, `"cas_recovery_candidate"`, or `"expire_recovery_candidates"`. Read methods do not require a mutation transaction. Binding registration/promotion creates the empty focus row in the same semantic transaction; revoke/archive first CAS-clears the focus and leaves its NULL row. Each successful binding/focus integration produces one semantic epoch/journal entry, never a nested second entry.

Implement the finalized prerequisite port on the borrowed connection; do not add another ProjectStore callback contract:

```python
class ContinuityBindingLifecycle(ProjectBindingLifecyclePort):
    def __init__(
        self,
        context_store: ContextStore,
        store: ContinuityStore,
    ) -> None:
        self.context_store = context_store
        self.store = store

    def ensure_focus_for_active_binding(
        self,
        *,
        project: str,
        workspace_fingerprint: str,
    ) -> None:
        self.context_store.require_semantic_transaction(
            "ensure_focus_for_active_binding"
        )
        focus = self.store.ensure_focus_row(project, workspace_fingerprint)
        if focus.workstream_id is not None:
            raise ContinuityConflict("active_binding_focus_not_empty")

    def clear_focus_for_inactive_binding(
        self,
        *,
        project: str,
        workspace_fingerprint: str,
    ) -> None:
        self.context_store.require_semantic_transaction(
            "clear_focus_for_inactive_binding"
        )
        focus = self.store.get_focus(project, workspace_fingerprint)
        if focus.workstream_id is None:
            return
        self.store.cas_focus(
            FocusMutation(
                project=project,
                workspace_fingerprint=workspace_fingerprint,
                expected_revision=focus.revision,
                expected_workstream_id=focus.workstream_id,
                next_workstream_id=None,
            )
        )


def build_continuity_project_store(
    context_store: ContextStore,
) -> tuple[ProjectStore, ContinuityStore]:
    continuity_store = ContinuityStore(context_store)
    lifecycle = ContinuityBindingLifecycle(context_store, continuity_store)
    project_store = ProjectStore(
        context_store,
        binding_lifecycle=lifecycle,
    )
    return project_store, continuity_store
```

Cleanup Task 3 already calls these methods only after its binding row CAS and before its binding event, inside the caller’s semantic transaction. Use `build_continuity_project_store` at every continuity-enabled composition root; Task 8 performs the MCP/operator production composition. With continuity absent, cleanup’s optional `None` remains the old-schema planner path only.

Add the new tables to the prerequisite digest in the same commit as their DDL. The event projection deliberately includes only bounded audit columns and never L1/L2 content:

```python
SEMANTIC_PROJECTIONS.update(
    {
        "continuity_workstreams": (
            "id", "project", "workspace_fingerprint", "parent_id",
            "current_context_id", "checkpoint_revision", "state_version",
            "status", "repo_anchor_json", "lease_token_hash",
            "lease_writer_hash", "lease_until", "lease_epoch", "created_at",
            "updated_at", "completed_at",
        ),
        "continuity_focus": (
            "project", "workspace_fingerprint", "workstream_id", "revision",
            "updated_at",
        ),
        "continuity_events": (
            "id", "workstream_id", "candidate_id", "event_type",
            "before_checkpoint_revision", "after_checkpoint_revision",
            "before_state_version", "after_state_version",
            "before_focus_revision", "after_focus_revision",
            "before_lease_epoch", "after_lease_epoch", "writer_hash",
            "error_code", "created_at",
        ),
    }
)
```

- [ ] **Step 4: Run store, binding, and production-boundary tests and observe green**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_store.py tests/test_context_store.py tests/test_project_store.py tests/test_mutation_state.py tests/test_production_write_boundaries.py
git diff --check
```

Expected: PASS; stale CAS and injected rollback leave the focus/workstream/event tables, epoch, and semantic journal unchanged.

- [ ] **Step 5: Commit Task 2**

Run:

```bash
git add evolvmem/continuity_store.py evolvmem/continuity_models.py evolvmem/context_store.py evolvmem/mutation_state.py tests/test_continuity_store.py tests/test_context_store.py tests/test_project_store.py tests/test_mutation_state.py tests/test_production_write_boundaries.py
git commit -m "feat: persist continuity workstreams and focus"
```

---

### Task 3: Capture server-owned repository anchors and deterministic staleness

**Files:**

- Create: `evolvmem/continuity_repo.py`
- Create: `tests/test_continuity_repo.py`
- Modify: `evolvmem/continuity_models.py: RepoAnchor and RepoStaleness symbols`

**Interfaces:**

- Consumes: transient `workspace_path` and `WorkspaceIdentityProvider.digest_private("continuity.repo.worktree.v1", payload: bytes) -> str`.
- Produces: `RepoAnchorProvider.capture(workspace_path: str) -> RepoAnchor` and `RepoAnchorProvider.compare(saved: RepoAnchor, current: RepoAnchor) -> RepoStaleness` with orthogonal flags and one primary code.

- [ ] **Step 1: Write failing capture and precedence tests**

Use real temporary Git repositories and fixed local identity:

```python
import subprocess

from evolvmem.continuity_models import RepoStalenessCode
from evolvmem.continuity_repo import RepoAnchorProvider


def _git(repo, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def test_capture_and_compare_report_head_advanced_without_paths(
    tmp_path,
    workspace_identity,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Continuity Test")
    _git(repo, "config", "user.email", "continuity@example.invalid")
    (repo / "state.txt").write_text("one", encoding="utf-8")
    _git(repo, "add", "state.txt")
    _git(repo, "commit", "-qm", "first")
    provider = RepoAnchorProvider(workspace_identity)
    saved = provider.capture(str(repo))

    (repo / "state.txt").write_text("two", encoding="utf-8")
    _git(repo, "commit", "-qam", "second")
    result = provider.compare(saved, provider.capture(str(repo)))

    assert result.primary_code is RepoStalenessCode.HEAD_ADVANCED
    assert "head_advanced" in result.flags
    assert str(repo) not in repr(saved)
    assert "state.txt" not in repr(saved)
```

Add table cases for root commit, same Git common-dir worktrees, dirty digest, detached HEAD, no-commit Git, non-Git, Git timeout/failure, divergence, branch change, wrong fingerprint, and every multi-flag combination. Assert the exact primary order `wrong_workspace > unknown > head_diverged > branch_changed > head_advanced > worktree_changed > fresh` and that `wrong_workspace` marks checkpoint layers undisclosable.

- [ ] **Step 2: Run repository tests and observe red**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_repo.py
```

Expected: FAIL because `RepoAnchorProvider` is absent.

- [ ] **Step 3: Implement fixed-argv capture and deterministic comparison**

Use no shell, a bounded timeout/output, and canonical porcelain bytes:

```python
class RepoAnchorProvider:
    def __init__(self, identity: WorkspaceIdentityProvider) -> None:
        self.identity = identity

    def capture(self, workspace_path: str) -> RepoAnchor:
        workspace = self.identity.resolve(workspace_path)
        try:
            root = self._git(workspace_path, "rev-list", "--max-parents=0", "HEAD")
            head = self._git(workspace_path, "rev-parse", "HEAD")
            branch = self._git(
                workspace_path,
                "symbolic-ref",
                "--quiet",
                "--short",
                "HEAD",
                allow_nonzero=True,
            )
            status = self._git_bytes(
                workspace_path,
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
            )
        except RepoCaptureError:
            return RepoAnchor.unknown(workspace.fingerprint)
        return RepoAnchor(
            kind="git",
            branch=branch,
            root_commit=root.splitlines()[0] if root else "",
            head_commit=head,
            worktree_state_hash=self.identity.digest_private(
                "continuity.repo.worktree.v1",
                status,
            ),
            workspace_fingerprint=workspace.fingerprint,
        )

    def _git(self, workspace_path: str, *args: str, allow_nonzero: bool = False) -> str:
        output = self._git_bytes(
            workspace_path,
            *args,
            allow_nonzero=allow_nonzero,
        )
        return output.decode("utf-8", errors="strict").strip()

    def _git_bytes(
        self,
        workspace_path: str,
        *args: str,
        allow_nonzero: bool = False,
    ) -> bytes:
        completed = subprocess.run(
            ["git", "-C", workspace_path, *args],
            check=False,
            capture_output=True,
            timeout=5,
        )
        if completed.returncode != 0 and not allow_nonzero:
            raise RepoCaptureError("git_probe_failed")
        if len(completed.stdout) > 65536:
            raise RepoCaptureError("git_probe_too_large")
        return completed.stdout
```

`compare` computes all independent flags first, then selects the first present member from the frozen priority tuple. Inability to prove identity is `unknown`, never `fresh`. Store no status text, file names, commands, remotes, stderr, or path.

- [ ] **Step 4: Run repository tests and observe green**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_repo.py
git diff --check
```

Expected: PASS for every capture and precedence case.

- [ ] **Step 5: Commit Task 3**

Run:

```bash
git add evolvmem/continuity_repo.py evolvmem/continuity_models.py tests/test_continuity_repo.py
git commit -m "feat: anchor continuity checkpoints to repositories"
```

---

### Task 4: Enforce the atomic checkpoint and workflow state machine

**Files:**

- Create: `evolvmem/continuity_service.py`
- Create: `tests/test_continuity_service.py`
- Modify: `evolvmem/context_models.py:39` (`ContextContentType`)
- Modify: `evolvmem/context_service.py:187` (`ContextService` typed boundary)
- Modify: `tests/test_context_models.py: existing ContextContentType validation tests`
- Modify: `tests/test_context_service.py: existing semantic service tests`
- Modify: `tests/test_production_write_boundaries.py:179` (`test_production_modules_have_no_raw_write_bypasses`)

**Interfaces:**

- Consumes: Task 1 `LeaseSessionPort` and codec, Task 2 `ContinuityStore`, Task 3 `RepoAnchorProvider`, `ProjectService.resolve_workspace(request: ResolveWorkspaceRequest) -> WorkspaceProjectResolution`, and Context item/layer/source/supersession methods under one semantic transaction.
- Produces: `ContinuityService.__init__(context_store: ContextStore, store: ContinuityStore, project_service: ProjectService, repo: RepoAnchorProvider, clock: Clock)`, `ContinuityService.checkpoint(request: ContinuityCheckpointRequest, *, lease_session: LeaseSessionPort) -> ContinuityMutationResult`, `ContinuityService.create(request: ContinuityCheckpointRequest, *, lease_session: LeaseSessionPort) -> ContinuityMutationResult`, internal `_CapabilityEffects.issue(session: LeaseSessionPort, workstream_id: str, lease_epoch: int) -> LeaseGrant`, `_CapabilityEffects.discard_after_commit(workstream_id: str) -> None`, `_CapabilityEffects.rollback(session: LeaseSessionPort) -> None`, `_CapabilityEffects.committed(session: LeaseSessionPort) -> None`, `_insert_initial_checkpoint_in_transaction(request: ContinuityCheckpointRequest, resolution: WorkspaceProjectResolution, payload: CheckpointPayload, source_context_ids: Sequence[int], lease_session: LeaseSessionPort, effects: _CapabilityEffects, workstream_id: str) -> ContinuityMutationResult`, `_write_checkpoint_in_transaction(request: ContinuityCheckpointRequest, resolution: WorkspaceProjectResolution, target: WorkstreamRecord, payload: CheckpointPayload, source_context_ids: Sequence[int], lease_session: LeaseSessionPort) -> ContinuityMutationResult`, stable `ContinuityConflict`/`ContinuityPermissionDenied` codes, and complete closed transition/focus behavior.

- [ ] **Step 1: Write failing create, transition, focus, rollback, and epoch tests**

In `tests/test_continuity_service.py` define a test-only `ContinuityRig` whose `create_open(make_focus: bool)` returns a workstream plus current fake capability, `request(action: ContinuityAction)` supplies current CAS values, `perform_success(action: ContinuityAction)` establishes the required pre-state then invokes the action, `journal_count() -> int` and `event_count() -> int` query their bounded audit tables, `active_identity_count(identity_key: str) -> int` queries active Context rows, and `inject_failure_after(boundary: str)` raises at one named persistence boundary. Use the Task 1 port, not Task 5 concrete session.

Add these representative assertions:

```python
import pytest

from evolvmem.continuity_models import ContinuityAction
from evolvmem.continuity_service import ContinuityConflict


@pytest.mark.parametrize(
    "action",
    [
        ContinuityAction.CREATE,
        ContinuityAction.UPDATE,
        ContinuityAction.PAUSE,
        ContinuityAction.BLOCK,
        ContinuityAction.RESUME,
        ContinuityAction.UNBLOCK,
        ContinuityAction.COMPLETE,
        ContinuityAction.CANCEL,
        ContinuityAction.SWITCH_FOCUS,
        ContinuityAction.CLEAR_FOCUS,
    ],
)
def test_each_successful_service_mutation_has_one_epoch_and_journal(
    continuity_rig,
    action,
) -> None:
    before_epoch = continuity_rig.context_store.current_mutation_epoch()
    before_journal = continuity_rig.journal_count()

    continuity_rig.perform_success(action)

    assert continuity_rig.context_store.current_mutation_epoch() == before_epoch + 1
    assert (
        continuity_rig.journal_count()
        == before_journal + 1
    )


def test_checkpoint_failure_after_sources_keeps_old_checkpoint_authoritative(
    continuity_rig,
) -> None:
    created = continuity_rig.create_open(make_focus=True)
    old_item = continuity_rig.context_store.get_item(created.current_context_id)
    before_epoch = continuity_rig.context_store.current_mutation_epoch()
    before_event_count = continuity_rig.event_count()
    continuity_rig.inject_failure_after("sources")

    with pytest.raises(RuntimeError, match="injected_after_sources"):
        continuity_rig.service.checkpoint(
            continuity_rig.request(ContinuityAction.UPDATE),
            lease_session=continuity_rig.lease_session,
        )

    current = continuity_rig.continuity_store.get_workstream(created.workstream_id)
    assert current.current_context_id == old_item.id
    assert continuity_rig.context_store.get_item(old_item.id).status.value == "active"
    assert continuity_rig.active_identity_count(old_item.identity_key) == 1
    assert continuity_rig.event_count() == before_event_count
    assert continuity_rig.context_store.current_mutation_epoch() == before_epoch


def test_stale_focus_switch_leaves_old_pointer_unchanged(continuity_rig) -> None:
    old_focus, target = continuity_rig.two_owned_open_workstreams()
    request = continuity_rig.switch_request(
        target=target,
        expected_focus_revision=old_focus.revision - 1,
    )

    with pytest.raises(ContinuityConflict, match="focus_revision_conflict"):
        continuity_rig.service.checkpoint(
            request,
            lease_session=continuity_rig.lease_session,
        )

    assert continuity_rig.current_focus() == old_focus


def test_create_commit_failure_discards_precommit_capability(
    continuity_rig,
) -> None:
    expected_id = continuity_rig.peek_next_workstream_id()
    continuity_rig.inject_failure_after("event")

    with pytest.raises(RuntimeError, match="injected_after_event"):
        continuity_rig.service.create(
            continuity_rig.create_request(make_focus=False),
            lease_session=continuity_rig.lease_session,
        )

    assert continuity_rig.lease_session.capability_for(expected_id) is None
    assert continuity_rig.lease_session.discard_calls == (expected_id,)
    assert continuity_rig.continuity_store.get_workstream(expected_id) is None
```

Freeze the state table exactly:

| Current state | Permitted action and next state |
|---|---|
| missing | `create -> open` |
| open | `update -> open`, `pause -> paused`, `block -> blocked`, `complete -> completed`, `cancel -> cancelled` |
| paused | `update -> paused`, `resume -> open`, `complete -> completed`, `cancel -> cancelled` |
| blocked | `update -> blocked`, `unblock -> open`, `pause -> paused`, `complete -> completed`, `cancel -> cancelled` |
| completed/cancelled | no content or lease action |

Freeze focus effects separately from workflow state:

| Action | Focus before | Required focus CAS | Focus after |
|---|---|---|---|
| `create(make_focus=false)` | NULL or existing | none | unchanged |
| `create(make_focus=true)` | NULL or existing | expected focus revision | new workstream |
| `update/resume/unblock` | NULL or target/other | none | unchanged |
| `pause/complete/cancel` | target | expected focus revision | NULL |
| `pause/complete/cancel` | NULL or another target | none | unchanged |
| `block` | NULL or target/other | none | unchanged |
| `claim/renew/release` | any | none | unchanged |
| `switch_focus` | NULL or existing | expected focus revision plus target state/capability and current active capability | target |
| `clear_focus` | existing | expected focus revision plus current state/capability | NULL |
| `accept_recovery_new/existing(make_focus=false)` | NULL or existing | none | unchanged |
| `accept_recovery_new/existing(make_focus=true)` | NULL or existing | expected focus revision | accepted workstream |
| `reject_recovery/expire` | any | none | unchanged |

Thus a newly confirmed goal never changes an old focus merely by creating its workstream. A separate explicit `switch_focus` is required unless the create request itself explicitly sets `make_focus=true` with current CAS.

Test that `update` never resumes/unblocks; pause/complete/cancel clear focus and lease with focus CAS; block retains focus and releases lease. Parent is same project/workspace, not self, and acyclic. Every source is existing, visible, non-deleted, and same-project or allowed global. `switch_focus` validates focus revision, target state version/capability, same workspace, nonterminal target, and current-focus capability when its lease is active. `clear_focus` validates focus revision plus current state/capability. Both leave workflow status and checkpoint revision unchanged.

- [ ] **Step 2: Run the service tests and observe red**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_service.py tests/test_context_models.py tests/test_production_write_boundaries.py
```

Expected: FAIL because `ContinuityService` and the new content types are absent.

- [ ] **Step 3: Implement one semantic mutation boundary and the exact state machine**

Add `WORKSTREAM_CHECKPOINT` and `WORKSTREAM_RECOVERY` to `ContextContentType`. Capability issuance is an in-memory side effect, so pair it with explicit rollback compensation while release-style discard is deferred until SQLite commit succeeds. Dispatch every action through one outer semantic transaction and append one bounded event inside it:

```python
from dataclasses import dataclass, field


@dataclass(slots=True)
class _CapabilityEffects:
    issued_workstream_ids: list[str] = field(default_factory=list)
    post_commit_discards: list[str] = field(default_factory=list)

    def issue(
        self,
        session: LeaseSessionPort,
        workstream_id: str,
        lease_epoch: int,
    ) -> LeaseGrant:
        grant = session.issue(workstream_id, lease_epoch)
        self.issued_workstream_ids.append(workstream_id)
        return grant

    def discard_after_commit(self, workstream_id: str) -> None:
        self.post_commit_discards.append(workstream_id)

    def rollback(self, session: LeaseSessionPort) -> None:
        for workstream_id in reversed(self.issued_workstream_ids):
            session.discard(workstream_id)
        self.issued_workstream_ids.clear()
        self.post_commit_discards.clear()

    def committed(self, session: LeaseSessionPort) -> None:
        for workstream_id in self.post_commit_discards:
            session.discard(workstream_id)
        self.issued_workstream_ids.clear()
        self.post_commit_discards.clear()


_CHECKPOINT_HANDLERS = {
    ContinuityAction.CREATE: "_create",
    ContinuityAction.UPDATE: "_write_checkpoint",
    ContinuityAction.PAUSE: "_write_checkpoint",
    ContinuityAction.BLOCK: "_write_checkpoint",
    ContinuityAction.RESUME: "_write_checkpoint",
    ContinuityAction.UNBLOCK: "_write_checkpoint",
    ContinuityAction.COMPLETE: "_write_checkpoint",
    ContinuityAction.CANCEL: "_write_checkpoint",
    ContinuityAction.SWITCH_FOCUS: "_switch_focus",
    ContinuityAction.CLEAR_FOCUS: "_clear_focus",
}


def checkpoint(
    self,
    request: ContinuityCheckpointRequest,
    *,
    lease_session: LeaseSessionPort,
) -> ContinuityMutationResult:
    try:
        handler_name = _CHECKPOINT_HANDLERS[request.action]
    except KeyError as exc:
        raise ContinuityValidationError("action is not implemented by this slice") from exc
    effects = _CapabilityEffects()
    try:
        with self.context_store.semantic_transaction(
            "continuity:" + request.action.value
        ):
            result = getattr(self, handler_name)(
                request,
                lease_session,
                effects,
            )
            self.store.append_event(
                ContinuityEvent.from_result(
                    action=request.action,
                    result=result,
                    writer_hash=lease_session.writer_hash,
                    at=self.clock.now(),
                )
            )
    except BaseException:
        effects.rollback(lease_session)
        raise
    effects.committed(lease_session)
    return result
```

Implement focus switch without changing status, checkpoint revision, state version, or lease epoch:

```python
def _switch_focus(
    self,
    request: ContinuityCheckpointRequest,
    lease_session: LeaseSessionPort,
    effects: _CapabilityEffects,
) -> ContinuityMutationResult:
    del effects
    resolution = self._resolve_workspace(request.workspace_path, request.project_hint)
    focus = self.store.get_focus(
        resolution.project,
        resolution.workspace_fingerprint,
    )
    target = self._require_unfinished_workstream(
        request.workstream_id,
        resolution,
    )
    if target.state_version != request.expected_state_version:
        raise ContinuityConflict("state_version_conflict")
    if not self._lease_is_active(target) or not lease_session.owns(
        target.id,
        target.lease_epoch,
        target.lease_token_hash or "",
    ):
        raise ContinuityConflict("target_capability_required")
    if focus.workstream_id is not None:
        current = self._require_unfinished_workstream(
            focus.workstream_id,
            resolution,
        )
        if self._lease_is_active(current) and not lease_session.owns(
            current.id,
            current.lease_epoch,
            current.lease_token_hash or "",
        ):
            raise ContinuityConflict("current_focus_lease_held_by_other")
    updated = self.store.cas_focus(
        FocusMutation(
            project=resolution.project,
            workspace_fingerprint=resolution.workspace_fingerprint,
            expected_revision=request.expected_focus_revision,
            expected_workstream_id=focus.workstream_id,
            next_workstream_id=target.id,
        )
    )
    return ContinuityMutationResult.for_focus(target, updated)
```

For create, generate an opaque `ws_` ID, require checkpoint/state 0, resolve the transient workspace, optionally promote a candidate binding using both registry and binding-row CAS, validate parent/sources, and capture repo. Call `grant = effects.issue(lease_session, workstream_id, lease_epoch=1)` before inserting the first active Context item/layers/sources and workstream at checkpoint/state/lease epoch 1; persist only `grant.token_hash` and `grant.writer_hash`. Optionally CAS focus, then append the event. Do not issue UPDATE against a nonexistent row. If any insert, CAS, event append, semantic digest, journal insert, immediately-before-commit hook, or fault-injected SQLite commit proven nondurable fails, the `except BaseException` path discards the issued capability after SQLite rollback, so no in-memory capability can outlive a missing workstream. Never inject an exception after a durable commit and then claim that the transaction rolled back.

For update-like actions, perform this exact order within the same transaction: resolve project/fingerprint/repo; validate expected checkpoint/state, transition, capability, parent, and sources; supersede old item; insert new item; write all three layers and relational sources; CAS workstream/current pointer/revisions/status/repo; CAS-clear focus when required; append event; let semantic transaction write one epoch/journal entry. Any exception rolls back every operation.

- [ ] **Step 4: Run service and atomic-boundary tests and observe green**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_service.py tests/test_context_models.py tests/test_context_service.py tests/test_production_write_boundaries.py
git diff --check
```

Expected: PASS, including injected failures after supersession, each layer, sources, workstream CAS, focus CAS, event append, epoch creation, immediately before outer commit, a fault-injected commit first proven nondurable, and two-writer conflict. The loser leaves no database residue beyond the winner’s single epoch, and a failed create leaves no issued capability. A separate successful-commit test calls the no-throw/idempotent post-commit effect twice and asserts the committed database state remains authoritative.

- [ ] **Step 5: Commit Task 4**

Run:

```bash
git add evolvmem/continuity_service.py evolvmem/context_models.py evolvmem/context_service.py tests/test_continuity_service.py tests/test_context_models.py tests/test_context_service.py tests/test_production_write_boundaries.py
git commit -m "feat: enforce continuity checkpoint state machine"
```

---

### Task 5: Bind leases to one server-side transport session

**Files:**

- Create: `evolvmem/continuity_session.py`
- Create: `tests/test_continuity_session.py`
- Modify: `evolvmem/continuity_models.py: LeaseMutation`
- Modify: `evolvmem/continuity_store.py: ContinuityStore.cas_lease`
- Modify: `evolvmem/continuity_service.py: ContinuityService.checkpoint and lease helpers`
- Modify: `evolvmem/mcp_server.py:114` (`MemoryMCPServer`), `evolvmem/mcp_server.py:120` (`__init__`), and `evolvmem/mcp_server.py:206` (`shutdown`)
- Modify: `tests/test_continuity_service.py: lease action matrix`
- Modify: `tests/test_mcp_protocol.py:123` and `tests/test_mcp_protocol.py:151` (current fake-service close hooks)

**Interfaces:**

- Consumes: Task 1 `LeaseSessionPort` and `LeaseGrant`, Task 4 mutation dispatcher, injected cryptographic RNG and UTC clock, and one `MemoryMCPServer` lifecycle.
- Produces: `SecretBuffer.compare(expected: bytes | bytearray) -> bool`, `SecretBuffer.close() -> None`, `TransportLeaseSession.__init__(*, rng: RandomBytes, workspace_key: bytes)`, `TransportLeaseSession.issue(workstream_id: str, lease_epoch: int) -> LeaseGrant`, `owns(workstream_id: str, lease_epoch: int, token_hash: str) -> bool`, `capability_for(workstream_id: str) -> LeaseCapability | None`, `discard(workstream_id: str) -> None`, `close() -> None`, `LeaseMutation`, `ContinuityStore.cas_lease(mutation: LeaseMutation) -> WorkstreamRecord`, concrete `_claim`, `_renew`, and `_release` handlers using Task 4 `_CapabilityEffects`, and injectable `MemoryMCPServer.__init__(config: Config | None = None, context_service: ContextService | None = None, continuity_service: ContinuityService | None = None, lease_session: TransportLeaseSession | None = None)`.

- [ ] **Step 1: Write failing secret, privacy, ownership, and epoch tests**

Add:

```python
from evolvmem.continuity_session import SecretBuffer, TransportLeaseSession


def test_secret_buffer_compares_owned_bytearray_and_zeroes_it() -> None:
    original = b"s" * 32
    secret = SecretBuffer(original)
    owned = secret._value

    assert secret.compare(original) is True
    assert secret.compare(b"x" * 32) is False

    secret.close()

    assert owned == bytearray(32)
    assert original == b"s" * 32


def test_two_transport_sessions_never_share_capabilities(
    deterministic_rng,
    lease_service_rig,
) -> None:
    first = TransportLeaseSession(
        rng=deterministic_rng.fork(b"first"),
        workspace_key=lease_service_rig.workspace_key,
    )
    second = TransportLeaseSession(
        rng=deterministic_rng.fork(b"second"),
        workspace_key=lease_service_rig.workspace_key,
    )
    created = lease_service_rig.create(first)

    assert first.owns(
        created.workstream_id,
        created.lease_epoch,
        created.lease_token_hash,
    )
    assert not second.owns(
        created.workstream_id,
        created.lease_epoch,
        created.lease_token_hash,
    )


def test_claim_rollback_discards_the_precommit_capability(lease_service_rig) -> None:
    workstream = lease_service_rig.expired_unowned_workstream()
    lease_service_rig.inject_failure_after("lease_cas")

    with pytest.raises(RuntimeError, match="injected_after_lease_cas"):
        lease_service_rig.claim(workstream)

    assert lease_service_rig.session.capability_for(workstream.id) is None
    assert lease_service_rig.session.discard_calls == (workstream.id,)
    assert lease_service_rig.current(workstream.id) == workstream


def test_failed_renew_is_fail_closed_and_never_restores_old_secret(
    lease_service_rig,
) -> None:
    workstream = lease_service_rig.owned_open_workstream()
    old_token = lease_service_rig.session.capability_for(workstream.id).token
    lease_service_rig.inject_failure_after("lease_cas")

    with pytest.raises(RuntimeError, match="injected_after_lease_cas"):
        lease_service_rig.renew(workstream)

    assert old_token._value == bytearray(32)
    assert lease_service_rig.session.capability_for(workstream.id) is None
    assert lease_service_rig.current(workstream.id) == workstream
    assert lease_service_rig.session.discard_calls == (workstream.id,)


def test_release_discards_only_after_the_database_commit(lease_service_rig) -> None:
    retained = lease_service_rig.owned_open_workstream()
    lease_service_rig.inject_failure_after("before_outer_commit")

    with pytest.raises(RuntimeError, match="injected_before_outer_commit"):
        lease_service_rig.release(retained)

    assert lease_service_rig.session.owns(
        retained.id,
        retained.lease_epoch,
        retained.lease_token_hash,
    )
    assert lease_service_rig.session.discard_calls == ()
    assert lease_service_rig.current(retained.id) == retained

    lease_service_rig.clear_failure()
    released = lease_service_rig.release(retained)
    assert released.lease_epoch == retained.lease_epoch + 1
    assert lease_service_rig.session.capability_for(retained.id) is None
    assert lease_service_rig.trace[-2:] == ("database_committed", "capability_discarded")
    lease_service_rig.run_last_committed_effects_again()
    assert lease_service_rig.session.capability_for(retained.id) is None
```

Table-test claim, renew, and release. Claim succeeds only when empty/expired; equality with server UTC is expired; renew/release require current capability; terminal rows reject; old capability fails after expiry/reclaim; crash takeover requires current state CAS. For each successful claim/renew/release assert state version +1, lease epoch +1, checkpoint revision unchanged, semantic epoch +1, journal +1, and event +1. For every stale/foreign failure assert all those counts unchanged. Inject failures after capability issue, lease CAS, event append, semantic digest, journal insert, and immediately before outer commit; if the SQLite commit primitive itself is fault-injected, first prove the transaction is nondurable before asserting rollback. Never model an exception after a durable commit as a rollback. Create, claim, renew, and accept-recovery-new must discard any newly issued capability on every actual rollback path. A failed renew is intentionally fail-closed: `issue` has already replaced and zeroed the old secret, rollback discards the new secret, and the session owns neither secret; it must wait for expiry or use the explicit recovery/claim protocol rather than silently restoring secret material. Release, block, pause, complete, and cancel schedule discard only after commit; rollback retains the still-authoritative capability. `TransportLeaseSession.discard` and `_CapabilityEffects.committed` are no-throw/idempotent so replay after a successful durable commit cannot turn a committed database mutation into an apparent failure.

Also assert 32 RNG bytes per writer secret and per token, domain-separated HMAC hashes, `repr`/MCP schema/results/errors/logs contain no secret/token/writer hash, and shutdown zeroes retained references to every owned mutable buffer.

- [ ] **Step 2: Run lease tests and observe red**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_session.py tests/test_continuity_service.py -k "secret or lease or claim or renew or release"
```

Expected: FAIL because `SecretBuffer` and `TransportLeaseSession` are absent and lease actions are not dispatched.

- [ ] **Step 3: Implement owned buffers, concrete lease session, and lease actions**

The direct comparison must not allocate a bytes copy of the owned secret:

```python
class SecretBuffer:
    def __init__(self, value: bytes) -> None:
        self._value = bytearray(value)
        self._closed = False

    def compare(self, expected: bytes | bytearray) -> bool:
        if self._closed:
            return False
        return hmac.compare_digest(self._value, expected)

    def close(self) -> None:
        if self._closed:
            return
        for index in range(len(self._value)):
            self._value[index] = 0
        self._closed = True


@dataclass(frozen=True, slots=True)
class LeaseCredentials:
    writer_instance: str = field(repr=False)
    secret: SecretBuffer = field(repr=False)


@dataclass(frozen=True, slots=True)
class LeaseCapability:
    workstream_id: str
    lease_epoch: int
    token: SecretBuffer = field(repr=False)
```

`TransportLeaseSession` generates and owns credentials at construction. `issue` generates a fresh 32-byte token, replaces/zeroes any prior capability for that workstream, stores the new capability, and returns only domain-separated token/writer hashes. `owns` retrieves the owned capability, checks workstream and epoch, recomputes the token hash, and compares both the owned token buffer and hash in constant time. `close` zeroes credentials plus every capability, then clears mappings.

Add claim/renew/release to Task 4’s dispatcher. `create` delegates to `checkpoint`, so all issue compensation passes through the same outer boundary. Use one exact lease CAS record and these handlers:

```python
@dataclass(frozen=True, slots=True)
class LeaseMutation:
    workstream_id: str
    expected_state_version: int
    expected_lease_epoch: int
    next_state_version: int
    next_lease_epoch: int
    token_hash: str | None
    writer_hash: str | None
    lease_until: datetime | None


def _claim(
    self,
    request: ContinuityCheckpointRequest,
    lease_session: LeaseSessionPort,
    effects: _CapabilityEffects,
) -> ContinuityMutationResult:
    record = self._resolve_requested_workstream(request)
    self._require_state_version(record, request.expected_state_version)
    self._require_unfinished(record)
    now = self.clock.now()
    if record.lease_until is not None and record.lease_until > now:
        raise ContinuityConflict("lease_held_by_other")
    next_epoch = record.lease_epoch + 1
    grant = effects.issue(lease_session, record.id, next_epoch)
    updated = self.store.cas_lease(
        LeaseMutation(
            workstream_id=record.id,
            expected_state_version=record.state_version,
            expected_lease_epoch=record.lease_epoch,
            next_state_version=record.state_version + 1,
            next_lease_epoch=next_epoch,
            token_hash=grant.token_hash,
            writer_hash=grant.writer_hash,
            lease_until=now + self.bounds.lease_duration,
        )
    )
    return ContinuityMutationResult.for_workstream(updated)


def _renew(
    self,
    request: ContinuityCheckpointRequest,
    lease_session: LeaseSessionPort,
    effects: _CapabilityEffects,
) -> ContinuityMutationResult:
    record = self._resolve_requested_workstream(request)
    self._require_state_version(record, request.expected_state_version)
    self._require_active_owned_lease(record, lease_session)
    next_epoch = record.lease_epoch + 1
    grant = effects.issue(lease_session, record.id, next_epoch)
    updated = self.store.cas_lease(
        LeaseMutation(
            workstream_id=record.id,
            expected_state_version=record.state_version,
            expected_lease_epoch=record.lease_epoch,
            next_state_version=record.state_version + 1,
            next_lease_epoch=next_epoch,
            token_hash=grant.token_hash,
            writer_hash=grant.writer_hash,
            lease_until=self.clock.now() + self.bounds.lease_duration,
        )
    )
    return ContinuityMutationResult.for_workstream(updated)


def _release(
    self,
    request: ContinuityCheckpointRequest,
    lease_session: LeaseSessionPort,
    effects: _CapabilityEffects,
) -> ContinuityMutationResult:
    record = self._resolve_requested_workstream(request)
    self._require_state_version(record, request.expected_state_version)
    self._require_active_owned_lease(record, lease_session)
    updated = self.store.cas_lease(
        LeaseMutation(
            workstream_id=record.id,
            expected_state_version=record.state_version,
            expected_lease_epoch=record.lease_epoch,
            next_state_version=record.state_version + 1,
            next_lease_epoch=record.lease_epoch + 1,
            token_hash=None,
            writer_hash=None,
            lease_until=None,
        )
    )
    effects.discard_after_commit(record.id)
    return ContinuityMutationResult.for_workstream(updated)
```

`ContinuityStore.cas_lease` calls `require_semantic_transaction("cas_lease")`, updates only `state_version`, `lease_epoch`, `lease_token_hash`, `lease_writer_hash`, `lease_until`, and `updated_at`, and requires both expected values in its `WHERE` clause. Every handler runs inside Task 4’s one existing semantic transaction, appends one event, and exposes no capability in `ContinuityMutationResult`. Any content/status handler that clears the lease uses `effects.discard_after_commit(record.id)` after its successful workstream CAS, never a precommit `discard`.

In `MemoryMCPServer.__init__` create one `TransportLeaseSession`; handler calls pass it to the service. In `shutdown` close it before releasing service resources. Capability discovery advertises v1 only when the transport has isolated authenticated session storage.

- [ ] **Step 4: Run lease, MCP lifecycle, and privacy tests and observe green**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_session.py tests/test_continuity_service.py tests/test_mcp_protocol.py
git diff --check
```

Expected: PASS, including exact expiry equality, cross-session denial, shutdown zeroization, issue rollback compensation, post-commit release discard, fail-closed renew failure, and one epoch/journal entry for every lease mutation.

- [ ] **Step 5: Commit Task 5**

Run:

```bash
git add evolvmem/continuity_session.py evolvmem/continuity_models.py evolvmem/continuity_store.py evolvmem/continuity_service.py evolvmem/mcp_server.py tests/test_continuity_session.py tests/test_continuity_service.py tests/test_mcp_protocol.py
git commit -m "feat: bind continuity leases to transport sessions"
```

---

### Task 6: Resume exact checkpoints, confirm focus explicitly, and exclude continuity from ordinary retrieval

**Files:**

- Create: `evolvmem/continuity_renderer.py`
- Create: `evolvmem/continuity_checks.py`
- Create: `tests/test_continuity_resume.py`
- Modify: `evolvmem/continuity_service.py: ContinuityService read/admin symbols`
- Modify: `evolvmem/cutover_models.py: PrimaryGateEvidence and PrimaryGateReport`
- Modify: `evolvmem/cutover_checks.py: ProjectMaintenanceVerifier.verify, collect_primary_gate_evidence, and verify_primary_gate`
- Modify: `evolvmem/maintenance_models.py: MaintenanceVerificationResult`
- Modify: `evolvmem/runtime_contract.py: ContinuityRuntimeContract and build_continuity_runtime_contract`
- Modify: `evolvmem/context_retriever.py:75` (`ContextRetriever`), `evolvmem/context_retriever.py:104` (`search`), and `evolvmem/context_retriever.py:207` (`_eligible`)
- Modify: `evolvmem/context_renderer.py:161` (`ContextRenderer`) and `evolvmem/context_renderer.py:169` (`render`)
- Modify: `evolvmem/context_service.py:509` (`ContextService.session_start`)
- Modify: `tests/test_context_retriever.py:425` (`test_non_active_items_never_reach_results`)
- Modify: `tests/test_context_renderer.py:107` (`test_render_wraps_one_item_in_the_frozen_history_block`)
- Modify: `tests/test_context_service.py:907` (`test_session_start_dedupes_retriever_hits_and_pinned_seeds_by_id`)
- Modify: `tests/test_cutover_checks.py: ProjectMaintenanceVerifier and primary-gate continuity cases`
- Modify: `tests/test_runtime_contract.py: continuity and final-suite readiness cases`
- Modify: `tests/test_maintenance_acceptance.py: installed continuity gate in the owned-temp harness`

**Interfaces:**

- Consumes: exact project/workspace resolution, permanent focus, current workstream/checkpoint exact reads, `RepoAnchorProvider.compare`, Task 5 caller-ownership view, cleanup Task 8 `ProjectMaintenanceVerifier.verify(plan: MaintenancePlan, run_snapshot: MaintenanceRunSnapshot) -> MaintenanceVerificationResult`, and cleanup Task 10 `collect_primary_gate_evidence`/`verify_primary_gate` plus runtime readiness contract.
- Produces: `ContinuityService.status() -> ContinuityStatus`, `resume(request: ContinuityResumeRequest, *, lease_session: LeaseSessionPort | None) -> ContinuityResumeResult`, `list(request: ContinuityListRequest) -> ContinuityListResult`, `admin_list(request: ContinuityAdminListRequest) -> ContinuityAdminListResult`, `admin_get(request: ContinuityAdminGetRequest) -> ContinuityWorkstreamDetail | None`, `ContinuityRenderer.render(result: ContinuityResumeResult, max_chars: int) -> str`, `ContinuityChecks.evaluate(*, allowed_maintenance_run_id: str | None = None) -> ContinuityInvariantResult`, content-free `ContinuityInvariantResult`, the cleanup Task 8 `ProjectMaintenanceVerifier` constructor extended with required keyword-only `continuity_checks: ContinuityChecks`, `PrimaryGateEvidence.continuity_installed`, `PrimaryGateEvidence.continuity_ready`, `PrimaryGateEvidence.continuity_reason_codes`, `PrimaryGateReport.continuity_ready`, `MaintenanceVerificationResult.continuity_ready`, `ContinuityRuntimeContract`, and `build_continuity_runtime_contract(continuity: ContinuityInvariantResult, primary: PrimaryGateReport, maintenance: MaintenanceVerificationResult) -> ContinuityRuntimeContract`.

- [ ] **Step 1: Write failing readiness, resume-order, focus-confirmation, and rendering tests**

Add the exact no-focus confirmation flow:

```python
from evolvmem.continuity_models import (
    ContinuityAction,
    ContinuityCheckpointRequest,
    ContinuityResumeRequest,
)


def test_single_no_focus_candidate_requires_claim_then_cas_switch(
    resume_rig,
) -> None:
    candidate = resume_rig.create_unfocused_open_workstream()
    before_focus = resume_rig.focus()

    result = resume_rig.service.resume(
        ContinuityResumeRequest(workspace_path=str(resume_rig.workspace)),
        lease_session=resume_rig.session,
    )

    assert result.code.value == "needs_focus_confirmation"
    assert result.candidates[0].workstream_id == candidate.id
    assert resume_rig.focus() == before_focus

    claimed = resume_rig.service.checkpoint(
        ContinuityCheckpointRequest(
            action=ContinuityAction.CLAIM,
            workspace_path=str(resume_rig.workspace),
            workstream_id=candidate.id,
            expected_state_version=candidate.state_version,
        ),
        lease_session=resume_rig.session,
    )
    switched = resume_rig.service.checkpoint(
        ContinuityCheckpointRequest(
            action=ContinuityAction.SWITCH_FOCUS,
            workspace_path=str(resume_rig.workspace),
            workstream_id=candidate.id,
            expected_state_version=claimed.state_version,
            expected_focus_revision=before_focus.revision,
        ),
        lease_session=resume_rig.session,
    )

    assert switched.focus_revision == before_focus.revision + 1
    assert resume_rig.focus().workstream_id == candidate.id
```

Add a table for this exact primary order:

1. unresolved/ambiguous project or binding;
2. no focus: one unfinished `needs_focus_confirmation`, multiple `ambiguous`, recovery-only `recovery_confirmation_required`, none `no_continuation`;
3. missing/cross-project/terminal pointer `dangling_focus` or identity/type/L2 mismatch `corrupt_checkpoint`;
4. foreign active lease `lease_held_by_other`, even for paused plus stale repo;
5. fixed primary repo staleness;
6. open, paused, or blocked interpretation.

Test readiness requires registry/continuity schema, safe key, one focus row per active binding, writable data root, no maintenance/rollback, and valid pointer/current Context/identity/L2 invariants. Test bounded `continuity_list` never crosses project/fingerprint or returns L1/L2/sources/lease internals. Under the smallest budget, rendering retains objective, current step, next action, blockers, checkpoint revision, and state version, never L2, and begins with the fixed untrusted-history boundary.

- [ ] **Step 2: Run resume tests and observe red**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_resume.py -k "readiness or resume or focus or budget"
```

Expected: FAIL because exact resume/readiness/renderer methods are absent.

- [ ] **Step 3: Implement readiness, fixed resume order, list, and bounded rendering**

Use explicit early returns in safety order:

```python
def resume(
    self,
    request: ContinuityResumeRequest,
    *,
    lease_session: LeaseSessionPort | None,
) -> ContinuityResumeResult:
    resolution = self._resolve_for_resume(request)
    if not resolution.is_unique:
        return ContinuityResumeResult.project_failure(resolution.reason_code)
    focus = self.store.get_focus(
        resolution.project,
        resolution.workspace_fingerprint,
    )
    if focus.workstream_id is None:
        return self._resume_without_focus(request, resolution, focus)
    integrity = self.checks.validate_pointer(resolution, focus)
    if not integrity.valid:
        return ContinuityResumeResult.integrity_failure(integrity, focus)
    workstream = integrity.workstream
    if self._foreign_active_lease(workstream, lease_session):
        return ContinuityResumeResult.lease_held(workstream, focus)
    current_anchor = self.repo.capture(request.workspace_path)
    staleness = self.repo.compare(workstream.repo_anchor, current_anchor)
    if staleness.primary_code is not RepoStalenessCode.FRESH:
        return ContinuityResumeResult.stale(workstream, focus, staleness)
    return ContinuityResumeResult.for_status(workstream, focus)
```

`_resume_without_focus` reads only bounded metadata/L0 and never mutates. A single unfinished result includes its current state version and focus revision so the caller can perform claim then switch. Paused returns the exact checkpoint but requires claim plus explicit `resume` before execution; blocked returns blockers and permits only verification/unblock; terminal never resumes. `wrong_workspace` suppresses L1/L2.

- [ ] **Step 4: Run resume tests and observe green**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_resume.py tests/test_continuity_service.py
```

Expected: PASS for the complete focus/pointer/lease/repo/status matrix and exact confirmation flow.

- [ ] **Step 5: Write failing maintenance, primary, and runtime continuity-gate tests**

Add an owned-temp gate fixture whose project/migration evidence is otherwise healthy. Cover not-installed schema, unsafe key, missing focus row, dangling focus target, wrong checkpoint identity/type, L2 authority mismatch, unwritable data root, active maintenance/rollback, and the healthy installed case. The corrupt-pointer case must fail all three deployment consumers:

```python
def test_corrupt_checkpoint_blocks_exact_active_maintenance(
    continuity_gate_rig,
) -> None:
    continuity_gate_rig.install_healthy_continuity()
    snapshot = continuity_gate_rig.start_run(
        run_id="run_corrupt",
        stage="vector_synced",
        status="running",
    )
    continuity_gate_rig.corrupt_focused_checkpoint_l2()

    maintenance = continuity_gate_rig.maintenance_verifier.verify(
        continuity_gate_rig.plan,
        snapshot,
    )

    assert maintenance.continuity_ready is False
    assert "continuity_checkpoint_l2_mismatch" in maintenance.reason_codes
    assert "continuity_maintenance_active" not in maintenance.reason_codes


def test_post_run_corruption_blocks_strict_primary_and_runtime(
    continuity_gate_rig,
) -> None:
    continuity_gate_rig.install_healthy_continuity()
    snapshot = continuity_gate_rig.start_run(
        run_id="run_then_corrupt",
        stage="vector_synced",
        status="running",
    )
    maintenance = continuity_gate_rig.maintenance_verifier.verify(
        continuity_gate_rig.plan,
        snapshot,
    )
    assert maintenance.ok is True
    continuity_gate_rig.mark_verified_and_completed(snapshot.run_id)
    continuity_gate_rig.corrupt_focused_checkpoint_l2()

    continuity = continuity_gate_rig.continuity_checks.evaluate()
    evidence = continuity_gate_rig.collect_primary_evidence()
    primary = verify_primary_gate(evidence)
    runtime = build_continuity_runtime_contract(
        continuity,
        primary,
        maintenance,
    )

    assert continuity.ready is False
    assert continuity.reason_codes == ("continuity_checkpoint_l2_mismatch",)
    assert evidence.continuity_installed is True
    assert evidence.continuity_ready is False
    assert primary.ready_primary is False
    assert primary.continuity_ready is False
    assert runtime.continuity_ready is False
    assert runtime.final_suite_ready is False


def test_healthy_installed_continuity_closes_the_not_installed_gate(
    continuity_gate_rig,
) -> None:
    continuity_gate_rig.install_healthy_continuity()
    snapshot = continuity_gate_rig.start_run(
        run_id="run_healthy",
        stage="vector_synced",
        status="running",
    )
    maintenance = continuity_gate_rig.maintenance_verifier.verify(
        continuity_gate_rig.plan,
        snapshot,
    )
    assert maintenance.ok is True
    continuity_gate_rig.mark_verified_and_completed(snapshot.run_id)

    continuity = continuity_gate_rig.continuity_checks.evaluate()
    primary = verify_primary_gate(
        continuity_gate_rig.collect_primary_evidence()
    )
    runtime = build_continuity_runtime_contract(
        continuity,
        primary,
        maintenance,
    )

    assert continuity.ready is True
    assert primary.ready_primary is True
    assert runtime.final_suite_ready is True


def test_verifier_allows_only_its_exact_vector_synced_run(
    continuity_gate_rig,
) -> None:
    continuity_gate_rig.install_healthy_continuity()
    snapshot = continuity_gate_rig.start_run(
        run_id="run_exact",
        stage="vector_synced",
        status="running",
    )

    allowed = continuity_gate_rig.maintenance_verifier.verify(
        continuity_gate_rig.plan,
        snapshot,
    )
    wrong = continuity_gate_rig.continuity_checks.evaluate(
        allowed_maintenance_run_id="run_other"
    )
    strict = continuity_gate_rig.continuity_checks.evaluate()

    assert allowed.continuity_ready is True
    assert wrong.reason_codes == ("continuity_maintenance_active",)
    assert strict.reason_codes == ("continuity_maintenance_active",)

    continuity_gate_rig.mark_rollback_active("run_exact")
    refused = continuity_gate_rig.maintenance_verifier.verify(
        continuity_gate_rig.plan,
        snapshot,
    )
    assert refused.continuity_ready is False
    assert "continuity_maintenance_active" in refused.reason_codes
```

The fixture’s `mark_verified_and_completed(run_id)` calls the cleanup store’s exact verified/completed transition only after an `ok=True` verifier result, then reloads maintenance state and asserts that no incomplete run remains. Never ask strict `evaluate()`, primary collection, or runtime aggregation to pass while the vector-synced/running row exists. Maintenance verification alone passes `allowed_maintenance_run_id` for that exact row; after verified/completed, every deployment consumer calls strict `evaluate()` with no allowance.

In `tests/test_maintenance_acceptance.py`, make the owned-temp fixture install continuity schema plus one permanent focus row for every active binding, then assert its final verification report contains `continuity_ready=True`; deleting one focus row must make the wrapper stop at verification rather than report `verified/completed`.

- [ ] **Step 6: Run the gate tests and observe red**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_resume.py tests/test_cutover_checks.py tests/test_runtime_contract.py tests/test_maintenance_acceptance.py -k "continuity or final_suite_ready"
```

Expected: FAIL because `ContinuityInvariantResult` is not connected to maintenance verification, primary evidence, or the runtime contract; the prerequisite `not_installed` sentinel still survives.

- [ ] **Step 7: Implement one continuity invariant result and wire every deployment consumer**

Define the independent readiness result in `continuity_checks.py`. Its counts and reason codes are content-free:

```python
@dataclass(frozen=True, slots=True)
class ContinuityInvariantResult:
    installed: bool
    ready: bool
    active_bindings: int
    missing_focus_rows: int
    invalid_pointer_rows: int
    corrupt_checkpoint_rows: int
    reason_codes: tuple[str, ...]


def evaluate(
    self,
    *,
    allowed_maintenance_run_id: str | None = None,
) -> ContinuityInvariantResult:
    if not self._schema_installed():
        return ContinuityInvariantResult(
            installed=False,
            ready=False,
            active_bindings=0,
            missing_focus_rows=0,
            invalid_pointer_rows=0,
            corrupt_checkpoint_rows=0,
            reason_codes=("continuity_not_installed",),
        )
    active_bindings = self._active_binding_count()
    missing_focus = self._missing_focus_count()
    invalid_pointers = self._invalid_pointer_count()
    corrupt_checkpoints = self._corrupt_checkpoint_count()
    reasons: list[str] = []
    if not self._registry_schema_current():
        reasons.append("continuity_registry_schema_mismatch")
    if not self._identity_key_safe():
        reasons.append("continuity_identity_key_unsafe")
    if missing_focus:
        reasons.append("continuity_focus_missing")
    if invalid_pointers:
        reasons.append("continuity_pointer_invalid")
    if corrupt_checkpoints:
        reasons.append("continuity_checkpoint_l2_mismatch")
    if not self._data_root_writable():
        reasons.append("continuity_data_root_unwritable")
    if not self._maintenance_state_permits(allowed_maintenance_run_id):
        reasons.append("continuity_maintenance_active")
    ordered = tuple(sorted(set(reasons)))
    return ContinuityInvariantResult(
        installed=True,
        ready=not ordered,
        active_bindings=active_bindings,
        missing_focus_rows=missing_focus,
        invalid_pointer_rows=invalid_pointers,
        corrupt_checkpoint_rows=corrupt_checkpoints,
        reason_codes=ordered,
    )
```

`_missing_focus_count` is a read-only active-binding LEFT JOIN on exact `(project, workspace_fingerprint)`. `_invalid_pointer_count` counts non-NULL focus targets that are missing, cross-project/workspace, or terminal. `_corrupt_checkpoint_count` exact-reads each focused current Context item and requires the stable checkpoint identity, `WORKSTREAM_CHECKPOINT`, active status, current pointer, and decoded L2 workstream/project/fingerprint/checkpoint revision/status equality. The remaining helpers inspect the approved schema version, owner-only HMAC-key mode/fingerprint, configured data-root writability, and persistent maintenance/rollback state without creating anything. `_maintenance_state_permits(None)` requires no incomplete run and no rollback. With a non-NULL ID it permits only one exact row whose run ID matches, `stage="vector_synced"`, `status="running"`, and rollback marker is absent; it rejects another active run, any other stage/status, concurrent rows, or any rollback. Thus ordinary status, primary, and runtime callers stay strict-idle, while the exact apply run can perform its final verification without a circular gate.

Extend the cleanup-produced models with required continuity fields and make the existing collectors/verifiers consume the checker rather than caller-supplied booleans:

Add `continuity_installed: bool = False`, `continuity_ready: bool = False`, and `continuity_reason_codes: tuple[str, ...] = ("continuity_not_installed",)` after the prerequisite `PrimaryGateEvidence` defaults. Add required `continuity_ready: bool` to `PrimaryGateReport` and `MaintenanceVerificationResult`, validate it with the same exact-bool guard, and include it in their content-free public projections. The extracted project-only helpers populate that field from evidence or `False`; only the aggregators below may turn the final verdict true.

```python
def collect_primary_gate_evidence(
    config: Config,
    store: ContextStore,
    *,
    continuity_checks: ContinuityChecks,
    context_vector_index=None,
    second_migration_created: int | None = None,
    fts_only_approved: bool = False,
    shadow_thresholds_met: bool = False,
) -> PrimaryGateEvidence:
    project = _collect_project_primary_evidence(
        config,
        store,
        context_vector_index=context_vector_index,
        second_migration_created=second_migration_created,
        fts_only_approved=fts_only_approved,
        shadow_thresholds_met=shadow_thresholds_met,
    )
    continuity = continuity_checks.evaluate()
    return replace(
        project,
        continuity_installed=continuity.installed,
        continuity_ready=continuity.ready,
        continuity_reason_codes=continuity.reason_codes,
    )


def verify_primary_gate(evidence: PrimaryGateEvidence) -> PrimaryGateReport:
    project = _verify_project_primary_gate(evidence)
    continuity_reasons = (
        evidence.continuity_reason_codes
        if not evidence.continuity_ready
        else ()
    )
    reasons = tuple(sorted(set(project.reason_codes + continuity_reasons)))
    return replace(
        project,
        ready_primary=project.ready_primary and evidence.continuity_ready,
        continuity_ready=evidence.continuity_ready,
        reason_codes=reasons,
    )
```

Extract the pre-existing project-only bodies verbatim into `_collect_project_primary_evidence` and `_verify_project_primary_gate`; do not duplicate or weaken any prerequisite gate. Require `continuity_checks` as a keyword-only argument so a caller cannot silently receive `not_installed` or fabricate readiness.

In `ProjectMaintenanceVerifier.verify`, evaluate continuity beside the existing project checks and merge its stable reasons into the same result:

```python
def verify(
    self,
    plan: MaintenancePlan,
    run_snapshot: MaintenanceRunSnapshot,
) -> MaintenanceVerificationResult:
    project = self._verify_project_invariants(plan, run_snapshot)
    continuity = self._continuity_checks.evaluate(
        allowed_maintenance_run_id=run_snapshot.run_id
    )
    reasons = tuple(
        sorted(
            set(
                project.reason_codes
                + (() if continuity.ready else continuity.reason_codes)
            )
        )
    )
    return replace(
        project,
        ok=project.ok and continuity.ready,
        continuity_ready=continuity.ready,
        reason_codes=reasons,
    )
```

The constructor requires `continuity_checks`; `_verify_project_invariants` is the exact prior `verify` body extracted without semantic changes. Finally replace the cleanup-plan `not_installed` runtime sentinel with a typed aggregation:

```python
@dataclass(frozen=True, slots=True)
class ContinuityRuntimeContract:
    contract_version: str
    continuity_installed: bool
    continuity_ready: bool
    final_suite_ready: bool
    reason_codes: tuple[str, ...]


def build_continuity_runtime_contract(
    continuity: ContinuityInvariantResult,
    primary: PrimaryGateReport,
    maintenance: MaintenanceVerificationResult,
) -> ContinuityRuntimeContract:
    reasons = tuple(
        sorted(
            set(
                continuity.reason_codes
                + primary.reason_codes
                + maintenance.reason_codes
            )
        )
    )
    return ContinuityRuntimeContract(
        contract_version="evolvmem.continuity.v1",
        continuity_installed=continuity.installed,
        continuity_ready=continuity.ready,
        final_suite_ready=(
            continuity.ready and primary.ready_primary and maintenance.ok
        ),
        reason_codes=reasons,
    )
```

- [ ] **Step 8: Run maintenance, primary, runtime, and owned-temp gates and observe green**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_resume.py tests/test_cutover_checks.py tests/test_runtime_contract.py tests/test_maintenance_acceptance.py
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/python scripts/accept_project_continuity_migration.py
```

Expected: PASS; healthy installed continuity replaces `not_installed`, while any missing focus, bad pointer/L2, unsafe key, unwritable root, or maintenance state prevents both `verified/completed` and `final_suite_ready=true`. This committed wiring is mandatory input to Tasks 11–13.

- [ ] **Step 9: Write failing ordinary-retrieval exclusion tests**

Insert both continuity content types with FTS and vector scores higher than a normal item:

```python
def test_default_retrieval_omits_checkpoint_and_recovery_even_when_ranked_first(
    store,
    retriever,
    context_item_factory,
) -> None:
    checkpoint = context_item_factory(
        content_type="workstream_checkpoint",
        identity_key="project:evolvmem:workstream:ws_unit:checkpoint",
        l0="unique-continuity-hit",
    )
    recovery = context_item_factory(
        content_type="workstream_recovery",
        identity_key="project:evolvmem:workstream-recovery:rc_unit",
        l0="unique-continuity-hit",
    )
    ordinary = context_item_factory(
        content_type="fact",
        identity_key="project:evolvmem:fact:ordinary",
        l0="unique-continuity-hit",
    )

    results = retriever.search(
        ContextSearchRequest(
            query="unique-continuity-hit",
            project="evolvmem",
            top_k=10,
        )
    )

    assert [item.id for item in results] == [ordinary.id]
    assert checkpoint.id not in [item.id for item in results]
    assert recovery.id not in [item.id for item in results]
```

Add corresponding session-injection/renderer tests and explicit admin content-type opt-in tests.

- [ ] **Step 10: Run exclusion tests and observe red**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_retriever.py tests/test_context_renderer.py tests/test_context_service.py -k "workstream or recovery"
```

Expected: FAIL because default eligibility still admits the new active content types.

- [ ] **Step 11: Implement exclusion at candidate eligibility and session injection**

Define one immutable exclusion set shared by lexical, vector, pinned-seed, and renderer eligibility:

```python
_AUTOMATICALLY_EXCLUDED_TYPES = frozenset(
    {
        ContextContentType.WORKSTREAM_CHECKPOINT,
        ContextContentType.WORKSTREAM_RECOVERY,
    }
)


def _eligible(
    self,
    record: ContextRetrievalRecord,
    request: ContextSearchRequest,
) -> bool:
    item = record.item
    if item.content_type in _AUTOMATICALLY_EXCLUDED_TYPES:
        return False
    return self._ordinary_eligibility(record, request)
```

An explicit admin content-type path bypasses this default only after the admin boundary has authorized L0 metadata. It must not change ordinary `ContextSearchRequest` behavior.

- [ ] **Step 12: Run resume/retrieval integration tests and observe green**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_resume.py tests/test_continuity_service.py tests/test_context_retriever.py tests/test_context_renderer.py tests/test_context_service.py
git diff --check
```

Expected: PASS; spies confirm exact resume performs zero lexical/vector search and ordinary search/injection never returns continuity content.

- [ ] **Step 13: Commit Task 6**

Run:

```bash
git add evolvmem/continuity_renderer.py evolvmem/continuity_checks.py evolvmem/continuity_service.py evolvmem/cutover_models.py evolvmem/cutover_checks.py evolvmem/maintenance_models.py evolvmem/runtime_contract.py evolvmem/context_retriever.py evolvmem/context_renderer.py evolvmem/context_service.py tests/test_continuity_resume.py tests/test_cutover_checks.py tests/test_runtime_contract.py tests/test_maintenance_acceptance.py tests/test_context_retriever.py tests/test_context_renderer.py tests/test_context_service.py
git commit -m "feat: resume exact continuity checkpoints"
```

---

### Task 7: Recover interrupted sessions without overwriting authority

**Files:**

- Create: `evolvmem/continuity_recovery.py`
- Create: `tests/test_continuity_recovery_candidates.py`
- Modify: `evolvmem/continuity_models.py: RecoveryCandidateRecord and ContinuityEvent recovery factory symbols`
- Modify: `evolvmem/continuity_store.py: recovery candidate repository symbols`
- Modify: `evolvmem/continuity_service.py: recovery action handlers`
- Modify: `evolvmem/mutation_state.py: SEMANTIC_PROJECTIONS recovery entry`
- Modify: `evolvmem/session_archive.py:91` (`SessionArchiver`), `evolvmem/session_archive.py:108` (`archive_session`), and `evolvmem/session_archive.py:210` (`sweep_expired`)
- Modify: `evolvmem/hooks.py:295` (`get_session_start_block` and session-end integration in the same module)
- Modify: `evolvmem/kimi_hooks.py:580` (`session_end`)
- Modify: `evolvmem/dsh_bridge.py:49` (`extract_from_messages`)
- Modify: `scripts/extract_stale_sessions.py:108` (`process_batch`)
- Modify: `tests/test_session_archive.py:189` (`test_sweep_purges_exactly_expired_archive_and_keeps_unexpired`)
- Modify: `tests/test_hooks.py:606` (`test_shadow_sweeps_before_maintenance_and_render`)
- Modify: `tests/test_kimi_hooks.py:1736` (`test_session_end_archives_payload_and_links_sources`)
- Modify: `tests/test_dsh_bridge.py:70` (`test_happy_path_persists_summary_and_atomic`)
- Modify: `tests/test_extract_stale_sessions.py: existing stale-session batch tests`
- Modify: `tests/test_mutation_state.py: recovery direct-write drift regression`

**Interfaces:**

- Consumes: encrypted `SessionArchiveRecord` metadata, exact project/fingerprint, Task 4 `_insert_initial_checkpoint_in_transaction`/`_write_checkpoint_in_transaction` helpers and `_CapabilityEffects`, Task 5 lease session, Task 6 recovery-only resume branch, and existing normal session-end `CheckpointPersistenceOutcome` signals.
- Produces: `RecoveryCandidateRecord`, `RecoveryCandidateMutation`, `CheckpointPersistenceOutcome`, `RecoveryFallbackResult`, `ContinuityEvent.for_recovery(action: str, candidate: RecoveryCandidateRecord) -> ContinuityEvent`, `ContinuityEvent.for_recovery_expiry(records: Sequence[RecoveryCandidateRecord], at: datetime) -> ContinuityEvent`, `ContinuityStore.get_recovery_candidate(candidate_id: str) -> RecoveryCandidateRecord | None`, `ContinuityStore.cas_recovery_candidate(mutation: RecoveryCandidateMutation) -> RecoveryCandidateRecord`, `ContinuityRecoveryService.create_candidate(request: RecoveryCandidateRequest) -> RecoveryCandidateResult`, `expire_candidates(*, now: datetime) -> RecoveryExpiryResult`, `RecoveryFallbackCoordinator.record(outcome: CheckpointPersistenceOutcome, archive: SessionArchiveRecord) -> RecoveryFallbackResult`, complete Task 4 `_accept_recovery_new`, `_accept_recovery_existing`, and `_reject_recovery` handlers, and complete `SEMANTIC_PROJECTIONS["continuity_recovery_candidates"]`.

- [ ] **Step 1: Write failing candidate schema, fallback-generation, and expiry tests**

Add:

```python
from datetime import timedelta

from evolvmem.mutation_state import SEMANTIC_PROJECTIONS


def test_recovery_candidate_is_bounded_nonfocus_and_expires_in_seven_days(
    recovery_rig,
) -> None:
    before_focus = recovery_rig.focus()
    before_epoch = recovery_rig.context_store.current_mutation_epoch()
    created = recovery_rig.service.create_candidate(
        recovery_rig.candidate_request(archive_id=41)
    )

    assert created.status.value == "candidate"
    assert created.expires_at == recovery_rig.now + timedelta(days=7)
    assert created.identity_key == (
        "project:evolvmem:workstream-recovery:" + created.candidate_id
    )
    assert recovery_rig.focus() == before_focus
    assert recovery_rig.context_store.current_mutation_epoch() == before_epoch + 1
    assert recovery_rig.ordinary_search(created.l0) == ()

    expired = recovery_rig.service.expire_candidates(
        now=created.expires_at
    )

    assert expired.closed_ids == (created.candidate_id,)
    assert recovery_rig.focus() == before_focus
    assert recovery_rig.context_store.current_mutation_epoch() == before_epoch + 2


def test_recovery_table_is_semantic_and_direct_bypass_is_detected(
    recovery_rig,
) -> None:
    assert SEMANTIC_PROJECTIONS["continuity_recovery_candidates"] == (
        "id", "project", "workspace_fingerprint", "archive_id",
        "candidate_context_id", "proposed_workstream_id", "status",
        "revision", "expires_at", "created_at", "updated_at",
    )
    created = recovery_rig.create_candidate()
    expected_epoch = recovery_rig.context_store.current_mutation_epoch()
    expected_digest = recovery_rig.context_store.canonical_state_digest()

    recovery_rig.context_store._connection().execute(
        "UPDATE continuity_recovery_candidates SET revision=revision+1 WHERE id=?",
        (created.id,),
    )
    recovery_rig.context_store._connection().commit()

    report = recovery_rig.context_store.detect_unjournaled_mutation(
        expected_epoch,
        expected_digest,
    )
    assert report.reason_codes == ("semantic_digest_changed_without_epoch",)
```

Assert schema columns exactly `id/project/workspace_fingerprint/archive_id/candidate_context_id/proposed_workstream_id/status/revision/expires_at/created_at/updated_at`. Generation occurs only when normal authoritative checkpoint persistence did not succeed, reads structured encrypted-archive metadata rather than raw transcript text, creates a bounded candidate Context item, and never changes workstream or focus. Equality with expiry time closes the candidate.

- [ ] **Step 2: Run candidate-generation tests and observe red**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_recovery_candidates.py -k "schema or generate or expiry"
```

Expected: FAIL because the recovery table, semantic projection, and service are absent.

- [ ] **Step 3: Implement candidate storage, fallback generation, and expiry**

```python
class ContinuityRecoveryService:
    def __init__(
        self,
        context_store: ContextStore,
        continuity_store: ContinuityStore,
        clock: Clock,
    ) -> None:
        self.context_store = context_store
        self.store = continuity_store
        self.clock = clock

    def create_candidate(
        self,
        request: RecoveryCandidateRequest,
    ) -> RecoveryCandidateResult:
        with self.context_store.semantic_transaction(
            "continuity:recovery_create"
        ):
            candidate = RecoveryCandidateRecord.from_request(
                request,
                created_at=self.clock.now(),
                expires_at=self.clock.now() + timedelta(days=7),
            )
            context_id = self.store.insert_recovery_context(candidate)
            stored = self.store.insert_recovery_candidate(
                candidate.with_context_id(context_id)
            )
            self.store.append_event(
                ContinuityEvent.for_recovery("recovery_create", stored)
            )
            return RecoveryCandidateResult.from_record(stored)

    def expire_candidates(self, *, now: datetime) -> RecoveryExpiryResult:
        due_ids = self.store.list_due_candidate_ids(now=now)
        if not due_ids:
            return RecoveryExpiryResult(closed_ids=())
        with self.context_store.semantic_transaction(
            "continuity:recovery_expire"
        ):
            closed = self.store.expire_candidates(
                candidate_ids=due_ids,
                now=now,
            )
            self.store.append_event(
                ContinuityEvent.for_recovery_expiry(closed, now)
            )
            return RecoveryExpiryResult.from_records(closed)
```

Add the table to the semantic digest in the same commit as its DDL:

```python
SEMANTIC_PROJECTIONS["continuity_recovery_candidates"] = (
    "id", "project", "workspace_fingerprint", "archive_id",
    "candidate_context_id", "proposed_workstream_id", "status", "revision",
    "expires_at", "created_at", "updated_at",
)
```

If no candidate is due, expiry is a read-only no-op and must not open a semantic mutation transaction. Hooks call `create_candidate` only after receiving the explicit result that no normal checkpoint was saved; successful normal checkpointing suppresses fallback creation.

- [ ] **Step 4: Run candidate-generation tests and observe green**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_recovery_candidates.py tests/test_mutation_state.py -k "recovery or generate or expiry"
```

Expected: PASS with one epoch/journal/event for each state-changing create/expiry operation and no focus mutation.

- [ ] **Step 5: Write failing accept-new, accept-existing, reject, and rollback tests**

Add an explicit non-focus accept-new test:

```python
import pytest

from evolvmem.continuity_models import (
    ContinuityAction,
    ContinuityCheckpointRequest,
)
from evolvmem.continuity_recovery import CheckpointPersistenceOutcome


def test_accept_recovery_new_without_make_focus_leaves_existing_focus(
    recovery_rig,
) -> None:
    existing_focus = recovery_rig.create_focused_open_workstream()
    candidate = recovery_rig.create_candidate()
    before_epoch = recovery_rig.context_store.current_mutation_epoch()

    accepted = recovery_rig.continuity_service.checkpoint(
        ContinuityCheckpointRequest(
            action=ContinuityAction.ACCEPT_RECOVERY_NEW,
            workspace_path=str(recovery_rig.workspace),
            recovery_candidate_id=candidate.id,
            expected_candidate_revision=candidate.revision,
            expected_focus_revision=existing_focus.focus_revision,
            make_focus=False,
            payload=recovery_rig.payload_from_candidate(candidate),
        ),
        lease_session=recovery_rig.session,
    )

    assert accepted.status.value == "open"
    assert accepted.checkpoint_revision == 1
    assert accepted.state_version == 1
    assert recovery_rig.focus().workstream_id == existing_focus.workstream_id
    assert recovery_rig.candidate(candidate.id).status.value == "accepted"
    assert recovery_rig.context_store.current_mutation_epoch() == before_epoch + 1


def test_accept_recovery_new_rollback_discards_issued_capability(
    recovery_rig,
) -> None:
    candidate = recovery_rig.create_candidate()
    expected_workstream_id = recovery_rig.peek_next_workstream_id()
    recovery_rig.inject_failure_after("candidate_close")

    with pytest.raises(RuntimeError, match="injected_after_candidate_close"):
        recovery_rig.accept_new(candidate, make_focus=False)

    assert recovery_rig.session.capability_for(expected_workstream_id) is None
    assert recovery_rig.session.discard_calls == (expected_workstream_id,)
    assert recovery_rig.workstream(expected_workstream_id) is None
    assert recovery_rig.candidate(candidate.id) == candidate


def test_accept_recovery_existing_preserves_workflow_status(
    recovery_rig,
) -> None:
    target = recovery_rig.create_owned_open_workstream(make_focus=False)
    candidate = recovery_rig.create_candidate()

    accepted = recovery_rig.accept_existing(
        candidate,
        target=target,
        make_focus=False,
    )

    assert accepted.status == target.status
    assert accepted.checkpoint_revision == target.checkpoint_revision + 1
    assert accepted.state_version == target.state_version + 1
    assert accepted.lease_epoch == target.lease_epoch
    assert recovery_rig.focus().workstream_id is None
    assert recovery_rig.candidate(candidate.id).status.value == "accepted"


def test_write_permission_denial_reports_unsaved_and_preserves_authority(
    recovery_rig,
) -> None:
    authoritative = recovery_rig.create_focused_open_workstream()
    outcome = CheckpointPersistenceOutcome.not_saved(
        code="checkpoint_not_saved",
        old_checkpoint_authoritative=True,
    )
    recovery_rig.deny_database_writes()

    result = recovery_rig.fallback.record(
        outcome,
        recovery_rig.archive_metadata(),
    )

    assert result.code == "checkpoint_not_saved"
    assert result.old_checkpoint_authoritative is True
    assert result.candidate_saved is False
    assert recovery_rig.workstream(authoritative.workstream_id) == authoritative
    assert recovery_rig.candidate_count() == 0
```

Test `accept_recovery_existing` requires candidate revision, target checkpoint/state revision, and active target capability, writes a new checkpoint, then closes the candidate. Optional focus needs focus CAS. `reject_recovery` requires only candidate revision and does not read/change focus. For accept-new, accept-existing, reject, and expiry, assert exactly one semantic epoch/journal/event on success. Stale/cross-project/expired candidate, foreign lease, source failure, capability issue followed by candidate-close failure, event failure, immediately-before-outer-commit failure, a commit fault first proven nondurable, and focus conflict roll back candidate, checkpoint, focus, event, epoch, and journal together. Never assert rollback after a durable commit. Accept-new uses Task 4 issue compensation; accept-existing reuses the already-owned capability and never issues a replacement secret.

- [ ] **Step 6: Run accept/reject tests and observe red**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_recovery_candidates.py -k "accept or reject or conflict or epoch"
```

Expected: FAIL because the recovery action handlers are not implemented.

- [ ] **Step 7: Implement all three recovery action transactions and hook integration**

Register recovery handlers in Task 4’s single dispatcher. The two shared checkpoint helpers neither open a transaction nor append an event; the outer dispatcher owns the one epoch, journal row, and event. Implement the exact candidate CAS and all handlers:

```python
@dataclass(frozen=True, slots=True)
class RecoveryCandidateMutation:
    candidate_id: str
    expected_revision: int
    expected_status: RecoveryCandidateStatus
    next_status: RecoveryCandidateStatus
    proposed_workstream_id: str | None


def _require_open_recovery_candidate(
    self,
    request: ContinuityCheckpointRequest,
    resolution: WorkspaceProjectResolution,
) -> RecoveryCandidateRecord:
    candidate = self.store.get_recovery_candidate(
        request.recovery_candidate_id
    )
    if candidate is None:
        raise ContinuityConflict("recovery_candidate_missing")
    if (
        candidate.project != resolution.project
        or candidate.workspace_fingerprint != resolution.workspace_fingerprint
    ):
        raise ContinuityConflict("recovery_candidate_wrong_workspace")
    if candidate.status is not RecoveryCandidateStatus.CANDIDATE:
        raise ContinuityConflict("recovery_candidate_closed")
    if candidate.revision != request.expected_candidate_revision:
        raise ContinuityConflict("recovery_candidate_revision_conflict")
    if candidate.expires_at <= self.clock.now():
        raise ContinuityConflict("recovery_candidate_expired")
    self._require_recovery_context_integrity(candidate)
    return candidate


def _checkpoint_target_status(
    action: ContinuityAction,
    current: WorkstreamStatus,
) -> WorkstreamStatus:
    if action is ContinuityAction.ACCEPT_RECOVERY_EXISTING:
        return current
    try:
        return _WORKFLOW_TRANSITIONS[(current, action)]
    except KeyError as exc:
        raise ContinuityConflict("workflow_transition_not_allowed") from exc


def _accept_recovery_new(
    self,
    request: ContinuityCheckpointRequest,
    lease_session: LeaseSessionPort,
    effects: _CapabilityEffects,
) -> ContinuityMutationResult:
    resolution = self._resolve_workspace(
        request.workspace_path,
        request.project_hint,
    )
    candidate = self._require_open_recovery_candidate(request, resolution)
    if request.payload is None:
        raise ContinuityValidationError("payload is required")
    payload = validate_checkpoint_payload(request.payload, self.bounds)
    source_ids = tuple(
        dict.fromkeys(
            payload.source_context_ids + (candidate.candidate_context_id,)
        )
    )
    workstream_id = (
        candidate.proposed_workstream_id or self.ids.new_workstream_id()
    )
    created = self._insert_initial_checkpoint_in_transaction(
        request=request,
        resolution=resolution,
        payload=payload,
        source_context_ids=source_ids,
        lease_session=lease_session,
        effects=effects,
        workstream_id=workstream_id,
    )
    closed = self.store.cas_recovery_candidate(
        RecoveryCandidateMutation(
            candidate_id=candidate.id,
            expected_revision=candidate.revision,
            expected_status=RecoveryCandidateStatus.CANDIDATE,
            next_status=RecoveryCandidateStatus.ACCEPTED,
            proposed_workstream_id=workstream_id,
        )
    )
    if request.make_focus:
        focus = self.store.get_focus(
            resolution.project,
            resolution.workspace_fingerprint,
        )
        updated_focus = self.store.cas_focus(
            FocusMutation(
                project=resolution.project,
                workspace_fingerprint=resolution.workspace_fingerprint,
                expected_revision=request.expected_focus_revision,
                expected_workstream_id=focus.workstream_id,
                next_workstream_id=workstream_id,
            )
        )
        created = created.with_focus_revision(updated_focus.revision)
    return created.with_recovery_candidate(closed.id, closed.revision)


def _accept_recovery_existing(
    self,
    request: ContinuityCheckpointRequest,
    lease_session: LeaseSessionPort,
    effects: _CapabilityEffects,
) -> ContinuityMutationResult:
    del effects
    resolution = self._resolve_workspace(
        request.workspace_path,
        request.project_hint,
    )
    candidate = self._require_open_recovery_candidate(request, resolution)
    target = self._require_unfinished_workstream(
        request.workstream_id,
        resolution,
    )
    self._require_checkpoint_revision(
        target,
        request.expected_checkpoint_revision,
    )
    self._require_state_version(target, request.expected_state_version)
    self._require_active_owned_lease(target, lease_session)
    if request.payload is None:
        raise ContinuityValidationError("payload is required")
    payload = validate_checkpoint_payload(request.payload, self.bounds)
    source_ids = tuple(
        dict.fromkeys(
            payload.source_context_ids + (candidate.candidate_context_id,)
        )
    )
    updated = self._write_checkpoint_in_transaction(
        request=request,
        resolution=resolution,
        target=target,
        payload=payload,
        source_context_ids=source_ids,
        lease_session=lease_session,
    )
    closed = self.store.cas_recovery_candidate(
        RecoveryCandidateMutation(
            candidate_id=candidate.id,
            expected_revision=candidate.revision,
            expected_status=RecoveryCandidateStatus.CANDIDATE,
            next_status=RecoveryCandidateStatus.ACCEPTED,
            proposed_workstream_id=target.id,
        )
    )
    if request.make_focus:
        focus = self.store.get_focus(
            resolution.project,
            resolution.workspace_fingerprint,
        )
        updated_focus = self.store.cas_focus(
            FocusMutation(
                project=resolution.project,
                workspace_fingerprint=resolution.workspace_fingerprint,
                expected_revision=request.expected_focus_revision,
                expected_workstream_id=focus.workstream_id,
                next_workstream_id=target.id,
            )
        )
        updated = updated.with_focus_revision(updated_focus.revision)
    return updated.with_recovery_candidate(closed.id, closed.revision)


def _reject_recovery(
    self,
    request: ContinuityCheckpointRequest,
    lease_session: LeaseSessionPort,
    effects: _CapabilityEffects,
) -> ContinuityMutationResult:
    del lease_session, effects
    resolution = self._resolve_workspace(
        request.workspace_path,
        request.project_hint,
    )
    candidate = self._require_open_recovery_candidate(request, resolution)
    closed = self.store.cas_recovery_candidate(
        RecoveryCandidateMutation(
            candidate_id=candidate.id,
            expected_revision=candidate.revision,
            expected_status=RecoveryCandidateStatus.CANDIDATE,
            next_status=RecoveryCandidateStatus.REJECTED,
            proposed_workstream_id=candidate.proposed_workstream_id,
        )
    )
    return ContinuityMutationResult.for_recovery(closed)
```

`ContinuityStore.cas_recovery_candidate` calls `require_semantic_transaction("cas_recovery_candidate")`, updates status/revision/proposed workstream/updated time with `WHERE id=? AND revision=? AND status=?`, and requires rowcount 1. `_insert_initial_checkpoint_in_transaction` issues epoch-1 capability through `effects.issue`; therefore an accept-new failure at any later boundary triggers Task 4 rollback discard. `_write_checkpoint_in_transaction` calls `_checkpoint_target_status(request.action, target.status)`, so `ACCEPT_RECOVERY_EXISTING` is an explicit update-like action: it preserves workflow status and lease epoch, advances checkpoint revision and state version exactly once, validates the current capability, and never calls `issue`. It must neither be rejected as an unknown transition nor silently resume, unblock, pause, or terminate the target. Both acceptance handlers add the candidate Context ID as a relational `context_reference` source. Reject never reads focus. The dispatcher emits exactly one recovery action event after the handler returns.

Represent adapter persistence truth explicitly and centralize fallback behavior:

```python
@dataclass(frozen=True, slots=True)
class CheckpointPersistenceOutcome:
    checkpoint_saved: bool
    code: str
    old_checkpoint_authoritative: bool

    @classmethod
    def not_saved(
        cls,
        *,
        code: str,
        old_checkpoint_authoritative: bool,
    ) -> "CheckpointPersistenceOutcome":
        if code != "checkpoint_not_saved":
            raise ValueError("not-saved outcome requires checkpoint_not_saved")
        return cls(
            checkpoint_saved=False,
            code=code,
            old_checkpoint_authoritative=old_checkpoint_authoritative,
        )


@dataclass(frozen=True, slots=True)
class RecoveryFallbackResult:
    code: str
    candidate_saved: bool
    candidate_id: str | None
    old_checkpoint_authoritative: bool


def _is_write_permission_denial(exc: BaseException) -> bool:
    sqlite_code = getattr(exc, "sqlite_errorcode", None)
    return isinstance(exc, PermissionError) or sqlite_code in {
        sqlite3.SQLITE_PERM,
        sqlite3.SQLITE_READONLY,
        sqlite3.SQLITE_CANTOPEN,
    }


class RecoveryFallbackCoordinator:
    def __init__(self, service: ContinuityRecoveryService) -> None:
        self.service = service

    def record(
        self,
        outcome: CheckpointPersistenceOutcome,
        archive: SessionArchiveRecord,
    ) -> RecoveryFallbackResult:
        if outcome.checkpoint_saved:
            return RecoveryFallbackResult(
                code="checkpoint_saved",
                candidate_saved=False,
                candidate_id=None,
                old_checkpoint_authoritative=False,
            )
        if outcome.code != "checkpoint_not_saved":
            raise ValueError("unsupported checkpoint persistence outcome")
        request = RecoveryCandidateRequest.from_archive_metadata(archive)
        try:
            created = self.service.create_candidate(request)
        except (PermissionError, sqlite3.OperationalError) as exc:
            if not _is_write_permission_denial(exc):
                raise
            return RecoveryFallbackResult(
                code="checkpoint_not_saved",
                candidate_saved=False,
                candidate_id=None,
                old_checkpoint_authoritative=True,
            )
        return RecoveryFallbackResult(
            code="recovery_candidate_saved",
            candidate_saved=True,
            candidate_id=created.candidate_id,
            old_checkpoint_authoritative=outcome.old_checkpoint_authoritative,
        )
```

In `SessionArchiver.archive_session`, `hooks` session-end, `kimi_hooks.session_end`, `dsh_bridge.extract_from_messages`, and `scripts/extract_stale_sessions.process_batch`, construct `CheckpointPersistenceOutcome` from the normal typed checkpoint call, archive first through the existing encrypted archive path, then call `fallback.record(outcome, archive_record)` only when `checkpoint_saved` is false. Pass only `SessionArchiveRecord` metadata into `RecoveryCandidateRequest.from_archive_metadata`; never decrypt or copy transcript text into the coordinator. Each adapter maps `checkpoint_not_saved` to the literal user-facing statement “checkpoint_not_saved; the old checkpoint remains authoritative” and maps `recovery_candidate_saved` separately; it never claims the authoritative checkpoint was written. A permission-denied candidate write returns `candidate_saved=False` without a second success message.

Do not create, replace, import, or stage `evolvmem/session_miner.py`. Wire only `continuity_recovery.py` into archive/hooks/Kimi/DSH/stale-session paths and preserve all current skill-mining/session-archive behavior.

- [ ] **Step 8: Run recovery and adapter integration tests and observe green**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_recovery_candidates.py tests/test_session_archive.py tests/test_hooks.py tests/test_kimi_hooks.py tests/test_dsh_bridge.py tests/test_extract_stale_sessions.py tests/test_production_write_boundaries.py
git diff --check
```

Expected: PASS; every handler is exercised, no fallback candidate overwrites authority or focus, permission denial reports the old checkpoint as authoritative, accept-new rollback leaves no capability, and every successful recovery mutation owns exactly one epoch/journal/event entry.

- [ ] **Step 9: Commit Task 7**

Run:

```bash
git add evolvmem/continuity_recovery.py evolvmem/continuity_models.py evolvmem/continuity_store.py evolvmem/continuity_service.py evolvmem/mutation_state.py evolvmem/session_archive.py evolvmem/hooks.py evolvmem/kimi_hooks.py evolvmem/dsh_bridge.py scripts/extract_stale_sessions.py tests/test_continuity_recovery_candidates.py tests/test_mutation_state.py tests/test_session_archive.py tests/test_hooks.py tests/test_kimi_hooks.py tests/test_dsh_bridge.py tests/test_extract_stale_sessions.py tests/test_production_write_boundaries.py
git commit -m "feat: recover interrupted workstreams safely"
```

---

### Task 8: Expose one adapter-independent MCP continuity contract

**Files:**

- Create: `evolvmem/continuation_intent.py`
- Create: `tests/test_continuation_intent.py`
- Modify: `evolvmem/mcp_contract.py:73` (`McpToolSpec`), `evolvmem/mcp_contract.py:432` (`tool_specs`), and `evolvmem/mcp_contract.py:461` (`initialization_instructions`)
- Modify: `evolvmem/mcp_server.py:241` (`_tool_handlers`), `evolvmem/mcp_server.py:618` (`_context_gate_error`), `evolvmem/mcp_server.py:631` (`_context_session_start`), and `evolvmem/mcp_server.py:1208` (`_handle_request`)
- Modify: `evolvmem/project_cli.py: continuity-enabled ProjectStore composition root`
- Modify: `evolvmem/context_models.py:372` (`ContextSessionStartRequest`)
- Modify: `evolvmem/context_service.py:509` (`ContextService.session_start`)
- Modify: `evolvmem/hooks.py:174` (`_core_session_start_block`)
- Modify: `evolvmem/kimi_hooks.py:159` (`session_start`)
- Modify: `evolvmem/dsh_bridge.py:34` (`inject`)
- Modify: `tests/test_mcp_protocol.py:615` (`test_tools_call_shares_the_tools_list_registry`), `tests/test_mcp_protocol.py:675` (`test_context_tool_schemas_are_frozen`), and `tests/test_mcp_protocol.py:925` (`test_session_start_failure_fails_open_without_legacy_fallback`)
- Modify: `tests/test_integration.py:765` (`test_mcp_tool_schemas_match_design`)
- Modify: `tests/test_context_service.py:907` (session-start integration)
- Modify: `tests/test_project_cli.py: continuity binding-lifecycle composition test`
- Modify: `tests/test_hooks.py:361`, `tests/test_kimi_hooks.py:159`-adjacent session-start tests, and `tests/test_dsh_bridge.py:44`-adjacent inject tests

**Interfaces:**

- Consumes: Task 6 status/resume/list, Task 4/5 checkpoint plus server-owned session, Task 2 `build_continuity_project_store(context_store: ContextStore) -> tuple[ProjectStore, ContinuityStore]`, current MCP registry, and current ordinary `ContextService.session_start`.
- Produces: `ContinuationIntentDetector.classify(text: str) -> ContinuationIntent`; MCP tools `continuity_resume`, `continuity_list`, `continuity_checkpoint`; capability `evolvmem.continuity.v1`; and extended `ContextSessionStartRequest(project: str, query: str, max_chars: int | None = None, workspace_path: str = "", project_hint: str = "")`.

- [ ] **Step 1: Write failing pure intent tests**

```python
import pytest

from evolvmem.continuation_intent import (
    ContinuationIntent,
    ContinuationIntentDetector,
)


@pytest.mark.parametrize(
    "text",
    [
        "继续原任务",
        "继续之前的任务",
        "接着做",
        "从断点继续",
        "继续上次工作",
        "resume previous task",
        "continue previous task",
        "pick up where we left off",
    ],
)
def test_exact_control_phrases_are_continuation(text: str) -> None:
    assert (
        ContinuationIntentDetector().classify(text)
        is ContinuationIntent.CONTINUE
    )


@pytest.mark.parametrize(
    "text",
    [
        "不要继续原任务",
        "请解释字符串“继续原任务”",
        "继续原任务之外，请改做 X",
        "Continue previous task, then replace it with X",
    ],
)
def test_negation_quote_and_explicit_new_goal_are_not_continuation(
    text: str,
) -> None:
    assert (
        ContinuationIntentDetector().classify(text)
        is ContinuationIntent.NEW_GOAL
    )
```

- [ ] **Step 2: Run intent tests and observe red**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuation_intent.py
```

Expected: FAIL because the detector is absent.

- [ ] **Step 3: Implement the pure exact-phrase detector**

```python
class ContinuationIntent(str, Enum):
    CONTINUE = "continue"
    NEW_GOAL = "new_goal"


_CONTROL_PHRASES = frozenset(
    {
        "继续原任务",
        "继续之前的任务",
        "接着做",
        "从断点继续",
        "继续上次工作",
        "resume previous task",
        "continue previous task",
        "pick up where we left off",
    }
)


class ContinuationIntentDetector:
    def classify(self, text: str) -> ContinuationIntent:
        normalized = " ".join(
            unicodedata.normalize("NFKC", text).casefold().split()
        )
        if normalized in _CONTROL_PHRASES:
            return ContinuationIntent.CONTINUE
        return ContinuationIntent.NEW_GOAL
```

The detector returns only a control classification, never a rewritten retrieval query.

- [ ] **Step 4: Run intent tests and observe green**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuation_intent.py
```

Expected: PASS.

- [ ] **Step 5: Write failing MCP schema, gate-order, focus, and write-denial tests**

Add three schema assertions: resume/list are `readOnlyHint=true`; checkpoint has no read-only annotation. Every request requires transient `workspace_path`, allows optional `project_hint`, and exposes public CAS fields but no token/capability/writer hash.

Add these behavior tests:

```python
def test_explicit_new_goal_leaves_existing_focus_untouched(
    continuity_server_rig,
) -> None:
    original = continuity_server_rig.create_focused_workstream()

    result = continuity_server_rig.server._context_session_start(
        {
            "project": "evolvmem",
            "project_hint": "evolvmem",
            "workspace_path": str(continuity_server_rig.workspace),
            "query": "继续原任务之外，请改做 X",
        }
    )

    assert result["intent"] == "new_goal"
    assert continuity_server_rig.ordinary_session_calls == 1
    assert continuity_server_rig.continuity_resume_calls == 0
    assert continuity_server_rig.focus().workstream_id == original.workstream_id


def test_checkpoint_permission_denial_reports_unsaved_and_old_authority(
    continuity_server_rig,
) -> None:
    original = continuity_server_rig.create_focused_workstream()
    continuity_server_rig.deny_checkpoint_writes()

    result = continuity_server_rig.server.handle_tool_call(
        "continuity_checkpoint",
        continuity_server_rig.update_arguments(original),
    )

    assert result == {
        "error": "checkpoint_not_saved",
        "checkpoint_saved": False,
        "authoritative_context_id": original.current_context_id,
        "authoritative_checkpoint_revision": original.checkpoint_revision,
    }
    assert (
        continuity_server_rig.workstream(original.workstream_id).current_context_id
        == original.current_context_id
    )


def test_production_composition_installs_binding_lifecycle(
    continuity_server_rig,
) -> None:
    server = continuity_server_rig.server
    binding = continuity_server_rig.bind_new_workspace_through_project_service()

    focus = server.continuity_store.get_focus(
        binding.project,
        binding.workspace_fingerprint,
    )
    assert focus.workstream_id is None
    assert focus.revision == 0
```

Also test the single-candidate MCP flow calls checkpoint `claim` first and only then `switch_focus` using claim’s returned state version; confirmation alone cannot switch. Test continuation classification happens before `_context_gate_error` in compat/shadow/primary, makes zero FTS/HNSW/search calls, and uses only independent `continuity_ready`. Test unique focus, no-focus variants, foreign lease, stale repo, blocked/paused/terminal/corrupt pointer, tiny budget, project summary, no L2, and legacy project alias compatibility. If `project` and `project_hint` normalize differently, return `project_hint_conflict`.

- [ ] **Step 6: Run MCP/session-start tests and observe red**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_mcp_protocol.py tests/test_integration.py tests/test_context_service.py -k "continuity or session_start or checkpoint_not_saved"
```

Expected: FAIL because tools, schemas, independent readiness, and continuation-first routing are absent.

- [ ] **Step 7: Implement tool specs, public handlers, and continuation-first routing**

Register immutable specs from the same registry used by `tools/list` and `tools/call`. Representative resume spec:

```python
McpToolSpec(
    name="continuity_resume",
    description="Resolve the caller workspace and read its exact focused checkpoint.",
    input_schema={
        "type": "object",
        "additionalProperties": False,
        "required": ["workspace_path"],
        "properties": {
            "workspace_path": {"type": "string", "minLength": 1},
            "project_hint": {"type": "string"},
            "max_chars": {"type": "integer", "minimum": 1},
        },
    },
    annotations={"readOnlyHint": True},
)
```

At both production composition roots, construct the stores once on the same `ContextStore` connection:

```python
project_store, continuity_store = build_continuity_project_store(context_store)
self.project_store = project_store
self.continuity_store = continuity_store
```

Replace the prerequisite composition roots’ direct `ProjectStore(context_store)` call with these three lines, then pass `self.project_store` and `self.continuity_store` through their already-reviewed constructors. `MemoryMCPServer` and the project operator CLI use this factory whenever continuity schema is installed. The cleanup old-schema read-only planner is the only composition allowed to pass `binding_lifecycle=None`; no continuity-enabled binding mutation may do so.

In `_context_session_start` parse/validate first, classify second, then branch before the ordinary gate:

```python
def _context_session_start(self, args: dict) -> dict:
    try:
        request = ContextSessionStartRequest(
            project=args.get("project", ""),
            query=args.get("query"),
            max_chars=args.get("max_chars"),
            workspace_path=args.get("workspace_path", ""),
            project_hint=args.get("project_hint", ""),
        )
    except (ContextValidationError, TypeError):
        return self._context_error("invalid_arguments")
    intent = self.continuation_intent.classify(request.query)
    if intent is ContinuationIntent.CONTINUE:
        readiness_error = self._continuity_gate_error()
        if readiness_error is not None:
            return readiness_error
        return self._continuity_resume_from_session_start(request)
    gate_error = self._context_gate_error()
    if gate_error is not None:
        return gate_error
    result = self.context_service.session_start(request)
    return self._public_session_start_result(result, intent="new_goal")
```

Handlers attach only `self.lease_session` and serialize public results. Catch `ContinuityPermissionDenied("checkpoint_not_saved")` separately and read back only the old authoritative pointer/revisions for the response. Never report saved on denial. Keep ordinary legacy clients working with `project`, but continuation requires nonblank workspace path and never falls back to process cwd or project-only lookup.

Initialization instructions require session start before the first substantive answer; checkpoint after confirmed goal/design, implementation start, each independently verified task, blocker, handoff, and final verification; conflict triggers exact resume rather than blind retry. Instructions explicitly say a new goal preserves current focus until an explicit switch and a denied write means the old checkpoint remains authoritative. Do not promise automatic calls for clients that do not follow instructions.

- [ ] **Step 8: Run MCP, adapter, and session-start tests and observe green**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuation_intent.py tests/test_mcp_protocol.py tests/test_integration.py tests/test_context_service.py tests/test_project_cli.py tests/test_hooks.py tests/test_kimi_hooks.py tests/test_dsh_bridge.py
git diff --check
```

Expected: PASS in compat/shadow/primary, stable `continuity_not_ready` in legacy-only/uninstalled state, exact schema/error parity across supported adapters, and no private value in schema/output/logs.

- [ ] **Step 9: Commit Task 8**

Run:

```bash
git add evolvmem/continuation_intent.py evolvmem/mcp_contract.py evolvmem/mcp_server.py evolvmem/project_cli.py evolvmem/context_models.py evolvmem/context_service.py evolvmem/hooks.py evolvmem/kimi_hooks.py evolvmem/dsh_bridge.py tests/test_continuation_intent.py tests/test_mcp_protocol.py tests/test_integration.py tests/test_context_service.py tests/test_project_cli.py tests/test_hooks.py tests/test_kimi_hooks.py tests/test_dsh_bridge.py
git commit -m "feat: expose continuity protocol over MCP"
```

---

### Task 9: Activate the Web v2 workstream administration page

**Files:**

- Modify: `evolvmem/context_admin_models.py: ContinuityAdminListRequest and ContinuityAdminGetRequest symbols created by the Web v2 prerequisite`
- Modify: `evolvmem/context_service.py:187` (`ContextService` admin delegates)
- Modify: `evolvmem/web_v2.py: ContextV2Router route table created by the Web v2 prerequisite`
- Modify: `evolvmem/web_static/pages/workstreams.js: loadWorkstreams and renderWorkstreamDetail symbols created by the Web v2 prerequisite`
- Modify: `tests/test_context_admin.py: prerequisite typed-admin boundary tests`
- Modify: `tests/test_web_v2.py: prerequisite route/projection tests`
- Modify: `tests/test_web_static.py: prerequisite page-module tests`

**Interfaces:**

- Consumes: Task 6 `ContinuityService.status/admin_list/admin_get` and the prerequisite `ContextV2Router`/Core page shell.
- Produces: `ContextService.continuity_status() -> ContinuityStatus`, `continuity_admin_list(request: ContinuityAdminListRequest) -> ContinuityAdminListResult`, `continuity_admin_get(request: ContinuityAdminGetRequest) -> ContinuityWorkstreamDetail | None`, `GET /api/v2/context/workstreams`, and `GET /api/v2/context/workstreams/{id}?include_l2=false|true`.

- [ ] **Step 1: Write failing typed-admin, HTTP, and UI disclosure tests**

```python
def test_workstream_list_is_l0_only_and_detail_requires_explicit_l2(
    web_v2_client,
    continuity_admin_fixture,
) -> None:
    workstream = continuity_admin_fixture.create_open_workstream()

    listing = web_v2_client.get("/api/v2/context/workstreams?limit=20")
    default_detail = web_v2_client.get(
        "/api/v2/context/workstreams/" + workstream.id
    )
    l2_detail = web_v2_client.get(
        "/api/v2/context/workstreams/"
        + workstream.id
        + "?include_l2=true"
    )

    assert listing.status_code == 200
    assert listing.json["items"][0]["l0"] == workstream.l0
    assert "l1" not in listing.json["items"][0]
    assert "l2" not in listing.json["items"][0]
    assert default_detail.json["l1"] == workstream.l1
    assert "l2" not in default_detail.json
    assert l2_detail.json["l2"]["workstream_id"] == workstream.id
```

Add filters for project/fingerprint/status, stable capped pagination, duplicate/unknown query rejection, content-free errors, readiness/focus/revision/staleness display, and assertions excluding lease token/hash, writer hash, absolute path, and raw repo status. The UI has no control that bypasses MCP/server-side capability semantics.

- [ ] **Step 2: Run Web tests and observe red**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_admin.py tests/test_web_v2.py tests/test_web_static.py -k workstream
```

Expected: FAIL because typed delegates/routes/page data are absent or the prerequisite shell still shows not-ready.

- [ ] **Step 3: Implement typed delegates, exact routes, and progressive disclosure**

Representative router branch:

```python
_WORKSTREAM_DETAIL_ROUTE = re.compile(
    r"^/api/v2/context/workstreams/(?P<workstream_id>ws_[A-Za-z0-9_-]+)$"
)


if method == "GET" and path == "/api/v2/context/workstreams":
    request = parse_continuity_admin_list(query)
    return json_response(
        200,
        public_workstream_page(
            self.service.continuity_admin_list(request)
        ),
    )
match = _WORKSTREAM_DETAIL_ROUTE.fullmatch(path)
if method == "GET" and match is not None:
    request = parse_continuity_admin_get(
        workstream_id=match.group("workstream_id"),
        query=query,
    )
    detail = self.service.continuity_admin_get(request)
    if detail is None:
        return content_free_error(404, "workstream_not_found")
    return json_response(200, public_workstream_detail(detail))
```

The router calls only `ContextService`. List projection includes metadata and checkpoint L0. Detail includes L1 by default; only exact `include_l2=true` includes canonical L2. Browser rendering uses text nodes, preserves the readiness banner, and asks for explicit user action before L2 fetch.

- [ ] **Step 4: Run typed-admin, Web, and resume tests and observe green**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_admin.py tests/test_web_v2.py tests/test_web_static.py tests/test_continuity_resume.py
git diff --check
```

Expected: PASS with no direct store/SQL use from Web code and no private disclosure.

- [ ] **Step 5: Commit Task 9**

Run:

```bash
git add evolvmem/context_admin_models.py evolvmem/context_service.py evolvmem/web_v2.py evolvmem/web_static/pages/workstreams.js tests/test_context_admin.py tests/test_web_v2.py tests/test_web_static.py
git commit -m "feat(web): expose continuity workstream views"
```

---

### Task 10: Prove fresh-Agent handoff with Codex memory disabled and hidden

**Files:**

- Create: `scripts/accept_continuity_handoff.py`
- Create: `tests/test_continuity_acceptance.py`
- Modify: `evolvmem/mcp_server.py: private CheckpointRequestInterceptor injection at the continuity_checkpoint handler`
- Modify: `tests/test_mcp_protocol.py:1026` (`test_primary_handshake_does_not_wait_for_embedding_model`-adjacent process tests)

**Interfaces:**

- Consumes: completed `evolvmem.continuity.v1` MCP contract, unique owned temp workspace/config/data/key/vector roots, Codex CLI, bubblewrap, the normal Codex authentication file only, and the generic MCP client.
- Produces: private `CheckpointRequestInterceptor.prepare(request: ContinuityCheckpointRequest) -> ContinuityCheckpointRequest`, `committed(result: ContinuityMutationResult) -> None`, and `rolled_back() -> None`; `CanaryMarkerFactory`; immutable `EventAuditPolicy`, `IsolatedCodexRun`, `AcceptanceTrace`, `GenericResumeEvidence`, and `AcceptanceReport`; `AcceptanceEnvironment.create(root: Path) -> AcceptanceEnvironment`, `agent_a_run(digest_write_fd: int) -> IsolatedCodexRun`, `agent_b_run() -> IsolatedCodexRun`, `run_generic_resume() -> GenericResumeEvidence`, `prove_stale_writer_conflict() -> bool`, and `close_auth_fds() -> None`; `build_codex_bwrap_argv(run: IsolatedCodexRun) -> list[str]`; `parse_codex_events(lines: Iterable[str], policy: EventAuditPolicy) -> AcceptanceTrace`; `run_isolated_codex(run: IsolatedCodexRun) -> AcceptanceTrace`; `run_acceptance() -> AcceptanceReport`; and content-free Codex-to-Codex/Codex-to-generic evidence.

- [ ] **Step 1: Preflight the required namespace primitive**

Run exactly:

```bash
bwrap --ro-bind / / --proc /proc --dev /dev /usr/bin/true
```

Expected outside the managed sandbox: exit 0. `No permissions to create a new namespace` inside the normal sandbox is a policy denial; rerun the same bounded command through explicit escalation/approval. If the approved probe still fails, stop this task as blocked. Do not downgrade to a merely new or memory-enabled Codex session.

- [ ] **Step 2: Write failing runner, isolation, trace, marker, and cleanup tests**

```python
import hashlib
from dataclasses import replace

import pytest

from scripts.accept_continuity_handoff import (
    EventAuditPolicy,
    IsolationUnavailable,
    build_codex_bwrap_argv,
    parse_codex_events,
    run_isolated_codex,
)


def test_codex_runner_disables_memories_and_mount_hides_history(
    acceptance_run,
) -> None:
    argv = build_codex_bwrap_argv(acceptance_run)
    joined = "\n".join(argv)

    assert "--ephemeral" in argv
    assert "--ignore-user-config" in argv
    assert "--ignore-rules" in argv
    assert "memories.use_memories=false" in argv
    assert "memories.generate_memories=false" in argv
    assert "history.persistence=none" in argv
    assert acceptance_run.home_env_assignment not in argv
    assert acceptance_run.codex_home_env_assignment not in argv
    assert argv.count("--tmpfs") == 1
    assert str(acceptance_run.normal_codex_dir) in argv
    assert str(acceptance_run.auth_file) not in joined
    auth_bind = argv.index("--ro-bind-fd")
    assert argv[auth_bind + 1] == str(acceptance_run.auth_fd)
    assert argv[auth_bind + 2] == str(
        acceptance_run.normal_codex_dir / acceptance_run.auth_file.name
    )
    assert str(acceptance_run.normal_sessions_dir) not in joined
    assert str(acceptance_run.normal_history_file) not in joined
    assert str(acceptance_run.normal_memories_db) not in joined
    writable_pairs = {
        (argv[index + 1], argv[index + 2])
        for index, value in enumerate(argv)
        if value == "--bind"
    }
    assert writable_pairs == {
        (str(path), str(path)) for path in acceptance_run.writable_roots
    }
    assert acceptance_run.writable_roots == (
        acceptance_run.workspace,
        acceptance_run.evolvmem_config_root,
        acceptance_run.evolvmem_data_root,
        acceptance_run.evolvmem_vector_root,
    )


def test_event_audit_rejects_search_and_direct_store_reads(
    forbidden_acceptance_events,
    acceptance_policy: EventAuditPolicy,
) -> None:
    trace = parse_codex_events(
        iter(forbidden_acceptance_events),
        acceptance_policy,
    )

    assert trace.forbidden_tool_calls == (
        "memory_search",
        "context_search",
    )
    assert trace.direct_store_reads == (
        "canary_evolvmem_database",
        "codex_history_store",
    )
    assert trace.accepted is False


def test_missing_namespace_never_launches_an_unisolated_fallback(
    acceptance_run,
    subprocess_spy,
) -> None:
    acceptance_run = replace(acceptance_run, namespace_available=False)

    with pytest.raises(IsolationUnavailable, match="namespace_required"):
        run_isolated_codex(acceptance_run)

    assert subprocess_spy.calls == ()


def test_server_generated_marker_never_enters_client_artifacts(
    deterministic_canary_server,
) -> None:
    result = deterministic_canary_server.agent_a_checkpoint()
    marker = deterministic_canary_server.raw_marker
    digest = hashlib.sha256(marker.encode("utf-8")).hexdigest()

    assert result.checkpoint_saved is True
    assert deterministic_canary_server.digest_pipe_value == digest
    assert marker in deterministic_canary_server.stored_checkpoint_l2
    assert marker not in deterministic_canary_server.agent_prompt
    assert marker not in "\n".join(deterministic_canary_server.codex_argv)
    assert marker not in "\n".join(
        deterministic_canary_server.environment_values
    )
    assert marker not in deterministic_canary_server.repository_bytes.decode(
        "utf-8",
        errors="ignore",
    )
    assert marker not in deterministic_canary_server.content_free_report_json


def test_agent_b_hashes_resumed_marker_without_retaining_raw_text(
    agent_b_events,
    acceptance_policy: EventAuditPolicy,
    expected_marker: str,
) -> None:
    trace = parse_codex_events(iter(agent_b_events), acceptance_policy)

    assert trace.resumed_marker_sha256 == hashlib.sha256(
        expected_marker.encode("utf-8")
    ).hexdigest()
    assert expected_marker not in repr(trace)
```

Also test owned-temp refusal/cleanup, owner UID and non-symlink roots, inline MCP config with exactly one canary server, event parser, first substantive call `context_session_start`, exact ID/revisions, repo check, lease claim, stale-writer conflict/no residue, generic-client parity, and content-free failure reports. Classify every MCP call plus every shell/file command from the JSON event stream. Reject `memory_search`, `context_search`, reads of the temporary EvolvMem SQLite/config/key/vector roots, and reads of normal Codex config/memory/session/history paths even though the MCP child can write the explicitly mounted temp roots. The harness refuses non-temp targets, a missing namespace, incomplete mount proof, any writable bind beyond the four exact roots, extra `.codex` bind, or missing auth-only bind before spawning Codex. The deterministic marker fixture may expose the raw value only to this server-side unit test; production acceptance output never does.

- [ ] **Step 3: Run acceptance unit tests and observe red**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_acceptance.py
```

Expected: FAIL because the harness and parser are absent.

- [ ] **Step 4: Implement the acceptance-only marker boundary, streaming audit, isolated runner, and complete orchestration**

`MemoryMCPServer` accepts a private optional `checkpoint_interceptor` defaulting to `None`. The `continuity_checkpoint` handler calls `prepare` after typed request validation but before `ContinuityService.checkpoint`; it calls `committed` only after that service returns, and calls `rolled_back` before returning or raising on every failure. No public MCP schema, result, capability, or production composition exposes this injection.

```python
class CheckpointRequestInterceptor(Protocol):
    def prepare(
        self,
        request: ContinuityCheckpointRequest,
    ) -> ContinuityCheckpointRequest:
        raise NotImplementedError

    def committed(self, result: ContinuityMutationResult) -> None:
        raise NotImplementedError

    def rolled_back(self) -> None:
        raise NotImplementedError


def _call_continuity_checkpoint(
    self,
    request: ContinuityCheckpointRequest,
) -> ContinuityMutationResult:
    interceptor = self._checkpoint_interceptor
    prepared = interceptor.prepare(request) if interceptor is not None else request
    try:
        result = self.continuity_service.checkpoint(
            prepared,
            lease_session=self.lease_session,
        )
    except BaseException:
        if interceptor is not None:
            interceptor.rolled_back()
        raise
    if interceptor is not None:
        interceptor.committed(result)
    return result
```

`committed` is required to be idempotent and no-throw because the SQLite mutation is already durable when it runs. A delivery failure is recorded privately by the acceptance interceptor; the harness then receives EOF instead of a digest and fails `marker_digest_pipe_closed` without misreporting the committed checkpoint as rolled back.

In the acceptance script, the fixed prompt sentinel is public and non-random. The raw randomized value is created only inside the server-side interceptor, substituted immediately before the real service call, and committed through the normal typed transaction:

```python
SERVER_CANARY_SENTINEL = "EVOLVMEM_ACCEPTANCE_SERVER_SENTINEL_V1"
_RAW_MARKER_RE = re.compile(r"EVOLVMEM-CANARY-[0-9A-F]{48}")


class CanaryMarkerFactory:
    def __init__(self, *, digest_fd: int, token_hex: Callable[[int], str]) -> None:
        self._digest_fd = digest_fd
        self._token_hex = token_hex
        self._pending_digest: str | None = None
        self._delivery_failed = False

    def prepare(
        self,
        request: ContinuityCheckpointRequest,
    ) -> ContinuityCheckpointRequest:
        payload = request.payload
        if payload is None:
            return request
        fields = (payload.objective, payload.current_step, payload.next_action)
        if SERVER_CANARY_SENTINEL not in fields:
            return request
        if fields != (SERVER_CANARY_SENTINEL,) * 3:
            raise ContinuityValidationError("acceptance_canary_sentinel_mismatch")
        if self._pending_digest is not None:
            raise ContinuityValidationError("acceptance_canary_already_pending")
        marker = "EVOLVMEM-CANARY-" + self._token_hex(24).upper()
        self._pending_digest = hashlib.sha256(marker.encode("utf-8")).hexdigest()
        transformed = replace(
            payload,
            objective="Verify isolated continuity marker " + marker,
            current_step="Server stored continuity marker " + marker,
            next_action=(
                "Validate the repository anchor and checkpoint one passing "
                "verification while preserving marker " + marker
            ),
        )
        return replace(request, payload=transformed)

    def committed(self, result: ContinuityMutationResult) -> None:
        del result
        if self._pending_digest is None:
            return
        encoded = (self._pending_digest + "\n").encode("ascii")
        try:
            written = os.write(self._digest_fd, encoded)
            if written != len(encoded):
                self._delivery_failed = True
        except OSError:
            self._delivery_failed = True
        finally:
            self._pending_digest = None

    def rolled_back(self) -> None:
        self._pending_digest = None
```

The script’s `serve-canary-mcp` mode receives only a numeric inherited digest FD and owned temp root paths, constructs the real server with `CanaryMarkerFactory(digest_fd=fd, token_hex=secrets.token_hex)`, and runs stdio. Neither the raw marker nor its digest appears in prompt, argv, environment, repository, or a harness file. Only the SHA-256 digest crosses the anonymous pipe after the checkpoint transaction commits. Agent B receives the raw marker only in the normal exact continuity result; the parser hashes it immediately and retains no message or checkpoint text.

Define the content-free trace and parse the live stdout iterator without retaining raw lines:

```python
@dataclass(frozen=True, slots=True)
class EventAuditPolicy:
    expected_server: str
    allowed_tools: frozenset[str]
    forbidden_tools: frozenset[str]
    forbidden_command_targets: tuple[tuple[str, str], ...]
    required_first_tool: str


@dataclass(frozen=True, slots=True)
class AcceptanceTrace:
    tool_calls: tuple[str, ...]
    failed_tool_calls: tuple[str, ...]
    forbidden_tool_calls: tuple[str, ...]
    unexpected_tool_calls: tuple[str, ...]
    direct_store_reads: tuple[str, ...]
    first_substantive_tool: str
    workstream_id: str
    context_id: int
    checkpoint_revision: int
    state_version: int
    resumed_marker_sha256: str
    process_exit_code: int
    accepted: bool
    reason_codes: tuple[str, ...]


def _tool_result_payload(item: dict[str, object]) -> dict[str, object]:
    result = item.get("result")
    if not isinstance(result, dict):
        return {}
    blocks = result.get("content")
    chunks = [
        block["text"]
        for block in blocks
        if isinstance(blocks, list)
        and isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ] if isinstance(blocks, list) else []
    if not chunks:
        structured = result.get("structured_content")
        return structured if isinstance(structured, dict) else {}
    try:
        parsed = json.loads("".join(chunks))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _resumed_marker_digest(payload: dict[str, object]) -> str:
    continuity = payload.get("continuity")
    body = continuity if isinstance(continuity, dict) else payload
    checkpoint = body.get("checkpoint")
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    values = [
        checkpoint.get("objective"),
        checkpoint.get("current_step"),
        checkpoint.get("next_action"),
        body.get("block"),
    ]
    markers = {
        match.group(0)
        for value in values
        if isinstance(value, str)
        for match in _RAW_MARKER_RE.finditer(value)
    }
    if not markers:
        return ""
    if len(markers) != 1:
        raise AcceptanceFailure("multiple_resumed_markers")
    marker = markers.pop()
    return hashlib.sha256(marker.encode("utf-8")).hexdigest()


def parse_codex_events(
    lines: Iterable[str],
    policy: EventAuditPolicy,
) -> AcceptanceTrace:
    tools: list[str] = []
    failed_tools: list[str] = []
    forbidden_tools: list[str] = []
    unexpected_tools: list[str] = []
    direct_reads: list[str] = []
    workstream_id = ""
    context_id = 0
    checkpoint_revision = 0
    state_version = 0
    marker_digest = ""
    turn_failed = False
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AcceptanceFailure("codex_event_not_json") from exc
        event_type = event.get("type")
        if event_type in {"error", "turn.failed"}:
            turn_failed = True
            continue
        if event_type != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "mcp_tool_call":
            tool = str(item.get("tool", ""))
            tools.append(tool)
            short_name = tool.rsplit("__", 1)[-1]
            if item.get("error") is not None or item.get("status") != "completed":
                failed_tools.append(short_name or "unknown_tool")
            if (
                item.get("server") != policy.expected_server
                or short_name not in policy.allowed_tools
            ):
                unexpected_tools.append(short_name or "unknown_tool")
            if short_name in policy.forbidden_tools:
                forbidden_tools.append(short_name)
            payload = _tool_result_payload(item)
            nested = payload.get("continuity")
            public = nested if isinstance(nested, dict) else payload
            workstream_id = str(public.get("workstream_id", workstream_id))
            raw_context_id = public.get("context_id", context_id)
            raw_checkpoint_revision = public.get(
                "checkpoint_revision",
                checkpoint_revision,
            )
            raw_state_version = public.get("state_version", state_version)
            if type(raw_context_id) is int:
                context_id = raw_context_id
            if type(raw_checkpoint_revision) is int:
                checkpoint_revision = raw_checkpoint_revision
            if type(raw_state_version) is int:
                state_version = raw_state_version
            resumed = _resumed_marker_digest(payload)
            if resumed:
                marker_digest = resumed
        elif item_type == "command_execution":
            command = item.get("command")
            if not isinstance(command, str):
                command = ""
            for label, target in policy.forbidden_command_targets:
                if target and target in command and label not in direct_reads:
                    direct_reads.append(label)
    first_tool = tools[0] if tools else ""
    reasons: list[str] = []
    if first_tool.rsplit("__", 1)[-1] != policy.required_first_tool:
        reasons.append("first_tool_not_context_session_start")
    if forbidden_tools:
        reasons.append("forbidden_search_tool")
    if failed_tools:
        reasons.append("tool_call_failed")
    if unexpected_tools:
        reasons.append("unexpected_tool_source")
    if direct_reads:
        reasons.append("direct_store_read")
    if turn_failed:
        reasons.append("codex_turn_failed")
    return AcceptanceTrace(
        tool_calls=tuple(tools),
        failed_tool_calls=tuple(failed_tools),
        forbidden_tool_calls=tuple(forbidden_tools),
        unexpected_tool_calls=tuple(unexpected_tools),
        direct_store_reads=tuple(direct_reads),
        first_substantive_tool=first_tool,
        workstream_id=workstream_id,
        context_id=context_id,
        checkpoint_revision=checkpoint_revision,
        state_version=state_version,
        resumed_marker_sha256=marker_digest,
        process_exit_code=0,
        accepted=not reasons,
        reason_codes=tuple(reasons),
    )
```

Build the namespace with the root filesystem read-only and only the validated canary workspace and three EvolvMem server roots writable:

```python
import fcntl


@dataclass(frozen=True, slots=True)
class IsolatedCodexRun:
    owned_temp_root: Path
    workspace: Path
    evolvmem_config_root: Path
    evolvmem_data_root: Path
    evolvmem_vector_root: Path
    normal_codex_dir: Path
    auth_file: Path
    auth_fd: int
    python_executable: Path
    harness_script: Path
    inline_mcp_config: str
    user_prompt: str
    policy: EventAuditPolicy
    pass_fds: tuple[int, ...]
    namespace_probe_argv: tuple[str, ...]
    timeout_seconds: float
    namespace_available: bool = True

    @property
    def writable_roots(self) -> tuple[Path, ...]:
        return (
            self.workspace,
            self.evolvmem_config_root,
            self.evolvmem_data_root,
            self.evolvmem_vector_root,
        )

    @property
    def normal_sessions_dir(self) -> Path:
        return self.normal_codex_dir / "sessions"

    @property
    def normal_history_file(self) -> Path:
        return self.normal_codex_dir / "history.jsonl"

    @property
    def normal_memories_db(self) -> Path:
        return self.normal_codex_dir / "memories_1.sqlite"

    @property
    def home_env_assignment(self) -> str:
        return "HOME=" + os.environ.get("HOME", "")

    @property
    def codex_home_env_assignment(self) -> str:
        return "CODEX_HOME=" + os.environ.get("CODEX_HOME", "")

    def child_environment(self) -> dict[str, str]:
        return {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("EVOLVMEM_")
        }

    def validate_owned_roots(self) -> None:
        root = self.owned_temp_root.resolve(strict=True)
        if self.owned_temp_root != root or self.owned_temp_root.is_symlink():
            raise IsolationUnavailable("owned_temp_root_not_canonical")
        root_stat = root.stat()
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or root_stat.st_uid != os.getuid()
            or root_stat.st_mode & 0o022
        ):
            raise IsolationUnavailable("owned_temp_root_not_private")
        if len(set(self.writable_roots)) != 4:
            raise IsolationUnavailable("writable_roots_not_distinct")
        for candidate in self.writable_roots:
            resolved = candidate.resolve(strict=True)
            candidate_stat = candidate.lstat()
            if (
                candidate != resolved
                or candidate.is_symlink()
                or root not in resolved.parents
                or not stat.S_ISDIR(candidate_stat.st_mode)
                or candidate_stat.st_uid != os.getuid()
                or candidate_stat.st_mode & 0o022
            ):
                raise IsolationUnavailable("writable_root_not_owned_temp")
        if self.namespace_probe_argv != (
            "bwrap", "--ro-bind", "/", "/", "--proc", "/proc",
            "--dev", "/dev", "/usr/bin/true",
        ):
            raise IsolationUnavailable("namespace_probe_not_exact")
        if any(type(fd) is not int or fd < 3 for fd in self.pass_fds):
            raise IsolationUnavailable("inherited_fd_invalid")
        if self.auth_fd not in self.pass_fds:
            raise IsolationUnavailable("auth_fd_not_inherited")
        auth_stat = os.fstat(self.auth_fd)
        auth_flags = fcntl.fcntl(self.auth_fd, fcntl.F_GETFL)
        if (
            not stat.S_ISREG(auth_stat.st_mode)
            or auth_stat.st_uid != os.getuid()
            or auth_flags & os.O_ACCMODE != os.O_RDONLY
        ):
            raise IsolationUnavailable("auth_fd_not_private_read_only")


def build_codex_bwrap_argv(run: IsolatedCodexRun) -> list[str]:
    run.validate_owned_roots()
    codex_argv = [
        "codex",
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--json",
        "-c",
        "memories.use_memories=false",
        "-c",
        "memories.generate_memories=false",
        "-c",
        "history.persistence=none",
        "-c",
        run.inline_mcp_config,
        "-C",
        str(run.workspace),
        run.user_prompt,
    ]
    namespace = [
        "bwrap",
        "--die-with-parent",
        "--new-session",
        "--ro-bind",
        "/",
        "/",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        str(run.normal_codex_dir),
        "--ro-bind-fd",
        str(run.auth_fd),
        str(run.normal_codex_dir / run.auth_file.name),
    ]
    for writable_root in run.writable_roots:
        namespace.extend(
            ("--bind", str(writable_root), str(writable_root))
        )
    return [
        *namespace,
        str(run.python_executable),
        str(run.harness_script),
        "preflight-exec",
        "--normal-codex-dir",
        str(run.normal_codex_dir),
        "--auth-name",
        run.auth_file.name,
        "--",
        *codex_argv,
    ]
```

`AcceptanceEnvironment.create` opens the normal auth file once with `O_RDONLY | O_CLOEXEC`, places that numeric FD in each run’s `pass_fds`, and never places its source path in argv. The `--ro-bind-fd` operation remains valid after `--tmpfs` hides the source `.codex` tree and exposes only the destination auth file. `close_auth_fds()` is idempotent and closes those owned descriptors after both Codex runs. `IsolatedCodexRun.validate_owned_roots()` resolves each writable root, requires a real non-symlink directory owned by the current UID below the one `owned_temp_root`, rejects group/world-writable ownership boundaries, requires the auth FD to be an inherited current-UID regular file opened read-only, and requires `writable_roots == (workspace, evolvmem_config_root, evolvmem_data_root, evolvmem_vector_root)`. `preflight-exec` runs inside the namespace, requires the normal `.codex` directory entries to equal `{auth_name}`, verifies that auth is a regular read-only file, verifies config, plugins, sessions, shell snapshots, `history.jsonl`, `memories_1.sqlite`, and thread-history databases are absent, then calls `os.execvp` with the supplied Codex argv. Do not set `HOME` or `CODEX_HOME`. The inline config exposes exactly one canary EvolvMem MCP command from the reviewed worktree and owned temp roots. The MCP command receives only paths, the public sentinel contract, and a numeric digest FD; it receives no raw marker. There is no plain-subprocess branch.

Run Codex by streaming JSONL directly into the parser; do not call `communicate`, retain raw lines, or write stdout/stderr to a file:

```python
def run_isolated_codex(run: IsolatedCodexRun) -> AcceptanceTrace:
    run.validate_owned_roots()
    if not run.namespace_available:
        raise IsolationUnavailable("namespace_required")
    probe = subprocess.run(
        run.namespace_probe_argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if probe.returncode != 0:
        raise IsolationUnavailable("namespace_required")
    timed_out = False
    with subprocess.Popen(
        build_codex_bwrap_argv(run),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        env=run.child_environment(),
        close_fds=True,
        pass_fds=run.pass_fds,
    ) as process:
        if process.stdout is None:
            raise AcceptanceFailure("codex_stdout_pipe_missing")
        def kill_on_timeout() -> None:
            nonlocal timed_out
            timed_out = True
            try:
                process.kill()
            except ProcessLookupError:
                timed_out = False

        timer = threading.Timer(run.timeout_seconds, kill_on_timeout)
        timer.start()
        try:
            parsed = parse_codex_events(
                iter(process.stdout.readline, ""),
                run.policy,
            )
            exit_code = process.wait()
        finally:
            timer.cancel()
    reasons = list(parsed.reason_codes)
    if timed_out:
        reasons.append("codex_process_timeout")
    if exit_code != 0:
        reasons.append("codex_process_failed")
    return replace(
        parsed,
        process_exit_code=exit_code,
        accepted=not reasons,
        reason_codes=tuple(sorted(set(reasons))),
    )
```

The test-only `namespace_available=False` path raises before `subprocess.run` or `Popen`, satisfying the no-fallback assertion. A real run always executes the exact approved bwrap probe first.

Agent A’s prompt uses `SERVER_CANARY_SENTINEL` for objective/current/next, creates the canary workstream/focus with accepted decision, harmless artifact, passing verification, and server-owned repo anchor, releases lease, and exits. The interceptor supplies the randomized value only after the request reaches the server. Agent B starts in a separately created empty namespace and receives only:

```text
继续原任务
```

Agent B’s trace must resume the same workstream/context/revisions, hash the marker from that exact result, make zero forbidden searches and zero direct-store reads, validate repo anchor, claim by state version, perform one harmless action, and write checkpoint revision +1. A stale concurrent writer conflicts without residue. Repeat the exact resume/result/hash check with an independent generic MCP harness declaring v1.

Implement the complete top-level orchestration. `AcceptanceEnvironment.create(root)` creates four sibling owned directories, a tiny Git repository, temp Config/key/vector state, inline one-server MCP config, policies labeling every forbidden temp and normal store, and Agent A/B `IsolatedCodexRun` values. `run_generic_resume` uses only MCP JSON-RPC and returns `GenericResumeEvidence(marker_sha256: str, workstream_id: str, context_id: int, checkpoint_revision: int, state_version: int)`. `prove_stale_writer_conflict` returns true only when database/event/epoch/journal snapshots are byte-equivalent before and after the refused stale write.

```python
@dataclass(frozen=True, slots=True)
class GenericResumeEvidence:
    marker_sha256: str
    workstream_id: str
    context_id: int
    checkpoint_revision: int
    state_version: int


@dataclass(frozen=True, slots=True)
class AcceptanceReport:
    ok: bool
    reason_codes: tuple[str, ...]
    codex_to_codex: bool
    codex_to_generic: bool
    forbidden_search_count: int
    direct_store_read_count: int
    exact_id_match: bool
    revision_increment: bool
    marker_digest_match: bool
    stale_writer_conflict: bool
    owned_temp_cleanup: bool

    def public_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "reason_codes": list(self.reason_codes),
            "codex_to_codex": self.codex_to_codex,
            "codex_to_generic": self.codex_to_generic,
            "forbidden_search_count": self.forbidden_search_count,
            "direct_store_read_count": self.direct_store_read_count,
            "exact_id_match": self.exact_id_match,
            "revision_increment": self.revision_increment,
            "marker_digest_match": self.marker_digest_match,
            "stale_writer_conflict": self.stale_writer_conflict,
            "owned_temp_cleanup": self.owned_temp_cleanup,
        }


def _read_digest_line(fd: int) -> str:
    chunks = bytearray()
    while b"\n" not in chunks:
        block = os.read(fd, 128)
        if not block:
            raise AcceptanceFailure("marker_digest_pipe_closed")
        chunks.extend(block)
        if len(chunks) > 65:
            raise AcceptanceFailure("marker_digest_pipe_oversized")
    line, separator, remainder = bytes(chunks).partition(b"\n")
    if separator != b"\n" or remainder:
        raise AcceptanceFailure("marker_digest_pipe_framing")
    digest = line.decode("ascii")
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise AcceptanceFailure("marker_digest_invalid")
    return digest


def run_acceptance() -> AcceptanceReport:
    partial: AcceptanceReport
    root_path: Path
    with tempfile.TemporaryDirectory(
        prefix="evolvmem-continuity-handoff-"
    ) as raw_root:
        root_path = Path(raw_root).resolve()
        environment = AcceptanceEnvironment.create(root_path)
        digest_read_fd, digest_write_fd = os.pipe()
        try:
            trace_a = run_isolated_codex(
                environment.agent_a_run(digest_write_fd)
            )
            os.close(digest_write_fd)
            digest_write_fd = -1
            expected_marker_digest = _read_digest_line(digest_read_fd)
            trace_b = run_isolated_codex(environment.agent_b_run())
            generic = environment.run_generic_resume()
            stale_conflict = environment.prove_stale_writer_conflict()
        finally:
            os.close(digest_read_fd)
            if digest_write_fd >= 0:
                os.close(digest_write_fd)
            environment.close_auth_fds()
        checks = {
            "agent_a": trace_a.accepted,
            "agent_b": trace_b.accepted,
            "same_workstream": trace_a.workstream_id == trace_b.workstream_id,
            "checkpoint_incremented": (
                trace_b.checkpoint_revision == trace_a.checkpoint_revision + 1
            ),
            "marker_from_evolvmem": (
                trace_b.resumed_marker_sha256 == expected_marker_digest
            ),
            "generic_marker_match": (
                generic.marker_sha256 == expected_marker_digest
            ),
            "generic_workstream_match": (
                generic.workstream_id == trace_b.workstream_id
            ),
            "stale_writer_conflict": stale_conflict,
            "forbidden_searches_zero": not trace_b.forbidden_tool_calls,
            "direct_store_reads_zero": not trace_b.direct_store_reads,
        }
        reasons = tuple(
            sorted(name for name, passed in checks.items() if not passed)
        )
        partial = AcceptanceReport(
            ok=not reasons,
            reason_codes=reasons,
            codex_to_codex=checks["agent_a"] and checks["agent_b"],
            codex_to_generic=(
                checks["generic_marker_match"]
                and checks["generic_workstream_match"]
            ),
            forbidden_search_count=len(trace_b.forbidden_tool_calls),
            direct_store_read_count=len(trace_b.direct_store_reads),
            exact_id_match=checks["same_workstream"],
            revision_increment=checks["checkpoint_incremented"],
            marker_digest_match=checks["marker_from_evolvmem"],
            stale_writer_conflict=stale_conflict,
            owned_temp_cleanup=False,
        )
    cleanup_ok = not root_path.exists()
    final_reasons = partial.reason_codes + (
        () if cleanup_ok else ("owned_temp_cleanup_failed",)
    )
    return replace(
        partial,
        ok=partial.ok and cleanup_ok,
        reason_codes=tuple(sorted(set(final_reasons))),
        owned_temp_cleanup=cleanup_ok,
    )
```

`AcceptanceReport.public_dict()` emits only these booleans, counts, and stable reason codes. It excludes paths, prompts, event text, marker/digest, tool arguments/results, stdout, stderr, and commands.

- [ ] **Step 5: Run acceptance unit tests and observe green**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_continuity_acceptance.py tests/test_mcp_protocol.py
git diff --check
```

Expected: PASS for marker provenance, streaming parser, isolation argv, exact writable binds, auth-only mount, temp ownership, cleanup, interceptor rollback, and deterministic fake-process traces.

- [ ] **Step 6: Run the real isolated temporary handoff**

Run this exact command with explicit unsandboxed approval because its tested child namespace cannot start in the managed command sandbox:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/python scripts/accept_continuity_handoff.py
```

Expected: one content-free PASS report for Codex-to-Codex, Codex-to-generic, forbidden-search count 0, direct-store-read count 0, exact ID equality, revision increment, in-memory marker-digest match, stale-writer conflict, and owned-temp cleanup. If Codex CLI, required auth, or approved namespace isolation is unavailable, record this gate unrun; refuse to launch and do not substitute unit mocks, a plain subprocess, or a merely new Codex session.

- [ ] **Step 7: Commit Task 10**

Run:

```bash
git add scripts/accept_continuity_handoff.py evolvmem/mcp_server.py tests/test_continuity_acceptance.py tests/test_mcp_protocol.py
git commit -m "test: verify cross-agent project continuity"
```

---

### Task 11: Run integrated gates, Python 3.10, dirty-WIP deployment selection, and independent review

**Files:**

- Read: every file changed by the three implementation plans
- Read: `pyproject.toml` and `uv.lock`
- Review-fix files: none in this task; a blocking finding stops execution and requires a revised exact TDD task before any code change
- Real database/config/vector files: no access beyond temporary harness roots

**Interfaces:**

- Consumes: all reviewed feature commits, `scripts/accept_project_continuity_migration.py`, `scripts/accept_continuity_handoff.py`, Python requirement `requires-python = ">=3.10"`, and the dirty-main inventory.
- Produces: current-Python and Python 3.10 full-suite evidence, two temporary acceptance reports, two independent review verdicts with zero blockers, and—only when dirty WIP is explicitly excluded—one exact `deployment_candidate_commit`; a request to integrate dirty WIP instead produces a hard stop for a separately revised and re-audited integration task.

- [ ] **Step 1: Run the current interpreter’s complete deterministic and temporary gates**

Run:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/python scripts/accept_project_continuity_migration.py
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/python scripts/accept_continuity_handoff.py
git diff --check
git status --short
```

The handoff command still requires the approved unsandboxed bubblewrap capability from Task 10. Expected: zero test failures, both content-free harnesses PASS, exact pass/skip counts recorded, and a clean feature worktree.

- [ ] **Step 2: Run the complete suite under the declared Python 3.10 floor with uv**

First prohibit an implicit interpreter download:

```bash
uv python find --no-python-downloads 3.10
```

If that command cannot find Python 3.10, request explicit approval for the network/cache mutation and run:

```bash
uv python install 3.10
```

Then run the full suite in an isolated uv environment without changing the repository’s existing `.venv` or lockfile:

```bash
uv run --isolated --locked --python 3.10 --extra dev python -m pytest -q
```

Expected: Python 3.10.x and zero failures. If interpreter/dependency download approval is denied or unavailable, this gate remains unrun and Task 12 cannot begin.

- [ ] **Step 3: Audit secrets, semantic-write boundaries, exact lookup, and formatting**

Run:

```bash
rg -n "MemoryStore\(|_connection\(|lease_token|lease_writer|workspace_path|WORKSTREAM_(CHECKPOINT|RECOVERY)|memory_search|context_search" evolvmem tests scripts
git grep -n -E "/home/jiangli|\.codex|BEGIN (RSA|OPENSSH)|Bearer [A-Za-z0-9]"
```

Classify every match. Production continuity mutations must be under `semantic_transaction`; continuation must not call search; public projections must exclude paths and private hashes. Documentation/test fixture paths are allowed only when they are synthetic or the already documented repository/worktree paths, never live data/auth paths.

- [ ] **Step 4: Make the dirty-WIP integration and deployment decision before real approval**

Inspect both checkouts without changing either:

```bash
git -C /home/jiangli/hermes-memory-plugin status --short
git -C /home/jiangli/hermes-memory-plugin diff --name-only
git -C /home/jiangli/hermes-memory-plugin ls-files --others --exclude-standard
git status --short
git rev-parse HEAD
```

Present two explicit outcomes to the user:

1. deploy the reviewed feature-worktree commit directly while leaving dirty WIP excluded and untouched; record the full 40-hex feature-worktree HEAD as `deployment_candidate_commit`, then continue only if it owns all Step 1–3 evidence; or
2. include dirty WIP. This selection is an immediate hard stop: do not create a branch/worktree, copy files, generate/apply a patch, stage, commit, merge, stash, reset, or run Task 12 under this plan.

For outcome 2, first revise and re-audit the implementation plan with a separate exact integration task that names the branch and worktree creation commands, the complete copy-or-patch source/destination file set, the allowed dirty-checkout mutation, pre/post tracked and untracked WIP hash commands, staging list, commit boundaries, focused dirty-WIP regressions, full current/Python 3.10 suites, both temporary harnesses, independent review, and the selected combined commit ancestry check. Obtain the specific authorization that task requires, execute it from its first red step, and rerun all of Task 11. Only the revised plan may then record an integrated `deployment_candidate_commit` and update Task 12’s selected-worktree root. Until that happens, Task 12 remains blocked and this plan makes no WIP capture promise. Never deploy uncommitted dirty files.

- [ ] **Step 5: Obtain two independent code-review verdicts**

Invoke `superpowers-requesting-code-review`. Reviewer A audits project resolution, historical old-schema plan/apply, backup, semantic epoch/journal, rollback, rollups, retention, and vector gates. Reviewer B audits pointer/L2 integrity, state/focus/lease CAS, SecretBuffer lifetime, exact resume order, recovery, MCP privacy, no-search routing, Web disclosure, and bubblewrap acceptance.

Expected: two written verdicts with zero blocking findings. A blocking finding ends this execution without changing code. Revise and re-audit this plan with concrete regression test and implementation content before resuming; then execute the revised red/green/commit cycle and rerun all of Task 11.

- [ ] **Step 6: Freeze final reviewed evidence**

Run after both reviewers pass:

```bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q
uv run --isolated --locked --python 3.10 --extra dev python -m pytest -q
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/python scripts/accept_project_continuity_migration.py
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/python scripts/accept_continuity_handoff.py
git diff --check
git status --short
git rev-parse HEAD
```

Expected: all gates pass, clean selected feature/integration worktree, and `HEAD` equals the recorded `deployment_candidate_commit`. Do not claim the real historical database is cleaned.

---

### Task 12: Build the real-data approval packet from the actual old-schema digest and stop

**Files:**

- Read: reviewed `scripts/accept_real_project_continuity_migration.py` from `deployment_candidate_commit`
- Repository files modified: none
- Real database/config/vector: open database read-only/query-only; do not create schema, key, backup, run row, lock, vector, or config change

**Interfaces:**

- Consumes: Task 11 `deployment_candidate_commit`, the cleanup-plan real-wrapper interface, live Config discovery, and the existing old-schema or current-schema live database.
- Produces: two byte-equivalent content-free read-only plans, one actual 64-hex `plan_digest`, writer-stop/restart and deployment decisions, backup policy, and one fully rendered `apply_command` containing that actual digest, an absolute reviewed-worktree script path, and an absolute `PYTHONPATH`.

- [ ] **Step 1: Verify the selected code and old-schema read-only planner without deployment**

Outcome 1 of Task 11 fixes the selected root to `/home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity`. If Task 11 outcome 2 was requested, this task is blocked until a revised plan replaces every selected-root literal below. Run:

```bash
git -C /home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity rev-parse HEAD
git -C /home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity status --short
PYTHONPATH=/home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity /home/jiangli/hermes-memory-plugin/.venv/bin/python /home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity/scripts/accept_real_project_continuity_migration.py plan --json
```

The first two outputs must match Task 11’s candidate and clean state. The plan command loads live Config but opens the database with SQLite `mode=ro` and `PRAGMA query_only=ON`. It must understand the approved old schema without bootstrapping continuity/project tables, creating an HMAC key, obtaining a cutover lock, writing config, or touching vectors. Any attempted write or unrecognized old schema stops the task.

- [ ] **Step 2: Run the exact read-only plan command a second time and compare canonical output**

Run the same command again:

```bash
PYTHONPATH=/home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity /home/jiangli/hermes-memory-plugin/.venv/bin/python /home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity/scripts/accept_real_project_continuity_migration.py plan --json
```

Expected: both canonical JSON reports have the same database/config/registry/resolver/generator fingerprints, action list, counts, and `plan_digest`. Capture bounded counts for legacy/mapped/unmapped (including `eva:progress:log:*`), resolved/conflict/unresolved/global, summaries/retention, collision/projection/vector/readiness, proposed run policy, and backup capacity. Include no item content, path, key, L1, or L2.

- [ ] **Step 3: Render the concrete apply command from the actual digest**

Build the approval packet in memory from the two parsed plan objects. Use this exact construction:

```python
import re
import shlex
import subprocess
from pathlib import Path


def render_apply_command(
    first_plan: dict[str, object],
    second_plan: dict[str, object],
    deployment_candidate_commit: str,
) -> str:
    selected_root = Path(
        "/home/jiangli/hermes-memory-plugin/.worktrees/"
        "evolvmem-project-continuity"
    ).resolve(strict=True)
    script = (
        selected_root / "scripts/accept_real_project_continuity_migration.py"
    ).resolve(strict=True)
    if script.parent.parent != selected_root:
        raise RuntimeError("real_wrapper_outside_selected_worktree")
    selected_head = subprocess.check_output(
        ["git", "-C", str(selected_root), "rev-parse", "HEAD"],
        text=True,
    ).strip()
    if re.fullmatch(r"[0-9a-f]{40}", deployment_candidate_commit) is None:
        raise RuntimeError("deployment_candidate_commit_invalid")
    if selected_head != deployment_candidate_commit:
        raise RuntimeError("selected_worktree_commit_mismatch")
    if first_plan != second_plan:
        raise RuntimeError("read_only_plan_not_repeatable")
    plan_digest = first_plan.get("plan_digest")
    if not isinstance(plan_digest, str) or re.fullmatch(
        r"[0-9a-f]{64}",
        plan_digest,
    ) is None:
        raise RuntimeError("plan_digest_invalid")
    apply_argv = [
        "/usr/bin/env",
        "PYTHONPATH=" + str(selected_root),
        "/home/jiangli/hermes-memory-plugin/.venv/bin/python",
        str(script),
        "apply",
        "--json",
        "--plan-digest",
        plan_digest,
        "--authorize-real-mutation",
        "PROJECT_CONTINUITY_V1",
    ]
    return shlex.join(apply_argv)


apply_command = render_apply_command(
    first_plan,
    second_plan,
    deployment_candidate_commit,
)
parsed_apply = shlex.split(apply_command)
selected_root_text = (
    "/home/jiangli/hermes-memory-plugin/.worktrees/"
    "evolvmem-project-continuity"
)
assert parsed_apply == [
    "/usr/bin/env",
    "PYTHONPATH=" + selected_root_text,
    "/home/jiangli/hermes-memory-plugin/.venv/bin/python",
    selected_root_text
    + "/scripts/accept_real_project_continuity_migration.py",
    "apply",
    "--json",
    "--plan-digest",
    first_plan["plan_digest"],
    "--authorize-real-mutation",
    "PROJECT_CONTINUITY_V1",
]
```

Print `apply_command` into the approval packet after replacing nothing: it must contain the actual 64-character digest, absolute selected-worktree `PYTHONPATH`, absolute selected-worktree wrapper path, and fixed interpreter, not metasyntax, a shell variable, relative path, shortened digest, or fake all-zero parser-test digest. The equality assertion above must pass before display. Also include the exact externally managed writer stop/restart commands discovered read-only, chosen deployment method, owner-only backup capacity/policy, and wrapper guarantee that `apply` itself owns lock-time replan, run-ID allocation, verified backup, optional key/schema bootstrap, and every maintenance stage.

- [ ] **Step 4: Ask for explicit authorization and end the turn**

Ask the user to approve the displayed `deployment_candidate_commit`, actual digest, exact `apply_command`, writer list/stop/restart commands, backup policy, staged cutover, real fresh-Agent canary, and protected exact-run rollback policy as one bounded scope.

Do not deploy, stop writers, acquire a lock, create a backup, bootstrap schema/key, apply, resume, rollback, switch live config, restart MCP, or write vectors in Task 12. Do not continue to Task 13 without approval of the exact rendered command. Any changed plan/digest/commit/config/writer scope returns to this step and requires new approval.

---

### Task 13: After approval, deploy, externally stop writers, execute the one owning apply command, and verify

**Files:**

- Create or update: `/home/jiangli/fix-records/records/2026-09-01-evolvmem-project-continuity.md`
- Repository integration: only the approved `deployment_candidate_commit` and approved deployment configuration
- Real data/config/vector/backup: only mutations owned by the approved wrapper command and bounded canary operations

**Interfaces:**

- Consumes: the user-approved Task 12 packet, exact rendered `apply_command`, selected commit/WIP decision, external writer-stop commands, owner-only backup policy, and live canary scope.
- Produces: one run whose wrapper owns `lock -> replan -> run ID -> verified backup -> bootstrap -> backfilled -> rolling_up -> archived -> vector_synced -> verified`, verified Core/project/continuity gates, a real one-line handoff, and a truthful repair record.

- [ ] **Step 1: Revalidate the approval tuple without acquiring the cutover lock**

Confirm selected commit, clean deployment source, approved digest text, exact `apply_command`, live config/database fingerprints, HMAC-key fingerprint/status, writer list, backup capacity/policy, and WIP include/exclude decision. Read-only drift returns to Task 12; changed code/config/migration logic returns first to Task 11 and then Task 12. Do not silently regenerate or substitute a plan.

- [ ] **Step 2: Deploy only the approved commit and stop writers externally**

Use the exact deployment method approved in Task 12. A direct worktree deployment changes only the service/MCP command to the reviewed worktree; an integration deployment uses only the separately verified integration commit. Never merge/reset/stash the dirty main checkout during this step.

Run the exact externally managed stop commands printed and approved in Task 12, then confirm the enumerated writers are stopped and new writes are refused. Do not acquire the cutover lock and do not create a backup in this step. Those operations belong exclusively to the real wrapper.

- [ ] **Step 3: Execute exactly one real apply command that owns the entire mutation sequence**

Execute Task 12’s displayed `apply_command` byte-for-byte, once. Do not prepend a manual lock, run-ID allocation, backup, key creation, schema bootstrap, migration subcommand, or vector rebuild.

The wrapper must perform, in this order:

1. acquire the exclusive cutover lock;
2. recompute the old-schema/current-schema plan under lock and require the approved full digest;
3. allocate a unique run ID in memory;
4. create an owner-only run-bound SQLite/config/key backup with SQLite Backup API;
5. reopen the backup and require `quick_check=ok` before any business mutation;
6. apply only approved key/schema bootstrap actions;
7. persist the original plan and run at `planned`;
8. execute backfill, rollup, coverage-aware archive, vector sync, and final verification stages;
9. finish only at `stage=verified,status=completed`.

Expected public output: content-free run ID, approved digest, stage/status, counts, reason codes, and checksums. A digest mismatch or backup/bootstrap failure produces zero business mutation. A later stage failure leaves maintenance incomplete with its exact run/backup preserved; do not improvise a resume. Diagnose, then return any code/config/migration change through Task 11 and a new Task 12 approval. Even an unchanged-code resume must be rendered with the actual run ID/digest and explicitly approved before execution.

- [ ] **Step 4: Verify completed data and require a zero-change second read-only plan**

Require mapping lag zero; every mapped item has exact L0/L1/L2/source/time/status preservation; resolved projects agree; conflicts/unresolved remain blank-project and pending review; each eligible project has one active rolling summary; archive includes only covered sources and holds uncovered sources; active L0/vector counts match; all post-pre semantic epochs belong to the run; focus rows/pointers/checkpoint L2 pass; continuity is ready; and maintenance is `verified/completed`.

Run the exact read-only command:

```bash
PYTHONPATH=/home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity /home/jiangli/hermes-memory-plugin/.venv/bin/python /home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity/scripts/accept_real_project_continuity_migration.py plan --json
```

Expected: zero business actions. Any nonzero action or gate failure means incomplete, not successful.

- [ ] **Step 5: Progress compatibility/shadow/primary only through passing gates and inspect Web v2**

Start at the approved compat/shadow state, compare project filtering and recall, and promote only when every legacy/new primary gate passes. On loopback, verify Core items, projects, rolling-summary status, pending-review queue, workstreams, L0 list, default L1 detail, explicit L2 detail, and read-only legacy projection. Do not expose a LAN listener.

- [ ] **Step 6: Run the authorized real fresh-session canary without replacing operator focus**

Use a dedicated disposable workspace binding/fingerprint. Through live continuity v1, Agent A creates an explicitly named canary, checkpoints randomized objective/current/next/verification, releases lease, and exits. Launch Agent B with the Task 10 memory-disabled bubblewrap isolation and sole input:

```text
继续原任务
```

Audit exact tool source, zero search calls, matching IDs/revisions, repo-anchor validation, state-version claim, one harmless action, and checkpoint revision +1. Repeat with the generic harness if that live transport is approved. Then complete/cancel using current checkpoint/state/focus CAS, verify the disposable focus row is NULL, revoke the disposable binding with registry/row CAS, and retain terminal audit records rather than hard-deleting live evidence.

- [ ] **Step 7: Restart only approved writers and rerun fresh production verification**

Use Task 12’s exact restart commands. Re-run the deployed commit’s full current-Python suite, Python 3.10 uv suite, both temporary harnesses, bounded live status, zero-change read-only plan, Web smoke, and continuity status. If post-run drift makes rollback unsafe, report a forward-repair requirement rather than restoring an unverified backup.

- [ ] **Step 8: Perform final review with a mandatory return gate for every change**

Invoke `superpowers-requesting-code-review` on the deployed diff, run evidence, migration manifest, and canary audit. If review requests any code, config, or migration change, stop without patching production. Author and re-audit a revised plan containing the concrete failing regression and implementation, execute it in the isolated worktree, rerun both temporary harnesses and Python 3.10, obtain fresh independent review, then return to Task 12 for a new actual digest and explicit approval. The existing apply approval is invalid for changed bits.

- [ ] **Step 9: Write the required repair record truthfully**

Read `/home/jiangli/fix-records/README.md` again. The record must contain these literal headings:

```markdown
## 症状
## 排查过程
## 根因
## 修复内容
## 验证
## 遗留事项
```

Under `验证` record exact commands/results plus content-free backup/run IDs and digests, distinguishing temporary tests, Python 3.10, real migration, Web inspection, Codex-to-Codex, and generic-Agent evidence. Every unrun/failed item belongs in `遗留事项` and cannot be described as fixed.

- [ ] **Step 10: Finish only after fresh evidence**

Invoke `superpowers-finishing-a-development-branch` and `superpowers-verification-before-completion`. Freshly run the full current-Python suite, Python 3.10 suite, both isolated temporary acceptance scripts, bounded live gates, zero-change real plan, canary audit checks, and repair-record format check. Report completion only when every required gate has passed; otherwise report the exact incomplete stage and preserved recovery assets.

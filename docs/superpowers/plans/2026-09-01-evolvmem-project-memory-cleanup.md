# EvolvMem Project Memory Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-subagent-driven-development (recommended) or superpowers-executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop creating projectless session logs, deterministically assign historical Context items to canonical projects, put uncertain ownership into an auditable review queue, maintain one rolling project summary per project, and provide a resumable, verified, rollback-safe maintenance workflow without losing L0, L1, L2, or source history.

**Architecture:** A DB-authoritative project registry and pure ProjectResolver normalize every new and historical write. ContextStore.semantic_transaction owns one mutation epoch and journal row per outer semantic transaction. Project rollups are versioned Context items whose relational source closure controls session-summary retention. A five-command maintenance service uses deterministic plans, an exclusive lock, a verified SQLite backup, a persistent writer gate, staged run records, and drift-aware rollback. This plan proves the data foundation in isolated data roots; real-data mutation remains behind the continuity plan approval gate.

**Tech Stack:** Python 3.10+, frozen dataclasses and enums, SQLite and FTS5, sqlite3.Connection.backup, HMAC-SHA-256, canonical JSON and SHA-256, the existing Context Core vector pipeline, pytest, and existing cutover lock and CLI conventions.

**Design source:** docs/superpowers/specs/2026-09-01-evolvmem-project-continuity-design.md

**Plan suite:** Execute this plan first, then docs/superpowers/plans/2026-09-01-evolvmem-context-web-v2.md, then docs/superpowers/plans/2026-09-01-evolvmem-continuity-protocol.md.

## Global Constraints

- Implement only from /home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity on branch feat/evolvmem-project-continuity after invoking superpowers-using-git-worktrees. Do not implement from the dirty main checkout.
- Preserve the dirty-main skill-distillation and session-mining work in README.md, dsh/src/common.js, dsh/src/sweep.js, evolvmem/config.py, evolvmem/context_models.py, evolvmem/context_service.py, evolvmem/context_store.py, evolvmem/mcp_contract.py, evolvmem/mcp_server.py, tests/test_integration.py, tests/test_mcp_protocol.py, plus untracked evolvmem/context_skill.py, evolvmem/session_miner.py, scripts/mine_skill_tasks.py, tests/test_context_skill.py, and tests/test_session_miner.py. Never stage, reset, overwrite, or silently absorb those changes.
- Use superpowers-test-driven-development for every behavior change. Run each newly written test and observe the stated failure before writing production code.
- Use superpowers-systematic-debugging for unexpected failures, superpowers-verification-before-completion before a passing claim, superpowers-requesting-code-review before integration, and superpowers-finishing-a-development-branch at final handoff.
- Keep Python 3.10 compatibility. Public service boundaries use immutable typed requests and results. Adapters, CLIs, MCP handlers, and later Web handlers do not issue SQL or mutate store internals.
- The DB registry is the sole runtime truth after one seed import. Config aliases can create first-plan seed actions; later differences are diagnostics only.
- workspace_path is transient. Persist only an owner-keyed HMAC fingerprint. Never expose an absolute path, Git remote, private key, memory text, archive text, L1, L2, query, token, or traceback in plans, manifests, logs, status, or public errors.
- Every semantic mutation of legacy projection, Context items and layers, sources, registry, resolution, rollup, continuity, or archive lifecycle enters one outer semantic transaction and advances mutation_epoch exactly once. Access telemetry, schema inspection, and maintenance bookkeeping do not advance it.
- Conflict and unresolved decisions keep project empty, create or update a pending item-ID resolution row, retain the existing item status, and remain excluded from normal project retrieval.
- Generate rollup model output outside long SQLite transactions. Generation, validation, or transaction failure preserves the old active project summary. Post-commit vector failure preserves the new summary and marks it vector_dirty.
- Archive a session summary for TTL or count only after its exact Context ID appears in a ready rollup relational source closure, or after an explicit audited administrator disposition. Uncovered evidence remains active with a bounded retry and archive hold.
- Maintenance plan is read-only, including on a legacy-only schema with no workspace key. Apply recomputes the approved digest under the exclusive lock and finishes a verified backup of the original DB/config/key before its first write. An approved missing-key bootstrap is protected by an owner-only external pending marker that every writer treats as a gate; approved schema bootstrap and insertion of the planned run/persistent DB gate then commit in one SQLite transaction before any business stage.
- A run is successful only at stage=verified and status=completed. Resume retries only failed_stage. Rollback addresses an explicit run ID and refuses any epoch, semantic digest, config, or key drift.
- Do not touch the real EvolvMem database, vectors, config, or HMAC key in this plan. Use unique temporary roots. The real wrapper is implemented and parser-tested here but is not executed.
- Before every commit, inspect git status --short and stage only the task files. Never commit databases, vectors, keys, private manifests, temporary roots, or reports containing local paths.
- The repair record required by /home/jiangli/AGENTS.md is written only after final real or explicitly scoped verification. Until then, record real-data work as unverified.

---

### Task 0: Create the isolated execution worktree and preserve dirty WIP evidence

**Files:**

- Read: /home/jiangli/AGENTS.md — complete repository-level instructions
- Read: /home/jiangli/fix-records/README.md — repair-record schema
- Read: .gitignore — .worktrees ignore rule
- Read: docs/superpowers/specs/2026-09-01-evolvmem-project-continuity-design.md — complete approved design
- Read: docs/superpowers/plans/2026-09-01-evolvmem-project-memory-cleanup.md — complete execution plan
- Modify: none

**Interfaces:**

- Consumes: committed main history containing design commit 243eafc and this three-plan suite; the current dirty-main inventory.
- Produces: clean feature worktree, baseline test evidence, and content hashes proving dirty-main WIP was not changed.

- [ ] **Step 1: Capture the dirty-main inventory without staging or cleaning it.**

~~~bash
cd /home/jiangli/hermes-memory-plugin
git status --short
git ls-files --error-unmatch docs/superpowers/specs/2026-09-01-evolvmem-project-continuity-design.md
git ls-files --error-unmatch docs/superpowers/plans/2026-09-01-evolvmem-project-memory-cleanup.md
git ls-files --error-unmatch docs/superpowers/plans/2026-09-01-evolvmem-context-web-v2.md
git ls-files --error-unmatch docs/superpowers/plans/2026-09-01-evolvmem-continuity-protocol.md
git diff --quiet -- docs/superpowers/specs/2026-09-01-evolvmem-project-continuity-design.md docs/superpowers/plans/2026-09-01-evolvmem-project-memory-cleanup.md docs/superpowers/plans/2026-09-01-evolvmem-context-web-v2.md docs/superpowers/plans/2026-09-01-evolvmem-continuity-protocol.md
git diff --cached --quiet -- docs/superpowers/specs/2026-09-01-evolvmem-project-continuity-design.md docs/superpowers/plans/2026-09-01-evolvmem-project-memory-cleanup.md docs/superpowers/plans/2026-09-01-evolvmem-context-web-v2.md docs/superpowers/plans/2026-09-01-evolvmem-continuity-protocol.md
umask 077
wip_state_dir=/home/jiangli/.local/state/evolvmem-project-continuity
install -d -m 700 "$wip_state_dir"
test ! -e "$wip_state_dir/tracked-wip.sha256"
test ! -e "$wip_state_dir/untracked-wip.sha256"
test ! -e "$wip_state_dir/implementation-base.commit"
test ! -e "$wip_state_dir/implementation-head.commit"
git diff HEAD --binary -- README.md dsh/src/common.js dsh/src/sweep.js evolvmem/config.py evolvmem/context_models.py evolvmem/context_service.py evolvmem/context_store.py evolvmem/mcp_contract.py evolvmem/mcp_server.py tests/test_integration.py tests/test_mcp_protocol.py | sha256sum | tee "$wip_state_dir/tracked-wip.sha256"
sha256sum evolvmem/context_skill.py evolvmem/session_miner.py scripts/mine_skill_tasks.py tests/test_context_skill.py tests/test_session_miner.py | tee "$wip_state_dir/untracked-wip.sha256"
git check-ignore -q .worktrees
~~~

Expected: all four design/plan files are tracked and unchanged, git check-ignore exits 0, and status contains the known tracked and untracked WIP. The streamed tracked diff and five untracked files have owner-readable, content-free hash records in the exact state directory; no WIP body is copied anywhere. Do not continue if an expected WIP or committed plan file is missing.

- [ ] **Step 2: Invoke superpowers-using-git-worktrees and create the feature branch.**

~~~bash
cd /home/jiangli/hermes-memory-plugin
git worktree add .worktrees/evolvmem-project-continuity -b feat/evolvmem-project-continuity main
cd /home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity
wip_state_dir=/home/jiangli/.local/state/evolvmem-project-continuity
umask 077
git branch --show-current
git status --short
git merge-base --is-ancestor 243eafc HEAD
test -f docs/superpowers/plans/2026-09-01-evolvmem-project-memory-cleanup.md
git rev-parse HEAD | tee "$wip_state_dir/implementation-base.commit"
~~~

Expected: branch output is feat/evolvmem-project-continuity, status is empty, the ancestor check exits 0, the plan exists, and implementation-base.commit contains the full committed plan-suite HEAD before Task 1.

- [ ] **Step 3: Run the untouched baseline.**

~~~bash
cd /home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q
~~~

Expected: zero failures. Record exact pass and skip counts. A baseline failure stops the plan and triggers systematic debugging.

- [ ] **Step 4: Recheck the source checkout WIP hashes.**

~~~bash
cd /home/jiangli/hermes-memory-plugin
wip_state_dir=/home/jiangli/.local/state/evolvmem-project-continuity
git diff HEAD --binary -- README.md dsh/src/common.js dsh/src/sweep.js evolvmem/config.py evolvmem/context_models.py evolvmem/context_service.py evolvmem/context_store.py evolvmem/mcp_contract.py evolvmem/mcp_server.py tests/test_integration.py tests/test_mcp_protocol.py | sha256sum | diff -u "$wip_state_dir/tracked-wip.sha256" -
sha256sum evolvmem/context_skill.py evolvmem/session_miner.py scripts/mine_skill_tasks.py tests/test_context_skill.py tests/test_session_miner.py | diff -u "$wip_state_dir/untracked-wip.sha256" -
~~~

Expected: both diff commands exit 0, both owner-readable hash records remain for Task 11 verification, and no WIP body was written outside the source checkout. Task 0 has no repository changes and no commit.

---

### Task 1: Define deterministic project resolution and private workspace identity

**Files:**

- Create: evolvmem/project_models.py — ProjectResolutionRequest, ProjectResolutionDecision, ProjectRegistrySnapshot, ProjectSignal
- Create: evolvmem/workspace_identity.py — WorkspaceIdentityProvider, WorkspaceIdentity, WorkspaceKeyStatus
- Create: evolvmem/project_resolver.py — ProjectResolver.resolve
- Create: tests/test_workspace_identity.py — workspace-key and fingerprint cases
- Create: tests/test_project_resolver.py — precedence and conflict matrix

**Interfaces:**

- Consumes: structured key, tags, scope, content type, source session/version, trusted archive project/version, project hint, registry aliases, workspace bindings, and transient workspace path.
- Produces: WorkspaceIdentityProvider(key_path: Path, expected_key_fingerprint: str=""); WorkspaceIdentityProvider.bootstrap_key() -> WorkspaceKeyStatus; WorkspaceIdentityProvider.status() -> WorkspaceKeyStatus; WorkspaceIdentityProvider.resolve(workspace_path: str) -> WorkspaceIdentity; WorkspaceIdentityProvider.digest_private(domain: str, payload: bytes) -> str; ProjectResolver.resolve(request: ProjectResolutionRequest, registry: ProjectRegistrySnapshot) -> ProjectResolutionDecision.

- [ ] **Step 1: Write the failing private-identity tests.**

~~~python
import os

import pytest

from evolvmem.workspace_identity import WorkspaceIdentityError, WorkspaceIdentityProvider


def test_private_digest_is_domain_separated_and_missing_key_fails_closed(tmp_path):
    key_path = tmp_path / "workspace-hmac.key"
    key_path.write_bytes(b"k" * 32)
    os.chmod(key_path, 0o600)
    provider = WorkspaceIdentityProvider(key_path=key_path)

    first = provider.digest_private("workspace.identity.v1", b"same-payload")
    second = provider.digest_private("continuity.repo.worktree.v1", b"same-payload")

    assert first.startswith("hmac-sha256:")
    assert second.startswith("hmac-sha256:")
    assert first != second
    missing = WorkspaceIdentityProvider(key_path=tmp_path / "missing.key")
    with pytest.raises(WorkspaceIdentityError, match="workspace_key_missing"):
        missing.resolve(str(tmp_path))
    assert not (tmp_path / "missing.key").exists()
~~~

Also test owner-only explicit bootstrap, unsafe permissions, changed-key fingerprint refusal, Git common-dir stability across worktrees, distinct non-Git directories, a moved directory producing a new fingerprint, and no path or remote in repr, result, or public status.

- [ ] **Step 2: Run the identity test and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_workspace_identity.py
~~~

Expected failure: ModuleNotFoundError for evolvmem.workspace_identity.

- [ ] **Step 3: Implement explicit key bootstrap and domain-separated private HMAC.**

~~~python
def bootstrap_key(self) -> WorkspaceKeyStatus:
    self._key_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(self._key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return self.status()
    key = secrets.token_bytes(32)
    try:
        os.write(fd, key)
        os.fsync(fd)
    finally:
        os.close(fd)
    return self.status()


def digest_private(self, domain: str, payload: bytes) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", domain):
        raise WorkspaceIdentityError("invalid_hmac_domain")
    key = self._load_established_owner_only_key()
    domain_bytes = domain.encode("ascii")
    framed = (
        len(domain_bytes).to_bytes(2, "big")
        + domain_bytes
        + len(payload).to_bytes(8, "big")
        + payload
    )
    digest = hmac.new(key, framed, hashlib.sha256).hexdigest()
    return f"hmac-sha256:{digest}"
~~~

WorkspaceIdentityProvider.resolve must call status and never bootstrap. Git identity is the resolved git-common-dir identity; non-Git identity is the resolved directory. Pass the canonical bytes directly to digest_private with domain workspace.identity.v1, then discard the bytes and path. bootstrap_key is called only by an explicitly approved maintenance action in Task 9.

status validates owner, mode, exact 32-byte length, and current SHA-256 key fingerprint. If expected_key_fingerprint is nonempty and differs, status returns workspace_key_changed and resolve/digest_private fail closed. Task 2 stores the established fingerprint in context_state_meta; Task 9 writes it only after the approved key-bootstrap action.

- [ ] **Step 4: Run the identity tests and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_workspace_identity.py
~~~

Expected: all tests in tests/test_workspace_identity.py pass.

- [ ] **Step 5: Write the failing resolver matrix tests.**

~~~python
from evolvmem.project_models import (
    ProjectRegistrySnapshot,
    ProjectResolutionRequest,
    ProjectResolutionState,
    WorkspaceBindingSnapshot,
)
from evolvmem.project_resolver import ProjectResolver


def test_resolver_requires_two_medium_signals_and_never_guesses_conflict():
    registry = ProjectRegistrySnapshot(
        projects=("eva", "hermes"),
        aliases=(("evolv", "eva"),),
        bindings=(
            WorkspaceBindingSnapshot(
                workspace_fingerprint="hmac-sha256:" + "1" * 64,
                project="eva",
                state="candidate",
                is_default=False,
            ),
        ),
        generic_names=("home", "jiangli", "project", "src", "workspace"),
        revision=7,
    )
    request = ProjectResolutionRequest(
        content_type="session_summary",
        scope="project",
        key="project:evolv:progress:log:1",
        tags=("分类:evolv",),
        source_session="session_1",
        source_version="kimi-v3",
        project_hint="hermes",
        workspace_fingerprint="hmac-sha256:" + "1" * 64,
    )

    decision = ProjectResolver().resolve(request, registry)

    assert decision.state is ProjectResolutionState.CONFLICT
    assert decision.resolved_project == ""
    assert decision.proposed_project == ""
    assert all(set(row) == {"source", "type", "source_version", "normalized_value"} for row in decision.evidence)
~~~

Add cases for active-default binding, matching active hint, trusted archive project, trusted typed project, all four medium signal forms, two-source agreement, alias collision, candidate-only binding, multiple active bindings without default, hint mismatch, known coarse-cwd generator versions, generic names, global content, bounded evidence ordering, and a resolver exception that preserves an existing project during repair analysis while exposing only resolver_error.

- [ ] **Step 6: Run the resolver test and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_resolver.py
~~~

Expected failure: ImportError for ProjectResolver or missing project model types.

- [ ] **Step 7: Implement the pure precedence algorithm.**

~~~python
def resolve(
    self,
    request: ProjectResolutionRequest,
    registry: ProjectRegistrySnapshot,
) -> ProjectResolutionDecision:
    if request.scope == "global" or request.content_type in {
        "constraint",
        "preference",
        "user_profile",
    }:
        return ProjectResolutionDecision.global_decision(self.VERSION)
    signals = self._canonical_signals(request, registry)
    trusted = tuple(signal for signal in signals if signal.trust != "ignored")
    names = {signal.normalized_value for signal in trusted if signal.normalized_value}
    evidence = tuple(signal.public_evidence() for signal in sorted(trusted, key=ProjectSignal.sort_key))
    if len(names) > 1:
        return ProjectResolutionDecision.conflict(self.VERSION, evidence)
    if not names:
        return ProjectResolutionDecision.unresolved(self.VERSION, evidence)
    project = next(iter(names))
    strong = tuple(signal for signal in trusted if signal.trust == "strong")
    medium_sources = {signal.type for signal in trusted if signal.trust == "medium"}
    if strong:
        return ProjectResolutionDecision.resolved(project, "strong", self.VERSION, evidence)
    if len(medium_sources) >= 2:
        method = "+".join(sorted(medium_sources))
        return ProjectResolutionDecision.resolved(project, method, self.VERSION, evidence)
    return ProjectResolutionDecision.unresolved(self.VERSION, evidence)
~~~

ProjectResolutionRequest must include archive_project="" and archive_source_version="" in addition to content_type, scope, key, tags, source_session, source_version, project_hint="", and workspace_fingerprint="". Internal project_summary and later continuity types require an explicit validated project and bypass historical heuristics.

- [ ] **Step 8: Run both Task 1 suites and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_workspace_identity.py tests/test_project_resolver.py
~~~

Expected: both files pass with stable ordering across repeated runs.

- [ ] **Step 9: Commit Task 1.**

~~~bash
git diff --check
git status --short
git add evolvmem/project_models.py evolvmem/workspace_identity.py evolvmem/project_resolver.py tests/test_workspace_identity.py tests/test_project_resolver.py
git commit -m "feat: add deterministic project resolution"
~~~

Expected: one commit containing only the five Task 1 files.

---

### Task 2: Journal every semantic mutation exactly once

**Files:**

- Create: evolvmem/mutation_state.py — MutationDriftReport and semantic table projections
- Create: tests/test_mutation_state.py — nesting, rollback, telemetry, and bypass cases
- Modify: evolvmem/context_store.py — ContextStore.transaction, ContextStore.create_schema_in_transaction, ContextStore.update_access, ContextStore semantic helpers
- Modify: evolvmem/context_service.py — ContextService.legacy_add, ContextService.persist_legacy_extraction, ContextService.record_outcome
- Modify: evolvmem/session_archive.py — SessionArchiver.archive_session, SessionArchiver._purge_rows
- Modify: tests/test_context_store.py — transaction fixture and access-accounting tests near ContextStore.update_access coverage
- Modify: tests/test_context_service.py — test_extraction_batch_commits_summary_and_candidates_in_one_outer_transaction and lifecycle write tests
- Modify: tests/test_session_archive.py — test_db_failure_removes_new_payload_and_keeps_previous_archive
- Modify: tests/test_production_write_boundaries.py — test_production_modules_have_no_raw_write_bypasses

**Interfaces:**

- Consumes: existing nested ContextStore.transaction calls and every committed ContextService, compatibility, and SessionArchiver semantic writer. The dirty-main skill and session-miner writers remain outside this branch until the explicit Task 11 handoff.
- Produces: ContextStore.semantic_transaction(kind: str, *, owner_run_id: str | None = None) -> ContextManager[ContextStore]; ContextStore.require_semantic_transaction(operation: str) -> None; ContextStore.current_mutation_epoch() -> int; ContextStore.canonical_state_digest() -> str; ContextStore.mutation_journal_rows(*, epoch: int) -> Sequence[MutationJournalEntry]; ContextStore.detect_unjournaled_mutation(expected_epoch: int, expected_digest: str) -> MutationDriftReport.

- [ ] **Step 1: Write failing epoch, rollback, and telemetry tests.**

~~~python
import pytest

from evolvmem.context_models import ContextScope, ContextStatus
from evolvmem.mutation_state import SEMANTIC_PROJECTIONS


def test_nested_semantic_transaction_advances_once_and_access_is_not_semantic(store, make_draft):
    before = store.current_mutation_epoch()
    with store.semantic_transaction("test.batch"):
        item = store.create_item(make_draft("epoch-one"))
        with store.semantic_transaction("test.inner"):
            store.set_item_status(item.id, ContextStatus.ACTIVE)
    after = store.current_mutation_epoch()
    digest = store.canonical_state_digest()

    store.update_access([item.id])

    assert after == before + 1
    assert store.canonical_state_digest() == digest
    journal = store.mutation_journal_rows(epoch=after)
    assert len(journal) == 1
    assert journal[0].epoch == after
    assert journal[0].owner_run_id is None
    assert journal[0].kind == "test.batch"
    assert len(journal[0].state_digest) == 64


def test_semantic_exception_rolls_back_item_epoch_and_journal(store, make_draft):
    before = store.current_mutation_epoch()
    with pytest.raises(RuntimeError, match="injected"):
        with store.semantic_transaction("test.rollback"):
            store.create_item(make_draft("rolled-back"))
            raise RuntimeError("injected")
    assert store.current_mutation_epoch() == before
    assert store.get_by_identity(
        "rolled-back",
        project="",
        scope=ContextScope.PROJECT,
    ) == []


def test_semantic_projection_inventory_covers_legacy_core_and_archive_tables():
    assert {
        "memories",
        "context_items",
        "context_layers",
        "context_sources",
        "context_evidence",
        "session_archives",
        "legacy_memory_migrations",
    } <= set(SEMANTIC_PROJECTIONS)
~~~

Also cover two outer transactions, schema bootstrap, maintenance bookkeeping, access telemetry on legacy and Core, persisted workspace_key_fingerprint, and an injected direct semantic-table update detected without leaking row content.

- [ ] **Step 2: Run the mutation tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_mutation_state.py tests/test_context_store.py -k "semantic or epoch or journal or access"
~~~

Expected failure: AttributeError for ContextStore.current_mutation_epoch or semantic_transaction.

- [ ] **Step 3: Implement the outer transaction and telemetry-free digest.**

~~~sql
CREATE TABLE IF NOT EXISTS context_state_meta (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id=1),
    mutation_epoch INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    workspace_key_fingerprint TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS context_mutation_journal (
    epoch INTEGER PRIMARY KEY,
    owner_run_id TEXT,
    kind TEXT NOT NULL,
    state_digest TEXT NOT NULL,
    created_at TEXT NOT NULL
);
~~~

~~~python
@dataclass(frozen=True, slots=True)
class MutationJournalEntry:
    epoch: int
    owner_run_id: str | None
    kind: str
    state_digest: str
    created_at: str


@contextmanager
def semantic_transaction(
    self,
    kind: str,
    *,
    owner_run_id: str | None = None,
) -> Iterator["ContextStore"]:
    outer = self._semantic_depth == 0
    if outer:
        self._semantic_kind = kind
        self._semantic_owner_run_id = owner_run_id
    self._semantic_depth += 1
    try:
        with self.transaction():
            yield self
            if outer:
                digest = self.canonical_state_digest()
                next_epoch = self.current_mutation_epoch() + 1
                self._connection().execute(
                    "UPDATE context_state_meta SET mutation_epoch=?, updated_at=? WHERE singleton_id=1",
                    (next_epoch, _now_iso()),
                )
                self._connection().execute(
                    "INSERT INTO context_mutation_journal(epoch,owner_run_id,kind,state_digest,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (next_epoch, owner_run_id, kind, digest, _now_iso()),
                )
    finally:
        self._semantic_depth -= 1
        if outer:
            self._semantic_kind = None
            self._semantic_owner_run_id = None


def require_semantic_transaction(self, operation: str) -> None:
    if self._semantic_depth == 0 or self._transaction_depth == 0:
        raise RuntimeError(f"{operation} requires an active semantic transaction")


def mutation_journal_rows(self, *, epoch: int) -> Sequence[MutationJournalEntry]:
    rows = self._connection().execute(
        "SELECT epoch,owner_run_id,kind,state_digest,created_at "
        "FROM context_mutation_journal WHERE epoch=? ORDER BY epoch",
        (epoch,),
    ).fetchall()
    return tuple(
        MutationJournalEntry(
            epoch=row["epoch"],
            owner_run_id=row["owner_run_id"],
            kind=row["kind"],
            state_digest=row["state_digest"],
            created_at=row["created_at"],
        )
        for row in rows
    )
~~~

In mutation_state.py define constant, reviewed projections. The context_items projection includes semantic columns but omits access_count and last_accessed. The legacy memories projection likewise omits access counters. Exclude context_state_meta, context_mutation_journal, context_maintenance_runs, FTS tables, vector metadata, and diagnostics.

~~~python
SEMANTIC_PROJECTIONS = {
    "memories": (
        "id", "key", "value", "status", "attribute", "tags",
        "source_session", "supersedes", "superseded_by", "created_at",
        "updated_at", "importance", "tier", "expires_at",
    ),
    "context_items": (
        "id", "identity_key", "content_type", "project", "scope", "status",
        "tier", "tags", "importance", "confidence", "source_state",
        "source_count", "success_count", "failure_count", "last_verified_at",
        "expires_at", "supersedes", "superseded_by", "created_at", "updated_at",
    ),
    "context_layers": (
        "id", "item_id", "layer", "content", "content_hash", "generator",
        "created_at", "updated_at",
    ),
    "context_sources": (
        "id", "item_id", "archive_id", "source_kind", "source_ref",
        "extraction_version", "created_at",
    ),
    "context_evidence": (
        "id", "item_id", "source_id", "outcome", "note", "observed_at",
        "created_at",
    ),
    "session_archives": (
        "id", "project", "adapter", "external_session_id", "payload_path",
        "payload_sha256", "state", "expires_at", "purged_at", "created_at",
    ),
    "legacy_memory_migrations": (
        "legacy_memory_id", "context_item_id", "migrated_at",
    ),
}


def canonical_projection_digest(conn: sqlite3.Connection) -> str:
    payload: list[tuple[str, tuple]] = []
    installed = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for table, columns in sorted(SEMANTIC_PROJECTIONS.items()):
        if table not in installed:
            continue
        selected = ",".join(columns)
        rows = tuple(tuple(row) for row in conn.execute(f"SELECT {selected} FROM {table} ORDER BY 1"))
        payload.append((table, rows))
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
~~~

Each later schema task must modify mutation_state.py and add its exact semantic columns to this reviewed constant in the same commit that creates the table. detect_unjournaled_mutation compares the caller snapshot with current epoch and digest and emits only stable reason codes and digests. require_semantic_transaction is the same-connection precondition used by ProjectStore, rollup, lifecycle, and continuity helpers; it never opens or commits a transaction.

- [ ] **Step 4: Run the focused tests and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_mutation_state.py tests/test_context_store.py -k "semantic or epoch or journal or access"
~~~

Expected: the focused mutation and telemetry tests pass.

- [ ] **Step 5: Write failing production-boundary coverage tests.**

~~~python
@pytest.mark.parametrize(
    "operation_name",
    (
        "legacy_add",
        "legacy_replace",
        "legacy_remove",
        "legacy_update",
        "legacy_archive",
        "legacy_restore",
        "legacy_hard_delete",
        "persist_legacy_extraction",
        "confirm",
        "record_outcome",
        "archive_session",
        "sweep_archives",
    ),
)
def test_each_production_semantic_boundary_owns_one_epoch(boundary_harness, operation_name):
    before = boundary_harness.store.current_mutation_epoch()
    boundary_harness.invoke(operation_name)
    assert boundary_harness.store.current_mutation_epoch() == before + 1
~~~

The harness must also assert a summary-plus-atomics extraction batch, supersede-plus-create, source linking, legacy projection update, and archive hold work each own one outer epoch rather than one epoch per inner store call.

- [ ] **Step 6: Run the boundary tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_mutation_state.py tests/test_context_service.py tests/test_session_archive.py tests/test_production_write_boundaries.py
~~~

Expected failure: at least one semantic writer leaves the epoch unchanged or increments it more than once.

- [ ] **Step 7: Wire every existing semantic boundary.**

Use one shared helper so vector work stays post-commit while every legacy/Core row mutation uses the ContextStore-owned connection:

~~~python
from collections.abc import Callable
from typing import TypeVar


T = TypeVar("T")


def _commit_semantic_write(
    self,
    kind: str,
    write: Callable[[], tuple[T, _VectorAftermath]],
) -> T:
    with self._cutover_lock.shared():
        with self.store.semantic_transaction(kind):
            result, aftermath = write()
    self._apply_vector_aftermath(aftermath)
    return result


def legacy_add(self, request: LegacyAddRequest) -> LegacyMutationResult:
    self._require_request(request, LegacyAddRequest)
    self._require_initialized()

    def write() -> tuple[LegacyMutationResult, _VectorAftermath]:
        if self._mode is not ContextMode.LEGACY:
            return self._add_dual_in_transaction(request)
        legacy_id = self.store.legacy_projection().insert(
            LegacyProjectionInsert(
                key=request.key,
                value=request.value,
                attribute=request.attribute,
                tags=request.tags,
                source_session=request.source_session,
                importance=request.importance,
                tier=request.tier,
                expires_at=request.expires_at,
            )
        )
        return (
            LegacyMutationResult(
                legacy_id=legacy_id,
                context_id=None,
                old_legacy_id=None,
                old_context_id=None,
                available_layers=(),
                changed=True,
            ),
            _VectorAftermath(legacy_upserts=((legacy_id, request.value),)),
        )

    return self._commit_semantic_write("legacy.add", write)
~~~

Apply the same outer-helper shape to legacy_replace, legacy_remove, legacy_update, legacy_archive, legacy_restore, legacy_hard_delete, persist_legacy_extraction, confirm, and record_outcome, with stable kinds matching those method names. SessionArchiver.archive_session and a whole sweep/purge batch use `session_archive.archive` and `session_archive.purge` respectively; their ContextStore helpers call require_semantic_transaction and never commit. Store helpers join the active transaction. Access accounting remains an ordinary transaction. Extend the exact production-boundary inventory when Tasks 3 through 10 add writers.

- [ ] **Step 8: Run the production-boundary tests and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_mutation_state.py tests/test_context_store.py tests/test_context_service.py tests/test_legacy_compat.py tests/test_session_archive.py tests/test_production_write_boundaries.py
~~~

Expected: all listed tests pass and the injected bypass produces semantic_digest_changed_without_epoch.

- [ ] **Step 9: Commit Task 2.**

~~~bash
git diff --check
git status --short
git add evolvmem/mutation_state.py evolvmem/context_store.py evolvmem/context_service.py evolvmem/session_archive.py tests/test_mutation_state.py tests/test_context_store.py tests/test_context_service.py tests/test_session_archive.py tests/test_production_write_boundaries.py
git commit -m "feat: journal semantic mutation epochs"
~~~

Expected: one commit containing only Task 2 files.

---

### Task 3: Persist the versioned registry, bindings, and resolution audit trail

**Files:**

- Create: evolvmem/project_store.py — ProjectStore schema, repositories, and CAS helpers
- Create: evolvmem/project_service.py — ProjectService operator and resolution boundaries
- Create: evolvmem/project_cursor.py — authenticated ProjectCursorAnchor codec and filter binding
- Create: tests/test_project_store.py — schema, revision, and event invariants
- Create: tests/test_project_service.py — double-CAS and decision-record sequencing
- Create: tests/test_project_cursor.py — tamper, filter, endpoint, length, and equal-timestamp pagination cases
- Modify: evolvmem/project_models.py — exact ProjectService request, page, record, evidence, and review-action DTO constructors consumed by Web Task 7
- Modify: evolvmem/context_models.py — ContextItem.row_version
- Modify: evolvmem/context_store.py — ContextStore.create_schema_in_transaction, ContextStore._row_to_item, semantic projections
- Modify: evolvmem/mutation_state.py — row_version plus exact registry, binding, resolution, and audit-event projections
- Modify: tests/test_context_models.py — test_retrieval_record_requires_a_typed_item_and_typed_layers
- Modify: tests/test_context_store.py — test_create_schema_in_transaction_is_idempotent and test_update_item_from_legacy_mirrors_only_supplied_metadata
- Modify: tests/test_mutation_state.py — test_project_tables_and_item_row_version_are_semantic

**Interfaces:**

- Consumes: Task 1 ProjectResolver and WorkspaceIdentityProvider; Task 2 semantic transactions; current ContextItem schema; optional ProjectBindingLifecyclePort injected when continuity tables are installed.
- Produces: ProjectBindingLifecyclePort.ensure_focus_for_active_binding(*, project: str, workspace_fingerprint: str) -> None and clear_focus_for_inactive_binding(*, project: str, workspace_fingerprint: str) -> None; ProjectStore(context_store: ContextStore, *, binding_lifecycle: ProjectBindingLifecyclePort | None = None); ProjectStore.bind_workspace_row(*, workspace_fingerprint: str, project: str, state: str, is_default: bool, method: str, expected_registry_revision: int, expected_row_revision: int, actor_hash: str, run_id: str) -> ProjectWorkspaceBindingRecord; ProjectStore.revoke_workspace_row(*, workspace_fingerprint: str, project: str, expected_registry_revision: int, expected_row_revision: int, actor_hash: str, run_id: str) -> ProjectWorkspaceBindingRecord; ProjectStore.get_workspace_binding(workspace_fingerprint: str, project: str) -> ProjectWorkspaceBindingRecord | None; ProjectCursorCodec(identity: WorkspaceIdentityProvider); ProjectCursorCodec.encode(endpoint: str, filters: Sequence[tuple[str, str]], anchor: ProjectCursorAnchor) -> str; ProjectCursorCodec.decode(token: str, endpoint: str, filters: Sequence[tuple[str, str]]) -> ProjectCursorAnchor; ProjectService.list_projects(request: ProjectPageRequest) -> ProjectPage; register_project(request: RegisterProjectRequest) -> ProjectRecord; archive_registry_project(request: ArchiveProjectRequest) -> ProjectRecord; list_aliases(request: ProjectAliasPageRequest) -> ProjectAliasPage; register_alias(request: RegisterProjectAliasRequest) -> ProjectAliasRecord; revoke_alias(request: RevokeProjectAliasRequest) -> ProjectAliasRecord; list_workspace_bindings(request: WorkspaceBindingPageRequest) -> WorkspaceBindingPage; bind_workspace(request: BindProjectWorkspaceRequest) -> ProjectWorkspaceBindingRecord; revoke_workspace_binding(request: RevokeProjectWorkspaceRequest) -> ProjectWorkspaceBindingRecord; set_default_workspace_binding(request: SetDefaultProjectWorkspaceRequest) -> ProjectWorkspaceBindingRecord; resolve_workspace(request: ResolveWorkspaceRequest) -> WorkspaceProjectResolution; decide_item(request: ResolveProjectItemRequest) -> ProjectResolutionDecision; record_item_resolution(item_id: int, decision: ProjectResolutionDecision, run_id: str) -> ProjectResolutionRecord; list_resolutions(request: ResolutionPageRequest) -> ResolutionPage; review_resolution(request: ProjectResolutionReviewRequest) -> ProjectResolutionRecord. decide_item is read-only and runs before item insertion; record_item_resolution requires the inserted item ID and joins the caller transaction. Binding activation calls ensure_focus_for_active_binding after its row CAS; binding revoke and project archive call clear_focus_for_inactive_binding before their audit event; every call remains inside the same semantic transaction.

The prerequisite DTO constructors consumed by Web Task 7 are frozen exactly here:

| DTO | Exact constructor fields in order |
|---|---|
| ProjectPageRequest | status: str="", limit: int=50, cursor: str="" |
| RegisterProjectRequest | project: str, expected_registry_revision: int, expected_row_revision: int, actor: str, run_id: str |
| ArchiveProjectRequest | project: str, expected_registry_revision: int, expected_row_revision: int, actor: str, run_id: str |
| ProjectRecord | project: str, status: str, revision: int, registry_revision: int, created_at: str, updated_at: str |
| ProjectPage | items: Sequence[ProjectRecord], next_cursor: str, has_more: bool, registry_revision: int |
| ProjectAliasPageRequest | project: str="", limit: int=50, cursor: str="" |
| RegisterProjectAliasRequest | alias: str, project: str, expected_registry_revision: int, expected_row_revision: int, actor: str, run_id: str |
| RevokeProjectAliasRequest | alias: str, expected_registry_revision: int, expected_row_revision: int, actor: str, run_id: str |
| ProjectAliasRecord | alias: str, project: str, revision: int, registry_revision: int, created_at: str, updated_at: str |
| ProjectAliasPage | items: Sequence[ProjectAliasRecord], next_cursor: str, has_more: bool, registry_revision: int |
| WorkspaceBindingPageRequest | project: str="", workspace_fingerprint: str="", state: str="", limit: int=50, cursor: str="" |
| BindProjectWorkspaceRequest | workspace_fingerprint: str, project: str, state: str, is_default: bool, method: str, expected_registry_revision: int, expected_row_revision: int, actor: str, run_id: str |
| RevokeProjectWorkspaceRequest | workspace_fingerprint: str, project: str, expected_registry_revision: int, expected_row_revision: int, actor: str, run_id: str |
| SetDefaultProjectWorkspaceRequest | workspace_fingerprint: str, project: str, expected_registry_revision: int, expected_row_revision: int, actor: str, run_id: str |
| ProjectWorkspaceBindingRecord | workspace_fingerprint: str, project: str, state: str, is_default: bool, method: str, revision: int, registry_revision: int, created_at: str, updated_at: str |
| WorkspaceBindingPage | items: Sequence[ProjectWorkspaceBindingRecord], next_cursor: str, has_more: bool, registry_revision: int |
| ResolutionPageRequest | review_state: str="", resolution_state: Optional[ProjectResolutionState]=None, project: str="", limit: int=50, cursor: str="" |
| ProjectResolutionEvidence | source: str, type: str, source_version: str, normalized_value: str |
| ProjectResolutionRecord | item_id: int, resolution_state: ProjectResolutionState, decision_source: str, review_state: str, proposed_project: str, resolved_project: str, previous_project: str, confidence: str, method: str, evidence: Sequence[ProjectResolutionEvidence], resolver_version: str, revision: int, item_row_version: int, reviewed_at: Optional[str], created_at: str, updated_at: str |
| ResolutionPage | items: Sequence[ProjectResolutionRecord], next_cursor: str, has_more: bool |
| ProjectResolutionReviewAction | enum values ACCEPT_PROJECT="accept_project", REJECT_PROJECT="reject_project", CONFIRM_GLOBAL="confirm_global", IGNORE="ignore" |
| ProjectResolutionReviewRequest | item_id: int, action: ProjectResolutionReviewAction, project: Optional[str], expected_revision: int, expected_item_row_version: Optional[int], actor: str, run_id: str |

All sequences are normalized to immutable tuples in __post_init__. Page limits accept 1 through 100. Each page fetches limit+1 rows, sets has_more from the extra row, and emits empty next_cursor on the last page. ResolutionPageRequest.resolution_state is None for no filter or a typed ProjectResolutionState value. expected_row_revision=0 means a project, alias, or binding create target must not exist; positive values mean exact row CAS. Review action validation requires a nonempty project and positive expected_item_row_version only for ACCEPT_PROJECT; the other three actions require project=None and expected_item_row_version=None. ProjectService hashes actor with the private HMAC domain project.resolution.reviewer.v1. The database current-state row and append-only event retain run_id and reviewed_by_hash for provenance and rollback, while ProjectResolutionRecord exposes reviewed_at but deliberately omits reviewer hash and run ID so the Web service cannot serialize them.

- [ ] **Step 1: Write failing schema and old-row migration tests.**

~~~python
def test_row_version_migration_sets_old_rows_to_one_and_is_idempotent(old_context_db, config):
    store = ContextStore(config)
    store.initialize()
    first = store.get_item(old_context_db.item_id, include_layers=False)
    store.create_schema_in_transaction()
    second = store.get_item(old_context_db.item_id, include_layers=False)

    assert first is not None
    assert first.row_version == 1
    assert second is not None
    assert second.row_version == 1


def test_registry_constraints_reject_two_active_defaults(project_store, fingerprint):
    project_store.bind(fingerprint, "eva", state="active", is_default=True, revision=1)
    with pytest.raises(ProjectConflictError, match="workspace_default_conflict"):
        project_store.bind(fingerprint, "hermes", state="active", is_default=True, revision=1)


def test_resolution_current_row_schema_retains_private_run_provenance(
    project_store,
    context_store,
):
    columns = {
        row["name"]
        for row in context_store._connection().execute(
            "PRAGMA table_info(context_project_resolutions)"
        )
    }
    assert {"run_id", "reviewed_by_hash", "reviewed_at"} <= columns


def test_project_tables_and_item_row_version_are_semantic():
    assert "row_version" in SEMANTIC_PROJECTIONS["context_items"]
    assert {
        "context_project_registry",
        "context_project_aliases",
        "context_project_workspace_bindings",
        "context_project_registry_meta",
        "context_project_registry_events",
        "context_project_resolutions",
        "context_project_resolution_events",
    } <= set(SEMANTIC_PROJECTIONS)
~~~

Assert all registry, alias, binding, meta, append-only event, resolution, and resolution-event tables; globally unique alias; foreign keys and CHECK constraints; candidate, active, and revoked binding states; one active default per fingerprint; pending-resolution indexes; and idempotent bootstrap. Assert each semantic item metadata, lifecycle, source-state, outcome, supersession, or project update increments row_version once, while access_count and last_accessed telemetry do not.

- [ ] **Step 2: Run the schema tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_store.py tests/test_context_store.py tests/test_mutation_state.py -k "project or row_version or bootstrap"
~~~

Expected failure: ProjectStore import failure and ContextItem has no row_version.

- [ ] **Step 3: Implement the schema migration and item CAS.**

~~~python
def _ensure_context_item_row_version(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(context_items)")}
    if "row_version" not in columns:
        conn.execute(
            "ALTER TABLE context_items ADD COLUMN row_version INTEGER NOT NULL DEFAULT 1"
        )


def update_item_project(
    self,
    item_id: int,
    project: str,
    expected_row_version: int,
) -> int:
    cursor = self._conn.execute(
        "UPDATE context_items "
        "SET project=?, row_version=row_version+1, updated_at=? "
        "WHERE id=? AND row_version=?",
        (project, _now_iso(), item_id, expected_row_version),
    )
    if cursor.rowcount != 1:
        raise ProjectConflictError("stale_item_row_version")
    return expected_row_version + 1


PROJECT_RESOLUTION_DDL = """
CREATE TABLE IF NOT EXISTS context_project_resolutions (
    item_id INTEGER PRIMARY KEY REFERENCES context_items(id),
    resolution_state TEXT NOT NULL
        CHECK (resolution_state IN ('resolved','conflict','unresolved','global','ignored')),
    decision_source TEXT NOT NULL
        CHECK (decision_source IN ('automatic','human','none')),
    review_state TEXT NOT NULL
        CHECK (review_state IN ('not_required','pending','accepted','rejected')),
    proposed_project TEXT NOT NULL DEFAULT '',
    resolved_project TEXT NOT NULL DEFAULT '',
    previous_project TEXT NOT NULL DEFAULT '',
    confidence TEXT NOT NULL CHECK (confidence IN ('high','medium','none')),
    method TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    resolver_version TEXT NOT NULL,
    run_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    reviewed_by_hash TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""


SEMANTIC_PROJECTIONS["context_items"] = (
    *SEMANTIC_PROJECTIONS["context_items"],
    "row_version",
)
SEMANTIC_PROJECTIONS.update(
    {
        "context_project_registry": (
            "project", "status", "revision", "created_at", "updated_at",
        ),
        "context_project_aliases": (
            "alias", "project", "revision", "created_at", "updated_at",
        ),
        "context_project_workspace_bindings": (
            "workspace_fingerprint", "project", "state", "is_default",
            "method", "revision", "created_at", "updated_at",
        ),
        "context_project_registry_meta": (
            "singleton_id", "revision", "updated_at",
        ),
        "context_project_registry_events": (
            "id", "entity_kind", "entity_key", "action", "previous_revision",
            "new_revision", "actor_hash", "run_id", "created_at",
        ),
        "context_project_resolutions": (
            "item_id", "resolution_state", "decision_source", "review_state",
            "proposed_project", "resolved_project", "previous_project",
            "confidence", "method", "evidence_json", "resolver_version",
            "run_id", "revision", "reviewed_by_hash", "reviewed_at",
            "created_at", "updated_at",
        ),
        "context_project_resolution_events": (
            "id", "item_id", "previous_revision", "new_revision",
            "previous_resolution_state", "new_resolution_state", "action",
            "actor_hash", "run_id", "evidence_digest", "created_at",
        ),
    }
)
~~~

Use the same row_version=row_version+1 rule in ContextStore.set_item_status, update_item_from_legacy, update_outcome_stats, recompute_source_states, supersede_item, and supersede_active. Do not change row_version in ContextStore.update_access. Add row_version to the context_items semantic projection so a semantic CAS change is visible while access telemetry remains invisible.

Create the registry/resolution tables and append-only event tables in ContextStore schema order. Add each current semantic table to SEMANTIC_PROJECTIONS. ProjectStore borrows the ContextStore connection and never opens or commits a second transaction.

- [ ] **Step 4: Run the schema tests and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_store.py tests/test_context_models.py tests/test_context_store.py tests/test_mutation_state.py -k "project or row_version or bootstrap"
~~~

Expected: schema, migration, and CAS tests pass.

- [ ] **Step 5: Write failing decision-record and double-CAS service tests.**

~~~python
from dataclasses import fields, replace

import pytest

from evolvmem.project_cursor import ProjectCursorAnchor
from evolvmem.project_models import (
    ArchiveProjectRequest,
    BindProjectWorkspaceRequest,
    ProjectAliasPage,
    ProjectAliasPageRequest,
    ProjectAliasRecord,
    ProjectPage,
    ProjectPageRequest,
    ProjectRecord,
    ProjectResolutionEvidence,
    ProjectResolutionRecord,
    ProjectResolutionReviewAction,
    ProjectResolutionReviewRequest,
    ProjectWorkspaceBindingRecord,
    ProjectValidationError,
    RegisterProjectAliasRequest,
    RegisterProjectRequest,
    ResolutionPage,
    ResolutionPageRequest,
    RevokeProjectAliasRequest,
    RevokeProjectWorkspaceRequest,
    SetDefaultProjectWorkspaceRequest,
    WorkspaceBindingPage,
    WorkspaceBindingPageRequest,
)


def test_web_task_7_project_dto_constructor_fields_are_frozen():
    expected = {
        ProjectPageRequest: ("status", "limit", "cursor"),
        RegisterProjectRequest: (
            "project", "expected_registry_revision", "expected_row_revision",
            "actor", "run_id",
        ),
        ArchiveProjectRequest: (
            "project", "expected_registry_revision", "expected_row_revision",
            "actor", "run_id",
        ),
        ProjectRecord: (
            "project", "status", "revision", "registry_revision",
            "created_at", "updated_at",
        ),
        ProjectPage: (
            "items", "next_cursor", "has_more", "registry_revision",
        ),
        ProjectAliasPageRequest: ("project", "limit", "cursor"),
        RegisterProjectAliasRequest: (
            "alias", "project", "expected_registry_revision",
            "expected_row_revision", "actor", "run_id",
        ),
        RevokeProjectAliasRequest: (
            "alias", "expected_registry_revision", "expected_row_revision",
            "actor", "run_id",
        ),
        ProjectAliasRecord: (
            "alias", "project", "revision", "registry_revision",
            "created_at", "updated_at",
        ),
        ProjectAliasPage: (
            "items", "next_cursor", "has_more", "registry_revision",
        ),
        WorkspaceBindingPageRequest: (
            "project", "workspace_fingerprint", "state", "limit", "cursor",
        ),
        BindProjectWorkspaceRequest: (
            "workspace_fingerprint", "project", "state", "is_default", "method",
            "expected_registry_revision", "expected_row_revision", "actor", "run_id",
        ),
        RevokeProjectWorkspaceRequest: (
            "workspace_fingerprint", "project", "expected_registry_revision",
            "expected_row_revision", "actor", "run_id",
        ),
        SetDefaultProjectWorkspaceRequest: (
            "workspace_fingerprint", "project", "expected_registry_revision",
            "expected_row_revision", "actor", "run_id",
        ),
        ProjectWorkspaceBindingRecord: (
            "workspace_fingerprint", "project", "state", "is_default", "method",
            "revision", "registry_revision", "created_at", "updated_at",
        ),
        WorkspaceBindingPage: (
            "items", "next_cursor", "has_more", "registry_revision",
        ),
        ResolutionPageRequest: (
            "review_state", "resolution_state", "project", "limit", "cursor",
        ),
        ProjectResolutionEvidence: (
            "source", "type", "source_version", "normalized_value",
        ),
        ProjectResolutionRecord: (
            "item_id", "resolution_state", "decision_source", "review_state",
            "proposed_project", "resolved_project", "previous_project",
            "confidence", "method", "evidence", "resolver_version", "revision",
            "item_row_version", "reviewed_at", "created_at", "updated_at",
        ),
        ResolutionPage: ("items", "next_cursor", "has_more"),
        ProjectResolutionReviewRequest: (
            "item_id", "action", "project", "expected_revision",
            "expected_item_row_version", "actor", "run_id",
        ),
    }
    for dto_type, field_names in expected.items():
        assert tuple(field.name for field in fields(dto_type)) == field_names
    assert ProjectPageRequest() == ProjectPageRequest(
        status="",
        limit=50,
        cursor="",
    )
    assert ResolutionPageRequest().resolution_state is None
    assert tuple(action.value for action in ProjectResolutionReviewAction) == (
        "accept_project",
        "reject_project",
        "confirm_global",
        "ignore",
    )
    review = ProjectResolutionReviewRequest(
        item_id=41,
        action=ProjectResolutionReviewAction.ACCEPT_PROJECT,
        project="alpha",
        expected_revision=4,
        expected_item_row_version=9,
        actor="web-operator",
        run_id="web-v2-000000000000000000000001",
    )
    assert review.item_id == 41
    assert review.project == "alpha"
    assert review.expected_revision == 4
    assert review.expected_item_row_version == 9
    ignored = ProjectResolutionReviewRequest(
        item_id=42,
        action=ProjectResolutionReviewAction.IGNORE,
        project=None,
        expected_revision=1,
        expected_item_row_version=None,
        actor="web-operator",
        run_id="web-v2-000000000000000000000002",
    )
    assert ignored.project is None


def test_decide_before_insert_and_record_after_insert_share_no_hidden_commit(
    context_store,
    project_service,
    resolution_request,
    make_draft,
):
    decision = project_service.decide_item(resolution_request)
    assert project_service.list_resolutions(ResolutionPageRequest(limit=10)).items == ()

    with context_store.semantic_transaction("test.resolve"):
        draft = replace(make_draft("resolved-item"), project=decision.resolved_project)
        item = context_store.create_item(draft)
        record = project_service.record_item_resolution(item.id, decision, "run_test")

    assert record.item_id == item.id
    assert record.resolved_project == decision.resolved_project
    assert context_store.current_mutation_epoch() == 1


def test_human_review_keeps_private_provenance_out_of_public_record(
    pending_resolution_fixture,
):
    request = ProjectResolutionReviewRequest(
        item_id=pending_resolution_fixture.item_id,
        action=ProjectResolutionReviewAction.ACCEPT_PROJECT,
        project="alpha",
        expected_revision=pending_resolution_fixture.resolution_revision,
        expected_item_row_version=pending_resolution_fixture.item_row_version,
        actor="reviewer-one",
        run_id="web-v2-000000000000000000000003",
    )
    public = pending_resolution_fixture.service.review_resolution(request)
    private = pending_resolution_fixture.store.get_resolution_private(request.item_id)

    assert private.run_id == request.run_id
    assert private.reviewed_by_hash == pending_resolution_fixture.identity.digest_private(
        "project.resolution.reviewer.v1",
        request.actor.encode("utf-8"),
    )
    assert private.reviewed_at == public.reviewed_at
    assert not hasattr(public, "run_id")
    assert not hasattr(public, "reviewed_by_hash")


def test_project_cursor_rejects_tamper_and_filter_or_endpoint_reuse(cursor_codec):
    filters = (("project", "alpha"), ("status", "active"))
    token = cursor_codec.encode(
        "project_aliases",
        filters,
        ProjectCursorAnchor("2026-09-01 10:00:00", "alias-b"),
    )
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")

    assert len(token) <= 1024
    with pytest.raises(ProjectValidationError, match="invalid_cursor"):
        cursor_codec.decode(tampered, "project_aliases", filters)
    with pytest.raises(ProjectValidationError, match="cursor_filter_mismatch"):
        cursor_codec.decode(token, "project_aliases", (("project", "beta"),))
    with pytest.raises(ProjectValidationError, match="cursor_endpoint_mismatch"):
        cursor_codec.decode(token, "project_resolutions", filters)
    with pytest.raises(ProjectValidationError, match="invalid_cursor"):
        cursor_codec.decode("A" * 1025, "project_aliases", filters)


def test_alias_pages_do_not_skip_equal_timestamp_rows(project_service, seeded_aliases):
    seeded_aliases.set_same_updated_at("2026-09-01 10:00:00")
    first = project_service.list_aliases(ProjectAliasPageRequest(limit=2))
    second = project_service.list_aliases(
        ProjectAliasPageRequest(limit=2, cursor=first.next_cursor)
    )

    assert tuple(row.alias for row in first.items + second.items) == (
        "alias-c",
        "alias-b",
        "alias-a",
    )
    assert first.has_more is True
    assert second.has_more is False
    assert second.next_cursor == ""


def test_binding_lifecycle_port_runs_inside_registry_transaction(
    context_store,
    project_store_with_recording_lifecycle,
):
    store, lifecycle = project_store_with_recording_lifecycle
    with context_store.semantic_transaction("test.binding.activate"):
        store.bind_workspace_row(
            workspace_fingerprint="hmac-sha256:" + "a" * 64,
            project="alpha",
            state="active",
            is_default=True,
            method="operator",
            expected_registry_revision=1,
            expected_row_revision=0,
            actor_hash="hmac-sha256:" + "c" * 64,
            run_id="run_binding_activate",
        )
    assert lifecycle.calls == [
        ("ensure", "alpha", "hmac-sha256:" + "a" * 64),
    ]

    with context_store.semantic_transaction("test.binding.revoke"):
        store.revoke_workspace_row(
            workspace_fingerprint="hmac-sha256:" + "a" * 64,
            project="alpha",
            expected_registry_revision=2,
            expected_row_revision=1,
            actor_hash="hmac-sha256:" + "c" * 64,
            run_id="run_binding_revoke",
        )
    assert lifecycle.calls[-1] == (
        "clear",
        "alpha",
        "hmac-sha256:" + "a" * 64,
    )


def test_binding_lifecycle_failure_rolls_back_registry_row(
    context_store,
    project_store_with_failing_lifecycle,
):
    with pytest.raises(RuntimeError, match="injected_focus_failure"):
        with context_store.semantic_transaction("test.binding.rollback"):
            project_store_with_failing_lifecycle.bind_workspace_row(
                workspace_fingerprint="hmac-sha256:" + "b" * 64,
                project="alpha",
                state="active",
                is_default=True,
                method="operator",
                expected_registry_revision=1,
                expected_row_revision=0,
                actor_hash="hmac-sha256:" + "d" * 64,
                run_id="run_binding_rollback",
            )
    assert project_store_with_failing_lifecycle.get_workspace_binding(
        "hmac-sha256:" + "b" * 64,
        "alpha",
    ) is None
~~~

Also test stale registry meta revision, stale alias/binding row revision, stale item row_version on human acceptance, competing alias registration, concurrent defaults, candidate promotion, revoke/default repair, multiple-active ambiguity, hashed operator identity, bounded evidence, and rollback leaving no event.

- [ ] **Step 6: Run service tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_service.py tests/test_project_cursor.py
~~~

Expected failure: ProjectService methods or typed request/result classes are missing.

- [ ] **Step 7: Implement split resolution boundaries and double CAS.**

Implement every frozen dataclass in the constructor table in evolvmem/project_models.py, in the tested field order. Normalize page items to tuples, validate limits and cursors, validate fingerprints as hmac-sha256 plus 64 lowercase hex characters, and validate registry/row/review revisions as nonnegative or positive according to their CAS semantics. The Web-critical review DTO is implemented exactly as follows:

Implement the cursor codec in evolvmem/project_cursor.py with a dedicated private-HMAC domain and no path, key, or row content:

~~~python
import base64
import binascii
from collections.abc import Sequence
from dataclasses import dataclass
import hmac
import json
import re

from evolvmem.project_models import ProjectValidationError
from evolvmem.workspace_identity import WorkspaceIdentityProvider


@dataclass(frozen=True, slots=True)
class ProjectCursorAnchor:
    updated_at: str
    stable_id: str


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(token: str) -> bytes:
    if not token or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise ProjectValidationError("invalid_cursor")
    try:
        return base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
    except (ValueError, binascii.Error) as exc:
        raise ProjectValidationError("invalid_cursor") from exc


class ProjectCursorCodec:
    DOMAIN = "project.pagination.cursor.v1"
    MAX_TOKEN_CHARS = 1024

    def __init__(self, identity: WorkspaceIdentityProvider):
        self._identity = identity

    def encode(
        self,
        endpoint: str,
        filters: Sequence[tuple[str, str]],
        anchor: ProjectCursorAnchor,
    ) -> str:
        _require_bounded_text(endpoint, "cursor_endpoint", maximum=64)
        _require_bounded_text(anchor.updated_at, "cursor_updated_at", maximum=32)
        _require_bounded_text(anchor.stable_id, "cursor_stable_id", maximum=256)
        canonical_filters = tuple(sorted(filters))
        if len(canonical_filters) > 8:
            raise ProjectValidationError("invalid_cursor_filters")
        for key, value in canonical_filters:
            _require_bounded_text(key, "cursor_filter_key", maximum=64)
            if not isinstance(value, str) or len(value) > 256:
                raise ProjectValidationError("invalid_cursor_filter_value")
        payload = json.dumps(
            {
                "endpoint": endpoint,
                "filters": [list(pair) for pair in canonical_filters],
                "stable_id": anchor.stable_id,
                "updated_at": anchor.updated_at,
                "version": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        envelope = json.dumps(
            {
                "payload": _b64url_encode(payload),
                "signature": self._identity.digest_private(self.DOMAIN, payload),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        token = _b64url_encode(envelope)
        if len(token) > self.MAX_TOKEN_CHARS:
            raise ProjectValidationError("cursor_too_large")
        return token

    def decode(
        self,
        token: str,
        endpoint: str,
        filters: Sequence[tuple[str, str]],
    ) -> ProjectCursorAnchor:
        if len(token) > self.MAX_TOKEN_CHARS:
            raise ProjectValidationError("invalid_cursor")
        try:
            envelope = json.loads(_b64url_decode(token))
            if set(envelope) != {"payload", "signature"}:
                raise ProjectValidationError("invalid_cursor")
            payload = _b64url_decode(envelope["payload"])
            expected = self._identity.digest_private(self.DOMAIN, payload)
            if not hmac.compare_digest(envelope["signature"], expected):
                raise ProjectValidationError("invalid_cursor")
            body = json.loads(payload)
            if set(body) != {
                "endpoint", "filters", "stable_id", "updated_at", "version"
            } or body["version"] != 1:
                raise ProjectValidationError("invalid_cursor")
            decoded_filters = tuple(tuple(pair) for pair in body["filters"])
            anchor = ProjectCursorAnchor(body["updated_at"], body["stable_id"])
        except (KeyError, TypeError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ProjectValidationError("invalid_cursor") from exc
        if body["endpoint"] != endpoint:
            raise ProjectValidationError("cursor_endpoint_mismatch")
        if decoded_filters != tuple(sorted(filters)):
            raise ProjectValidationError("cursor_filter_mismatch")
        _require_bounded_text(anchor.updated_at, "cursor_updated_at", maximum=32)
        _require_bounded_text(anchor.stable_id, "cursor_stable_id", maximum=256)
        return anchor
~~~

Each list method binds the endpoint and every normalized filter into the cursor. Ordering is updated_at DESC then project DESC for registry rows, alias DESC for aliases, `(workspace_fingerprint, project)` DESC for bindings, and item_id DESC for resolutions. Project and alias queries continue with `updated_at < ? OR (updated_at = ? AND identity < ?)`. Binding stable_id is `workspace_fingerprint + "\u001f" + project`; decode splits exactly once and SQL continues with `updated_at < ? OR (updated_at = ? AND (workspace_fingerprint < ? OR (workspace_fingerprint = ? AND project < ?)))`. Resolution stable_id is the decimal item ID; decode requires a positive canonical integer and SQL compares item_id numerically. Fetch limit+1, return only limit rows, derive has_more from the extra row, and encode the last returned row only when has_more is true. Empty cursor starts at the first page; malformed, over-1024, wrong-endpoint, wrong-filter, and signature-mismatched cursors fail closed.

~~~python
@dataclass(frozen=True, slots=True)
class ProjectResolutionReviewRequest:
    item_id: int
    action: ProjectResolutionReviewAction
    project: Optional[str]
    expected_revision: int
    expected_item_row_version: Optional[int]
    actor: str
    run_id: str

    def __post_init__(self) -> None:
        _require_positive_int(self.item_id, "item_id")
        if not isinstance(self.action, ProjectResolutionReviewAction):
            raise ProjectValidationError("invalid_resolution_review_action")
        _require_positive_int(self.expected_revision, "expected_revision")
        actor = _require_bounded_text(self.actor, "actor", maximum=256)
        run_id = _require_bounded_text(self.run_id, "run_id", maximum=128)
        if self.project is None:
            project = None
        elif isinstance(self.project, str) and self.project.strip():
            project = _require_project_name(self.project.strip())
        else:
            raise ProjectValidationError("invalid_project")
        if self.action is ProjectResolutionReviewAction.ACCEPT_PROJECT:
            if project is None:
                raise ProjectValidationError("project_required")
            _require_positive_int(
                self.expected_item_row_version,
                "expected_item_row_version",
            )
        elif project is not None or self.expected_item_row_version is not None:
            raise ProjectValidationError("unexpected_project_or_item_row_version")
        object.__setattr__(self, "project", project)
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "run_id", run_id)
~~~

~~~python
def decide_item(self, request: ResolveProjectItemRequest) -> ProjectResolutionDecision:
    identity = (
        self._identity.resolve(request.workspace_path)
        if request.workspace_path
        else None
    )
    resolver_request = request.to_resolution_request(
        workspace_fingerprint="" if identity is None else identity.fingerprint
    )
    return self._resolver.resolve(resolver_request, self._store.registry_snapshot())


def record_item_resolution(
    self,
    item_id: int,
    decision: ProjectResolutionDecision,
    run_id: str,
) -> ProjectResolutionRecord:
    self._store.require_existing_item(item_id)
    self._store.require_semantic_transaction("record_item_resolution")
    record = self._store.upsert_resolution(item_id, decision, run_id)
    self._store.append_resolution_event(record, action="automatic")
    return record
~~~

Define the binding lifecycle port and invoke it only after the registry row CAS has succeeded and before the append-only registry event:

~~~python
from typing import Protocol


class ProjectBindingLifecyclePort(Protocol):
    def ensure_focus_for_active_binding(
        self,
        *,
        project: str,
        workspace_fingerprint: str,
    ) -> None:
        raise NotImplementedError

    def clear_focus_for_inactive_binding(
        self,
        *,
        project: str,
        workspace_fingerprint: str,
    ) -> None:
        raise NotImplementedError


def _apply_binding_lifecycle(
    self,
    *,
    previous_state: str | None,
    record: ProjectWorkspaceBindingRecord,
) -> None:
    self._context_store.require_semantic_transaction("project_binding_lifecycle")
    if self._binding_lifecycle is None:
        return
    if record.state == "active" and previous_state != "active":
        self._binding_lifecycle.ensure_focus_for_active_binding(
            project=record.project,
            workspace_fingerprint=record.workspace_fingerprint,
        )
    elif previous_state == "active" and record.state != "active":
        self._binding_lifecycle.clear_focus_for_inactive_binding(
            project=record.project,
            workspace_fingerprint=record.workspace_fingerprint,
        )
~~~

Registry and alias mutations require expected_registry_revision and their expected row revision. Resolution review uses exact item ID and expected resolution revision; ACCEPT_PROJECT also requires expected_item_row_version and updates item plus resolution plus event in one semantic transaction. REJECT_PROJECT, CONFIRM_GLOBAL, and IGNORE remain separate actions. bind_workspace_row, candidate-to-active promotion, revoke_workspace_row, and archive-project binding transitions all call _apply_binding_lifecycle on the borrowed connection. The continuity adapter implements ensure by INSERT-on-conflict-do-nothing followed by an assertion that a newly active binding has a permanent NULL focus row; it implements clear by reading current focus revision/workstream ID and calling its exact rowcount-CAS clear, retaining the NULL row and incrementing focus revision only when a non-NULL focus was cleared. A hook error or stale focus CAS aborts the outer registry transaction, so neither binding row, registry revision, nor registry event commits. Task 3 defines no continuity tables.

- [ ] **Step 8: Run Task 3 tests and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_store.py tests/test_project_service.py tests/test_project_cursor.py tests/test_context_models.py tests/test_context_store.py tests/test_mutation_state.py tests/test_production_write_boundaries.py
~~~

Expected: all listed tests pass, including one epoch for each registry or review mutation.

- [ ] **Step 9: Commit Task 3.**

~~~bash
git diff --check
git status --short
git add evolvmem/project_models.py evolvmem/project_store.py evolvmem/project_service.py evolvmem/project_cursor.py evolvmem/context_models.py evolvmem/context_store.py evolvmem/mutation_state.py tests/test_project_store.py tests/test_project_service.py tests/test_project_cursor.py tests/test_context_models.py tests/test_context_store.py tests/test_mutation_state.py tests/test_production_write_boundaries.py
git commit -m "feat: add versioned project registry and resolutions"
~~~

Expected: one commit with the Task 3 implementation and boundary-test updates.

---

### Task 4: Resolve every typed write and stop permanent session logs

**Files:**

- Modify: evolvmem/legacy_models.py — LegacyAddRequest, LegacyReplaceRequest, LegacyExtractionRequest transient fields
- Modify: evolvmem/context_migration.py — LegacyMemoryMigrator.draft_from_projection_row and migrate_projection_row
- Modify: evolvmem/context_service.py — ContextService._add_dual_in_transaction, ContextService._replace_dual_in_transaction, ContextService._persist_extraction_dual
- Modify: evolvmem/config.py — Config session_summary_ttl_days
- Modify: evolvmem/kimi_hooks.py — session_end workspace and project propagation
- Modify: evolvmem/dsh_bridge.py — extract_from_messages and extract_cli propagation
- Modify: evolvmem/mcp_contract.py — tool_specs and _context_specs
- Modify: evolvmem/mcp_server.py — MemoryMCPServer._memory_add and _memory_replace
- Modify: dsh/src/common.js — dispatchExtraction
- Modify: dsh/src/sweep.js — apply
- Modify: tests/test_context_migration.py — test_draft_from_projection_row_derives_core_metadata_from_the_stored_row and test_migrate_projection_row_runs_inside_the_caller_transaction
- Modify: tests/test_context_service.py — test_extraction_batch_commits_summary_and_candidates_in_one_outer_transaction
- Modify: tests/test_kimi_hooks.py — TestSessionEndOutcome.test_session_end_rolls_back_summary_and_atomics_on_third_write_failure
- Modify: tests/test_dsh_bridge.py — TestExtract and TestCli
- Modify: tests/test_mcp_protocol.py — TestRegistry.test_context_tool_schemas_are_frozen and TestContextProtocolErrors
- Modify: tests/test_integration.py — TestIntegration and TestKimiSessionArchiveIntegration

**Interfaces:**

- Consumes: ProjectService.decide_item(request: ResolveProjectItemRequest) -> ProjectResolutionDecision before insertion and ProjectService.record_item_resolution(item_id: int, decision: ProjectResolutionDecision, run_id: str) -> ProjectResolutionRecord after insertion; transient workspace_path and project_hint from Codex MCP, Kimi, and DSH.
- Produces: LegacyAddRequest, LegacyReplaceRequest, and LegacyExtractionRequest gain workspace_path: str="" and project_hint: str=""; every new resolvable project-scope item has a canonical project; conflict and unresolved items have project empty plus a pending resolution row; new session summaries without explicit expiry receive created_at plus 30 days.

- [ ] **Step 1: Write failing service sequencing and privacy tests.**

~~~python
def test_typed_write_decides_before_insert_then_records_by_real_item_id(
    service,
    recording_project_service,
):
    result = service.legacy_add(
        LegacyAddRequest(
            key="project:eva:fact:one",
            value="bounded value",
            tags=("分类:eva",),
            workspace_path="/private/work/eva",
            project_hint="eva",
        )
    )

    assert recording_project_service.events == [
        ("decide", None),
        ("record", result.context_id),
    ]
    stored = service._store.get_item(result.context_id, include_layers=True)
    assert stored.project == "eva"
    assert "/private/work/eva" not in repr(stored)
~~~

Add resolved binding, trusted explicit project, generic cwd rejection, conflict and unresolved pending rows, global types, resolver exception preserving an existing project in repair analysis, and raw workspace path absence from items, layers, sources, evidence, logs, and errors.

- [ ] **Step 2: Run service project tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_migration.py tests/test_context_service.py -k "project or workspace or resolution"
~~~

Expected failure: Legacy request types reject workspace_path or no resolution call is recorded.

- [ ] **Step 3: Implement the canonical decide-insert-record sequence.**

~~~python
def _create_resolved_item(
    self,
    request: ResolveProjectItemRequest,
    draft: ContextItemDraft,
    *,
    run_id: str,
) -> ContextItem:
    decision = self._project_service.decide_item(request)
    project = (
        decision.resolved_project
        if decision.state is ProjectResolutionState.RESOLVED
        else ""
    )
    canonical = replace(draft, project=project)
    with self._store.semantic_transaction("context.item.create", owner_run_id=run_id or None):
        item = self._store.create_item(canonical)
        self._project_service.record_item_resolution(item.id, decision, run_id)
    return item
~~~

For existing outer extraction or dual-write transactions, use an internal same-connection variant rather than opening a nested commit. The legacy projection receives the canonical project metadata only after the decision. Historical backfill uses archive_project and archive_source_version only when the generator version is trusted.

- [ ] **Step 4: Run service project tests and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_migration.py tests/test_context_service.py -k "project or workspace or resolution"
~~~

Expected: all focused project, workspace, and resolution tests pass.

- [ ] **Step 5: Write failing TTL and adapter propagation tests.**

~~~python
def test_session_summary_default_ttl_and_all_adapters_forward_transient_workspace(
    adapter_harness,
    frozen_now,
):
    outcomes = adapter_harness.write_one_summary_through_each_adapter(
        workspace_path="/private/repo/eva",
        project_hint="eva",
    )

    assert {outcome.adapter for outcome in outcomes} == {"codex", "kimi", "dsh"}
    assert all(outcome.project == "eva" for outcome in outcomes)
    assert all(outcome.expires_at == "2026-10-01 00:00:00" for outcome in outcomes)
    assert all("/private/repo/eva" not in outcome.public_json for outcome in outcomes)
~~~

Also test explicit expiry validation, summary plus atomics all-or-nothing, memory_add and memory_replace MCP schemas, DSH JavaScript forwarding, the eva:progress:log identity no longer immortal, and known coarse-cwd generator versions not becoming trusted because a new adapter supplies cwd.

- [ ] **Step 6: Run adapter tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_kimi_hooks.py tests/test_dsh_bridge.py tests/test_mcp_protocol.py tests/test_integration.py -k "project or summary or ttl or workspace"
~~~

Expected failure: missing typed fields, missing forwarding, or expires_at remains null.

- [ ] **Step 7: Implement TTL and adapter forwarding.**

~~~python
def _summary_expiry(
    *,
    content_type: ContextContentType,
    created_at: datetime,
    explicit_expires_at: str | None,
    ttl_days: int,
) -> str | None:
    if explicit_expires_at is not None:
        return explicit_expires_at
    if content_type is not ContextContentType.SESSION_SUMMARY:
        return None
    expires = created_at.astimezone(timezone.utc) + timedelta(days=ttl_days)
    return expires.strftime("%Y-%m-%d %H:%M:%S")
~~~

Set Config.session_summary_ttl_days to 30 and require a positive integer. Codex MCP, Kimi, and DSH pass actual cwd only as workspace_path and pass explicit user/operator project only as project_hint. Kimi and DSH keep summary plus atomics in one semantic transaction.

- [ ] **Step 8: Run Task 4 tests and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_migration.py tests/test_context_service.py tests/test_kimi_hooks.py tests/test_dsh_bridge.py tests/test_mcp_protocol.py tests/test_integration.py tests/test_production_write_boundaries.py
~~~

Expected: all listed tests pass and each adapter batch advances one epoch.

- [ ] **Step 9: Commit Task 4.**

~~~bash
git diff --check
git status --short
git add evolvmem/legacy_models.py evolvmem/context_migration.py evolvmem/context_service.py evolvmem/config.py evolvmem/kimi_hooks.py evolvmem/dsh_bridge.py evolvmem/mcp_contract.py evolvmem/mcp_server.py dsh/src/common.js dsh/src/sweep.js tests/test_context_migration.py tests/test_context_service.py tests/test_kimi_hooks.py tests/test_dsh_bridge.py tests/test_mcp_protocol.py tests/test_integration.py tests/test_production_write_boundaries.py
git commit -m "fix: resolve project on every typed write"
~~~

Expected: one commit containing only Task 4 files.

---

### Task 5: Generate versioned rolling project summaries

**Files:**

- Modify: evolvmem/context_models.py — ContextContentType.PROJECT_SUMMARY
- Create: evolvmem/project_rollup.py — ProjectRollupRequest, ProjectRollupResult, ProjectRollupGenerator
- Create: tests/test_project_rollup.py — identity, source closure, failure, and orchestration cases
- Modify: evolvmem/context_store.py — ContextStore.create_schema_in_transaction, ContextStore.record_experience_source, ContextStore.get_retrieval_records
- Modify: evolvmem/mutation_state.py — context_project_rollups semantic projection
- Modify: evolvmem/context_service.py — ContextService.persist_legacy_extraction and ContextService._persist_extraction_dual
- Modify: evolvmem/kimi_hooks.py — session_end
- Modify: evolvmem/dsh_bridge.py — extract_from_messages
- Modify: evolvmem/context_vector_sync.py — ContextVectorSynchronizer.upsert_active_l0
- Modify: tests/test_context_models.py — test_context_enums_expose_the_persisted_values
- Modify: tests/test_context_vector_sync.py — test_failed_upsert_preserves_the_dirty_marker_and_old_entry
- Modify: tests/test_kimi_hooks.py — TestSessionEndOutcome and TestSessionEndConsolidationWiring
- Modify: tests/test_dsh_bridge.py — TestExtract
- Modify: tests/test_mutation_state.py — test_rollup_water_mutation_without_epoch_is_detected

**Interfaces:**

- Consumes: active same-project atomic Context IDs, session-summary Context ID, previous project-summary L1, configured LLM boundary, and ContextVectorSynchronizer.
- Produces: ProjectRollupGenerator.generate(request: ProjectRollupRequest) -> ProjectRollupResult; ProjectRollupGenerator.retry_vector(project: str) -> ProjectRollupResult; one active project:{project}:knowledge:current item; context_project_rollups water row with pending, ready, failed, or vector_dirty.

- [ ] **Step 1: Write failing identity, idempotency, and source-closure tests.**

~~~python
def test_same_source_set_and_generator_skips_llm_and_preserves_relational_closure(
    rollup_generator,
    project_sources,
    recording_llm,
):
    request = ProjectRollupRequest(
        project="eva",
        source_context_ids=(project_sources.second, project_sources.first),
        run_id="run_one",
    )
    first = rollup_generator.generate(request)
    second = rollup_generator.generate(request)

    assert first.status == "ready"
    assert second.status == "ready"
    assert second.changed is False
    assert recording_llm.calls == 1
    assert rollup_generator.source_ids("eva") == (
        project_sources.first,
        project_sources.second,
    )


def test_rollup_schema_water_mutation_without_epoch_is_detected(rollup_fixture):
    before_epoch = rollup_fixture.store.current_mutation_epoch()
    before_digest = rollup_fixture.store.canonical_state_digest()
    with rollup_fixture.store.transaction():
        rollup_fixture.store._connection().execute(
            "UPDATE context_project_rollups SET status='failed' WHERE project=?",
            (rollup_fixture.project,),
        )

    report = rollup_fixture.store.detect_unjournaled_mutation(
        expected_epoch=before_epoch,
        expected_digest=before_digest,
    )
    assert report.reason_codes == ("semantic_digest_changed_without_epoch",)
~~~

Assert the stable identity, one active summary, exact water columns project/current_context_id/source_set_hash/covered_through/generator_version/run_id/status/revision/updated_at, legal statuses, generator-version sensitivity, and relational context_reference sources. L0 must contain project, current stage, blockers, and next step. L1 must contain stable knowledge, recent changes, decisions, known issues, current workflow, and keywords. L2 maps every statement to Context IDs and contains no transcript copies.

- [ ] **Step 2: Run rollup model tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_rollup.py -k "identity or source or idempotent or schema"
~~~

Expected failure: PROJECT_SUMMARY or ProjectRollupGenerator is missing.

- [ ] **Step 3: Implement canonical source hashing and validated generation.**

~~~python
def _source_set_hash(source_context_ids: Sequence[int], generator_version: str) -> str:
    payload = {
        "generator_version": generator_version,
        "source_context_ids": sorted(set(source_context_ids)),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate(self, request: ProjectRollupRequest) -> ProjectRollupResult:
    source_ids = tuple(sorted(set(request.source_context_ids)))
    sources = self._store.load_rollup_sources(request.project, source_ids)
    if tuple(item.id for item in sources) != source_ids:
        return ProjectRollupResult.failed(request.project, "invalid_source_set")
    source_hash = _source_set_hash(source_ids, self.GENERATOR_VERSION)
    current = self._store.get_project_rollup(request.project)
    if current is not None and current.source_set_hash == source_hash and current.status == "ready":
        return ProjectRollupResult.unchanged(current)
    previous_l1 = self._store.active_project_summary_l1(request.project)
    candidate = self._validator.validate(
        project=request.project,
        source_ids=source_ids,
        payload=self._llm.generate(previous_l1, sources),
    )
    return self._commit_candidate(request, source_hash, candidate)
~~~

load_rollup_sources must require active, unexpired, same-project items and exclude candidate, deleted, and expired rows. The continuity plan extends it to exclude checkpoint and recovery content types after those enums exist. Model generation and validation happen before the semantic transaction.

In mutation_state.py add the rollup water row exactly when its table is added:

~~~python
SEMANTIC_PROJECTIONS["context_project_rollups"] = (
    "project",
    "current_context_id",
    "source_set_hash",
    "covered_through",
    "generator_version",
    "run_id",
    "status",
    "revision",
    "updated_at",
)
~~~

- [ ] **Step 4: Run rollup model tests and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_rollup.py -k "identity or source or idempotent or schema"
~~~

Expected: identity, schema, closure, and idempotency tests pass.

- [ ] **Step 5: Write failing failure-semantics and post-commit orchestration tests.**

~~~python
def test_vector_failure_keeps_new_summary_and_marks_vector_dirty(
    rollup_generator,
    seeded_old_summary,
    source_ids,
    failing_vector_sync,
):
    result = rollup_generator.generate(
        ProjectRollupRequest("eva", source_ids, "run_vector_failure")
    )

    current = rollup_generator.current_summary("eva")
    assert result.status == "vector_dirty"
    assert current.id != seeded_old_summary.id
    assert current.status.value == "active"
    assert rollup_generator.rollup_state("eva").status == "vector_dirty"


def test_post_session_rollup_failure_never_rolls_back_committed_batch(session_writer):
    result = session_writer.persist_with_rollup_failure()
    assert result.persisted == 3
    assert session_writer.persisted_context_ids() == result.context_ids
    assert result.rollup_status == "failed"
~~~

Also prove LLM, parse, validation, and SQLite failures preserve the old active summary; successful replacement supersedes old item and writes L0/L1/L2/sources/water atomically; conflicts stay in sources but appear under pending confirmation rather than stable knowledge.

- [ ] **Step 6: Run failure tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_rollup.py tests/test_context_vector_sync.py tests/test_kimi_hooks.py tests/test_dsh_bridge.py -k "failure or vector_dirty or post_commit or conflict"
~~~

Expected failure: old summary is changed too early, vector failure is reported as rollback, or adapters do not call the post-commit hook.

- [ ] **Step 7: Implement atomic replacement and post-commit vector handling.**

~~~python
def _commit_candidate(self, request, source_hash, candidate) -> ProjectRollupResult:
    with self._store.semantic_transaction(
        "project.rollup.replace",
        owner_run_id=request.run_id or None,
    ):
        item = self._store.supersede_active(candidate.to_context_draft(request.project))
        for source_id in sorted(set(request.source_context_ids)):
            self._store.record_context_reference(item.id, source_id, self.GENERATOR_VERSION)
        water = self._store.upsert_project_rollup(
            project=request.project,
            current_context_id=item.id,
            source_set_hash=source_hash,
            covered_through=candidate.covered_through,
            generator_version=self.GENERATOR_VERSION,
            run_id=request.run_id,
            status="ready",
        )
    try:
        self._vector_sync.upsert_active_l0(item.id, candidate.l0)
    except Exception:
        with self._store.semantic_transaction(
            "project.rollup.vector_dirty",
            owner_run_id=request.run_id or None,
        ):
            water = self._store.set_project_rollup_status(
                request.project,
                expected_revision=water.revision,
                status="vector_dirty",
            )
    return ProjectRollupResult.from_records(item, water)
~~~

ContextService invokes this hook only after summary plus atomics commit and passes their exact Context IDs. Kimi and DSH expose only bounded status and never change a successful extraction result when rollup fails.

- [ ] **Step 8: Run Task 5 tests and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_rollup.py tests/test_context_models.py tests/test_context_vector_sync.py tests/test_context_service.py tests/test_kimi_hooks.py tests/test_dsh_bridge.py tests/test_mutation_state.py
~~~

Expected: all listed tests pass, including vector-dirty retry.

- [ ] **Step 9: Commit Task 5.**

~~~bash
git diff --check
git status --short
git add evolvmem/context_models.py evolvmem/project_rollup.py evolvmem/context_store.py evolvmem/context_service.py evolvmem/context_vector_sync.py evolvmem/kimi_hooks.py evolvmem/dsh_bridge.py evolvmem/mutation_state.py tests/test_project_rollup.py tests/test_context_models.py tests/test_context_vector_sync.py tests/test_kimi_hooks.py tests/test_dsh_bridge.py tests/test_mutation_state.py
git commit -m "feat: add versioned project knowledge rollups"
~~~

Expected: one commit containing only Task 5 files.

---

### Task 6: Retain session evidence until exact rollup coverage

**Files:**

- Create: evolvmem/session_summary_lifecycle.py — SessionSummaryLifecycle and administrator disposition types
- Create: tests/test_session_summary_lifecycle.py — TTL, cap, hold, retry, and disposition cases
- Modify: evolvmem/session_archive.py — SessionArchiver.sweep_expired, SessionArchiver.purge_project, SessionArchiver._purge_rows
- Modify: evolvmem/forgetting.py — ForgettingEngine.find_candidates and ForgettingEngine._expired_ids
- Modify: evolvmem/context_service.py — ContextService._persist_extraction_dual, ContextService.sweep_archives
- Modify: evolvmem/context_store.py — ContextStore.create_schema_in_transaction, ContextStore.record_session_source, ContextStore.list_expired_session_archives
- Modify: evolvmem/mutation_state.py — session_archive_holds and session_archive_hold_events semantic projections
- Modify: tests/test_session_archive.py — test_sweep_purges_exactly_expired_archive_and_keeps_unexpired
- Modify: tests/test_forgetting.py — TestForgettingEngine.test_run_archives_expired and TestForgettingThroughFacade
- Modify: tests/test_production_write_boundaries.py — test_production_modules_have_no_raw_write_bypasses
- Modify: tests/test_mutation_state.py — test_archive_hold_tables_are_semantic

**Interfaces:**

- Consumes: ready project-rollup relational source closure, session-summary expiry, active-count cap, linked encrypted archive, and expected item row_version.
- Produces: SessionSummaryLifecycle.plan(project: str, now: datetime) -> SessionSummaryLifecyclePlan; SessionSummaryLifecycle.apply(plan: SessionSummaryLifecyclePlan) -> SessionSummaryLifecycleResult; SessionSummaryLifecycle.dispose_uncovered(request: SessionEvidenceDispositionRequest) -> SessionSummaryLifecycleResult; derived rollup_pending IDs and archive holds.

- [ ] **Step 1: Write failing retention, hold, and administrator-disposition tests.**

~~~python
def test_uncovered_expired_summary_stays_active_and_blocks_archive_purge(lifecycle_fixture):
    plan = lifecycle_fixture.lifecycle.plan(
        project="eva",
        now=lifecycle_fixture.after_expiry,
    )
    result = lifecycle_fixture.lifecycle.apply(plan)

    assert result.archived_context_ids == ()
    assert result.rollup_pending_context_ids == (lifecycle_fixture.summary_id,)
    assert lifecycle_fixture.store.get_item(
        lifecycle_fixture.summary_id,
        include_layers=False,
    ).status.value == "active"
    assert lifecycle_fixture.archiver.sweep_expired(
        now=lifecycle_fixture.after_expiry
    ).purged == 0


def test_explicit_administrator_disposition_releases_hold_and_archives_atomically(
    lifecycle_fixture,
):
    result = lifecycle_fixture.lifecycle.dispose_uncovered(
        SessionEvidenceDispositionRequest(
            item_id=lifecycle_fixture.summary_id,
            archive_id=lifecycle_fixture.archive_id,
            expected_item_row_version=1,
            action="archive_after_review",
            actor_hash="sha256:" + "2" * 64,
            reason_code="operator_reviewed_uncovered_evidence",
        )
    )
    assert result.archived_context_ids == (lifecycle_fixture.summary_id,)
    assert lifecycle_fixture.store.has_archive_hold(
        lifecycle_fixture.archive_id,
        lifecycle_fixture.summary_id,
    ) is False


def test_archive_hold_tables_are_semantic():
    assert SEMANTIC_PROJECTIONS["session_archive_holds"] == (
        "archive_id",
        "source_context_id",
        "reason",
        "created_at",
    )
    assert SEMANTIC_PROJECTIONS["session_archive_hold_events"] == (
        "id",
        "archive_id",
        "source_context_id",
        "action",
        "actor_hash",
        "reason_code",
        "created_at",
    )
~~~

Also test cap 10 per project, 30-day TTL, covered expiry/count archive, one bounded retry extension, derived rollup_pending visibility, hold creation in the same semantic transaction as source-summary/archive linking, failed rollup retaining holds, and stale row-version refusal for administrator disposition.

- [ ] **Step 2: Run lifecycle tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_session_summary_lifecycle.py tests/test_session_archive.py tests/test_forgetting.py tests/test_mutation_state.py -k "lifecycle or hold or archive"
~~~

Expected failure: SessionSummaryLifecycle import failure or held evidence is purged.

- [ ] **Step 3: Implement exact-closure planning and atomic apply.**

~~~python
def plan(self, *, project: str, now: datetime) -> SessionSummaryLifecyclePlan:
    summaries = self._store.active_session_summaries(project)
    covered = self._store.ready_rollup_source_ids(project)
    ordered = sorted(summaries, key=lambda item: (item.created_at, item.id), reverse=True)
    candidates: list[int] = []
    pending: list[int] = []
    for position, item in enumerate(ordered):
        expired = item.expires_at is not None and item.expires_at <= _format_ts(now)
        over_cap = position >= self._active_cap
        if not expired and not over_cap:
            continue
        if item.id in covered:
            candidates.append(item.id)
        else:
            pending.append(item.id)
    return SessionSummaryLifecyclePlan(
        project=project,
        archive_context_ids=tuple(candidates),
        rollup_pending_context_ids=tuple(pending),
        planned_at=_format_ts(now),
    )


def apply(self, plan: SessionSummaryLifecyclePlan) -> SessionSummaryLifecycleResult:
    with self._store.semantic_transaction("session_summary.lifecycle"):
        archived = self._store.archive_covered_summaries(
            plan.project,
            plan.archive_context_ids,
        )
        self._store.extend_uncovered_once(
            plan.rollup_pending_context_ids,
            retry_days=self._retry_days,
        )
        self._store.release_covered_archive_holds(archived)
    return SessionSummaryLifecycleResult(archived, plan.rollup_pending_context_ids)
~~~

session_archive_holds has archive_id, source_context_id, reason, and created_at with a unique pair and foreign keys. session_archive_hold_events has id, archive_id, source_context_id, action, actor_hash, reason_code, and created_at; it contains no evidence text. Add both tables to the semantic projections. Create the hold in the source-link transaction before purge can observe the archive. Purge uses NOT EXISTS against holds. Forgetting excludes active project summaries and uncovered session summaries. dispose_uncovered records the append-only bounded operator event, archives the exact item, and releases its exact hold in one semantic transaction.

In mutation_state.py add both tables with their complete semantic columns:

~~~python
SEMANTIC_PROJECTIONS.update(
    {
        "session_archive_holds": (
            "archive_id", "source_context_id", "reason", "created_at",
        ),
        "session_archive_hold_events": (
            "id", "archive_id", "source_context_id", "action", "actor_hash",
            "reason_code", "created_at",
        ),
    }
)
~~~

- [ ] **Step 4: Run lifecycle tests and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_session_summary_lifecycle.py tests/test_session_archive.py tests/test_forgetting.py tests/test_project_rollup.py tests/test_context_service.py tests/test_mutation_state.py tests/test_production_write_boundaries.py
~~~

Expected: all lifecycle tests pass; sustained rollup failure exposes backlog and deletes nothing.

- [ ] **Step 5: Commit Task 6.**

~~~bash
git diff --check
git status --short
git add evolvmem/session_summary_lifecycle.py evolvmem/session_archive.py evolvmem/forgetting.py evolvmem/context_service.py evolvmem/context_store.py evolvmem/mutation_state.py tests/test_session_summary_lifecycle.py tests/test_session_archive.py tests/test_forgetting.py tests/test_mutation_state.py tests/test_production_write_boundaries.py
git commit -m "feat: retain session evidence until rollup coverage"
~~~

Expected: one commit containing only Task 6 files.

---

### Task 7: Produce deterministic side-effect-free plans on old and current schemas

**Files:**

- Create: evolvmem/maintenance_models.py — MaintenanceAction, MaintenanceCounts, MaintenancePlan, MaintenancePlanRequest
- Create: evolvmem/project_maintenance.py — ProjectMaintenance.plan and read-only inspectors
- Create: tests/test_project_maintenance_plan.py — old-schema purity, classifications, digest, key, seed, and collision cases
- Modify: evolvmem/cutover_checks.py — _open_readonly and semantic fingerprint helpers
- Modify: tests/test_cutover_checks.py — test_preflight_opens_the_database_readonly_and_query_only

**Interfaces:**

- Consumes: a database path, read-only old/current schema inspection, registry snapshot when installed, config alias seeds, generic-name/version configuration, resolver/generator versions, and WorkspaceIdentityProvider.status without bootstrap.
- Produces: ProjectMaintenance.plan(request: MaintenancePlanRequest) -> MaintenancePlan with canonical bootstrap_schema, bootstrap_workspace_hmac_key, seed_registry, seed_alias, migrate, repair_project, pending_conflict, rollup, archive, vector, and diagnostic actions; no files, directories, tables, columns, keys, or rows are created.

- [ ] **Step 1: Write failing legacy-only plan-purity and key-bootstrap tests.**

~~~python
def test_old_schema_plan_is_repeatable_and_does_not_bootstrap_schema_or_key(
    legacy_only_fixture,
):
    before = legacy_only_fixture.snapshot_everything()
    first = legacy_only_fixture.maintenance.plan(legacy_only_fixture.request)
    middle = legacy_only_fixture.snapshot_everything()
    second = legacy_only_fixture.maintenance.plan(legacy_only_fixture.request)
    after = legacy_only_fixture.snapshot_everything()

    assert first.canonical_json() == second.canonical_json()
    assert before == middle == after
    assert "bootstrap_schema" in {action.kind for action in first.actions}
    assert "bootstrap_workspace_hmac_key" in {
        action.kind for action in first.actions
    }
    assert legacy_only_fixture.key_path.exists() is False


def test_same_actions_with_different_safe_workspace_keys_change_plan_digest(
    current_schema_fixture,
):
    first = current_schema_fixture.plan_with_safe_workspace_key(b"a" * 32)
    second = current_schema_fixture.plan_with_safe_workspace_key(b"b" * 32)

    assert first.actions == second.actions
    assert first.counts == second.counts
    assert first.workspace_key_state_digest != second.workspace_key_state_digest
    assert first.plan_digest != second.plan_digest
~~~

The snapshot covers database bytes, mtime, schema, optional epoch and registry revision, config bytes, vector tree, key status, and directory entries. Add current-schema fixtures for 451 unmapped rows, mapped wrong/empty projects, strong, medium, conflict, unknown, global, duplicate active identity, stale projection, and retention actions.

- [ ] **Step 2: Run plan tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_maintenance_plan.py
~~~

Expected failure: ModuleNotFoundError for evolvmem.maintenance_models or ProjectMaintenance has no plan.

- [ ] **Step 3: Implement schema-aware read-only inspection and canonical actions.**

~~~python
@contextmanager
def _readonly_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    encoded = urllib.parse.quote(str(db_path.resolve()), safe="/")
    conn = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        yield conn
    finally:
        conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> frozenset[str]:
    installed = {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if table not in installed:
        return frozenset()
    return frozenset(row["name"] for row in conn.execute(f"PRAGMA table_info({table})"))


def _workspace_key_state_digest(status: WorkspaceKeyStatus) -> str:
    if status.state not in {
        "missing",
        "ready",
        "unsafe_permissions",
        "changed",
    }:
        raise MaintenancePlanError("invalid_workspace_key_state")
    fingerprint = status.fingerprint if status.state == "ready" else ""
    if fingerprint and not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise MaintenancePlanError("invalid_workspace_key_fingerprint")
    body = json.dumps(
        {"state": status.state, "fingerprint": fingerprint},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(body).hexdigest()
~~~

Never instantiate normal schema bootstrap during plan. Inspect sqlite_master and PRAGMA table_info before each optional query. Model each missing table, index, and row_version column as a stable bootstrap_schema action. Inspect the workspace key without creating its parent. Missing key yields bootstrap_workspace_hmac_key; unsafe permissions or a changed established key yields a blocking diagnostic.

- [ ] **Step 4: Write the canonical plan and telemetry-free digest algorithm.**

~~~python
def _finalize_plan(self, facts: InspectionFacts, actions: list[MaintenanceAction]) -> MaintenancePlan:
    ordered = tuple(
        sorted(
            actions,
            key=lambda action: (
                action.target_kind,
                action.target_id,
                action.kind,
                action.reason_code,
            ),
        )
    )
    body = {
        "kind": "project_continuity_v1",
        "database_fingerprint": facts.semantic_database_fingerprint,
        "schema_version": facts.schema_version,
        "resolver_version": self._resolver.VERSION,
        "generator_version": self._generator_version,
        "registry_digest": facts.registry_digest,
        "config_digest": facts.config_digest,
        "workspace_key_state_digest": facts.workspace_key_state_digest,
        "actions": [action.canonical_dict() for action in ordered],
        "counts": facts.counts.canonical_dict(),
    }
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True)
    return MaintenancePlan.from_body(body, hashlib.sha256(raw.encode("utf-8")).hexdigest())
~~~

The semantic database fingerprint uses the Task 2 logical projections and excludes access_count, last_accessed, FTS, vector, run bookkeeping, epoch, and journal. For an old schema, apply equivalent explicit legacy projections with access telemetry omitted. InspectionFacts.workspace_key_state_digest is computed with _workspace_key_state_digest from the bounded key state and, only for a safe established key, its SHA-256 fingerprint; MaintenancePlan exposes this derived digest but no raw fingerprint, key bytes, or key path. Digest inputs include semantic fingerprint, schema/resolver/generator versions, registry/alias/generic-name configuration digests, workspace-key state digest, and canonical actions; exclude timestamps, report layout, local paths, and model output.

If registry is absent or has never had a revision, valid config aliases become seed_registry and seed_alias actions. Once registry revision exists, config differences become config_registry_drift diagnostics and are never applied. Pre-detect target active-identity collisions and emit pending_conflict with unchanged target item. Each action contains stable IDs and bounded reason codes only.

- [ ] **Step 5: Run plan tests and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_maintenance_plan.py tests/test_cutover_checks.py
~~~

Expected: tests pass; two plans are byte-identical; access telemetry and wall-clock changes do not alter the digest; semantic, registry, config, resolver, generator, schema, key-status, and action changes do.

- [ ] **Step 6: Commit Task 7.**

~~~bash
git diff --check
git status --short
git add evolvmem/maintenance_models.py evolvmem/project_maintenance.py evolvmem/cutover_checks.py tests/test_project_maintenance_plan.py tests/test_cutover_checks.py
git commit -m "feat: plan project migration deterministically"
~~~

Expected: one commit containing only Task 7 files.

---

### Task 8: Build verified backup and pure invariant-checking primitives before apply

**Files:**

- Create: evolvmem/maintenance_backup.py — MaintenanceBackupManager and VerifiedMaintenanceBackup
- Create: tests/test_maintenance_backup.py — run binding, WAL, key/config snapshot, permissions, and restore cases
- Modify: evolvmem/cutover_backup.py — create_cutover_backup, _backup_database, verify_cutover_backup
- Modify: evolvmem/cutover_checks.py — ProjectMaintenanceVerifier
- Modify: evolvmem/cutover_vector.py — ContextVectorStageReport and rebuild_context_vector_atomically
- Modify: tests/test_cutover_backup.py — test_manifest_records_relative_names_hashes_and_summaries and test_public_summaries_are_guarded_and_reports_frozen
- Modify: tests/test_cutover_checks.py — test_primary_gate_passes_only_when_every_invariant_holds
- Modify: tests/test_cutover_vector.py — test_stage_reads_only_list_vector_documents_from_the_store

**Interfaces:**

- Consumes: deterministic MaintenancePlan, pre epoch and semantic/config/key state, existing SQLite backup and vector inspection helpers.
- Produces: MaintenanceBackupManager.create(run_id: str, plan: MaintenancePlan, pre_state: MaintenancePreState) -> VerifiedMaintenanceBackup; MaintenanceBackupManager.restore(backup: VerifiedMaintenanceBackup) -> BackupRestoreResult; ProjectMaintenanceVerifier.verify(plan: MaintenancePlan, run_snapshot: MaintenanceRunSnapshot) -> MaintenanceVerificationResult. No writable maintenance entry point exists in this task.

- [ ] **Step 1: Write failing WAL-consistent backup and private-manifest tests.**

~~~python
def test_verified_backup_captures_wal_key_and_config_before_returning(
    maintenance_backup_fixture,
):
    backup = maintenance_backup_fixture.manager.create(
        run_id="run_backup",
        plan=maintenance_backup_fixture.plan,
        pre_state=maintenance_backup_fixture.pre_state,
    )

    assert backup.run_id == "run_backup"
    assert backup.plan_digest == maintenance_backup_fixture.plan.plan_digest
    assert backup.quick_check == "ok"
    assert backup.private_manifest.key_bytes == maintenance_backup_fixture.key_bytes
    assert backup.private_manifest.key_mode == 0o600
    assert backup.private_manifest.config_bytes == maintenance_backup_fixture.config_bytes
    public = backup.public_dict()
    assert set(public) == {
        "run_id",
        "plan_digest",
        "database_sha256",
        "key_fingerprint",
        "counts",
        "reason_codes",
        "quick_check",
    }
~~~

Also test uncheckpointed WAL rows, owner-only directories/files, exact run/plan binding, absent-key state, reopen plus PRAGMA quick_check, config/key snapshot failure returning no verified handle, tampering refusal, exact bytes/mode restore, and public projection privacy.

- [ ] **Step 2: Run backup tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_maintenance_backup.py tests/test_cutover_backup.py
~~~

Expected failure: MaintenanceBackupManager or VerifiedMaintenanceBackup is missing.

- [ ] **Step 3: Implement backup completion before a verified handle can exist.**

~~~python
def create(
    self,
    run_id: str,
    plan: MaintenancePlan,
    pre_state: MaintenancePreState,
) -> VerifiedMaintenanceBackup:
    directory = self._create_owner_only_run_directory(run_id)
    database_path = directory / "context.sqlite3"
    encoded = urllib.parse.quote(str(self._config.db_path.resolve()), safe="/")
    with sqlite3.connect(f"file:{encoded}?mode=ro", uri=True) as source:
        with sqlite3.connect(database_path) as target:
            source.backup(target)
    with sqlite3.connect(f"file:{database_path}?mode=ro", uri=True) as check:
        quick_check = check.execute("PRAGMA quick_check").fetchone()[0]
    if quick_check != "ok":
        raise MaintenanceBackupError("backup_quick_check_failed")
    private = self._snapshot_private_state(directory, pre_state)
    manifest = self._write_and_fsync_manifest(
        directory=directory,
        run_id=run_id,
        plan=plan,
        pre_state=pre_state,
        private=private,
        quick_check=quick_check,
    )
    return VerifiedMaintenanceBackup.from_verified_manifest(manifest, private)
~~~

Use the SQLite Backup API for create and restore; never copy the live DB/WAL pair. Restore requires a VerifiedMaintenanceBackup and exact key-state semantics: restore bytes and original safe mode when present; when pre_key_state is absent, remove only the run-generated key after its post fingerprint matches. Public serializers omit paths and private bytes.

- [ ] **Step 4: Run backup tests and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_maintenance_backup.py tests/test_cutover_backup.py
~~~

Expected: all backup, manifest, and restore primitive tests pass.

- [ ] **Step 5: Write failing pure-verifier tests.**

~~~python
def test_verifier_requires_plan_exact_counts_sources_vectors_and_epoch_ownership(
    verifier_fixture,
):
    result = verifier_fixture.verifier.verify(
        verifier_fixture.plan,
        verifier_fixture.run_snapshot,
    )

    assert result.ok is True
    assert result.reason_codes == ()
    assert result.actual_counts == verifier_fixture.plan.counts

    drifted = verifier_fixture.with_unowned_epoch()
    refused = drifted.verifier.verify(drifted.plan, drifted.run_snapshot)
    assert refused.ok is False
    assert "unowned_mutation_epoch" in refused.reason_codes
~~~

The verifier must check mapping lag zero; exactly L0/L1/L2 for mapped items; resolution/project consistency; plan-exact resolved/conflict/unresolved/global/action counts; pending exclusion; registry availability for the installed schema stage; unique required project summaries; exact rollup source coverage and archive holds; projection lag distinct from resolver drift; active L0/vector cardinality; run-owned journal epochs; and no unjournaled semantic digest drift. It performs no state mutation.

- [ ] **Step 6: Run verifier tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_cutover_checks.py tests/test_cutover_vector.py -k "project or rollup or resolution or epoch or count"
~~~

Expected failure: project, rollup, plan-count, or epoch gates are absent.

- [ ] **Step 7: Implement content-free invariant aggregation.**

~~~python
def verify(
    self,
    plan: MaintenancePlan,
    run_snapshot: MaintenanceRunSnapshot,
) -> MaintenanceVerificationResult:
    checks = (
        self._check_mapping_and_layers(plan),
        self._check_project_resolutions(plan),
        self._check_rollups_and_holds(plan),
        self._check_projection_and_resolver_drift(plan),
        self._check_vectors(plan),
        self._check_epoch_ownership(run_snapshot),
        self._check_semantic_digest(run_snapshot),
    )
    reasons = tuple(sorted({code for check in checks for code in check.reason_codes}))
    actual_counts = MaintenanceCounts.merge(check.counts for check in checks)
    return MaintenanceVerificationResult(
        ok=not reasons and actual_counts.matches(plan.counts),
        reason_codes=reasons,
        actual_counts=actual_counts,
    )
~~~

- [ ] **Step 8: Run verifier and backup regressions and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_maintenance_backup.py tests/test_cutover_backup.py tests/test_cutover_checks.py tests/test_cutover_vector.py
~~~

Expected: all listed tests pass and verifier output contains only counts, digests, booleans, and reason codes.

- [ ] **Step 9: Commit Task 8.**

~~~bash
git diff --check
git status --short
git add evolvmem/maintenance_backup.py evolvmem/cutover_backup.py evolvmem/cutover_checks.py evolvmem/cutover_vector.py tests/test_maintenance_backup.py tests/test_cutover_backup.py tests/test_cutover_checks.py tests/test_cutover_vector.py
git commit -m "feat: prepare verified project migration backups"
~~~

Expected: one commit containing only backup and verifier primitives; ProjectMaintenance still has no apply method.

---

### Task 9: Apply, resume, verify, and roll back one staged run safely

**Files:**

- Modify: evolvmem/project_maintenance.py — apply, resume, verify, rollback, stage dispatcher, and persistent run gate
- Modify: evolvmem/maintenance_backup.py — PreparedWorkspaceKeyBootstrap, owner-only pending marker, exact orphan cleanup
- Create: tests/test_project_maintenance_apply.py — backup ordering, gate, bootstrap, backfill, retry, and 451-row cases
- Create: tests/test_project_maintenance_rollback.py — drift refusal and exact restore cases
- Modify: tests/test_maintenance_backup.py — prepared key install, marker, cleanup, and recovery cases
- Modify: evolvmem/cutover_lock.py — CutoverLock.exclusive integration
- Modify: evolvmem/context_migration.py — LegacyMemoryMigrator.migrate, LegacyMemoryMigrator.migrate_projection_row
- Modify: evolvmem/context_service.py — ContextService._require_initialized and all public semantic writers
- Modify: tests/test_cutover_lock.py — test_lock_is_released_when_the_holder_process_raises
- Modify: tests/test_context_migration.py — test_migration_preserves_legacy_metadata_layers_sources_and_supersession and duplicate-active tests
- Modify: tests/test_context_service.py — legacy mutation and production-write gate cases
- Modify: tests/test_production_write_boundaries.py — test_production_modules_have_no_raw_write_bypasses

**Interfaces:**

- Consumes: approved Task 7 plan digest, Task 8 VerifiedMaintenanceBackup and verifier, exclusive cutover lock, resolver/registry, rollup, lifecycle, and vector services.
- Produces: ProjectMaintenance.apply(approved_plan_digest: str) -> MaintenanceRunResult; ProjectMaintenance.resume(run_id: str, approved_plan_digest: str) -> MaintenanceRunResult; ProjectMaintenance.verify(run_id: str) -> MaintenanceVerificationResult; ProjectMaintenance.rollback(run_id: str) -> MaintenanceRollbackResult; ContextService writer errors maintenance_bootstrap_pending and maintenance_incomplete.

- [ ] **Step 1: Write failing backup-before-write and atomic persistent-gate tests.**

~~~python
def test_apply_orders_verified_backup_key_bootstrap_and_atomic_schema_gate(
    apply_fixture,
):
    result = apply_fixture.maintenance.apply(apply_fixture.plan.plan_digest)
    backup = apply_fixture.backup_for(result.run_id)

    assert apply_fixture.events[:7] == [
        "exclusive_lock_acquired",
        "plan_recomputed",
        "run_id_allocated",
        "backup_quick_check_ok",
        "workspace_key_pending_guard_created",
        "workspace_key_installed",
        "approved_schema_and_planned_gate_committed",
    ]
    assert result.run_id.startswith("run_")
    assert backup.pre_key_state == "absent"
    assert apply_fixture.reverify_backup(backup).verified is True
    assert apply_fixture.bootstrap_staging_is_outside(backup.directory) is True
    assert apply_fixture.first_business_epoch_owner() == result.run_id


def test_crash_after_key_install_before_db_gate_blocks_writers_and_retry_cleans_orphan(
    pre_gate_crash_fixture,
):
    pre_gate_crash_fixture.crash_after("workspace_key_installed")
    assert pre_gate_crash_fixture.fcntl_lock_is_available() is True
    assert pre_gate_crash_fixture.store.incomplete_maintenance_run() is None
    assert pre_gate_crash_fixture.pending_key_guard_exists() is True
    with pytest.raises(ContextServiceError, match="maintenance_bootstrap_pending"):
        pre_gate_crash_fixture.service.legacy_add(pre_gate_crash_fixture.add_request)

    result = pre_gate_crash_fixture.retry_apply()

    assert result.status == "completed"
    assert pre_gate_crash_fixture.orphan_key_cleanup_count == 1
    assert pre_gate_crash_fixture.pending_key_guard_exists() is False


def test_crash_after_atomic_gate_commit_uses_db_gate_and_resumes_exact_run(
    post_gate_crash_fixture,
):
    post_gate_crash_fixture.crash_after("approved_schema_and_planned_gate_committed")
    run = post_gate_crash_fixture.store.incomplete_maintenance_run()
    assert run is not None
    assert run.stage == "planned"
    with pytest.raises(ContextServiceError, match="maintenance_incomplete"):
        post_gate_crash_fixture.service.legacy_add(post_gate_crash_fixture.add_request)
    result = post_gate_crash_fixture.maintenance.resume(
        run.run_id,
        run.plan_digest,
    )
    assert result.status == "completed"
    assert post_gate_crash_fixture.pending_key_guard_exists() is False
~~~

Also inject failure after the pending marker but before key install and a normal exception during the atomic schema/run transaction. In both cases exact cleanup restores the pre-key state and removes only the matching marker; it never removes a pre-existing key or a mismatched file.

On a legacy-only DB, verified backup captures the original old schema, config, and absent key. The approved key action then creates a fixed owner-only maintenance-bootstrap pending marker, fsyncs a run-private generated-key record in a separate owner-only bootstrap-staging directory, and installs the active key with O_EXCL and mode 0600. The staging directory and marker bind run ID, approved plan digest, verified-backup manifest digest, pre-state digests, key phase, and post-key fingerprint; neither may add, remove, or rewrite anything inside the immutable verified-backup directory or manifest. ContextService first returns maintenance_incomplete when a committed incomplete DB run exists; when no such row/table exists it checks the external marker and returns maintenance_bootstrap_pending. Thus either gate stops writers after a process crash. The first DB write is one SQLite transaction that applies all approved bootstrap_schema actions and inserts stage=planned,status=running with the original canonical plan, verified backup binding, and post-key fingerprint. Schema and planned gate either both commit or both roll back.

- [ ] **Step 2: Run gate and ordering tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_maintenance_apply.py tests/test_cutover_lock.py tests/test_context_service.py tests/test_production_write_boundaries.py -k "backup or gate or maintenance or crash"
~~~

Expected failure: apply is missing or a writer succeeds while an incomplete run exists.

- [ ] **Step 3: Implement lock, replan, backup, and atomic run reservation.**

~~~python
def apply(self, approved_plan_digest: str) -> MaintenanceRunResult:
    with self._lock.exclusive():
        self._cleanup_exact_orphaned_key_bootstrap(approved_plan_digest)
        approved = self._planner.plan(self._plan_request)
        if not hmac.compare_digest(approved.plan_digest, approved_plan_digest):
            raise MaintenanceRefusal("approved_plan_drift")
        run_id = self._new_run_id()
        pre_state = self._capture_pre_state(approved)
        backup = self._backup.create(run_id, approved, pre_state)
        prepared_key = self._backup.prepare_workspace_key_bootstrap(
            run_id,
            approved,
            backup,
        )
        try:
            prepared_key.install()
            with self._bootstrap_and_gate_transaction():
                self._apply_approved_schema_bootstrap(approved)
                self._insert_planned_run(
                    run_id,
                    approved,
                    pre_state,
                    backup,
                    prepared_key.post_fingerprint,
                )
        except Exception:
            if not self._run_exists(run_id):
                prepared_key.restore_pre_state()
            raise
        prepared_key.clear_pending_marker()
        return self._advance_run(run_id)
~~~

The lock-time plan must match both database fingerprint and full digest. The run ID exists only in memory until backup verification and external key preparation succeed. prepare_workspace_key_bootstrap is a no-op for an established safe matching key; for an approved missing key it generates 256 random bits only after backup quick_check, writes the generated key and pending marker outside the immutable backup, fsyncs their containing directories before active-key installation, and records pre_key_state=absent plus the post fingerprint in the private bundle. VerifiedMaintenanceBackup remains byte-for-byte re-verifiable before and after preparation, installation, DB-gate commit, resume, and orphan cleanup.

_cleanup_exact_orphaned_key_bootstrap runs under the exclusive lock before replanning. It cleans only a marker whose plan digest matches the caller, whose verified backup proves the pre-key state, and whose current semantic DB/config digests still equal that backup. A prepared-phase marker with the original key still absent removes only the run-private generated key and marker. An installed-phase marker additionally requires the active key fingerprint to equal the marker before removing that exact run-created key. Any mismatch refuses without changing anything. If the corresponding planned DB run exists, apply returns resume_required and leaves both gates; resume validates the marker/run/backup tuple and clears the marker only after observing the committed DB gate. Approved registry seeds import only when the registry is empty.

- [ ] **Step 4: Run ordering tests and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_maintenance_apply.py tests/test_cutover_lock.py tests/test_context_service.py tests/test_production_write_boundaries.py -k "backup or gate or maintenance or crash"
~~~

Expected: backup ordering and persistent writer-gate tests pass.

- [ ] **Step 5: Write failing stage, exact-retry, backfill, and completion tests.**

~~~python
def test_resume_retries_only_failed_stage_using_stored_old_schema_plan(
    failed_rollup_run,
):
    before = failed_rollup_run.stage_counts()
    result = failed_rollup_run.maintenance.resume(
        failed_rollup_run.run_id,
        failed_rollup_run.plan_digest,
    )

    assert before["backfilled"] == 1
    assert failed_rollup_run.stage_counts()["backfilled"] == 1
    assert failed_rollup_run.stage_counts()["rolling_up"] == 2
    assert result.stage == "verified"
    assert result.status == "completed"
    assert result.attempt == 2
~~~

Add legal sequence planned to backfilled to rolling_up to archived to vector_synced to verified; status running, failed, completed, rolled_back; exact failed_stage; no stage skip or direct complete; resume requiring exact run and digest; old-schema stored-plan validation after schema/key bootstrap rather than reproducing the pre-bootstrap plan; 451 unmapped rows; exact L0/L1/L2/status/time/source/supersession preservation; mapped project repair; conflict collision leaving item unchanged and pending; plan-exact counts; vector rebuild; and second plan with zero business actions.

- [ ] **Step 6: Run stage and backfill tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_maintenance_apply.py tests/test_context_migration.py tests/test_project_rollup.py tests/test_session_summary_lifecycle.py -k "stage or resume or backfill or collision or idempotent"
~~~

Expected failure: stage dispatcher, exact retry, or approved-action backfill is missing.

- [ ] **Step 7: Implement stage dispatch and verified completion.**

~~~python
STAGE_ORDER = (
    "planned",
    "backfilled",
    "rolling_up",
    "archived",
    "vector_synced",
    "verified",
)


def _advance_run(self, run_id: str) -> MaintenanceRunResult:
    run = self._store.get_maintenance_run(run_id)
    while run.stage not in {"vector_synced", "verified"}:
        next_stage = STAGE_ORDER[STAGE_ORDER.index(run.stage) + 1]
        if run.status == "failed" and run.failed_stage != next_stage:
            raise MaintenanceRefusal("failed_stage_mismatch")
        try:
            self._execute_stage(run, next_stage)
        except MaintenanceStageError as exc:
            self._store.mark_run_failed(run_id, next_stage, exc.code)
            raise
        run = self._store.mark_stage_succeeded(run_id, next_stage)
    if run.stage == "verified":
        return self._store.require_completed_run(run_id)
    verification = self._verifier.verify(run.plan, self._snapshot_run(run_id))
    if not verification.ok:
        self._store.mark_run_failed(run_id, "verified", "verification_failed")
        raise MaintenanceStageError("verification_failed")
    return self._store.mark_verified_and_completed(run_id)
~~~

Each semantic stage passes owner_run_id to semantic_transaction. Rollups run outside long DB transactions while the persistent gate refuses production writers. Archive only exact covered IDs. Resume reacquires the exclusive lock, validates stored original plan/config/key/run/backup bindings and current-stage prerequisites, increments attempt, and retries only failed_stage. Only verifier success writes stage=verified,status=completed.

- [ ] **Step 8: Run stage and backfill tests and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_maintenance_apply.py tests/test_context_migration.py tests/test_project_rollup.py tests/test_session_summary_lifecycle.py -k "stage or resume or backfill or collision or idempotent"
~~~

Expected: all focused stage, backfill, and idempotency tests pass.

- [ ] **Step 9: Write failing exact-run rollback tests.**

~~~python
@pytest.mark.parametrize(
    "drift_kind,reason_code",
    (
        ("unowned_epoch", "rollback_epoch_drift"),
        ("semantic_digest", "rollback_state_drift"),
        ("config", "rollback_config_drift"),
        ("workspace_key", "rollback_key_drift"),
    ),
)
def test_rollback_refuses_every_post_run_drift(rollback_fixture, drift_kind, reason_code):
    rollback_fixture.inject_drift(drift_kind)
    with pytest.raises(MaintenanceRefusal, match=reason_code):
        rollback_fixture.maintenance.rollback(rollback_fixture.run_id)


def test_clean_rollback_restores_database_config_key_and_vectors(rollback_fixture):
    result = rollback_fixture.maintenance.rollback(rollback_fixture.run_id)
    assert result.status == "rolled_back"
    assert rollback_fixture.current_semantic_snapshot() == rollback_fixture.pre_semantic_snapshot
    assert rollback_fixture.current_private_state() == rollback_fixture.pre_private_state
    assert rollback_fixture.vector_ids() == rollback_fixture.pre_active_l0_ids
    assert rollback_fixture.rollback_audit(rollback_fixture.run_id).status == "rolled_back"
~~~

Also refuse latest, empty/wrong run ID, missing/unsafe/tampered manifest, an epoch not owned by the run, post semantic digest mismatch, post config digest mismatch, and post key fingerprint mismatch.

- [ ] **Step 10: Run rollback tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_maintenance_rollback.py
~~~

Expected failure: rollback is missing or accepts drift.

- [ ] **Step 11: Implement drift-gated exact restore.**

~~~python
def rollback(self, run_id: str) -> MaintenanceRollbackResult:
    with self._lock.exclusive():
        run = self._store.require_exact_run(run_id)
        backup = self._backup.require_verified_for_run(run_id, run.plan_digest)
        current = self._snapshot_run(run_id)
        if current.unowned_epochs:
            raise MaintenanceRefusal("rollback_epoch_drift")
        if current.semantic_digest != run.post_state_digest:
            raise MaintenanceRefusal("rollback_state_drift")
        if current.config_digest != run.post_config_digest:
            raise MaintenanceRefusal("rollback_config_drift")
        if current.key_fingerprint != run.post_key_fingerprint:
            raise MaintenanceRefusal("rollback_key_drift")
        self._backup.restore(backup)
        self._reopen_store_after_restore()
        self._vector_rebuilder.rebuild()
        self._verify_restored_pre_state(run, backup)
        return self._record_restored_rollback_audit(run)
~~~

Restore DB and config through verified primitives. Restore exact key bytes/mode, or remove only the matching run-generated key when pre_key_state was absent. Reopen the store, rebuild vectors, and verify pre epoch, semantic digest, config digest, key state, and active L0 IDs. Then create only the minimal non-semantic maintenance bookkeeping schema when the restored old DB lacks it and insert an audit tombstone for this exact run with status=rolled_back; this bookkeeping is excluded from the semantic digest and persistent-writer gate. Do not recreate project, resolution, rollup, or continuity schema after an old-schema restore.

- [ ] **Step 12: Run the full Task 9 slice and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_maintenance_apply.py tests/test_project_maintenance_rollback.py tests/test_context_migration.py tests/test_cutover_lock.py tests/test_context_service.py tests/test_production_write_boundaries.py tests/test_project_rollup.py tests/test_session_summary_lifecycle.py tests/test_maintenance_backup.py tests/test_cutover_checks.py tests/test_cutover_vector.py
~~~

Expected: all listed tests pass; the 451-row fixture reaches verified/completed, second plan has zero business actions, and rollback drift cases refuse.

- [ ] **Step 13: Commit Task 9.**

~~~bash
git diff --check
git status --short
git add evolvmem/project_maintenance.py evolvmem/maintenance_backup.py evolvmem/cutover_lock.py evolvmem/context_migration.py evolvmem/context_service.py tests/test_project_maintenance_apply.py tests/test_project_maintenance_rollback.py tests/test_maintenance_backup.py tests/test_cutover_lock.py tests/test_context_migration.py tests/test_context_service.py tests/test_production_write_boundaries.py
git commit -m "feat: apply and roll back resumable project maintenance"
~~~

Expected: one commit containing only Task 9 files.

---

### Task 10: Expose operator CLIs, exact acceptance wrappers, and primary gates

**Files:**

- Create: evolvmem/project_cli.py — project registry command parser and typed delegation
- Create: evolvmem/maintenance_cli.py — plan, apply, resume, verify, rollback parser and typed delegation
- Create: tests/test_project_cli.py — operator command and double-CAS contract
- Create: tests/test_maintenance_cli.py — maintenance output and exit-code contract
- Create: tests/test_maintenance_acceptance.py — temp harness and real wrapper parser safety
- Create: scripts/accept_project_continuity_migration.py — owned-temp end-to-end harness
- Create: scripts/accept_real_project_continuity_migration.py — exact real wrapper with no alternate migration logic
- Modify: evolvmem/cutover_cli.py — _build_parser, _emit, main
- Modify: evolvmem/cutover_checks.py — verify_primary_gate and collect_primary_gate_evidence
- Modify: evolvmem/runtime_contract.py — project-maintenance readiness fields
- Modify: tests/test_cutover_cli.py — test_backup_cutover_and_rollback_require_their_arguments
- Modify: tests/test_runtime_contract.py — test_context_configuration_defaults_match_the_frozen_design_values
- Modify: tests/test_production_write_boundaries.py — test_scan_scope_covers_every_adapter

**Interfaces:**

- Consumes: ProjectService, ProjectMaintenance, existing cutover reports, and Config.from_file for the fixed live configuration location.
- Produces: project list/register/archive, alias list/register/revoke, binding list/bind/revoke/set-default; maintenance plan/apply/resume/verify/rollback; scripts/accept_real_project_continuity_migration.py exact parser contracts plan --json, apply --json --plan-digest followed by 64 zeroes --authorize-real-mutation PROJECT_CONTINUITY_V1, resume --json --run-id run_test --plan-digest followed by 64 zeroes --authorize-real-mutation PROJECT_CONTINUITY_V1, verify --json --run-id run_test, rollback --json --run-id run_test --authorize-real-rollback PROJECT_CONTINUITY_V1.

- [ ] **Step 1: Write failing typed CLI and content-free output tests.**

~~~python
def test_maintenance_cli_requires_explicit_digest_and_run_id(fake_maintenance, capsys):
    assert maintenance_main(["apply", "--plan-digest", "a" * 64, "--json"], fake_maintenance) == 0
    assert fake_maintenance.calls == [("apply", "a" * 64)]
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) <= {"ok", "run_id", "stage", "status", "counts", "digests", "reason_codes"}

    with pytest.raises(SystemExit):
        maintenance_main(["resume", "--plan-digest", "a" * 64, "--json"], fake_maintenance)
    with pytest.raises(SystemExit):
        maintenance_main(["rollback", "--json"], fake_maintenance)
~~~

Add project, alias, and binding double-CAS commands; maintenance plan/apply/resume/verify/rollback; stable exit codes; JSON-only stdout; exact digest validation; no implicit latest run; and errors containing no content, path, token, exception string, or traceback.

- [ ] **Step 2: Run CLI tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_cli.py tests/test_maintenance_cli.py tests/test_cutover_cli.py
~~~

Expected failure: project_cli or maintenance_cli import failure.

- [ ] **Step 3: Implement thin parser-to-service delegation.**

~~~python
def _cmd_apply(args: argparse.Namespace, service: ProjectMaintenance) -> int:
    result = service.apply(args.plan_digest)
    _emit_json(
        {
            "ok": result.status == "completed",
            "run_id": result.run_id,
            "stage": result.stage,
            "status": result.status,
            "counts": result.counts.public_dict(),
            "digests": result.public_digests(),
            "reason_codes": list(result.reason_codes),
        }
    )
    return 0 if result.status == "completed" else 3
~~~

Parsing constructs immutable requests and delegates. plan is the only command that emits a new approval digest. apply and resume require the digest verbatim. Keep ContextService.archive_project for session archive purge distinct from ProjectService.archive_registry_project.

- [ ] **Step 4: Run the CLI tests and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_cli.py tests/test_maintenance_cli.py tests/test_cutover_cli.py
~~~

Expected: all CLI contract tests pass.

- [ ] **Step 5: Write failing exact real-wrapper parser and pre-open authorization tests.**

~~~python
@pytest.mark.parametrize(
    "argv,expected_call",
    (
        (["plan", "--json"], ("plan",)),
        (
            [
                "apply", "--json", "--plan-digest", "0" * 64,
                "--authorize-real-mutation", "PROJECT_CONTINUITY_V1",
            ],
            ("apply", "0" * 64),
        ),
        (
            [
                "resume", "--json", "--run-id", "run_test",
                "--plan-digest", "0" * 64,
                "--authorize-real-mutation", "PROJECT_CONTINUITY_V1",
            ],
            ("resume", "run_test", "0" * 64),
        ),
        (["verify", "--json", "--run-id", "run_test"], ("verify", "run_test")),
        (
            [
                "rollback", "--json", "--run-id", "run_test",
                "--authorize-real-rollback", "PROJECT_CONTINUITY_V1",
            ],
            ("rollback", "run_test"),
        ),
    ),
)
def test_real_wrapper_exact_subcommands_delegate(argv, expected_call, real_wrapper_harness):
    assert real_wrapper_harness.main(argv) == 0
    assert real_wrapper_harness.calls == [expected_call]


def test_real_wrapper_rejects_missing_ack_before_opening_writable_config(real_wrapper_harness):
    code = real_wrapper_harness.main(
        ["apply", "--json", "--plan-digest", "0" * 64]
    )
    assert code == 2
    assert real_wrapper_harness.config_opens == 0
    assert real_wrapper_harness.service_opens == 0
~~~

Parser tests use only inert run_test and zero digests against fake services. Reject any data-dir or backup-dir argument. plan opens read-only/query-only. apply, resume, and rollback check the exact acknowledgement before Config.from_file or any writable service is constructed.

- [ ] **Step 6: Run wrapper tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_maintenance_acceptance.py -k "real_wrapper or subcommand or authorization"
~~~

Expected failure: scripts.accept_real_project_continuity_migration is missing.

- [ ] **Step 7: Implement the exact real wrapper.**

~~~python
REAL_ACK = "PROJECT_CONTINUITY_V1"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subcommands = parser.add_subparsers(dest="command", required=True)
    plan = subcommands.add_parser("plan")
    plan.add_argument("--json", action="store_true", required=True)
    apply = subcommands.add_parser("apply")
    apply.add_argument("--json", action="store_true", required=True)
    apply.add_argument("--plan-digest", required=True, type=_sha256_digest)
    apply.add_argument("--authorize-real-mutation", required=True)
    resume = subcommands.add_parser("resume")
    resume.add_argument("--json", action="store_true", required=True)
    resume.add_argument("--run-id", required=True, type=_run_id)
    resume.add_argument("--plan-digest", required=True, type=_sha256_digest)
    resume.add_argument("--authorize-real-mutation", required=True)
    verify = subcommands.add_parser("verify")
    verify.add_argument("--json", action="store_true", required=True)
    verify.add_argument("--run-id", required=True, type=_run_id)
    rollback = subcommands.add_parser("rollback")
    rollback.add_argument("--json", action="store_true", required=True)
    rollback.add_argument("--run-id", required=True, type=_run_id)
    rollback.add_argument("--authorize-real-rollback", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command in {"apply", "resume"} and args.authorize_real_mutation != REAL_ACK:
        return _emit_refusal("real_mutation_not_authorized")
    if args.command == "rollback" and args.authorize_real_rollback != REAL_ACK:
        return _emit_refusal("real_rollback_not_authorized")
    service = _build_readonly_service() if args.command == "plan" else _build_live_service()
    return _delegate_exact_command(args, service)
~~~

_delegate_exact_command calls ProjectMaintenance directly. apply does not accept a run ID because it owns lock-time replan, in-memory run allocation, verified backup, recoverable optional key bootstrap, atomic schema-plus-run-gate reservation, and stages. The wrapper never selects a latest run/backup and contains no migration SQL.

- [ ] **Step 8: Run wrapper tests and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_maintenance_acceptance.py -k "real_wrapper or subcommand or authorization"
~~~

Expected: exact parser, acknowledgement, no-path-argument, and delegation tests pass.

- [ ] **Step 9: Write failing temp-harness and primary-gate tests.**

~~~python
def test_temp_acceptance_runs_only_in_owned_temp_root(acceptance_harness):
    report = acceptance_harness.run()
    assert report["ok"] is True
    assert report["fixture_unmapped_count"] == 451
    assert report["second_plan_business_actions"] == 0
    assert report["stage_failure_resumed"] is True
    assert report["drifted_rollback_refused"] is True
    assert report["clean_rollback_verified"] is True
    assert report["real_config_opened"] is False


def test_primary_gate_requires_all_project_foundation_invariants(healthy_gate_evidence):
    assert verify_primary_gate(healthy_gate_evidence).ok is True
    failed = replace(healthy_gate_evidence, pending_items_excluded=False)
    assert verify_primary_gate(failed).reason_codes == ("pending_items_not_excluded",)
~~~

Add unique-owned-temp refusal, deterministic failure injection at backup/stage/vector/rollback, resolvable project item gate, resolution agreement, pending exclusion, unique and required summaries, mapping, rollup/hold/vector consistency, journal integrity, and incomplete-maintenance refusal. Continuity pointer gates report not_installed until the continuity plan implements them; primary cannot claim final suite readiness until those gates pass.

- [ ] **Step 10: Run harness and gate tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_maintenance_acceptance.py tests/test_cutover_checks.py tests/test_runtime_contract.py tests/test_production_write_boundaries.py -k "temp or acceptance or project or primary or maintenance"
~~~

Expected failure: temp script entry point or new primary-gate evidence is missing.

- [ ] **Step 11: Implement the temp harness and gate aggregation.**

~~~python
def run_temp_acceptance() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="evolvmem-project-continuity-") as raw_root:
        root = Path(raw_root).resolve()
        config = Config(data_dir=root / "data")
        config.ensure_dirs()
        fixture = AcceptanceFixture.create(config, unmapped_count=451)
        first = fixture.maintenance.plan(fixture.plan_request)
        repeated = fixture.maintenance.plan(fixture.plan_request)
        if first.canonical_json() != repeated.canonical_json():
            raise AcceptanceFailure("plan_not_repeatable")
        fixture.inject_one_stage_failure("rolling_up")
        failed_run = fixture.apply_expect_stage_failure(first.plan_digest)
        completed = fixture.maintenance.resume(failed_run.run_id, first.plan_digest)
        verified = fixture.maintenance.verify(completed.run_id)
        second = fixture.maintenance.plan(fixture.plan_request)
        drift_refused = fixture.prove_drifted_rollback_refusal(completed.run_id)
        clean_rollback = fixture.prove_clean_rollback(first)
        return fixture.public_report(
            completed,
            verified,
            second,
            drift_refused,
            clean_rollback,
        )
~~~

The fixture owns data, config, key, vector, and backup roots below its TemporaryDirectory and refuses any resolved path outside it. Output contains counts, booleans, digests, run IDs, stages, statuses, and reason codes only.

- [ ] **Step 12: Run the temp-harness and primary-gate tests and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_maintenance_acceptance.py tests/test_cutover_checks.py tests/test_runtime_contract.py tests/test_production_write_boundaries.py -k "temp or acceptance or project or primary or maintenance"
~~~

Expected: the focused harness, primary-gate, runtime, and writer-boundary tests pass.

- [ ] **Step 13: Run the temp acceptance executable and observe PASS.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/python scripts/accept_project_continuity_migration.py
~~~

Expected: one content-free JSON PASS report, fixture_unmapped_count=451, zero second-plan business actions, resumed injected failure, drift-refused rollback, clean rollback, and no real config/data access.

- [ ] **Step 14: Run focused and full verification.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_project_cli.py tests/test_maintenance_cli.py tests/test_maintenance_acceptance.py tests/test_cutover_cli.py tests/test_cutover_checks.py tests/test_runtime_contract.py tests/test_production_write_boundaries.py
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q
git diff --check
~~~

Expected: zero failures. Record exact pass and skip counts. Do not execute scripts/accept_real_project_continuity_migration.py.

- [ ] **Step 15: Commit Task 10.**

~~~bash
git status --short
git add evolvmem/project_cli.py evolvmem/maintenance_cli.py evolvmem/cutover_cli.py evolvmem/cutover_checks.py evolvmem/runtime_contract.py scripts/accept_project_continuity_migration.py scripts/accept_real_project_continuity_migration.py tests/test_project_cli.py tests/test_maintenance_cli.py tests/test_maintenance_acceptance.py tests/test_cutover_cli.py tests/test_runtime_contract.py tests/test_production_write_boundaries.py
git commit -m "test: gate real project continuity migration"
~~~

Expected: one commit containing only Task 10 code, scripts, and tests.

---

### Task 11: Review and hand off the data foundation without touching real data

**Files:**

- Create: docs/superpowers/reports/2026-09-01-evolvmem-project-memory-cleanup-verification.md — exact evidence and integration handoff
- Modify: tests/test_docs_context_core.py — add test_project_cleanup_verification_report_defers_dirty_wip_integration after runbook contract tests

**Interfaces:**

- Consumes: Tasks 1 through 10 commit range, full pytest evidence, temp acceptance report, original dirty-WIP inventory/hashes, and the approved design.
- Produces: reviewed data-foundation branch and a content-free verification report that explicitly defers dirty-WIP integration to docs/superpowers/plans/2026-09-01-evolvmem-continuity-protocol.md Task 11 before final suite or real-data readiness can be claimed.

- [ ] **Step 1: Invoke superpowers-requesting-code-review.**

Ask the reviewer to compare the Task 1 through 10 diff with the design and this plan. Require review of resolver trust, domain-separated HMAC and key bootstrap, decide-before-insert/record-after-insert, telemetry-free digest, one-epoch transactions, old-schema read-only planning, backup-before-write ordering, atomic persistent gate, stored-plan resume, rollback, L0/L1/L2 preservation, rollup failure behavior, coverage-aware archive holds, and exact real-wrapper commands.

Expected: the reviewer returns no unresolved blocking findings. If a blocking finding exists, hard-stop Task 11: revise this plan first by adding the exact failing test, red command and failure, implementation code, green command and pass expectation to the owning earlier task; review the revised plan; then execute that revision and request code review again. Do not improvise an ad hoc review-driven implementation.

- [ ] **Step 2: Write the failing report-contract test.**

~~~python
from pathlib import Path


def test_project_cleanup_verification_report_defers_dirty_wip_integration():
    report = Path(
        "docs/superpowers/reports/"
        "2026-09-01-evolvmem-project-memory-cleanup-verification.md"
    ).read_text(encoding="utf-8")

    assert "scope: isolated_temp_data_only" in report
    assert "real_data_mutated: false" in report
    assert "dirty_wip_integration: deferred_to_continuity_task_11" in report
    assert "tests/test_context_skill.py" in report
    assert "tests/test_session_miner.py" in report
    assert "scripts/mine_skill_tasks.py" in report
    assert "final_suite_ready: false" in report
    assert "/home/jiangli/.evolvmem" not in report
~~~

- [ ] **Step 3: Run the report test and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_docs_context_core.py -k project_cleanup_verification_report
~~~

Expected failure: FileNotFoundError for the verification report.

- [ ] **Step 4: Run final isolated evidence before writing the report.**

~~~bash
cd /home/jiangli/hermes-memory-plugin
wip_state_dir=/home/jiangli/.local/state/evolvmem-project-continuity
git diff HEAD --binary -- README.md dsh/src/common.js dsh/src/sweep.js evolvmem/config.py evolvmem/context_models.py evolvmem/context_service.py evolvmem/context_store.py evolvmem/mcp_contract.py evolvmem/mcp_server.py tests/test_integration.py tests/test_mcp_protocol.py | sha256sum | diff -u "$wip_state_dir/tracked-wip.sha256" -
sha256sum evolvmem/context_skill.py evolvmem/session_miner.py scripts/mine_skill_tasks.py tests/test_context_skill.py tests/test_session_miner.py | diff -u "$wip_state_dir/untracked-wip.sha256" -
cd /home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity
wip_state_dir=/home/jiangli/.local/state/evolvmem-project-continuity
umask 077
read -r implementation_base < "$wip_state_dir/implementation-base.commit"
test ! -e "$wip_state_dir/implementation-head.commit"
git merge-base --is-ancestor "$implementation_base" HEAD
git log --format=%H -1 "$implementation_base"
git log --format=%H -1 HEAD
git rev-parse HEAD | tee "$wip_state_dir/implementation-head.commit"
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/python scripts/accept_project_continuity_migration.py
git diff --check
git status --short
~~~

Expected: the first two git-log lines are the full implementation-base and Task-10 HEAD hashes, the ancestor check exits 0, and pytest and the harness have zero failures. Record exact pass/skip counts, the base..HEAD reviewed commit range, temp harness counts/digests, and remaining not_installed continuity gates. Do not execute the real wrapper.

- [ ] **Step 5: Create the report directory without creating report content.**

~~~bash
cd /home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity
install -d -m 755 docs/superpowers/reports
test -d docs/superpowers/reports
git status --short
~~~

Expected: the directory exists with mode 0755; because Git does not track empty directories, status still contains no report file or unrelated change.

- [ ] **Step 6: Write the exact content-free report and dirty-WIP handoff.**

Use apply_patch to create docs/superpowers/reports/2026-09-01-evolvmem-project-memory-cleanup-verification.md with this constant portion exactly:

~~~text
# Project Memory Cleanup Verification

scope: isolated_temp_data_only
real_data_mutated: false
temp_acceptance: PASS
fixture_unmapped_count: 451
second_plan_business_actions: 0
dirty_wip_integration: deferred_to_continuity_task_11
dirty_wip_required_tests:
  - tests/test_context_skill.py
  - tests/test_session_miner.py
  - tests/test_kimi_hooks.py
  - tests/test_context_service.py
dirty_wip_required_script:
  - scripts/mine_skill_tasks.py
dirty_wip_rule: reapply or copy the preserved dirty-main behavior only in continuity Task 11, then run the listed tests and the full suite before real-data approval
continuity_gates: not_installed
final_suite_ready: false
next_plan: docs/superpowers/plans/2026-09-01-evolvmem-context-web-v2.md
~~~

Add reviewed_commit_range using, in order, the two full hashes printed by the exact `git log --format=%H -1` commands in Step 4, separated by two periods. This range is base-exclusive and HEAD-inclusive, so it covers Tasks 1 through 10. Add pytest_passed and pytest_skipped using the exact integers from the final pytest terminal summary. Add the content-free plan and acceptance digests printed by the temp harness. The report must not contain real data paths, memory/archive text, L1/L2, queries, tokens, private manifests, or an ad hoc current-task memory.

- [ ] **Step 7: Run the report contract and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_docs_context_core.py -k project_cleanup_verification_report
git diff --check
~~~

Expected: the report contract passes.

- [ ] **Step 8: Commit the verification handoff.**

~~~bash
git status --short
git add docs/superpowers/reports/2026-09-01-evolvmem-project-memory-cleanup-verification.md tests/test_docs_context_core.py
git commit -m "docs: record project cleanup verification"
~~~

Expected: the report and its contract test are the only staged files. The branch is data-foundation complete but final_suite_ready remains false until the Web and continuity plans, dirty-WIP integration, real approval, and final repair record are complete.

- [ ] **Step 9: Recheck dirty-main hashes and remove only the content-free evidence records.**

~~~bash
cd /home/jiangli/hermes-memory-plugin
wip_state_dir=/home/jiangli/.local/state/evolvmem-project-continuity
git diff HEAD --binary -- README.md dsh/src/common.js dsh/src/sweep.js evolvmem/config.py evolvmem/context_models.py evolvmem/context_service.py evolvmem/context_store.py evolvmem/mcp_contract.py evolvmem/mcp_server.py tests/test_integration.py tests/test_mcp_protocol.py | sha256sum | diff -u "$wip_state_dir/tracked-wip.sha256" -
sha256sum evolvmem/context_skill.py evolvmem/session_miner.py scripts/mine_skill_tasks.py tests/test_context_skill.py tests/test_session_miner.py | diff -u "$wip_state_dir/untracked-wip.sha256" -
read -r implementation_head < "$wip_state_dir/implementation-head.commit"
git -C /home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity merge-base --is-ancestor "$implementation_head" HEAD
rm -- "$wip_state_dir/tracked-wip.sha256"
rm -- "$wip_state_dir/untracked-wip.sha256"
rm -- "$wip_state_dir/implementation-base.commit"
rm -- "$wip_state_dir/implementation-head.commit"
rmdir -- "$wip_state_dir"
git status --short
~~~

Expected: both hash comparisons and the recorded Task-10-HEAD ancestor check exit 0 before deletion. Only the two WIP hash records, two commit-anchor records, and their now-empty exact state directory are removed; dirty-main status is unchanged, and no WIP body was ever copied or deleted.

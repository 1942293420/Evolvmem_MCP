# EvolvMem Context Web v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-subagent-driven-development (recommended) or superpowers-executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Context Core L0/L1/L2, canonical projects, rolling-summary state, and project-resolution review the default EvolvMem library experience while preserving the exact legacy HTTP contracts and preventing stale or unauthorized mutations.

**Architecture:** ContextService is the only business boundary used by Web code. ContextStore adds narrow parameterized admin reads and inherited row-version CAS operations; ProjectService remains the project registry and resolution authority created by the prerequisite plan. A strict ContextV2Router translates HTTP requests to typed service requests. One WebMutationSecurity policy covers v2 and legacy traffic. The browser is a small vanilla-JavaScript module application that fetches L0 first, L1 only for exact detail, and L2 only after an explicit action.

**Tech Stack:** Python 3.10+, stdlib http.server, immutable dataclasses and enums, SQLite keyset pagination, HMAC-bound Base64url cursors, vanilla HTML/CSS/JavaScript modules, pytest, and temporary loopback HTTP servers.

**Design source:** docs/superpowers/specs/2026-09-01-evolvmem-project-continuity-design.md, especially lines 572-616.

**Prerequisite:** Complete and review docs/superpowers/plans/2026-09-01-evolvmem-project-memory-cleanup.md through Task 11 on the same isolated feature branch. That plan owns ContextItem.row_version, semantic_transaction(), ProjectService, registry and resolution schemas, project rollups, maintenance readiness, and the temporary acceptance script. This Web plan consumes those interfaces and must not recreate them.

## Global Constraints

- Continue in /home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity on feat/evolvmem-project-continuity. Do not implement this plan in the dirty main checkout, create another feature branch, or stage unrelated files.
- Use superpowers-test-driven-development for every production behavior and superpowers-verification-before-completion before any completion claim.
- The line ranges below describe the 2026-09-01 main-checkout baseline. After the prerequisite plan moves lines, locate the named symbol anchor before editing.
- Preserve the exact success and error payload shapes, methods, paths, 500-row cap, key/value fields, and 64 KiB update limit for GET /api/stats, GET /api/memories, and POST /api/memory/{id}/update|archive|restore|delete|hard_delete. Authorized or loopback legacy clients must observe no v2 envelope, cursor, 410 response, or renamed field.
- Legacy routes continue through LegacyCompatibilityFacade and ContextService. They never open SQLite or write the legacy table directly.
- ContextItem.row_version is inherited from the prerequisite plan. Do not add another version column, trigger, counter, or Web-owned CAS layer.
- Every v2 item mutation carries expected_row_version. Registry, alias, binding, and resolution mutations carry the exact registry, row, item, and resolution revisions required by ProjectService.
- Frozen legacy mutation bodies have no version field and are the only row-CAS compatibility exemption. After BEGIN IMMEDIATE has started, the service reads the mapped ContextItem row_version and passes that exact value to the same Store CAS helper in the same transaction. A v2 writer that committed first becomes the version legacy reads; otherwise the competing writer loses with conflict. Legacy never performs an unconditional Core update and never claims stale-form detection.
- Task 7 consumes ProjectResolutionReviewRequest with the exact fields item_id, action, project, expected_revision, expected_item_row_version, actor, and run_id. project is Optional[str]: ACCEPT_PROJECT carries the validated project, while every other action carries None. The prerequisite implementation must expose that constructor before Web work begins; a missing field is an incomplete prerequisite, not permission to create a Web-specific review DTO.
- Web modules call typed ContextService methods only. evolvmem/web_v2.py and evolvmem/web_security.py must not import sqlite3, ContextStore, ProjectStore, MemoryStore, vector internals, or access service.store.
- Core list order is exactly updated_at descending, then id descending. Related sources and evidence use created_at descending, then id descending. Cursor tokens are at most 1024 characters, authenticate their payload, bind endpoint plus canonical filters, and carry a typed order anchor.
- Default page size is 50, Core item maximum is 100, and related-source/evidence maximum is 50. Duplicate or unknown query parameters are errors.
- Core list rows contain only ID, row version, identity, project, scope, content type, status, tier, confidence, importance, L0, mapping state, source state, resolution state, rollup state when applicable, and timestamps. They never contain L1, L2, legacy value, source text, evidence note, workspace path, reviewer identity, or lease identifiers.
- The default Core request is status=active. Consequently the homepage excludes archived raw session_summary items. An operator may see them only by explicitly selecting archived status and session_summary content type.
- Exact item detail defaults to L0 plus bounded provenance. layer=l1 discloses L1 for that exact ID. layer=l2 or include_l2=true discloses L2 for that exact ID only. Detail includes bounded source records, direct predecessor/successor links, and bounded structured project-resolution evidence; none of those may cause an implicit L2 read.
- GET /api/v2/context/workstreams and GET /api/v2/context/workstreams/{id} are reserved here and return the bounded continuity_unavailable response until the continuity plan supplies its domain service.
- Core PATCH changes only tier, importance, confidence, tags, and expires_at. Project assignment uses the resolution endpoint. Lifecycle uses archive, restore, and delete actions. There is no v2 physical hard-delete route or default UI control.
- Loopback keeps the established local no-credential behavior. Non-loopback startup requires a nonblank operator token and one exact allowed HTTP or HTTPS Origin. Non-loopback API reads require Bearer authentication; every non-loopback mutation additionally requires exact Origin and a per-process CSRF nonce. Secret comparisons use hmac.compare_digest.
- The operator token is entered by the user and retained only in a JavaScript module variable. It does not appear in HTML, status JSON, logs, URLs, cookies, localStorage, or sessionStorage. An authenticated status response exposes the CSRF nonce only in X-EvolvMem-CSRF.
- Status and errors are content-free: versions, counts, flags, stages, and stable reason codes only. They contain no memory content, query, path, token, nonce, exception message, traceback, reviewer identity, or run/lease identifier.
- Static assets use an exact allowlist and explicit MIME types. Never map an untrusted URL path to a filesystem path.
- All tests use temporary databases and ephemeral loopback ports. Do not run scripts/accept_real_project_continuity_migration.py, mutate real data, or bind a real non-loopback listener.

## Upstream Interface Ledger

The cleanup plan is an implementation prerequisite, not an informal reference. Before Task 1, verify that its named task produced every interface below. Stop at the prerequisite if a constructor, field, enum value, or return type differs; do not create a Web-local substitute.

| Producer | Interface consumed here | Exact Web-relevant contract |
|---|---|---|
| Cleanup Task 1 | ProjectResolutionState | resolved, conflict, unresolved, global, and ignored |
| Cleanup Task 2 | ContextStore.semantic_transaction | semantic_transaction(kind: str, owner_run_id: Optional[str] = None), current_mutation_epoch() returning int, and exactly one epoch/journal entry per outer semantic mutation |
| Cleanup Task 3 | ContextItem.row_version | Positive int initialized to 1 for old rows; incremented once by semantic metadata, lifecycle, source-state, outcome, supersession, or project changes; unchanged by access telemetry |
| Cleanup Task 3 | ProjectService read methods | list_projects(ProjectPageRequest) returning ProjectPage; list_aliases(ProjectAliasPageRequest) returning ProjectAliasPage; list_workspace_bindings(WorkspaceBindingPageRequest) returning WorkspaceBindingPage; list_resolutions(ResolutionPageRequest) returning ResolutionPage |
| Cleanup Task 3 | ProjectService mutation methods | register_project(RegisterProjectRequest), archive_registry_project(ArchiveProjectRequest), register_alias(RegisterProjectAliasRequest), revoke_alias(RevokeProjectAliasRequest), bind_workspace(BindProjectWorkspaceRequest), revoke_workspace_binding(RevokeProjectWorkspaceRequest), set_default_workspace_binding(SetDefaultProjectWorkspaceRequest), and review_resolution(ProjectResolutionReviewRequest) |
| Cleanup Task 3 | ProjectResolutionReviewAction | accept_project, reject_project, confirm_global, and ignore |
| Cleanup Task 3 | Registry pages | items, next_cursor, has_more, and registry_revision; ordering is updated_at descending then stable row identity descending |
| Cleanup Task 3 | ProjectRecord | project, status, revision, registry_revision, created_at, and updated_at |
| Cleanup Task 3 | ProjectAliasRecord | alias, project, revision, registry_revision, created_at, and updated_at |
| Cleanup Task 3 | ProjectWorkspaceBindingRecord | workspace_fingerprint, project, state, is_default, method, revision, registry_revision, created_at, and updated_at; no workspace path or remote |
| Cleanup Task 3 | ProjectResolutionRecord | item_id, resolution_state, decision_source, review_state, proposed_project, resolved_project, previous_project, confidence, method, bounded evidence, resolver_version, revision, item_row_version, reviewed_at, created_at, and updated_at; Web projections omit run_id and reviewed_by_hash |
| Cleanup Task 5 | context_project_rollups water row | project, current_context_id, source_set_hash, covered_through, generator_version, run_id, status, revision, and updated_at; status is pending, ready, failed, or vector_dirty |
| Cleanup Tasks 9 and 10 | maintenance and readiness reports | content-free status, stage, failed_stage, schema version, mutation epoch, mapping/resolution/rollup/vector counts, readiness flags, and reason codes; Web omits run IDs and paths |
| Cleanup Task 10 | temporary acceptance harness | scripts/accept_project_continuity_migration.py; it owns only unique temporary roots and emits content-free verification |

Cleanup Task 3 must expose these exact request fields because Task 7 constructs them directly:

- ProjectPageRequest: status: str = "", limit: int = 50, cursor: str = "".
- RegisterProjectRequest: project: str, expected_registry_revision: int, expected_row_revision: int, actor: str, run_id: str. A create uses expected_row_revision=0.
- ArchiveProjectRequest: project: str, expected_registry_revision: int, expected_row_revision: int, actor: str, run_id: str.
- ProjectAliasPageRequest: project: str = "", limit: int = 50, cursor: str = "".
- RegisterProjectAliasRequest: alias: str, project: str, expected_registry_revision: int, expected_row_revision: int, actor: str, run_id: str. A create uses expected_row_revision=0.
- RevokeProjectAliasRequest: alias: str, expected_registry_revision: int, expected_row_revision: int, actor: str, run_id: str.
- WorkspaceBindingPageRequest: project: str = "", workspace_fingerprint: str = "", state: str = "", limit: int = 50, cursor: str = "".
- BindProjectWorkspaceRequest: workspace_fingerprint: str, project: str, state: str, is_default: bool, method: str, expected_registry_revision: int, expected_row_revision: int, actor: str, run_id: str. A create uses expected_row_revision=0.
- RevokeProjectWorkspaceRequest: workspace_fingerprint: str, project: str, expected_registry_revision: int, expected_row_revision: int, actor: str, run_id: str.
- SetDefaultProjectWorkspaceRequest: workspace_fingerprint: str, project: str, expected_registry_revision: int, expected_row_revision: int, actor: str, run_id: str.
- ResolutionPageRequest: review_state: str = "", resolution_state: Optional[ProjectResolutionState] = None, project: str = "", limit: int = 50, cursor: str = "".
- ProjectResolutionReviewRequest: item_id: int, action: ProjectResolutionReviewAction, project: Optional[str], expected_revision: int, expected_item_row_version: Optional[int], actor: str, run_id: str. action is accept_project, reject_project, confirm_global, or ignore; project and expected_item_row_version are required only for accept_project, and the other actions carry None for both fields.

The continuity plan is downstream of this plan. No continuity service DTO is consumed while Web Tasks 1-9 execute. This plan produces the following exact handoff:

- Web Task 2 produces ContinuityAdminListRequest(project, workspace_fingerprint, status, limit=50, cursor="") and ContinuityAdminGetRequest(workstream_id, include_l2=False).
- Web Task 4 produces ContextV2Router and reserves GET /api/v2/context/workstreams plus GET /api/v2/context/workstreams/{id} with continuity_unavailable.
- Web Task 8 produces loadWorkstreams() and renderWorkstreamDetail(container, result) in pages/workstreams.js.
- Continuity Task 6 later produces ContinuityStatus, ContinuityAdminListResult, and ContinuityWorkstreamDetail and implements ContinuityService.admin_list/admin_get.
- Continuity Task 9 consumes the Web Task 2/4/8 handoff and activates the reserved routes without renaming the DTOs, router, or JavaScript symbols.

## Execution preflight

- [ ] Enter the prerequisite worktree and prove the expected branch and clean state.

~~~bash
cd /home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity
git branch --show-current
git status --short
git log --oneline -12
~~~

Expected: the branch is feat/evolvmem-project-continuity, status has no output, and the prerequisite verification handoff is present.

- [ ] Record the exact cleanup-handoff commit before Task 1.

~~~bash
cd /home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity
umask 077
web_state_dir=/home/jiangli/.local/state/evolvmem-context-web-v2
install -d -m 700 -- "$web_state_dir"
test ! -e "$web_state_dir/web-base.commit"
cleanup_report=docs/superpowers/reports/2026-09-01-evolvmem-project-memory-cleanup-verification.md
git cat-file -e "HEAD:${cleanup_report}"
test "$(git log --format=%H -1 -- "$cleanup_report")" = "$(git log --format=%H -1)"
git log --format=%H -1 | tee "$web_state_dir/web-base.commit"
test "$(wc -l < "$web_state_dir/web-base.commit")" -eq 1
web_base_commit=$(sed -n '1p' "$web_state_dir/web-base.commit")
test "${#web_base_commit}" -eq 40
case "$web_base_commit" in *[!0-9a-f]*) exit 1 ;; esac
git cat-file -e "${web_base_commit}^{commit}"
test "$web_base_commit" = "$(git log --format=%H -1)"
git status --short
~~~

Expected: the command prints one exact 40-character cleanup Task 11 HEAD hash, validates that it is the current commit, stores only that content-free base anchor outside the repository with owner-only permissions, and leaves repository status empty. An existing anchor is a stale or concurrent execution; stop and investigate it instead of overwriting it.

- [ ] Run the unchanged Web tests, project tests, and full suite before writing a test.

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_web_server.py tests/test_project_service.py tests/test_project_store.py
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q
~~~

Expected: zero failures. Record the exact pass and skip counts in the work log.

### Task 1: Freeze legacy contracts and install a disjoint v2 dispatch seam

**Files:**

- Create: evolvmem/web_v2.py — new WebRequest, WebResponse, and unavailable dispatcher at the top of the module.
- Create: tests/test_web_v2.py — new v2 seam and compatibility tests.
- Modify: evolvmem/web_server.py:26-39 at _STATIC_INDEX and _MEMORY_FIELDS; 257-350 at _MEM_ACTION_RE and make_handler().
- Modify: tests/test_web_server.py:1-564 at API characterization tests and the HTTP fixture.

**Interfaces:**

- **Consumes:** api_stats(), api_memories(), api_update(), api_archive(), api_restore(), api_delete(), api_hard_delete(), _MEM_ACTION_RE, and make_handler() from the current server.
- **Produces:** immutable WebRequest and WebResponse records; a make_handler(service, v2_dispatcher=None, security=None) seam whose v2 prefix is disjoint from every legacy route.

- [ ] **Write the failing compatibility test.** Add exact field-set, raw-list, route, and injected-dispatch assertions. The import of evolvmem.web_v2 must fail before production code exists.

~~~python
from evolvmem.web_server import (
    _MEMORY_FIELDS,
    _MEM_ACTION_RE,
    api_memories,
    api_update,
    make_handler,
)
from evolvmem.web_v2 import WebRequest, WebResponse


class RecordingV2:
    def __init__(self) -> None:
        self.requests: list[WebRequest] = []

    def dispatch(self, request: WebRequest) -> WebResponse:
        self.requests.append(request)
        return WebResponse(
            status=200,
            payload={"ok": True, "version": "v2"},
            headers=(),
        )


def test_legacy_contract_is_exact_with_a_v2_dispatcher(backend):
    facade, _, service, ids = backend
    rows = api_memories(facade, {})
    assert isinstance(rows, list)
    assert tuple(rows[0]) == _MEMORY_FIELDS
    assert rows[0]["id"] == ids["hot"]
    assert "key" in rows[0] and "value" in rows[0]
    result = api_update(facade, ids["warm"], {"importance": 8.0})
    assert tuple(result) == ("ok", "memory")
    assert result["memory"]["id"] == ids["warm"]

    for action in ("update", "archive", "restore", "delete", "hard_delete"):
        match = _MEM_ACTION_RE.fullmatch(
            "/api/memory/" + str(ids["warm"]) + "/" + action
        )
        assert match is not None

    dispatcher = RecordingV2()
    handler = make_handler(service, v2_dispatcher=dispatcher)
    assert handler.v2_dispatcher is dispatcher
~~~

Also preserve current HTTP assertions for GET /api/stats, raw GET /api/memories, all five POST action paths, 400/404/413/500 legacy bodies, and the list cap of 500.

- [ ] **Run the focused test and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_web_server.py tests/test_web_v2.py -k "legacy or dispatcher or contract"
~~~

Expected: collection fails because evolvmem.web_v2 or the make_handler keyword does not exist. Do not weaken existing legacy assertions.

- [ ] **Implement the minimum typed seam without implementing v2 routes.**

~~~python
# evolvmem/web_v2.py
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WebRequest:
    method: str
    target: str
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class WebResponse:
    status: int
    payload: object
    headers: Sequence[tuple[str, str]] = ()


class UnavailableV2Dispatcher:
    def dispatch(self, request: WebRequest) -> WebResponse:
        return WebResponse(
            status=503,
            payload={
                "ok": False,
                "error": {"code": "context_v2_unavailable"},
            },
        )
~~~

In make_handler(), store the injected object as MemoryWebHandler.v2_dispatcher. Only paths beginning /api/v2/context/ may call it. A missing dispatcher retains the existing 404. Keep every legacy branch and serializer byte-for-byte equivalent.

~~~python
def _is_v2_path(path: str) -> bool:
    return path == "/api/v2/context" or path.startswith("/api/v2/context/")


def make_handler(service: ContextService, *, v2_dispatcher=None, security=None):
    facade = service.legacy_facade()

    class MemoryWebHandler(BaseHTTPRequestHandler):
        v2_dispatcher = None
        mutation_security = None

        def _send_json(self, payload, status=200, headers=()):
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header(
                "Content-Type",
                "application/json; charset=utf-8",
            )
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _dispatch_v2_get(self) -> bool:
            path = urlparse(self.path).path
            if not _is_v2_path(path) or self.v2_dispatcher is None:
                return False
            response = self.v2_dispatcher.dispatch(
                WebRequest(
                    method="GET",
                    target=self.path,
                    headers=dict(self.headers.items()),
                    body=b"",
                )
            )
            self._send_json(
                response.payload,
                response.status,
                response.headers,
            )
            return True

        def do_GET(self):
            if self._dispatch_v2_get():
                return
            self._dispatch_legacy_get()

    MemoryWebHandler.v2_dispatcher = v2_dispatcher
    MemoryWebHandler.mutation_security = security
    return MemoryWebHandler
~~~

Rename the current do_GET body to _dispatch_legacy_get() without changing its branches. The shown do_GET becomes the only new entry point, so an injected v2 dispatcher cannot capture a legacy path.

- [ ] **Run the focused tests green and verify no legacy diff in behavior.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_web_server.py tests/test_web_v2.py -k "legacy or dispatcher or contract"
git diff --check
~~~

Expected: zero failures; the v2 seam is injectable and all old responses remain exact.

- [ ] **Commit only Task 1 files.**

~~~bash
git status --short
git add evolvmem/web_v2.py evolvmem/web_server.py tests/test_web_v2.py tests/test_web_server.py
git diff --cached --check
git commit -m "test(web): freeze legacy contracts and v2 seam"
~~~

### Task 2: Add immutable admin DTOs, authenticated cursors, and L0-only Store pages

**Files:**

- Create: evolvmem/context_admin_models.py — admin requests, projections, pages, and cursor anchors.
- Create: evolvmem/context_admin.py — canonical filter digest and HMAC cursor codec.
- Create: tests/test_context_admin.py — DTO and cursor tests.
- Modify: evolvmem/context_store.py:257-342 at ContextStore lifecycle; 888-982 at evidence/source reads; 990-1015 at list_item_ids(); 1256-1417 at exact item/layer conversion.
- Modify: tests/test_context_store.py:1-1431 at schema, row conversion, source, evidence, and list tests.
- Read only: evolvmem/context_models.py:39-79 at enums and 158-185 at ContextItem. ContextItem.row_version must already exist after the prerequisite.
- Read only: evolvmem/project_models.py at ProjectResolutionState and rollup records created by the prerequisite.

**Interfaces:**

- **Consumes:** Cleanup Task 1 ProjectResolutionState; Cleanup Task 3 ContextItem.row_version plus registry/resolution tables; Cleanup Task 5 context_project_rollups; existing ContextLayer, ContextStatus, ContextTier, ContextContentType, context_items, context_layers, legacy_memory_migrations, context_sources, and context_evidence.
- **Produces:** ContextItemPageRequest, ContextItemDetailRequest, ContextItemPatchRequest, ContextItemLifecycleRequest, ContextItemL0, ContextItemDetail, ContextItemPage, related provenance pages, AdminCursor, CursorCodec, narrow ContextStore admin-read methods, and the ContinuityAdminListRequest/ContinuityAdminGetRequest shells consumed by the continuity plan.

- [ ] **Write failing DTO, cursor, privacy, and default-filter tests.**

~~~python
from dataclasses import replace

import pytest

from evolvmem.context_admin import CursorCodec, item_filter_digest
from evolvmem.context_admin_models import (
    AdminCursor,
    ContextItemPageRequest,
)
from evolvmem.context_models import ContextStatus, ContextValidationError


def test_cursor_is_authenticated_bounded_and_filter_bound():
    request = ContextItemPageRequest(project="alpha", limit=2)
    digest = item_filter_digest(request)
    codec = CursorCodec(b"0123456789abcdef0123456789abcdef")
    cursor = AdminCursor(
        kind="items",
        filter_digest=digest,
        order_value="2026-09-01 09:00:00",
        item_id=42,
    )
    token = codec.encode(cursor)
    assert len(token) <= 1024
    assert codec.decode(token, kind="items", filter_digest=digest) == cursor

    changed = replace(request, project="beta")
    with pytest.raises(ContextValidationError, match="invalid_cursor"):
        codec.decode(
            token,
            kind="items",
            filter_digest=item_filter_digest(changed),
        )
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    with pytest.raises(ContextValidationError, match="invalid_cursor"):
        codec.decode(tampered, kind="items", filter_digest=digest)


def test_default_item_page_is_active_and_hides_archived_session_summaries(
    populated_admin_store,
):
    page = populated_admin_store.list_admin_items(
        ContextItemPageRequest(),
        anchor=None,
    )
    assert all(row.status is ContextStatus.ACTIVE for row in page)
    assert all(
        not (
            row.status.value == "archived"
            and row.content_type.value == "session_summary"
        )
        for row in page
    )
    assert all(not hasattr(row, "l1") for row in page)
    assert all(not hasattr(row, "l2") for row in page)
~~~

Add separate cases rejecting bool as an integer, zero and negative IDs, limit 0/101, cursor length 1025, invalid enum strings, noncanonical timestamps, duplicate tags, and an empty field mask. Freeze ContinuityAdminListRequest at maximum limit 100 and ContinuityAdminGetRequest at a ws_ prefixed opaque ID plus a strict bool include_l2; these types contain no path or lease field. Add a SQL trace assertion that list_admin_items never selects l1 or l2 content. Add equal-timestamp pagination fixtures proving no duplicate or gap.

- [ ] **Run the focused tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_admin.py tests/test_context_store.py -k "admin or cursor or row_version or session_summary"
~~~

Expected: imports and Store methods are missing. The inherited row_version bootstrap tests from the prerequisite remain green; if they fail, stop and repair the prerequisite task rather than adding Web schema code.

- [ ] **Implement the minimum immutable types, cursor codec, and parameterized reads.**

Use the existing enums rather than parallel string enums. Freeze this L0 shape:

~~~python
# evolvmem/context_admin_models.py
from dataclasses import dataclass

from evolvmem.context_models import (
    ContextContentType,
    ContextLayer,
    ContextStatus,
    ContextTier,
)
from evolvmem.project_models import ProjectResolutionState


@dataclass(frozen=True, slots=True)
class ContextItemPageRequest:
    project: str = ""
    content_type: ContextContentType | None = None
    status: ContextStatus | None = ContextStatus.ACTIVE
    tier: ContextTier | None = None
    resolution_state: ProjectResolutionState | None = None
    limit: int = 50
    cursor: str = ""


@dataclass(frozen=True, slots=True)
class ContextItemDetailRequest:
    item_id: int
    layer: ContextLayer = ContextLayer.L0
    related_limit: int = 25
    sources_cursor: str = ""
    evidence_cursor: str = ""


@dataclass(frozen=True, slots=True)
class ContinuityAdminListRequest:
    project: str = ""
    workspace_fingerprint: str = ""
    status: str = ""
    limit: int = 50
    cursor: str = ""


@dataclass(frozen=True, slots=True)
class ContinuityAdminGetRequest:
    workstream_id: str
    include_l2: bool = False


@dataclass(frozen=True, slots=True)
class ContextItemL0:
    id: int
    row_version: int
    identity_key: str
    project: str
    scope: str
    content_type: str
    status: str
    tier: str
    confidence: float
    importance: float
    l0: str
    mapping_state: str
    source_state: str
    resolution_state: str
    rollup_status: str
    rollup_covered_through: str | None
    created_at: str
    updated_at: str
    expires_at: str | None


@dataclass(frozen=True, slots=True)
class AdminCursor:
    kind: str
    filter_digest: str
    order_value: str
    item_id: int
~~~

ContextItemPatchRequest uses a frozenset field_mask plus typed tier, importance, confidence, tags, and expires_at values so JSON null can intentionally clear expires_at. Its constructor validates that field_mask is nonempty and is a subset of those five names. ContextItemLifecycleRequest carries item_id, a ContextLifecycleAction enum limited to archive, restore, and delete, and positive expected_row_version.

Implement a cursor whose MAC covers the exact canonical JSON bytes:

~~~python
# evolvmem/context_admin.py
import base64
from datetime import datetime
import hashlib
import hmac
import json

from evolvmem.context_admin_models import AdminCursor, ContextItemPageRequest
from evolvmem.context_models import ContextValidationError


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(
        value + padding,
        altchars=b"-_",
        validate=True,
    )


def item_filter_digest(request: ContextItemPageRequest) -> str:
    payload = {
        "content_type": (
            request.content_type.value if request.content_type is not None else ""
        ),
        "project": request.project,
        "resolution_state": (
            request.resolution_state.value
            if request.resolution_state is not None
            else ""
        ),
        "status": request.status.value if request.status is not None else "all",
        "tier": request.tier.value if request.tier is not None else "",
    }
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    return hashlib.sha256(raw).hexdigest()


class CursorCodec:
    def __init__(self, secret: bytes) -> None:
        if not isinstance(secret, bytes) or len(secret) < 32:
            raise ContextValidationError("invalid_cursor_secret")
        self._secret = secret

    def encode(self, cursor: AdminCursor) -> str:
        raw = json.dumps(
            {
                "digest": cursor.filter_digest,
                "id": cursor.item_id,
                "kind": cursor.kind,
                "order": cursor.order_value,
                "v": 1,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
        encoded = _b64encode(raw)
        signature = _b64encode(
            hmac.new(self._secret, raw, hashlib.sha256).digest()
        )
        token = encoded + "." + signature
        if len(token) > 1024:
            raise ContextValidationError("cursor_too_large")
        return token

    def decode(
        self,
        token: str,
        *,
        kind: str,
        filter_digest: str,
    ) -> AdminCursor:
        try:
            if not token or len(token) > 1024:
                raise ValueError
            encoded, signature = token.split(".", 1)
            raw = _b64decode(encoded)
            expected = _b64encode(
                hmac.new(self._secret, raw, hashlib.sha256).digest()
            )
            if not hmac.compare_digest(signature, expected):
                raise ValueError
            value = json.loads(raw.decode("ascii"))
            if set(value) != {"digest", "id", "kind", "order", "v"}:
                raise ValueError
            if value["v"] != 1:
                raise ValueError
            raw_id = value["id"]
            if isinstance(raw_id, bool) or not isinstance(raw_id, int):
                raise ValueError
            order_value = value["order"]
            if not isinstance(order_value, str):
                raise ValueError
            parsed_order = datetime.strptime(
                order_value,
                "%Y-%m-%d %H:%M:%S",
            )
            if parsed_order.strftime("%Y-%m-%d %H:%M:%S") != order_value:
                raise ValueError
            cursor = AdminCursor(
                kind=str(value["kind"]),
                filter_digest=str(value["digest"]),
                order_value=order_value,
                item_id=raw_id,
            )
            if cursor.kind != kind:
                raise ValueError
            if not hmac.compare_digest(cursor.filter_digest, filter_digest):
                raise ValueError
            if cursor.item_id <= 0:
                raise ValueError
            return cursor
        except (UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise ContextValidationError("invalid_cursor") from exc
~~~

Use a fixed-column query. Conditions may be assembled only from a constant mapping; values remain parameters. Fetch limit + 1 and derive has_more and next_cursor above the Store.

~~~python
_ADMIN_ITEM_PAGE_SQL = """
SELECT
    i.id, i.row_version, i.identity_key, i.project, i.scope,
    i.content_type, i.status, i.tier, i.confidence, i.importance,
    l0.content AS l0,
    CASE
        WHEN EXISTS (
            SELECT 1 FROM legacy_memory_migrations m
            WHERE m.context_item_id = i.id
        ) THEN 'mapped'
        ELSE 'unmapped'
    END AS mapping_state,
    i.source_state,
    COALESCE(r.resolution_state, 'none') AS resolution_state,
    COALESCE(pr.status, '') AS rollup_status,
    pr.covered_through AS rollup_covered_through,
    i.created_at, i.updated_at, i.expires_at
FROM context_items i
JOIN context_layers l0 ON l0.item_id = i.id AND l0.layer = 'l0'
LEFT JOIN context_project_resolutions r ON r.item_id = i.id
LEFT JOIN context_project_rollups pr ON pr.current_context_id = i.id
WHERE {where_clause}
ORDER BY i.updated_at DESC, i.id DESC
LIMIT ?
"""


def list_admin_items(self, request, *, anchor):
    clauses = ["1 = 1"]
    params: list[object] = []
    filters = (
        ("project", request.project, "i.project = ?"),
        (
            "content_type",
            request.content_type,
            "i.content_type = ?",
        ),
        ("status", request.status, "i.status = ?"),
        ("tier", request.tier, "i.tier = ?"),
        (
            "resolution_state",
            request.resolution_state,
            "r.resolution_state = ?",
        ),
    )
    for _, value, clause in filters:
        if value not in (None, ""):
            clauses.append(clause)
            params.append(value.value if hasattr(value, "value") else value)
    if anchor is not None:
        clauses.append(
            "(i.updated_at < ? OR (i.updated_at = ? AND i.id < ?))"
        )
        params.extend((anchor.order_value, anchor.order_value, anchor.item_id))
    params.append(request.limit + 1)
    sql = _ADMIN_ITEM_PAGE_SQL.format(
        where_clause=" AND ".join(clauses)
    )
    rows = self._connection().execute(sql, params).fetchall()
    return tuple(self._row_to_admin_l0(row) for row in rows)
~~~

Add exact-ID L0, direct supersession, current-resolution evidence, source-page, and evidence-page Store methods. Source/evidence projections expose only structured IDs, kinds, versions, normalized values, states, and timestamps; omit context_evidence.note and any absolute archive path. Use created_at/id keysets and limit + 1.

- [ ] **Run the focused model and Store tests green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_admin.py tests/test_context_store.py -k "admin or cursor or row_version or session_summary or source or evidence"
git diff --check
~~~

Expected: stable pages have no gaps, the default excludes archived session summaries, list SQL loads L0 only, and all cursor/privacy cases pass.

- [ ] **Commit only Task 2 files.**

~~~bash
git status --short
git add evolvmem/context_admin_models.py evolvmem/context_admin.py evolvmem/context_store.py tests/test_context_admin.py tests/test_context_store.py
git diff --cached --check
git commit -m "feat(context): add bounded admin L0 pages"
~~~

### Task 3: Add typed ContextService admin reads and content-free readiness

**Files:**

- Modify: evolvmem/context_admin.py at CursorCodec and new ContextAdminReader.
- Modify: evolvmem/context_admin_models.py at response pages and ContextAdminStatus.
- Modify: evolvmem/context_service.py:187-280 at ContextService construction/lifecycle; 844-849 at legacy_facade(); add admin methods before typed legacy mutations.
- Modify: evolvmem/cutover_checks.py:458-648 at projection checks; 796-910 at primary evidence/status aggregation.
- Modify: tests/test_context_admin.py at reader composition and layer privacy.
- Modify: tests/test_context_service.py:345-777 at validation, initialization, read gates, and status privacy.
- Modify: tests/test_cutover_checks.py:802-1114 and 1386-1598 at mapping/vector/primary evidence.
- Modify: tests/test_production_write_boundaries.py:1-259 at forbidden Web storage dependencies.

**Interfaces:**

- **Consumes:** Web Task 2 DTOs and Store reads; Cleanup Task 2 mutation_epoch; Cleanup Task 3 registry revision and resolution records; Cleanup Task 5 rollup status; Cleanup Tasks 9 and 10 maintenance status, mapping checks, primary evidence, and existing ContextService mode/readiness rules.
- **Produces:** ContextService.context_admin_status(), list_context_items(), get_context_item(), plus independent admin_read_ready, core_item_write_ready, context_ready, and continuity readiness flags.

- [ ] **Write failing service, detail-provenance, and status tests.**

~~~python
from evolvmem.context_admin_models import (
    ContextItemDetailRequest,
    ContextItemPageRequest,
)
from evolvmem.context_models import ContextLayer


def test_service_admin_detail_discloses_one_layer_and_bounded_provenance(
    primary_project_service,
    seeded_context_id,
    seeded_l1,
):
    detail = primary_project_service.get_context_item(
        ContextItemDetailRequest(
            item_id=seeded_context_id,
            layer=ContextLayer.L1,
            related_limit=2,
        )
    )
    assert detail is not None
    assert detail.layer is ContextLayer.L1
    assert detail.content == seeded_l1
    assert not hasattr(detail, "l1")
    assert not hasattr(detail, "l2")
    assert len(detail.sources.items) <= 2
    assert len(detail.evidence.items) <= 2
    assert len(detail.supersession.predecessors) <= 1
    assert len(detail.supersession.successors) <= 1
    assert detail.resolution.item_id == seeded_context_id


def test_admin_status_is_content_free_and_has_independent_gates(
    primary_project_service,
):
    status = primary_project_service.context_admin_status()
    public = status.to_public_dict()
    assert set(public) == {
        "schema_version",
        "mutation_epoch",
        "mode",
        "registry_revision",
        "maintenance",
        "mapping",
        "resolution",
        "rollup",
        "vector",
        "admin_read_ready",
        "core_item_write_ready",
        "context_ready",
        "continuity",
        "reason_codes",
    }
    encoded = repr(public)
    assert "/home/" not in encoded
    assert "payload_path" not in encoded
    assert "lease_id" not in encoded
~~~

Add cases for legacy, compat, shadow, and primary modes; readable schema with normal retrieval unavailable; incomplete or failed maintenance; vector_dirty rollups; continuity not installed; exact L2 request; missing/deleted/expired item policy; independent related cursors; and reads that do not change access_count or mutation_epoch. Add an AST boundary test rejecting sqlite3, ContextStore, ProjectStore, MemoryStore, service.store, and _connection in web_server.py, web_v2.py, and web_security.py.

- [ ] **Run the focused tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_admin.py tests/test_context_service.py tests/test_cutover_checks.py tests/test_production_write_boundaries.py -k "admin or readiness or provenance or web_boundary"
~~~

Expected: typed admin methods and independent readiness fields are absent.

- [ ] **Implement the minimum reader composition and gates.**

ContextAdminReader owns CursorCodec and the Store reference; it is constructed inside ContextService. Web code sees only ContextService. ContextService creates one 32-byte process-local cursor secret, so cursors survive pagination within the process but fail authentication after restart rather than depending on a persisted credential.

~~~python
import secrets


# In ContextService.__init__()
self._admin_cursor_secret = secrets.token_bytes(32)
self._admin_reader: ContextAdminReader | None = None


def _context_admin_reader(self) -> ContextAdminReader:
    if self._admin_reader is None:
        self._admin_reader = ContextAdminReader(
            self.store,
            CursorCodec(self._admin_cursor_secret),
        )
    return self._admin_reader
~~~

~~~python
class ContextAdminReader:
    def __init__(self, store, cursor_codec: CursorCodec) -> None:
        self._store = store
        self._cursor_codec = cursor_codec

    def list_items(self, request: ContextItemPageRequest) -> ContextItemPage:
        digest = item_filter_digest(request)
        anchor = (
            self._cursor_codec.decode(
                request.cursor,
                kind="items",
                filter_digest=digest,
            )
            if request.cursor
            else None
        )
        rows = self._store.list_admin_items(request, anchor=anchor)
        visible = rows[: request.limit]
        has_more = len(rows) > request.limit
        next_cursor = ""
        if has_more:
            last = visible[-1]
            next_cursor = self._cursor_codec.encode(
                AdminCursor(
                    kind="items",
                    filter_digest=digest,
                    order_value=last.updated_at,
                    item_id=last.id,
                )
            )
        return ContextItemPage(
            items=visible,
            next_cursor=next_cursor,
            has_more=has_more,
        )

    def get_item(
        self,
        request: ContextItemDetailRequest,
    ) -> ContextItemDetail | None:
        summary = self._store.get_admin_item_l0(request.item_id)
        if summary is None:
            return None
        content = summary.l0
        if request.layer is not ContextLayer.L0:
            content = self._store.get_layer(request.item_id, request.layer)
            if content is None:
                return None
        return ContextItemDetail(
            summary=summary,
            layer=request.layer,
            content=content,
            sources=self._source_page(request),
            evidence=self._evidence_page(request),
            supersession=self._store.get_admin_supersession(request.item_id),
            resolution=self._store.get_admin_resolution_evidence(
                request.item_id
            ),
        )
~~~

ContextItemDetail has one content field paired with one layer enum. Do not include dormant l1 or l2 attributes. Supersession links are direct, bounded L0 references. Resolution evidence is the current structured bounded evidence_json projection, never event text or reviewer hash.

Expose exact typed service methods:

~~~python
def list_context_items(
    self,
    request: ContextItemPageRequest,
) -> ContextItemPage:
    self._require_request(request, ContextItemPageRequest)
    self._require_admin_read()
    return self._context_admin_reader().list_items(request)


def get_context_item(
    self,
    request: ContextItemDetailRequest,
) -> ContextItemDetail | None:
    self._require_request(request, ContextItemDetailRequest)
    self._require_admin_read()
    return self._context_admin_reader().get_item(request)


def context_admin_status(self) -> ContextAdminStatus:
    self._require_initialized()
    return self._build_context_admin_status()
~~~

_require_admin_read() checks readable schema and maintenance safety without reusing a serving gate that rejects compat diagnostics. _require_core_item_write() permits only a ready Core schema in compat, shadow, or primary with no active/incomplete maintenance. Registry and resolution repairs use their prerequisite ProjectService readiness rather than the Core-item gate. _build_context_admin_status() copies only the fixed public fields.

- [ ] **Run the focused service/readiness tests green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_admin.py tests/test_context_service.py tests/test_cutover_checks.py tests/test_production_write_boundaries.py -k "admin or readiness or provenance or web_boundary"
git diff --check
~~~

Expected: service reads are side-effect free, exact layer disclosure passes, and recursive status privacy assertions pass.

- [ ] **Commit only Task 3 files.**

~~~bash
git status --short
git add evolvmem/context_admin.py evolvmem/context_admin_models.py evolvmem/context_service.py evolvmem/cutover_checks.py tests/test_context_admin.py tests/test_context_service.py tests/test_cutover_checks.py tests/test_production_write_boundaries.py
git diff --cached --check
git commit -m "feat(context): expose typed admin reads and readiness"
~~~

### Task 4: Implement strict v2 read routing, L0 envelopes, and bounded errors

**Files:**

- Modify: evolvmem/web_v2.py at WebRequest, WebResponse, and UnavailableV2Dispatcher.
- Modify: evolvmem/web_server.py:257-350 at make_handler() GET/POST handling.
- Modify: tests/test_web_v2.py at dispatcher parser, response, privacy, and HTTP integration tests.
- Modify: tests/test_web_server.py:388-564 at HTTP fixtures and legacy coexistence.

**Interfaces:**

- **Consumes:** ContextService.context_admin_status(), list_context_items(), and get_context_item(); Task 1 WebRequest/WebResponse seam.
- **Produces:** GET /api/v2/context/status, GET /api/v2/context/items, GET /api/v2/context/items/{id}, and reserved workstream reads; strict query parsing and one v2 error envelope.

- [ ] **Write failing route, parser, layer, and error tests.**

~~~python
import json

from evolvmem.web_v2 import ContextV2Router, WebRequest


def _request(target: str) -> WebRequest:
    return WebRequest(
        method="GET",
        target=target,
        headers={"Accept": "application/json"},
        body=b"",
    )


def test_item_list_is_l0_only_and_detail_layers_are_explicit(v2_service):
    dispatcher = ContextV2Router(v2_service)
    page = dispatcher.dispatch(
        _request("/api/v2/context/items?limit=1&status=active")
    )
    assert page.status == 200
    row = page.payload["items"][0]
    assert set(row) == {
        "id",
        "row_version",
        "identity_key",
        "project",
        "scope",
        "content_type",
        "status",
        "tier",
        "confidence",
        "importance",
        "l0",
        "mapping_state",
        "source_state",
        "resolution_state",
        "rollup_status",
        "rollup_covered_through",
        "created_at",
        "updated_at",
        "expires_at",
    }
    assert all("l1" not in item for item in page.payload["items"])
    assert all("l2" not in item for item in page.payload["items"])

    detail = dispatcher.dispatch(
        _request("/api/v2/context/items/7?layer=l1&related_limit=2")
    )
    assert detail.status == 200
    assert detail.payload["layer"] == "l1"
    assert len(detail.payload["sources"]["items"]) <= 2
    assert len(detail.payload["evidence"]["items"]) <= 2
    assert "supersession" in detail.payload
    assert "project_resolution" in detail.payload


def test_duplicate_unknown_and_cross_filter_cursor_are_rejected(v2_service):
    dispatcher = ContextV2Router(v2_service)
    for target in (
        "/api/v2/context/items?limit=1&limit=2",
        "/api/v2/context/items?unknown=x",
        "/api/v2/context/items?cursor=" + "x" * 1025,
    ):
        response = dispatcher.dispatch(_request(target))
        assert response.status in (400, 413)
        assert set(response.payload) == {"ok", "error"}
        assert set(response.payload["error"]) == {"code"}
~~~

Add exact tests for status privacy, default active filtering, explicit status=all, item limit 100, related limit 50, malformed percent encoding, invalid UTF-8/JSON, boolean integer rejection, not found, method not allowed, cursor filter mismatch, direct L2 only, include_l2=true only with an exact ID, and unexpected exceptions whose messages contain a fake path/content. Assert reserved workstream paths return 503 with continuity_unavailable.

- [ ] **Run the v2 read tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_web_v2.py tests/test_web_server.py -k "read or layer or cursor or parser or error or legacy"
~~~

Expected: the unavailable dispatcher returns 503 or routes are missing.

- [ ] **Implement strict parsing, projection, and dispatch.**

Parse each query into single values before constructing DTOs:

~~~python
from urllib.parse import parse_qsl, urlsplit


class WebProblem(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


def _single_query(target: str, allowed: frozenset[str]) -> tuple[str, dict[str, str]]:
    parsed = urlsplit(target)
    try:
        pairs = parse_qsl(
            parsed.query,
            keep_blank_values=True,
            strict_parsing=True,
            encoding="utf-8",
            errors="strict",
        )
    except (UnicodeError, ValueError) as exc:
        raise WebProblem(400, "invalid_query") from exc
    values: dict[str, str] = {}
    for key, value in pairs:
        if key not in allowed:
            raise WebProblem(400, "unknown_query_parameter")
        if key in values:
            raise WebProblem(400, "duplicate_query_parameter")
        if key == "cursor" and len(value) > 1024:
            raise WebProblem(413, "cursor_too_large")
        values[key] = value
    return parsed.path, values
~~~

Project the L0 record with an explicit field map:

~~~python
def _l0_json(item: ContextItemL0) -> dict[str, object]:
    return {
        "id": item.id,
        "row_version": item.row_version,
        "identity_key": item.identity_key,
        "project": item.project,
        "scope": item.scope,
        "content_type": item.content_type,
        "status": item.status,
        "tier": item.tier,
        "confidence": item.confidence,
        "importance": item.importance,
        "l0": item.l0,
        "mapping_state": item.mapping_state,
        "source_state": item.source_state,
        "resolution_state": item.resolution_state,
        "rollup_status": item.rollup_status,
        "rollup_covered_through": item.rollup_covered_through,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "expires_at": item.expires_at,
    }
~~~

Dispatch only exact routes:

~~~python
_ITEM_DETAIL_RE = re.compile(r"^/api/v2/context/items/([1-9][0-9]*)$")
_WORKSTREAM_DETAIL_RE = re.compile(
    r"^/api/v2/context/workstreams/(ws_[A-Za-z0-9_-]{1,125})$"
)


class ContextV2Router:
    def __init__(self, service: ContextService, security=None) -> None:
        self._service = service
        self._security = security

    def dispatch(self, request: WebRequest) -> WebResponse:
        try:
            path = urlsplit(request.target).path
            if path == "/api/v2/context/status":
                _, query = _single_query(request.target, frozenset())
                if request.method != "GET":
                    raise WebProblem(405, "method_not_allowed")
                status = self._service.context_admin_status()
                return WebResponse(200, {"ok": True, "status": status.to_public_dict()})
            if path == "/api/v2/context/items":
                _, query = _single_query(
                    request.target,
                    frozenset(
                        {
                            "project",
                            "content_type",
                            "status",
                            "tier",
                            "resolution_state",
                            "limit",
                            "cursor",
                        }
                    ),
                )
                if request.method != "GET":
                    raise WebProblem(405, "method_not_allowed")
                page = self._service.list_context_items(
                    _item_page_request(query)
                )
                return WebResponse(200, _item_page_json(page))
            detail_match = _ITEM_DETAIL_RE.fullmatch(path)
            if detail_match is not None:
                _, query = _single_query(
                    request.target,
                    frozenset(
                        {
                            "layer",
                            "include_l2",
                            "related_limit",
                            "sources_cursor",
                            "evidence_cursor",
                        }
                    ),
                )
                if request.method != "GET":
                    raise WebProblem(405, "method_not_allowed")
                detail = self._service.get_context_item(
                    _item_detail_request(int(detail_match.group(1)), query)
                )
                if detail is None:
                    raise WebProblem(404, "item_not_found")
                return WebResponse(200, _detail_json(detail))
            if (
                path == "/api/v2/context/workstreams"
                or _WORKSTREAM_DETAIL_RE.fullmatch(path) is not None
            ):
                _single_query(request.target, frozenset())
                if request.method != "GET":
                    raise WebProblem(405, "method_not_allowed")
                raise WebProblem(503, "continuity_unavailable")
            raise WebProblem(404, "route_not_found")
        except WebProblem as exc:
            return _problem(exc.status, exc.code)
        except ContextValidationError:
            return _problem(400, "invalid_request")
        except ContextServiceError as exc:
            return _service_problem(exc.code)
        except Exception:
            return _problem(500, "internal_error")
~~~

_item_detail_request() rejects layer plus include_l2 conflicts and never selects L2 unless layer=l2 or include_l2=true. _detail_json() emits content under one content key, sources/evidence page envelopes, direct supersession links, and current project_resolution evidence. It never serializes dataclasses generically.

In web_server.py, read a v2 body only after validating a decimal Content-Length at or below 65536, construct WebRequest, call the dispatcher, copy only its fixed response headers, and use the existing JSON writer. Add do_PATCH for v2 only. Legacy do_POST retains its old reader and body/error shapes.

- [ ] **Run v2 reads and legacy coexistence green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_web_v2.py tests/test_web_server.py -k "read or layer or cursor or parser or error or legacy"
git diff --check
~~~

Expected: exact v2 envelopes pass, no list/detail privacy regression occurs, and every old test remains green.

- [ ] **Commit only Task 4 files.**

~~~bash
git status --short
git add evolvmem/web_v2.py evolvmem/web_server.py tests/test_web_v2.py tests/test_web_server.py
git diff --cached --check
git commit -m "feat(web): add strict Context v2 read API"
~~~

### Task 5: Enforce non-loopback Bearer, Origin, and CSRF policy across old and new APIs

**Files:**

- Create: evolvmem/web_security.py — immutable startup configuration and request policy.
- Create: tests/test_web_security.py — host classification, startup, authorization, and leakage tests.
- Modify: evolvmem/web_server.py:260-385 at make_handler(), run(), and main().
- Modify: evolvmem/web_v2.py at ContextV2Router authorization and status headers.
- Modify: tests/test_web_server.py:388-564 at temporary HTTP server and legacy authorization tests.
- Modify: tests/test_web_v2.py at authenticated status/read/mutation tests.

**Interfaces:**

- **Consumes:** Task 1 handler seam, Task 4 dispatcher, run(host, port, data_dir), and all legacy/v2 route classifications.
- **Produces:** WebSecurityConfig.from_startup(), WebMutationSecurity.authorize_read(), authorize_mutation(), status_headers(), and startup refusal before database/server creation.

- [ ] **Write failing startup, shared-gate, and secret-leakage tests.**

~~~python
import pytest

from evolvmem.web_security import WebSecurityConfig, WebSecurityError


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_keeps_local_contract(host):
    config = WebSecurityConfig.from_startup(
        host=host,
        operator_token="",
        allowed_origin="",
    )
    assert config.loopback is True


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.0.2.10"])
def test_nonloopback_refuses_missing_credentials_before_server_creation(host):
    with pytest.raises(WebSecurityError, match="operator_token_required"):
        WebSecurityConfig.from_startup(
            host=host,
            operator_token="",
            allowed_origin="https://memory.example",
        )
    with pytest.raises(WebSecurityError, match="allowed_origin_required"):
        WebSecurityConfig.from_startup(
            host=host,
            operator_token="operator-secret-value",
            allowed_origin="",
        )


def test_security_repr_and_failures_never_contain_secrets():
    config = WebSecurityConfig.from_startup(
        host="0.0.0.0",
        operator_token="operator-secret-value",
        allowed_origin="https://memory.example",
    )
    assert "operator-secret-value" not in repr(config)
    assert "operator-secret-value" not in repr(config.policy())
~~~

Add parameterized HTTP cases for every existing legacy mutation and the frozen v2 mutation path patterns with missing/wrong Bearer, wrong Origin, missing/wrong CSRF, and valid headers. At this point an authorized future v2 mutation path still returns its Task 4 route-not-found response; the security assertion is that unauthorized requests are rejected before routing. Tasks 6 and 7 repeat the matrix after those mutations become functional. Add non-loopback v2 read tests requiring only Bearer. Assert static HTML can load before token entry, authenticated status returns X-EvolvMem-CSRF only as a header, and token/nonce are absent from body, log capture, exception repr, URL, and HTML. Reject wildcard, null, userinfo, path, query, fragment, and non-HTTP origins.

- [ ] **Run security tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_web_security.py tests/test_web_server.py tests/test_web_v2.py -k "startup or auth or bearer or origin or csrf or secret"
~~~

Expected: web_security is missing and at least one legacy mutation bypasses policy.

- [ ] **Implement the minimum immutable policy and wire it once.**

~~~python
# evolvmem/web_security.py
from dataclasses import dataclass, field
import hashlib
import hmac
import ipaddress
import secrets
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit


class WebSecurityError(RuntimeError):
    """Raised when Web startup security is incomplete or invalid."""


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _valid_origin(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme in {"http", "https"}
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and parsed.path == ""
        and parsed.query == ""
        and parsed.fragment == ""
        and "*" not in value
        and value != "null"
    )


@dataclass(frozen=True, slots=True)
class WebSecurityConfig:
    host: str
    loopback: bool
    operator_token: str = field(repr=False)
    allowed_origin: str

    @classmethod
    def from_startup(
        cls,
        *,
        host: str,
        operator_token: str,
        allowed_origin: str,
    ) -> "WebSecurityConfig":
        loopback = _is_loopback_host(host)
        if not loopback and not operator_token.strip():
            raise WebSecurityError("operator_token_required")
        if not loopback and not _valid_origin(allowed_origin):
            raise WebSecurityError("allowed_origin_required")
        return cls(
            host=host,
            loopback=loopback,
            operator_token=operator_token,
            allowed_origin=allowed_origin,
        )

    def policy(self) -> "WebMutationSecurity":
        return WebMutationSecurity(self)


@dataclass(frozen=True, slots=True)
class AuthorizationFailure:
    status: int
    code: str


class WebMutationSecurity:
    def __init__(self, config: WebSecurityConfig) -> None:
        self._config = config
        self._csrf_nonce = secrets.token_urlsafe(32)

    def authorize_read(
        self,
        headers: Mapping[str, str],
    ) -> AuthorizationFailure | None:
        if self._config.loopback:
            return None
        expected = "Bearer " + self._config.operator_token
        actual = headers.get("Authorization", "")
        if not hmac.compare_digest(actual, expected):
            return AuthorizationFailure(401, "unauthorized")
        return None

    def authorize_mutation(
        self,
        headers: Mapping[str, str],
    ) -> AuthorizationFailure | None:
        failure = self.authorize_read(headers)
        if failure is not None:
            return failure
        if self._config.loopback:
            return None
        if headers.get("Origin", "") != self._config.allowed_origin:
            return AuthorizationFailure(403, "origin_forbidden")
        csrf = headers.get("X-EvolvMem-CSRF", "")
        if not hmac.compare_digest(csrf, self._csrf_nonce):
            return AuthorizationFailure(403, "csrf_forbidden")
        return None

    def status_headers(self) -> Sequence[tuple[str, str]]:
        return (("X-EvolvMem-CSRF", self._csrf_nonce),)

    def operator_actor(self) -> str:
        digest = hmac.new(
            self._config.operator_token.encode("utf-8"),
            b"evolvmem-web-operator-v1",
            hashlib.sha256,
        ).hexdigest()
        return "web:" + digest
~~~

Use case-insensitive header normalization before policy calls. For v2, map failures to the v2 code-only envelope. For legacy denials, retain the legacy top-level string error shape. Gate all non-loopback API reads with authorize_read() and every POST/PATCH mutation with authorize_mutation(). Call status_headers() only after the status request is authorized.

In run(), construct WebSecurityConfig before Config.from_file(), ContextService.initialize(), or HTTPServer. Read the token from EVOLVMEM_WEB_OPERATOR_TOKEN and the exact origin from --allowed-origin or EVOLVMEM_WEB_ALLOWED_ORIGIN. Never accept a token in a URL or print it. Remove the current database-path text from the startup banner.

- [ ] **Run security and compatibility tests green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_web_security.py tests/test_web_server.py tests/test_web_v2.py -k "startup or auth or bearer or origin or csrf or secret or legacy"
git diff --check
~~~

Expected: all route families use one policy, startup fails before storage initialization, and secret scans pass.

- [ ] **Commit only Task 5 files.**

~~~bash
git status --short
git add evolvmem/web_security.py evolvmem/web_server.py evolvmem/web_v2.py tests/test_web_security.py tests/test_web_server.py tests/test_web_v2.py
git diff --cached --check
git commit -m "fix(web): secure non-loopback API access"
~~~

### Task 6: Add inherited-row-CAS item PATCH and lifecycle actions

**Files:**

- Modify: evolvmem/context_admin_models.py at ContextItemPatchRequest and ContextItemLifecycleRequest.
- Modify: evolvmem/context_models.py:26-37 at ContextServiceError; add a typed ContextConflictError without changing ContextItem.row_version ownership.
- Modify: evolvmem/context_store.py:1086-1255 at supersession/status/update/hard-delete operations; add CAS helpers beside set_item_status().
- Modify: evolvmem/context_service.py:850-1254 at typed legacy mutations; add v2 item mutations before legacy_facade().
- Modify: evolvmem/web_v2.py at JSON-body parsing and item mutation routes.
- Modify: tests/test_context_admin.py at patch/lifecycle DTO and service CAS tests.
- Modify: tests/test_context_models.py at typed conflict-code validation.
- Modify: tests/test_web_v2.py at PATCH/action HTTP tests.
- Modify: tests/test_legacy_compat.py:620-910 and 1013-1205 at legacy status/metadata transaction behavior.
- Modify: tests/test_production_write_boundaries.py:1-259 at semantic-transaction and CAS enforcement.
- Read only unless a test proves a facade translation defect: evolvmem/legacy_compat.py:26-159 at LegacyCompatibilityFacade.

**Interfaces:**

- **Consumes:** Cleanup Task 2 ContextStore.semantic_transaction(); Cleanup Task 3 ContextItem.row_version and item-version rules; Web Task 2 mutation DTOs; Web Task 3 _require_core_item_write(); Web Task 5 security policy; existing typed legacy facade.
- **Produces:** ContextConflictError with stale_item and illegal_item_transition codes; ContextService.update_context_item() and transition_context_item(); PATCH /api/v2/context/items/{id}; POST archive, restore, and delete; Store metadata/status CAS used by both v2 and legacy paths.

- [ ] **Write failing CAS, rollback, allowed-field, epoch, and legacy-exemption tests.**

~~~python
import pytest

from evolvmem.context_admin_models import (
    ContextItemLifecycleRequest,
    ContextItemPatchRequest,
)
from evolvmem.context_models import ContextConflictError


def test_item_patch_requires_current_version_and_advances_once(admin_service, item):
    before_epoch = admin_service.store.current_mutation_epoch()
    updated = admin_service.update_context_item(
        ContextItemPatchRequest(
            item_id=item.id,
            expected_row_version=item.row_version,
            field_mask=frozenset({"importance", "tags"}),
            importance=8.0,
            tags=("reviewed",),
        )
    )
    assert updated.row_version == item.row_version + 1
    assert admin_service.store.current_mutation_epoch() == before_epoch + 1

    with pytest.raises(ContextConflictError) as conflict:
        admin_service.update_context_item(
            ContextItemPatchRequest(
                item_id=item.id,
                expected_row_version=item.row_version,
                field_mask=frozenset({"importance"}),
                importance=4.0,
            )
        )
    assert conflict.value.code == "stale_item"
    current = admin_service.store.get_item(item.id)
    assert current.importance == 8.0
    assert current.row_version == updated.row_version
    assert admin_service.store.current_mutation_epoch() == before_epoch + 1


def test_legacy_update_reads_version_inside_same_write_transaction(
    legacy_cas_probe_service,
    mapped_legacy_id,
):
    legacy_cas_probe_service.legacy_facade().update_metadata(
        mapped_legacy_id,
        importance=7.0,
    )
    probe = legacy_cas_probe_service.store.probe
    assert probe == [
        ("read_version", True),
        ("metadata_cas", True),
    ]
~~~

Add DTO cases for missing, bool, zero, and stale expected_row_version; empty PATCH; unknown keys; project, scope, status, identity_key, and content_type rejection; invalid tier, importance, confidence, tags, and expires_at; legal and illegal lifecycle transitions; projection/event rollback; one row-version and one mutation-epoch increment; no v2 hard-delete; and unchanged legacy request/response bodies. Add a race where v2 captures version N, legacy commits using the version it reads under BEGIN IMMEDIATE, and the stale v2 request gets 409 without partial state.

- [ ] **Run mutation tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_models.py tests/test_context_admin.py tests/test_web_v2.py tests/test_legacy_compat.py tests/test_production_write_boundaries.py -k "patch or lifecycle or row_version or legacy_cas or mutation_epoch or conflict"
~~~

Expected: v2 mutation methods/routes and shared CAS helpers are absent.

- [ ] **Implement the minimum same-transaction CAS and typed service methods.**

Add the bounded conflict type used by Store, service, and Web:

~~~python
class ContextConflictError(RuntimeError):
    _CODES = frozenset({"stale_item", "illegal_item_transition"})

    def __init__(self, code: str) -> None:
        if code not in self._CODES:
            raise ContextValidationError("invalid_context_conflict_code")
        self.code = code
        super().__init__(code)
~~~

Use a fixed field-to-column map and one conditional update:

~~~python
def update_item_metadata_cas(
    self,
    request: ContextItemPatchRequest,
) -> ContextItem:
    self._require_transaction("update_item_metadata_cas")
    values = {
        "tier": request.tier.value if request.tier is not None else None,
        "importance": request.importance,
        "confidence": request.confidence,
        "tags": (
            self._encode_tags(request.tags)
            if request.tags is not None
            else None
        ),
        "expires_at": request.expires_at,
    }
    assignments: list[str] = []
    params: list[object] = []
    for field_name in (
        "tier",
        "importance",
        "confidence",
        "tags",
        "expires_at",
    ):
        if field_name in request.field_mask:
            assignments.append(field_name + " = ?")
            params.append(values[field_name])
    now = _now_iso()
    assignments.extend(("row_version = row_version + 1", "updated_at = ?"))
    params.extend((now, request.item_id, request.expected_row_version))
    cursor = self._connection().execute(
        "UPDATE context_items SET "
        + ", ".join(assignments)
        + " WHERE id = ? AND row_version = ?",
        params,
    )
    if cursor.rowcount != 1:
        raise ContextConflictError("stale_item")
    item = self.get_item(request.item_id)
    if item is None:
        raise ContextConflictError("stale_item")
    return item
~~~

Implement lifecycle CAS with an explicit transition table; do not call the non-CAS set_item_status() from Web paths.

~~~python
_LIFECYCLE_TARGETS = {
    ("active", "archive"): "archived",
    ("archived", "restore"): "active",
    ("active", "delete"): "deleted",
    ("archived", "delete"): "deleted",
    ("superseded", "delete"): "deleted",
}


def transition_item_status_cas(
    self,
    request: ContextItemLifecycleRequest,
) -> ContextItem:
    self._require_transaction("transition_item_status_cas")
    row = self._connection().execute(
        "SELECT status, row_version FROM context_items WHERE id = ?",
        (request.item_id,),
    ).fetchone()
    if row is None or row["row_version"] != request.expected_row_version:
        raise ContextConflictError("stale_item")
    target = _LIFECYCLE_TARGETS.get(
        (row["status"], request.action.value)
    )
    if target is None:
        raise ContextConflictError("illegal_item_transition")
    cursor = self._connection().execute(
        "UPDATE context_items "
        "SET status=?, row_version=row_version+1, updated_at=? "
        "WHERE id=? AND row_version=? AND status=?",
        (
            target,
            _now_iso(),
            request.item_id,
            request.expected_row_version,
            row["status"],
        ),
    )
    if cursor.rowcount != 1:
        raise ContextConflictError("stale_item")
    item = self.get_item(request.item_id)
    if item is None:
        raise ContextConflictError("stale_item")
    return item
~~~

ContextService owns the outer semantic transaction, projection synchronization, and one result:

~~~python
def update_context_item(
    self,
    request: ContextItemPatchRequest,
) -> ContextItem:
    self._require_request(request, ContextItemPatchRequest)
    self._require_core_item_write()
    with self._cutover_lock.shared():
        with self.store.semantic_transaction("web_context_item_update"):
            updated = self.store.update_item_metadata_cas(request)
            self._sync_mapped_legacy_metadata(updated)
            return updated


def transition_context_item(
    self,
    request: ContextItemLifecycleRequest,
) -> ContextItem:
    self._require_request(request, ContextItemLifecycleRequest)
    self._require_core_item_write()
    with self._cutover_lock.shared():
        with self.store.semantic_transaction(
            "web_context_item_" + request.action.value
        ):
            updated = self.store.transition_item_status_cas(request)
            self._sync_mapped_legacy_status(updated)
            return updated
~~~

For the legacy compatibility exemption, change only the service internals. Keep the old facade and HTTP shapes. The version read and CAS must both occur after semantic_transaction() has entered its BEGIN IMMEDIATE transaction:

~~~python
def get_item_row_version_for_update(self, item_id: int) -> int:
    self._require_transaction("get_item_row_version_for_update")
    row = self._connection().execute(
        "SELECT row_version FROM context_items WHERE id = ?",
        (item_id,),
    ).fetchone()
    if row is None:
        raise ContextServiceError(
            "degraded_legacy",
            "mapped item missing",
        )
    return int(row["row_version"])


with self.store.semantic_transaction("legacy_metadata_update"):
    context_id = self.store.resolve_legacy_mapping(request.legacy_id)
    if context_id is not None:
        current_version = self.store.get_item_row_version_for_update(
            context_id
        )
        patch = self._legacy_patch_request(
            request,
            context_id=context_id,
            expected_row_version=current_version,
        )
        updated = self.store.update_item_metadata_cas(patch)
        self._write_legacy_projection_from_context(updated)
~~~

The actual error message stays internal and is never serialized. Apply the identical pattern to legacy archive, restore, and soft delete. hard_delete remains the existing explicit legacy-only typed operation.

Add exact HTTP branches and no others:

~~~text
PATCH /api/v2/context/items/{id}
POST  /api/v2/context/items/{id}/archive
POST  /api/v2/context/items/{id}/restore
POST  /api/v2/context/items/{id}/delete
~~~

Reject /api/v2/context/items/{id}/hard_delete. Require a JSON object no larger than 65536 bytes. Return the updated L0 projection including its new row_version. Map stale_item to 409.

- [ ] **Run item, legacy, and boundary tests green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_models.py tests/test_context_admin.py tests/test_web_v2.py tests/test_web_server.py tests/test_legacy_compat.py tests/test_production_write_boundaries.py -k "patch or lifecycle or row_version or legacy or mutation_epoch or hard_delete or conflict"
git diff --check
~~~

Expected: both route families share CAS, successful semantic writes increment once, stale writes roll back fully, and legacy shapes remain exact.

- [ ] **Commit only Task 6 files.**

~~~bash
git status --short
git add evolvmem/context_admin_models.py evolvmem/context_models.py evolvmem/context_store.py evolvmem/context_service.py evolvmem/web_v2.py tests/test_context_models.py tests/test_context_admin.py tests/test_web_v2.py tests/test_legacy_compat.py tests/test_production_write_boundaries.py
git diff --cached --check
git commit -m "feat(web): add CAS-protected Core item mutations"
~~~

### Task 7: Expose exact project registry, binding, and resolution-review APIs

**Files:**

- Modify: evolvmem/context_service.py at the Task 3 admin methods; add typed ProjectService delegates.
- Modify: evolvmem/web_v2.py at strict project route tables and projections.
- Modify: tests/test_context_service.py at project delegate/gate tests.
- Modify: tests/test_web_v2.py at project pagination, mutation, privacy, and exact-path tests.
- Modify: tests/test_production_write_boundaries.py at typed Web project boundary.
- Read only: evolvmem/project_models.py at all prerequisite request/page/record types.
- Read only: evolvmem/project_service.py at ProjectService signatures created by prerequisite Task 3.
- Read only: evolvmem/project_store.py at double-CAS and event behavior.
- Read only: tests/test_project_service.py and tests/test_project_store.py at prerequisite CAS contracts.

**Interfaces:**

- **Consumes:** Cleanup Task 3 ProjectService.list_projects(), register_project(), archive_registry_project(), list_aliases(), register_alias(), revoke_alias(), list_workspace_bindings(), bind_workspace(), revoke_workspace_binding(), set_default_workspace_binding(), list_resolutions(), and review_resolution(); the exact Cleanup Task 3 request, page, and record DTOs frozen in the upstream ledger.
- **Produces:** ContextService.list_projects(), register_project(), archive_registry_project(), list_project_aliases(), register_project_alias(), revoke_project_alias(), list_project_workspace_bindings(), bind_project_workspace(), revoke_project_workspace_binding(), set_default_project_workspace_binding(), list_project_resolutions(), and review_project_resolution(); bounded GET/POST project, alias, binding, and resolution routes. It explicitly freezes POST /api/v2/context/project-resolutions/{id}/resolve, where {id} is the exact Context item_id primary key of context_project_resolutions.

The exact route-to-type mapping is:

| Method and path | Action | Typed request |
|---|---|---|
| GET /api/v2/context/projects | list | ProjectPageRequest |
| POST /api/v2/context/projects | register | RegisterProjectRequest |
| POST /api/v2/context/projects | archive | ArchiveProjectRequest |
| GET /api/v2/context/project-aliases | list | ProjectAliasPageRequest |
| POST /api/v2/context/project-aliases | register | RegisterProjectAliasRequest |
| POST /api/v2/context/project-aliases | revoke | RevokeProjectAliasRequest |
| GET /api/v2/context/project-workspace-bindings | list | WorkspaceBindingPageRequest |
| POST /api/v2/context/project-workspace-bindings | bind | BindProjectWorkspaceRequest |
| POST /api/v2/context/project-workspace-bindings | revoke | RevokeProjectWorkspaceRequest |
| POST /api/v2/context/project-workspace-bindings | set_default | SetDefaultProjectWorkspaceRequest |
| GET /api/v2/context/project-resolutions | list | ResolutionPageRequest |
| POST /api/v2/context/project-resolutions/{id}/resolve | accept_project, reject_project, confirm_global, ignore | ProjectResolutionReviewRequest |

Every shared POST path requires an exact lowercase `action` field; the dispatcher never infers an operation from the other fields. Client JSON keys are frozen exactly as follows (server-owned `actor` and `run_id` are never accepted from JSON):

| POST path | action | Exact client JSON keys |
|---|---|---|
| /api/v2/context/projects | register | action, project, expected_registry_revision, expected_row_revision |
| /api/v2/context/projects | archive | action, project, expected_registry_revision, expected_row_revision |
| /api/v2/context/project-aliases | register | action, alias, project, expected_registry_revision, expected_row_revision |
| /api/v2/context/project-aliases | revoke | action, alias, expected_registry_revision, expected_row_revision |
| /api/v2/context/project-workspace-bindings | bind | action, workspace_fingerprint, project, state, is_default, method, expected_registry_revision, expected_row_revision |
| /api/v2/context/project-workspace-bindings | revoke | action, workspace_fingerprint, project, expected_registry_revision, expected_row_revision |
| /api/v2/context/project-workspace-bindings | set_default | action, workspace_fingerprint, project, expected_registry_revision, expected_row_revision |
| /api/v2/context/project-resolutions/{id}/resolve | accept_project | action, project, expected_revision, expected_item_row_version |
| /api/v2/context/project-resolutions/{id}/resolve | reject_project, confirm_global, ignore | action, expected_revision |

- [ ] **Write failing exact-path, typed-delegate, double-CAS, and privacy tests.**

~~~python
import json

import pytest

from evolvmem.project_models import (
    ArchiveProjectRequest,
    BindProjectWorkspaceRequest,
    ProjectResolutionReviewAction,
    ProjectResolutionReviewRequest,
    RegisterProjectAliasRequest,
    RegisterProjectRequest,
    RevokeProjectAliasRequest,
    RevokeProjectWorkspaceRequest,
    SetDefaultProjectWorkspaceRequest,
)
from evolvmem.web_v2 import (
    WebProblem,
    WebRequest,
    _project_mutation_request,
)


@pytest.mark.parametrize(
    "path,body,expected_delegate,expected_type",
    (
        (
            "/api/v2/context/projects",
            {
                "action": "register",
                "project": "alpha",
                "expected_registry_revision": 2,
                "expected_row_revision": 0,
            },
            "register_project",
            RegisterProjectRequest,
        ),
        (
            "/api/v2/context/projects",
            {
                "action": "archive",
                "project": "alpha",
                "expected_registry_revision": 3,
                "expected_row_revision": 1,
            },
            "archive_registry_project",
            ArchiveProjectRequest,
        ),
        (
            "/api/v2/context/project-aliases",
            {
                "action": "register",
                "alias": "a",
                "project": "alpha",
                "expected_registry_revision": 3,
                "expected_row_revision": 0,
            },
            "register_project_alias",
            RegisterProjectAliasRequest,
        ),
        (
            "/api/v2/context/project-aliases",
            {
                "action": "revoke",
                "alias": "a",
                "expected_registry_revision": 4,
                "expected_row_revision": 1,
            },
            "revoke_project_alias",
            RevokeProjectAliasRequest,
        ),
        (
            "/api/v2/context/project-workspace-bindings",
            {
                "action": "bind",
                "workspace_fingerprint": "hmac-sha256:" + "a" * 64,
                "project": "alpha",
                "state": "active",
                "is_default": True,
                "method": "operator",
                "expected_registry_revision": 4,
                "expected_row_revision": 0,
            },
            "bind_project_workspace",
            BindProjectWorkspaceRequest,
        ),
        (
            "/api/v2/context/project-workspace-bindings",
            {
                "action": "revoke",
                "workspace_fingerprint": "hmac-sha256:" + "a" * 64,
                "project": "alpha",
                "expected_registry_revision": 5,
                "expected_row_revision": 1,
            },
            "revoke_project_workspace_binding",
            RevokeProjectWorkspaceRequest,
        ),
        (
            "/api/v2/context/project-workspace-bindings",
            {
                "action": "set_default",
                "workspace_fingerprint": "hmac-sha256:" + "a" * 64,
                "project": "alpha",
                "expected_registry_revision": 5,
                "expected_row_revision": 1,
            },
            "set_default_project_workspace_binding",
            SetDefaultProjectWorkspaceRequest,
        ),
    ),
)
def test_shared_post_action_selects_exact_prerequisite_dto(
    path,
    body,
    expected_delegate,
    expected_type,
):
    delegate, request = _project_mutation_request(
        path=path,
        body=body,
        actor="web:operator",
    )
    assert delegate == expected_delegate
    assert isinstance(request, expected_type)
    assert request.actor == "web:operator"
    assert request.run_id.startswith("web-v2-")


def test_shared_post_rejects_unknown_action_extra_field_and_near_miss(
    v2_dispatcher,
):
    for path in (
        "/api/v2/context/projects",
        "/api/v2/context/project-aliases",
        "/api/v2/context/project-workspace-bindings",
    ):
        with pytest.raises(WebProblem, match="unknown_action"):
            _project_mutation_request(
                path=path,
                body={"action": "remove"},
                actor="web:operator",
            )
    with pytest.raises(WebProblem, match="invalid_body_fields"):
        _project_mutation_request(
            path="/api/v2/context/projects",
            body={
                "action": "register",
                "project": "alpha",
                "expected_registry_revision": 2,
                "expected_row_revision": 0,
                "actor": "client-controlled",
            },
            actor="web:operator",
        )
    for path in (
        "/api/v2/context/projects/archive",
        "/api/v2/context/project-aliases/revoke",
        "/api/v2/context/project-workspace-bindings/set_default",
    ):
        response = v2_dispatcher.dispatch(WebRequest("POST", path, {}, b"{}"))
        assert response.status in (404, 405)


def test_exact_resolution_route_builds_prerequisite_review_request(
    v2_dispatcher,
    recording_context_service,
):
    body = {
        "action": "accept_project",
        "project": "alpha",
        "expected_revision": 4,
        "expected_item_row_version": 9,
    }
    response = v2_dispatcher.dispatch(
        WebRequest(
            method="POST",
            target="/api/v2/context/project-resolutions/41/resolve",
            headers={},
            body=json.dumps(body).encode("utf-8"),
        )
    )
    assert response.status == 200
    request = recording_context_service.review_requests[-1]
    assert isinstance(request, ProjectResolutionReviewRequest)
    assert request.item_id == 41
    assert request.action is ProjectResolutionReviewAction.ACCEPT_PROJECT
    assert request.project == "alpha"
    assert request.expected_revision == 4
    assert request.expected_item_row_version == 9


def test_non_accept_resolution_route_maps_project_to_none(
    v2_dispatcher,
    recording_context_service,
):
    response = v2_dispatcher.dispatch(
        WebRequest(
            method="POST",
            target="/api/v2/context/project-resolutions/41/resolve",
            headers={},
            body=json.dumps(
                {
                    "action": "reject_project",
                    "expected_revision": 4,
                }
            ).encode("utf-8"),
        )
    )
    assert response.status == 200
    request = recording_context_service.review_requests[-1]
    assert request.action is ProjectResolutionReviewAction.REJECT_PROJECT
    assert request.project is None
    assert request.expected_item_row_version is None


def test_resolution_near_miss_paths_are_not_mutations(v2_dispatcher):
    for path in (
        "/api/v2/context/project-resolutions/41",
        "/api/v2/context/project-resolutions/41/review",
        "/api/v2/context/project-resolution/41/resolve",
    ):
        response = v2_dispatcher.dispatch(
            WebRequest("POST", path, {}, b"{}")
        )
        assert response.status in (404, 405)
~~~

Add bounded stable pagination and cursor/filter binding for all four GET collections. Resolution filters are review_state, resolution_state, project, limit, and cursor. Assert workspace bindings expose only HMAC fingerprints and never paths/remotes. Assert resolution list/detail evidence contains only source, type, source_version, and normalized_value; omit memory text, reviewer hash, run ID, and raw workspace data.

For every mutation, test missing/stale expected_registry_revision and relevant expected_row_revision. Reject missing, case-variant, or unknown actions and every missing or extra client key before constructing a DTO. For accept_project, require a nonempty project, expected_revision, and expected_item_row_version and prove the resolution plus Context item update share one semantic transaction. For reject_project, confirm_global, and ignore, require expected_revision, normalize project to None, and reject project or item-row-version JSON fields. Prove all four actions retain distinct audit states. Prove project archive calls archive_registry_project(), never the existing ContextService.archive_project() session-archive purge method.

- [ ] **Run project Web tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_web_v2.py tests/test_context_service.py tests/test_project_service.py tests/test_project_store.py tests/test_production_write_boundaries.py -k "project or alias or binding or resolution or registry"
~~~

Expected: project routes are 404 and ContextService delegates are missing.

- [ ] **Implement thin ContextService delegates and exact HTTP mappings.**

Delegate without opening a second transaction or reproducing ProjectService CAS:

~~~python
def list_projects(self, request: ProjectPageRequest) -> ProjectPage:
    self._require_request(request, ProjectPageRequest)
    self._require_project_admin_read()
    return self._project_service().list_projects(request)


def register_project(
    self,
    request: RegisterProjectRequest,
) -> ProjectRecord:
    self._require_request(request, RegisterProjectRequest)
    self._require_project_operator_write()
    return self._project_service().register_project(request)


def archive_registry_project(
    self,
    request: ArchiveProjectRequest,
) -> ProjectRecord:
    self._require_request(request, ArchiveProjectRequest)
    self._require_project_operator_write()
    return self._project_service().archive_registry_project(request)


def review_project_resolution(
    self,
    request: ProjectResolutionReviewRequest,
) -> ProjectResolutionRecord:
    self._require_request(request, ProjectResolutionReviewRequest)
    self._require_project_operator_write()
    return self._project_service().review_resolution(request)


def list_project_aliases(
    self,
    request: ProjectAliasPageRequest,
) -> ProjectAliasPage:
    self._require_request(request, ProjectAliasPageRequest)
    self._require_project_admin_read()
    return self._project_service().list_aliases(request)


def register_project_alias(
    self,
    request: RegisterProjectAliasRequest,
) -> ProjectAliasRecord:
    self._require_request(request, RegisterProjectAliasRequest)
    self._require_project_operator_write()
    return self._project_service().register_alias(request)


def revoke_project_alias(
    self,
    request: RevokeProjectAliasRequest,
) -> ProjectAliasRecord:
    self._require_request(request, RevokeProjectAliasRequest)
    self._require_project_operator_write()
    return self._project_service().revoke_alias(request)


def list_project_workspace_bindings(
    self,
    request: WorkspaceBindingPageRequest,
) -> WorkspaceBindingPage:
    self._require_request(request, WorkspaceBindingPageRequest)
    self._require_project_admin_read()
    return self._project_service().list_workspace_bindings(request)


def bind_project_workspace(
    self,
    request: BindProjectWorkspaceRequest,
) -> ProjectWorkspaceBindingRecord:
    self._require_request(request, BindProjectWorkspaceRequest)
    self._require_project_operator_write()
    return self._project_service().bind_workspace(request)


def revoke_project_workspace_binding(
    self,
    request: RevokeProjectWorkspaceRequest,
) -> ProjectWorkspaceBindingRecord:
    self._require_request(request, RevokeProjectWorkspaceRequest)
    self._require_project_operator_write()
    return self._project_service().revoke_workspace_binding(request)


def set_default_project_workspace_binding(
    self,
    request: SetDefaultProjectWorkspaceRequest,
) -> ProjectWorkspaceBindingRecord:
    self._require_request(request, SetDefaultProjectWorkspaceRequest)
    self._require_project_operator_write()
    return self._project_service().set_default_workspace_binding(request)


def list_project_resolutions(
    self,
    request: ResolutionPageRequest,
) -> ResolutionPage:
    self._require_request(request, ResolutionPageRequest)
    self._require_project_admin_read()
    return self._project_service().list_resolutions(request)
~~~

Keep ProjectService as the semantic-transaction owner for every mutation shown above.

Parse every shared POST path through one exact discriminator before choosing a typed delegate:

~~~python
import secrets


def _require_exact_fields(
    body: dict[str, object],
    expected: frozenset[str],
) -> None:
    if frozenset(body) != expected:
        raise WebProblem(400, "invalid_body_fields")


def _required_text(body: dict[str, object], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value.strip():
        raise WebProblem(400, "invalid_" + name)
    return value


def _required_revision(
    body: dict[str, object],
    name: str,
    *,
    positive: bool = False,
) -> int:
    value = body.get(name)
    minimum = 1 if positive else 0
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise WebProblem(400, "invalid_" + name)
    return value


def _required_bool(body: dict[str, object], name: str) -> bool:
    value = body.get(name)
    if not isinstance(value, bool):
        raise WebProblem(400, "invalid_" + name)
    return value


def _project_mutation_request(
    *,
    path: str,
    body: dict[str, object],
    actor: str,
) -> tuple[str, object]:
    action = _required_text(body, "action")
    run_id = "web-v2-" + secrets.token_hex(12)

    if path == "/api/v2/context/projects":
        fields = frozenset(
            {
                "action",
                "project",
                "expected_registry_revision",
                "expected_row_revision",
            }
        )
        if action not in {"register", "archive"}:
            raise WebProblem(400, "unknown_action")
        _require_exact_fields(body, fields)
        values = {
            "project": _required_text(body, "project"),
            "expected_registry_revision": _required_revision(
                body, "expected_registry_revision"
            ),
            "expected_row_revision": _required_revision(
                body, "expected_row_revision"
            ),
            "actor": actor,
            "run_id": run_id,
        }
        if action == "register":
            return "register_project", RegisterProjectRequest(**values)
        return "archive_registry_project", ArchiveProjectRequest(**values)

    if path == "/api/v2/context/project-aliases":
        if action == "register":
            _require_exact_fields(
                body,
                frozenset(
                    {
                        "action",
                        "alias",
                        "project",
                        "expected_registry_revision",
                        "expected_row_revision",
                    }
                ),
            )
            return (
                "register_project_alias",
                RegisterProjectAliasRequest(
                    alias=_required_text(body, "alias"),
                    project=_required_text(body, "project"),
                    expected_registry_revision=_required_revision(
                        body, "expected_registry_revision"
                    ),
                    expected_row_revision=_required_revision(
                        body, "expected_row_revision"
                    ),
                    actor=actor,
                    run_id=run_id,
                ),
            )
        if action == "revoke":
            _require_exact_fields(
                body,
                frozenset(
                    {
                        "action",
                        "alias",
                        "expected_registry_revision",
                        "expected_row_revision",
                    }
                ),
            )
            return (
                "revoke_project_alias",
                RevokeProjectAliasRequest(
                    alias=_required_text(body, "alias"),
                    expected_registry_revision=_required_revision(
                        body, "expected_registry_revision"
                    ),
                    expected_row_revision=_required_revision(
                        body, "expected_row_revision"
                    ),
                    actor=actor,
                    run_id=run_id,
                ),
            )
        raise WebProblem(400, "unknown_action")

    if path == "/api/v2/context/project-workspace-bindings":
        common_fields = {
            "action",
            "workspace_fingerprint",
            "project",
            "expected_registry_revision",
            "expected_row_revision",
        }
        if action not in {"bind", "revoke", "set_default"}:
            raise WebProblem(400, "unknown_action")
        expected_fields = (
            common_fields | {"state", "is_default", "method"}
            if action == "bind"
            else common_fields
        )
        _require_exact_fields(body, frozenset(expected_fields))
        common_values = {
            "workspace_fingerprint": _required_text(
                body, "workspace_fingerprint"
            ),
            "project": _required_text(body, "project"),
            "expected_registry_revision": _required_revision(
                body, "expected_registry_revision"
            ),
            "expected_row_revision": _required_revision(
                body, "expected_row_revision"
            ),
            "actor": actor,
            "run_id": run_id,
        }
        if action == "bind":
            return (
                "bind_project_workspace",
                BindProjectWorkspaceRequest(
                    **common_values,
                    state=_required_text(body, "state"),
                    is_default=_required_bool(body, "is_default"),
                    method=_required_text(body, "method"),
                ),
            )
        if action == "revoke":
            return (
                "revoke_project_workspace_binding",
                RevokeProjectWorkspaceRequest(**common_values),
            )
        if action == "set_default":
            return (
                "set_default_project_workspace_binding",
                SetDefaultProjectWorkspaceRequest(**common_values),
            )
    raise WebProblem(404, "route_not_found")
~~~

The dispatcher invokes `_project_mutation_request()` only for the three exact shared POST paths after security authorization, then calls the returned named ContextService delegate with the returned typed request. It never dispatches from a suffix path, guessed field set, or client-provided delegate name.

Parse the frozen resolution path with an anchored expression:

~~~python
import secrets


_PROJECT_RESOLUTION_ACTION_RE = re.compile(
    r"^/api/v2/context/project-resolutions/([1-9][0-9]*)/resolve$"
)


def _resolution_review_request(
    *,
    item_id: int,
    body: dict[str, object],
    actor: str,
) -> ProjectResolutionReviewRequest:
    try:
        action = ProjectResolutionReviewAction(_required_text(body, "action"))
    except ValueError:
        raise WebProblem(400, "unknown_action") from None
    if action is ProjectResolutionReviewAction.ACCEPT_PROJECT:
        _require_exact_fields(
            body,
            frozenset(
                {
                    "action",
                    "project",
                    "expected_revision",
                    "expected_item_row_version",
                }
            ),
        )
        project = _required_text(body, "project")
        expected_item_version = _required_revision(
            body,
            "expected_item_row_version",
            positive=True,
        )
    else:
        _require_exact_fields(
            body,
            frozenset({"action", "expected_revision"}),
        )
        project = None
        expected_item_version = None
    return ProjectResolutionReviewRequest(
        item_id=item_id,
        action=action,
        project=project,
        expected_revision=_required_revision(
            body,
            "expected_revision",
            positive=True,
        ),
        expected_item_row_version=expected_item_version,
        actor=actor,
        run_id="web-v2-" + secrets.token_hex(12),
    )
~~~

The dispatcher passes self._security.operator_actor() as actor after successful authorization. ProjectService performs its established actor hashing. Never pass the Bearer token, CSRF nonce, or Origin into ProjectService.

Every list projection is an explicit allowlist. Registry rows include current registry and row revisions needed for the next mutation. Resolution rows include item row_version only where needed to accept a project. Map every stale registry, row, item, or resolution conflict to 409 without returning current private values.

- [ ] **Run project and prerequisite CAS tests green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_web_v2.py tests/test_context_service.py tests/test_project_service.py tests/test_project_store.py tests/test_production_write_boundaries.py -k "project or alias or binding or resolution or registry"
git diff --check
~~~

Expected: the exact resolve endpoint passes, all double-CAS regressions remain green, and Web contains no Store access.

- [ ] **Commit only Task 7 files.**

~~~bash
git status --short
git add evolvmem/context_service.py evolvmem/web_v2.py tests/test_context_service.py tests/test_web_v2.py tests/test_production_write_boundaries.py
git diff --cached --check
git commit -m "feat(web): expose project and resolution APIs"
~~~

### Task 8: Make the Core L0 library the packaged default browser UI

**Files:**

- Modify: evolvmem/web_static/index.html:1-end at the current monolithic page; replace with a shell.
- Create: evolvmem/web_static/styles.css.
- Create: evolvmem/web_static/api.js.
- Create: evolvmem/web_static/app.js.
- Create: evolvmem/web_static/pages/items.js.
- Create: evolvmem/web_static/pages/projects.js.
- Create: evolvmem/web_static/pages/reviews.js.
- Create: evolvmem/web_static/pages/workstreams.js.
- Create: evolvmem/web_static/pages/legacy.js.
- Create: tests/test_web_static.py.
- Modify: evolvmem/web_server.py:26 at _STATIC_INDEX and 260-350 at static GET handling.
- Modify: tests/test_web_server.py:522-532 at test_http_index_served() and adjacent HTTP tests.
- Modify: pyproject.toml:1-25 at setuptools package discovery; add explicit package-data globs.

**Interfaces:**

- **Consumes:** v2 status/items/projects/aliases/bindings/resolutions, reserved continuity readiness, frozen legacy GET /api/stats and GET /api/memories, and X-EvolvMem-CSRF.
- **Produces:** exact static allowlist; packaged modular UI; default active Core L0 list; exact L1/L2 detail controls; bounded provenance, rollup, and project-review views; read-only legacy compatibility page.

- [ ] **Create only the missing static page-module directory.**

~~~bash
mkdir -p -- evolvmem/web_static/pages
test -d evolvmem/web_static/pages
git status --short
~~~

Expected: the directory exists and status remains clean because an empty directory is not staged. Do not create another static root or copy any existing asset.

- [ ] **Write failing static allowlist, package-data, default-filter, provenance, and secret-storage tests.**

~~~python
from pathlib import Path
import tomllib


STATIC = Path("evolvmem/web_static")


def test_default_items_module_is_core_l0_and_excludes_archived_raw_summaries():
    source = (STATIC / "pages" / "items.js").read_text(encoding="utf-8")
    assert 'status: "active"' in source
    assert "/api/v2/context/items" in source
    assert "layer=l1" in source
    assert "layer=l2" in source
    assert "sources" in source
    assert "supersession" in source
    assert "project_resolution" in source
    assert "/api/memories" not in source
    assert "hard_delete" not in source


def test_static_modules_never_persist_operator_secrets():
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(STATIC.rglob("*.js"))
    )
    assert "localStorage" not in source
    assert "sessionStorage" not in source
    assert "document.cookie" not in source
    assert ".innerHTML" not in source


def test_wheel_configuration_includes_every_static_asset():
    config = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    patterns = set(config["tool"]["setuptools"]["package-data"]["evolvmem"])
    assert patterns == {
        "web_static/*.html",
        "web_static/*.css",
        "web_static/*.js",
        "web_static/pages/*.js",
    }
~~~

Add HTTP tests for /, every allowed /static path, exact MIME type, cache policy, unknown asset 404, percent-encoded traversal 404, query-on-static rejection, and path text not echoed. Assert index has navigation for Items, Projects, Reviews, Workstreams, and Legacy Projection; no inline secret; and no physical hard-delete control. Assert legacy.js issues GET only. Assert reviews.js uses the exact /api/v2/context/project-resolutions/{id}/resolve path. Assert workstreams.js renders continuity_unavailable without querying an unimplemented data route.

- [ ] **Run static tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_web_static.py tests/test_web_server.py -k "static or index or package or default_items or secret"
~~~

Expected: modules and package-data configuration are absent; only index.html can be served.

- [ ] **Implement the exact asset map and minimal modular UI.**

Serve resources from an allowlist, not a joined request path:

~~~python
from importlib import resources


_STATIC_ROOT = resources.files("evolvmem").joinpath("web_static")
_STATIC_ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/static/api.js": ("api.js", "text/javascript; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/pages/items.js": (
        "pages/items.js",
        "text/javascript; charset=utf-8",
    ),
    "/static/pages/projects.js": (
        "pages/projects.js",
        "text/javascript; charset=utf-8",
    ),
    "/static/pages/reviews.js": (
        "pages/reviews.js",
        "text/javascript; charset=utf-8",
    ),
    "/static/pages/workstreams.js": (
        "pages/workstreams.js",
        "text/javascript; charset=utf-8",
    ),
    "/static/pages/legacy.js": (
        "pages/legacy.js",
        "text/javascript; charset=utf-8",
    ),
}
~~~

Reject a query string on static paths. Read only the allowlisted Traversable and send its fixed MIME type.

api.js retains secrets only in module variables:

~~~javascript
let operatorToken = "";
let csrfNonce = "";

export function setOperatorToken(value) {
  operatorToken = String(value);
}

export async function contextFetch(path, options = {}) {
  const method = options.method || "GET";
  const headers = new Headers({"Accept": "application/json"});
  if (operatorToken !== "") {
    headers.set("Authorization", "Bearer " + operatorToken);
  }
  if (method !== "GET" && method !== "HEAD") {
    headers.set("Content-Type", "application/json");
    if (csrfNonce !== "") {
      headers.set("X-EvolvMem-CSRF", csrfNonce);
    }
  }
  const response = await fetch(path, {
    method: method,
    headers: headers,
    body: options.body === undefined
      ? undefined
      : JSON.stringify(options.body),
  });
  if (path === "/api/v2/context/status") {
    csrfNonce = response.headers.get("X-EvolvMem-CSRF") || "";
  }
  const payload = await response.json();
  if (!response.ok) {
    const code = payload.error && payload.error.code
      ? payload.error.code
      : "request_failed";
    throw new Error(code);
  }
  return payload;
}
~~~

items.js starts with the active Core query, explicitly requests L1 for selection, and binds L2 to a user action:

~~~javascript
import {contextFetch} from "../api.js";

export const DEFAULT_ITEM_FILTERS = Object.freeze({
  project: "",
  content_type: "",
  status: "active",
  tier: "",
  resolution_state: "",
});

export async function loadItems(filters = DEFAULT_ITEM_FILTERS, cursor = "") {
  const query = new URLSearchParams();
  for (const [name, value] of Object.entries(filters)) {
    if (value !== "") {
      query.set(name, value);
    }
  }
  query.set("limit", "50");
  if (cursor !== "") {
    query.set("cursor", cursor);
  }
  return contextFetch("/api/v2/context/items?" + query.toString());
}

export function loadItemDetail(itemId) {
  return contextFetch(
    "/api/v2/context/items/" + String(itemId)
      + "?layer=l1&related_limit=25"
  );
}

export function loadItemL2(itemId) {
  return contextFetch(
    "/api/v2/context/items/" + String(itemId)
      + "?layer=l2&related_limit=25"
  );
}
~~~

Render all server data through document.createTextNode() or textContent. The detail panel renders sources, direct supersession predecessor/successor links, and project_resolution structured evidence from the L1 response before showing the separate “Load L2” button. It displays rollup_status and rollup_covered_through for project_summary rows, including pending, failed, vector_dirty, and ready.

projects.js displays registry and row revisions beside mutation controls. reviews.js displays accept project, reject project, confirm global, and ignore as four separate actions and submits the required resolution/item revisions to the exact frozen resolve endpoint. legacy.js labels itself “Read-only compatibility projection,” uses only GET /api/stats and GET /api/memories, and renders legacy value as compatibility data rather than Core L0/L1/L2.

workstreams.js exposes the two exact symbols consumed by the continuity plan while rendering the bounded unavailable state now:

~~~javascript
import {contextFetch} from "../api.js";

export async function loadWorkstreams() {
  const payload = await contextFetch("/api/v2/context/status");
  return {
    ready: payload.status.continuity.ready,
    reason_codes: payload.status.continuity.reason_codes,
    items: [],
  };
}

export function renderWorkstreamDetail(container, result) {
  container.replaceChildren();
  const heading = document.createElement("h2");
  heading.textContent = "Current workstream";
  const message = document.createElement("p");
  message.textContent = result.ready
    ? "Continuity data will load from the activated service."
    : "Continuity unavailable";
  container.append(heading, message);
}
~~~

Task 8 does not call the reserved workstream list/detail routes. The continuity plan replaces loadWorkstreams() with bounded list pagination and extends renderWorkstreamDetail() with default L1 plus explicit L2.

Add explicit package data:

~~~toml
[tool.setuptools.package-data]
evolvmem = [
    "web_static/*.html",
    "web_static/*.css",
    "web_static/*.js",
    "web_static/pages/*.js",
]
~~~

- [ ] **Run static, API privacy, and legacy tests green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_web_static.py tests/test_web_server.py tests/test_web_v2.py tests/test_web_security.py
git diff --check
~~~

Expected: all allowlist/package/privacy assertions pass; the default first request is active Core L0 and archived raw session summaries are absent.

- [ ] **Commit only Task 8 files.**

~~~bash
git status --short
git add evolvmem/web_static/index.html evolvmem/web_static/styles.css evolvmem/web_static/api.js evolvmem/web_static/app.js evolvmem/web_static/pages/items.js evolvmem/web_static/pages/projects.js evolvmem/web_static/pages/reviews.js evolvmem/web_static/pages/workstreams.js evolvmem/web_static/pages/legacy.js evolvmem/web_server.py pyproject.toml tests/test_web_static.py tests/test_web_server.py
git diff --cached --check
git commit -m "feat(web): make Context Core the default library"
~~~

### Task 9: Document, review, and verify the Web v2 slice

**Files:**

- Modify: README.md:121-170 at Web Console and Context Core; 192-218 at lifecycle/privacy operations.
- Modify: tests/test_docs_context_core.py:21-73 at README helpers and Core assertions; add Web v2 documentation tests.
- Create: docs/superpowers/reports/2026-09-01-evolvmem-context-web-v2-verification.md.
- Read then remove: /home/jiangli/.local/state/evolvmem-context-web-v2/web-base.commit, reviewed-head.commit, and pytest-counts — two content-free full-hash anchors plus the two-integer suite summary created by the execution preflight and this task.
- Review-fix files: none in this task; a blocking finding stops execution and requires a revised plan with an exact owning test file, production symbol, red command, implementation, green command, and commit command.

**Interfaces:**

- **Consumes:** Web Tasks 1-8 behavior, the exact cleanup Task 11 HEAD saved as web-base.commit before Task 1, Cleanup Task 10 scripts/accept_project_continuity_migration.py temporary acceptance harness, Cleanup Task 11 verification report and its docs/superpowers/reports parent directory, and the repository operator documentation.
- **Produces:** tested operator instructions, independent review evidence, exact full-hash base-exclusive Web commit range, full-suite/temp-harness evidence, and a content-free verification handoff to the continuity plan.

- [ ] **Verify the prerequisite report directory and cleanup handoff exist.**

~~~bash
test -d docs/superpowers/reports
test -f docs/superpowers/reports/2026-09-01-evolvmem-project-memory-cleanup-verification.md
test -f /home/jiangli/.local/state/evolvmem-context-web-v2/web-base.commit
git status --short
~~~

Expected: all three tests exit 0 and the feature worktree is clean. If either repository path is absent, stop and finish Cleanup Task 11; if the base anchor is absent, restart the Web plan from its execution preflight. Do not silently create a replacement report or infer an old base from current history.

- [ ] **Write failing documentation contract tests.**

~~~python
from pathlib import Path


def test_readme_documents_context_web_v2_contract():
    readme = Path("README.md").read_text(encoding="utf-8")
    required = (
        "Context Core library",
        "L0",
        "L1",
        "L2",
        "/api/v2/context/items",
        "/api/v2/context/project-resolutions/{id}/resolve",
        "expected_row_version",
        "read-only compatibility projection",
        "operator token",
        "Origin",
        "CSRF",
        "no v2 physical hard delete",
    )
    for phrase in required:
        assert phrase in readme


def test_readme_states_default_and_legacy_compatibility():
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "archived raw session summaries are excluded by default" in readme
    assert "GET /api/memories remains a raw JSON array" in readme
    assert "server-side same-transaction row CAS" in readme
~~~

Add assertions for Core L0 mapping/source/resolution state, bounded detail provenance, explicit L2, rolling-summary coverage/pending/vector_dirty, non-loopback startup refusal, in-memory token handling, reserved workstream state, exact old endpoints, and temporary-only verification. Do not put a real token, path, nonce, or data sample in docs.

- [ ] **Run documentation tests and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_docs_context_core.py -k "web or readme"
~~~

Expected: the new Web v2 operations and security text are absent.

- [ ] **Write the minimum operator documentation.**

Insert the following contract text under README.md’s Web Console heading, then update the adjacent Context Core table so it points to the same routes:

~~~markdown
The Web Console now opens the Context Core library by default. Its first
request is an active-item L0 page; archived raw session summaries are excluded
by default. Exact detail loads L1 and bounded sources, supersession, and project
resolution evidence. L2 requires a separate exact-ID action.

Existing GET /api/stats, raw-array GET /api/memories, and every
POST /api/memory/{id}/update|archive|restore|delete|hard_delete contract remain
available. Those mutations still use the typed compatibility facade. Because
their frozen bodies have no version field, they use server-side
same-transaction row CAS after the write transaction begins.

Every v2 mutation requires its client revision. Non-loopback operation requires
an operator token and exact Origin at startup; API reads use Bearer
authentication and mutations also use the per-process CSRF header. The browser
keeps the token in memory only. There is no v2 physical hard delete.
~~~

Document the exact environment variable and --allowed-origin invocation using a symbolic TOKEN_FROM_SECRET_MANAGER value, not a usable secret. Explain that project review uses four distinct actions at POST /api/v2/context/project-resolutions/{id}/resolve. State that Workstreams remains a bounded unavailable page until the continuity plan.

- [ ] **Run documentation and all focused Web tests green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_docs_context_core.py tests/test_web_server.py tests/test_web_v2.py tests/test_web_security.py tests/test_web_static.py tests/test_context_admin.py tests/test_context_service.py tests/test_project_service.py tests/test_project_store.py tests/test_production_write_boundaries.py
git diff --check
~~~

Expected: zero failures and no secret/path fixture in public output.

- [ ] **Commit only the documentation test and README.**

~~~bash
git status --short
git add README.md tests/test_docs_context_core.py
git diff --cached --check
git commit -m "docs: document Context Web v2 operations"
~~~

- [ ] **Freeze the exact Web range and present it to the reviewer.**

~~~bash
cd /home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity
web_state_dir=/home/jiangli/.local/state/evolvmem-context-web-v2
test ! -e "$web_state_dir/reviewed-head.commit"
git log --format=%H -1 | tee "$web_state_dir/reviewed-head.commit"
web_base_commit=$(sed -n '1p' "$web_state_dir/web-base.commit")
web_head_commit=$(sed -n '1p' "$web_state_dir/reviewed-head.commit")
test "$(wc -l < "$web_state_dir/reviewed-head.commit")" -eq 1
test "${#web_base_commit}" -eq 40
test "${#web_head_commit}" -eq 40
case "$web_base_commit$web_head_commit" in *[!0-9a-f]*) exit 1 ;; esac
git cat-file -e "${web_base_commit}^{commit}"
git cat-file -e "${web_head_commit}^{commit}"
git merge-base --is-ancestor "$web_base_commit" "$web_head_commit"
test "$web_base_commit" != "$web_head_commit"
test "$web_head_commit" = "$(git log --format=%H -1)"
printf 'web_base_commit=%s\nreviewed_commit_range=%s..%s\n' "$web_base_commit" "$web_base_commit" "$web_head_commit"
git status --short
~~~

Expected: the command prints two exact full hashes as `web_base_commit` and base-exclusive, HEAD-inclusive `reviewed_commit_range`; the range starts at the cleanup handoff, ends at the Web documentation commit, covers every Web implementation commit, and status is empty. Give those exact two printed values to the reviewer; use neither an abbreviated hash nor a later report commit.

- [ ] **Invoke superpowers-requesting-code-review and stop on any blocker.**

Ask the reviewer to compare the design and prerequisite interfaces with the exact printed base-exclusive, HEAD-inclusive range, including Tasks 1-8 and the Task 9 operator documentation commit. Require explicit review of exact legacy shapes, inherited row_version ownership, legacy same-transaction CAS, semantic epoch count, ProjectService double-CAS, the exact resolution route, L0 mapping/source/resolution fields, archived-summary default filtering, layer privacy, bounded provenance, cursor authentication, error redaction, non-loopback security, static traversal, DOM injection, package data, and absence of v2 hard delete.

A blocking finding ends this execution without changing code. Revise and re-audit this plan with a concrete TDD task that names every affected file and symbol, includes the complete failing regression, separates the observed-red and observed-green commands, stages explicit paths only, and has its own commit. After that task passes, obtain renewed independent review and rerun this Task 9 from its focused verification step. Do not infer review-fix scope or make an ad hoc edit from the review prose.

- [ ] **Run fresh full and temporary acceptance evidence.**

~~~bash
set -o pipefail
umask 077
web_state_dir=/home/jiangli/.local/state/evolvmem-context-web-v2
test -f "$web_state_dir/web-base.commit"
test ! -e "$web_state_dir/pytest-counts"
full_suite_output=$(mktemp)
trap 'rm -f -- "$full_suite_output"' EXIT
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q --color=no | tee "$full_suite_output"
/home/jiangli/hermes-memory-plugin/.venv/bin/python - "$full_suite_output" <<'PY' | tee "$web_state_dir/pytest-counts"
import re
import sys
from pathlib import Path

output = Path(sys.argv[1]).read_text(encoding="utf-8")
matches = re.findall(
    r"(?m)^(?:=+ )?(\d+) passed(?:, (\d+) skipped)?(?:, [^\n]+)? in [^\n]+?(?: =+)?$",
    output,
)
if not matches:
    raise SystemExit("missing successful pytest summary")
passed, skipped = matches[-1]
print(f"pytest_passed={passed}")
print(f"pytest_skipped={skipped or '0'}")
PY
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/python scripts/accept_project_continuity_migration.py
git diff --check
git status --short
rm -f -- "$full_suite_output"
trap - EXIT
~~~

Expected: zero test/harness failures, two exact nonnegative pytest counts in the owner-only external evidence directory, a content-free PASS from the prerequisite temp-only script, and a clean feature worktree. Do not run the real migration wrapper or a non-loopback server. The temporary pytest transcript is removed in both success and failure paths.

- [ ] **Write the failing verification-report contract test.** Add this test to tests/test_docs_context_core.py.

~~~python
import re
from pathlib import Path


def test_context_web_v2_verification_report_is_commit_anchored():
    report = Path(
        "docs/superpowers/reports/"
        "2026-09-01-evolvmem-context-web-v2-verification.md"
    ).read_text(encoding="utf-8")
    fields = dict(
        line.split(": ", 1)
        for line in report.splitlines()
        if ": " in line
    )

    assert fields["scope"] == "isolated_temp_data_only"
    assert fields["real_data_mutated"] == "false"
    assert fields["nonloopback_listener_started"] == "false"
    assert fields["temp_acceptance"] == "PASS"
    assert fields["workstreams"] == "continuity_unavailable"
    assert fields["review_verdict"] == "PASS"
    assert fields["next_plan"] == (
        "docs/superpowers/plans/2026-09-01-evolvmem-continuity-protocol.md"
    )
    assert re.fullmatch(r"[0-9a-f]{40}", fields["web_base_commit"])
    assert re.fullmatch(
        r"[0-9a-f]{40}\.\.[0-9a-f]{40}",
        fields["reviewed_commit_range"],
    )
    range_base, range_head = fields["reviewed_commit_range"].split("..", 1)
    assert range_base == fields["web_base_commit"]
    assert range_head != range_base
    assert fields["pytest_passed"].isdigit()
    assert fields["pytest_skipped"].isdigit()
    for forbidden in (
        "/home/jiangli/.evolvmem",
        "Authorization:",
        "X-EvolvMem-CSRF:",
        "operator_token:",
        "nonce:",
        "run_id:",
        "reviewer_identity:",
    ):
        assert forbidden not in report
~~~

- [ ] **Run the verification-report contract and observe red.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_docs_context_core.py -k context_web_v2_verification_report
~~~

Expected failure: FileNotFoundError for docs/superpowers/reports/2026-09-01-evolvmem-context-web-v2-verification.md.

- [ ] **Create the exact content-free verification handoff.**

Run the validations below, then execute the shown apply_patch command without changing its fields. Variable expansion inserts only the already-validated full hashes and decimal counts; it never infers a range from an abbreviated log line.

~~~bash
cd /home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity
web_state_dir=/home/jiangli/.local/state/evolvmem-context-web-v2
web_base_commit=$(sed -n '1p' "$web_state_dir/web-base.commit")
web_head_commit=$(sed -n '1p' "$web_state_dir/reviewed-head.commit")
pytest_passed=$(sed -n 's/^pytest_passed=//p' "$web_state_dir/pytest-counts")
pytest_skipped=$(sed -n 's/^pytest_skipped=//p' "$web_state_dir/pytest-counts")
test "$(wc -l < "$web_state_dir/pytest-counts")" -eq 2
test "${#web_base_commit}" -eq 40
test "${#web_head_commit}" -eq 40
test -n "$pytest_passed"
test -n "$pytest_skipped"
case "$web_base_commit$web_head_commit" in *[!0-9a-f]*) exit 1 ;; esac
case "$pytest_passed$pytest_skipped" in *[!0-9]*) exit 1 ;; esac
git merge-base --is-ancestor "$web_base_commit" "$web_head_commit"
test "$web_head_commit" = "$(git log --format=%H -1)"
apply_patch <<PATCH
*** Begin Patch
*** Add File: docs/superpowers/reports/2026-09-01-evolvmem-context-web-v2-verification.md
+# Context Web v2 Verification
+
+scope: isolated_temp_data_only
+real_data_mutated: false
+nonloopback_listener_started: false
+temp_acceptance: PASS
+workstreams: continuity_unavailable
+review_verdict: PASS
+review_findings: none_blocking
+web_base_commit: ${web_base_commit}
+reviewed_commit_range: ${web_base_commit}..${web_head_commit}
+pytest_passed: ${pytest_passed}
+pytest_skipped: ${pytest_skipped}
+full_suite_command: PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q --color=no
+temporary_acceptance_command: PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/python scripts/accept_project_continuity_migration.py
+legacy_contracts: exact_unchanged_shapes
+l2_policy: explicit_exact_id_only
+next_plan: docs/superpowers/plans/2026-09-01-evolvmem-continuity-protocol.md
*** End Patch
PATCH
~~~

Expected: apply_patch creates exactly one report whose base is the pre-Task-1 cleanup handoff and whose range head is the reviewed pre-report HEAD. The report contains exact commands, exact pass/skip counts, temp-only scope, the workstream dependency, review verdict, and next plan, but no memory content, real-data path, token, nonce, run ID, or reviewer identity.

- [ ] **Run the verification-report contract and observe green.**

~~~bash
PYTHONPATH=. /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_docs_context_core.py -k context_web_v2_verification_report
git diff --check
~~~

Expected: the report contract passes and diff checking emits no diagnostics.

- [ ] **Commit only the verification handoff and its contract test.**

~~~bash
git status --short
git add docs/superpowers/reports/2026-09-01-evolvmem-context-web-v2-verification.md tests/test_docs_context_core.py
git diff --cached --check
git commit -m "docs: record Context Web v2 verification"
git status --short
~~~

Expected: the report and its contract test are the only files in the commit and final status has no output.

- [ ] **Remove only the Web plan's content-free external anchors.**

~~~bash
cd /home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-project-continuity
web_state_dir=/home/jiangli/.local/state/evolvmem-context-web-v2
web_base_commit=$(sed -n '1p' "$web_state_dir/web-base.commit")
web_head_commit=$(sed -n '1p' "$web_state_dir/reviewed-head.commit")
git cat-file -e "${web_base_commit}^{commit}"
git cat-file -e "${web_head_commit}^{commit}"
git merge-base --is-ancestor "$web_head_commit" HEAD
rm -- "$web_state_dir/web-base.commit"
rm -- "$web_state_dir/reviewed-head.commit"
rm -- "$web_state_dir/pytest-counts"
rmdir -- "$web_state_dir"
git status --short
~~~

Expected: both recorded commits still exist, the report commit descends from the reviewed head, only the two hash anchors plus the two-integer pytest count record and their exact empty directory are removed, and repository status remains empty.

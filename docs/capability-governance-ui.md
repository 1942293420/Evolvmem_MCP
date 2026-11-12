# Capability governance in the desktop workbench

The workbench keeps browsing separate from automated recall. A historical capability
with invalid sources remains visible; it is not made to look deleted merely because
recall eligibility excludes it. Existing desktop navigation, icon arrangement and palette
are retained. Styles are appended only for capability status/filter components.

## Browse API and UI

`GET /api/capabilities` accepts `status=active|pending|all`, defaulting to active. It returns
`pending_total` independently of the current search, category and page. Active means
“已整理”, not successful verification. Pending material is reachable through “待核对”; all
records includes ignored/archived history. Each lane preserves the existing query/cache
and paging behavior. Category assignment has a clear result notice and a button to view
all pending records when further value review is required.

The server fetches only the page's memory bodies, then calls `recall_facts` once for those
IDs and loads bounded latest assessment summaries. Cards show knowledge usefulness,
method result “未独立验证”, source state and current project association separately. Expanded
cards show readable reasons and the stored source pointer. They do not return encrypted
archive contents, full assessment evidence payloads, credentials or model prompts.

`item.recall_facts` retains the combined recall/source/value facts. UI
`item.current_source_projects` / `item.source_projects` use all currently valid supporting
source associations, independently of value admission or the memory's active state. Thus
a pending candidate may have a confirmed project without being approved for recall.
Current projects are not taken from the legacy `source_project` label. An explicit global
source is shown as general material; an empty project list alone means unconfirmed.

The compact `item.governance` projection contains value, verification status, source state,
ownership state, readable reasons, source reason codes and assessment count. Source state
is current, partial, invalid or untraceable. A source can be current while value needs review;
a useful historical method may have an invalid current source. These are distinct facts.

## Project filtering without a whole-library body scan

The default `source_scope=current` first obtains candidate IDs from source/unit/context/
resolution metadata and legacy labels. Only those candidates receive a batch current-source
check; the final membership uses current valid source projects. SQL then applies status,
query/category counts and ID-only paging before fetching page bodies. This covers empty
legacy labels with a valid binding and manual ownership corrections.

The project selector includes registered project names and stored label candidates; an
option is not a promise that matching current records exist. “按历史项目筛选” explicitly
selects `source_scope=historical`, preserving the old stored-label search as a historical
lookup. Old labels are not presented as current confirmation on cards.

Without a project filter, the list and count queries stay SQL-paged and source/value reads
stay bounded to page IDs. The pending count is a scalar SQL count. Source inspection uses
stored metadata/clean history; listing does not decrypt archives.

## Intake assignment no longer bypasses value assessment

Category confirmation writes an assigned intake and a visible capability, but it does not
by itself establish knowledge value or verified experience.

For an intake whose pointer exactly identifies a current unit, assignment requires the
same v3 receipt key (source revision, rule revision, unit text/range/digest and assessment
policy) used by extraction. It checks the receipt against the intake title/content/category,
validates structured fields and exact quotes, and evaluates the selected category using
stored unit text. It binds that unit source and persists an assessment in the same database
transaction. This path makes no model request and does not decrypt archives. Unavailable
structural roles remain unknown, and method results remain unverified. A paraphrase needing
an uncached independent support review stays pending rather than starting a synchronous
model request in the browser action.

Without a current matching receipt/source, a newly created capability remains pending.
Stale/changed/missing sources never turn category confirmation into automatic approval.
Independent existing active content is preserved; an existing ignored/archived record is
not reactivated. The assigned intake exits its old queue while its pending capability remains
available under “待核对”.

The POST response retains `status=assigned` for the intake and adds `memory_status`,
`assessment_value` and a readable `notice`. This avoids conflating “category saved” with
“knowledge approved” or “method verified”.

## Module component integration

Expanded cards provide a `data-memory-modules` container and call
`window.EvolvMemModules.mountMemory(container, memory_id)` only when that card is expanded.
The current DOM mounts once; collapse preserves its module component. A failed module
load shows a retryable message without hiding the capability or blocking its source view.
The project-module script/API and its authorization are supplied by the separate module
workstream. The capability list does not issue one module request per card in advance.

## Validation scope

Python tests cover legacy intake remaining pending, independent active preservation,
current v3 receipt reuse, stale-unit rejection, active/pending/all counts and paging,
visibility of invalid sources, current project corrections, empty legacy labels with
valid bindings, pending records with confirmed project associations, and page-bounded
annotation without archive decryption. Existing capability web tests retain historical
filter coverage explicitly through `source_scope=historical`.

`node --check evolvmem/web_static/capabilities.js` validates JavaScript syntax. Full desktop
browser interaction/layout checks and screenshots are performed by the root integration
workstream using isolated fixtures; this scoped change does not claim production visual QA.

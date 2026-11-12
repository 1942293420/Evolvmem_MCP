# B6 fixed project history and continuity migration candidate

This is private quarantined history import code. It does not invoke continuity_begin,
continuity_checkpoint, project event creation, date repair application or other live
business interfaces. It does not activate a route or establish business API readiness.

## Reviewed schema and parent

The eight fixed tables are continuity_events, continuity_workstreams,
continuity_focus, project_archives, project_events, project_event_sources,
project_history_date_repairs and project_memory_documents. Metadata comes from the
2026-11-01 live-v3 metadata artifact; its complete table records, DDL, columns/xinfo,
indexes and FK lists are pinned in resources/continuity_reviewed_schema.json.
Owner has all eight tables. Kane/public lack project_history_date_repairs. The
manifest records that absence explicitly and refuses a newly present table. Target
date-repair storage is still created and reconciled as empty for these namespaces.

Only continuity_workstreams.current_context_id has a cross-domain hard FK, to
core context_items.id. Therefore the required parent is the verified six-table
core plan, with the same sealed source and namespace. No source/organization or
knowledge domain is a B6 dependency. Core completion, current generation, owner
and quarantine gates and unconditional generation triggers are rechecked in every
target transaction. Parent JSON lengths are checked before loading legacy metadata.

The actual domain FKs are workstream parent_id -> workstreams.id,
focus.workstream_id -> workstreams.id, and event_sources.event_id ->
project_events.event_id. All target FKs include library_id. There is deliberately
no invented FK for continuity_events.workstream_id, event_sources.context_item_id,
task_id, source_key or source_ref. Nullable text primary keys remain nullable;
natural uniqueness is scoped by library, with source_rowid as preservation key.

## Preservation and bounded recovery

Every original column, INTEGER identity alias and rowid is retained. occurred_at
and recorded_at remain distinct uninterpreted text; manual date repairs and
plan_json remain exact. No JSON parse or merge is applied to history bodies,
source_ids, projects, plan_json or revision strings. NULL is not an empty string.
Checkpoint revision, state_version, focus revision, archived-project revision,
parent/current_context IDs and statuses are imported as stored. This preserves
CAS tokens; execution of the future native CAS business API is outside B6.
Repeated continuity audit events retain their original IDs and are not replayed.
Project-event fingerprint and event/binding-key uniqueness remain enforced.

Pages are capped at 128 rows and 4 MiB. The additive source_page_budget helper
uses a closed domain-to-reviewed-mapping allowlist. A bounded metadata SELECT
probes actual storage types and byte lengths (length after CAST AS BLOB, not
character count). An argument-free PRAGMA encoding read binds the byte estimate:
UTF-8 lengths are direct; UTF-16le/be TEXT reserves up to three UTF-8 bytes per
UTF-16 code unit, because the Python text factory receives UTF-8. Unknown encodings
are rejected explicitly. The helper then reduces the frozen reader's row LIMIT before it executes
the payload SELECT. It reserves the typed-hash envelope and checks remaining
total row/byte budgets. A later row that does not fit is read on a later page;
an oversized first row is rejected without fetching its body. No row is cut or
silently omitted. NULL remains NULL, and rowid is the unchanged seek coordinate.
Both SELECTs use the same live read-only transaction and the frozen cursor,
typed-hash, sidecar, deadline and completion rules remain in force.
Every fresh SQLite range belongs to one read-only transaction and
the sealed source's entry/exit hash proof; rowid is a seek coordinate, not a reusable
SQLite snapshot token. Non-id AUTOINCREMENT aliases event_id/source_id are handled
by a private scanner specialization without changing the frozen reader.

Workstreams can point forward, self-reference or form cycles across pages.
The importer first inserts workstreams with physical parent_id NULL while recording
the exact original parent_id (including NULL) in a private per-row escrow table.
The row, escrow, content-hash receipt and next_load frontier commit together.
Only after ALL rows have loaded does next_restore restore parent IDs in page order.
All referenced workstream IDs now exist, so no FK is removed or deferred across
transactions and no cycle is broken. Each restoration, readback and frontier is
atomic. Readback hashes reconstruct the source row from escrow during both phases.

Escrow/target text lengths are checked before either is fetched. Restored parent
equality is evaluated in SQL and the parent text is fetched once, keeping the
combined client payload within the 4 MiB bound. Escrow rows get their own ninth
count frontier; extra escrow rows cannot hide behind eight business-table totals.
Generation watches business rows, chunk receipts and escrow. Verification begins
only after restoration and requires one unchanged generation through readback,
count and finalization. Unknown commits never trigger automatic retries; an explicit
new attempt validates committed receipts/frontiers before resuming.

AUTOINCREMENT evidence distinguishes absent sqlite_sequence rows from stored zero.
Deleted high water marks are retained. The explicit high water or largest imported
ID may be INT64_MAX; the target sequence remains exhausted with NO CYCLE instead
of restarting below that value. Original evidence remains in the bound manifest.
PostgreSQL sequence transaction semantics have not been exercised by this candidate.

## Integration and operation

Copy only the additive candidate files into the existing migration package. Use
integration/operator.patch for the shared CLI. Its base hash is recorded in
integration/base.json; if another domain was integrated afterward, rebase the patch
and retain every existing domain. Do not overwrite a newer operator with this copy.
The independent operator_continuity helper reuses OperatorRuntime, its private
lease, capacity checks and supplied configuration; no DSN/configuration is added.

The CLI flow is prepare, plan, step/status, verify/finalize with --domain continuity
and the existing private --config. Core must already be verified. Every bounded
step retains the existing max_steps and <=60-second source/transaction budget,
with the operator's outer worker deadline. A locally stored completion proof binds
manifest, source seal, config and target generation. Target-only status cannot
claim completion without a successful source exit proof. Final reports always keep
history_complete=false, route_activated=false and production_migration_proven=false.

## Verification scope

Small Windows SQLite and in-memory checks cover actual reviewed DDL/namespace
absence, raw preservation, non-id identities, 128-row ranges, INT64 boundaries,
cross-page cyclic parent restoration, atomic escrow/frontiers, explicit unknown-
commit replay, text pre-read budgets, current-generation rejection and operator
completion proof. Effect tests bypass PostgreSQL catalog/security gates explicitly.
No production history body, credential, running PostgreSQL, service startup, large
backup, route activation or canonical write was used. PostgreSQL DDL, FK/RLS
catalog expression text, sequence exhaustion/rollback and live runtime CAS remain
untested and are not proven by SQLite checks.

For installed-package synthetic checks use tests/native_runner.py with absolute
--package-root and --tmpdir; optional --operator names the merged operator. Windows
scratch defaults to D:/EvolvMem-test-tmp. The Linux native runner was not executed
by the author of this candidate. See verification.json for exact local results.

The source pre-read correction adds four focused Windows synthetic checks.
Instrumentation at SQLite's text_factory confirms that a second 3 MiB body does
not materialize during the first page; oversize and wrong-storage rows are refused
before body conversion; UTF-8 byte limits, NULL keys and negative rowids retain
their original values. A fourth check exercises both UTF-16 encodings and catches
database-byte versus UTF-8-payload underestimation. It was run separately after
the encoding correction; the earlier three were not repeated. These checks
supplement the original 13 checks, which were
not repeated for this correction, and do not establish PostgreSQL behavior.

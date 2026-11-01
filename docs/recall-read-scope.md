# Capability recall read scopes

Capability recall previously repeated source-body hashes, frozen-unit slice proofs and module binding queries while checking ranked candidates. A source may back several memories and be checked again by value assessments or module validation.

The retriever now owns one query-only read snapshot for each candidate search. It checks small ranked windows and backfills when source, project or module filters reject early hits. Module bindings are fetched in batches, with each memory retaining its own precise allowed source identities. It keeps the same ranking and candidate budget; it does not accept unproved candidates to fill a page.

An explicit snapshot shares source revisions and role-free unit proofs among internal readers on the same connection. The scope checks transaction state, query-only mode, total changes, data version and schema version. All pre-existing caller transactions are rejected and left untouched; only scopes created here may nest. An owned scope rolls back and restores connection mode on success or failure. Nested role readers retain their own role and input-budget gates. Context eligibility is evaluated again on each use, since a derived summary's dependencies may cross a time boundary even in a stable SQL snapshot.

Unit proofs are memoized only in that scope, bounded to 1,024 entries and 8 MiB of serialized proof payload (plus object overhead). Callers receive independent dictionaries and lists. Explicit structured message roles bypass the role-free cache. Reaching a cache limit falls back to normal source reads; it does not weaken the proof.

Final selected-result validation opens a separate current snapshot. It rechecks content, source availability, current project/module ownership and Context structure. A candidate withdrawn or changed after search cannot rely on the earlier scope. The index audit also uses an explicit read scope; its existing database, time-window and index-file publication fences remain in force.

Hosted LAN capability and continuation reads now have independent connections, as documented in `lan-capability-reads.md` and `lan-continuity-reads.md`. Other business paths keep their existing locking. These read optimizations do not migrate databases to PostgreSQL, certify all memories as valuable, or change historical classifications. No schema or production data migration is required for this code change.

Vector lookup also proves ranked windows within its owned snapshot. It retains the same native candidate limit (four times the requested vector result count), ordering, source eligibility and vector/proof checks. Inactive rows, category mismatches and mismatching vector proofs are discarded before expensive source checks. Windows contain at most 20 native matches; source validation stops after enough eligible results survive and otherwise backfills through the same original budget. It does not share proof decisions with the subsequent retriever snapshot or final current validation. Legacy entries without vector proofs retain their pre-existing transitional behavior.

Custom row predicates retain their historical category and vector-proof checks after the callback, since callbacks can modify the supplied row. The standard production batch eligibility path can apply these inexpensive filters first. A failure in any visited source-proof window discards the whole vector contribution, including earlier accumulated hits. Tests cover the production 60-result/240-candidate/20-window path, exhausted budgets, later-window failure and mutable custom predicates with and without batch eligibility.

## Validation

The focused regression suite covers readonly ownership, rejected caller transactions including writes followed by query-only mode, connection changes, rollback, external commits, changed original bodies, withdrawn sources, project/module changes, dependency time windows, role-message isolation, output mutation, cache limits, batching and ranked backfill. All 251 targeted cases passed after release review fixes.

A separate read-only comparison used the same pre-existing frozen database, with no models or vector calls and zero database changes. The three selected results had identical content/ownership/module signatures in deployed and candidate code. Search SQL fell from 21,741 to 2,780 statements; frozen-source slice reads fell from 1,070 to 15. Observed search time was 23.28 seconds versus 6.59 seconds; final validation was 1.58 seconds versus 0.22 seconds. Cache warmth and host load were not controlled, so these timings are diagnostic samples, not production latency guarantees or a concurrency SLO.

Desktop product-diagram checks cover 1280 and 1440 widths. No mobile work is included. Deployment still requires code review, browser verification and a live post-deploy smoke/recall check. Database migration and mixed-read/write acceptance remain subsequent stages.

## Current assessment and original archive reads

Current status selects the newest assessment for each memory/source identity before deciding whether it is usable. A rejected or stale latest record cannot fall back to an older passing record. Historical rows remain available for counts, legacy-preserved source metadata, content-hash checks and malformed metadata rejection; their expensive current-source proofs are skipped only by this status path. Historical detail reads still validate every record. Preserved origins are checked against current provenance rather than accepted from old metadata.

Within one explicit, checked provenance snapshot, OriginalRead instances share an immutable set of current original archive heads and receive independent mutable copies. This set is distinct from governance visibility and project enrollment. It does not carry role, source body, value or ownership decisions. No cross-request cache is introduced, and a changed connection, transaction or read stamp still rejects the scope. Final result validation uses its own current snapshot as before.

These changes do not remove the reconciliation publication fence or reduce the final source and permission checks. Functional regressions and same-data read-only measurements must be checked separately from end-to-end production recall latency.


The incremental assessment/head change passed 181 existing native regressions, four new native regressions and desktop 1280/1440 product-diagram checks. A bounded read-only comparison used three fixed memories from an existing recovery snapshot with the same current saved rules and matching runtime dependencies. All three status samples were eligible in both versions and their status summaries matched; both original-head sets also matched. Database changes were zero, actual model/vector operations were denied, and the process group peaked at 96.2 MiB under a 192 MiB limit.

Each chosen memory had one assessment, so this data sample does not exercise old-assessment skipping; that behavior is covered by the native historical-proof and latest-rejection regressions. The two-reader original-head check performed two full scans in the baseline and one in the candidate. Observed times were 3,897.503 ms and 950.711 ms. The A-then-B run has uncontrolled OS cache/order effects and uses in-memory temporary sorting to satisfy the probe's no-file-write boundary. These are diagnostic function samples, not a full-recall equivalence test or production latency claim. Structured role-reader calls were not reached by this particular sample; the probe did not substitute role or eligibility implementations.

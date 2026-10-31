# Capability recall read scopes

Capability recall previously repeated source-body hashes, frozen-unit slice proofs and module binding queries while checking ranked candidates. A source may back several memories and be checked again by value assessments or module validation.

The retriever now owns one query-only read snapshot for each candidate search. It checks small ranked windows and backfills when source, project or module filters reject early hits. Module bindings are fetched in batches, with each memory retaining its own precise allowed source identities. It keeps the same ranking and candidate budget; it does not accept unproved candidates to fill a page.

An explicit snapshot shares source revisions and role-free unit proofs among internal readers on the same connection. The scope checks transaction state, query-only mode, total changes, data version and schema version. All pre-existing caller transactions are rejected and left untouched; only scopes created here may nest. An owned scope rolls back and restores connection mode on success or failure. Nested role readers retain their own role and input-budget gates. Context eligibility is evaluated again on each use, since a derived summary's dependencies may cross a time boundary even in a stable SQL snapshot.

Unit proofs are memoized only in that scope, bounded to 1,024 entries and 8 MiB of serialized proof payload (plus object overhead). Callers receive independent dictionaries and lists. Explicit structured message roles bypass the role-free cache. Reaching a cache limit falls back to normal source reads; it does not weaken the proof.

Final selected-result validation opens a separate current snapshot. It rechecks content, source availability, current project/module ownership and Context structure. A candidate withdrawn or changed after search cannot rely on the earlier scope. The index audit also uses an explicit read scope; its existing database, time-window and index-file publication fences remain in force.

This change does not remove the shared business dispatch lock, migrate databases to PostgreSQL, certify all memories as valuable, or change historical classifications. The UI workflow explicitly keeps those boundaries visible. No schema or production data migration is required for this code change.

## Validation

The focused regression suite covers readonly ownership, rejected caller transactions including writes followed by query-only mode, connection changes, rollback, external commits, changed original bodies, withdrawn sources, project/module changes, dependency time windows, role-message isolation, output mutation, cache limits, batching and ranked backfill. All 251 targeted cases passed after release review fixes.

A separate read-only comparison used the same pre-existing frozen database, with no models or vector calls and zero database changes. The three selected results had identical content/ownership/module signatures in deployed and candidate code. Search SQL fell from 21,741 to 2,780 statements; frozen-source slice reads fell from 1,070 to 15. Observed search time was 23.28 seconds versus 6.59 seconds; final validation was 1.58 seconds versus 0.22 seconds. Cache warmth and host load were not controlled, so these timings are diagnostic samples, not production latency guarantees or a concurrency SLO.

Desktop product-diagram checks cover 1280 and 1440 widths. No mobile work is included. Deployment still requires code review, browser verification and a live post-deploy smoke/recall check. Database migration and mixed-read/write acceptance remain subsequent stages.

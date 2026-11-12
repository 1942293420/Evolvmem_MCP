# Two-table SQLite source locator candidate

Ready for parent code review and integration; **no PostgreSQL execution or live
migration has occurred**. `PAUSED.md` records the earlier stop and is superseded
by this resumed implementation record. No original table, frozen reader, frozen
pin, canonical source or production data has been changed by this candidate.

Current baseline: 256 paths, inventory
`cadf8eed9bd426fc2c6d01c6bb12cd3b164584b45fe849c5b737d2cdc7e169fb`,
from `artifacts/pg-project-acl-integration-20261101/canonical-integration.json`.
All imported existing Python dependencies match this baseline byte for byte.

## Scope and representation

The only source tables read are:

| Original table | Original logical key |
| --- | --- |
| `context_project_registry` | `project` |
| `session_archive_holds` | `archive_id`, `source_context_id` |

The sidecar keeps the hidden SQLite rowid, canonical original logical-key JSON,
its hash and a hash of ordered original visible fields with declared type and
Python value type. Whitespace, original text, signed rowids and integer values
are preserved. Unsupported storage types, non-UTF-8 TEXT, NULL logical keys and
duplicate keys stop, following the old core/source mapper boundary. The old
logical key and mapper/content hashes are not replaced with these metadata hashes.

Three new private tables are created: `history_source_locator_progress`,
`history_source_locators`, `history_source_locator_operations`. They do not
replace or alter original tables and are not an upgrade for a previously used
sidecar schema. Prepare refuses any existing sidecar relation. The owner-only
RLS policy, FORCE RLS, table/column ACLs, column shapes, primary/unique indexes,
generation trigger and absence of extra triggers, rewrite rules or inheritance
are checked under owner table locks. Runtime has no grants or permitted RLS
policy. The new generation trigger watches locator INSERT/UPDATE/DELETE/TRUNCATE.

The trusted OperatorRuntime and existing core/source importer gates require the
same sealed source, the exact parent manifests/runs and target library, staging
quarantine, verified parents and unchanged parent generations. The new ledger
binding records these identities. No arbitrary connection, SQL, table, source
path or activation argument is accepted.

## Runner API

Import `evolvmem.history_migration.source_locator` after parent integration.
Each call receives an actual trusted `OperatorRuntime`, retained `core_plan`
and `source_plan.Plan`. Use a fresh runtime per CLI stage.

```python
prepare_locator(runtime, core_plan, sources_plan, receipt_path)
sync_locator(runtime, core_plan, sources_plan, receipt_path)
verify_locator(runtime, core_plan, sources_plan, receipt_path=None,
               expected_rows=None)
status_locator(runtime, core_plan, sources_plan)
recover_locator(runtime, core_plan, sources_plan, receipt_path)
```

- `prepare`: parents must already be verified; fresh source entry/exit hash;
  create the three new private tables and bound progress in one transaction.
- `sync`: exactly one fixed-table page, at most 128 rows, is read from a sealed
  source. Only after successful source exit is it inserted with its atomic
  operation receipt. No effect is replayed. It advances `loading` to `verifying`
  only when both tables' complete source counts match parent manifests.
- `verify` while `verifying`: independently reread one sealed source page and
  compare exact target locator rowid/key/hash values. This also detects missing,
  extra and duplicate locators, including negative or trailing rowids. Full
  counts, stable sidecar generation and current parent generations are required
  for `verified` and the bound proof.
- `verify` while already `verified`: no new effect or receipt is written. It
  obtains a fresh source entry/exit hash, then rechecks current target/parent
  generations and recomputes the stored proof binding/digest. It returns the
  proof plus `fresh_source_exit`. This is the final readback completeness call.
- `status`: reads current ledger/generation state only. Its `complete` describes
  the verified ledger; a fresh source proof requires the preceding `verify` API.
- `recover`: explicit lookup only; returns `confirmed_applied` or
  `confirmed_not_applied`, always `replayed=False`. It never retries an effect.

Progress returns `stage`, `complete`, `load_table`, `load_after`, `verify_table`,
`verify_after`, `generation`, `verify_generation`, `parent_generations`, `proof`.
Table indexes are 0..2 in the order above; rowid cursors are signed integers or
null. A verified proof includes both table counts, parent and target generations,
manifest/library binding hash, source seal/file hashes and `proof_sha256`.
`history_complete=False` and `route_activated=False` remain explicit: this
sidecar proves only the two missing metadata coordinates.

`expected_rows` is accepted for the runner interface but is not an authority
input. The combined runner's independent fixture oracle must compare the
sidecar against its sealed `expected-rows.json` separately.

## Receipts and unknown commit

Each effect requires a new absolute receipt directly under `config.work_dir`,
named `locator-[a-z0-9_.-]{1,90}.json` excluding `locator-pending.json`. The
receipt and its suffixes cannot be reused. Files are private and written with
exclusive create; source keys/content and credentials are not copied to reports.

Before effects, `.started` and fixed `locator-pending.json` bind the operation.
The database receipt is inserted atomically with its effect. A failed commit
acknowledgment leaves `.unknown` and pending; pending blocks all new effects and
final completeness. A successful commit with failed local final receipt also
leaves pending and reports `locator_commit_confirmed_receipt_incomplete`.
Only explicit `recover` reads the intent, fresh source proof, current private
ledger and affected page before recording `.recovered` and clearing pending.
Interrupted cleanup after `.recovered` is supported without replay.

The actual pinned transaction chain was inspected and checked with local stubs:
`Importer._tx` checks capacity before commit; `transaction` converts every commit
exception to `commit_outcome_unknown`; its final connection-close exceptions
are swallowed. `LeaseConnection.commit` has no operation after `raw.commit`.
Thus a post-commit cleanup exception does not incorrectly become an ordinary
pre-commit failure in this current chain. No frozen transaction code was edited.

## Budgets and practical limit

One page is at most 128 rows and 4 MiB; source storage types and lengths are
probed before value materialization, including conservative UTF-16-to-UTF-8
accounting. Target key text, progress JSON and operation receipts are also
size-probed before materialization. The whole run has at most 250,000 rows and
1 GiB of accounted input. Target leases retain the existing five-second bound;
source page reads have ten seconds inside a maximum 60-second sealed phase.

**Each sync/verify page hashes the entire sealed source at entry and exit.**
The final verified call hashes it again. These limits ensure bounded correctness,
not good production throughput: a large source with many rows in these two
tables can be expensive or time out. The combined synthetic fixtures contain
one row per table and are small. A later real-history migration should measure
this cost or separately design a reviewed multi-page sealed phase; do not remove
source checks, increase timeouts or automatically retry to hide the limitation.

## Validation performed

Only three focused local check groups were executed, no old suite:

1. Two-table original hidden rowids/keys/hashes across owner, kane and public
   synthetic fixtures; UTF-8/UTF-16LE/UTF-16BE and 128/129-row paging.
2. NULL, unsupported storage, invalid UTF-8, duplicate/missing/changed rowids,
   source/target byte budgets, stale parent generation, proof corruption and
   source-exit failure. Fresh source exit must precede final target recheck.
3. Actual pinned transaction-wrapper cleanup/unknown acknowledgment behavior;
   simulated applied/not-applied recovery, no replay, used receipt refusal,
   interrupted recovery cleanup and post-commit local receipt IO failure.

Windows-only tests substitute active-production-path discovery with an empty
set for pre-existing **synthetic** sealed files, and substitute POSIX private
receipt reads in the test harness. Production source/hash/stat checks and
private-file readers are unchanged. No real history was opened.

The initial two test harness failures were from using SQLite's auto-commit
context manager under a deliberately restrictive authorizer; the harness was
changed to explicit connection closing. Those groups and later changed groups
passed focused reruns. Actual PostgreSQL DDL/catalog/transaction behavior remains
for the one planned combined environment check after parent review.

No background helper, service, test process or unified-exec session is left
running. No PostgreSQL, SSH or remote service was started for this candidate.

# Current-source governance migration

This offline entrypoint inventories all stored archive versions, unassigned item
sources and registered conversation groups. It distinguishes current logical
sources from transport snapshots and retired tasks. It never calls a model.

## Contracts

- `build_plan(conn, rule_revision=...)` uses SELECTs only. The CLI opens an
  existing database with `mode=ro`, `query_only=ON` and one read transaction;
  it does not initialize ContextStore, ContextService, schemas or embeddings.
- `history_organization.source_refs`, the logical archive identity/head rules,
  and `ProvenanceRead.source_revision` determine eligibility. Current
  conversation manifests reserve their members, so fragments are not all
  replayed. System sources, deleted/unavailable archives, retired snapshots and
  superseded tasks are excluded.
- Sources include family, logical family, current revision, task status counts,
  current/historical/manual/unresolved unit counts, cleaning state, approximate
  characters and reasons. Only hashes and metadata leave the database; plans
  contain no original dialogue, frozen snapshot, unit text or cleaned draft.
- The plan records `policy_sha256` (the KnowledgeRules revision), a complete
  content hash, and source/task/unit/protection/registry preconditions.
  This is a content-addressed CAS artifact, not an authorization signature.
- A conversation manifest's `updated_at` is a refresh timestamp: registration
  updates it even when identity and membership are unchanged. This field alone
  is excluded from the source fingerprint. All other manifest fields, each
  member's original, cleaning decisions, task/unit state, manual protection,
  policy and registry remain bound. Reprepare plans made before this change;
  do not edit their frozen expected hashes.
- Current completed tasks with unresolved automatic units, failed tasks and
  review tasks with unresolved automatic units can be retried. Explicit manual
  assignments are preserved, including global decisions; unknown/inactive
  projects remain unresolved. Already-pending tasks are reported without reset.
- Rule changes, historical tasks needing a new source revision, invalid/missing
  bodies and ambiguous current tasks are separate blockers. They are never
  bypassed by overwriting an old task's source/rule revision. The
  `upgrade_candidates` list is for a separate reviewed revision upgrade.

## Bounded apply and recovery

`apply_plan(service, plan, limit=5)` validates the exact plan and current rule,
then rechecks all selected source/task/unit/protection fingerprints inside one
write transaction. It additionally compares deterministic current cleaning with
every existing task's frozen snapshot before retrying units. Snapshot drift
raises `migration_snapshot_conflict`; it never calls resegment.

Existing tasks use `auto_organization.retry`: the same task ID returns to
assignment when it has units. Manual ownership and source coordinates remain.
The existing retry API resets failed extraction queues to pending; its exact
queue-field changes are audited. New sources use `auto_organization.enqueue`
once. The normal worker later upgrades raw/item jobs to complete conversation
tasks using its existing protection logic.

No old task/source outputs are deleted or rewritten by migration. No model or
worker is started by apply. The additive `governance_migration_audit` table is
created only during explicit writes. Repeating a plan continues with its next
unapplied entries; one invocation selects at most five by default (maximum 100).
All selected entries pass CAS before any queue changes. A stale entry aborts
that invocation atomically; prepare again to review the new state.

On a busy shared database, use one entry per invocation and yield between
committed batches. A longer connection busy timeout waits for the writer; it
does not authorize replay after a partial effect. Inspect durable audits before
resuming any failed call, preserving completed entries and active worker leases.

`rollback_plan(service, plan)` only reverses a still-pending queue row whose
entire task/unit state, source revision, policy and scope protection still match
the state recorded after apply. Newly enqueued, unclaimed empty tasks can be
removed; retries restore only their prior queue fields and failed-extraction
queue fields. New human edits, claims, processing, outputs and changed source
versions cause retention. Processed outputs and audit records remain.
Rolled-back entries stay recorded and are not reapplied by the same plan.
A new prepare is required for a new attempt.

## Missing cleaned bodies

Raw encrypted archives are separate from `conversation_history`, manual
cleaning drafts, organization units, Context layers, capabilities and project
events. AES-authenticated payloads may use encrypted manifests pointing to
shared compressed chunks; this is lossless transport storage, not evidence
that a cleaned or derived layer exists. Metadata inventory does not prove that
all ciphertext, keys or chunks can currently be read.

`repair_cleaning(service, plan, limit=5)` is a distinct opt-in operation for
current, available archives with a missing conversation_history row, including
already-assigned current archives. It reads only the selected payloads using
the existing archiver, checks the physical manifest hash, content hash/size and current source/head
CAS, and reuses `history_memory.save_clean` (local parsing, noise removal and
credential redaction). It does not call a model, replace an existing body,
overwrite a cleaning review, assign a project or delete original files.
The existing archive key must exist and is read directly; repair never calls
key-creation code. Lossless compaction preserves the logical payload_sha256:
the physical file must instead match session_archive_storage.manifest_sha256
when that record exists. Logical evidence identity and physical ciphertext
integrity are not interchangeable.

Cleaning repair bounds logical payload size before reading or decrypting: at most
32 MiB per archive and 64 MiB per invocation, in addition to the item limit.
`max_archive_bytes` / `max_batch_bytes` (CLI `--max-archive-bytes` /
`--max-batch-bytes`) may lower these ceilings, never raise or disable them.
Known oversized payloads remain `oversized_pending`; absent logical-size
metadata remains `unknown_size_pending`. Neither is opened or marked repaired.
`budget_deferred` identifies otherwise eligible rows left for a later call.
Oversized entries do not prevent smaller later entries from making progress.
Large raw payloads require a separately bounded streaming repair path: a small
compacted manifest is not a reliable memory-size estimate.

Missing/unreadable payload is reported unavailable and creates no empty row.
A readable payload can genuinely parse to an empty technical/no-dialogue body.
Existing empty bodies are explicitly `empty_unverified`; this operation does
not mass-decrypt them or silently declare them empty sources. They require a
separate bounded raw-verification pass. Repair changes the source derivation:
prepare again before deciding what newly readable work to queue. Cleaning
repair is additive and has no delete-based rollback.

Context layer counts in the plan are coverage by type/status, not a claim that
every type requires all three layers or that existing derived content is true.

## CLI

The database and data directory are explicit. A separate read-only policy
directory is supported for inventory against a canonical database backup.
Do not use a production directory as an output path.

```sh
python -m evolvmem.governance_migration prepare \
  --db /path/backup/memory.db --data-dir /path/backup \
  --policy-data-dir /path/policy-reference --output /path/plans/governance.json

# Explicit writes, only after reviewing the exact artifact and deployed worker:
python -m evolvmem.governance_migration apply \
  --db /path/authorized/memory.db --data-dir /path/authorized \
  --plan /path/plans/governance.json --limit 5

python -m evolvmem.governance_migration rollback \
  --db /path/authorized/memory.db --data-dir /path/authorized \
  --plan /path/plans/governance.json

python -m evolvmem.governance_migration repair-cleaning \
  --db /path/authorized/memory.db --data-dir /path/authorized \
  --plan /path/plans/governance.json --limit 5
```

The write CLI attaches an explicitly opened existing SQLite connection to the
normal store without initializing its schema or a full service. It uses the
ordinary knowledge/learning read adapters required by source snapshots.
Only the migration audit table is added. The deployment's existing worker and
configured primary/dual mode determine later processing.

## Read-only baseline evidence, 2026-10-31

Database: `/home/jiangli/evolvmem-governance-backups/20261031-baseline/memory.db`.
The canonical backup was not modified. The live policy file was read and copied
with metadata to a separate `20261031-policy-reference/knowledge-management/SKILL.md`;
source/copy SHA-256 both:
`ce4b3c8bb64b07f031bf00fb87ceeb80f565207cef1e0e5ee5fd3a7526503c03`.

| Coverage | Count |
| --- | ---: |
| All raw archive rows | 12,520 |
| Available / purged raw rows | 9,446 / 3,074 |
| Current logical archive heads/batches | 2,663 |
| Current unassigned logical archives | 2,546 |
| Source-current tasks | 111 |
| Current units / manual units | 461 / 47 |
| Completed tasks with unresolved units | 29 |
| Selected work | 629 |
| Retry current conversations | 88 |
| New archive / item enqueue | 7 / 534 |
| Current tasks requiring a rule upgrade | 2 |
| Other historical-source upgrade blockers | 7 |
| Current missing body rows, selected separately | 14 |
| Unassigned current empty bodies needing verification | 26 |

The 88 retries comprise 29 completed-unassigned and 59 failed/review tasks.
Current logical body coverage is 2,617 present, 14 missing and 32 empty
(26 empty are unassigned). All snapshots together have 4,293 missing body rows;
that number is not the actionable current-source repair backlog. The fourteen
repair IDs are 1005, 1007, 1464, 160, 1651, 1736, 1743, 1745, 1747, 1749, 534,
567, 695 and 818; 1747 is already assigned.

Estimated selected work: 426 existing units plus new-source segmentation,
1,639,370 characters. These are planning estimates, not token or provider cost
guarantees. Initial prepare did not decrypt baseline ciphertext or call a model.

Final measured SELECT-only prepare: 11.66 seconds, 185,436 KB peak RSS.
Current rows are streamed and fingerprinted; full historical task snapshots
are not retained in memory. Apply inventories task/unit rows only for its
bounded selected sources and their conversation members.

Synthetic verification covers stale source/rule/manual/worker plans, logical
head changes, malformed messages, default limit/resume, exact completed-task
retry, manual preservation, no-body vs unavailable, assigned-body repair,
CLI read/write adapters, scope-withdrawal rollback and retained processed
outputs. The processed-output test uses the existing fake provider only.


### Complete archive denominator and bounded raw verification

The 2,546 current unassigned logical archives reconcile without treating every
incremental fragment as a separate model job:

| Archive coverage | Count |
| --- | ---: |
| Complete conversation failed/review retry | 1,940 |
| Complete conversation completed-unassigned retry | 295 |
| Already-organized complete conversation | 258 |
| Already-organized archive task | 5 |
| Old rule requiring upgrade | 2 |
| New independent archive enqueue | 7 |
| Missing cleaned body | 13 |
| Existing empty cleaned body | 26 |
| Total | 2,546 |

Ninety-four current complete conversation groups cover 2,493 archives.
The 88 selected conversation retries cover 2,235 of these archives; the
remaining 258 archives are already organized. Thus the seven new archive jobs
are additional work, not the full conversation coverage.

After prepare, a separate authorized read-only script verified just the 26
current unassigned empty bodies against the canonical backup. All 26 had
readable authenticated encrypted payloads and produced no dialogue under the
recorded policy. No ciphertext, chunk, key, database row or original content
was written. Four compacted archives initially failed an overstrict comparison
to the old logical payload digest; checking the physical manifest digest,
content hash/size and authenticated chunks resolved all four. This distinction
has a regression test in repair_cleaning.

Evidence artifacts are stored outside the canonical backup, under
`/home/jiangli/evolvmem-governance-backups/20261031-governance-migration-plan/`:
`archive-coverage.json` maps every archive to its current conversation/status;
`empty-body-verification.json` contains only IDs, counts, state and hashes;
`verify-coverage.py` reproduces the SELECT-only and bounded decrypt checks.
The initial prepare artifact deliberately retains its original
empty_unverified label; the separate verification receipt proves the later
check. Fourteen missing body rows remain separately planned for explicit repair.


### Completed automatic ownership decisions

`prepare` also schedules a current completed/review task when any automatic kept
unit has an ownership analysis that fails the current `ownership_reasoning.validated`
check. This includes an old reasoning version, a missing analysis, a changed text
hash or a stale rule revision. The entry reports `ownership_policy_changed` and
`outdated_ownership_units`; it reuses ordinary bounded retry at the assignment
stage. No source is resegmented and no project is assigned by this migration.
Manual decisions are excluded and remain protected by the worker.

Plans now bind `ownership_policy_version` as well as the source, rule and complete
unit state. Regenerate previously prepared plans before applying this release.
Already-current automatic analyses and completed manual-only tasks remain settled.

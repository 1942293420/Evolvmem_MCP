# Assigned sources in owner governance

An archive's client-supplied `project` is attribution metadata. It does not
confirm the business projects of its content. Owner governance may explicitly
enroll a current assigned source for content processing without clearing that
field, changing the encrypted payload, or moving existing knowledge.

`governance_sources` separates this read grant from ownership. The additive
enrollment table binds archive ID, project, adapter, native identity, payload
digest/path and creation time. The grant is valid only while that identity is
unchanged, the source remains the current logical head, the archive is available,
and neither a verified system exclusion nor a deleted cleaning review applies.
Clearing the project field cannot revive an invalid grant through the ordinary
unassigned path. A new explicit plan is required to enroll the changed identity.

Content revisions remain independent of grants. Restoring a missing derived
clean body preserves the grant; task and provenance readers immediately compute
the new content revision. Source changes invalidate existing tasks and recall
support. An enrollment never writes a unit project, confirms a capability's
ownership, or grants an archived project validity.

## Admission and processing

The public history/cleaning source reader retains its unassigned guard. Internal
conversation registration, snapshots, task CAS, review groups and provenance use
the separate owner-governance visibility oracle. Every assigned archive must
already have a valid enrollment. Native sibling expansion only includes eligible
members and does not borrow an unenrolled assigned fragment.

Existing complete-conversation grouping, structured roles, quote checks,
ownership decisions and primary extraction are unchanged. In particular, an
archive attributed to project A may yield a unit supported by original text for
project B. Existing linked human Context records, bodies, source links and
resolutions remain unchanged. Manual unit decisions keep the existing protection.
Readable sources with no dialogue receive the existing terminal no-dialogue
record without provider attempts.

Automatic discovery enrolls only its bounded selection after the existing
`auto_new` activation baseline. It rechecks the exact persisted settings inside
the write transaction; pausing or changing the baseline while selecting sources
rejects the operation. Enrollment and enqueue share one transaction. Historical
assigned sources are not silently added by enabling the new implementation.

## Explicit historical migration

Prepare reads a single SQLite snapshot in `mode=ro`; it does not initialize a
service/schema, decrypt an archive, start a worker, or call a provider. It exports
hashes rather than source text. Derived body, message and layer fingerprints are
computed using 65,536-character SQLite slices.

```sh
python -m evolvmem.governance_sources prepare --db /absolute/memory.db \
  --output /private/enrollment-plan.json
python -m evolvmem.governance_sources apply --db /absolute/memory.db \
  --plan /private/enrollment-plan.json --limit 20 \
  --output /private/enrollment-receipt-1.json
```

Apply binds the exact database path/device/inode, verifies the plan hash and
current source/head/body/manual-context fingerprints, and writes at most the
explicit batch size inside one transaction. All entries in a batch are checked
before any grant is written. Receipts and before/after audit rows support bounded
continuation. Replay verifies that previously applied grants are still current;
it refuses a changed origin or altered grant instead of reporting completion.

After enrollment, prepare a fresh `governance_migration` plan. Its inventory
includes enrolled assigned sources while retaining a separate unassigned count.
Missing clean bodies use the existing bounded `repair-cleaning` or streaming
repair path. Never reuse a queue plan frozen before enrollment or cleaning.
Finally prepare and apply the queue plan, then monitor actual tasks to terminal
outcomes. Successful enrollment is a prerequisite, not completed extraction.

```sh
python -m evolvmem.governance_sources rollback --db /absolute/memory.db \
  --plan /private/enrollment-plan.json --output /private/revocation.json
```

Rollback compares the exact audited grant before restoring its predecessor or
deleting only that grant. It retains archives, tasks and all knowledge; dependent
sources cease to be current. Later grant edits reject the entire rollback batch.
Repeated rollback is idempotent, and a rolled-back plan cannot be replayed to
reactivate access.

## Verification boundary

Real disposable SQLite tests cover public guards, original archive preservation,
identity/head/body/manual CAS, read-only preparation, long-body hashing, linked
human Context protection through primary processing, native grouping, review UI
visibility, automatic activation/pause bounds, no-dialogue handling and revocation.
Tests replace external providers with explicit synthetic responses. Production
plan generation is read-only; production application and worker completion must
be supported by separate receipts.

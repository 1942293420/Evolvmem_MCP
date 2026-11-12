# Capability vector recovery

`CapabilityIndex.reconcile(*, limit=64)` is the capability-only maintenance entry point.
It uses the configured model and dimension; it does not instantiate an encoder, call a
new provider, mutate capability database rows, or touch the Context/legacy vector files.
The caller supplies the same loaded embedding engine used for capability retrieval.

## Truth and proofs

The default eligible set is every `capability_memories.status = active` row. A caller
may supply `CapabilityIndex(store, config, engine, eligible=predicate)` to further
restrict the set; the predicate receives a memory dictionary and must make a current,
read-only eligibility decision. For provenance checks, prefer the batch constructor
argument `eligible_ids=callable(ids) -> set[int]`. Each initial/final audit checks all
active IDs in one batch, search checks its candidates in one batch, and each write
checks only its own ID before encoding and again before commit. Both callbacks may
be supplied, in which case both must allow the record. For example:

```python
CapabilityIndex(store, config, engine,
                eligible_ids=lambda ids: eligible_memory_ids(store, ids))
```

This avoids an accidental full provenance scan for every index item. The same predicate is checked for search, planning,
and immediately before saving an encoded result. Source withdrawal logic belongs in
that predicate, not in the vector cache. Excluded vectors and their proofs are removed;
their database records remain untouched.

The canonical embedding input is `title + "\n" + content` read from SQLite. The existing
`sync(memory_id, text)` signature is retained, but its supplied text is not authoritative.
Each proof binds the ID, canonical text SHA-256, and actual float32 vector digest to
an embedding contract (model filename, dimension, document prefix, and input-token
contract). It never stores memory text or credentials. The manifest is
`capability_vectors.provenance.json`, alongside the capability USearch file. It has a
monotonic generation, a round-robin resume cursor and per-ID proofs.

A recovery pass inspects the complete eligible ID/content set, but attempts at most
`limit` changed IDs (valid range 1–4096). Missing vectors, missing/stale proofs, modified
vector bytes, and obsolete IDs form the pending set. Already matching proofs require
no encoding. The cursor advances over failed IDs as well, so a persistent failure
cannot starve later work. Restarting a process resumes from the durable index/proofs;
no in-memory queue is required. Full-set inspection is intentional: bounded model work
alone cannot establish that an entire cache is complete.

## Atomicity and concurrent writers

On Linux, capability writers always use the existing `SharedVectorCache` reload/pending-merge/
atomic-replace implementation, including when its global opt-in flag is absent. The
capability-only `.recovery.lock` additionally serializes proof/index commits. Model
encoding happens outside both locks and outside SQLite write transactions.

After encoding, the writer acquires the capability gate, begins a short SQLite
transaction, reloads the current disk index and proofs, and re-reads the memory. A
changed row revision, text, eligibility or embedding contract rejects the old result.
Vector save uses the existing shared file lock; proof writes use a temporary file,
flush/fsync and atomic replacement. A crash after vector save but before proof save
leaves an unproven vector that is encoded again, rather than falsely certifying it.

Both whole-library audits run in explicit `query_only` SQLite read transactions,
so each sees one consistent WAL snapshot without reserving the database writer.
The initial snapshot selects work only: each selected ID still passes the existing
current-source check before encoding and before its individual write. A concurrent
upload therefore cannot starve all bounded recovery work merely by changing this
advisory snapshot.

Final completion is checked after save has merged concurrent pending vectors. The
capability gate and shared-cache file lock freeze the final durable image while the
full source/value audit runs outside a write transaction. Its certificate binds
`PRAGMA data_version` (other connections), `total_changes` (this connection), and the
embedding contract. It also records the separate expiration, effective-from and
effective-until boundary states of active Context sources: time can change eligibility
without a database write, and crossing both edges of a short window must not look
unchanged. These checks use only temporal metadata, not another full provenance scan.

Only then does a short `BEGIN IMMEDIATE` recheck that certificate and publish the
proof/dirty-marker result. Any intervening commit, including an unrelated write,
or crossed time boundary retains dirty and returns `partial`, `stale + 1`,
`remaining = None`, with `eligibility_snapshot_changed`. This conservative result
never certifies old eligibility. A later bounded pass can certify a stable snapshot.
A single `sync` never clears global dirty debt. Source rows and qualification rules
are unchanged. Production eligibility reads the same SQLite store; predicates based
on unversioned external mutable state do not gain this database snapshot guarantee.

Call maintenance outside an already-open business transaction; `reconcile` refuses
to certify an existing caller transaction and never commits it. Individual writes
use capability gate → SQLite writer → shared vector file lock. Final certification
uses capability gate → shared vector file lock → read snapshot → short SQLite writer.
The common capability gate serializes these participating writers. Integration code
must synchronize after its own database commit (for example via deferred-after-commit),
not enter while holding an unrelated writer transaction. All capability writers must
use this implementation; old, nonparticipating processes must exit before deployment.

On Windows/non-POSIX local installations, imports and the existing single-writer
`sync`/vector `search`/lexical paths remain available. The POSIX lock and shared-cache
imports are lazy. This local fallback does **not** claim cross-process safety;
`reconcile` returns `unsupported_platform` without attempting recovery or clearing
dirty debt. LAN clients continue to use the Linux host's supported recovery path.
This change does not add a new Windows shared-cache lock implementation.

## Existing caches and model changes

A legacy index with no manifest remains searchable while proofs are backfilled in
batches; existing IDs are not removed just because their proof is absent. Its dimension
must match the configured engine. This compatibility path depends on retaining the
already-audited deployed model contract. **The historical model identity cannot be
proven from a bare USearch file.** Do not change the configured model while bootstrapping
these legacy proofs. The 2026-10-31 production audit identified
`bge-base-zh-v1.5.f16.gguf`, 768 dimensions; this implementation does not hard-code it.

Once a manifest exists, a same-dimension model or document-prefix mismatch is rejected
as `incompatible`; the cache is not silently re-labelled or cleared. A differently sized
index or mismatched configured engine is also rejected before encoding. An invalid
manifest is reported as incompatible. A deliberate model migration or corrupt-manifest
repair requires a separately reviewed cache rebuild; this bounded entry point does
not automatically erase prior cache evidence.

Known proofs whose text or vector bytes changed are excluded from vector search until
repaired. Other proven vectors and the database-backed lexical path remain usable.

## Return contract and integration

The result includes `status`, `processed`, `updated`, `removed`, `failed`, `stale`,
`unavailable`, `remaining`, `dirty`, and error summaries without source text. Successful
final audits also report `eligible`, `indexed`, `missing`, `outdated`, and `obsolete`.
`remaining` is the number of IDs needing work after the pass; it is `None` if a failure
or a changed snapshot prevented a reliable final certification.

| Status | Meaning / next action |
|---|---|
| `ready` | Current full-set audit passed; dirty cleared. |
| `partial` | More work remains, or a source changed during encoding; run another bounded pass. |
| `unavailable` | Needed encoding has no loaded engine; retain dirty and retry after engine recovery. |
| `failed` | At least one operation failed; completed proofs remain reusable, retry bounded work. |
| `unsupported_platform` | Shared recovery requires POSIX locks; local single-writer sync/search remain available. |
| `incompatible` | Index/model/manifest contract is incompatible; inspect before retry or explicit rebuild. |

The legacy `sync` returns `synchronized`, `unavailable`, `failed`, or `stale`. A stale
result must not be retried with the old text; a later pass reads current SQLite truth.
This change provides the maintenance entry point only. Scheduling it from ContextService
and deploying or running production recovery are separate integration steps.

## Validation

Tests use temporary SQLite databases, real USearch files and fake local encoders. They
cover dirty-marker preservation, bounded restart, changed/deleted/filtered records,
per-item failure and retry fairness, missing legacy proofs without index deletion,
model/dimension mismatch, stored-vector corruption, unavailable encoding, partial proof
write failure, and preserving prior valid vectors on replacement failure. Threaded
checks exercise two resident writers, concurrent additions/deletion, and content edits
or withdrawal during encoding. Another check injects a competing save at the final
merge boundary and verifies that incomplete coverage cannot be reported ready.

Run:

```sh
python -m pytest tests/test_capability_index_recovery.py tests/test_capability_index.py \
  tests/test_capability_audit_lock.py tests/test_lan_vector_cache.py tests/test_vector_index.py -q
```

Additional real SQLite tests prove that another writer can commit during the full
audit, source/manual changes invalidate its certificate, the snapshot stays internally
consistent, source time boundaries (including both window edges) cannot falsely clear
dirty, and initial concurrent writes do not prevent individually checked progress.

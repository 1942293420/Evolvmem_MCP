# Bounded recovery of large native transcript archives

Missing cleaned dialogue is a derived-data gap, not evidence that the encrypted
original is lost. Small-payload repair is limited to 32 MiB per archive and
64 MiB per invocation. Larger or unknown-sized records are reported separately.

`evolvmem.archive_streaming_clean` recovers a single explicitly planned native
Codex transcript without loading its entire transport JSON into memory:

1. Verify the compact manifest, AES-GCM chunks, keyed chunk identities, complete
   payload hash and logical byte count. Decompression has output bounds.
2. Spool authenticated plaintext into a private disk-backed cache. Temporary
   directories are 0700 and files 0600. `/tmp` may be a RAM filesystem and is
   intentionally not the default spool destination.
3. Decode the outer transcript string incrementally. Each native JSONL event
   is syntax-checked with bounded retained strings; unused tool output is
   checked without constructing the entire value. Preserve message roles,
   native mirror collapse, cleaning-before-redaction and duplicate semantics.
4. Keep at most 16 MiB of cleaned dialogue. Unsupported outer conversation
   formats, invalid source metadata, malformed JSONL, or budget excess are
   explicit failures; never save truncated content as a successful recovery.
5. Recheck the exact source, manual state, policy and manifest before an
   additive missing-row insert. No replacement of an existing clean body,
   original archive, cleaning review or task. Record a content-free receipt.

The current large path accepts compact native transcript archives up to 2 GiB.
Preview is the default; `--apply` is required to write the missing derived row.
Use the current read-only governance plan, and prepare a new plan after repair
before deciding what to queue.

```sh
python -m evolvmem.archive_streaming_clean \
  --db /private/rehearsal/memory.db --data-dir /private/rehearsal \
  --plan /private/plans/current-governance.json --source-key archive:123

# After validating the preview, the same source and plan can be applied.
python -m evolvmem.archive_streaming_clean \
  --db /private/rehearsal/memory.db --data-dir /private/rehearsal \
  --plan /private/plans/current-governance.json --source-key archive:123 --apply
```

On Linux run recovery in a memory-limited service/cgroup, with no swap allowance
and a runtime deadline, before using it against live data. File-cache memory
counts toward a cgroup limit; reported process RSS is a different measurement.
Normal completion and Python exceptions remove plaintext spools. After an
external SIGKILL or host crash, inspect and remove only that invocation's
private recovery spool, retaining all original encrypted files.

Verification includes parity with existing dialogue cleaning, Unicode and JSON
escapes across small buffers, invalid skipped values, metadata and JSONL line
boundaries, duplicate stream keys, whitespace-only tool output, secret
redaction ordering, bounded root-key metadata, file permissions and cleanup,
source/policy races and no overwrite of a concurrently created clean body.
Public fixtures are synthetic; private rehearsal receipts carry actual archive
counts, hashes, runtime and peak process RSS. Rehearsal is not deployment.

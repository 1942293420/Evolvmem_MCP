# Recall, capture and historical-date operations

## Verify the actual client boundary

An installed CLI, configured MCP entry and successful tool response are separate checks. Inspect Desktop runtime paths independently from PATH CLI shims. Use a known registered project and verify the returned source references. Keep authentication values in private configuration or inherited environment variables, never public source or diagnostics.

New knowledge structure uses `project_archive_get` for an exact project/alias, `context_project_recall` when a question names a registered project, and `capability_search` for a development question. `experience_recall` retains its verified-evidence rules. A missing/ambiguous project is not the same as an empty knowledge base. The legacy `knowledge_recall` response names replacement tools in primary mode.

## Windows capture and recovery

The Windows bridge discovers `.jsonl`, `.jsonl.zst` and `.jsonl.zstd`. Compressed sources use the installed `decode-transcript.py` helper through the explicit `decoder_python` executable, with streaming UTF-8 decoding and a 128 MiB maximum zstd window. Native session identity is checked, incomplete records remain excluded, and captured cache stays protected by DPAPI. A decoder failure does not create a successful receipt.

Install with `-DecoderPython <absolute-python-path>`. The Python runtime must have `zstandard` available; keep it in a dedicated runtime rather than depending on whichever Python happens to be on PATH. Use `-DeferWorkerStart` when server rollout is pending: the installer keeps the sync task disabled until it is explicitly enabled after verification.

Normal discovery retains the original capture cutoff. A newly discovered compressed file whose native session time is older than the cutoff, or lacks a trusted source timestamp, is not automatically replayed just because recompression changed its filesystem mtime. Registered sources can resume normally.

`evolvmem-codex.ps1 -Action discover-history -HistoricalLimit 50` only lists historical candidates; it does not enqueue, upload or extract them. Review native session IDs, source hashes and mixed-project content before a bounded historical recovery. Do not mark an entire multi-project session as one project because it contains a matching excerpt.

The sync scheduled task runs the installed launcher, which starts the worker; normal transcript hooks only enqueue snapshots. After restoring a task, verify its executable, hidden execution, single-instance policy, latest run result, queue progress, durable local receipt and server archive/extraction status. A disabled task or stale worker-status must not be reported as synchronized.

## Source dates, ingestion dates and reviewed repairs

Project history keeps `recorded_at` as ingestion time. `occurred_at` is supplied only from explicit event evidence or a strict legacy `progress:log:YYYY-MM-DD-HHMM` source key. A source key without timezone is represented only as `YYYY-MM-DD`; no UTC timestamp is invented. Unknown occurrence stays unknown. The displayed earliest dated record does not claim the project's actual beginning or complete development duration.

Prepare a single-project repair plan against a private database path:

```sh
python -m evolvmem.project_history_repair --database /private/memory.db --project registered_project --output /private/date-plan.json
```

Review the exact changes and skipped reasons. After a verified backup, apply the unchanged plan:

```sh
python -m evolvmem.project_history_repair --database /private/memory.db --project registered_project --apply-plan /private/date-plan.json
```

The plan includes original values, source mapping and a SHA-256 digest. Apply rechecks the current snapshot inside its own transaction; stale plans fail without partial updates. The audit table retains the original plan. Already known dates, manual classification and original recorded timestamps remain intact. No model invocation or source re-extraction occurs.

## Private backup snapshot

```sh
python -m scripts.backup_snapshot --data-dir /private/evolvmem --output /private/backups/new-snapshot --samples 3
```

The output directory must be new and outside the source data directory. The script pins a read-only SQLite WAL snapshot and uses the online backup API. It copies snapshot-referenced encrypted archives, all referenced chunks, local archive/workspace keys and selected configuration; validates each unique chunk's authenticated content, checks the copied database and restores a bounded payload sample. Files are private and synchronized before the complete manifest is written. Missing or changed source files leave an incomplete artifact without a success manifest; the source is never repaired or deleted by backup.

This is a same-host rollback point. It does not include purged payloads, unrelated user namespaces, unreferenced files or rebuildable vector caches, and it is not an off-host disaster-recovery guarantee. Protect matching keys and artifacts independently, use consistent remote copies and perform isolated restore exercises for full disaster recovery. Never restore a whole old production database merely to undo a small date correction: use the audit's original field values and current-version checks so later knowledge is preserved.

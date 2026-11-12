# EvolvMem health repair implementation plan

> Execution: subagent-driven implementation with isolated tests and root integration review. User explicitly authorized repair and deployment after the read-only report.

Goal: restore responsive, correct recall and durable Windows capture, retain historical evidence, and deploy verified fixes with rollback.

Architecture: retain namespace/transaction protection; cache complete health snapshots with explicit bounded freshness and invalidation instead of repeatedly scanning the database. Separate protocol liveness from business dispatch. Keep history dates and import provenance explicit. Do not rerun all historical extraction or approve unreviewed knowledge to clear counts.

Tech stack: Python/SQLite/MCP, PowerShell Windows bridge, plain JavaScript web UI.

Spec: EvolvMem-体检报告-2026-10-30.md; server-remediation-plan.md; windows-remediation-plan.md; procurement-recovery-preflight.md.

Workspace: /home/jiangli/hermes-memory-plugin/.worktrees/health-repair-20261030
Branch: codex/health-repair-20261030
Baseline: 4f7a52dded007e33434792ce3412e89f6c853911 (local snapshot of deployed changes, not a public release).
Production baseline: 44368c153dde07d9d52c82596e05df912809a0c7 plus preserved dirty files.
Backup: /home/jiangli/evolvmem-deploy-backups/health-repair-20261030

## Constraints and ownership

- No source credentials or production data in commits/public artifacts.
- Preserve existing production changes. Deploy only files whose baseline hashes still match.
- Preserve source discovery and provenance. Historical recovery stays bounded to reviewed native sessions; mixed-project material is archived without automatic extraction or forced project assignment.
- Retain all original data and snapshots. No queue deletion to hide failed uploads; no bulk candidate approval.
- Root coordinates deployment/restart and any production data repair. Agents modify isolated source or prepare/reversibly configure Windows only within assigned scope.
- Server agent owns mcp_server.py, lan_tools.py, context_service.py, request_health.py and corresponding tests. Coordinate contract documentation changes with root.
- History agent owns project_archive_store.py, project_archive_pipeline.py, knowledge_restructure.py, capability_web.py, project-archives.js and corresponding tests. No production DB mutation.
- Windows agent owns windows/ bridge code and tests, actual local DSH/Kimi config with backups; coordinate enabling uploader with root after server stabilization.
- Root owns product principles/docs, backup/deploy orchestration, integration test/review, release export.

## Task 1: server recall and response performance

- [ ] Add handler-level regression for primary context_project_recall returning valid archive results; observe current failure.
- [ ] Fix dict/object result contract and preserve explicit unknown/ambiguous project errors.
- [ ] Add blocking-dispatch protocol tests proving ping/initialize complete independently; add health snapshot freshness, invalidation, single-flight, and failure-gate tests.
- [ ] Implement minimum safe cache/fast-path changes; preserve business serialization and per-request identity protection. No unsafe removal of locks.
- [ ] Run affected health/LAN/primary/capability suites using production venv but isolated tmp data. Record output and changed paths; do not restart production.

## Task 2: Windows capture and actual client integration

- [ ] Reproduce collector compressed-source omission using controlled fixtures; add .jsonl.zst/.jsonl.zstd support or a bounded streaming decoder with installed runtime dependencies and clear error states.
- [ ] Preserve source session identity and capture cutoff; add explicit historical-discovery mode rather than silently replaying all history.
- [ ] Restore missing uploader task from supported installer logic; preserve existing queue and receipt files. Validate task command and hidden execution; enable after server-ready signal.
- [ ] Back up and fix DSH PATH shim to installed desktop CLI; configure Kimi's actual runtime MCP using existing authorized EvolvMem credentials without printing them.
- [ ] Verify executable versions, configs, collector fixture behavior, then actual authenticated read-only client requests and durable server receipts after deployment.

## Task 3: historical visibility and dates

- [ ] Add regression: migrated records retain reliable explicit source dates and date precision; no invented timezone or timestamp from ingestion date.
- [ ] Fix migration/display semantics and canonical Chinese display name while preserving event recorded_at.
- [ ] Prepare idempotent, dry-run-first repair for existing mapped events, restricted to verifiable source-date patterns; list all changes and preserve originals. Root applies only after backup and review.
- [ ] Do not overwrite known occurred_at, manual classifications, archived versions, or unrelated multi-project source bindings.
- [ ] Test representative July 20 / August 5 / August 31 entries with hand-derived expected dates and missing-date display.

## Task 4: integration, rollback and deployment

- [ ] Save consistent SQLite backup using sqlite backup API plus protected keys/config/archive manifests; record coverage and hashes. Do not call file copies alone a verified complete backup.
- [ ] Review each agent diff and test evidence; independently run selected integrated suites and product-modules UI checks.
- [ ] Update principles.js product explanation for recall/health/history/capture changes; update docs and committed release notes without private data.
- [ ] Deploy a hash-checked file manifest, restart only affected services, verify protocol/known project recall and three client boundaries.
- [ ] Apply reviewed bounded history-date repair; verify archive date/summary before and after. Enable uploader and verify queue progress plus server receipt; do not claim every historical version is extracted.
- [ ] Retain reviewed source commits and the exact deployed manifest locally. The replacement AGENTS instructions do not require GitHub publication; do not automatically push or publish this repair.
- [ ] Deliver exact successes, residual constraints, timings, backup paths and rollback instructions. Use fresh live evidence, not historical assertions.

## Review matrix

| Tasks | Shared boundary | Ruling |
|---|---|---|
| 1 / 3 | recall_project_archive return | History keeps dictionary contract; server normalizes handler response explicitly |
| 1 / 2 | MCP authentication/timeouts | Enable uploader only after root confirms server readiness; no parallel probe storm |
| 2 / 3 | compressed multi-project history | Archive native session unchanged; never label entire mixed session ai_purchase just from one excerpt |
| 1 / 4 | production dirty lan_tools | Snapshot existing file and compare baseline hash before deploying tested merged content |
| 3 / 4 | historical SQL writes | Dry-run, consistent backup, idempotent mapping and exact old-value capture; no broad re-extraction |

No further permission gate is needed for the authorized repair/deployment. Unexpected destructive operations or a new data-sharing scope remain outside this plan.

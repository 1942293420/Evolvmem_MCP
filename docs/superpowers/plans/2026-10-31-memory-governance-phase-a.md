# Memory Governance Phase A Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement task-by-task. Steps use checkbox syntax.

**Goal:** Restore source-bound unit ownership before primary extraction, preserve historical evidence and produce a bounded repair plan for old records.

**Architecture:** Reuse the existing ownership analysis and CAS-protected manual decisions. Separate assignment from legacy materialization; project history and capability memory consume the same confirmed scope. A private snapshot supplies migration evidence; production is changed only after isolated tests and a guarded release.

**Tech Stack:** Python, SQLite, existing ContextStore, USearch, bge-base-zh-v1.5 768 dimensions. Existing configured model only; no new provider.

**Spec:** 目标与验收.md

## Global Constraints

- Preserve originals, manual ownership, current source revisions and independently supported history.
- Do not interpret empty project as global scope; do not classify a mixed entire archive as one project.
- Keep legacy behavior compatible; primary must not accidentally write legacy derived records merely to reuse classification.
- Only owner jiangli namespace is in scope; public summaries/other user's private DB are not a migration target.
- Working branch: codex/memory-governance-20261031, deployed-source snapshot 8dc7e1d. Production is read-only during implementation.

## Task 1: Restore assignment in the primary path

**Files:** evolvmem/auto_organization.py; tests/test_primary_ownership.py; existing ownership/organization tests.

**Interfaces:** `_assign(service, task, *, materialize=True)` retains its legacy default. `materialize=False` performs existing evidence checking and CAS updates but skips `_ingest` and legacy `_refresh_task_state`. It returns current unit decisions so the primary caller can truthfully choose review/completed. Existing callers ignoring its return continue to work.

- [x] Add a failing test proving primary currently calls extraction before assignment.
- [x] Add tests for manual decisions, unresolved units, stale source versions and no legacy `_ingest` in primary.
- [x] Call assignment before both primary pipelines, re-read the task/units after assignment, and recheck current source before derived writes.
- [x] Keep dual and legacy ordering compatible; no duplicate ownership model calls.
- [x] Primary task remains review if retained units are unresolved. Confirmed units can be processed independently; an unresolved unit must not become a project event through an archive fallback.
- [x] Run targeted primary/ownership/conversation tests, record results and review diff.

## Task 2: Shared scope, provenance and repeat protection

**Files:** evolvmem/capability_pipeline.py, project_archive_pipeline.py, capability_store.py, context_store.py; capability/project pipeline and store tests.

**Interfaces:** Scope resolution reads unit decision, knowledge_scope and projects. The primary project label must be active and registered; multi-project applicability is retained as a relation, not collapsed by choosing projects[0]. Existing source_ref remains a display-compatible pointer; new source bindings keep task ID, digest, source revision, rule revision and source ranges.

- [x] Add tests for conversation source with confirmed unit project, unresolved unit, explicit global, multiple projects and invalid archive fallback.
- [x] Add multi-source upsert tests: same content can retain multiple sources, a newly confirmed source repairs an empty derived label, conflicting sources do not silently overwrite the old label.
- [x] Persist a per-unit extraction receipt keyed by source identity/revision, unit digest and extraction policy version. An identical receipt reuses the candidate without another model call; current source guards still run before idempotent publication.
- [x] Cache only the candidate response, then publish derived rows and source/value bindings atomically; model calls remain outside the transaction. A stale source after model return saves no new derived row.
- [x] Preserve independent success-evidence rules; capability knowledge is not promoted into verified method experience.
- [x] Validate retries, partial failure, interrupted claim and a newer source revision.

## Task 3: Historical repair inventory and first bounded rehearsal

**Files:** new governance audit/repair module and CLI, tests for dry-run/CAS/idempotence; docs.

**Interfaces:** Dry-run emits immutable source IDs, before-values/revisions, proposed evidence-backed changes and skip reason codes. Apply accepts only the exact reviewed manifest hash, records before/after rows and rejects stale source versions. It does not delete archives, replay unrelated sources or call a model by default.

- [x] Build inventory from the copied SQLite DB and encrypted archives without initializing a live worker.
- [x] Distinguish current tasks from superseded/retired, native source snapshots from logical conversations and derived records from original facts.
- [x] Reconcile old records only when an existing current manual/evidence-bound decision supports it. Otherwise output an explicit unresolved reason; historical withdrawn decisions never auto-restore.
- [x] Test dry-run has zero writes, reapply has zero new effects, source/decision drift rejects and rollback preserves later unrelated changes.
- [x] Run the same bounded plan on a writable copy and verify source payload checksums unchanged.

## Next slice

After Phase A validation, implement capability source eligibility and full-set vector reconciliation, then hybrid project/module candidate recall and structured knowledge-value gates. The top-level goal remains active until historical migration, deployment and retrieval verification are complete; Phase A alone is not full completion.


## Expanded implementation and release gate

- [x] Current source provenance and value intersection gate for capability recall.
- [x] Current source support for project events, retaining withdrawn history.
- [x] Background capability vector reconciliation and independent capability IDs.
- [x] Business module registry, exact source bindings, authenticated API and desktop UI.
- [x] Registered module vector candidates plus source-bound role judgment.
- [x] Full current logical-source inventory, compact-source cleaning repair and resumable bounded task queue.
- [x] Synthetic configured-provider acceptance: useful procedure remains unverified; unsupported success claim does not become verified knowledge.
- [ ] Complete cross-component browser and source-level regression.
- [ ] Fresh private production backup and guarded source deployment.
- [ ] Current-live repair plans, bounded application, queue processing and full coverage receipt.
- [ ] Live recall/index verification, public source release, operational receipt and final checkpoint.

The earlier Phase A boxes record implemented and tested components, not
production completion. Backup rehearsal counts and live counts must remain
distinct in the final receipt.

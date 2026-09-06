# Context Core 技术参考

安装和入门请先读 [README](../README.md)。本页保留存储、生命周期、续接与参数的详细说明。

Context Core is the canonical EvolvMem store once the run mode leaves `legacy`: every memory is one typed `ContextItem` and SQLite remains the content source of truth. In `compat`/`shadow`/`primary` modes the legacy `memories` table becomes a transactional legacy projection — one outer `BEGIN IMMEDIATE` writes the `ContextItem`, its three layers, the legacy projection row, and their ID mapping — so adapters that have not switched keep reading their old shapes while every canonical write lands in Context Core. Both vector indexes stay derived, independently dirty-able caches updated only after the SQLite commit; the Context Core cache is `context_vectors.usearch`, separate from the legacy `vectors.usearch` throughout the transition.

Context Core's L0/L1/L2 names describe three bounded representations of one `ContextItem`, not the legacy stack's active/history/vector labels: L0 is the compact retrieval representation served by search, L1 is the bounded detailed representation rendered into session-start injection, and L2 retains the complete source/evidence representation, disclosed only by exact context ID. Default FTS/trigram indexes contain only L0 and L1; an explicit vector rebuild uses active, unexpired L0 only.

### Run modes

`context_mode` (persisted in `config.json`, overridable per process via `EVOLVMEM_CONTEXT_MODE`) selects one of four modes; an unknown value fails closed to a diagnostic tool set:

| Mode | Behavior |
|---|---|
| `legacy` | Code default. Context reads are disabled (`context_not_enabled`); all reads/writes use the service-owned legacy backend. |
| `compat` | Persisted only by a successful formal cutover. Context Core is the canonical write store and the legacy projection is kept in sync transactionally; Context reads stay disabled for the process. |
| `shadow` | Codex/Kimi/DSH. Explicit `context_*` reads are served while legacy reads still come from the legacy retriever, with a shadow comparison for gate measurement; no automatic injection instructions are issued. |
| `primary` | Codex/Kimi/DSH. Core reads/writes with automatic L1 session-start recall plus explicit search/read. A degraded primary fails closed: structured reads/writes are disabled, `context_status` remains diagnostic, and legacy/continuity tool names can remain listed with call-time readiness checks. Instructions state that memory is unavailable. |

### Adapter matrix

| Adapter | Writes | Reads / injection |
|---|---|---|
| Codex / Kimi / DSH | Context Core when enabled | Ready shadow/primary exposes structured Context and experience tools; primary provides active recall instructions |
| Claude | Mode-selected compatibility boundary | Legacy MCP memory tools; its SessionStart helper can try Core in primary and fall back to legacy |
| Web | Mode-selected compatibility boundary | Operator data views and organization; reading the page does not record successful experience |

The structured MCP adapter allowlist is `codex`, `kimi`, `dsh`. Expanding it to other adapters remains future work; enabling a client requires its own acceptance, not merely renaming an environment variable.

### Context tools and recall contract

In ready `shadow`/`primary` mode Codex, Kimi, and DSH expose structured Context and experience tools next to the legacy tools. The four read-only `context_*` tools are `context_session_start`, `context_search`, `context_read`, and `context_status`; `experience_recall` is also read-only. Exact availability comes from `evolvmem/mcp_contract.py`. Automatic recall has one hard limitation: it depends on Codex following the MCP server `instructions`. Every automatic call is fail-open — if a context tool is unavailable, errors, or times out, Codex continues the current task without memory — and the server never claims an injection happened when it did not.

The operator procedure for the formal Codex cutover (preflight, backup, apply, verification, rollback) lives in [docs/codex-context-core-runbook.md](codex-context-core-runbook.md). To create the Context Core schema and safely migrate local legacy rows ahead of time, run this from the repository with its environment active:

```bash
python - <<'PY'
from evolvmem import Config, ContextCore

config = Config.from_file()
core = ContextCore(config)
try:
    report = core.initialize()
    print(report.migration)
finally:
    core.close()
PY
```

This command is local, transactional, non-destructive, and safe to repeat. It does not load or download an embedding model and does not create or rebuild either vector index.

### Phase 3: encrypted session archives, candidates, and lifecycle

Phase 3 adds an evidence-backed experience lifecycle on top of Context Core. At session end the Kimi adapter can retain the raw conversation in an encrypted local session archive alongside extracted records: payloads are AES-GCM encrypted files under `session_archives/` (never SQLite BLOBs, never sent to any provider), the symmetric key lives in `archive.key` with owner-only permissions, and each archive expires `context_archive_ttl_days` (default 30) days after the session. Archiving is best-effort local evidence, not a precondition for extraction: without an encryption backend or on any write failure the hook logs a content-free warning and extraction continues without source links — it never falls back to plaintext on disk.

Every SessionStart and each explicit `context_sweep` runs a lightweight TTL purge that deletes expired archive payloads and marks their rows `purged`; `context_archive_project` purges one project's available archives immediately. Purge is irreversible — a failed purge keeps the archive `available` and the next sweep retries; active ContextItems are never deleted, only their `source_state` is recomputed.

Extraction items the model marks `experience` or `playbook` are quarantined as Core candidates: no legacy projection row, no vector cache entry, and they never enter retrieval or the session-start block — they are visible only through the explicit candidate review API (`ContextService.list_candidates`, read-only L0 metadata). A candidate becomes active in two ways: a user confirms it through `context_confirm` (which also records a `confirmed` evidence), or automatic promotion fires once it carries at least `context_promotion_min_successes` (default 2) `success` evidence rows from distinct session archives and zero `failure` evidence. `context_record_outcome` appends `success`/`failure`/`confirmed`/`contradicted` evidence; a `failure` or `contradicted` outcome lowers confidence, and an active experience whose failures reach its successes is archived, which demotes any dependent playbook back to candidate review.

When one project (or the global scope) holds at least `context_playbook_min_experiences` (default 3) active experiences — each with at least two `success` evidence and no unresolved contradiction — whose pairwise L0 normalized similarity `(1+cos)/2` reaches `context_promotion_similarity_threshold` (default 0.95), one playbook is generated automatically, referencing its source experiences without overwriting them. If the LLM or embedding engine is unavailable, or the generated output fails the quality gates, nothing changes and the run reports its explicit degraded reason instead of failing.

The four lifecycle tools — `context_confirm`, `context_record_outcome`, `context_archive_project`, `context_sweep` — are exposed to Codex, Kimi, and DSH in `shadow`/`primary` mode with a ready service. They carry no `readOnlyHint`, so MCP clients treat them as write operations that require approval; the two purge tools are irreversible for the payloads they delete.

## Project Continuity

**Project attribution.** Every typed write resolves its project through a small registry: `context_project_registry` holds registered projects, `context_project_aliases` maps globally unique alias names to them, and `context_project_workspace_bindings` binds HMAC workspace fingerprints to projects — the raw path is fingerprinted with the owner-only `workspace.key` and never stored. Each write also records a `context_project_resolutions` row; a conflicted or unresolvable write goes `pending` with an empty project instead of guessing, and operator accept/reject decisions are final. Administration is JSON-only (identifiers, revisions, and stable codes — never memory content or absolute paths), and every state change is guarded by `--expected-revision` CAS:

```bash
python -m evolvmem.project_cli bootstrap-key
python -m evolvmem.project_cli projects register myproj
python -m evolvmem.project_cli fingerprint /path/to/workspace
python -m evolvmem.project_cli bindings bind <fingerprint> myproj --default
python -m evolvmem.project_cli resolutions list-pending
python -m evolvmem.project_cli resolutions accept 12 myproj --expected-revision 3
```

**Rolling project summaries.** After a session-end extraction batch lands its summary on a resolved project, a best-effort rollup refreshes that project's single active `project:{project}:knowledge:current` summary — one transaction supersedes the old PROJECT_SUMMARY, and the L0 vector handoff follows the commit. The source set is the project's active session summaries plus the atomic items created after the rollup's `covered_through` watermark; when its hash is unchanged the project is `skipped`/`unchanged` and the LLM is never called, and when no LLM is wired the run degrades to `skipped`/`llm_unavailable` and writes nothing. A failed attempt leaves the old active summary untouched. Manual runs use the same configured extraction provider; missing credentials report `llm_unavailable` and leave the current summary unchanged:

```bash
python -m evolvmem.project_cli rollup run --project myproj
```

**Session-log TTL and coverage-gated archiving.** Each project keeps its newest `context_session_summary_keep` (default 10) active session summaries; older ones are archived only once they belong to the rollup's covered source set, and archiving them releases the `rollup_pending` holds that kept their raw encrypted archives unpurgeable. An expired summary without rollup coverage is held, never archived, and its project is reported pending until a rollup covers it. Every freshly written summary records its archive hold in the same write transaction, and the retention sweep follows the archive TTL purge at SessionStart and behind `context_sweep`, fail-open.

**Continuity tools.** The same `python -m evolvmem.mcp_server` process configured above exposes three continuity tools to Codex, Kimi, and DSH in `compat`/`shadow`/`primary` mode: `continuity_resume` (exact focused-workstream read — never semantic search), `continuity_checkpoint` (a closed action whitelist under per-row revision CAS; a stale token reliably returns `revision_conflict`), and `continuity_list` (unfinished workstreams, metadata plus L0 summaries only). The transient workspace path is fingerprinted and discarded; readiness is checked per call (`continuity_not_ready`), so the tools stay listed even in `compat` or a degraded `primary`. A "继续原任务"-style session-start query is detected as control intent and routed to an exact resume instead of FTS/HNSW; no-focus, multi-candidate, and dangling-focus states each return a stable code, and any continuity failure silently degrades to the normal retrieval path.

**One-shot historical backfill.** `plan` prints a read-only, deterministic preview whose digest fingerprints the database. `apply` recomputes the plan under the exclusive cutover lock (a digest mismatch is a usage error, not an apply failure), makes a verified cutover backup, then migrates legacy rows and backfills project resolutions in one transaction before rolling summaries, sweeping retention, and rebuilding the disposable Context vector cache; already-converged rows are skipped, so re-applying is a no-op. `verify` checks the post-apply invariants and exits 0 only when every one passes. Output carries counts, identifiers, and stable codes only:

```bash
python -m evolvmem.maintenance_cli plan
python -m evolvmem.maintenance_cli apply --plan-digest <hex> --yes
python -m evolvmem.maintenance_cli verify
```

## Configuration

Edit `${EVOLVMEM_DATA_DIR:-$HOME/.claude/evolvmem}/config.json` to adjust the following parameters:

- `fts_top_k` / `vector_top_k`: FTS5 and vector search recall counts, default 20 each
- `fts_weight` / `vector_weight`: Hybrid search weight allocation, default 0.6 / 0.4
- `forget_days_threshold`: Days since last access before a memory can be archived, default 90
- `forget_access_count_threshold`: Max access count below which memories may be downgraded, default 2
- `embedding_model_filename`: GGUF filename to load; the canonical default is `nomic-embed-text-v1.5.f16.gguf`
- `embedding_dim`: Vector dimension, must match model, default 768 for the canonical Nomic model
- `embedding_query_prefix` / `embedding_doc_prefix`: Task prefixes applied when embedding queries/documents (nomic defaults `search_query: ` / `search_document: `, set to `""` to disable)
- `context_mode`: Context Core run mode — `legacy` (default), `compat`, `shadow`, or `primary`; an unknown value fails Context features closed. `EVOLVMEM_CONTEXT_MODE` overrides the persisted value per process
- `adapter`: Current adapter identity (`codex`, `claude`, `kimi`, `dsh`, `web`), default empty (unspecified); `EVOLVMEM_ADAPTER` overrides it per process
- `context_l0_max_chars` / `context_l1_max_chars` / `context_l2_max_chars`: Character caps for one `ContextItem`'s L0/L1/L2 representations, defaults 240 / 1200 / 6000; must satisfy L0 ≤ L1 ≤ L2
- `context_inject_max_chars`: Total character budget of one Context session-start injection, default 6000
- `context_inject_max_items`: Max items in one Context injection, default 12
- `context_inject_pinned_max_chars` / `context_inject_project_max_chars` / `context_inject_related_max_chars`: Character budgets of the pinned, current-project, and related injection pools, defaults 1500 / 3000 / 1500
- `context_min_confidence`: Minimum confidence for Context retrieval and injection, default 0.55
- `context_vector_min_similarity`: Minimum normalized similarity for pure-vector Context candidates, default 0.80
- `context_fts_weight` / `context_vector_weight`: Fusion weights of the lexical and vector Context retrieval channels, defaults 0.60 / 0.40; must sum to 1.0
- `context_score_relevance_weight` / `context_score_project_weight` / `context_score_type_weight` / `context_score_confidence_weight` / `context_score_importance_weight` / `context_score_evidence_weight` / `context_score_recency_weight` / `context_score_frequency_weight`: Context ranking weights, defaults 0.35 / 0.15 / 0.10 / 0.10 / 0.10 / 0.05 / 0.10 / 0.05; must sum to 1.0
- `context_recency_tau_days`: Recency decay time constant in days for Context scoring, default 30.0
- `context_frequency_cap`: Access-count normalization cap for Context frequency scoring, default 20
- `context_project_aliases`: Map of workspace directory name → project name for Context project matching, default `{}`
- `context_archive_ttl_days`: Days an encrypted session archive is retained before the TTL purge deletes its payload, default 30
- `context_promotion_min_successes`: Distinct-archive `success` evidence rows (with zero failures) required to auto-promote a candidate experience to active, default 2
- `context_playbook_min_experiences`: Minimum active experiences in one project (or global scope) forming a playbook qualification cluster, default 3
- `context_promotion_similarity_threshold`: Minimum pairwise normalized L0 similarity `(1+cos)/2` for a playbook qualification cluster, default 0.95
- `inject_max_count`: Max memories injected on SessionStart, default 50
- `inject_max_chars`: Total character budget for SessionStart injection, default 8000
- `inject_pinned_max_count` / `inject_pinned_max_chars`: Max count and character budget for the pinned layer, default 10 / 2000
- `inject_index_max_chars`: Character budget for the index layer, default 1000 (0 disables the index layer)
- `inject_key_prefix_quota`: Max injected memories sharing the same key prefix (first two segments), default 3
- `inject_w_importance` / `inject_w_recency` / `inject_w_frequency`: Scoring weights for importance/10, recency decay, and log1p(access_count), default 0.5 / 0.3 / 0.2
- `inject_recency_tau_days`: Recency decay time constant in days, default 14.0
- `inject_freq_norm_cap`: Access-count normalization cap for frequency scoring, default 20
- `inject_w_relevance`: Weight of the project-relevance bonus in SessionStart scoring (memories whose key contains the current directory name — or its alias — as a substring), default 0.3
- `inject_project_aliases`: Map of directory name → memory key segment for project matching (e.g. `{"my-project": "myproj"}`), default `{}`
- `consolidate_similarity_threshold`: Similarity threshold above which two memories are near-duplicate merge candidates for `memory_consolidate`, default 0.92. Note the metric is `similarity = (1+cos)/2` (not raw cosine): 0.92 corresponds to a true cosine of ≈ 0.84; for real merges a threshold ≥ 0.97 (≈ cosine 0.94) is recommended
- `consolidate_auto_run_hours`: Minimum interval between auto-consolidation runs at SessionStart (merges near-identical pairs at a conservative 0.97 threshold; failures never block session start), default 168 (weekly); 0 disables
- `add_merge_threshold`: Write-time semantic merge threshold — when a new value's similarity to an existing memory meets or exceeds it, the existing memory is superseded instead of adding a near-duplicate, default 0.95
- `expires_at` (per-memory field, not config): Optional expiry date set via `memory_add` (e.g. `2026-12-31`); expired memories are excluded from injection and search, and are auto-archived
- `forget_auto_run_hours`: Minimum interval between auto-forgetting runs at SessionStart, default 24
- `forget_rate_limit_days`: Minimum interval between two downgrades of the same memory, default 7
- `stop_hook_safe`: Prevent Stop Hook infinite loops, default true
- `value_max_chars`: Hard length cap on `memory_add`/`memory_replace` values, default 500
- `value_min_chars`: Minimum length for `memory_add`/`memory_replace` values — shorter values are rejected as having no information content, default 10

## Dependencies

Install the base Python dependencies from the repository root (also handled by `bash install.sh`):

```bash
python -m pip install .
```

Local semantic retrieval is optional. `bash install.sh --with-embedding` installs the `llama-cpp-python` extra and downloads Nomic Embed Text v1.5 F16 GGUF (768 dimensions); the base installation does neither. For manual installation, run `python -m pip install '.[embedding]'` and place `nomic-embed-text-v1.5.f16.gguf` in the data directory's `models/` folder (default `~/.claude/evolvmem/models/`). Existing configuration and model files are preserved.

### Migration from the prior BGE installer

The old installer wrote only a 512-dimensional setting while downloading its BGE model. To keep using that model, explicitly add its filename and matching prefixes to `config.json` before using its existing vector index. Otherwise migrate to the Nomic 768-dimensional contract and rebuild `vectors.usearch`; do not mix vectors from the two model spaces.

## Architecture

The active compatibility stack has three layers: active-memory injection (L0, SessionStart system prompt), exact retrieval (L1, SQLite + FTS5/trigram), and semantic retrieval (L2, USearch HNSW). Context Core routing is mode-driven as described above: in `compat`/`shadow`/`primary` modes Context Core is the source of truth and the legacy stack becomes its transactional projection, while `legacy` mode keeps serving the pre-cutover path unchanged. Memories self-iterate through auto-extraction, conflict detection, and access-decay forgetting. Durable data is stored locally. Manual storage and lexical retrieval need no external model service; enabled extraction, rollups and generated playbooks can call the configured provider.

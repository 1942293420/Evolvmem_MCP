# EvolvMem

A fully-local, three-layer memory plugin for Claude Code with Chinese language support — FTS5/trigram + HNSW vector hybrid search.

## Features

- **L0 Active Memory**: SessionStart injection — a project digest layer (recent per-project session summaries from `:progress:log:` memories, `digest_*` config), then pinned memories always injected, normal memories ranked by importance+recency+frequency score, the rest listed as a searchable index (progressive disclosure)
- **L1 Full History**: SQLite + FTS5/trigram exact search, supports Chinese substring matching
- **L2 Semantic Index**: USearch HNSW vector search for finding related memories expressed differently
- **Self-Iteration**: Auto-extraction, conflict detection, access-decay forgetting
- **Reliable Session Extraction**: Kimi SessionEnd extraction sends the complete conversation first, falls back to message-boundary chunks only after an explicit context-window error, and keeps transient failures pending for retry
- **Crash Recovery**: An optional stale-session worker reprocesses idle `wire.jsonl` sessions that never reached SessionEnd; completed/skipped sessions advance state, while timeouts, rate limits, and malformed responses do not
- **Consolidation**: `memory_consolidate` finds and merges near-duplicate memories via vector similarity (dry-run by default)
- **Semantic Merge**: Write-time semantic merge — new values automatically supersede near-identical memories instead of duplicating them (`add_merge_threshold`) — plus weekly auto-consolidation at SessionStart (`consolidate_auto_run_hours`)
- **Expiry**: Memories can carry an `expires_at` date; expired memories stop being injected/searched and are archived automatically
- **Project Relevance**: SessionStart scoring boosts memories whose key matches the current project directory (configurable aliases)
- **Quality Gate**: `memory_add`/`memory_replace` reject values shorter than `value_min_chars` (default 10) and low-information placeholder phrases (e.g. "等待用户确认", "no action required"), keeping trivial auto-summary noise out of the store

## Quick Start

```bash
./install.sh
```

The script will automatically:
- Create `~/.claude/evolvmem/` directory and `models/` subdirectory
- Install pip dependencies usearch and llama-cpp-python
- Download bge-small-zh-Q5_K_M.gguf (~50MB, skipped if already present)
- Generate default `config.json`
- Verify the Config module can be imported

## Manual Configuration

Add the MCP Server config to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "evolvmem": {
      "command": "python",
      "args": ["-m", "evolvmem.mcp_server"],
      "env": {
        "PYTHONPATH": "/path/to/evolvmem-plugin"
      }
    }
  }
}
```

Optional: add a SessionStart hook for automatic active memory injection:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hook": "python -c \"from evolvmem.hooks import get_session_start_block; print(get_session_start_block())\"",
        "env": {
          "PYTHONPATH": "/path/to/evolvmem-plugin"
        }
      }
    ]
  }
}
```

### Kimi Code automatic extraction

`evolvmem.kimi_hooks session-end` reads the session's complete `wire.jsonl` conversation and sends it to the configured extraction provider. DeepSeek V4 Flash in non-thinking mode is the default. Provider credentials live outside the repository at `~/.claude/evolvmem/llm_credentials.json`:

```json
{
  "provider": "deepseek",
  "api_key": "your-key-here",
  "base_url": "https://api.deepseek.com/chat/completions",
  "model": "deepseek-v4-flash"
}
```

The extractor requests a top-level JSON object shaped as `{"memories": [...]}`. The parser also accepts the legacy top-level array for compatibility. It does not pre-split ordinary long conversations. Only an explicit model context-window error triggers fallback chunks, which preserve user/assistant message boundaries. HTTP 429, transient 5xx responses, network timeouts, authentication failures, and responses without the required session summary are reported as retryable instead of being treated as successful extraction.

For sessions that terminate without firing SessionEnd, run the offline worker periodically:

```cron
23 * * * * /usr/bin/flock -n ~/.claude/evolvmem/.extract_stale.lock env PYTHONPATH=/path/to/evolvmem-plugin /path/to/evolvmem-plugin/.venv/bin/python /path/to/evolvmem-plugin/scripts/extract_stale_sessions.py >> ~/.claude/evolvmem/extract_stale.log 2>&1
```

The worker scans sessions idle for at least 30 minutes and processes at most three per run. Its state file is `~/.claude/evolvmem/.extracted_sessions.json`. A session version is recorded only after a `completed` or intentional `skipped` result; retryable failures remain pending, and exhausted rate limiting stops the rest of that run.

## Tools

| Tool Name | Description |
|---|---|
| `memory_search` | FTS5 + HNSW hybrid search, supports Chinese |
| `memory_status` | View memory system status and statistics |
| `memory_add` | Manually write a memory (optional `importance` 1-10, `tier` pinned/normal, and `expires_at` date parameters) |
| `memory_replace` | Replace a memory (old value marked as superseded) |
| `memory_remove` | Soft-delete a memory |
| `memory_consolidate` | Find and merge near-duplicate memories by vector similarity; `dry_run=true` (default) only reports candidates |

Deletion is two-staged: `memory_remove` soft-deletes (recoverable via restore), while the Web Console's `POST /api/memory/<id>/hard_delete` permanently removes the row — irreversible, intended for confirmed junk. The quality gate above applies to every live `memory_add`/`memory_replace` call, so rejected values never enter the store in the first place.

## Web Console

`python -m evolvmem.web_server --host 0.0.0.0 --port 9377` serves a local console for browsing, filtering, editing and deleting memories (`/api/stats`, `/api/memories`, `/api/memory/<id>/<action>` with actions `update|archive|restore|delete|hard_delete`).

The stats "hot list" (`top_accessed`) ranks by composite heat — `importance × (access_count + 1)` — instead of raw hit count, so a high-importance memory with few hits outranks a trivial one that was matched often; each entry carries both `access_count` and `importance` so the two signals stay visible. Raw `access_count` still counts every retrieval hit and remains available as a pure frequency signal elsewhere in the console.

## Data Directory

All data is stored under `~/.claude/evolvmem/`:

| File/Directory | Description |
|---|---|
| `memory.db` | SQLite database with FTS5/trigram indexes |
| `vectors.usearch` | USearch HNSW vector index |
| `models/` | BGE-small-zh Q5_K_M GGUF model file |
| `config.json` | Retrieval, forgetting, and other parameters |

## Configuration

Edit `~/.claude/evolvmem/config.json` to adjust the following parameters:

- `fts_top_k` / `vector_top_k`: FTS5 and vector search recall counts, default 20 each
- `fts_weight` / `vector_weight`: Hybrid search weight allocation, default 0.6 / 0.4
- `forget_days_threshold`: Days since last access before a memory can be archived, default 90
- `forget_access_count_threshold`: Max access count below which memories may be downgraded, default 2
- `embedding_dim`: Vector dimension, must match model, default 768
- `embedding_query_prefix` / `embedding_doc_prefix`: Task prefixes applied when embedding queries/documents (nomic defaults `search_query: ` / `search_document: `, set to `""` to disable)
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

Python dependencies (auto-installed by install.sh):

```bash
pip install usearch llama-cpp-python
```

Embedding model: BGE-small-zh Q5_K_M GGUF (~50MB), auto-downloaded by install.sh. For manual download, place `bge-small-zh-Q5_K_M.gguf` in `~/.claude/evolvmem/models/`.

## Architecture

Three-layer memory structure: active memory (L0, SessionStart system prompt injection) -> exact retrieval (L1, SQLite + FTS5/trigram) -> semantic retrieval (L2, USearch HNSW). Memories self-iterate through auto-extraction, conflict detection, and access-decay forgetting. All data is stored locally, no external services required.

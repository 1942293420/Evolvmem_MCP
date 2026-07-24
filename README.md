# EvolvMem

A fully-local, three-layer memory plugin for Claude Code with Chinese language support — FTS5/trigram + HNSW vector hybrid search.

## Features

- **L0 Active Memory**: Auto-injected into system prompt on SessionStart
- **L1 Full History**: SQLite + FTS5/trigram exact search, supports Chinese substring matching
- **L2 Semantic Index**: USearch HNSW vector search for finding related memories expressed differently
- **Self-Iteration**: Auto-extraction, conflict detection, access-decay forgetting

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

## Tools

| Tool Name | Description |
|---|---|
| `memory_search` | FTS5 + HNSW hybrid search, supports Chinese |
| `memory_status` | View memory system status and statistics |
| `memory_add` | Manually write a memory |
| `memory_replace` | Replace a memory (old value marked as superseded) |
| `memory_remove` | Soft-delete a memory |

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
- `embedding_dim`: Vector dimension, must match model, default 512

## Dependencies

Python dependencies (auto-installed by install.sh):

```bash
pip install usearch llama-cpp-python
```

Embedding model: BGE-small-zh Q5_K_M GGUF (~50MB), auto-downloaded by install.sh. For manual download, place `bge-small-zh-Q5_K_M.gguf` in `~/.claude/evolvmem/models/`.

## Architecture

Three-layer memory structure: active memory (L0, SessionStart system prompt injection) -> exact retrieval (L1, SQLite + FTS5/trigram) -> semantic retrieval (L2, USearch HNSW). Memories self-iterate through auto-extraction, conflict detection, and access-decay forgetting. All data is stored locally, no external services required.

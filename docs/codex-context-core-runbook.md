# Codex Context Core Cutover Runbook

This runbook is the operator procedure for the reversible Context Core Codex
cutover. It installs nothing blindly: every step is dry-run first, and no
database or config file is overwritten without an explicit `--apply`.

Reference for the Codex-side MCP configuration knobs mentioned below
(stdio `env`, server `instructions`, `enabled_tools`/`disabled_tools` tool
policy, and `default_tools_approval_mode`/per-tool approval):
<https://developers.openai.com/codex/mcp>.

## What the cutover changes

- EvolvMem persisted config flips from `legacy` to `compat`: Context Core
  becomes the canonical write store and the legacy `memories` table becomes a
  transactional legacy projection. Claude, Kimi, DSH, and the Web Console keep
  their existing read shapes against that projection; their writes now land in
  Context Core canonically. They get no Context automatic injection yet —
  switching them to `primary` remains future work.
- The Codex MCP stanza (`mcp_servers.evolvmem` in `~/.codex/config.toml`) is
  switched to `primary` for that one process via environment overrides
  (`EVOLVMEM_ADAPTER=codex`, `EVOLVMEM_CONTEXT_MODE=primary`) plus
  `default_tools_approval_mode = "writes"`. Codex then reads and writes Core,
  receives automatic L1 session-start recall instructions, and can call the
  four read-only `context_*` tools explicitly.
- Environment overrides win over the persisted `config.json`
  (`EVOLVMEM_CONTEXT_MODE` / `EVOLVMEM_ADAPTER`); an unknown mode value is
  fail-closed — Context features shut down to `context_status` diagnostics and
  writes are rejected, never silently promoted to another mode.

## Prerequisites

- Run everything from the repository with its Python environment active.
- Every command takes explicit absolute paths only; the CLI never guesses a
  default data directory or config path. The CLI also strips any
  `EVOLVMEM_DATA_DIR` / `EVOLVMEM_CONTEXT_MODE` / `EVOLVMEM_ADAPTER` process
  overrides for the duration of the command so the explicit arguments win.
- Restart every writer process before the real apply: old Claude/Kimi/DSH/Web
  or MCP server processes do not take the cutover lock and must not write
  during the switch. The `cutover --apply` command refuses to run without the
  `--writers-restarted` acknowledgement.

## Step 0 — side-effect-free preflight

```bash
python -m evolvmem.cutover_cli preflight \
  --data-dir /absolute/data/dir \
  --codex-config /absolute/config.toml \
  --output /absolute/preflight.json --json
```

Preflight is a pure inspection: it opens the database through a read-only,
query-only SQLite connection and performs no schema creation, directory
creation, vector initialization, config write, access-count update, or
migration. Exit code 0 means `ready=true`; 1 means a gate failed — read the
reason codes in the report. The output envelope carries a digest that the
later steps re-verify under the cutover lock.

## Optional standalone backup

```bash
python -m evolvmem.cutover_cli backup \
  --preflight-report /absolute/preflight.json --json          # dry-run
python -m evolvmem.cutover_cli backup \
  --preflight-report /absolute/preflight.json --apply --json  # verified backup
```

`backup` without `--apply` is a dry-run that creates nothing. The `cutover`
step always creates its own fresh verified backup under the lock even when a
standalone backup exists, so this subcommand is optional verification tooling.

Backups live in a unique `backups/context-core-cutover-<UTC>/` directory with
owner-only permissions (directory 0700, files 0600). A backup contains the
database snapshot taken through the SQLite Backup API (never a raw file copy,
so committed rows inside an uncheckpointed WAL are not lost), the pre-cutover
config copies, the structured Codex stanza snapshot, and the legacy vector
file when present. The manifest is written last and every entry is then
independently reopened and re-hashed; a partial failure leaves an `INCOMPLETE`
marker instead of a `complete=true` manifest. Backups are never deleted automatically;
they are retained until a separate cleanup decision after all
adapters have completed their primary migration.

## Apply the cutover

```bash
python -m evolvmem.cutover_cli cutover \
  --preflight-report /absolute/preflight.json \
  --writers-restarted \
  --output /absolute/cutover-result.json --json               # dry-run
python -m evolvmem.cutover_cli cutover \
  --preflight-report /absolute/preflight.json \
  --writers-restarted \
  --output /absolute/cutover-result.json --apply --json       # real apply
```

Without `--apply` the command performs no write at all. With `--apply` it runs
the journaled ten-step gate chain (validate inputs → lock and re-run preflight
→ snapshot and backup → migrate schema → validate migration → stage vector →
shadow gates → persist `compat` → Codex `primary` CAS → release and await the
post-cutover canary). A failure before the persist step changes neither the
EvolvMem mode nor the Codex config; a failure at or after the Codex step
forces an operational CAS rollback to explicit `legacy` while the persistent
`compat` mode and migrated Context data are retained — the database is never
restored or deleted automatically.

### FTS-only degraded apply

`--allow-fts-only` is meaningful only beside an explicit `--apply` (the parser
rejects it otherwise). It approves continuing without a rebuilt Context vector
index and is recorded as degraded: the journal's vector gate at
`vector_ready_or_approved_fts` carries `status="fts_only"`, the cutover result
carries `fts_only=true`, and neither ever claims vector health.
Operationally this means retrieval runs on the FTS5/trigram lexical
channel only (no semantic/vector recall) until an explicit Context vector
rebuild completes; `context_status` keeps reporting
`context_vector_ready=false` until then.

## Journal and status interpretation

The owner-only journal `cutover-journal.json` lives inside the verified backup
directory and moves only forward:
`planned → locked → backed_up → migrated → vector_ready_or_approved_fts →
shadow_passed → compat_persisted → codex_primary →
awaiting_post_cutover_canary → complete`. `failed` and `rolled_back` are
terminal side branches carrying the exact failed step. The journal and every
public projection contain only hashes, counts, reason codes, and timestamps —
never memory content or stanza values.

After the apply, the `context_status` tool reports the live snapshot: `mode`,
`adapter`, `ready`, per-status item counts (`status_counts`), `mapping_count`,
`projection_lag` (must be 0 after a healthy cutover),
`context_vector_ready`/`context_vector_dirty`,
`legacy_vector_ready`/`legacy_vector_dirty`, plus safe `diagnostics` and
`reason_codes`. A healthy Codex `primary` shows `ready=true`; a degraded
primary reports `ready=false` with reason codes such as `degraded_legacy` and
serving fails closed. The snapshot never contains data directories or memory
content.

## Codex config scope, CAS, and approval semantics

The editor touches only the `mcp_servers.evolvmem` stanza of the Codex TOML
config. Every apply re-reads the current file and compares only the target
stanza hash (compare-and-swap), so concurrent unrelated edits survive and a
stanza that drifted since the snapshot aborts the write with nothing changed.
Edits go through a comment-preserving TOML document and a durable
same-directory atomic replace, so comments, formatting, unknown fields, and
other servers are preserved. The tool policy is gated: if `enabled_tools`
omits a required context tool or `disabled_tools` blocks one, the apply fails
closed.

Approval semantics follow the MCP tool annotations:

- The four `context_*` tools (`context_session_start`, `context_search`,
  `context_read`, `context_status`) are marked `readOnlyHint`; under
  `default_tools_approval_mode = "writes"` Codex does not prompt for them.
- The legacy write tools (`memory_add`, `memory_replace`, `memory_remove`,
  and a non-dry-run `memory_consolidate`) carry no read-only hint, so the
  `writes` mode keeps prompting before they mutate anything.

## Dual verification after the Codex switch

Codex CLI 0.147 does not echo approval fields: `codex mcp get evolvmem --json`
reports the name, enabled flag, stdio transport (`command`, `args`, `env`,
`cwd`), `enabled_tools`/`disabled_tools`, and timeouts, but not
`default_tools_approval_mode`. Verification is therefore dual-source:

1. The CLI-visible fields are compared against the intended stanza via
   `codex mcp get evolvmem --json`.
2. The approval field is verified by reparsing the target TOML stanza
   structurally (the cutover records the resulting stanza hash), with the
   real-behavior acceptance run as the backstop.

## Operational rollback and destructive restore

Operational rollback is journal-driven and fast:

```bash
python -m evolvmem.cutover_cli rollback \
  --journal /absolute/backup/dir/cutover-journal.json --json          # dry-run
python -m evolvmem.cutover_cli rollback \
  --journal /absolute/backup/dir/cutover-journal.json --apply --json  # apply
```

It compare-and-swaps the Codex stanza environment back to explicit
`EVOLVMEM_CONTEXT_MODE=legacy` and marks the journal `rolled_back`. The
persistent `compat` mode and the migrated Context data are retained — rollback
never restores or deletes the database. Without `--apply` it is a dry-run that
writes nothing and only reports whether the journal state is rollbackable.

Restoring a backup (database/vector files or the full pre-cutover stanza
snapshot) is a separate destructive recovery operation and requires its own
explicit human approval; it is never part of the normal rollback path.

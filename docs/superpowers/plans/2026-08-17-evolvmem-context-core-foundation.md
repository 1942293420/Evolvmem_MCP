# EvolvMem Context Core Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers-subagent-driven-development (recommended) or superpowers-executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** Deliver the safe, backward-compatible foundation for EvolvMem 2.0: a verified embedding runtime contract, a typed L0/L1/L2 Context Core stored in SQLite, idempotent lossless migration from legacy memories, and an isolated L0 vector-index rebuild path.

**Architecture:** Leave the existing MemoryStore, Retriever, hooks, and memory_* MCP tools as the production compatibility path during this stage. Add a standalone Context Core beside them. SQLite context tables are the source of truth; context_layers stores the three representations of one ContextItem; FTS/trigram and a separate USearch file are derived indices. A migration service imports legacy rows transactionally without deleting or mutating the legacy memories table. Later phases will switch adapters to the new core only after layered recall, lifecycle, archive, and compatibility APIs are proven.

**Tech Stack:** Python 3.10+, stdlib dataclasses/enums/sqlite3, SQLite FTS5/trigram, NumPy, existing USearch HNSW and llama-cpp-python. No server, ORM, external database, or external LLM is introduced in this phase.

**Design source:** docs/superpowers/specs/2026-08-17-evolvmem-context-core-design.md

## Scope and invariants

- The existing memory.db remains in place. The legacy memories table is never dropped, renamed, or rewritten by this plan.
- New context tables live in the same SQLite database, but ContextStore owns all SQL touching them. No adapter may use context SQL directly.
- A ContextItem is one logical memory. Its L0, L1, and L2 are layers of that same item, not three independently retrievable memories.
- L0 is the default lexical/vector document; L1 and L2 remain source content. The default FTS path indexes L0 and L1 only; L2 is never indexed by default.
- Context Core uses config.context_vector_path (context_vectors.usearch), not the existing vectors.usearch. This prevents ContextItem IDs from contaminating the legacy vector cache before old APIs are delegated in a later phase.
- Existing user data must remain recoverable. A malformed or unusual legacy row is migrated as a visible record or explicitly reported; it must never be silently dropped.
- New Context Core writes are atomic across context_items, context_layers, context_sources, and legacy_memory_migrations. A failed transaction leaves no partial ContextItem.
- Vector synchronization is best effort. A failed rebuild must leave a durable dirty marker and must not roll back committed SQLite context data.
- This plan intentionally does not implement candidate promotion, archive encryption/TTL, context injection, context_search/context_read MCP tools, or adapters. Those are separate follow-up plans.

## Clarifications applied by this foundation

1. The canonical fresh-install embedding profile is Nomic Embed Text v1.5 GGUF:

       filename: nomic-embed-text-v1.5.f16.gguf
       dimension: 768
       query prefix: search_query:
       document prefix: search_document:

   An existing 512-dimensional BGE configuration is not overwritten. It receives a clear runtime diagnostic until its filename/dimension/prefixes are made coherent by the user. This avoids silently loading a mismatched model and creating a corrupt vector index.

2. Legacy records can contain duplicate active keys, while the Context Core deliberately permits at most one active item for a normalized (identity_key, project, scope). During migration, the most recently updated legacy active row wins. Other active duplicates are retained as candidate ContextItems, carry a migration source marked duplicate-active, and appear in the migration report. No value is deleted.

3. A legacy value can be longer than a new injection limit. Its L2 is retained byte-for-byte after normalizing line endings; its L1 is the original value when within the L1 limit, otherwise a deterministic shortened overview; L0 is a deterministic <= 240-character abstract. Thus retrieval/injection stays bounded while the original durable content remains available in L2.

## Repository and execution context

- Worktree: /home/jiangli/hermes-memory-plugin/.worktrees/evolvmem-context-core-v2
- Branch: feat/evolvmem-context-core-v2
- Test interpreter: /home/jiangli/hermes-memory-plugin/.venv/bin/pytest
- Baseline before this plan: 313 passed, 2 skipped.
- Before each task, inspect git status. Preserve any user changes that appear after this plan was written.
- Use test-first development: add the focused failing test, run it to observe the expected failure, then implement the smallest coherent change.
- After any diagnosis/fix made while executing this plan, record it in /home/jiangli/fix-records/records/ according to /home/jiangli/fix-records/README.md. Do not claim a fix is verified unless its stated tests passed.

---

## Task 1: Make installation, configuration, and embedding runtime one contract

**Files:**

- Create: evolvmem/runtime_contract.py
- Create: tests/test_runtime_contract.py
- Modify: evolvmem/config.py
- Modify: evolvmem/embedding.py
- Modify: evolvmem/vector_index.py
- Modify: evolvmem/mcp_server.py
- Modify: install.sh
- Modify: README.md

- [ ] **Step 1: Write failing runtime-contract tests.**

  Cover these observable behaviors in tests/test_runtime_contract.py:

  - The default contract has filename nomic-embed-text-v1.5.f16.gguf, dimension 768, and non-empty query/document prefixes.
  - Config(data_dir=temp_dir).model_path is temp_dir / models / the contract filename.
  - Config.save followed by Config.from_file retains embedding_model_filename, embedding_dim, and both prefixes.
  - A configuration containing the Nomic filename with embedding_dim=512 produces a structured mismatch diagnostic; it must not claim the configuration is valid.
  - validate_runtime(require_model=True) reports a missing model without exposing credentials or a full home-directory path.
  - EmbeddingEngine.initialize rejects a mocked model whose probe embedding length differs from config.embedding_dim and leaves is_loaded false.
  - install.sh obtains the filename and download URL from evolvmem.runtime_contract and no longer contains bge-small-zh or a hard-coded 512 embedding dimension.

  Run:

      /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_runtime_contract.py

  Expected before implementation: import failure for evolvmem.runtime_contract and/or failed assertions for the old installer contract.

- [ ] **Step 2: Define the only canonical default embedding profile.**

  In evolvmem/runtime_contract.py, create these public interfaces:

      @dataclass(frozen=True, slots=True)
      class EmbeddingRuntimeContract:
          filename: str
          download_url: str
          dimension: int
          query_prefix: str
          document_prefix: str

      DEFAULT_EMBEDDING_CONTRACT: EmbeddingRuntimeContract

      def known_embedding_contract(filename: str) -> EmbeddingRuntimeContract | None

  Set DEFAULT_EMBEDDING_CONTRACT to the Nomic values in the scope section. Do not model arbitrary providers in this phase. known_embedding_contract may initially recognize only the canonical Nomic filename; custom models remain allowed but receive only generic dimensionality validation.

- [ ] **Step 3: Move Config to that contract without silently rewriting existing user settings.**

  Add Config fields:

      embedding_model_filename: str = DEFAULT_EMBEDDING_CONTRACT.filename
      embedding_dim: int = DEFAULT_EMBEDDING_CONTRACT.dimension
      embedding_query_prefix: str = DEFAULT_EMBEDDING_CONTRACT.query_prefix
      embedding_doc_prefix: str = DEFAULT_EMBEDDING_CONTRACT.document_prefix
      context_l0_max_chars: int = 240
      context_l1_max_chars: int = 1200
      context_l2_max_chars: int = 6000

  Add:

      @property
      def context_vector_path(self) -> Path:
          return self.data_dir / "context_vectors.usearch"

      def validate_runtime(self, *, require_model: bool = False) -> tuple[str, ...]

  validate_runtime must return diagnostics rather than calling sys.exit. Validate: non-empty filename, positive dimension, a known filename/dimension mismatch, invalid layer limits (L0 <= L1 <= L2 and all positive), and missing model when require_model is true. Messages should identify the filename and configuration field, but not print API keys, payloads, or an absolute user home path.

  Persist the new fields in Config.save. Config.from_file must continue to ignore unknown JSON keys and retain the current EVOLVMEM_DATA_DIR behavior.

- [ ] **Step 4: Verify the actual model output dimension at engine initialization.**

  In evolvmem/embedding.py:

  - Run config.validate_runtime(require_model=True) before importing/loading llama_cpp. Raise RuntimeError with the joined diagnostics when it returns any issue.
  - After creating Llama, call its embedding operation once on a fixed non-secret probe using the configured document prefix.
  - Normalize the response exactly as encode does, verify its vector length equals config.embedding_dim, then set self._dim to the observed dimension.
  - On probe failure or mismatch, close and clear the loaded model before raising. Do not leave a partially usable engine.

  Keep encode, encode_query, and encode_document backward-compatible. Do not test against a real GGUF in the deterministic suite.

- [ ] **Step 5: Make VectorIndex path-selectable without changing legacy callers.**

  Change its initializer to:

      def __init__(self, config: Config, *, path: Path | None = None)

  Store the resolved path on the instance and make initialize, save, and dirty-marker handling use it. Existing callers that pass only config must continue to use config.vector_path unchanged. A future Context Core caller will pass config.context_vector_path.

  Add or extend tests/test_vector_index.py if required to assert that a custom path gets its own .dirty marker and does not alter config.vector_path.

- [ ] **Step 6: Remove duplicated installer defaults and surface diagnostics safely.**

  In install.sh, obtain the default filename and URL by running a small local Python import of evolvmem.runtime_contract with PYTHONPATH pointed at PLUGIN_DIR. Use the same Config.save path to create a fresh config.json rather than embedding a second JSON copy in shell. Preserve the rule that an existing config is not overwritten.

  In mcp_server.py:

  - Keep startup fail-open: a failed embedding engine still permits FTS operation.
  - Add a bounded embedding_diagnostics array to memory_status, populated from Config.validate_runtime(require_model=True).
  - Do not log raw content, credentials, or a full configuration file.

  Update README.md Quick Start, Data Directory, Configuration, and architecture language so it describes the Nomic contract and distinguishes the existing retrieval layers from the upcoming Context Core layers. Include a concise migration note for users with the prior BGE/512 installer output.

- [ ] **Step 7: Verify and commit Task 1.**

  Run:

      /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_runtime_contract.py tests/test_embedding.py tests/test_vector_index.py tests/test_integration.py
      git diff --check

  Inspect the diff for accidental changes to existing user config files. Commit only the task files:

      git add evolvmem/runtime_contract.py evolvmem/config.py evolvmem/embedding.py evolvmem/vector_index.py evolvmem/mcp_server.py install.sh README.md tests/test_runtime_contract.py tests/test_vector_index.py
      git commit -m "fix: unify embedding runtime contract"

---

## Task 2: Add typed ContextItem and L0/L1/L2 layer construction

**Files:**

- Create: evolvmem/context_models.py
- Create: evolvmem/context_layers.py
- Create: tests/test_context_models.py
- Create: tests/test_context_layers.py
- Modify: evolvmem/config.py

- [ ] **Step 1: Write failing domain-model and layer tests.**

  Tests must establish all of the following:

  - ContextContentType supports decision, fact, experience, playbook, workflow_policy, constraint, preference, user_profile, reference, and session_summary.
  - ContextStatus supports candidate, active, superseded, archived, and deleted; ContextScope supports global and project; ContextTier supports pinned, normal, and reference; ContextLayer supports l0, l1, and l2.
  - A valid ContextItemDraft has a non-empty normalized identity_key, exactly three non-empty layers, tags normalized to a stable tuple, and numbers constrained to importance 1..10 and confidence 0..1.
  - New user/extractor layers reject content over the three Config limits and reject an L2-only or L0-only object.
  - A deterministic legacy conversion preserves the full source in L2, derives a stable L0 that is within context_l0_max_chars, and keeps L1 within its budget without losing the original L2.
  - Chinese text, whitespace-only input, an exact character-limit boundary, and a one-character overflow each have deterministic results.

  Run:

      /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_models.py tests/test_context_layers.py

  Expected before implementation: module import failures.

- [ ] **Step 2: Implement pure, database-free models.**

  In evolvmem/context_models.py, use Python 3.10-compatible str, Enum classes rather than StrEnum. Define:

      class ContextContentType(str, Enum)
      class ContextStatus(str, Enum)
      class ContextScope(str, Enum)
      class ContextTier(str, Enum)
      class ContextLayer(str, Enum)

      @dataclass(frozen=True, slots=True)
      class ContextLayers:
          l0: str
          l1: str
          l2: str
          generator: str

      @dataclass(frozen=True, slots=True)
      class ContextItemDraft:
          identity_key: str
          content_type: ContextContentType
          layers: ContextLayers
          project: str = ""
          scope: ContextScope = ContextScope.PROJECT
          status: ContextStatus = ContextStatus.CANDIDATE
          tier: ContextTier = ContextTier.NORMAL
          tags: tuple[str, ...] = ()
          importance: float = 5.0
          confidence: float = 0.5
          expires_at: str | None = None
          supersedes: int | None = None

      @dataclass(frozen=True, slots=True)
      class ContextItem:
          id: int
          identity_key: str
          content_type: ContextContentType
          project: str
          scope: ContextScope
          status: ContextStatus
          tier: ContextTier
          tags: tuple[str, ...]
          importance: float
          confidence: float
          source_state: str
          source_count: int
          success_count: int
          failure_count: int
          access_count: int
          last_accessed: str | None
          last_verified_at: str | None
          expires_at: str | None
          supersedes: int | None
          superseded_by: int | None
          created_at: str
          updated_at: str
          layers: ContextLayers | None

      @dataclass(frozen=True, slots=True)
      class ContextSearchHit:
          item_id: int
          score: float
          match_layers: tuple[ContextLayer, ...]
          content_type: ContextContentType
          project: str
          status: ContextStatus

      @dataclass(frozen=True, slots=True)
      class ContextVectorDocument:
          item_id: int
          l0: str

  ContextItem contains all persisted counters/timestamps/source_state plus ContextLayers when loaded with layers. Use a dedicated ContextValidationError(ValueError) for input errors. Keep conversion from SQLite rows in ContextStore, not in these classes.

- [ ] **Step 3: Implement deterministic layer normalization and validation.**

  In evolvmem/context_layers.py, expose:

      def normalize_content(value: str) -> str
      def validate_layers(layers: ContextLayers, config: Config, *, allow_legacy_overflow: bool = False) -> None
      def layers_from_legacy_value(value: str, *, content_type: ContextContentType, config: Config) -> ContextLayers

  normalize_content must normalize CRLF to LF, strip only leading/trailing outer whitespace, and preserve meaningful interior newlines. L0 derivation must be deterministic: prefer the first meaningful sentence/line, prepend a lightweight type cue only if it fits, and append an ellipsis when truncating. It must never call an LLM or rely on locale-specific randomness.

  For ordinary new content, validate all maximums. For migration only, allow L2 to exceed context_l2_max_chars so the original survives. L1 remains deterministic and bounded even in that case. generator is migrated for legacy conversion; it is caller-owned for user/extractor content.

- [ ] **Step 4: Verify and commit Task 2.**

  Run:

      /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_models.py tests/test_context_layers.py
      git diff --check

  Commit:

      git add evolvmem/context_models.py evolvmem/context_layers.py evolvmem/config.py tests/test_context_models.py tests/test_context_layers.py
      git commit -m "feat: add layered context models"

---

## Task 3: Build ContextStore schema, atomic CRUD, and L0/L1 FTS

**Files:**

- Create: evolvmem/context_store.py
- Create: tests/test_context_store.py
- Modify: evolvmem/context_models.py only if row model fields discovered necessary

- [ ] **Step 1: Write failing storage tests against a temporary Config database.**

  Cover:

  - initialize is idempotent and enables WAL plus foreign keys.
  - context_items, context_layers, context_sources, context_evidence, session_archives, and legacy_memory_migrations are created with the fields specified by the design.
  - create_item inserts one item and exactly L0/L1/L2 atomically; an invalid layer or a deliberately raised insert error leaves neither an item nor any layer.
  - get_item returns the complete typed item and layers; get_item(item_id, include_layers=False) avoids loading text layers.
  - only one active item may exist for the same identity_key, project, scope; candidates may coexist.
  - supersede_active changes the old item to superseded and creates the successor in one transaction, with both directional links set.
  - L0 and L1 text are found through search_fts; L2-only text is not found through search_fts.
  - CJK query behavior follows the existing trigram-then-LIKE fallback rule when trigram support is available or absent.
  - list_vector_documents returns only active, unexpired item IDs and their L0 contents; it excludes candidate, archived, deleted, and expired items.

  Run:

      /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_store.py

  Expected before implementation: import failure.

- [ ] **Step 2: Implement the schema as a new independent store.**

  ContextStore must mirror MemoryStore lifecycle conventions: initialize, close, context manager support, a private sqlite3.Connection, and a nested transaction context manager using BEGIN IMMEDIATE only at the outermost level.

  Create these tables and constraints:

      context_items
        id INTEGER PRIMARY KEY AUTOINCREMENT
        identity_key TEXT NOT NULL
        content_type TEXT NOT NULL
        project TEXT NOT NULL DEFAULT ''
        scope TEXT NOT NULL DEFAULT 'project'
        status TEXT NOT NULL DEFAULT 'candidate'
        tier TEXT NOT NULL DEFAULT 'normal'
        tags TEXT NOT NULL DEFAULT ''
        importance REAL NOT NULL DEFAULT 5.0
        confidence REAL NOT NULL DEFAULT 0.5
        source_state TEXT NOT NULL DEFAULT 'none'
        source_count INTEGER NOT NULL DEFAULT 0
        success_count INTEGER NOT NULL DEFAULT 0
        failure_count INTEGER NOT NULL DEFAULT 0
        access_count INTEGER NOT NULL DEFAULT 0
        last_accessed TEXT
        last_verified_at TEXT
        expires_at TEXT
        supersedes INTEGER REFERENCES context_items(id)
        superseded_by INTEGER REFERENCES context_items(id)
        created_at TEXT NOT NULL
        updated_at TEXT NOT NULL

      context_layers
        id INTEGER PRIMARY KEY AUTOINCREMENT
        item_id INTEGER NOT NULL REFERENCES context_items(id) ON DELETE CASCADE
        layer TEXT NOT NULL
        content TEXT NOT NULL
        content_hash TEXT NOT NULL
        generator TEXT NOT NULL
        created_at TEXT NOT NULL
        updated_at TEXT NOT NULL
        UNIQUE(item_id, layer)

      context_sources
        id INTEGER PRIMARY KEY AUTOINCREMENT
        item_id INTEGER NOT NULL REFERENCES context_items(id) ON DELETE CASCADE
        archive_id INTEGER REFERENCES session_archives(id)
        source_kind TEXT NOT NULL
        source_ref TEXT NOT NULL DEFAULT ''
        extraction_version TEXT NOT NULL
        created_at TEXT NOT NULL
        UNIQUE(item_id, source_kind, source_ref)

      context_evidence
        id INTEGER PRIMARY KEY AUTOINCREMENT
        item_id INTEGER NOT NULL REFERENCES context_items(id) ON DELETE CASCADE
        source_id INTEGER REFERENCES context_sources(id)
        outcome TEXT NOT NULL
        note TEXT NOT NULL DEFAULT ''
        observed_at TEXT NOT NULL
        created_at TEXT NOT NULL

      session_archives
        id INTEGER PRIMARY KEY AUTOINCREMENT
        project TEXT NOT NULL
        adapter TEXT NOT NULL
        external_session_id TEXT NOT NULL
        payload_path TEXT NOT NULL
        payload_sha256 TEXT NOT NULL
        state TEXT NOT NULL DEFAULT 'available'
        expires_at TEXT NOT NULL
        purged_at TEXT
        created_at TEXT NOT NULL
        UNIQUE(adapter, external_session_id)

      legacy_memory_migrations
        legacy_memory_id INTEGER PRIMARY KEY
        context_item_id INTEGER NOT NULL REFERENCES context_items(id)
        migrated_at TEXT NOT NULL

  session_archives, context_sources, and context_evidence are schema-only in this phase; do not implement archive payload handling or promotion behavior yet.

  Add indexes for common item filters and a partial unique index:

      CREATE UNIQUE INDEX idx_context_items_one_active_identity ON context_items(identity_key, project, scope)
      WHERE status = 'active'

  Use SHA-256 of normalized layer text for content_hash.

- [ ] **Step 3: Implement FTS/trigram indices with no L2 leakage.**

  Build context_layers_fts and, when SQLite supports it, context_layers_fts_trigram. They must contain only context_layers rows whose layer is l0 or l1. Use insert/update/delete triggers with WHEN clauses so direct ContextStore writes cannot leave an out-of-sync lexical index.

  Implement:

      def search_fts(
          self,
          query: str,
          *,
          top_k: int = 20,
          statuses: tuple[ContextStatus, ...] | None = None,
      ) -> list[ContextSearchHit]

  Sanitize FTS query syntax the same way the legacy store does. For CJK text, supplement FTS results with a LIKE query and deduplicate by item ID while preserving the best score. The result must identify matched layers so Phase 2 can render L0/L1 differently.

- [ ] **Step 4: Implement the minimal public ContextStore API.**

  Use these exact methods:

      def create_item(self, draft: ContextItemDraft) -> ContextItem
      def get_item(self, item_id: int, *, include_layers: bool = True) -> ContextItem | None
      def get_by_identity(
          self, identity_key: str, *, project: str = "", scope: ContextScope = ContextScope.PROJECT
      ) -> list[ContextItem]
      def supersede_active(self, draft: ContextItemDraft) -> ContextItem
      def update_access(self, item_ids: list[int]) -> None
      def list_vector_documents(self) -> list[ContextVectorDocument]
      def count_by_status(self) -> dict[str, int]

  supersede_active must first update the old active row to superseded, then insert the new active row in the same outer transaction; this ordering satisfies the partial unique index without creating an observable gap. If no active predecessor exists, create the supplied draft normally. Do not add a generic raw SQL escape hatch.

- [ ] **Step 5: Verify and commit Task 3.**

  Run:

      /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_store.py
      /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_memory_store.py tests/test_retriever.py
      git diff --check

  Commit:

      git add evolvmem/context_store.py evolvmem/context_models.py tests/test_context_store.py
      git commit -m "feat: add context core sqlite store"

---

## Task 4: Migrate legacy memories idempotently and without destructive fallback

**Files:**

- Create: evolvmem/context_migration.py
- Create: tests/test_context_migration.py
- Modify: evolvmem/context_store.py
- Modify: evolvmem/context_models.py if migration report types belong there

- [ ] **Step 1: Write failing migration tests using real legacy MemoryStore data.**

  Create legacy rows through MemoryStore in a temporary data directory, then use direct safe test updates only to construct historical statuses/links that public methods cannot create. Test:

  - Empty/new database: migration reports no legacy table or zero rows and succeeds.
  - Active, superseded, archived, and deleted legacy rows become ContextItems with correct types, tier, importance, tags, expiry, access statistics, timestamps, and directional supersede links.
  - Legacy attribute mapping: constraint, preference, user_profile, decision map directly; fact with :progress:log: in its key becomes session_summary; other fact becomes fact; unknown attribute becomes reference.
  - source_session appears in a context_sources row with source_kind=migration and source_ref equal to the old value.
  - L0/L1/L2 behavior follows Task 2, including a deliberately oversized legacy value whose exact original is retained in L2.
  - A second migration run creates zero new ContextItems and reports already_migrated rows.
  - A transaction failure during migration rolls back both the newly inserted ContextItems and their legacy_memory_migrations mappings.
  - Two legacy active rows with the same key preserve both values; the deterministically newest row remains active and the other becomes candidate, with duplicate_active_count in the report.
  - The migration itself does not alter, delete, or archive any row in memories.

  Run:

      /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_migration.py

  Expected before implementation: module import failure.

- [ ] **Step 2: Implement a transactionally idempotent migrator.**

  In evolvmem/context_migration.py define:

      @dataclass(frozen=True, slots=True)
      class LegacyMigrationReport:
          legacy_table_found: bool
          scanned: int
          created: int
          already_migrated: int
          duplicate_active_count: int

      class LegacyMemoryMigrator:
          def __init__(self, store: ContextStore, config: Config)
          def migrate(self) -> LegacyMigrationReport

  The migrator must read legacy rows through ContextStore's same SQLite connection, not a second MemoryStore connection. This permits a single transaction to create the ContextItem, its layers, its migration source, and its mapping row.

  It must tolerate an older legacy table that lacks importance, tier, or expires_at by inspecting PRAGMA table_info and using deterministic defaults; it must not assume a fresh MemoryStore initialization has already upgraded the old schema.

  Mapping rules:

  - identity_key is the legacy key after whitespace normalization.
  - project is empty for this one-time migration. scope is global for constraint/preference/user_profile and project for all other types.
  - importance/tier/status preserve known valid values; invalid historical values become normal / archived conservatively.
  - confidence is 1.0 for migrated active/pinned durable records and 0.5 for other migrated records.
  - source_state is none. A source row records source_kind=migration, source_ref=legacy source_session, and extraction_version=legacy-v1.
  - The first pass creates/mappings all rows. The second pass maps legacy supersedes and superseded_by IDs to ContextItem IDs.
  - Resolve duplicate active normalized identities before insertion by descending updated_at then descending legacy id. The winner keeps active; each other duplicate is inserted candidate with extraction_version=legacy-v1:duplicate-active.

  Never invoke an LLM, embedding engine, or vector index during migration.

- [ ] **Step 3: Add only migration-supporting store primitives.**

  Add narrow ContextStore methods needed by the migrator rather than exposing its connection publicly:

      def legacy_memory_table_exists(self) -> bool
      def iter_unmigrated_legacy_rows(self) -> list[dict]
      def record_migration_source(
          self, item_id: int, *, source_ref: str, extraction_version: str
      ) -> int
      def record_legacy_mapping(self, legacy_memory_id: int, context_item_id: int) -> None
      def resolve_legacy_mapping(self, legacy_memory_id: int) -> int | None
      def set_supersession_links(self, item_id: int, *, supersedes: int | None, superseded_by: int | None) -> None

  These methods must require the caller to be inside the ContextStore transaction where appropriate. Keep them explicitly legacy-named and do not let future adapters depend on them.

- [ ] **Step 4: Verify and commit Task 4.**

  Run:

      /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_migration.py tests/test_context_store.py tests/test_memory_store.py
      git diff --check

  Commit:

      git add evolvmem/context_migration.py evolvmem/context_store.py evolvmem/context_models.py tests/test_context_migration.py
      git commit -m "feat: migrate legacy memories into context core"

---

## Task 5: Rebuild a separate L0 context vector cache from SQLite truth

**Files:**

- Create: evolvmem/context_vector_sync.py
- Create: tests/test_context_vector_sync.py
- Modify: evolvmem/vector_index.py if Task 1 did not finish path isolation
- Modify: evolvmem/context_store.py

- [ ] **Step 1: Write failing vector synchronization tests with fake embeddings.**

  Test:

  - Only active, unexpired ContextItems contribute their L0 content and IDs to the Context Core vector index.
  - The synchronizer calls encode_document rather than encode/query on each L0.
  - It initializes/rebuilds at config.embedding_dim using config.context_vector_path and never writes config.vector_path.
  - A successful rebuild saves a matching vector count and clears the context dirty marker.
  - An encoding/rebuild failure leaves the context dirty marker present and does not remove or alter durable ContextItems.
  - An unavailable embedding engine returns an explicit unavailable report and leaves FTS usable; it is not treated as a successful synchronized index.
  - An empty active set produces a valid empty context index.

  Run:

      /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_vector_sync.py

  Expected before implementation: module import failure.

- [ ] **Step 2: Implement an infrastructure-only synchronizer.**

  In evolvmem/context_vector_sync.py define:

      @dataclass(frozen=True, slots=True)
      class ContextVectorSyncReport:
          status: str
          document_count: int
          detail: str = ""

      class ContextVectorSynchronizer:
          def __init__(
              self,
              config: Config,
              store: ContextStore,
              vector_index: VectorIndex,
              embedding_engine: EmbeddingEngine | None,
          )

          def rebuild_active_l0(self) -> ContextVectorSyncReport

  status must be one of synchronized, unavailable, or failed. The method must not catch and hide a failed vector rebuild from its report. It may convert an exception to status=failed after VectorIndex's existing dirty-marker behavior has run, but detail must contain only exception class/name-level diagnostics, never ContextItem content.

  list_vector_documents is the sole source of records. Convert returned lists to np.float32, verify dimensionality before rebuilding, and call VectorIndex.rebuild once. Do not increment ContextItem access counters during reindexing.

- [ ] **Step 3: Verify isolation and compatibility.**

  Add a regression test that creates a legacy vector index at config.vector_path, runs the Context Core synchronizer, and asserts both vector files remain independently readable. This guards the most dangerous upgrade failure: a ContextItem ID overwriting the legacy MemoryStore cache.

- [ ] **Step 4: Verify and commit Task 5.**

  Run:

      /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_vector_sync.py tests/test_vector_index.py tests/test_context_store.py
      git diff --check

  Commit:

      git add evolvmem/context_vector_sync.py evolvmem/context_store.py evolvmem/vector_index.py tests/test_context_vector_sync.py tests/test_vector_index.py
      git commit -m "feat: rebuild context l0 vector cache"

---

## Task 6: Expose a narrow bootstrap boundary, document upgrade behavior, and run the full regression suite

**Files:**

- Create: evolvmem/context_core.py
- Create: tests/test_context_core.py
- Modify: evolvmem/__init__.py
- Modify: README.md
- Create: /home/jiangli/fix-records/records/2026-08-17-evolvmem-context-core-foundation.md after verification, following the repository-required record format

- [ ] **Step 1: Write a failing end-to-end foundation test.**

  In tests/test_context_core.py:

  - Create legacy rows with MemoryStore.
  - Instantiate ContextCore with the same Config.
  - Call initialize(migrate_legacy=True).
  - Assert the returned bootstrap report includes an idempotent migration report, a usable ContextStore, and no mandatory model load.
  - Call initialize a second time and assert zero duplicate ContextItems.
  - Confirm legacy MemoryStore retrieval still returns the original values unchanged.

  Run:

      /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_context_core.py

  Expected before implementation: module import failure.

- [ ] **Step 2: Implement the stable foundation entry point.**

  In evolvmem/context_core.py define:

      @dataclass(frozen=True, slots=True)
      class ContextCoreBootstrapReport:
          migration: LegacyMigrationReport

      class ContextCore:
          def __init__(self, config: Config)
          def initialize(self, *, migrate_legacy: bool = True) -> ContextCoreBootstrapReport
          def close(self) -> None

  initialize opens ContextStore and, by default, runs LegacyMemoryMigrator. It must not instantiate an EmbeddingEngine or run a vector rebuild automatically: those require a real model and remain an explicit operation. Provide context-manager support. Export ContextCore from evolvmem/__init__.py.

  Do not yet wire ContextCore into hooks, kimi_hooks, dsh_bridge, web_server, or MCP tool routing. Existing adapters remain solely on MemoryStore until the Phase 2/4 compatibility plan proves layered recall and rendering.

- [ ] **Step 3: Document the precise upgrade state.**

  Update README.md with a short Context Core 2.0 Foundation section:

  - The new tables and migration are available now, while existing memory_* behavior remains the active compatibility path.
  - Explain L0/L1/L2 as representations of one ContextItem, not the old active/history/vector labels.
  - State that a later release will enable layered injection and candidate/promotion behavior.
  - Give a local, safe migration command through a small __main__ entry or documented Python invocation. It must not delete legacy data or start network/model downloads.
  - Mention context_vectors.usearch is separate from vectors.usearch during the transition.

- [ ] **Step 4: Record verification truthfully and run the full suite.**

  First run focused tests:

      /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q tests/test_runtime_contract.py tests/test_context_models.py tests/test_context_layers.py tests/test_context_store.py tests/test_context_migration.py tests/test_context_vector_sync.py tests/test_context_core.py

  Then run:

      /home/jiangli/hermes-memory-plugin/.venv/bin/pytest -q
      git diff --check
      git status --short

  If any test fails, use systematic debugging before changing code. Do not write the record as fixed until the relevant verification has passed. After results are known, read /home/jiangli/fix-records/README.md and create the required record with symptom, investigation, root cause, fix content, verification, and remaining issues. Include that raw archive/candidate/promotion/injection are intentionally not implemented in this foundation.

- [ ] **Step 5: Commit Task 6.**

  Commit only after the full-suite outcome and record are accurate:

      git add evolvmem/context_core.py evolvmem/__init__.py README.md tests/test_context_core.py
      git commit -m "feat: bootstrap evolvmem context core"

  The required fix record is outside the EvolvMem worktree. Commit it in its own repository only if it is already a Git repository and the user has not asked to keep documentation uncommitted. Never add an absolute external path to the EvolvMem repository index.

## Foundation acceptance checklist

- [ ] A clean fresh configuration, installer, README, and actual Nomic default use one 768-dimensional runtime contract.
- [ ] A misconfigured old BGE/512 installation remains untouched and reports a specific non-sensitive diagnostic instead of quietly producing a wrong vector index.
- [ ] ContextItems persist L0/L1/L2 as one entity, and L2 is absent from default FTS/trigram indexing.
- [ ] SQLite is the only content source of truth. Context HNSW is an independently rebuildable cache in context_vectors.usearch.
- [ ] Legacy migration is idempotent, transactional, preserves values/metadata/history, records sources, and retains duplicate active legacy rows as reviewable candidates.
- [ ] Existing MemoryStore/MCP/hook tests still pass and operate against the unchanged legacy compatibility path.
- [ ] The full automated suite passes, or any environmental skips/failures are documented accurately in the required fix record.

## Deferred plans and gate to proceed

After this foundation is green and reviewed, write separate implementation plans for:

1. Phase 2: ContextRetriever, L1 injection budgets, context_search/context_read, and old memory_* compatibility facade.
2. Phase 3: encrypted 30-day SessionArchive, candidate isolation, evidence, confirmation, and Experience-to-Playbook promotion.
3. Phase 4: Claude/Kimi/DSH/MCP adapters through ContextService, observability, and optional platform preflight.

Do not begin those phases merely because their schema tables already exist. Their APIs must be designed against the verified ContextStore result of this plan.

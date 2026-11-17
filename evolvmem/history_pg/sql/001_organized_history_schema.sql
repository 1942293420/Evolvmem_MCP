-- Organized history, management trash/audit, coverage reports, and board sync tables.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE SCHEMA IF NOT EXISTS em_history;

CREATE TABLE IF NOT EXISTS em_history.schema_migrations (
    version integer PRIMARY KEY,
    migration_name text NOT NULL UNIQUE,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS em_history.projects (
    project text PRIMARY KEY,
    display_name text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT 'active',
    revision bigint NOT NULL DEFAULT 1,
    created_at text NOT NULL DEFAULT '',
    updated_at text NOT NULL DEFAULT '',
    block_count integer NOT NULL DEFAULT 0,
    message_count integer NOT NULL DEFAULT 0,
    turn_count integer NOT NULL DEFAULT 0,
    attachment_count integer NOT NULL DEFAULT 0,
    first_at text NOT NULL DEFAULT '',
    last_at text NOT NULL DEFAULT '',
    summary text NOT NULL DEFAULT '',
    summary_purpose text NOT NULL DEFAULT '',
    summary_source text NOT NULL DEFAULT '',
    environments text NOT NULL DEFAULT '[]',
    evidence_status text NOT NULL DEFAULT '',
    intro_json text NOT NULL DEFAULT '{}',
    embedding vector(768)
);

CREATE TABLE IF NOT EXISTS em_history.blocks (
    block_id text PRIMARY KEY,
    adapter text NOT NULL DEFAULT '',
    kind text NOT NULL DEFAULT '',
    family_key text NOT NULL DEFAULT '',
    native_session_id text NOT NULL DEFAULT '',
    native_device_id text NOT NULL DEFAULT '',
    native_id_evidence text NOT NULL DEFAULT '',
    title text NOT NULL DEFAULT '',
    message_count integer NOT NULL DEFAULT 0,
    turn_count integer NOT NULL DEFAULT 0,
    attachment_recovered integer NOT NULL DEFAULT 0,
    attachment_missing integer NOT NULL DEFAULT 0,
    observed_first_at text NOT NULL DEFAULT '',
    observed_last_at text NOT NULL DEFAULT '',
    ended_at text NOT NULL DEFAULT '',
    coverage_state text NOT NULL DEFAULT '',
    coverage_json text NOT NULL DEFAULT '{}',
    source_archive_ids text NOT NULL DEFAULT '[]',
    failed_archive_ids text NOT NULL DEFAULT '[]',
    projects text NOT NULL DEFAULT '[]',
    block_json text NOT NULL DEFAULT '{}',
    block_sha256 text NOT NULL DEFAULT '',
    device_id text NOT NULL DEFAULT '',
    search_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(projects, ''))
    ) STORED,
    embedding vector(768)
);

CREATE TABLE IF NOT EXISTS em_history.block_projects (
    block_id text NOT NULL REFERENCES em_history.blocks(block_id) ON DELETE CASCADE,
    project text NOT NULL,
    evidence text NOT NULL DEFAULT '',
    PRIMARY KEY (block_id, project)
);

CREATE TABLE IF NOT EXISTS em_history.messages (
    block_id text NOT NULL,
    ordinal integer NOT NULL,
    role text NOT NULL DEFAULT '',
    content text NOT NULL DEFAULT '',
    recorded_at text NOT NULL DEFAULT '',
    archive_id bigint,
    message_index integer,
    attachments text NOT NULL DEFAULT '[]',
    search_tsv tsvector GENERATED ALWAYS AS (
        to_tsvector('simple', coalesce(content, ''))
    ) STORED,
    embedding vector(768),
    PRIMARY KEY (block_id, ordinal)
);

CREATE TABLE IF NOT EXISTS em_history.attachments (
    sha256 text PRIMARY KEY,
    mime_type text NOT NULL DEFAULT '',
    bytes bigint NOT NULL DEFAULT 0,
    path text NOT NULL DEFAULT '',
    format_ok integer NOT NULL DEFAULT 0,
    width integer,
    height integer,
    occurrences integer NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS em_history.sources (
    archive_id bigint PRIMARY KEY,
    family_id text NOT NULL DEFAULT '',
    block_id text NOT NULL DEFAULT '',
    adapter text NOT NULL DEFAULT '',
    kind text NOT NULL DEFAULT '',
    external_session_id text NOT NULL DEFAULT '',
    native_session_id text NOT NULL DEFAULT '',
    native_device_id text NOT NULL DEFAULT '',
    selection_status text NOT NULL DEFAULT '',
    state text NOT NULL DEFAULT '',
    status text NOT NULL DEFAULT '',
    detail text NOT NULL DEFAULT '',
    logical_bytes bigint NOT NULL DEFAULT 0,
    manifest_bytes bigint NOT NULL DEFAULT 0,
    manifest_sha256_verified integer NOT NULL DEFAULT 0,
    content_sha256_verified integer NOT NULL DEFAULT 0,
    native_hash_verified integer NOT NULL DEFAULT 0,
    messages integer NOT NULL DEFAULT 0,
    attachments_recovered integer NOT NULL DEFAULT 0,
    attachments_missing integer NOT NULL DEFAULT 0,
    retained_range text NOT NULL DEFAULT '',
    elapsed_seconds double precision NOT NULL DEFAULT 0.0
);

CREATE TABLE IF NOT EXISTS em_history.recovery_sources (
    block_id text NOT NULL,
    project text NOT NULL,
    adapter text NOT NULL DEFAULT '',
    kind text NOT NULL DEFAULT '',
    session_id text NOT NULL DEFAULT '',
    cwd text NOT NULL DEFAULT '',
    source_path text NOT NULL,
    source_sha256 text NOT NULL DEFAULT '',
    line_start integer,
    line_end integer,
    archive_ids text NOT NULL DEFAULT '[]',
    message_count integer NOT NULL DEFAULT 0,
    user_messages integer NOT NULL DEFAULT 0,
    assistant_messages integer NOT NULL DEFAULT 0,
    completeness text NOT NULL DEFAULT '',
    note text NOT NULL DEFAULT '',
    extension_of text NOT NULL DEFAULT '',
    source_message_range text NOT NULL DEFAULT '',
    PRIMARY KEY (block_id, project, source_path)
);

CREATE TABLE IF NOT EXISTS em_history.recovery_reconciliation (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    project text NOT NULL DEFAULT '',
    candidate text NOT NULL DEFAULT '',
    action text NOT NULL DEFAULT '',
    base_block_id text NOT NULL DEFAULT '',
    new_block_id text NOT NULL DEFAULT '',
    covered_messages integer NOT NULL DEFAULT 0,
    note text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS em_history.candidate_meta (
    key text PRIMARY KEY,
    value text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS em_history.exceptions (
    id bigint PRIMARY KEY,
    archive_id bigint,
    family_id text NOT NULL DEFAULT '',
    classification text NOT NULL DEFAULT '',
    detail text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS em_history.management_trash (
    kind text NOT NULL,
    project text NOT NULL,
    key text NOT NULL,
    title text NOT NULL DEFAULT '',
    deleted_at text NOT NULL DEFAULT '',
    PRIMARY KEY (kind, project, key)
);

CREATE TABLE IF NOT EXISTS em_history.management_operations (
    request_id text PRIMARY KEY,
    fingerprint text NOT NULL DEFAULT '',
    result text NOT NULL DEFAULT '',
    created_at text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS em_history.history_management_meta (
    id integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    version integer NOT NULL DEFAULT 1,
    scope text NOT NULL DEFAULT 'personal'
);

CREATE TABLE IF NOT EXISTS em_history.project_board_sync_state (
    project text NOT NULL,
    workspace_fingerprint text NOT NULL,
    workstream_id text NOT NULL,
    checkpoint_revision bigint NOT NULL DEFAULT 0,
    state_version bigint NOT NULL DEFAULT 0,
    payload_json text NOT NULL DEFAULT '{}',
    content_hash text NOT NULL DEFAULT '',
    pending integer NOT NULL DEFAULT 1,
    target_record_id text NOT NULL DEFAULT '',
    binding_id text NOT NULL DEFAULT '',
    sent_hash text NOT NULL DEFAULT '',
    last_status text NOT NULL DEFAULT 'pending',
    updated_at text NOT NULL DEFAULT '',
    PRIMARY KEY (project, workspace_fingerprint, workstream_id)
);

CREATE TABLE IF NOT EXISTS em_history.coverage_reports (
    report_key text PRIMARY KEY,
    payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_em_hist_blocks_tsv ON em_history.blocks USING gin (search_tsv);
CREATE INDEX IF NOT EXISTS idx_em_hist_blocks_title_trgm ON em_history.blocks USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_em_hist_blocks_hnsw ON em_history.blocks USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64) WHERE embedding IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_em_hist_msgs_tsv ON em_history.messages USING gin (search_tsv);
CREATE INDEX IF NOT EXISTS idx_em_hist_msgs_trgm ON em_history.messages USING gin (content gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_em_hist_msgs_archive ON em_history.messages (archive_id);
CREATE INDEX IF NOT EXISTS idx_em_hist_msgs_hnsw ON em_history.messages USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64) WHERE embedding IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_em_hist_bp_project ON em_history.block_projects (project);
CREATE INDEX IF NOT EXISTS idx_em_hist_proj_hnsw ON em_history.projects USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64) WHERE embedding IS NOT NULL;

INSERT INTO em_history.schema_migrations (version, migration_name)
VALUES (1, '001_organized_history_schema')
ON CONFLICT (version) DO NOTHING;

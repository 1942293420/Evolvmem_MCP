-- EvolvMem Full-Chain Interception & Gate Metadata Schema (Append-Only Migration 005)

ALTER TABLE proposal_decisions
  ADD COLUMN IF NOT EXISTS meta_json JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS pipeline_interceptions (
  interception_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  track TEXT NOT NULL CHECK (track IN ('live', 'backfill')),
  session_id TEXT NOT NULL DEFAULT '',
  source_ref TEXT NOT NULL DEFAULT '',
  segment_hash TEXT NOT NULL DEFAULT '',
  moment_id TEXT NOT NULL DEFAULT '',
  moment_kind TEXT NOT NULL DEFAULT '',
  moment_summary TEXT NOT NULL DEFAULT '',
  intercept_layer TEXT NOT NULL CHECK (intercept_layer IN ('Moments', 'Extract', 'Quota')),
  reason_code TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  agent_client TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  candidate_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  llm_call_id BIGINT,
  tokens_used INT NOT NULL DEFAULT 0 CHECK (tokens_used >= 0),
  rescued_proposal_uid TEXT,
  rescued_note_id TEXT,
  rescued_by TEXT,
  rescued_at TIMESTAMPTZ,
  is_shadow BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_interceptions_owner_time
  ON pipeline_interceptions (owner_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_pipeline_interceptions_layer_reason
  ON pipeline_interceptions (owner_id, intercept_layer, reason_code);

CREATE INDEX IF NOT EXISTS idx_pipeline_interceptions_session
  ON pipeline_interceptions (owner_id, track, session_id);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evolvmem_experience_coordinator') THEN
    GRANT SELECT, INSERT, UPDATE ON pipeline_interceptions TO evolvmem_experience_coordinator;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evolvmem_experience_reader') THEN
    GRANT SELECT ON pipeline_interceptions TO evolvmem_experience_reader;
  END IF;
END
$$;

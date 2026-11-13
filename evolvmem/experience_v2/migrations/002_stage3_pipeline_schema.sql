-- EvolvMem Stage 3 Pipeline Schema (Append-Only Migration 002)

CREATE TABLE IF NOT EXISTS llm_calls (
  id BIGSERIAL PRIMARY KEY,
  track TEXT NOT NULL CHECK (track IN ('live', 'backfill')),
  stage TEXT NOT NULL,
  model TEXT NOT NULL,
  session_id TEXT,
  input_tokens INT NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
  output_tokens INT NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
  cost_estimate NUMERIC(12, 6) NOT NULL DEFAULT 0,
  outcome TEXT NOT NULL,
  proposal_uid TEXT,
  is_shadow BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_track_day
  ON llm_calls (track, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llm_calls_session
  ON llm_calls (session_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_llm_calls_stage_day
  ON llm_calls (track, stage, created_at DESC);

CREATE TABLE IF NOT EXISTS pipeline_segment_runs (
  owner_id TEXT NOT NULL,
  track TEXT NOT NULL CHECK (track IN ('live', 'backfill')),
  session_id TEXT NOT NULL,
  segment_hash TEXT NOT NULL,
  pipeline_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'deferred', 'failed', 'skipped_zero_moments')),
  moments_count INT NOT NULL DEFAULT 0,
  proposals_count INT NOT NULL DEFAULT 0,
  tokens_spent INT NOT NULL DEFAULT 0,
  last_event_index INT NOT NULL DEFAULT 0,
  error_code TEXT NOT NULL DEFAULT '',
  is_shadow BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, track, session_id, segment_hash, pipeline_version)
);

CREATE INDEX IF NOT EXISTS idx_pipeline_segment_runs_status
  ON pipeline_segment_runs (owner_id, track, status, updated_at DESC);

CREATE TABLE IF NOT EXISTS pipeline_circuit_state (
  owner_id TEXT NOT NULL,
  track TEXT NOT NULL CHECK (track IN ('live', 'backfill')),
  paused BOOLEAN NOT NULL DEFAULT false,
  pause_reason TEXT NOT NULL DEFAULT '',
  consecutive_bad_days INT NOT NULL DEFAULT 0,
  last_checked_day DATE,
  metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, track)
);

CREATE TABLE IF NOT EXISTS challenger_trials (
  id BIGSERIAL PRIMARY KEY,
  owner_id TEXT NOT NULL,
  challenger_note_id TEXT NOT NULL,
  incumbent_note_id TEXT NOT NULL,
  relation TEXT NOT NULL CHECK (relation IN ('duplicate', 'refines', 'scope_split', 'outdated', 'refuted', 'contested')),
  proposal_uid TEXT,
  required_successes INT NOT NULL DEFAULT 2 CHECK (required_successes >= 1),
  success_count INT NOT NULL DEFAULT 0 CHECK (success_count >= 0),
  failure_count INT NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
  status TEXT NOT NULL DEFAULT 'trial' CHECK (status IN ('trial', 'promoted', 'rolled_back')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_challenger_trials_owner
  ON challenger_trials (owner_id, status, challenger_note_id);

CREATE TABLE IF NOT EXISTS shadow_pilot_runs (
  run_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  track TEXT NOT NULL DEFAULT 'live' CHECK (track IN ('live', 'backfill')),
  pipeline_version TEXT NOT NULL,
  sessions_total INT NOT NULL DEFAULT 0,
  sessions_zero_moments INT NOT NULL DEFAULT 0,
  moments_detected INT NOT NULL DEFAULT 0,
  proposals_generated INT NOT NULL DEFAULT 0,
  proposals_admitted_candidate INT NOT NULL DEFAULT 0,
  tokens_total INT NOT NULL DEFAULT 0,
  llm_calls_total INT NOT NULL DEFAULT 0,
  report_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS shadow_pilot_items (
  id BIGSERIAL PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES shadow_pilot_runs(run_id) ON DELETE CASCADE,
  session_id TEXT NOT NULL,
  moment_kind TEXT NOT NULL,
  stage_outcome TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT '',
  problem TEXT NOT NULL DEFAULT '',
  solution TEXT NOT NULL DEFAULT '',
  reusable_experience TEXT NOT NULL DEFAULT '',
  misbelief TEXT NOT NULL DEFAULT '',
  evidence_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  gate_layer TEXT NOT NULL DEFAULT '',
  gate_verdict TEXT NOT NULL DEFAULT '',
  reason_code TEXT NOT NULL DEFAULT '',
  tokens_used INT NOT NULL DEFAULT 0,
  sample_for_user_review BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_shadow_pilot_items_run
  ON shadow_pilot_items (run_id, sample_for_user_review, id ASC);

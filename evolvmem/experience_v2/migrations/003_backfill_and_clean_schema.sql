-- EvolvMem Backfill Track & Track Calibration Schema (Append-Only Migration 003)

CREATE TABLE IF NOT EXISTS backfill_jobs (
  job_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  scope_kind TEXT NOT NULL CHECK (scope_kind IN ('project', 'month')),
  scope_value TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  session_id TEXT NOT NULL,
  session_created_at TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK (status IN ('queued', 'running', 'completed', 'deferred', 'skipped_zero_moments', 'failed')),
  moments_count INT NOT NULL DEFAULT 0,
  proposals_count INT NOT NULL DEFAULT 0,
  tokens_spent INT NOT NULL DEFAULT 0,
  error_code TEXT NOT NULL DEFAULT '',
  is_shadow BOOLEAN NOT NULL DEFAULT false,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (owner_id, scope_kind, scope_value, source_ref, is_shadow)
);

CREATE INDEX IF NOT EXISTS idx_backfill_jobs_queue
  ON backfill_jobs (owner_id, is_shadow, status, session_created_at DESC, source_ref DESC);

CREATE TABLE IF NOT EXISTS backfill_cursors (
  owner_id TEXT NOT NULL,
  scope_kind TEXT NOT NULL CHECK (scope_kind IN ('project', 'month')),
  scope_value TEXT NOT NULL,
  is_shadow BOOLEAN NOT NULL DEFAULT false,
  last_created_at TEXT NOT NULL DEFAULT '',
  last_source_ref TEXT NOT NULL DEFAULT '',
  processed_count INT NOT NULL DEFAULT 0 CHECK (processed_count >= 0),
  completed BOOLEAN NOT NULL DEFAULT false,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, scope_kind, scope_value, is_shadow)
);

CREATE TABLE IF NOT EXISTS track_calibration_snapshots (
  snapshot_id BIGSERIAL PRIMARY KEY,
  owner_id TEXT NOT NULL,
  source_track TEXT NOT NULL CHECK (source_track IN ('live', 'backfill')),
  category TEXT NOT NULL CHECK (category IN ('前端经验', '业务经验', '系统环境经验')),
  reviewed_total INT NOT NULL DEFAULT 0 CHECK (reviewed_total >= 0),
  agreed_total INT NOT NULL DEFAULT 0 CHECK (agreed_total >= 0),
  agreement_rate REAL NOT NULL DEFAULT 0.0,
  precedents_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  recommended_mode TEXT NOT NULL DEFAULT 'review' CHECK (recommended_mode IN ('review', 'partial', 'auto')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_track_calibration_latest
  ON track_calibration_snapshots (owner_id, source_track, category, created_at DESC);

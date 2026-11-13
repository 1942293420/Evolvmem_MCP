-- EvolvMem Personal Experience v2 Schema (Append-Only Migration 001)
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;

CREATE TABLE IF NOT EXISTS experience_notes (
  id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  category TEXT NOT NULL CHECK (category IN ('前端经验', '业务经验', '系统环境经验')),
  problem TEXT NOT NULL CHECK (btrim(problem) <> '' AND char_length(problem) <= 1000),
  solution TEXT NOT NULL CHECK (btrim(solution) <> '' AND char_length(solution) <= 8000),
  reusable_experience TEXT NOT NULL CHECK (btrim(reusable_experience) <> '' AND char_length(reusable_experience) <= 8000),
  origin TEXT NOT NULL DEFAULT 'manual' CHECK (origin IN ('manual', 'pipeline', 'agent', 'legacy')),
  kind TEXT CHECK (kind IS NULL OR kind IN ('env_fact', 'pitfall', 'procedure', 'preference', 'decision', 'habit')),
  misbelief TEXT NOT NULL DEFAULT '',
  scope JSONB NOT NULL DEFAULT '{}'::jsonb,
  evidence JSONB NOT NULL DEFAULT '[]'::jsonb,
  occurrence_count INT NOT NULL DEFAULT 1 CHECK (occurrence_count >= 1),
  importance INT NOT NULL DEFAULT 5 CHECK (importance BETWEEN 1 AND 10),
  pinned BOOLEAN NOT NULL DEFAULT false,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'challenger', 'dormant', 'contested', 'retired')),
  confidence_a REAL NOT NULL DEFAULT 1.0,
  confidence_b REAL NOT NULL DEFAULT 1.0,
  stability REAL,
  last_used_at TIMESTAMPTZ,
  use_count INT NOT NULL DEFAULT 0 CHECK (use_count >= 0),
  valid_from TIMESTAMPTZ,
  valid_until TIMESTAMPTZ,
  embedding public.vector(768),
  proposal_uid TEXT,
  source_actor TEXT,
  source_device_id TEXT,
  source_agent TEXT,
  source_model TEXT,
  revision INT NOT NULL DEFAULT 1 CHECK (revision >= 1),
  deleted_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_experience_notes_owner_active
  ON experience_notes (owner_id, deleted_at, updated_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_experience_notes_status_model
  ON experience_notes (owner_id, status, source_agent, source_model);

CREATE INDEX IF NOT EXISTS idx_experience_notes_embedding_hnsw
  ON experience_notes USING hnsw (embedding public.vector_cosine_ops);

CREATE TABLE IF NOT EXISTS experience_requests (
  owner_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  payload_sha256 TEXT NOT NULL,
  response_json JSONB NOT NULL,
  http_status INT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (owner_id, request_id)
);

CREATE TABLE IF NOT EXISTS experience_events (
  seq BIGSERIAL PRIMARY KEY,
  event_id TEXT UNIQUE NOT NULL,
  owner_id TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  request_id TEXT NOT NULL,
  action TEXT NOT NULL,
  note_id TEXT NOT NULL,
  before_json JSONB,
  after_json JSONB NOT NULL,
  proposal_uid TEXT,
  agent_client TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  rolled_back_by_seq BIGINT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_experience_events_owner_seq
  ON experience_events (owner_id, seq);

CREATE INDEX IF NOT EXISTS idx_experience_events_note_seq
  ON experience_events (owner_id, note_id, seq DESC);

CREATE INDEX IF NOT EXISTS idx_experience_events_model_window
  ON experience_events (owner_id, agent_client, model, created_at);

CREATE OR REPLACE FUNCTION notify_experience_event()
RETURNS TRIGGER AS $$
BEGIN
  PERFORM pg_notify(
    'experience_changes',
    json_build_object(
      'seq', NEW.seq,
      'owner_id', NEW.owner_id,
      'note_id', NEW.note_id,
      'action', NEW.action,
      'agent_client', NEW.agent_client,
      'model', NEW.model
    )::text
  );
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_experience_events_notify ON experience_events;
CREATE TRIGGER trg_experience_events_notify
  AFTER INSERT ON experience_events
  FOR EACH ROW
  EXECUTE FUNCTION notify_experience_event();

CREATE TABLE IF NOT EXISTS experience_proposals (
  uid TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  op TEXT NOT NULL CHECK (op IN ('add', 'update', 'retire')),
  target_uid TEXT,
  category TEXT NOT NULL CHECK (category IN ('前端经验', '业务经验', '系统环境经验')),
  kind TEXT NOT NULL CHECK (kind IN ('env_fact', 'pitfall', 'procedure', 'preference', 'decision', 'habit')),
  problem TEXT NOT NULL,
  solution TEXT NOT NULL,
  reusable_experience TEXT NOT NULL,
  misbelief TEXT NOT NULL DEFAULT '',
  scope JSONB NOT NULL DEFAULT '{}'::jsonb,
  payload_json JSONB NOT NULL,
  evidence_json JSONB NOT NULL,
  source_kind TEXT NOT NULL CHECK (source_kind IN ('user', 'agent', 'pipeline', 'legacy')),
  source_track TEXT NOT NULL DEFAULT 'live' CHECK (source_track IN ('live', 'backfill')),
  agent_client TEXT NOT NULL,
  model TEXT NOT NULL,
  device_id TEXT NOT NULL DEFAULT '',
  actor_id TEXT NOT NULL,
  session_id TEXT,
  request_id TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending'
    CHECK (state IN ('pending', 'rejected', 'merged', 'review', 'accepted', 'quarantined')),
  importance INT NOT NULL DEFAULT 5 CHECK (importance BETWEEN 1 AND 10),
  pinned BOOLEAN NOT NULL DEFAULT false,
  embedding public.vector(768),
  merged_into_uid TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (owner_id, request_id)
);

CREATE INDEX IF NOT EXISTS idx_experience_proposals_inbox
  ON experience_proposals (owner_id, source_track, state, created_at DESC);

CREATE TABLE IF NOT EXISTS proposal_decisions (
  decision_id TEXT PRIMARY KEY,
  proposal_uid TEXT NOT NULL REFERENCES experience_proposals(uid) ON DELETE CASCADE,
  layer TEXT NOT NULL CHECK (layer IN ('L0', 'L1', 'L2a', 'L2b', 'L3', 'L4')),
  verdict TEXT NOT NULL CHECK (verdict IN ('pass', 'reject', 'merge', 'review')),
  reason_code TEXT,
  confidence REAL,
  judge_model TEXT NOT NULL DEFAULT '',
  detail TEXT NOT NULL DEFAULT '',
  target_note_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_proposal_decisions_uid
  ON proposal_decisions (proposal_uid, created_at ASC);

CREATE TABLE IF NOT EXISTS review_decisions (
  review_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  reviewer_id TEXT NOT NULL,
  proposal_uid TEXT NOT NULL REFERENCES experience_proposals(uid) ON DELETE CASCADE,
  category TEXT NOT NULL,
  source_track TEXT NOT NULL CHECK (source_track IN ('live', 'backfill')),
  gate_verdict TEXT,
  gate_confidence REAL,
  gate_reason TEXT,
  user_verdict TEXT NOT NULL CHECK (user_verdict IN ('accept', 'reject', 'edit_accept', 'rescue')),
  user_reason TEXT,
  note TEXT NOT NULL DEFAULT '',
  edited_payload JSONB,
  decision_id TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_review_decisions_track
  ON review_decisions (owner_id, source_track, category, created_at DESC);

CREATE TABLE IF NOT EXISTS experience_relations (
  relation_id BIGSERIAL PRIMARY KEY,
  owner_id TEXT NOT NULL,
  from_uid TEXT NOT NULL,
  to_uid TEXT NOT NULL,
  relation TEXT NOT NULL CHECK (relation IN ('duplicate', 'refines', 'scope_split', 'outdated', 'refuted', 'contested')),
  reason TEXT NOT NULL DEFAULT '',
  evidence_ref JSONB,
  proposal_uid TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_experience_relations_nodes
  ON experience_relations (owner_id, from_uid, to_uid);

CREATE TABLE IF NOT EXISTS retrieval_impressions (
  id BIGSERIAL PRIMARY KEY,
  owner_id TEXT NOT NULL,
  item_uid TEXT NOT NULL,
  session_id TEXT,
  task_id TEXT,
  channel TEXT NOT NULL,
  rank INT,
  agent_client TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL DEFAULT '',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_retrieval_impressions_owner
  ON retrieval_impressions (owner_id, item_uid, created_at DESC);

-- Stage 4 Tasks 4.3-4.5: Legacy Cleanup Authorization & Backup Registry (Append-Only Migration 004)

CREATE TABLE IF NOT EXISTS legacy_cleanup_registry (
  cleanup_id TEXT PRIMARY KEY,
  owner_id TEXT NOT NULL,
  approved_by TEXT NOT NULL,
  approved_at TIMESTAMPTZ NOT NULL,
  authorization_note TEXT NOT NULL,
  backup_archive_path TEXT NOT NULL,
  backup_sha256 TEXT NOT NULL,
  backup_bytes BIGINT NOT NULL CHECK (backup_bytes >= 0),
  retain_until TIMESTAMPTZ NOT NULL,
  deleted_total INT NOT NULL CHECK (deleted_total >= 0),
  migrated_keep_total INT NOT NULL CHECK (migrated_keep_total >= 0),
  rescued_ids_json JSONB NOT NULL DEFAULT '[]'::jsonb,
  table_dispositions_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  manifest_json JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_legacy_cleanup_registry_owner
  ON legacy_cleanup_registry (owner_id, approved_at DESC);

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evolvmem_experience_coordinator') THEN
    GRANT SELECT, INSERT, UPDATE ON legacy_cleanup_registry TO evolvmem_experience_coordinator;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'evolvmem_experience_reader') THEN
    GRANT SELECT ON legacy_cleanup_registry TO evolvmem_experience_reader;
  END IF;
END
$$;

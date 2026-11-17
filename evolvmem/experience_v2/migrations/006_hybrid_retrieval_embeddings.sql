-- Additive retrieval projection: retain 768-dimensional admission/dedupe vectors.
ALTER TABLE experience_notes
  ADD COLUMN retrieval_embedding public.vector(1024),
  ADD COLUMN retrieval_embedding_contract TEXT,
  ADD COLUMN retrieval_embedding_content_sha256 TEXT,
  ADD COLUMN retrieval_embedding_updated_at TIMESTAMPTZ;

CREATE INDEX idx_experience_notes_retrieval_hnsw ON experience_notes
  USING hnsw (retrieval_embedding public.vector_cosine_ops)
  WHERE retrieval_embedding IS NOT NULL;

-- No activation row is inserted by a schema migration. The operator activates
-- the policy only after verified backfill and compatible writers are deployed.
CREATE TABLE experience_embedding_policy (
  singleton BOOLEAN PRIMARY KEY CHECK (singleton),
  required BOOLEAN NOT NULL,
  retrieval_contract_id TEXT NOT NULL CHECK (length(retrieval_contract_id) > 0),
  legacy_contract_id TEXT NOT NULL CHECK (length(legacy_contract_id) > 0),
  activated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE FUNCTION enforce_experience_embedding_policy() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
DECLARE
  policy RECORD;
  content_sha TEXT;
BEGIN
  IF NEW.status <> 'active' OR NEW.deleted_at IS NOT NULL THEN
    RETURN NEW;
  END IF;
  EXECUTE format(
    'SELECT required,retrieval_contract_id FROM %I.experience_embedding_policy WHERE singleton',
    TG_TABLE_SCHEMA
  ) INTO policy;
  IF policy.required IS DISTINCT FROM true THEN
    RETURN NEW;
  END IF;
  content_sha := encode(sha256(convert_to(
    NEW.category || E'\n' || NEW.problem || E'\n' || NEW.solution || E'\n' || NEW.reusable_experience,
    'UTF8'
  )), 'hex');
  IF NEW.embedding IS NULL OR NEW.retrieval_embedding IS NULL
     OR public.vector_norm(NEW.embedding) <= 0
     OR public.vector_norm(NEW.retrieval_embedding) <= 0
     OR NEW.retrieval_embedding_contract IS DISTINCT FROM policy.retrieval_contract_id
     OR NEW.retrieval_embedding_content_sha256 IS DISTINCT FROM content_sha THEN
    RAISE EXCEPTION 'experience_embedding_projection_required'
      USING ERRCODE = '23514', CONSTRAINT = 'experience_embedding_projection_required';
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER experience_embedding_policy_guard
  BEFORE INSERT OR UPDATE ON experience_notes
  FOR EACH ROW EXECUTE FUNCTION enforce_experience_embedding_policy();

DO $$ BEGIN
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'evolvmem_experience_coordinator') THEN
    GRANT SELECT ON experience_embedding_policy TO evolvmem_experience_coordinator;
  END IF;
  IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'evolvmem_experience_reader') THEN
    GRANT SELECT ON experience_embedding_policy TO evolvmem_experience_reader;
  END IF;
END $$;

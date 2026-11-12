\set ON_ERROR_STOP on
-- Trusted provisioner: owner_role, library_id, model_contract,
-- document_contract_text (canonical JSON), query_prefix. No runtime DDL.
BEGIN;
SELECT pg_catalog.set_config('evolvmem.vector_library', :'library_id', true);
SELECT pg_catalog.set_config('evolvmem.vector_owner', :'owner_role', true);
DO $gate$
BEGIN
 IF current_user<>current_setting('evolvmem.vector_owner')
    OR (SELECT count(*) FROM em_content.business_schema_migrations WHERE version=1)<>1
    OR (SELECT count(*) FROM em_security.library_state WHERE singleton
        AND library_id=current_setting('evolvmem.vector_library')::uuid)<>1
    OR NOT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector' AND extversion='0.8.6') THEN
   RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='vector_provisioning_boundary_required';
 END IF;
END;
$gate$;

CREATE TABLE em_content.vector_schema_migrations(
 version integer PRIMARY KEY CHECK(version=1),library_id uuid NOT NULL,project_id uuid,
 applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE em_content.vector_schema_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.vector_schema_migrations FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_owner ON em_content.vector_schema_migrations TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_version_read ON em_content.vector_schema_migrations FOR SELECT TO PUBLIC
 USING(em_security.allows(library_id,project_id,'read'));
REVOKE ALL ON em_content.vector_schema_migrations FROM PUBLIC;

ALTER TABLE em_content.vector_outbox DROP CONSTRAINT vector_outbox_status_check;
ALTER TABLE em_content.vector_outbox ADD CONSTRAINT vector_outbox_status_check
 CHECK(status IN ('pending','completed','stale'));
ALTER TABLE em_content.vector_outbox ADD COLUMN processed_at timestamptz;
ALTER TABLE em_content.vector_outbox ADD COLUMN result_code text;
ALTER TABLE em_content.vector_outbox ADD CONSTRAINT vector_job_receipt_check CHECK(
 (status='pending' AND processed_at IS NULL AND result_code IS NULL)
 OR (status='completed' AND processed_at IS NOT NULL AND result_code IS NOT NULL AND result_code IN ('upsert','remove'))
 OR (status='stale' AND processed_at IS NOT NULL AND result_code IS NOT NULL
     AND result_code IN ('stale_missing','stale_revision','stale_content','stale_action')));
ALTER TABLE em_content.vector_outbox ADD CONSTRAINT vector_job_revision_hash_check
 CHECK(entity_revision>0 AND content_hash ~ '^[0-9a-f]{64}$');
DO $unique$
DECLARE constraint_name text;
BEGIN
 SELECT c.conname INTO STRICT constraint_name FROM pg_constraint c
 WHERE c.conrelid='em_content.vector_outbox'::regclass AND c.contype='u'
 AND (SELECT array_agg(a.attname::text ORDER BY k.ordinality)
      FROM unnest(c.conkey) WITH ORDINALITY k(attnum,ordinality)
      JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum)
     =ARRAY['library_id','index_kind','entity_id','entity_revision','action'];
 EXECUTE format('ALTER TABLE em_content.vector_outbox DROP CONSTRAINT %I',constraint_name);
END;
$unique$;
ALTER TABLE em_content.vector_outbox ADD CONSTRAINT vector_job_contract_unique
 UNIQUE(library_id,index_kind,entity_id,entity_revision,action,model_contract);
CREATE INDEX vector_pending_contract ON em_content.vector_outbox(library_id,model_contract,id) WHERE status='pending';
CREATE FUNCTION em_content.keep_vector_job_identity() RETURNS trigger LANGUAGE plpgsql
 SET search_path=pg_catalog AS $fn$
BEGIN
 IF ROW(NEW.id,NEW.index_kind,NEW.entity_id,NEW.action,NEW.entity_revision,NEW.content_hash,NEW.model_contract,NEW.created_at)
 IS DISTINCT FROM ROW(OLD.id,OLD.index_kind,OLD.entity_id,OLD.action,OLD.entity_revision,OLD.content_hash,OLD.model_contract,OLD.created_at)
 OR OLD.status<>'pending' THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='immutable_vector_job';
 END IF;
 RETURN NEW;
END;
$fn$;
REVOKE ALL ON FUNCTION em_content.keep_vector_job_identity() FROM PUBLIC;
CREATE TRIGGER immutable_vector_job BEFORE UPDATE ON em_content.vector_outbox
 FOR EACH ROW EXECUTE FUNCTION em_content.keep_vector_job_identity();

CREATE TABLE em_content.vector_models(
 library_id uuid NOT NULL,project_id uuid,model_contract text NOT NULL,
 dimension integer NOT NULL CHECK(dimension=768),document_contract_text text NOT NULL,query_prefix text NOT NULL,
 PRIMARY KEY(library_id,model_contract),
 CHECK(model_contract=encode(sha256(convert_to(document_contract_text,'UTF8')),'hex')),
 CHECK(jsonb_typeof(document_contract_text::jsonb)='array'
   AND jsonb_array_length(document_contract_text::jsonb)=4
   AND (document_contract_text::jsonb->>1)::integer=dimension)
);
ALTER TABLE em_content.vector_models ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.vector_models FORCE ROW LEVEL SECURITY;
CREATE POLICY model_owner ON em_content.vector_models TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_model_read ON em_content.vector_models FOR SELECT TO PUBLIC
 USING(em_security.allows(library_id,project_id,'read'));
REVOKE ALL ON em_content.vector_models FROM PUBLIC;
INSERT INTO em_content.vector_models(library_id,project_id,model_contract,dimension,document_contract_text,query_prefix)
 SELECT library_id,project_id,:'model_contract',768,:'document_contract_text',:'query_prefix'
 FROM em_security.library_state WHERE singleton AND library_id=:'library_id'::uuid;

CREATE TABLE em_content.vector_projections(
 library_id uuid NOT NULL,project_id uuid,index_kind text NOT NULL CHECK(index_kind IN ('legacy','context')),
 entity_id bigint NOT NULL,model_contract text NOT NULL,dimension integer NOT NULL CHECK(dimension=768),
 entity_revision bigint NOT NULL CHECK(entity_revision>0),content_hash text NOT NULL CHECK(content_hash ~ '^[0-9a-f]{64}$'),
 projection_state text NOT NULL CHECK(projection_state IN ('ready','removed')),
 embedding public.vector(768),vector_hash text,updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(library_id,index_kind,entity_id,model_contract),
 FOREIGN KEY(library_id,model_contract) REFERENCES em_content.vector_models(library_id,model_contract),
 CHECK((projection_state='removed' AND embedding IS NULL AND vector_hash IS NULL)
  OR (projection_state='ready' AND embedding IS NOT NULL AND vector_hash IS NOT NULL
      AND vector_hash=encode(sha256(public.vector_send(embedding)),'hex') AND public.vector_norm(embedding)>0))
) PARTITION BY LIST(library_id);
CREATE TABLE em_content.vector_library PARTITION OF em_content.vector_projections
 FOR VALUES IN (:'library_id') PARTITION BY LIST(model_contract);
CREATE TABLE em_content.vector_current PARTITION OF em_content.vector_library
 FOR VALUES IN (:'model_contract') PARTITION BY LIST(index_kind);
CREATE TABLE em_content.vector_legacy PARTITION OF em_content.vector_current FOR VALUES IN ('legacy');
CREATE TABLE em_content.vector_context PARTITION OF em_content.vector_current FOR VALUES IN ('context');
CREATE INDEX vector_legacy_hnsw ON em_content.vector_legacy USING hnsw(embedding public.vector_cosine_ops);
CREATE INDEX vector_context_hnsw ON em_content.vector_context USING hnsw(embedding public.vector_cosine_ops);

ALTER TABLE em_content.vector_projections ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.vector_projections FORCE ROW LEVEL SECURITY;
CREATE POLICY projection_owner ON em_content.vector_projections TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_projection_read ON em_content.vector_projections FOR SELECT TO PUBLIC
 USING(em_security.allows(library_id,project_id,'read'));
CREATE POLICY authorized_projection_insert ON em_content.vector_projections FOR INSERT TO PUBLIC
 WITH CHECK(em_security.allows(library_id,project_id,'write'));
CREATE POLICY authorized_projection_update ON em_content.vector_projections FOR UPDATE TO PUBLIC
 USING(em_security.allows(library_id,project_id,'write')) WITH CHECK(em_security.allows(library_id,project_id,'write'));
CREATE TRIGGER immutable_security_scope BEFORE UPDATE ON em_content.vector_projections
 FOR EACH ROW EXECUTE FUNCTION em_content.keep_security_scope();
CREATE FUNCTION em_content.keep_vector_projection_identity() RETURNS trigger LANGUAGE plpgsql
 SET search_path=pg_catalog AS $fn$
BEGIN
 IF ROW(NEW.index_kind,NEW.entity_id,NEW.model_contract,NEW.dimension)
 IS DISTINCT FROM ROW(OLD.index_kind,OLD.entity_id,OLD.model_contract,OLD.dimension)
 OR NEW.entity_revision<OLD.entity_revision THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='immutable_vector_projection_identity';
 END IF;
 RETURN NEW;
END;
$fn$;
REVOKE ALL ON FUNCTION em_content.keep_vector_projection_identity() FROM PUBLIC;
CREATE TRIGGER immutable_vector_projection BEFORE UPDATE ON em_content.vector_projections
 FOR EACH ROW EXECUTE FUNCTION em_content.keep_vector_projection_identity();
REVOKE ALL ON em_content.vector_projections FROM PUBLIC;
-- Runtime receives parent-table privileges only. Direct partition queries are
-- denied independently, including if someone later grants schema USAGE.
ALTER TABLE em_content.vector_library ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.vector_library FORCE ROW LEVEL SECURITY;
CREATE POLICY partition_owner ON em_content.vector_library TO :"owner_role" USING(true) WITH CHECK(true);
ALTER TABLE em_content.vector_current ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.vector_current FORCE ROW LEVEL SECURITY;
CREATE POLICY partition_owner ON em_content.vector_current TO :"owner_role" USING(true) WITH CHECK(true);
ALTER TABLE em_content.vector_legacy ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.vector_legacy FORCE ROW LEVEL SECURITY;
CREATE POLICY partition_owner ON em_content.vector_legacy TO :"owner_role" USING(true) WITH CHECK(true);
ALTER TABLE em_content.vector_context ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.vector_context FORCE ROW LEVEL SECURITY;
CREATE POLICY partition_owner ON em_content.vector_context TO :"owner_role" USING(true) WITH CHECK(true);
REVOKE ALL ON em_content.vector_library,em_content.vector_current,em_content.vector_legacy,em_content.vector_context FROM PUBLIC;
INSERT INTO em_content.vector_schema_migrations(version,library_id,project_id)
 SELECT 1,library_id,project_id FROM em_security.library_state WHERE singleton AND library_id=:'library_id'::uuid;
COMMIT;

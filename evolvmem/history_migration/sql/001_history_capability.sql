\set ON_ERROR_STOP on
-- Private quarantined legacy migration; formal targets require a bound operator lease.
-- psql variables: database_name owner_role library_id route_epoch policy_epoch
--                 parent_run_id parent_manifest_sha256
BEGIN;
SET LOCAL statement_timeout='3000ms'; SET LOCAL lock_timeout='300ms';
SELECT set_config('history.capability_database', :'database_name', true);
SELECT set_config('history.capability_owner', :'owner_role', true);
SELECT set_config('history.capability_library', :'library_id', true);
SELECT set_config('history.capability_route', :'route_epoch', true);
SELECT set_config('history.capability_policy', :'policy_epoch', true);
SELECT set_config('history.capability_parent', :'parent_run_id', true);
SELECT set_config('history.capability_parent_hash', :'parent_manifest_sha256', true);
DO $gate$
DECLARE t text;
BEGIN
 IF current_database() !~ '^em_pilot_[a-z0-9_]+$' THEN
   PERFORM em_security.assert_history_operator_binding();
 END IF;
 PERFORM 1 FROM em_security.library_state WHERE singleton FOR UPDATE;
 IF current_database()<>current_setting('history.capability_database')
 OR current_user<>current_setting('history.capability_owner')
 OR NOT EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
   WHERE r.rolname=current_user AND d.datname=current_database() AND NOT r.rolcanlogin
   AND NOT r.rolsuper AND NOT r.rolbypassrls AND NOT r.rolcreatedb AND NOT r.rolcreaterole)
 OR (SELECT count(*) FROM em_security.library_state WHERE singleton AND library_id=current_setting('history.capability_library')::uuid
   AND kind='legacy' AND device_id IS NULL AND project_id IS NULL AND authority_state='pg_staging' AND acl_state='updating'
   AND route_epoch=current_setting('history.capability_route')::bigint AND policy_epoch=current_setting('history.capability_policy')::bigint)<>1
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_capability_precondition'; END IF;
 -- Preserve the original core/source lock order. No existing rows are altered.
 FOREACH t IN ARRAY ARRAY['context_project_registry','memories','context_items','context_layers',
   'legacy_memory_migrations','context_project_resolutions','history_schema_migrations','history_import_runs',
   'history_import_chunks','history_import_links','history_batch_schema_migrations','history_batch_progress',
   'session_archives','session_archive_storage','conversation_history','archive_source_exclusions',
   'session_archive_holds','context_sources','context_evidence','history_source_schema_migrations',
   'history_source_runs','history_source_chunks','history_source_batch_schema_migrations','history_source_batch_progress'] LOOP
   EXECUTE format('LOCK TABLE em_content.%I IN ACCESS EXCLUSIVE MODE',t);
   IF NOT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='em_content'
     AND c.relname=t AND c.relowner=current_user::regrole AND c.relrowsecurity AND c.relforcerowsecurity)
   THEN RAISE EXCEPTION 'history_capability_parent_owner_rls_required'; END IF;
   IF t=ANY(ARRAY['session_archives','session_archive_storage','conversation_history','archive_source_exclusions',
     'session_archive_holds','context_sources','context_evidence']) AND NOT EXISTS(SELECT 1 FROM pg_policy p
     WHERE p.polrelid=('em_content.'||t)::regclass AND p.polname='history_source_quarantine'
     AND p.polcmd='*' AND NOT p.polpermissive AND p.polroles=ARRAY[0::oid]
     AND pg_get_expr(p.polqual,p.polrelid)='(CURRENT_USER = '||quote_literal(current_user)||'::name)'
     AND pg_get_expr(p.polwithcheck,p.polrelid)='(CURRENT_USER = '||quote_literal(current_user)||'::name)')
   THEN RAISE EXCEPTION 'history_capability_parent_quarantine_required'; END IF;
 END LOOP;
 IF (SELECT count(*) FROM em_content.history_source_batch_schema_migrations WHERE version=1
   AND migration_name='002_history_sources_bounded' AND library_id=current_setting('history.capability_library')::uuid)<>1
 OR (SELECT count(*) FROM em_content.history_source_runs r JOIN em_content.history_source_batch_progress p USING(run_id)
   JOIN em_content.history_import_runs cr ON cr.run_id=r.core_run_id JOIN em_content.history_batch_progress cp ON cp.run_id=cr.run_id
   WHERE r.run_id=current_setting('history.capability_parent')::uuid AND r.library_id=current_setting('history.capability_library')::uuid
   AND p.manifest_hash=current_setting('history.capability_parent_hash') AND r.state='loading'
   AND p.stage='verified' AND p.generation=p.verify_generation
   AND cr.library_id=r.library_id AND cr.state='core_verified' AND cp.stage='verified' AND cp.generation=cp.verify_generation
   AND r.manifest->'source'->>'source_namespace' IN ('owner','kane','public'))<>1
 THEN RAISE EXCEPTION 'history_capability_verified_parent_required'; END IF;
END;
$gate$;

CREATE TABLE em_content.capability_categories(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "id" text,
 "label" text NOT NULL,
 "enabled" bigint DEFAULT 1 NOT NULL,
 "revision" bigint DEFAULT 1 NOT NULL,
 "created_at" text NOT NULL,
 "updated_at" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"id")
);
CREATE TABLE em_content.capability_memories(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "memory_id" bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
 "category" text NOT NULL,
 "title" text NOT NULL,
 "content" text NOT NULL,
 "keywords" text DEFAULT '[]' NOT NULL,
 "source_project" text DEFAULT '' NOT NULL,
 "source_ref" text DEFAULT '' NOT NULL,
 "occurred_at" text,
 "fingerprint" text NOT NULL,
 "status" text DEFAULT 'active' NOT NULL,
 "revision" bigint DEFAULT 1 NOT NULL,
 "created_at" text NOT NULL,
 "updated_at" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 CHECK(status IN ('active','pending','ignored','archived')),
 UNIQUE(library_id,"memory_id"),
 UNIQUE(library_id,"fingerprint")
);
CREATE TABLE em_content.capability_sources(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "source_id" bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
 "memory_id" bigint NOT NULL,
 "source_kind" text NOT NULL,
 "source_key" text NOT NULL,
 "source_ref" text NOT NULL,
 "source_revision" text DEFAULT '' NOT NULL,
 "context_item_id" bigint,
 "task_id" bigint,
 "unit_digest" text DEFAULT '' NOT NULL,
 "unit_revision" bigint,
 "projects" text DEFAULT '[]' NOT NULL,
 "knowledge_scope" text DEFAULT 'project' NOT NULL,
 "evidence_status" text DEFAULT 'unverified' NOT NULL,
 "created_at" text NOT NULL,
 "updated_at" text NOT NULL,
 "revision" bigint DEFAULT 1 NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 CHECK(source_kind IN ('context','unit','reference','manual')),
 UNIQUE(library_id,"source_id"),
 UNIQUE(library_id,"memory_id","source_kind","source_key")
);
CREATE TABLE em_content.capability_assessments(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "assessment_id" bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
 "memory_id" bigint NOT NULL,
 "source_id" bigint NOT NULL,
 "source_binding_revision" bigint NOT NULL,
 "source_revision" text NOT NULL,
 "unit_revision" bigint,
 "unit_text_hash" text NOT NULL,
 "policy_version" text NOT NULL,
 "assessment_key" text NOT NULL,
 "value" text NOT NULL,
 "verification_status" text DEFAULT 'unverified' NOT NULL,
 "score" bigint NOT NULL,
 "details" text NOT NULL,
 "legacy_active_preserved" bigint DEFAULT 0 NOT NULL,
 "revision" bigint NOT NULL,
 "created_at" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 CHECK(value IN ('reusable','reference','review','ignore')),
 CHECK(verification_status='unverified'),
 UNIQUE(library_id,"assessment_id"),
 UNIQUE(library_id,"source_id","revision"),
 UNIQUE(library_id,"source_id","assessment_key")
);
CREATE TABLE em_content.capability_category_suggestions(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "suggestion_id" bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
 "label" text NOT NULL,
 "reason" text NOT NULL,
 "source_memory_id" bigint,
 "status" text DEFAULT 'pending' NOT NULL,
 "created_at" text NOT NULL,
 "reviewed_at" text,
 PRIMARY KEY(library_id,source_rowid),
 CHECK(status IN ('pending','accepted','rejected')),
 UNIQUE(library_id,"suggestion_id")
);
CREATE TABLE em_content.capability_intake(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "intake_id" bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
 "title" text NOT NULL,
 "content" text NOT NULL,
 "keywords" text DEFAULT '[]' NOT NULL,
 "suggested_category" text DEFAULT '' NOT NULL,
 "source_project" text DEFAULT '' NOT NULL,
 "source_ref" text DEFAULT '' NOT NULL,
 "occurred_at" text,
 "fingerprint" text NOT NULL,
 "status" text DEFAULT 'pending' NOT NULL,
 "revision" bigint DEFAULT 1 NOT NULL,
 "created_at" text NOT NULL,
 "updated_at" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 CHECK(status IN ('pending','assigned','ignored')),
 UNIQUE(library_id,"intake_id"),
 UNIQUE(library_id,"fingerprint")
);
CREATE TABLE em_content.existing_capability_assessment_runs(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "plan_sha256" text NOT NULL,
 "memory_id" bigint NOT NULL,
 "source_id" bigint NOT NULL,
 "state" text NOT NULL,
 "lease_token" text DEFAULT '' NOT NULL,
 "lease_until" double precision DEFAULT 0 NOT NULL,
 "assessment_id" bigint,
 "result_hash" text DEFAULT '' NOT NULL,
 "updated_at" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"plan_sha256","memory_id","source_id")
);
ALTER TABLE em_content.capability_memories ADD FOREIGN KEY(library_id,"category") REFERENCES em_content.capability_categories(library_id,"id");
CREATE INDEX b5_1_0 ON em_content.capability_memories(library_id,"category","status","memory_id");
ALTER TABLE em_content.capability_sources ADD FOREIGN KEY(library_id,"memory_id") REFERENCES em_content.capability_memories(library_id,"memory_id");
ALTER TABLE em_content.capability_assessments ADD FOREIGN KEY(library_id,"memory_id") REFERENCES em_content.capability_memories(library_id,"memory_id");
CREATE INDEX b5_3_0 ON em_content.capability_assessments(library_id,"memory_id","source_id","assessment_id");
CREATE TABLE em_content.history_capability_schema_migrations(
 version integer PRIMARY KEY CHECK(version=1),migration_name text NOT NULL UNIQUE,
 library_id uuid NOT NULL UNIQUE,applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE em_content.history_capability_runs(
 run_id uuid PRIMARY KEY,library_id uuid NOT NULL UNIQUE,parent_run_id uuid NOT NULL REFERENCES em_content.history_source_runs(run_id),
 manifest_hash text NOT NULL CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),manifest jsonb NOT NULL,
 loaded_counts jsonb NOT NULL,loaded_bytes bigint NOT NULL DEFAULT 0 CHECK(loaded_bytes>=0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE em_content.history_capability_chunks(
 run_id uuid NOT NULL REFERENCES em_content.history_capability_runs(run_id),
 table_code text NOT NULL CHECK(table_code IN ('capability_categories','capability_memories','capability_sources','capability_assessments','capability_category_suggestions','capability_intake','existing_capability_assessment_runs')),
 chunk_key text NOT NULL CHECK(chunk_key ~ '^c[0-9]{6}$'),input_hash text NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
 row_keys jsonb NOT NULL CHECK(jsonb_typeof(row_keys)='array'),row_count integer NOT NULL CHECK(row_count BETWEEN 1 AND 128),
 input_bytes integer NOT NULL CHECK(input_bytes BETWEEN 1 AND 4194304),committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(run_id,table_code,chunk_key),CHECK(jsonb_array_length(row_keys)=row_count)
);
CREATE TABLE em_content.history_capability_progress(
 run_id uuid PRIMARY KEY REFERENCES em_content.history_capability_runs(run_id),manifest_hash text NOT NULL CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),
 stage text NOT NULL CHECK(stage IN ('loading','verifying','counting','verified')),
 next_load integer NOT NULL DEFAULT 0 CHECK(next_load>=0),next_verify integer NOT NULL DEFAULT 0 CHECK(next_verify>=0 AND next_verify<=next_load),
 generation bigint NOT NULL DEFAULT 0 CHECK(generation>=0),verify_generation bigint,
 count_table integer NOT NULL DEFAULT 0 CHECK(count_table BETWEEN 0 AND 7),count_rows bigint NOT NULL DEFAULT 0 CHECK(count_rows>=0),count_last jsonb
);
CREATE FUNCTION em_content.history_capability_touch() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $body$
BEGIN
 UPDATE em_content.history_capability_progress SET generation=generation+1;
 RETURN NULL;
END;
$body$;
REVOKE ALL ON FUNCTION em_content.history_capability_touch() FROM PUBLIC;
DO $policies$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY['capability_categories','capability_memories','capability_sources','capability_assessments','capability_category_suggestions','capability_intake','existing_capability_assessment_runs'] LOOP
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE em_content.%I FORCE ROW LEVEL SECURITY',t);
   EXECUTE format('CREATE POLICY history_capability_owner ON em_content.%I TO %I USING(true) WITH CHECK(true)',t,current_user);
   EXECUTE format('CREATE POLICY history_capability_quarantine ON em_content.%I AS RESTRICTIVE TO PUBLIC USING(CURRENT_USER=%L::name) WITH CHECK(CURRENT_USER=%L::name)',t,current_user,current_user);
   EXECUTE format('CREATE TRIGGER immutable_security_scope BEFORE UPDATE ON em_content.%I FOR EACH ROW EXECUTE FUNCTION em_content.keep_security_scope()',t);
   EXECUTE format('REVOKE ALL ON em_content.%I FROM PUBLIC',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['history_capability_schema_migrations','history_capability_runs','history_capability_chunks','history_capability_progress'] LOOP
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE em_content.%I FORCE ROW LEVEL SECURITY',t);
   EXECUTE format('CREATE POLICY history_owner_only ON em_content.%I TO %I USING(true) WITH CHECK(true)',t,current_user);
   EXECUTE format('REVOKE ALL ON em_content.%I FROM PUBLIC',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['capability_categories','capability_memories','capability_sources','capability_assessments','capability_category_suggestions','capability_intake','existing_capability_assessment_runs','history_capability_chunks'] LOOP
   EXECUTE format('CREATE TRIGGER history_capability_generation AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON em_content.%I FOR EACH STATEMENT EXECUTE FUNCTION em_content.history_capability_touch()',t);
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ALWAYS TRIGGER history_capability_generation',t);
 END LOOP;
END;
$policies$;
INSERT INTO em_content.history_capability_schema_migrations(version,migration_name,library_id)
 VALUES(1,'001_history_capability_seven',:'library_id'::uuid);
COMMIT;

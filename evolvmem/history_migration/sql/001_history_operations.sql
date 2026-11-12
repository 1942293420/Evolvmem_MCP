\set ON_ERROR_STOP on
-- Private quarantined legacy migration; formal targets require a bound operator lease.
-- psql variables: database_name owner_role library_id route_epoch policy_epoch
--                 parent_run_id parent_manifest_sha256
BEGIN;
SET LOCAL statement_timeout='3000ms'; SET LOCAL lock_timeout='300ms';
SELECT set_config('history.operations_database', :'database_name', true);
SELECT set_config('history.operations_owner', :'owner_role', true);
SELECT set_config('history.operations_library', :'library_id', true);
SELECT set_config('history.operations_route', :'route_epoch', true);
SELECT set_config('history.operations_policy', :'policy_epoch', true);
SELECT set_config('history.operations_parent', :'parent_run_id', true);
SELECT set_config('history.operations_parent_hash', :'parent_manifest_sha256', true);
DO $gate$
DECLARE t text;
BEGIN
 IF current_database() !~ '^em_pilot_[a-z0-9_]+$' THEN
   PERFORM em_security.assert_history_operator_binding();
 END IF;
 PERFORM 1 FROM em_security.library_state WHERE singleton FOR UPDATE;
 IF current_database()<>current_setting('history.operations_database')
 OR current_user<>current_setting('history.operations_owner')
 OR NOT EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
   WHERE r.rolname=current_user AND d.datname=current_database() AND NOT r.rolcanlogin
   AND NOT r.rolsuper AND NOT r.rolbypassrls AND NOT r.rolcreatedb AND NOT r.rolcreaterole)
 OR (SELECT count(*) FROM em_security.library_state WHERE singleton AND library_id=current_setting('history.operations_library')::uuid
   AND kind='legacy' AND device_id IS NULL AND project_id IS NULL AND authority_state='pg_staging' AND acl_state='updating'
   AND route_epoch=current_setting('history.operations_route')::bigint AND policy_epoch=current_setting('history.operations_policy')::bigint)<>1
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_operations_precondition'; END IF;
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
   THEN RAISE EXCEPTION 'history_operations_parent_owner_rls_required'; END IF;
   IF t=ANY(ARRAY['session_archives','session_archive_storage','conversation_history','archive_source_exclusions',
     'session_archive_holds','context_sources','context_evidence']) AND NOT EXISTS(SELECT 1 FROM pg_policy p
     WHERE p.polrelid=('em_content.'||t)::regclass AND p.polname='history_source_quarantine'
     AND p.polcmd='*' AND NOT p.polpermissive AND p.polroles=ARRAY[0::oid]
     AND pg_get_expr(p.polqual,p.polrelid)='(CURRENT_USER = '||quote_literal(current_user)||'::name)'
     AND pg_get_expr(p.polwithcheck,p.polrelid)='(CURRENT_USER = '||quote_literal(current_user)||'::name)')
   THEN RAISE EXCEPTION 'history_operations_parent_quarantine_required'; END IF;
 END LOOP;
 IF (SELECT count(*) FROM em_content.history_source_batch_schema_migrations WHERE version=1
   AND migration_name='002_history_sources_bounded' AND library_id=current_setting('history.operations_library')::uuid)<>1
 OR (SELECT count(*) FROM em_content.history_source_runs r JOIN em_content.history_source_batch_progress p USING(run_id)
   JOIN em_content.history_import_runs cr ON cr.run_id=r.core_run_id JOIN em_content.history_batch_progress cp ON cp.run_id=cr.run_id
   WHERE r.run_id=current_setting('history.operations_parent')::uuid AND r.library_id=current_setting('history.operations_library')::uuid
   AND p.manifest_hash=current_setting('history.operations_parent_hash') AND r.state='loading'
   AND p.stage='verified' AND p.generation=p.verify_generation
   AND cr.library_id=r.library_id AND cr.state='core_verified' AND cp.stage='verified' AND cp.generation=cp.verify_generation
   AND r.manifest->'source'->>'source_namespace' IN ('owner','kane','public'))<>1
 THEN RAISE EXCEPTION 'history_operations_verified_parent_required'; END IF;
END;
$gate$;

CREATE TABLE em_content.context_health_generation(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "id" bigint NOT NULL,
 "version" bigint NOT NULL,
 "generation" bigint NOT NULL,
 "schema_version" bigint NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 CHECK(id=1),
 CHECK(generation>=0),
 UNIQUE(library_id,"id")
);
CREATE TABLE em_content.lan_memory_revision(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "id" bigint NOT NULL,
 "revision" bigint NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"id")
);
CREATE TABLE em_content.lan_publications(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "context_id" bigint NOT NULL,
 "author" text NOT NULL,
 "active" bigint NOT NULL,
 "title" text NOT NULL,
 "summary" text NOT NULL,
 "project" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 CHECK(author IN ('jiangli','kane')),
 CHECK(active IN (0,1)),
 UNIQUE(library_id,"context_id")
);
CREATE TABLE em_content.lan_workspace_bindings(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "remote_key" text,
 "fingerprint" text NOT NULL,
 "workstream_id" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"remote_key")
);
CREATE TABLE em_content.history_lan_write_requests(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "user" text NOT NULL,
 "tool" text NOT NULL,
 "request_id" text NOT NULL,
 "payload_hash" text NOT NULL,
 "result" text,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"user","tool","request_id")
);
CREATE TABLE em_content.local_capture_files(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "id" bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
 "session_id" text NOT NULL,
 "path" text NOT NULL,
 "device" bigint DEFAULT 0 NOT NULL,
 "inode" bigint DEFAULT 0 NOT NULL,
 "scanned_offset_bytes" bigint DEFAULT 0 NOT NULL,
 "scanned_line" bigint DEFAULT 0 NOT NULL,
 "pending_bytes" bigint DEFAULT 0 NOT NULL,
 "since_floor" double precision DEFAULT 0 NOT NULL,
 "file_size" bigint DEFAULT 0 NOT NULL,
 "file_mtime" double precision DEFAULT 0 NOT NULL,
 "anchor_offset" bigint DEFAULT 0 NOT NULL,
 "anchor_bytes" bigint DEFAULT 0 NOT NULL,
 "anchor_sha256" text DEFAULT '' NOT NULL,
 "file_state" text DEFAULT 'advancing' NOT NULL,
 "blocked_cap_bytes" bigint DEFAULT 0 NOT NULL,
 "archived_offset_bytes" bigint DEFAULT 0 NOT NULL,
 "archived_line" bigint DEFAULT 0 NOT NULL,
 "archived_batches" bigint DEFAULT 0 NOT NULL,
 "archived_events" bigint DEFAULT 0 NOT NULL,
 "filtered_events" bigint DEFAULT 0 NOT NULL,
 "failed_batches" bigint DEFAULT 0 NOT NULL,
 "rewrite_count" bigint DEFAULT 0 NOT NULL,
 "last_error" text DEFAULT '' NOT NULL,
 "last_success_at" text DEFAULT '' NOT NULL,
 "last_error_at" text DEFAULT '' NOT NULL,
 "first_seen_at" text DEFAULT '' NOT NULL,
 "updated_at" text DEFAULT '' NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"id"),
 UNIQUE(library_id,"device","inode")
);
CREATE TABLE em_content.local_capture_scans(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "id" bigint NOT NULL,
 "last_scan_at" text DEFAULT '' NOT NULL,
 "last_success_at" text DEFAULT '' NOT NULL,
 "last_error" text DEFAULT '' NOT NULL,
 "scans" bigint DEFAULT 0 NOT NULL,
 "scanned_bytes" bigint DEFAULT 0 NOT NULL,
 "archived_batches" bigint DEFAULT 0 NOT NULL,
 "archived_events" bigint DEFAULT 0 NOT NULL,
 "filtered_events" bigint DEFAULT 0 NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 CHECK(id=1),
 UNIQUE(library_id,"id")
);
CREATE TABLE em_content.organization_review_vectors(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "cache_key" text,
 "vector" bytea NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"cache_key")
);
CREATE TABLE em_content.round_metrics(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "round" bigint NOT NULL,
 "batch_id" text,
 "m1_valid_rate" double precision,
 "m1_samples" bigint,
 "m2_reuse_rate" double precision,
 "m3_per_project" double precision,
 "m3_items_per_correction" double precision,
 "m4_auto_rate" double precision,
 "m4_samples" bigint,
 "pending_count" bigint,
 "candidate_count" bigint,
 "auto_named_count" bigint,
 "measured_at" text,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"round")
);
ALTER TABLE em_content.lan_publications ADD FOREIGN KEY(library_id,"context_id") REFERENCES em_content.context_items(library_id,"id");
CREATE TABLE em_content.history_operations_schema_migrations(
 version integer PRIMARY KEY CHECK(version=1),migration_name text NOT NULL UNIQUE,
 library_id uuid NOT NULL UNIQUE,applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE em_content.history_operations_runs(
 run_id uuid PRIMARY KEY,library_id uuid NOT NULL UNIQUE,parent_run_id uuid NOT NULL REFERENCES em_content.history_source_runs(run_id),
 manifest_hash text NOT NULL CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),manifest jsonb NOT NULL,
 loaded_counts jsonb NOT NULL,loaded_bytes bigint NOT NULL DEFAULT 0 CHECK(loaded_bytes>=0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE em_content.history_operations_chunks(
 run_id uuid NOT NULL REFERENCES em_content.history_operations_runs(run_id),
 table_code text NOT NULL CHECK(table_code IN ('context_health_generation','lan_memory_revision','lan_publications','lan_workspace_bindings','lan_write_requests','local_capture_files','local_capture_scans','organization_review_vectors','round_metrics')),
 chunk_key text NOT NULL CHECK(chunk_key ~ '^c[0-9]{6}$'),input_hash text NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
 row_keys jsonb NOT NULL CHECK(jsonb_typeof(row_keys)='array'),row_count integer NOT NULL CHECK(row_count BETWEEN 1 AND 128),
 input_bytes integer NOT NULL CHECK(input_bytes BETWEEN 1 AND 4194304),committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(run_id,table_code,chunk_key),CHECK(jsonb_array_length(row_keys)=row_count)
);
CREATE TABLE em_content.history_operations_progress(
 run_id uuid PRIMARY KEY REFERENCES em_content.history_operations_runs(run_id),manifest_hash text NOT NULL CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),
 stage text NOT NULL CHECK(stage IN ('loading','verifying','counting','verified')),
 next_load integer NOT NULL DEFAULT 0 CHECK(next_load>=0),next_verify integer NOT NULL DEFAULT 0 CHECK(next_verify>=0 AND next_verify<=next_load),
 generation bigint NOT NULL DEFAULT 0 CHECK(generation>=0),verify_generation bigint,
 count_table integer NOT NULL DEFAULT 0 CHECK(count_table BETWEEN 0 AND 9),count_rows bigint NOT NULL DEFAULT 0 CHECK(count_rows>=0),count_last jsonb
);
CREATE FUNCTION em_content.history_operations_touch() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $body$
BEGIN
 UPDATE em_content.history_operations_progress SET generation=generation+1;
 RETURN NULL;
END;
$body$;
REVOKE ALL ON FUNCTION em_content.history_operations_touch() FROM PUBLIC;
DO $policies$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY['context_health_generation','lan_memory_revision','lan_publications','lan_workspace_bindings','history_lan_write_requests','local_capture_files','local_capture_scans','organization_review_vectors','round_metrics'] LOOP
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE em_content.%I FORCE ROW LEVEL SECURITY',t);
   EXECUTE format('CREATE POLICY history_operations_owner ON em_content.%I TO %I USING(true) WITH CHECK(true)',t,current_user);
   EXECUTE format('CREATE POLICY history_operations_quarantine ON em_content.%I AS RESTRICTIVE TO PUBLIC USING(CURRENT_USER=%L::name) WITH CHECK(CURRENT_USER=%L::name)',t,current_user,current_user);
   EXECUTE format('CREATE TRIGGER immutable_security_scope BEFORE UPDATE ON em_content.%I FOR EACH ROW EXECUTE FUNCTION em_content.keep_security_scope()',t);
   EXECUTE format('REVOKE ALL ON em_content.%I FROM PUBLIC',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['history_operations_schema_migrations','history_operations_runs','history_operations_chunks','history_operations_progress'] LOOP
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE em_content.%I FORCE ROW LEVEL SECURITY',t);
   EXECUTE format('CREATE POLICY history_owner_only ON em_content.%I TO %I USING(true) WITH CHECK(true)',t,current_user);
   EXECUTE format('REVOKE ALL ON em_content.%I FROM PUBLIC',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['context_health_generation','lan_memory_revision','lan_publications','lan_workspace_bindings','history_lan_write_requests','local_capture_files','local_capture_scans','organization_review_vectors','round_metrics','history_operations_chunks'] LOOP
   EXECUTE format('CREATE TRIGGER history_operations_generation AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON em_content.%I FOR EACH STATEMENT EXECUTE FUNCTION em_content.history_operations_touch()',t);
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ALWAYS TRIGGER history_operations_generation',t);
 END LOOP;
END;
$policies$;
INSERT INTO em_content.history_operations_schema_migrations(version,migration_name,library_id)
 VALUES(1,'001_history_operations_nine',:'library_id'::uuid);
COMMIT;

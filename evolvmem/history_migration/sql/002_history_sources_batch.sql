\set ON_ERROR_STOP on
-- Explicit empty seven-table target. Existing migration rows are NOT deleted.
BEGIN;
SET LOCAL statement_timeout='3000ms'; SET LOCAL lock_timeout='300ms';
SELECT set_config('history.sources_database', :'database_name', true);
SELECT set_config('history.sources_owner', :'owner_role', true);
SELECT set_config('history.sources_library', :'library_id', true);
DO $gate$
DECLARE t text; occupied boolean;
BEGIN
 IF current_database() !~ '^em_pilot_[a-z0-9_]+$' THEN
   PERFORM em_security.assert_history_operator_binding();
 END IF;
 PERFORM 1 FROM em_security.library_state WHERE singleton FOR UPDATE;
 IF current_database()<>current_setting('history.sources_database') 
 OR current_user<>current_setting('history.sources_owner')
 OR NOT EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
   WHERE r.rolname=current_user AND d.datname=current_database() AND NOT r.rolcanlogin
   AND NOT r.rolsuper AND NOT r.rolbypassrls AND NOT r.rolcreatedb AND NOT r.rolcreaterole)
 OR (SELECT count(*) FROM em_security.library_state WHERE singleton AND library_id=current_setting('history.sources_library')::uuid
   AND kind='legacy' AND device_id IS NULL AND project_id IS NULL AND authority_state='pg_staging' AND acl_state='updating')<>1
 OR (SELECT count(*) FROM em_content.history_source_schema_migrations WHERE version=1
   AND migration_name='001_history_sources_seven' AND library_id=current_setting('history.sources_library')::uuid)<>1
 THEN RAISE EXCEPTION 'history_source_batch_precondition'; END IF;
 FOREACH t IN ARRAY ARRAY['session_archives','session_archive_storage','conversation_history','archive_source_exclusions',
   'session_archive_holds','context_sources','context_evidence','history_source_runs','history_source_chunks'] LOOP
   EXECUTE format('LOCK TABLE em_content.%I IN ACCESS EXCLUSIVE MODE',t);
   IF NOT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='em_content'
     AND c.relname=t AND c.relowner=current_user::regrole AND c.relrowsecurity AND c.relforcerowsecurity)
   THEN RAISE EXCEPTION 'history_source_batch_owner_rls_required'; END IF;
   EXECUTE format('SELECT EXISTS(SELECT 1 FROM em_content.%I)',t) INTO occupied;
   IF occupied THEN RAISE EXCEPTION 'history_source_batch_empty_target_required'; END IF;
   IF t NOT IN ('history_source_runs','history_source_chunks') AND NOT EXISTS(SELECT 1 FROM pg_policy p
     WHERE p.polrelid=('em_content.'||t)::regclass AND p.polname='history_source_quarantine'
     AND p.polcmd='*' AND NOT p.polpermissive AND p.polroles=ARRAY[0::oid]
     AND pg_get_expr(p.polqual,p.polrelid)='(CURRENT_USER = '||quote_literal(current_user)||'::name)'
     AND pg_get_expr(p.polwithcheck,p.polrelid)='(CURRENT_USER = '||quote_literal(current_user)||'::name)')
   THEN RAISE EXCEPTION 'history_source_batch_quarantine_required'; END IF;
 END LOOP;
END;
$gate$;
CREATE TABLE em_content.history_source_batch_schema_migrations(
 version integer PRIMARY KEY CHECK(version=1),migration_name text NOT NULL UNIQUE,
 library_id uuid NOT NULL UNIQUE,applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE em_content.history_source_batch_progress(
 run_id uuid PRIMARY KEY REFERENCES em_content.history_source_runs(run_id),manifest_hash text NOT NULL CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),
 stage text NOT NULL CHECK(stage IN ('loading','verifying','counting','verified')),
 next_load integer NOT NULL DEFAULT 0 CHECK(next_load>=0),next_verify integer NOT NULL DEFAULT 0 CHECK(next_verify>=0 AND next_verify<=next_load),
 generation bigint NOT NULL DEFAULT 0 CHECK(generation>=0),verify_generation bigint,
 count_table integer NOT NULL DEFAULT 0 CHECK(count_table BETWEEN 0 AND 7),
 count_rows bigint NOT NULL DEFAULT 0 CHECK(count_rows>=0),count_last jsonb
);
CREATE FUNCTION em_content.history_source_batch_touch() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $body$
BEGIN
 UPDATE em_content.history_source_batch_progress SET generation=generation+1;
 RETURN NULL;
END;
$body$;
REVOKE ALL ON FUNCTION em_content.history_source_batch_touch() FROM PUBLIC;
DO $policies$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY['history_source_batch_schema_migrations','history_source_batch_progress'] LOOP
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE em_content.%I FORCE ROW LEVEL SECURITY',t);
   EXECUTE format('CREATE POLICY history_owner_only ON em_content.%I TO %I USING(true) WITH CHECK(true)',t,current_user);
   EXECUTE format('REVOKE ALL ON em_content.%I FROM PUBLIC',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['session_archives','session_archive_storage','conversation_history','archive_source_exclusions',
   'session_archive_holds','context_sources','context_evidence','history_source_chunks'] LOOP
   EXECUTE format('CREATE TRIGGER history_source_batch_generation AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON em_content.%I FOR EACH STATEMENT EXECUTE FUNCTION em_content.history_source_batch_touch()',t);
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ALWAYS TRIGGER history_source_batch_generation',t);
 END LOOP;
END;
$policies$;
INSERT INTO em_content.history_source_batch_schema_migrations(version,migration_name,library_id)
 VALUES(1,'002_history_sources_bounded',:'library_id'::uuid);
COMMIT;

\set ON_ERROR_STOP on
-- Additional independent ledger on an EMPTY, already quarantined synthetic DB.
-- No database/role creation, control registration, route changes or activation.
BEGIN;
SELECT pg_catalog.set_config('history.batch_database', :'database_name', true);
SELECT pg_catalog.set_config('history.batch_owner', :'owner_role', true);
SELECT pg_catalog.set_config('history.batch_library', :'library_id', true);
DO $gate$
DECLARE t text;
BEGIN
 IF current_database() !~ '^em_pilot_[a-z0-9_]+$' THEN
   PERFORM em_security.assert_history_operator_binding();
 END IF;
 PERFORM 1 FROM em_security.library_state WHERE singleton FOR UPDATE;
 IF current_database()<>current_setting('history.batch_database')
    OR current_user<>current_setting('history.batch_owner')
    
    OR NOT EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
       WHERE r.rolname=current_user AND d.datname=current_database()
       AND NOT r.rolcanlogin AND NOT r.rolsuper AND NOT r.rolbypassrls
       AND NOT r.rolcreatedb AND NOT r.rolcreaterole)
    OR (SELECT count(*) FROM em_security.library_state WHERE singleton
       AND library_id=current_setting('history.batch_library')::uuid AND kind='legacy'
       AND project_id IS NULL AND authority_state='pg_staging' AND acl_state='updating')<>1
    OR NOT EXISTS(SELECT 1 FROM em_content.history_schema_migrations h JOIN em_security.library_state l
       ON l.library_id=h.library_id WHERE h.version=1 AND h.migration_name='001_history_six_fixture')
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_batch_precondition'; END IF;
 FOREACH t IN ARRAY ARRAY['context_project_registry','memories','context_items','context_layers',
                         'legacy_memory_migrations','context_project_resolutions','history_import_runs'] LOOP
   EXECUTE format('LOCK TABLE em_content.%I IN ACCESS EXCLUSIVE MODE',t);
   IF NOT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='em_content' AND c.relname=t AND c.relowner=current_user::regrole
      AND c.relrowsecurity AND c.relforcerowsecurity)
   THEN RAISE EXCEPTION 'history_batch_owner_rls_required'; END IF;
 END LOOP;
 IF EXISTS(SELECT 1 FROM em_content.history_import_runs)
 THEN RAISE EXCEPTION 'history_batch_empty_run_required'; END IF;
END;
$gate$;
CREATE TABLE em_content.history_batch_schema_migrations(
 version integer PRIMARY KEY CHECK(version=1),migration_name text NOT NULL UNIQUE,
 library_id uuid NOT NULL UNIQUE,applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE em_content.history_batch_progress(
 run_id uuid PRIMARY KEY REFERENCES em_content.history_import_runs(run_id),
 manifest_hash text NOT NULL CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),
 stage text NOT NULL CHECK(stage IN ('loading','restoring','verifying','counting','verified')),
 next_load integer NOT NULL DEFAULT 0 CHECK(next_load>=0),
 next_restore integer NOT NULL DEFAULT 0 CHECK(next_restore>=0),
 next_verify integer NOT NULL DEFAULT 0 CHECK(next_verify>=0),
 loaded_counts jsonb NOT NULL,loaded_bytes bigint NOT NULL DEFAULT 0 CHECK(loaded_bytes>=0),
 generation bigint NOT NULL DEFAULT 0 CHECK(generation>=0),verify_generation bigint,
 count_table integer NOT NULL DEFAULT 0 CHECK(count_table BETWEEN 0 AND 6),
 count_rows bigint NOT NULL DEFAULT 0 CHECK(count_rows>=0),count_last jsonb,
 CHECK(next_verify<=next_restore AND next_restore<=next_load)
);
CREATE FUNCTION em_content.history_batch_touch() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $body$
BEGIN
 UPDATE em_content.history_batch_progress SET generation=generation+1;
 RETURN NULL;
END;
$body$;
REVOKE ALL ON FUNCTION em_content.history_batch_touch() FROM PUBLIC;
DO $policies$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY['history_batch_schema_migrations','history_batch_progress'] LOOP
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE em_content.%I FORCE ROW LEVEL SECURITY',t);
   EXECUTE format('CREATE POLICY history_owner_only ON em_content.%I TO %I USING(true) WITH CHECK(true)',t,current_user);
   EXECUTE format('REVOKE ALL ON em_content.%I FROM PUBLIC',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['context_project_registry','memories','context_items','context_layers',
                         'legacy_memory_migrations','context_project_resolutions',
                         'history_import_chunks','history_import_links'] LOOP
   EXECUTE format('CREATE TRIGGER history_batch_generation AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON em_content.%I FOR EACH STATEMENT EXECUTE FUNCTION em_content.history_batch_touch()',t);
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ALWAYS TRIGGER history_batch_generation',t);
 END LOOP;
END;
$policies$;
INSERT INTO em_content.history_batch_schema_migrations(version,migration_name,library_id)
 SELECT 1,'001_history_bounded_chain',library_id FROM em_security.library_state WHERE singleton;
COMMIT;

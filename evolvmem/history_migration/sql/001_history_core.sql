\set ON_ERROR_STOP on
-- Private unattached synthetic legacy fixture only. No control route changes.
-- psql variables: database_name, owner_role, library_id.
BEGIN;
SELECT pg_catalog.set_config('history.expected_database', :'database_name', true);
SELECT pg_catalog.set_config('history.expected_owner', :'owner_role', true);
SELECT pg_catalog.set_config('history.expected_library', :'library_id', true);
DO $gate$
DECLARE t text; occupied boolean;
BEGIN
 IF current_database() !~ '^em_pilot_[a-z0-9_]+$' THEN
   PERFORM em_security.assert_history_operator_binding();
 END IF;
 PERFORM 1 FROM em_security.library_state WHERE singleton FOR UPDATE;
 IF current_database()<>current_setting('history.expected_database')
    
    OR current_user<>current_setting('history.expected_owner')
    OR NOT EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
       WHERE r.rolname=current_user AND d.datname=current_database()
       AND NOT r.rolcanlogin AND NOT r.rolsuper AND NOT r.rolbypassrls
       AND NOT r.rolcreatedb AND NOT r.rolcreaterole)
    OR NOT EXISTS(SELECT 1 FROM em_content.business_schema_migrations
       WHERE version=1 AND migration_name='002_business_core_candidate')
    OR (SELECT count(*) FROM em_security.library_state WHERE singleton
       AND library_id=current_setting('history.expected_library')::uuid
       AND kind='legacy' AND project_id IS NULL
       AND authority_state='pg_staging' AND acl_state='updating')<>1
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_fixture_precondition'; END IF;
 FOREACH t IN ARRAY ARRAY['memories','context_items','context_layers','legacy_memory_migrations',
                         'context_project_registry','context_project_resolutions'] LOOP
   EXECUTE format('LOCK TABLE em_content.%I IN ACCESS EXCLUSIVE MODE',t);
   IF NOT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
      WHERE n.nspname='em_content' AND c.relname=t AND c.relowner=current_user::regrole)
   THEN RAISE EXCEPTION 'history_table_owner_mismatch'; END IF;
   EXECUTE format('SELECT EXISTS(SELECT 1 FROM em_content.%I)',t) INTO STRICT occupied;
   IF occupied THEN RAISE EXCEPTION 'history_fixture_target_not_empty'; END IF;
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE em_content.%I FORCE ROW LEVEL SECURITY',t);
   -- Even accidental later authority activation cannot expose this fixture.
   EXECUTE format('CREATE POLICY history_fixture_quarantine ON em_content.%I AS RESTRICTIVE TO PUBLIC USING(CURRENT_USER=%L::name) WITH CHECK(CURRENT_USER=%L::name)',
                  t,current_user,current_user);
 END LOOP;
END;
$gate$;
ALTER TABLE em_content.memories ALTER COLUMN attribute DROP NOT NULL;
ALTER TABLE em_content.memories ALTER COLUMN tags DROP NOT NULL;
ALTER TABLE em_content.memories ALTER COLUMN source_session DROP NOT NULL;
ALTER TABLE em_content.memories ALTER COLUMN access_count DROP NOT NULL;
ALTER TABLE em_content.context_layers ADD COLUMN legacy_layer_id bigint;
CREATE UNIQUE INDEX history_layer_identity ON em_content.context_layers(library_id,legacy_layer_id);
ALTER TABLE em_content.context_items ADD COLUMN legacy_tags_text text;
ALTER TABLE em_content.context_items ADD COLUMN legacy_projects_text text;
ALTER TABLE em_content.context_items ADD COLUMN legacy_experience_payload_text text;
ALTER TABLE em_content.context_project_resolutions ADD COLUMN legacy_evidence_json_text text;

CREATE TABLE em_content.history_schema_migrations(
 version integer PRIMARY KEY CHECK(version=1),migration_name text NOT NULL UNIQUE,
 library_id uuid NOT NULL UNIQUE,applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE em_content.history_import_runs(
 run_id uuid PRIMARY KEY,library_id uuid NOT NULL UNIQUE,
 binding_hash text NOT NULL CHECK(binding_hash ~ '^[0-9a-f]{64}$'),manifest jsonb NOT NULL,
 state text NOT NULL CHECK(state IN ('loading','links_restored','core_verified')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),verified_at timestamptz
);
CREATE TABLE em_content.history_import_chunks(
 run_id uuid NOT NULL REFERENCES em_content.history_import_runs(run_id),
 table_code text NOT NULL CHECK(table_code IN ('memories','context_items','context_layers',
   'legacy_memory_migrations','context_project_registry','context_project_resolutions')),
 chunk_key text NOT NULL CHECK(length(chunk_key) BETWEEN 1 AND 80),
 input_hash text NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
 row_keys jsonb NOT NULL CHECK(jsonb_typeof(row_keys)='array'),
 row_count integer NOT NULL CHECK(row_count BETWEEN 1 AND 128),
 input_bytes integer NOT NULL CHECK(input_bytes BETWEEN 1 AND 4194304),
 committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(run_id,table_code,chunk_key),
 CHECK(jsonb_array_length(row_keys)=row_count)
);
CREATE TABLE em_content.history_import_links(
 run_id uuid NOT NULL REFERENCES em_content.history_import_runs(run_id),
 table_code text NOT NULL CHECK(table_code IN ('memories','context_items')),
 source_id bigint NOT NULL,supersedes bigint,superseded_by bigint,
 PRIMARY KEY(run_id,table_code,source_id)
);
DO $policies$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY['history_schema_migrations','history_import_runs','history_import_chunks','history_import_links'] LOOP
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE em_content.%I FORCE ROW LEVEL SECURITY',t);
   EXECUTE format('CREATE POLICY history_owner_only ON em_content.%I TO %I USING(true) WITH CHECK(true)',t,current_user);
   EXECUTE format('REVOKE ALL ON em_content.%I FROM PUBLIC',t);
 END LOOP;
END;
$policies$;
INSERT INTO em_content.history_schema_migrations(version,migration_name,library_id)
 VALUES(1,'001_history_six_fixture',:'library_id'::uuid);
COMMIT;

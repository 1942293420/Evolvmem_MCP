\set ON_ERROR_STOP on
-- Unexecuted candidate. Fixed unattached synthetic legacy target only.
-- psql: database_name, owner_role, library_id, route_epoch, policy_epoch.
-- Run as the existing trusted nonlogin database owner; never HTTP credentials.
BEGIN;
SET LOCAL statement_timeout='3000ms';
SET LOCAL lock_timeout='300ms';
SELECT set_config('history.sources_database', :'database_name', true);
SELECT set_config('history.sources_owner', :'owner_role', true);
SELECT set_config('history.sources_library', :'library_id', true);
SELECT set_config('history.sources_route', :'route_epoch', true);
SELECT set_config('history.sources_policy', :'policy_epoch', true);
DO $precondition$
DECLARE t text;
BEGIN
 IF current_database() !~ '^em_pilot_[a-z0-9_]+$' THEN
   PERFORM em_security.assert_history_operator_binding();
 END IF;
 PERFORM 1 FROM em_security.library_state WHERE singleton FOR UPDATE;
 IF current_database()<>current_setting('history.sources_database')
 
 OR current_user<>current_setting('history.sources_owner')
 OR NOT EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
   WHERE r.rolname=current_user AND d.datname=current_database()
   AND NOT r.rolcanlogin AND NOT r.rolsuper AND NOT r.rolbypassrls
   AND NOT r.rolcreatedb AND NOT r.rolcreaterole)
 OR (SELECT count(*) FROM em_security.library_state WHERE singleton
   AND library_id=current_setting('history.sources_library')::uuid AND kind='legacy'
   AND device_id IS NULL AND project_id IS NULL AND authority_state='pg_staging' AND acl_state='updating'
   AND route_epoch=current_setting('history.sources_route')::bigint
   AND policy_epoch=current_setting('history.sources_policy')::bigint)<>1
 OR (SELECT count(*) FROM em_content.history_schema_migrations
   WHERE version=1 AND migration_name='001_history_six_fixture'
   AND library_id=current_setting('history.sources_library')::uuid)<>1
 OR (SELECT count(*) FROM em_content.history_import_runs WHERE state='core_verified'
   AND library_id=current_setting('history.sources_library')::uuid)<>1
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_sources_precondition'; END IF;
 -- Same order as the frozen core gate, before any policy/constraint inspection.
 FOREACH t IN ARRAY ARRAY['context_project_registry','memories','context_items','context_layers',
   'legacy_memory_migrations','context_project_resolutions','history_schema_migrations',
   'history_import_runs','history_import_chunks','history_import_links',
   'history_batch_schema_migrations','history_batch_progress','context_sources'] LOOP
   EXECUTE format('LOCK TABLE em_content.%I IN ACCESS EXCLUSIVE MODE',t);
   IF NOT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
       WHERE n.nspname='em_content' AND c.relname=t AND c.relowner=current_user::regrole
       AND c.relrowsecurity AND c.relforcerowsecurity)
   THEN RAISE EXCEPTION 'history_sources_owner_rls_required'; END IF;
   IF t=ANY(ARRAY['context_project_registry','memories','context_items','context_layers',
      'legacy_memory_migrations','context_project_resolutions'])
      AND NOT EXISTS(SELECT 1 FROM pg_policy p WHERE p.polrelid=('em_content.'||t)::regclass
        AND p.polname='history_fixture_quarantine' AND p.polcmd='*' AND NOT p.polpermissive
        AND p.polroles=ARRAY[0::oid]
        AND pg_get_expr(p.polqual,p.polrelid)='(CURRENT_USER = '||quote_literal(current_user)||'::name)'
        AND pg_get_expr(p.polwithcheck,p.polrelid)='(CURRENT_USER = '||quote_literal(current_user)||'::name)')
   THEN RAISE EXCEPTION 'history_sources_core_quarantine_required'; END IF;
 END LOOP;
 IF (SELECT count(*) FROM em_content.history_batch_schema_migrations
      WHERE version=1 AND migration_name='001_history_bounded_chain'
       AND library_id=current_setting('history.sources_library')::uuid)<>1
    OR (SELECT count(*) FROM em_content.history_import_runs r JOIN em_content.history_batch_progress p USING(run_id)
      WHERE r.library_id=current_setting('history.sources_library')::uuid AND r.state='core_verified'
      AND p.stage='verified' AND p.generation=p.verify_generation)<>1
 THEN RAISE EXCEPTION 'history_sources_verified_core_required'; END IF;
 IF EXISTS(SELECT 1 FROM em_content.context_sources
   WHERE library_id<>current_setting('history.sources_library')::uuid OR project_id IS NOT NULL
      OR archive_id IS NOT NULL OR source_kind<>'migration')
 THEN RAISE EXCEPTION 'history_sources_old_source_shape_changed'; END IF;
END;
$precondition$;

-- Establish restrictive quarantine BEFORE removing the old source restriction.
CREATE POLICY history_source_quarantine ON em_content.context_sources AS RESTRICTIVE TO PUBLIC
 USING(CURRENT_USER=:'owner_role'::name) WITH CHECK(CURRENT_USER=:'owner_role'::name);
DO $upgrade$
DECLARE oldcheck name; oldfk name;
BEGIN
 SELECT conname INTO STRICT oldcheck FROM pg_constraint
 WHERE conrelid='em_content.context_sources'::regclass AND contype='c'
   AND pg_get_expr(conbin,conrelid)='((archive_id IS NULL) AND (source_kind = ''migration''::text))';
 SELECT conname INTO STRICT oldfk FROM pg_constraint
 WHERE conrelid='em_content.context_sources'::regclass AND contype='f'
   AND confrelid='em_content.context_items'::regclass
   AND pg_get_constraintdef(oid)='FOREIGN KEY (library_id, item_id) REFERENCES em_content.context_items(library_id, id)';
 -- Unknown/changed constraints fail instead of removing a broader predicate.
 EXECUTE format('ALTER TABLE em_content.context_sources DROP CONSTRAINT %I',oldcheck);
 EXECUTE format('ALTER TABLE em_content.context_sources DROP CONSTRAINT %I',oldfk);
END;
$upgrade$;

CREATE TABLE em_content.session_archives(
 library_id uuid NOT NULL,project_id uuid,id bigint GENERATED ALWAYS AS IDENTITY,
 project text NOT NULL,adapter text NOT NULL,external_session_id text NOT NULL,
 payload_path text NOT NULL,payload_sha256 text NOT NULL,state text NOT NULL DEFAULT 'available',
 expires_at text NOT NULL,purged_at text,created_at text NOT NULL,
 PRIMARY KEY(library_id,id),UNIQUE(library_id,adapter,external_session_id)
);
CREATE INDEX history_archives_state ON em_content.session_archives(library_id,state);
CREATE INDEX history_archives_project ON em_content.session_archives(library_id,project);
CREATE TABLE em_content.session_archive_storage(
 library_id uuid NOT NULL,project_id uuid,archive_id bigint NOT NULL,
 manifest_sha256 text NOT NULL,content_sha256 text NOT NULL,logical_bytes bigint NOT NULL,
 PRIMARY KEY(library_id,archive_id),
 FOREIGN KEY(library_id,archive_id) REFERENCES em_content.session_archives(library_id,id) ON DELETE CASCADE
);
CREATE TABLE em_content.conversation_history(
 library_id uuid NOT NULL,project_id uuid,archive_id bigint NOT NULL,
 messages text NOT NULL,body text NOT NULL,content_hash text NOT NULL,cleaned_at text NOT NULL,
 PRIMARY KEY(library_id,archive_id),FOREIGN KEY(library_id,archive_id) REFERENCES em_content.session_archives(library_id,id)
);
CREATE TABLE em_content.archive_source_exclusions(
 library_id uuid NOT NULL,project_id uuid,archive_id bigint NOT NULL,
 payload_sha256 text NOT NULL,reason text NOT NULL,verified_at text NOT NULL,
 PRIMARY KEY(library_id,archive_id),FOREIGN KEY(library_id,archive_id) REFERENCES em_content.session_archives(library_id,id)
);
CREATE TABLE em_content.session_archive_holds(
 library_id uuid NOT NULL,project_id uuid,archive_id bigint NOT NULL,source_context_id bigint NOT NULL,
 reason text NOT NULL,created_at text NOT NULL,PRIMARY KEY(library_id,archive_id,source_context_id),
 FOREIGN KEY(library_id,archive_id) REFERENCES em_content.session_archives(library_id,id),
 FOREIGN KEY(library_id,source_context_id) REFERENCES em_content.context_items(library_id,id)
);
ALTER TABLE em_content.context_sources ADD CONSTRAINT history_sources_item_fk
 FOREIGN KEY(library_id,item_id) REFERENCES em_content.context_items(library_id,id) ON DELETE CASCADE;
ALTER TABLE em_content.context_sources ADD CONSTRAINT history_sources_archive_fk
 FOREIGN KEY(library_id,archive_id) REFERENCES em_content.session_archives(library_id,id);
CREATE INDEX history_sources_item ON em_content.context_sources(library_id,item_id);
CREATE INDEX history_sources_archive_item ON em_content.context_sources(library_id,archive_id,item_id,source_kind);
CREATE TABLE em_content.context_evidence(
 library_id uuid NOT NULL,project_id uuid,id bigint GENERATED ALWAYS AS IDENTITY,item_id bigint NOT NULL,
 source_id bigint,outcome text NOT NULL,note text NOT NULL DEFAULT '',observed_at text NOT NULL,created_at text NOT NULL,
 event_key text NOT NULL DEFAULT '',task_id text NOT NULL DEFAULT '',verification_level text NOT NULL DEFAULT '',
 conditions_json text NOT NULL DEFAULT '{}',revision bigint NOT NULL DEFAULT 1,experience_version bigint NOT NULL DEFAULT 1,
 PRIMARY KEY(library_id,id),
 FOREIGN KEY(library_id,item_id) REFERENCES em_content.context_items(library_id,id) ON DELETE CASCADE,
 FOREIGN KEY(library_id,source_id) REFERENCES em_content.context_sources(library_id,id)
);
CREATE INDEX history_evidence_item ON em_content.context_evidence(library_id,item_id);
CREATE UNIQUE INDEX history_experience_event ON em_content.context_evidence(library_id,item_id,event_key,revision) WHERE event_key<>'';

CREATE TABLE em_content.history_source_schema_migrations(
 version integer PRIMARY KEY CHECK(version=1),migration_name text NOT NULL UNIQUE,
 library_id uuid NOT NULL UNIQUE,applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE em_content.history_source_runs(
 run_id uuid PRIMARY KEY,library_id uuid NOT NULL UNIQUE,
 core_run_id uuid NOT NULL REFERENCES em_content.history_import_runs(run_id),
 binding_hash text NOT NULL CHECK(binding_hash ~ '^[0-9a-f]{64}$'),manifest jsonb NOT NULL,
 loaded_counts jsonb NOT NULL,loaded_bytes bigint NOT NULL DEFAULT 0 CHECK(loaded_bytes>=0),
 state text NOT NULL CHECK(state='loading'),created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE em_content.history_source_chunks(
 run_id uuid NOT NULL REFERENCES em_content.history_source_runs(run_id),
 table_code text NOT NULL CHECK(table_code IN ('session_archives','session_archive_storage','conversation_history',
   'archive_source_exclusions','session_archive_holds','context_sources','context_evidence')),
 chunk_key text NOT NULL CHECK(length(chunk_key) BETWEEN 1 AND 80),
 input_hash text NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),row_keys jsonb NOT NULL CHECK(jsonb_typeof(row_keys)='array'),
 row_count integer NOT NULL CHECK(row_count BETWEEN 1 AND 128),
 input_bytes integer NOT NULL CHECK(input_bytes BETWEEN 1 AND 4194304),
 committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(run_id,table_code,chunk_key),
 CHECK(jsonb_array_length(row_keys)=row_count)
);
DO $policies$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY['session_archives','session_archive_storage','conversation_history',
   'archive_source_exclusions','session_archive_holds','context_sources','context_evidence'] LOOP
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE em_content.%I FORCE ROW LEVEL SECURITY',t);
   EXECUTE format('CREATE POLICY history_source_owner ON em_content.%I TO %I USING(true) WITH CHECK(true)',t,current_user);
   IF t<>'context_sources' THEN
     EXECUTE format('CREATE POLICY history_source_quarantine ON em_content.%I AS RESTRICTIVE TO PUBLIC USING(CURRENT_USER=%L::name) WITH CHECK(CURRENT_USER=%L::name)',t,current_user,current_user);
     EXECUTE format('CREATE TRIGGER immutable_security_scope BEFORE UPDATE ON em_content.%I FOR EACH ROW EXECUTE FUNCTION em_content.keep_security_scope()',t);
   END IF;
   EXECUTE format('REVOKE ALL ON em_content.%I FROM PUBLIC',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['history_source_schema_migrations','history_source_runs','history_source_chunks'] LOOP
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE em_content.%I FORCE ROW LEVEL SECURITY',t);
   EXECUTE format('CREATE POLICY history_owner_only ON em_content.%I TO %I USING(true) WITH CHECK(true)',t,current_user);
   EXECUTE format('REVOKE ALL ON em_content.%I FROM PUBLIC',t);
 END LOOP;
END;
$policies$;
INSERT INTO em_content.history_source_schema_migrations(version,migration_name,library_id)
 VALUES(1,'001_history_sources_seven',:'library_id'::uuid);
COMMIT;

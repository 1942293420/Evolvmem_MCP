\set ON_ERROR_STOP on
-- Private quarantined legacy migration; formal targets require a bound operator lease.
-- psql variables: database_name owner_role library_id route_epoch policy_epoch
--                 parent_run_id parent_manifest_sha256
BEGIN;
SET LOCAL statement_timeout='3000ms'; SET LOCAL lock_timeout='300ms';
SELECT set_config('history.knowledge_database', :'database_name', true);
SELECT set_config('history.knowledge_owner', :'owner_role', true);
SELECT set_config('history.knowledge_library', :'library_id', true);
SELECT set_config('history.knowledge_route', :'route_epoch', true);
SELECT set_config('history.knowledge_policy', :'policy_epoch', true);
SELECT set_config('history.knowledge_parent', :'parent_run_id', true);
SELECT set_config('history.knowledge_parent_hash', :'parent_manifest_sha256', true);
DO $gate$
DECLARE t text;
BEGIN
 IF current_database() !~ '^em_pilot_[a-z0-9_]+$' THEN
   PERFORM em_security.assert_history_operator_binding();
 END IF;
 PERFORM 1 FROM em_security.library_state WHERE singleton FOR UPDATE;
 IF current_database()<>current_setting('history.knowledge_database')
 OR current_user<>current_setting('history.knowledge_owner')
 OR NOT EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
   WHERE r.rolname=current_user AND d.datname=current_database() AND NOT r.rolcanlogin
   AND NOT r.rolsuper AND NOT r.rolbypassrls AND NOT r.rolcreatedb AND NOT r.rolcreaterole)
 OR (SELECT count(*) FROM em_security.library_state WHERE singleton AND library_id=current_setting('history.knowledge_library')::uuid
   AND kind='legacy' AND device_id IS NULL AND project_id IS NULL AND authority_state='pg_staging' AND acl_state='updating'
   AND route_epoch=current_setting('history.knowledge_route')::bigint AND policy_epoch=current_setting('history.knowledge_policy')::bigint)<>1
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_knowledge_precondition'; END IF;
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
   THEN RAISE EXCEPTION 'history_knowledge_parent_owner_rls_required'; END IF;
   IF t=ANY(ARRAY['session_archives','session_archive_storage','conversation_history','archive_source_exclusions',
     'session_archive_holds','context_sources','context_evidence']) AND NOT EXISTS(SELECT 1 FROM pg_policy p
     WHERE p.polrelid=('em_content.'||t)::regclass AND p.polname='history_source_quarantine'
     AND p.polcmd='*' AND NOT p.polpermissive AND p.polroles=ARRAY[0::oid]
     AND pg_get_expr(p.polqual,p.polrelid)='(CURRENT_USER = '||quote_literal(current_user)||'::name)'
     AND pg_get_expr(p.polwithcheck,p.polrelid)='(CURRENT_USER = '||quote_literal(current_user)||'::name)')
   THEN RAISE EXCEPTION 'history_knowledge_parent_quarantine_required'; END IF;
 END LOOP;
 IF (SELECT count(*) FROM em_content.history_source_batch_schema_migrations WHERE version=1
   AND migration_name='002_history_sources_bounded' AND library_id=current_setting('history.knowledge_library')::uuid)<>1
 OR (SELECT count(*) FROM em_content.history_source_runs r JOIN em_content.history_source_batch_progress p USING(run_id)
   JOIN em_content.history_import_runs cr ON cr.run_id=r.core_run_id JOIN em_content.history_batch_progress cp ON cp.run_id=cr.run_id
   WHERE r.run_id=current_setting('history.knowledge_parent')::uuid AND r.library_id=current_setting('history.knowledge_library')::uuid
   AND p.manifest_hash=current_setting('history.knowledge_parent_hash') AND r.state='loading'
   AND p.stage='verified' AND p.generation=p.verify_generation
   AND cr.library_id=r.library_id AND cr.state='core_verified' AND cp.stage='verified' AND cp.generation=cp.verify_generation
   AND r.manifest->'source'->>'source_namespace' IN ('owner','kane','public'))<>1
 THEN RAISE EXCEPTION 'history_knowledge_verified_parent_required'; END IF;
END;
$gate$;

CREATE TABLE em_content.knowledge_objects(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "knowledge_id" text,
 "fingerprint" text NOT NULL,
 "category" text NOT NULL,
 "item_id" bigint,
 "history_json" text DEFAULT '{}' NOT NULL,
 "decision_json" text DEFAULT '{}' NOT NULL,
 "revision" bigint DEFAULT 0 NOT NULL,
 "retired" bigint DEFAULT 0 NOT NULL,
 "created_at" text NOT NULL,
 "updated_at" text NOT NULL,
 "parent_id" text DEFAULT '' NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"knowledge_id")
);
CREATE TABLE em_content.knowledge_fact_groups(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "id" text,
 "subject" text NOT NULL,
 "property" text NOT NULL,
 "revision" bigint DEFAULT 1 NOT NULL,
 "created_at" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"id")
);
CREATE TABLE em_content.knowledge_fact_versions(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "id" text,
 "group_id" text NOT NULL,
 "value" text NOT NULL,
 "conditions_json" text NOT NULL,
 "valid_from" text NOT NULL,
 "valid_until" text NOT NULL,
 "recorded_at" text NOT NULL,
 "exclusive" bigint DEFAULT 0 NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"id")
);
CREATE TABLE em_content.knowledge_decisions(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "knowledge_id" text NOT NULL,
 "revision" bigint NOT NULL,
 "decision_json" text NOT NULL,
 "coverage_json" text NOT NULL,
 "task_id" bigint NOT NULL,
 "unit_digest" text NOT NULL,
 "created_at" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"knowledge_id","revision")
);
CREATE TABLE em_content.knowledge_equivalences(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "source_knowledge_id" text,
 "target_knowledge_id" text NOT NULL,
 "active" bigint DEFAULT 1 NOT NULL,
 "revision" bigint DEFAULT 1 NOT NULL,
 "created_at" text NOT NULL,
 "updated_at" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"source_knowledge_id")
);
CREATE TABLE em_content.knowledge_fact_actions(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "id" bigint NOT NULL,
 "group_id" text NOT NULL,
 "action" text NOT NULL,
 "payload_json" text NOT NULL,
 "created_at" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"id")
);
CREATE TABLE em_content.knowledge_fact_choices(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "group_id" text NOT NULL,
 "conditions_json" text NOT NULL,
 "version_id" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"group_id","conditions_json")
);
CREATE TABLE em_content.knowledge_fact_conditions(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "version_id" text,
 "conditions_json" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"version_id")
);
CREATE TABLE em_content.knowledge_fact_issues(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "id" text,
 "group_id" text NOT NULL,
 "conditions_json" text NOT NULL,
 "members_json" text NOT NULL,
 "kind" text NOT NULL,
 "state" text DEFAULT 'pending' NOT NULL,
 "revision" bigint DEFAULT 1 NOT NULL,
 "updated_at" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"id")
);
CREATE TABLE em_content.knowledge_fact_relations(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "id" text,
 "knowledge_id" text NOT NULL,
 "version_id" text NOT NULL,
 "type" text NOT NULL,
 "target" text NOT NULL,
 "quote" text NOT NULL,
 "state" text DEFAULT 'suggested' NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"id")
);
CREATE TABLE em_content.knowledge_fact_separations(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "base_group" text NOT NULL,
 "knowledge_id" text NOT NULL,
 "group_id" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"base_group","knowledge_id")
);
CREATE TABLE em_content.knowledge_fact_sources(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "version_id" text NOT NULL,
 "knowledge_id" text NOT NULL,
 "source_key" text NOT NULL,
 "start" bigint NOT NULL,
 "end" bigint NOT NULL,
 "quote" text NOT NULL,
 "role" text NOT NULL,
 "active" bigint DEFAULT 1 NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"version_id","knowledge_id","source_key","start","end")
);
CREATE TABLE em_content.knowledge_metadata(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "item_id" bigint NOT NULL,
 "title" text DEFAULT '' NOT NULL,
 "revision" bigint DEFAULT 1 NOT NULL,
 "ingestion_reason" text DEFAULT '' NOT NULL,
 "rule_revision" text DEFAULT '' NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"item_id")
);
CREATE TABLE em_content.knowledge_qa(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "item_id" bigint NOT NULL,
 "question" text NOT NULL,
 "answer" text NOT NULL,
 "source_fingerprint" text NOT NULL,
 "status" text NOT NULL,
 "reason" text DEFAULT '' NOT NULL,
 "origin" text NOT NULL,
 "revision" bigint DEFAULT 1 NOT NULL,
 "updated_at" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"item_id")
);
CREATE TABLE em_content.knowledge_restructure_map(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "legacy_item_id" bigint NOT NULL,
 "target_kind" text NOT NULL,
 "target_id" text NOT NULL,
 "outcome" text NOT NULL,
 "updated_at" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"legacy_item_id")
);
CREATE TABLE em_content.knowledge_restructure_state(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "key" text,
 "value" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"key")
);
CREATE TABLE em_content.knowledge_source_bindings(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "knowledge_id" text NOT NULL,
 "source_key" text NOT NULL,
 "source_start" bigint NOT NULL,
 "source_end" bigint NOT NULL,
 "quote" text NOT NULL,
 "quote_hash" text NOT NULL,
 "task_id" bigint NOT NULL,
 "snapshot_hash" text NOT NULL,
 "created_at" text NOT NULL,
 "approved" bigint DEFAULT 0 NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"knowledge_id","source_key","source_start","source_end","quote_hash")
);
CREATE TABLE em_content.learning_project_types(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "project" text,
 "family" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"project")
);
CREATE TABLE em_content.learning_rules(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "id" bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
 "fingerprint" text NOT NULL,
 "topic" text NOT NULL,
 "instruction" text NOT NULL,
 "trigger_text" text NOT NULL,
 "rationale" text NOT NULL,
 "exceptions" text NOT NULL,
 "scope" text NOT NULL,
 "target" text NOT NULL,
 "sources" text NOT NULL,
 "status" text NOT NULL,
 "origin" text NOT NULL,
 "reason" text NOT NULL,
 "revision" bigint DEFAULT 1 NOT NULL,
 "created_at" text NOT NULL,
 "updated_at" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"id"),
 UNIQUE(library_id,"fingerprint")
);
CREATE TABLE em_content.learning_runs(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "id" bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
 "scope" text NOT NULL,
 "target" text NOT NULL,
 "input_ids" text NOT NULL,
 "result_ids" text NOT NULL,
 "status" text NOT NULL,
 "reason" text NOT NULL,
 "created_at" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"id")
);
CREATE TABLE em_content.learning_settings(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "id" bigint NOT NULL,
 "framework" text NOT NULL,
 "revision" bigint DEFAULT 1 NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 CHECK(id=1),
 UNIQUE(library_id,"id")
);
CREATE TABLE em_content.learning_versions(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "id" bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
 "framework" text NOT NULL,
 "rules" text NOT NULL,
 "reason" text NOT NULL,
 "created_at" text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"id")
);
CREATE TABLE em_content.skill_proposals(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "id" bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
 "name" text NOT NULL,
 "status" text DEFAULT 'staged' NOT NULL,
 "description" text DEFAULT '' NOT NULL,
 "cluster_key" text DEFAULT '' NOT NULL,
 "source_item_ids" text DEFAULT '[]' NOT NULL,
 "created_at" text NOT NULL,
 "adopted_at" text,
 "rejected_at" text,
 "origin" text DEFAULT 'memory_cluster' NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,"id")
);
CREATE INDEX b4_0_0 ON em_content.knowledge_objects(library_id,"item_id");
CREATE INDEX b4_0_1 ON em_content.knowledge_objects(library_id,"fingerprint","retired");
ALTER TABLE em_content.knowledge_fact_versions ADD FOREIGN KEY(library_id,"group_id") REFERENCES em_content.knowledge_fact_groups(library_id,"id");
CREATE INDEX b4_2_0 ON em_content.knowledge_fact_versions(library_id,"group_id");
ALTER TABLE em_content.knowledge_decisions ADD FOREIGN KEY(library_id,"knowledge_id") REFERENCES em_content.knowledge_objects(library_id,"knowledge_id");
ALTER TABLE em_content.knowledge_equivalences ADD FOREIGN KEY(library_id,"target_knowledge_id") REFERENCES em_content.knowledge_objects(library_id,"knowledge_id");
ALTER TABLE em_content.knowledge_equivalences ADD FOREIGN KEY(library_id,"source_knowledge_id") REFERENCES em_content.knowledge_objects(library_id,"knowledge_id");
ALTER TABLE em_content.knowledge_fact_conditions ADD FOREIGN KEY(library_id,"version_id") REFERENCES em_content.knowledge_fact_versions(library_id,"id");
ALTER TABLE em_content.knowledge_fact_issues ADD FOREIGN KEY(library_id,"group_id") REFERENCES em_content.knowledge_fact_groups(library_id,"id");
ALTER TABLE em_content.knowledge_fact_relations ADD FOREIGN KEY(library_id,"version_id") REFERENCES em_content.knowledge_fact_versions(library_id,"id");
ALTER TABLE em_content.knowledge_fact_relations ADD FOREIGN KEY(library_id,"knowledge_id") REFERENCES em_content.knowledge_objects(library_id,"knowledge_id");
CREATE INDEX b4_9_0 ON em_content.knowledge_fact_relations(library_id,"version_id");
ALTER TABLE em_content.knowledge_fact_separations ADD FOREIGN KEY(library_id,"group_id") REFERENCES em_content.knowledge_fact_groups(library_id,"id");
ALTER TABLE em_content.knowledge_fact_sources ADD FOREIGN KEY(library_id,"knowledge_id") REFERENCES em_content.knowledge_objects(library_id,"knowledge_id");
ALTER TABLE em_content.knowledge_fact_sources ADD FOREIGN KEY(library_id,"version_id") REFERENCES em_content.knowledge_fact_versions(library_id,"id");
CREATE INDEX b4_11_0 ON em_content.knowledge_fact_sources(library_id,"active","knowledge_id");
ALTER TABLE em_content.knowledge_metadata ADD FOREIGN KEY(library_id,"item_id") REFERENCES em_content.context_items(library_id,"id") ON DELETE CASCADE;
ALTER TABLE em_content.knowledge_qa ADD FOREIGN KEY(library_id,"item_id") REFERENCES em_content.context_items(library_id,"id") ON DELETE CASCADE;
ALTER TABLE em_content.knowledge_source_bindings ADD FOREIGN KEY(library_id,"knowledge_id") REFERENCES em_content.knowledge_objects(library_id,"knowledge_id");
CREATE INDEX b4_16_0 ON em_content.knowledge_source_bindings(library_id,"source_key");
ALTER TABLE em_content.learning_project_types ADD FOREIGN KEY(library_id,"project") REFERENCES em_content.context_project_registry(library_id,"project");
CREATE INDEX b4_22_0 ON em_content.skill_proposals(library_id,"name","status");
CREATE TABLE em_content.history_knowledge_schema_migrations(
 version integer PRIMARY KEY CHECK(version=1),migration_name text NOT NULL UNIQUE,
 library_id uuid NOT NULL UNIQUE,applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE em_content.history_knowledge_runs(
 run_id uuid PRIMARY KEY,library_id uuid NOT NULL UNIQUE,parent_run_id uuid NOT NULL REFERENCES em_content.history_source_runs(run_id),
 manifest_hash text NOT NULL CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),manifest jsonb NOT NULL,
 loaded_counts jsonb NOT NULL,loaded_bytes bigint NOT NULL DEFAULT 0 CHECK(loaded_bytes>=0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE em_content.history_knowledge_chunks(
 run_id uuid NOT NULL REFERENCES em_content.history_knowledge_runs(run_id),
 table_code text NOT NULL CHECK(table_code IN ('knowledge_objects','knowledge_fact_groups','knowledge_fact_versions','knowledge_decisions','knowledge_equivalences','knowledge_fact_actions','knowledge_fact_choices','knowledge_fact_conditions','knowledge_fact_issues','knowledge_fact_relations','knowledge_fact_separations','knowledge_fact_sources','knowledge_metadata','knowledge_qa','knowledge_restructure_map','knowledge_restructure_state','knowledge_source_bindings','learning_project_types','learning_rules','learning_runs','learning_settings','learning_versions','skill_proposals')),
 chunk_key text NOT NULL CHECK(chunk_key ~ '^c[0-9]{6}$'),input_hash text NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
 row_keys jsonb NOT NULL CHECK(jsonb_typeof(row_keys)='array'),row_count integer NOT NULL CHECK(row_count BETWEEN 1 AND 128),
 input_bytes integer NOT NULL CHECK(input_bytes BETWEEN 1 AND 4194304),committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(run_id,table_code,chunk_key),CHECK(jsonb_array_length(row_keys)=row_count)
);
CREATE TABLE em_content.history_knowledge_progress(
 run_id uuid PRIMARY KEY REFERENCES em_content.history_knowledge_runs(run_id),manifest_hash text NOT NULL CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),
 stage text NOT NULL CHECK(stage IN ('loading','verifying','counting','verified')),
 next_load integer NOT NULL DEFAULT 0 CHECK(next_load>=0),next_verify integer NOT NULL DEFAULT 0 CHECK(next_verify>=0 AND next_verify<=next_load),
 generation bigint NOT NULL DEFAULT 0 CHECK(generation>=0),verify_generation bigint,
 count_table integer NOT NULL DEFAULT 0 CHECK(count_table BETWEEN 0 AND 23),count_rows bigint NOT NULL DEFAULT 0 CHECK(count_rows>=0),count_last jsonb
);
CREATE FUNCTION em_content.history_knowledge_touch() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $body$
BEGIN
 UPDATE em_content.history_knowledge_progress SET generation=generation+1;
 RETURN NULL;
END;
$body$;
REVOKE ALL ON FUNCTION em_content.history_knowledge_touch() FROM PUBLIC;
DO $policies$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY['knowledge_objects','knowledge_fact_groups','knowledge_fact_versions','knowledge_decisions','knowledge_equivalences','knowledge_fact_actions','knowledge_fact_choices','knowledge_fact_conditions','knowledge_fact_issues','knowledge_fact_relations','knowledge_fact_separations','knowledge_fact_sources','knowledge_metadata','knowledge_qa','knowledge_restructure_map','knowledge_restructure_state','knowledge_source_bindings','learning_project_types','learning_rules','learning_runs','learning_settings','learning_versions','skill_proposals'] LOOP
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE em_content.%I FORCE ROW LEVEL SECURITY',t);
   EXECUTE format('CREATE POLICY history_knowledge_owner ON em_content.%I TO %I USING(true) WITH CHECK(true)',t,current_user);
   EXECUTE format('CREATE POLICY history_knowledge_quarantine ON em_content.%I AS RESTRICTIVE TO PUBLIC USING(CURRENT_USER=%L::name) WITH CHECK(CURRENT_USER=%L::name)',t,current_user,current_user);
   EXECUTE format('CREATE TRIGGER immutable_security_scope BEFORE UPDATE ON em_content.%I FOR EACH ROW EXECUTE FUNCTION em_content.keep_security_scope()',t);
   EXECUTE format('REVOKE ALL ON em_content.%I FROM PUBLIC',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['history_knowledge_schema_migrations','history_knowledge_runs','history_knowledge_chunks','history_knowledge_progress'] LOOP
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE em_content.%I FORCE ROW LEVEL SECURITY',t);
   EXECUTE format('CREATE POLICY history_owner_only ON em_content.%I TO %I USING(true) WITH CHECK(true)',t,current_user);
   EXECUTE format('REVOKE ALL ON em_content.%I FROM PUBLIC',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['knowledge_objects','knowledge_fact_groups','knowledge_fact_versions','knowledge_decisions','knowledge_equivalences','knowledge_fact_actions','knowledge_fact_choices','knowledge_fact_conditions','knowledge_fact_issues','knowledge_fact_relations','knowledge_fact_separations','knowledge_fact_sources','knowledge_metadata','knowledge_qa','knowledge_restructure_map','knowledge_restructure_state','knowledge_source_bindings','learning_project_types','learning_rules','learning_runs','learning_settings','learning_versions','skill_proposals','history_knowledge_chunks'] LOOP
   EXECUTE format('CREATE TRIGGER history_knowledge_generation AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON em_content.%I FOR EACH STATEMENT EXECUTE FUNCTION em_content.history_knowledge_touch()',t);
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ALWAYS TRIGGER history_knowledge_generation',t);
 END LOOP;
END;
$policies$;
INSERT INTO em_content.history_knowledge_schema_migrations(version,migration_name,library_id)
 VALUES(1,'001_history_knowledge_twentythree',:'library_id'::uuid);
COMMIT;

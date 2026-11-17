\set ON_ERROR_STOP on
-- Independent, owner-only quarantine upgrade. No legacy file/archive reads.
-- Existing six/seven verified parents are required. Seven new tables start empty;
-- no migration/source row is deleted/adopted and this ledger is NOT import-ready.
BEGIN;
SET LOCAL statement_timeout='3000ms'; SET LOCAL lock_timeout='300ms';
SELECT set_config('history.organization_database', :'database_name', true);
SELECT set_config('history.organization_owner', :'owner_role', true);
SELECT set_config('history.organization_library', :'library_id', true);
DO $gate$
DECLARE t text;
BEGIN
 PERFORM 1 FROM em_security.library_state WHERE singleton FOR UPDATE;
 IF current_database()<>current_setting('history.organization_database') OR current_database() !~ '^em_pilot_[a-z0-9_]+$'
 OR current_user<>current_setting('history.organization_owner')
 OR NOT EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
   WHERE r.rolname=current_user AND d.datname=current_database() AND NOT r.rolcanlogin
   AND NOT r.rolsuper AND NOT r.rolbypassrls AND NOT r.rolcreatedb AND NOT r.rolcreaterole)
 OR (SELECT count(*) FROM em_security.library_state WHERE singleton AND library_id=current_setting('history.organization_library')::uuid
   AND kind='legacy' AND device_id IS NULL AND project_id IS NULL AND authority_state='pg_staging' AND acl_state='updating')<>1
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='organization_fixture_precondition'; END IF;
 FOREACH t IN ARRAY ARRAY['memories','context_items','context_layers','legacy_memory_migrations','context_project_registry',
   'context_project_resolutions','session_archives','session_archive_storage','session_archive_holds',
   'conversation_history','archive_source_exclusions','context_sources','context_evidence'] LOOP
   EXECUTE format('LOCK TABLE em_content.%I IN SHARE ROW EXCLUSIVE MODE',t);
   IF NOT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='em_content'
      AND c.relname=t AND c.relowner=current_user::regrole AND c.relrowsecurity AND c.relforcerowsecurity)
   OR NOT EXISTS(SELECT 1 FROM pg_policy p WHERE p.polrelid=('em_content.'||t)::regclass AND NOT p.polpermissive
      AND p.polcmd='*' AND p.polroles=ARRAY[0::oid]
      AND pg_get_expr(p.polqual,p.polrelid)='(CURRENT_USER = '||quote_literal(current_user)||'::name)'
      AND pg_get_expr(p.polwithcheck,p.polrelid)='(CURRENT_USER = '||quote_literal(current_user)||'::name)')
   THEN RAISE EXCEPTION 'organization_parent_quarantine_required'; END IF;
 END LOOP;
 IF (SELECT count(*) FROM em_content.history_import_runs c JOIN em_content.history_batch_progress cp ON cp.run_id=c.run_id
    JOIN em_content.history_source_runs s ON s.core_run_id=c.run_id AND s.library_id=c.library_id
    JOIN em_content.history_source_batch_progress sp ON sp.run_id=s.run_id
    WHERE c.library_id=current_setting('history.organization_library')::uuid AND c.state='core_verified'
      AND cp.stage='verified' AND cp.generation=cp.verify_generation AND sp.stage='verified' AND sp.generation=sp.verify_generation)<>1
 THEN RAISE EXCEPTION 'organization_verified_parents_required'; END IF;
END;
$gate$;

CREATE TABLE em_content.organization_tasks(
 library_id uuid NOT NULL CHECK(library_id=:'library_id'::uuid),
 project_id uuid CHECK(project_id IS NULL),
 source_rowid bigint NOT NULL,
 id bigint GENERATED ALWAYS AS IDENTITY,
 source_key text NOT NULL,
 source_revision text NOT NULL,
 rule_revision text NOT NULL,
 source_snapshot text NOT NULL DEFAULT '',
 source_title text NOT NULL DEFAULT '',
 stage text NOT NULL DEFAULT 'queued',
 status text NOT NULL DEFAULT 'pending',
 attempts bigint NOT NULL DEFAULT 0,
 error_code text NOT NULL DEFAULT '',
 error_detail text NOT NULL DEFAULT '',
 unit_count bigint NOT NULL DEFAULT 0,
 review_count bigint NOT NULL DEFAULT 0,
 superseded_by bigint,
 created_at text NOT NULL,
 updated_at text NOT NULL,
 started_at text,
 finished_at text,
 context_basis text NOT NULL DEFAULT '',
 task_draft text NOT NULL DEFAULT '[]',
 segmentation_progress text NOT NULL DEFAULT '{}',
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,id),
 CHECK(id=source_rowid),
 UNIQUE(library_id,source_key,source_revision,rule_revision)
);
CREATE TABLE em_content.organization_units(
 library_id uuid NOT NULL CHECK(library_id=:'library_id'::uuid),
 project_id uuid CHECK(project_id IS NULL),
 source_rowid bigint NOT NULL,
 task_id bigint NOT NULL,
 ordinal bigint NOT NULL,
 digest text NOT NULL,
 title text NOT NULL,
 text text NOT NULL,
 source_start bigint NOT NULL,
 source_end bigint NOT NULL,
 category text NOT NULL DEFAULT 'reference',
 role text NOT NULL DEFAULT '',
 cleaned_text text NOT NULL DEFAULT '',
 disposition text NOT NULL DEFAULT 'keep',
 disposition_reason text NOT NULL DEFAULT '',
 extraction_stage text NOT NULL DEFAULT 'pending',
 extraction_error text NOT NULL DEFAULT '',
 extraction_signature text NOT NULL DEFAULT '',
 project_hint text NOT NULL DEFAULT '',
 project text NOT NULL DEFAULT '',
 decision text NOT NULL DEFAULT 'review',
 reason text NOT NULL DEFAULT '',
 evidence_quote text NOT NULL DEFAULT '',
 revision bigint NOT NULL DEFAULT 1,
 item_id bigint,
 created_at text NOT NULL,
 updated_at text NOT NULL,
 continues_context bigint NOT NULL DEFAULT 0,
 context_basis text NOT NULL DEFAULT '',
 applied_guidance_id bigint NOT NULL DEFAULT 0,
 extraction_diagnostic text NOT NULL DEFAULT '',
 projects text NOT NULL DEFAULT '[]',
 knowledge_scope text NOT NULL DEFAULT 'project',
 ownership_analysis text NOT NULL DEFAULT '{}',
 source_ranges text NOT NULL DEFAULT '[]',
 task_record text NOT NULL DEFAULT '{}',
 knowledge_id text NOT NULL DEFAULT '',
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,task_id,ordinal),
 FOREIGN KEY(library_id,task_id) REFERENCES em_content.organization_tasks(library_id,id) ON DELETE CASCADE
);
CREATE TABLE em_content.organization_conversations(
 library_id uuid NOT NULL CHECK(library_id=:'library_id'::uuid),
 project_id uuid CHECK(project_id IS NULL),
 source_rowid bigint NOT NULL,
 source_key text,
 identity text NOT NULL,
 members text NOT NULL,
 updated_at text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,source_key)
);
CREATE TABLE em_content.organization_context_basis(
 library_id uuid NOT NULL CHECK(library_id=:'library_id'::uuid),
 project_id uuid CHECK(project_id IS NULL),
 source_rowid bigint NOT NULL,
 unit_task_id bigint NOT NULL,
 unit_digest text NOT NULL,
 prior_task_id bigint NOT NULL,
 prior_digest text NOT NULL,
 prior_source_key text NOT NULL DEFAULT '',
 prior_archive_id bigint NOT NULL DEFAULT 0,
 prior_start_line bigint NOT NULL DEFAULT 0,
 prior_end_line bigint NOT NULL DEFAULT 0,
 prior_revision bigint NOT NULL DEFAULT 0,
 prior_source_revision text NOT NULL DEFAULT '',
 prior_payload_sha256 text NOT NULL DEFAULT '',
 prior_cleaning_revision bigint NOT NULL DEFAULT 0,
 project text NOT NULL DEFAULT '',
 state text NOT NULL DEFAULT 'active',
 created_at text NOT NULL,
 updated_at text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,unit_task_id,unit_digest)
);
CREATE TABLE em_content.unit_derivations(
 library_id uuid NOT NULL CHECK(library_id=:'library_id'::uuid),
 project_id uuid CHECK(project_id IS NULL),
 source_rowid bigint NOT NULL,
 unit_task_id bigint NOT NULL,
 unit_digest text NOT NULL,
 item_id bigint NOT NULL,
 kind text NOT NULL DEFAULT '',
 project text NOT NULL DEFAULT '',
 created_at text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,unit_task_id,unit_digest,item_id),
 FOREIGN KEY(library_id,item_id) REFERENCES em_content.context_items(library_id,id) ON DELETE CASCADE
);
CREATE TABLE em_content.learning_memories(
 library_id uuid NOT NULL CHECK(library_id=:'library_id'::uuid),
 project_id uuid CHECK(project_id IS NULL),
 source_rowid bigint NOT NULL,
 item_id bigint NOT NULL,
 category text NOT NULL,
 payload text NOT NULL,
 revision bigint NOT NULL DEFAULT 1,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,item_id),
 CHECK(item_id=source_rowid),
 FOREIGN KEY(library_id,item_id) REFERENCES em_content.context_items(library_id,id) ON DELETE CASCADE
);
CREATE TABLE em_content.knowledge_cleaning_reviews(
 library_id uuid NOT NULL CHECK(library_id=:'library_id'::uuid),
 project_id uuid CHECK(project_id IS NULL),
 source_rowid bigint NOT NULL,
 source_key text,
 source_revision text NOT NULL,
 source_text text NOT NULL,
 cleaned_text text NOT NULL,
 category text NOT NULL DEFAULT 'reference',
 rule_revision text NOT NULL,
 revision bigint NOT NULL DEFAULT 1,
 state text NOT NULL DEFAULT 'ready',
 updated_at text NOT NULL,
 PRIMARY KEY(library_id,source_rowid),
 UNIQUE(library_id,source_key)
);
CREATE INDEX organization_unit_digest ON em_content.organization_units(library_id,task_id,digest);
CREATE INDEX organization_derived_item ON em_content.unit_derivations(library_id,item_id);
CREATE TABLE em_content.history_organization_schema_migrations(
 version integer PRIMARY KEY CHECK(version=1),migration_name text NOT NULL UNIQUE,
 library_id uuid NOT NULL UNIQUE,applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
DO $policies$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY['organization_tasks','organization_units','organization_conversations','organization_context_basis','unit_derivations','learning_memories','knowledge_cleaning_reviews'] LOOP
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE em_content.%I FORCE ROW LEVEL SECURITY',t);
   EXECUTE format('CREATE POLICY history_organization_owner ON em_content.%I TO %I USING(true) WITH CHECK(true)',t,current_user);
   EXECUTE format('CREATE POLICY history_organization_quarantine ON em_content.%I AS RESTRICTIVE TO PUBLIC USING(CURRENT_USER=%L::name) WITH CHECK(CURRENT_USER=%L::name)',t,current_user,current_user);
   EXECUTE format('CREATE TRIGGER immutable_security_scope BEFORE UPDATE ON em_content.%I FOR EACH ROW EXECUTE FUNCTION em_content.keep_security_scope()',t);
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ALWAYS TRIGGER immutable_security_scope',t);
   EXECUTE format('REVOKE ALL ON em_content.%I FROM PUBLIC',t);
 END LOOP;
 ALTER TABLE em_content.history_organization_schema_migrations ENABLE ROW LEVEL SECURITY;
 ALTER TABLE em_content.history_organization_schema_migrations FORCE ROW LEVEL SECURITY;
 EXECUTE format('CREATE POLICY history_organization_owner ON em_content.history_organization_schema_migrations TO %I USING(true) WITH CHECK(true)',current_user);
 REVOKE ALL ON em_content.history_organization_schema_migrations FROM PUBLIC;
END;
$policies$;
INSERT INTO em_content.history_organization_schema_migrations(version,migration_name,library_id)
 VALUES(1,'001_organization_qualification_fixture',:'library_id'::uuid);
COMMIT;

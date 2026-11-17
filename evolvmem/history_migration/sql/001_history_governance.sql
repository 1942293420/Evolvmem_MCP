\set ON_ERROR_STOP on
-- Private quarantined legacy migration; formal targets require a bound operator lease.
-- psql variables: database_name owner_role library_id route_epoch policy_epoch
--                 parent_run_id parent_manifest_sha256
BEGIN;
SET LOCAL statement_timeout='3000ms'; SET LOCAL lock_timeout='300ms';
SELECT set_config('history.governance_database', :'database_name', true);
SELECT set_config('history.governance_owner', :'owner_role', true);
SELECT set_config('history.governance_library', :'library_id', true);
SELECT set_config('history.governance_route', :'route_epoch', true);
SELECT set_config('history.governance_policy', :'policy_epoch', true);
SELECT set_config('history.governance_parent', :'parent_run_id', true);
SELECT set_config('history.governance_parent_hash', :'parent_manifest_sha256', true);
SELECT set_config('history.governance_org_run', :'organization_run_id', true);
SELECT set_config('history.governance_org_hash', :'organization_manifest_sha256', true);
DO $gate$
DECLARE t text;
BEGIN
 IF current_database() !~ '^em_pilot_[a-z0-9_]+$' THEN
   PERFORM em_security.assert_history_operator_binding();
 END IF;
 PERFORM 1 FROM em_security.library_state WHERE singleton FOR UPDATE;
 IF current_database()<>current_setting('history.governance_database')
 OR current_user<>current_setting('history.governance_owner')
 OR NOT EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
   WHERE r.rolname=current_user AND d.datname=current_database() AND NOT r.rolcanlogin
   AND NOT r.rolsuper AND NOT r.rolbypassrls AND NOT r.rolcreatedb AND NOT r.rolcreaterole)
 OR (SELECT count(*) FROM em_security.library_state WHERE singleton AND library_id=current_setting('history.governance_library')::uuid
   AND kind='legacy' AND device_id IS NULL AND project_id IS NULL AND authority_state='pg_staging' AND acl_state='updating'
   AND route_epoch=current_setting('history.governance_route')::bigint AND policy_epoch=current_setting('history.governance_policy')::bigint)<>1
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_governance_precondition'; END IF;
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
   THEN RAISE EXCEPTION 'history_governance_parent_owner_rls_required'; END IF;
   IF t=ANY(ARRAY['session_archives','session_archive_storage','conversation_history','archive_source_exclusions',
     'session_archive_holds','context_sources','context_evidence']) AND NOT EXISTS(SELECT 1 FROM pg_policy p
     WHERE p.polrelid=('em_content.'||t)::regclass AND p.polname='history_source_quarantine'
     AND p.polcmd='*' AND NOT p.polpermissive AND p.polroles=ARRAY[0::oid]
     AND pg_get_expr(p.polqual,p.polrelid)='(CURRENT_USER = '||quote_literal(current_user)||'::name)'
     AND pg_get_expr(p.polwithcheck,p.polrelid)='(CURRENT_USER = '||quote_literal(current_user)||'::name)')
   THEN RAISE EXCEPTION 'history_governance_parent_quarantine_required'; END IF;
 END LOOP;
 IF (SELECT count(*) FROM em_content.history_source_batch_schema_migrations WHERE version=1
   AND migration_name='002_history_sources_bounded' AND library_id=current_setting('history.governance_library')::uuid)<>1
 OR (SELECT count(*) FROM em_content.history_source_runs r JOIN em_content.history_source_batch_progress p USING(run_id)
   JOIN em_content.history_import_runs cr ON cr.run_id=r.core_run_id JOIN em_content.history_batch_progress cp ON cp.run_id=cr.run_id
   WHERE r.run_id=current_setting('history.governance_parent')::uuid AND r.library_id=current_setting('history.governance_library')::uuid
   AND p.manifest_hash=current_setting('history.governance_parent_hash') AND r.state='loading'
   AND p.stage='verified' AND p.generation=p.verify_generation
   AND cr.library_id=r.library_id AND cr.state='core_verified' AND cp.stage='verified' AND cp.generation=cp.verify_generation
   AND r.manifest->'source'->>'source_namespace' IN ('owner','kane','public'))<>1
 THEN RAISE EXCEPTION 'history_governance_verified_parent_required'; END IF;
 LOCK TABLE em_content.organization_tasks,em_content.history_organization_progress IN ACCESS EXCLUSIVE MODE;
 IF (SELECT count(*) FROM em_content.history_organization_progress p JOIN em_content.history_source_batch_progress sp
 ON sp.run_id=current_setting('history.governance_parent')::uuid
 WHERE p.run_id=current_setting('history.governance_org_run')::uuid AND p.manifest_hash=current_setting('history.governance_org_hash')
 AND p.stage='verified' AND p.generation=p.verify_generation AND p.count_table=7 AND p.parent_generation=sp.generation
 AND p.proof->>'manifest_hash'=p.manifest_hash AND p.proof->>'parent_plan_hash'=sp.manifest_hash)<>1
 THEN RAISE EXCEPTION 'history_governance_organization_parent_required'; END IF;
END;
$gate$;

CREATE TABLE em_content.capability_provenance_repairs(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "plan_sha256" text COLLATE "C",
 "applied_at" text COLLATE "C" NOT NULL,
 "plan_json" text COLLATE "C" NOT NULL,
 "before_json" text COLLATE "C" NOT NULL,
 "after_json" text COLLATE "C" NOT NULL,
 "rolled_back_at" text COLLATE "C",
 CONSTRAINT hg_0_0 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_0_1 UNIQUE (library_id, plan_sha256)
);
CREATE TABLE em_content.classification_results(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "batch_id" text COLLATE "C" NOT NULL,
 "task_id" bigint NOT NULL,
 "ordinal" bigint NOT NULL,
 "text_hash" text COLLATE "C" NOT NULL,
 "predicted_project" text COLLATE "C",
 "confidence" text COLLATE "C" NOT NULL,
 "score" bigint NOT NULL DEFAULT 0,
 "margin" bigint NOT NULL DEFAULT 0,
 "evidence" text COLLATE "C",
 "competitor" text COLLATE "C",
 "source_key" text COLLATE "C",
 "reused_existing" bigint NOT NULL DEFAULT 0,
 "reviewed" bigint NOT NULL DEFAULT 0,
 "reviewed_correct" bigint,
 "reviewed_project" text COLLATE "C",
 "created_at" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_1_2 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_1_3 UNIQUE (library_id, batch_id, task_id, ordinal)
);
CREATE TABLE em_content.history_context_project_aliases(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "alias" text COLLATE "C",
 "project" text COLLATE "C" NOT NULL,
 "revision" bigint NOT NULL DEFAULT 1,
 "created_at" text COLLATE "C" NOT NULL,
 "updated_at" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_2_4 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_2_5 UNIQUE (library_id, alias),
 CONSTRAINT hg_2_6 FOREIGN KEY (library_id, project) REFERENCES em_content.context_project_registry(library_id, project)
);
CREATE TABLE em_content.context_project_rollups(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "project" text COLLATE "C",
 "current_context_id" bigint,
 "source_set_hash" text COLLATE "C" NOT NULL DEFAULT '',
 "covered_through" text COLLATE "C",
 "generator_version" text COLLATE "C" NOT NULL DEFAULT '',
 "status" text COLLATE "C" NOT NULL DEFAULT 'pending',
 "revision" bigint NOT NULL DEFAULT 1,
 "updated_at" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_3_7 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_3_8 UNIQUE (library_id, project),
 CONSTRAINT hg_3_9 FOREIGN KEY (library_id, current_context_id) REFERENCES em_content.context_items(library_id, id),
 CONSTRAINT hg_check_3_status CHECK ("status" IN ('pending','ready','failed','vector_dirty'))
);
CREATE TABLE em_content.history_context_project_workspace_bindings(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "workspace_fingerprint" text COLLATE "C" NOT NULL,
 "project" text COLLATE "C" NOT NULL,
 "state" text COLLATE "C" NOT NULL DEFAULT 'candidate',
 "is_default" bigint NOT NULL DEFAULT 0,
 "method" text COLLATE "C" NOT NULL DEFAULT '',
 "revision" bigint NOT NULL DEFAULT 1,
 "created_at" text COLLATE "C" NOT NULL,
 "updated_at" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_4_10 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_4_11 UNIQUE (library_id, workspace_fingerprint, project),
 CONSTRAINT hg_4_12 FOREIGN KEY (library_id, project) REFERENCES em_content.context_project_registry(library_id, project),
 CONSTRAINT hg_check_4_state CHECK ("state" IN ('candidate','active','revoked'))
);
CREATE TABLE em_content.corrections(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "id" bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
 "round" bigint NOT NULL,
 "kind" text COLLATE "C" NOT NULL,
 "project" text COLLATE "C" NOT NULL,
 "target_project" text COLLATE "C",
 "pattern" text COLLATE "C",
 "reason" text COLLATE "C",
 "affected_items" bigint NOT NULL DEFAULT 0,
 "created_at" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_5_13 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_5_14 UNIQUE (library_id, id),
 CONSTRAINT hg_alias_5 CHECK ("id"=source_rowid)
);
CREATE TABLE em_content.extraction_receipts(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "input_key" text COLLATE "C",
 "pipeline" text COLLATE "C" NOT NULL,
 "source_key" text COLLATE "C" NOT NULL,
 "source_revision" text COLLATE "C" NOT NULL,
 "unit_digest" text COLLATE "C" NOT NULL,
 "payload" text COLLATE "C",
 "lease_token" text COLLATE "C" NOT NULL DEFAULT '',
 "lease_until" double precision NOT NULL DEFAULT 0,
 "created_at" double precision NOT NULL,
 "updated_at" double precision NOT NULL,
 CONSTRAINT hg_6_15 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_6_16 UNIQUE (library_id, input_key)
);
CREATE TABLE em_content.governance_migration_audit(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "plan_sha256" text COLLATE "C" NOT NULL,
 "operation" text COLLATE "C" NOT NULL,
 "source_key" text COLLATE "C" NOT NULL,
 "state" text COLLATE "C" NOT NULL,
 "detail" text COLLATE "C" NOT NULL,
 "created_at" text COLLATE "C" NOT NULL,
 "updated_at" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_7_17 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_7_18 UNIQUE (library_id, plan_sha256, operation, source_key)
);
CREATE TABLE em_content.governance_module_backfill_runs(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "plan_sha256" text COLLATE "C" NOT NULL,
 "task_id" bigint NOT NULL,
 "state" text COLLATE "C" NOT NULL,
 "lease_token" text COLLATE "C" NOT NULL DEFAULT '',
 "lease_until" double precision NOT NULL DEFAULT 0,
 "output_hash" text COLLATE "C" NOT NULL DEFAULT '',
 "result_json" text COLLATE "C" NOT NULL DEFAULT '{{}}',
 CONSTRAINT hg_8_19 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_8_20 UNIQUE (library_id, plan_sha256, task_id)
);
CREATE TABLE em_content.governance_module_seed_runs(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "plan_sha256" text COLLATE "C" NOT NULL,
 "project" text COLLATE "C" NOT NULL,
 "name_key" text COLLATE "C" NOT NULL,
 "module_id" text COLLATE "C" NOT NULL,
 "result_hash" text COLLATE "C" NOT NULL,
 "proof_json" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_9_21 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_9_22 UNIQUE (library_id, plan_sha256, project, name_key)
);
CREATE TABLE em_content.governance_source_enrollment_audit(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "plan_sha256" text COLLATE "C" NOT NULL,
 "archive_id" bigint NOT NULL,
 "state" text COLLATE "C" NOT NULL,
 "before_json" text COLLATE "C" NOT NULL,
 "after_json" text COLLATE "C" NOT NULL,
 "created_at" text COLLATE "C" NOT NULL,
 "updated_at" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_10_23 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_10_24 UNIQUE (library_id, plan_sha256, archive_id)
);
CREATE TABLE em_content.governance_source_enrollments(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "archive_id" bigint NOT NULL,
 "identity_sha256" text COLLATE "C" NOT NULL,
 "origin" text COLLATE "C" NOT NULL,
 "plan_sha256" text COLLATE "C" NOT NULL,
 "revision" bigint NOT NULL,
 "created_at" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_11_25 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_11_26 UNIQUE (library_id, archive_id),
 CONSTRAINT hg_11_27 FOREIGN KEY (library_id, archive_id) REFERENCES em_content.session_archives(library_id, id),
 CONSTRAINT hg_alias_11 CHECK ("archive_id"=source_rowid)
);
CREATE TABLE em_content.organization_guidance(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "id" bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
 "scope" text COLLATE "C" NOT NULL DEFAULT 'batch',
 "project" text COLLATE "C" NOT NULL DEFAULT '',
 "condition" text COLLATE "C" NOT NULL DEFAULT '',
 "exceptions" text COLLATE "C" NOT NULL DEFAULT '',
 "guidance" text COLLATE "C" NOT NULL,
 "state" text COLLATE "C" NOT NULL DEFAULT 'reusable',
 "negative" bigint NOT NULL DEFAULT 0,
 "enabled" bigint NOT NULL DEFAULT 1,
 "revision" bigint NOT NULL DEFAULT 1,
 "source_text" text COLLATE "C" NOT NULL DEFAULT '',
 "source_task_id" bigint,
 "created_at" text COLLATE "C" NOT NULL,
 "updated_at" text COLLATE "C" NOT NULL,
 "projects" text COLLATE "C" NOT NULL DEFAULT '[]',
 "knowledge_scope" text COLLATE "C" NOT NULL DEFAULT 'project',
 "matching_mode" text COLLATE "C" NOT NULL DEFAULT 'phrase',
 CONSTRAINT hg_12_28 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_12_29 UNIQUE (library_id, id),
 CONSTRAINT hg_alias_12 CHECK ("id"=source_rowid),
 CONSTRAINT hg_check_12_scope CHECK ("scope" IN ('batch','future'))
);
CREATE TABLE em_content.organization_review_feedback(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "task_id" bigint NOT NULL,
 "digest" text COLLATE "C" NOT NULL,
 "unit_revision" text COLLATE "C" NOT NULL,
 "verdict" text COLLATE "C" NOT NULL,
 "updated_at" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_13_30 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_13_31 UNIQUE (library_id, task_id, digest),
 CONSTRAINT hg_check_13_verdict CHECK ("verdict" IN ('correct','incorrect'))
);
CREATE TABLE em_content.organization_review_jobs(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "id" text COLLATE "C",
 "status" text COLLATE "C" NOT NULL,
 "request" text COLLATE "C" NOT NULL,
 "base_revision" text COLLATE "C" NOT NULL,
 "judgments" text COLLATE "C" NOT NULL DEFAULT '{}',
 "error" text COLLATE "C" NOT NULL DEFAULT '',
 "processed" bigint NOT NULL DEFAULT 0,
 "total" bigint NOT NULL DEFAULT 0,
 "created_at" text COLLATE "C" NOT NULL,
 "updated_at" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_14_32 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_14_33 UNIQUE (library_id, id)
);
CREATE TABLE em_content.organization_scope_exclusions(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "source_key" text COLLATE "C" NOT NULL,
 "digest" text COLLATE "C" NOT NULL,
 "scope_key" text COLLATE "C" NOT NULL,
 "content_stamp" text COLLATE "C" NOT NULL,
 "revision" bigint NOT NULL DEFAULT 1,
 "updated_at" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_15_34 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_15_35 UNIQUE (library_id, source_key, digest, scope_key)
);
CREATE TABLE em_content.organization_settings(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "id" bigint NOT NULL,
 "auto_new" bigint NOT NULL DEFAULT 0,
 "activation_at" text COLLATE "C" NOT NULL DEFAULT '',
 "baseline_id" bigint NOT NULL DEFAULT 0,
 "revision" bigint NOT NULL DEFAULT 1,
 "updated_at" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_16_36 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_16_37 UNIQUE (library_id, id),
 CONSTRAINT hg_alias_16 CHECK ("id"=source_rowid),
 CONSTRAINT hg_settings_singleton CHECK (id=1)
);
CREATE TABLE em_content.organization_task_upgrades(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "task_id" bigint NOT NULL,
 "replacement_id" bigint,
 "error_code" text COLLATE "C" NOT NULL DEFAULT '',
 "updated_at" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_17_38 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_17_39 UNIQUE (library_id, task_id),
 CONSTRAINT hg_17_40 FOREIGN KEY (library_id, task_id) REFERENCES em_content.organization_tasks(library_id, id),
 CONSTRAINT hg_alias_17 CHECK ("task_id"=source_rowid)
);
CREATE TABLE em_content.private_governance_empty_terminals(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "plan_sha256" text COLLATE "C",
 "receipt" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_18_41 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_18_42 UNIQUE (library_id, plan_sha256)
);
CREATE TABLE em_content.private_governance_empty_terminals_v2(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "plan_sha256" text COLLATE "C",
 "receipt" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_19_43 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_19_44 UNIQUE (library_id, plan_sha256)
);
CREATE TABLE em_content.private_governance_source_upgrades(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "plan_sha256" text COLLATE "C",
 "detail" text COLLATE "C" NOT NULL,
 "created_at" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_20_45 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_20_46 UNIQUE (library_id, plan_sha256)
);
CREATE TABLE em_content.project_module_audit(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "audit_id" bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
 "module_id" text COLLATE "C" NOT NULL,
 "binding_id" bigint,
 "action" text COLLATE "C" NOT NULL,
 "actor" text COLLATE "C" NOT NULL,
 "before_json" text COLLATE "C" NOT NULL,
 "after_json" text COLLATE "C" NOT NULL,
 "created_at" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_21_47 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_21_48 UNIQUE (library_id, audit_id),
 CONSTRAINT hg_alias_21 CHECK ("audit_id"=source_rowid)
);
CREATE TABLE em_content.project_modules(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "module_id" text COLLATE "C",
 "project" text COLLATE "C" NOT NULL,
 "name" text COLLATE "C" NOT NULL,
 "name_key" text COLLATE "C" NOT NULL,
 "aliases" text COLLATE "C" NOT NULL DEFAULT '[]',
 "description" text COLLATE "C" NOT NULL DEFAULT '',
 "status" text COLLATE "C" NOT NULL,
 "revision" bigint NOT NULL DEFAULT 1,
 "created_at" text COLLATE "C" NOT NULL,
 "updated_at" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_22_49 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_22_50 UNIQUE (library_id, project, name_key),
 CONSTRAINT hg_22_51 UNIQUE (library_id, module_id),
 CONSTRAINT hg_22_52 FOREIGN KEY (library_id, project) REFERENCES em_content.context_project_registry(library_id, project),
 CONSTRAINT hg_check_22_status CHECK ("status" IN ('pending','active','inactive'))
);
CREATE TABLE em_content.project_module_bindings(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "binding_id" bigint GENERATED BY DEFAULT AS IDENTITY NOT NULL,
 "module_id" text COLLATE "C" NOT NULL,
 "source_kind" text COLLATE "C" NOT NULL,
 "source_key" text COLLATE "C" NOT NULL,
 "task_id" bigint,
 "unit_digest" text COLLATE "C" NOT NULL DEFAULT '',
 "capability_source_id" bigint,
 "source_revision" text COLLATE "C" NOT NULL,
 "unit_revision" bigint,
 "source_binding_revision" bigint,
 "source_fingerprint" text COLLATE "C" NOT NULL,
 "origin_anchor" text COLLATE "C" NOT NULL,
 "quote" text COLLATE "C" NOT NULL,
 "role" text COLLATE "C" NOT NULL,
 "chain_json" text COLLATE "C" NOT NULL DEFAULT '{}',
 "decision" text COLLATE "C" NOT NULL,
 "status" text COLLATE "C" NOT NULL,
 "revision" bigint NOT NULL DEFAULT 1,
 "created_at" text COLLATE "C" NOT NULL,
 "updated_at" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_23_53 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_23_54 UNIQUE (library_id, binding_id),
 CONSTRAINT hg_23_55 UNIQUE (library_id, module_id, source_key),
 CONSTRAINT hg_23_56 FOREIGN KEY (library_id, module_id) REFERENCES em_content.project_modules(library_id, module_id),
 CONSTRAINT hg_alias_23 CHECK ("binding_id"=source_rowid),
 CONSTRAINT hg_check_23_source_kind CHECK ("source_kind" IN ('unit','capability_source')),
 CONSTRAINT hg_check_23_decision CHECK ("decision" IN ('auto','manual','review')),
 CONSTRAINT hg_check_23_status CHECK ("status" IN ('active','inactive'))
);
CREATE TABLE em_content.project_module_names(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "project" text COLLATE "C" NOT NULL,
 "name_key" text COLLATE "C" NOT NULL,
 "module_id" text COLLATE "C" NOT NULL,
 CONSTRAINT hg_24_57 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_24_58 UNIQUE (library_id, project, name_key),
 CONSTRAINT hg_24_59 FOREIGN KEY (library_id, module_id) REFERENCES em_content.project_modules(library_id, module_id)
);
CREATE TABLE em_content.typed_ownership_repairs(
 library_id uuid NOT NULL,
 project_id uuid,
 source_rowid bigint NOT NULL,
 "plan_sha256" text COLLATE "C",
 "applied_at" text COLLATE "C" NOT NULL,
 "plan_json" text COLLATE "C" NOT NULL,
 "before_json" text COLLATE "C" NOT NULL,
 "after_json" text COLLATE "C" NOT NULL,
 "rolled_back_at" text COLLATE "C",
 CONSTRAINT hg_25_60 PRIMARY KEY (library_id, source_rowid),
 CONSTRAINT hg_25_61 UNIQUE (library_id, plan_sha256)
);
CREATE INDEX hg_idx_0 ON em_content.classification_results(library_id,reviewed,reviewed_correct);
CREATE INDEX hg_idx_1 ON em_content.classification_results(library_id,batch_id,predicted_project);
CREATE INDEX hg_idx_2 ON em_content.classification_results(library_id,batch_id);
CREATE UNIQUE INDEX hg_active_default ON em_content.history_context_project_workspace_bindings(library_id,workspace_fingerprint) WHERE state='active' AND is_default=1;
CREATE INDEX hg_idx_4 ON em_content.corrections(library_id,round);
CREATE INDEX hg_idx_5 ON em_content.organization_guidance(library_id,enabled,scope);
CREATE INDEX hg_idx_6 ON em_content.project_module_bindings(library_id,source_key);
CREATE TABLE em_content.history_governance_schema_migrations(
 version integer PRIMARY KEY CHECK(version=1),migration_name text NOT NULL UNIQUE,
 library_id uuid NOT NULL UNIQUE,applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE em_content.history_governance_runs(
 run_id uuid PRIMARY KEY,library_id uuid NOT NULL UNIQUE,parent_run_id uuid NOT NULL REFERENCES em_content.history_source_runs(run_id),
 manifest_hash text NOT NULL CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),manifest jsonb NOT NULL,
 loaded_counts jsonb NOT NULL,loaded_bytes bigint NOT NULL DEFAULT 0 CHECK(loaded_bytes>=0),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE em_content.history_governance_chunks(
 run_id uuid NOT NULL REFERENCES em_content.history_governance_runs(run_id),
 table_code text NOT NULL CHECK(table_code IN ('capability_provenance_repairs','classification_results','context_project_aliases','context_project_rollups','context_project_workspace_bindings','corrections','extraction_receipts','governance_migration_audit','governance_module_backfill_runs','governance_module_seed_runs','governance_source_enrollment_audit','governance_source_enrollments','organization_guidance','organization_review_feedback','organization_review_jobs','organization_scope_exclusions','organization_settings','organization_task_upgrades','private_governance_empty_terminals','private_governance_empty_terminals_v2','private_governance_source_upgrades','project_module_audit','project_modules','project_module_bindings','project_module_names','typed_ownership_repairs')),
 chunk_key text NOT NULL CHECK(chunk_key ~ '^c[0-9]{6}$'),input_hash text NOT NULL CHECK(input_hash ~ '^[0-9a-f]{64}$'),
 row_keys jsonb NOT NULL CHECK(jsonb_typeof(row_keys)='array'),row_count integer NOT NULL CHECK(row_count BETWEEN 1 AND 128),
 input_bytes integer NOT NULL CHECK(input_bytes BETWEEN 1 AND 16777216),committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(run_id,table_code,chunk_key),CHECK(jsonb_array_length(row_keys)=row_count)
);
CREATE TABLE em_content.history_governance_progress(
 run_id uuid PRIMARY KEY REFERENCES em_content.history_governance_runs(run_id),manifest_hash text NOT NULL CHECK(manifest_hash ~ '^[0-9a-f]{64}$'),
 stage text NOT NULL CHECK(stage IN ('loading','verifying','counting','verified')),
 next_load integer NOT NULL DEFAULT 0 CHECK(next_load>=0),next_verify integer NOT NULL DEFAULT 0 CHECK(next_verify>=0 AND next_verify<=next_load),
 generation bigint NOT NULL DEFAULT 0 CHECK(generation>=0),verify_generation bigint,
 count_table integer NOT NULL DEFAULT 0 CHECK(count_table BETWEEN 0 AND 26),count_rows bigint NOT NULL DEFAULT 0 CHECK(count_rows>=0),count_last jsonb
);
CREATE FUNCTION em_content.history_governance_touch() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $body$
BEGIN
 UPDATE em_content.history_governance_progress SET generation=generation+1;
 RETURN NULL;
END;
$body$;
REVOKE ALL ON FUNCTION em_content.history_governance_touch() FROM PUBLIC;
DO $policies$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY['capability_provenance_repairs','classification_results','history_context_project_aliases','context_project_rollups','history_context_project_workspace_bindings','corrections','extraction_receipts','governance_migration_audit','governance_module_backfill_runs','governance_module_seed_runs','governance_source_enrollment_audit','governance_source_enrollments','organization_guidance','organization_review_feedback','organization_review_jobs','organization_scope_exclusions','organization_settings','organization_task_upgrades','private_governance_empty_terminals','private_governance_empty_terminals_v2','private_governance_source_upgrades','project_module_audit','project_modules','project_module_bindings','project_module_names','typed_ownership_repairs'] LOOP
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE em_content.%I FORCE ROW LEVEL SECURITY',t);
   EXECUTE format('CREATE POLICY history_governance_owner ON em_content.%I TO %I USING(true) WITH CHECK(true)',t,current_user);
   EXECUTE format('CREATE POLICY history_governance_quarantine ON em_content.%I AS RESTRICTIVE TO PUBLIC USING(CURRENT_USER=%L::name) WITH CHECK(CURRENT_USER=%L::name)',t,current_user,current_user);
   EXECUTE format('CREATE TRIGGER immutable_security_scope BEFORE UPDATE ON em_content.%I FOR EACH ROW EXECUTE FUNCTION em_content.keep_security_scope()',t);
   EXECUTE format('REVOKE ALL ON em_content.%I FROM PUBLIC',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['history_governance_schema_migrations','history_governance_runs','history_governance_chunks','history_governance_progress'] LOOP
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE em_content.%I FORCE ROW LEVEL SECURITY',t);
   EXECUTE format('CREATE POLICY history_owner_only ON em_content.%I TO %I USING(true) WITH CHECK(true)',t,current_user);
   EXECUTE format('REVOKE ALL ON em_content.%I FROM PUBLIC',t);
 END LOOP;
 FOREACH t IN ARRAY ARRAY['capability_provenance_repairs','classification_results','history_context_project_aliases','context_project_rollups','history_context_project_workspace_bindings','corrections','extraction_receipts','governance_migration_audit','governance_module_backfill_runs','governance_module_seed_runs','governance_source_enrollment_audit','governance_source_enrollments','organization_guidance','organization_review_feedback','organization_review_jobs','organization_scope_exclusions','organization_settings','organization_task_upgrades','private_governance_empty_terminals','private_governance_empty_terminals_v2','private_governance_source_upgrades','project_module_audit','project_modules','project_module_bindings','project_module_names','typed_ownership_repairs','history_governance_chunks'] LOOP
   EXECUTE format('CREATE TRIGGER history_governance_generation AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON em_content.%I FOR EACH STATEMENT EXECUTE FUNCTION em_content.history_governance_touch()',t);
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ALWAYS TRIGGER history_governance_generation',t);
 END LOOP;
END;
$policies$;
INSERT INTO em_content.history_governance_schema_migrations(version,migration_name,library_id)
 VALUES(1,'001_history_governance_twenty_six',:'library_id'::uuid);
COMMIT;

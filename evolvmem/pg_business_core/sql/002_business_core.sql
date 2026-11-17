\set ON_ERROR_STOP on
-- Candidate only: apply to an empty synthetic pilot content database as owner.
BEGIN;
SELECT pg_catalog.set_config('em_candidate.expected_database', :'database_name', true);
SELECT pg_catalog.set_config('em_candidate.expected_owner', :'owner_role', true);
DO $guard$
BEGIN
  IF current_database() <> current_setting('em_candidate.expected_database')
     OR current_user <> current_setting('em_candidate.expected_owner')
     OR NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=current_user AND NOT rolcanlogin
          AND NOT rolsuper AND NOT rolbypassrls AND NOT rolcreatedb AND NOT rolcreaterole)
     OR NOT EXISTS (SELECT 1 FROM em_security.schema_migrations WHERE version=1 AND migration_name='001_content')
  THEN RAISE EXCEPTION 'business_migration_precondition'; END IF;
END $guard$;
-- Independent ledger: em_security version 2 belongs to the ACL projector.
CREATE TABLE em_content.business_schema_migrations (
 version integer PRIMARY KEY, migration_name text NOT NULL UNIQUE,
 applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE em_content.business_schema_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.business_schema_migrations FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_content.business_schema_migrations TO :"owner_role" USING(true) WITH CHECK(true);
REVOKE ALL ON em_content.business_schema_migrations FROM PUBLIC;
CREATE TABLE em_content.memories (
 library_id uuid NOT NULL, project_id uuid, id bigint GENERATED ALWAYS AS IDENTITY,
 key text NOT NULL, value text NOT NULL, attribute text NOT NULL DEFAULT '', tags text NOT NULL DEFAULT '',
 source_session text NOT NULL DEFAULT '', importance double precision NOT NULL DEFAULT 5,
 tier text NOT NULL DEFAULT 'normal', expires_at text, status text NOT NULL DEFAULT 'active',
 supersedes bigint, superseded_by bigint, access_count bigint NOT NULL DEFAULT 0, last_accessed text,
 created_at text NOT NULL, updated_at text NOT NULL, revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
 PRIMARY KEY(library_id,id),
 FOREIGN KEY(library_id,supersedes) REFERENCES em_content.memories(library_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(library_id,superseded_by) REFERENCES em_content.memories(library_id,id) DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX memories_active_key ON em_content.memories(library_id,key,id) WHERE status='active';
CREATE TABLE em_content.context_items (
 library_id uuid NOT NULL, project_id uuid, id bigint GENERATED ALWAYS AS IDENTITY,
 identity_key text NOT NULL, content_type text NOT NULL, project text NOT NULL DEFAULT '',
 scope text NOT NULL CHECK(scope IN ('global','project')), status text NOT NULL,
 tier text NOT NULL, tags jsonb NOT NULL DEFAULT '[]', importance double precision NOT NULL,
 confidence double precision NOT NULL, source_state text NOT NULL DEFAULT 'none',
 source_count bigint NOT NULL DEFAULT 0, success_count bigint NOT NULL DEFAULT 0,
 failure_count bigint NOT NULL DEFAULT 0, access_count bigint NOT NULL DEFAULT 0,
 last_accessed text,last_verified_at text,expires_at text,supersedes bigint,superseded_by bigint,
 created_at text NOT NULL,updated_at text NOT NULL, projects jsonb NOT NULL DEFAULT '[]',
 personal_general boolean NOT NULL DEFAULT false,experience_payload jsonb,
 effective_from text,effective_until text,occurred_at text,mentioned_at text,
 revision bigint NOT NULL DEFAULT 1 CHECK(revision>0), PRIMARY KEY(library_id,id),
 FOREIGN KEY(library_id,supersedes) REFERENCES em_content.context_items(library_id,id) DEFERRABLE INITIALLY DEFERRED,
 FOREIGN KEY(library_id,superseded_by) REFERENCES em_content.context_items(library_id,id) DEFERRABLE INITIALLY DEFERRED
);
CREATE UNIQUE INDEX context_one_active_identity ON em_content.context_items(library_id,identity_key,project,scope) WHERE status='active';
CREATE TABLE em_content.context_layers (
 library_id uuid NOT NULL,project_id uuid,item_id bigint NOT NULL,layer text NOT NULL CHECK(layer IN ('l0','l1','l2')),
 content text NOT NULL,content_hash text NOT NULL,generator text NOT NULL,created_at text NOT NULL,updated_at text NOT NULL,
 PRIMARY KEY(library_id,item_id,layer), FOREIGN KEY(library_id,item_id) REFERENCES em_content.context_items(library_id,id)
);
CREATE TABLE em_content.legacy_memory_migrations (
 library_id uuid NOT NULL,project_id uuid,legacy_memory_id bigint NOT NULL,context_item_id bigint NOT NULL,migrated_at text NOT NULL,
 PRIMARY KEY(library_id,legacy_memory_id),
 FOREIGN KEY(library_id,legacy_memory_id) REFERENCES em_content.memories(library_id,id),
 FOREIGN KEY(library_id,context_item_id) REFERENCES em_content.context_items(library_id,id)
);
CREATE TABLE em_content.context_sources (
 library_id uuid NOT NULL,project_id uuid,id bigint GENERATED ALWAYS AS IDENTITY,item_id bigint NOT NULL,
 archive_id bigint,source_kind text NOT NULL,source_ref text NOT NULL DEFAULT '',extraction_version text NOT NULL,created_at text NOT NULL,
 PRIMARY KEY(library_id,id), UNIQUE(library_id,item_id,source_kind,source_ref),
 FOREIGN KEY(library_id,item_id) REFERENCES em_content.context_items(library_id,id),
 CHECK(archive_id IS NULL AND source_kind='migration') -- richer evidence requires its future schema, never fabricate it
);
CREATE TABLE em_content.context_project_registry (
 library_id uuid NOT NULL,project_id uuid,project text NOT NULL,project_uuid uuid NOT NULL,
 status text NOT NULL DEFAULT 'active' CHECK(status IN ('active','archived')),display_name text NOT NULL DEFAULT '',
 revision bigint NOT NULL DEFAULT 1,created_at text NOT NULL,updated_at text NOT NULL,PRIMARY KEY(library_id,project),UNIQUE(library_id,project_uuid)
);
CREATE TABLE em_content.context_project_aliases (
 library_id uuid NOT NULL,project_id uuid,alias text NOT NULL,project text NOT NULL,revision bigint NOT NULL DEFAULT 1,
 created_at text NOT NULL,updated_at text NOT NULL,PRIMARY KEY(library_id,alias),
 FOREIGN KEY(library_id,project) REFERENCES em_content.context_project_registry(library_id,project)
);
CREATE TABLE em_content.context_project_workspace_bindings (
 library_id uuid NOT NULL,project_id uuid,workspace_fingerprint text NOT NULL,project text NOT NULL,
 state text NOT NULL CHECK(state IN ('candidate','active','revoked')),is_default boolean NOT NULL DEFAULT false,
 method text NOT NULL DEFAULT '',revision bigint NOT NULL DEFAULT 1,created_at text NOT NULL,updated_at text NOT NULL,
 PRIMARY KEY(library_id,workspace_fingerprint,project),
 FOREIGN KEY(library_id,project) REFERENCES em_content.context_project_registry(library_id,project)
);
CREATE UNIQUE INDEX project_one_default ON em_content.context_project_workspace_bindings(library_id,workspace_fingerprint) WHERE state='active' AND is_default;
CREATE TABLE em_content.context_project_resolutions (
 library_id uuid NOT NULL,project_id uuid,item_id bigint NOT NULL,resolution_state text NOT NULL,
 decision_source text NOT NULL DEFAULT 'automatic',review_state text NOT NULL,
 proposed_project text NOT NULL DEFAULT '',resolved_project text NOT NULL DEFAULT '',confidence text NOT NULL,
 method text NOT NULL,evidence_json jsonb NOT NULL DEFAULT '[]',resolver_version text NOT NULL,
 revision bigint NOT NULL DEFAULT 1,reviewed_at text,created_at text NOT NULL,updated_at text NOT NULL,
 PRIMARY KEY(library_id,item_id),FOREIGN KEY(library_id,item_id) REFERENCES em_content.context_items(library_id,id)
);
CREATE TABLE em_content.lan_write_requests (
 library_id uuid NOT NULL,project_id uuid,principal_id uuid NOT NULL,tool text NOT NULL,request_id text NOT NULL,
 payload_hash text NOT NULL CHECK(length(payload_hash)=64),result_json jsonb,created_at text NOT NULL,completed_at text,
 PRIMARY KEY(library_id,principal_id,tool,request_id), CHECK(length(request_id) BETWEEN 1 AND 200)
);
CREATE TABLE em_content.vector_outbox (
 library_id uuid NOT NULL,project_id uuid,id bigint GENERATED ALWAYS AS IDENTITY,
 index_kind text NOT NULL CHECK(index_kind IN ('legacy','context')),entity_id bigint NOT NULL,
 action text NOT NULL CHECK(action IN ('upsert','remove')),entity_revision bigint NOT NULL,
 content_hash text NOT NULL,model_contract text NOT NULL,status text NOT NULL DEFAULT 'pending' CHECK(status='pending'),
 created_at text NOT NULL,PRIMARY KEY(library_id,id),
 UNIQUE(library_id,index_kind,entity_id,entity_revision,action)
);
CREATE INDEX vector_pending ON em_content.vector_outbox(library_id,id);
CREATE FUNCTION em_content.keep_security_scope() RETURNS trigger LANGUAGE plpgsql
 SET search_path=pg_catalog AS $fn$
BEGIN
 IF OLD.library_id IS DISTINCT FROM NEW.library_id OR OLD.project_id IS DISTINCT FROM NEW.project_id
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='immutable_library_scope'; END IF;
 RETURN NEW;
END $fn$;
REVOKE ALL ON FUNCTION em_content.keep_security_scope() FROM PUBLIC;
ALTER TABLE em_content.memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.memories FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_owner ON em_content.memories TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_read ON em_content.memories FOR SELECT TO PUBLIC USING(em_security.allows(library_id,project_id,'read'));
CREATE POLICY authorized_insert ON em_content.memories FOR INSERT TO PUBLIC WITH CHECK(em_security.allows(library_id,project_id,'write'));
CREATE POLICY authorized_update ON em_content.memories FOR UPDATE TO PUBLIC USING(em_security.allows(library_id,project_id,'write')) WITH CHECK(em_security.allows(library_id,project_id,'write'));
CREATE TRIGGER immutable_security_scope BEFORE UPDATE ON em_content.memories FOR EACH ROW EXECUTE FUNCTION em_content.keep_security_scope();
REVOKE ALL ON em_content.memories FROM PUBLIC;
ALTER TABLE em_content.context_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.context_items FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_owner ON em_content.context_items TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_read ON em_content.context_items FOR SELECT TO PUBLIC USING(em_security.allows(library_id,project_id,'read'));
CREATE POLICY authorized_insert ON em_content.context_items FOR INSERT TO PUBLIC WITH CHECK(em_security.allows(library_id,project_id,'write'));
CREATE POLICY authorized_update ON em_content.context_items FOR UPDATE TO PUBLIC USING(em_security.allows(library_id,project_id,'write')) WITH CHECK(em_security.allows(library_id,project_id,'write'));
CREATE TRIGGER immutable_security_scope BEFORE UPDATE ON em_content.context_items FOR EACH ROW EXECUTE FUNCTION em_content.keep_security_scope();
REVOKE ALL ON em_content.context_items FROM PUBLIC;
ALTER TABLE em_content.context_layers ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.context_layers FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_owner ON em_content.context_layers TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_read ON em_content.context_layers FOR SELECT TO PUBLIC USING(em_security.allows(library_id,project_id,'read'));
CREATE POLICY authorized_insert ON em_content.context_layers FOR INSERT TO PUBLIC WITH CHECK(em_security.allows(library_id,project_id,'write'));
CREATE POLICY authorized_update ON em_content.context_layers FOR UPDATE TO PUBLIC USING(em_security.allows(library_id,project_id,'write')) WITH CHECK(em_security.allows(library_id,project_id,'write'));
CREATE TRIGGER immutable_security_scope BEFORE UPDATE ON em_content.context_layers FOR EACH ROW EXECUTE FUNCTION em_content.keep_security_scope();
REVOKE ALL ON em_content.context_layers FROM PUBLIC;
ALTER TABLE em_content.legacy_memory_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.legacy_memory_migrations FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_owner ON em_content.legacy_memory_migrations TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_read ON em_content.legacy_memory_migrations FOR SELECT TO PUBLIC USING(em_security.allows(library_id,project_id,'read'));
CREATE POLICY authorized_insert ON em_content.legacy_memory_migrations FOR INSERT TO PUBLIC WITH CHECK(em_security.allows(library_id,project_id,'write'));
CREATE POLICY authorized_update ON em_content.legacy_memory_migrations FOR UPDATE TO PUBLIC USING(em_security.allows(library_id,project_id,'write')) WITH CHECK(em_security.allows(library_id,project_id,'write'));
CREATE TRIGGER immutable_security_scope BEFORE UPDATE ON em_content.legacy_memory_migrations FOR EACH ROW EXECUTE FUNCTION em_content.keep_security_scope();
REVOKE ALL ON em_content.legacy_memory_migrations FROM PUBLIC;
ALTER TABLE em_content.context_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.context_sources FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_owner ON em_content.context_sources TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_read ON em_content.context_sources FOR SELECT TO PUBLIC USING(em_security.allows(library_id,project_id,'read'));
CREATE POLICY authorized_insert ON em_content.context_sources FOR INSERT TO PUBLIC WITH CHECK(em_security.allows(library_id,project_id,'write'));
CREATE POLICY authorized_update ON em_content.context_sources FOR UPDATE TO PUBLIC USING(em_security.allows(library_id,project_id,'write')) WITH CHECK(em_security.allows(library_id,project_id,'write'));
CREATE TRIGGER immutable_security_scope BEFORE UPDATE ON em_content.context_sources FOR EACH ROW EXECUTE FUNCTION em_content.keep_security_scope();
REVOKE ALL ON em_content.context_sources FROM PUBLIC;
ALTER TABLE em_content.context_project_registry ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.context_project_registry FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_owner ON em_content.context_project_registry TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_read ON em_content.context_project_registry FOR SELECT TO PUBLIC USING(em_security.allows(library_id,project_id,'read'));
CREATE POLICY authorized_insert ON em_content.context_project_registry FOR INSERT TO PUBLIC WITH CHECK(em_security.allows(library_id,project_id,'admin'));
CREATE POLICY authorized_update ON em_content.context_project_registry FOR UPDATE TO PUBLIC USING(em_security.allows(library_id,project_id,'admin')) WITH CHECK(em_security.allows(library_id,project_id,'admin'));
CREATE TRIGGER immutable_security_scope BEFORE UPDATE ON em_content.context_project_registry FOR EACH ROW EXECUTE FUNCTION em_content.keep_security_scope();
REVOKE ALL ON em_content.context_project_registry FROM PUBLIC;
ALTER TABLE em_content.context_project_aliases ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.context_project_aliases FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_owner ON em_content.context_project_aliases TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_read ON em_content.context_project_aliases FOR SELECT TO PUBLIC USING(em_security.allows(library_id,project_id,'read'));
CREATE POLICY authorized_insert ON em_content.context_project_aliases FOR INSERT TO PUBLIC WITH CHECK(em_security.allows(library_id,project_id,'admin'));
CREATE POLICY authorized_update ON em_content.context_project_aliases FOR UPDATE TO PUBLIC USING(em_security.allows(library_id,project_id,'admin')) WITH CHECK(em_security.allows(library_id,project_id,'admin'));
CREATE TRIGGER immutable_security_scope BEFORE UPDATE ON em_content.context_project_aliases FOR EACH ROW EXECUTE FUNCTION em_content.keep_security_scope();
REVOKE ALL ON em_content.context_project_aliases FROM PUBLIC;
ALTER TABLE em_content.context_project_workspace_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.context_project_workspace_bindings FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_owner ON em_content.context_project_workspace_bindings TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_read ON em_content.context_project_workspace_bindings FOR SELECT TO PUBLIC USING(em_security.allows(library_id,project_id,'read'));
CREATE POLICY authorized_insert ON em_content.context_project_workspace_bindings FOR INSERT TO PUBLIC WITH CHECK(em_security.allows(library_id,project_id,'admin'));
CREATE POLICY authorized_update ON em_content.context_project_workspace_bindings FOR UPDATE TO PUBLIC USING(em_security.allows(library_id,project_id,'admin')) WITH CHECK(em_security.allows(library_id,project_id,'admin'));
CREATE TRIGGER immutable_security_scope BEFORE UPDATE ON em_content.context_project_workspace_bindings FOR EACH ROW EXECUTE FUNCTION em_content.keep_security_scope();
REVOKE ALL ON em_content.context_project_workspace_bindings FROM PUBLIC;
ALTER TABLE em_content.context_project_resolutions ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.context_project_resolutions FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_owner ON em_content.context_project_resolutions TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_read ON em_content.context_project_resolutions FOR SELECT TO PUBLIC USING(em_security.allows(library_id,project_id,'read'));
CREATE POLICY authorized_insert ON em_content.context_project_resolutions FOR INSERT TO PUBLIC WITH CHECK(em_security.allows(library_id,project_id,'write'));
CREATE POLICY authorized_update ON em_content.context_project_resolutions FOR UPDATE TO PUBLIC USING(em_security.allows(library_id,project_id,'write')) WITH CHECK(em_security.allows(library_id,project_id,'write'));
CREATE TRIGGER immutable_security_scope BEFORE UPDATE ON em_content.context_project_resolutions FOR EACH ROW EXECUTE FUNCTION em_content.keep_security_scope();
REVOKE ALL ON em_content.context_project_resolutions FROM PUBLIC;
ALTER TABLE em_content.lan_write_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.lan_write_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_owner ON em_content.lan_write_requests TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_read ON em_content.lan_write_requests FOR SELECT TO PUBLIC USING(em_security.allows(library_id,project_id,'read'));
CREATE POLICY authorized_insert ON em_content.lan_write_requests FOR INSERT TO PUBLIC WITH CHECK(em_security.allows(library_id,project_id,'write'));
CREATE POLICY authorized_update ON em_content.lan_write_requests FOR UPDATE TO PUBLIC USING(em_security.allows(library_id,project_id,'write')) WITH CHECK(em_security.allows(library_id,project_id,'write'));
CREATE TRIGGER immutable_security_scope BEFORE UPDATE ON em_content.lan_write_requests FOR EACH ROW EXECUTE FUNCTION em_content.keep_security_scope();
REVOKE ALL ON em_content.lan_write_requests FROM PUBLIC;
ALTER TABLE em_content.vector_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.vector_outbox FORCE ROW LEVEL SECURITY;
CREATE POLICY migration_owner ON em_content.vector_outbox TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_read ON em_content.vector_outbox FOR SELECT TO PUBLIC USING(em_security.allows(library_id,project_id,'read'));
CREATE POLICY authorized_insert ON em_content.vector_outbox FOR INSERT TO PUBLIC WITH CHECK(em_security.allows(library_id,project_id,'write'));
CREATE POLICY authorized_update ON em_content.vector_outbox FOR UPDATE TO PUBLIC USING(em_security.allows(library_id,project_id,'write')) WITH CHECK(em_security.allows(library_id,project_id,'write'));
CREATE TRIGGER immutable_security_scope BEFORE UPDATE ON em_content.vector_outbox FOR EACH ROW EXECUTE FUNCTION em_content.keep_security_scope();
REVOKE ALL ON em_content.vector_outbox FROM PUBLIC;
-- Receipt payload/results are private to the authenticated principal, even when the library is shared.
CREATE POLICY receipt_principal_read ON em_content.lan_write_requests AS RESTRICTIVE FOR SELECT TO PUBLIC
 USING(principal_id=(SELECT c.principal_id FROM em_security.session_context() c));
CREATE POLICY receipt_principal_insert ON em_content.lan_write_requests AS RESTRICTIVE FOR INSERT TO PUBLIC
 WITH CHECK(principal_id=(SELECT c.principal_id FROM em_security.session_context() c));
CREATE POLICY receipt_principal_update ON em_content.lan_write_requests AS RESTRICTIVE FOR UPDATE TO PUBLIC
 USING(principal_id=(SELECT c.principal_id FROM em_security.session_context() c))
 WITH CHECK(principal_id=(SELECT c.principal_id FROM em_security.session_context() c));
REVOKE ALL ON ALL SEQUENCES IN SCHEMA em_content FROM PUBLIC;
INSERT INTO em_content.business_schema_migrations(version,migration_name) VALUES(1,'002_business_core_candidate');
COMMIT;

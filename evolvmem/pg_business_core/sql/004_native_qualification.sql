-- Native new-library qualification only. Never apply as history promotion.
-- Trusted psql variables: database_name, owner_role, library_id.
\set ON_ERROR_STOP on
SELECT current_database()=:'database_name' AND current_user=:'owner_role'
 AND EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
 WHERE r.rolname=current_user AND d.datname=current_database()
 AND NOT(r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb)) AS preflight_ok \gset
\if :preflight_ok
\else
 \quit 3
\endif
BEGIN;
SELECT pg_advisory_xact_lock(pg_catalog.hashtextextended('evolvmem.content.schema',0));
DO $guard$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM em_security.schema_migrations WHERE version=4)
  OR NOT EXISTS(SELECT 1 FROM em_content.business_schema_migrations WHERE version=1)
  OR to_regclass('em_content.lexical_schema_migrations') IS NULL
  OR to_regclass('em_content.history_schema_migrations') IS NOT NULL
  OR to_regclass('em_content.native_qualification_migrations') IS NOT NULL THEN
  RAISE EXCEPTION 'native_qualification_migration_precondition'; END IF;
END; $guard$;
CREATE TABLE em_content.native_qualification_migrations(
 version integer PRIMARY KEY CHECK(version=1),mapping text NOT NULL CHECK(mapping='native-v1')
);
CREATE TABLE em_security.recall_readiness(
 library_id uuid PRIMARY KEY REFERENCES em_security.library_state(library_id),
 schema_version integer NOT NULL CHECK(schema_version=1),mode text NOT NULL CHECK(mode='native-v1'),
 enabled boolean NOT NULL DEFAULT false,revision bigint NOT NULL DEFAULT 1 CHECK(revision>0)
);
INSERT INTO em_security.recall_readiness(library_id,schema_version,mode)
 SELECT library_id,1,'native-v1' FROM em_security.library_state WHERE library_id=:'library_id'::uuid;
DO $seed$ BEGIN
 IF (SELECT count(*) FROM em_security.recall_readiness)<>1 THEN RAISE EXCEPTION 'native_library_missing'; END IF;
END; $seed$;
ALTER TABLE em_content.context_sources DROP CONSTRAINT context_sources_check;
ALTER TABLE em_content.context_sources ADD CONSTRAINT native_context_source_kind CHECK(
 source_kind IN('migration','manual','session','organization','extraction','context_reference',
 'tool_result','user_confirmation','historical_record') AND (archive_id IS NULL OR archive_id>0));

CREATE TABLE em_content.native_learning_memories(
 library_id uuid NOT NULL,project_id uuid,item_id bigint NOT NULL,category text NOT NULL,payload text NOT NULL,
 revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),PRIMARY KEY(library_id,item_id),
 FOREIGN KEY(library_id,item_id) REFERENCES em_content.context_items(library_id,id),CHECK(octet_length(payload)<=32768)
);
CREATE TABLE em_content.native_human_decisions(
 library_id uuid NOT NULL,project_id uuid,item_id bigint NOT NULL,
 decision text NOT NULL CHECK(decision IN('accepted','rejected','withdrawn')),
 item_revision bigint NOT NULL CHECK(item_revision>0),item_content_hash text NOT NULL CHECK(item_content_hash~'^[0-9a-f]{64}$'),
 actor_principal_id uuid NOT NULL,revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
 PRIMARY KEY(library_id,item_id),FOREIGN KEY(library_id,item_id) REFERENCES em_content.context_items(library_id,id)
);
CREATE TABLE em_content.native_source_states(
 library_id uuid NOT NULL,project_id uuid,item_id bigint NOT NULL,source_id bigint NOT NULL,
 state text NOT NULL CHECK(state IN('available','withdrawn','excluded','unavailable')),
 verification text NOT NULL CHECK(verification IN('verified','unverified')),
 source_kind text NOT NULL,source_ref text NOT NULL,extraction_version text NOT NULL,archive_id bigint,
 source_revision bigint NOT NULL CHECK(source_revision>0),content_hash text NOT NULL CHECK(content_hash~'^[0-9a-f]{64}$'),
 revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),PRIMARY KEY(library_id,source_id),
 FOREIGN KEY(library_id,source_id) REFERENCES em_content.context_sources(library_id,id),
 FOREIGN KEY(library_id,item_id) REFERENCES em_content.context_items(library_id,id)
);
CREATE INDEX native_source_states_item ON em_content.native_source_states(library_id,item_id);
CREATE TABLE em_content.native_unit_derivations(
 library_id uuid NOT NULL,project_id uuid,item_id bigint NOT NULL,unit_task_id text NOT NULL,unit_digest text NOT NULL,
 source_id bigint NOT NULL,source_key text NOT NULL,source_revision bigint NOT NULL CHECK(source_revision>0),
 status text NOT NULL CHECK(status IN('active','superseded')),
 decision text NOT NULL CHECK(decision IN('auto','manual','rejected','pending')),
 disposition text NOT NULL CHECK(disposition IN('keep','history_only','withdrawn')),
 project text NOT NULL,projects jsonb NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(projects)='array'),
 knowledge_scope text NOT NULL CHECK(knowledge_scope IN('project','global')),
 conversation_archives jsonb NOT NULL DEFAULT '[]' CHECK(jsonb_typeof(conversation_archives)='array'),
 basis_source_id bigint,basis_source_revision bigint,basis_content_hash text,
 revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),PRIMARY KEY(library_id,item_id,unit_task_id,unit_digest),
 FOREIGN KEY(library_id,item_id) REFERENCES em_content.context_items(library_id,id),
 FOREIGN KEY(library_id,source_id) REFERENCES em_content.context_sources(library_id,id),
 FOREIGN KEY(library_id,basis_source_id) REFERENCES em_content.context_sources(library_id,id),
 CHECK((basis_source_id IS NULL AND basis_source_revision IS NULL AND basis_content_hash IS NULL)
  OR(basis_source_id IS NOT NULL AND basis_source_revision>0 AND basis_content_hash~'^[0-9a-f]{64}$'))
);
CREATE TABLE em_content.native_source_evidence(
 library_id uuid NOT NULL,project_id uuid,item_id bigint NOT NULL,evidence_id text NOT NULL,source_id bigint NOT NULL,
 task_id text NOT NULL CHECK(length(task_id) BETWEEN 1 AND 500),event_id text NOT NULL CHECK(length(event_id) BETWEEN 1 AND 500),
 event_order bigint GENERATED ALWAYS AS IDENTITY,conditions jsonb NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(conditions)='object'),
 verification_level text NOT NULL CHECK(verification_level IN('technical','business','user_confirmed')),
 item_revision bigint NOT NULL CHECK(item_revision>0),source_revision bigint NOT NULL CHECK(source_revision>0),
 item_content_hash text NOT NULL CHECK(item_content_hash~'^[0-9a-f]{64}$'),
 source_hash text NOT NULL CHECK(source_hash~'^[0-9a-f]{64}$'),
 state text NOT NULL CHECK(state IN('active','withdrawn')),outcome text NOT NULL CHECK(outcome IN('success','confirmed','failure','contradicted','inapplicable','unknown','used')),
 verification text NOT NULL CHECK(verification IN('verified','unverified')),role text NOT NULL CHECK(role IN('user','tool','assistant')),
 revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),PRIMARY KEY(library_id,item_id,evidence_id),UNIQUE(library_id,event_order),
 FOREIGN KEY(library_id,item_id) REFERENCES em_content.context_items(library_id,id),
 FOREIGN KEY(library_id,source_id) REFERENCES em_content.context_sources(library_id,id)
);
CREATE TABLE em_content.native_project_rollups(
 library_id uuid NOT NULL,project_id uuid,item_id bigint NOT NULL,source_set_hash text NOT NULL,
 status text NOT NULL CHECK(status IN('active','withdrawn')),revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
 PRIMARY KEY(library_id,item_id),FOREIGN KEY(library_id,item_id) REFERENCES em_content.context_items(library_id,id)
);

-- Sidecar metadata cannot silently point into another project, even when the
-- provisioning owner can see both. Runtime has SELECT only, no proof-writing API.
CREATE FUNCTION em_content.check_native_qualification_scope() RETURNS trigger LANGUAGE plpgsql
 SET search_path=pg_catalog,em_content AS $fn$
DECLARE parent_project uuid; parent_item bigint; source_row em_content.context_sources%ROWTYPE;
BEGIN
 IF TG_OP='UPDATE' AND (OLD.library_id IS DISTINCT FROM NEW.library_id OR OLD.project_id IS DISTINCT FROM NEW.project_id
  OR OLD.item_id IS DISTINCT FROM NEW.item_id) THEN RAISE EXCEPTION 'immutable_native_scope'; END IF;
 SELECT i.project_id INTO parent_project FROM em_content.context_items i WHERE i.library_id=NEW.library_id AND i.id=NEW.item_id;
 IF NOT FOUND OR parent_project IS DISTINCT FROM NEW.project_id THEN RAISE EXCEPTION 'native_item_scope_mismatch'; END IF;
 IF TG_TABLE_NAME IN('native_source_states','native_unit_derivations','native_source_evidence') THEN
  SELECT s.project_id,s.item_id INTO parent_project,parent_item FROM em_content.context_sources s
   WHERE s.library_id=NEW.library_id AND s.id=NEW.source_id;
  IF NOT FOUND OR parent_project IS DISTINCT FROM NEW.project_id OR parent_item<>NEW.item_id
   THEN RAISE EXCEPTION 'native_source_scope_mismatch'; END IF;
 END IF;
 IF TG_TABLE_NAME='native_source_states' THEN
  SELECT * INTO source_row FROM em_content.context_sources WHERE library_id=NEW.library_id AND id=NEW.source_id;
  IF NEW.source_kind IS DISTINCT FROM source_row.source_kind OR NEW.source_ref IS DISTINCT FROM source_row.source_ref
   OR NEW.extraction_version IS DISTINCT FROM source_row.extraction_version OR NEW.archive_id IS DISTINCT FROM source_row.archive_id
   THEN RAISE EXCEPTION 'native_source_descriptor_mismatch'; END IF;
 END IF;
 IF TG_TABLE_NAME='native_unit_derivations' THEN
  IF NEW.basis_source_id IS NOT NULL THEN
   SELECT s.project_id INTO parent_project FROM em_content.context_sources s WHERE s.library_id=NEW.library_id AND s.id=NEW.basis_source_id;
   IF NOT FOUND OR parent_project IS DISTINCT FROM NEW.project_id THEN RAISE EXCEPTION 'native_basis_scope_mismatch'; END IF;
  END IF;
 END IF;
 IF TG_OP='UPDATE' THEN
  NEW.revision:=OLD.revision+1;
  IF TG_TABLE_NAME='native_source_evidence' THEN
   NEW.event_order:=nextval('em_content.native_source_evidence_event_order_seq'::regclass);
  END IF;
 END IF;
 RETURN NEW;
END; $fn$;
REVOKE ALL ON FUNCTION em_content.check_native_qualification_scope() FROM PUBLIC;
ALTER TABLE em_content.native_learning_memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.native_learning_memories FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_content.native_learning_memories TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_read ON em_content.native_learning_memories FOR SELECT TO PUBLIC USING(em_security.allows(library_id,project_id,'read'));
CREATE TRIGGER native_scope BEFORE INSERT OR UPDATE ON em_content.native_learning_memories FOR EACH ROW EXECUTE FUNCTION em_content.check_native_qualification_scope();
REVOKE ALL ON em_content.native_learning_memories FROM PUBLIC;
ALTER TABLE em_content.native_human_decisions ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.native_human_decisions FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_content.native_human_decisions TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_read ON em_content.native_human_decisions FOR SELECT TO PUBLIC USING(em_security.allows(library_id,project_id,'read'));
CREATE TRIGGER native_scope BEFORE INSERT OR UPDATE ON em_content.native_human_decisions FOR EACH ROW EXECUTE FUNCTION em_content.check_native_qualification_scope();
REVOKE ALL ON em_content.native_human_decisions FROM PUBLIC;
ALTER TABLE em_content.native_source_states ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.native_source_states FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_content.native_source_states TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_read ON em_content.native_source_states FOR SELECT TO PUBLIC USING(em_security.allows(library_id,project_id,'read'));
CREATE TRIGGER native_scope BEFORE INSERT OR UPDATE ON em_content.native_source_states FOR EACH ROW EXECUTE FUNCTION em_content.check_native_qualification_scope();
REVOKE ALL ON em_content.native_source_states FROM PUBLIC;
ALTER TABLE em_content.native_unit_derivations ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.native_unit_derivations FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_content.native_unit_derivations TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_read ON em_content.native_unit_derivations FOR SELECT TO PUBLIC USING(em_security.allows(library_id,project_id,'read'));
CREATE TRIGGER native_scope BEFORE INSERT OR UPDATE ON em_content.native_unit_derivations FOR EACH ROW EXECUTE FUNCTION em_content.check_native_qualification_scope();
REVOKE ALL ON em_content.native_unit_derivations FROM PUBLIC;
ALTER TABLE em_content.native_source_evidence ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.native_source_evidence FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_content.native_source_evidence TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_read ON em_content.native_source_evidence FOR SELECT TO PUBLIC USING(em_security.allows(library_id,project_id,'read'));
CREATE TRIGGER native_scope BEFORE INSERT OR UPDATE ON em_content.native_source_evidence FOR EACH ROW EXECUTE FUNCTION em_content.check_native_qualification_scope();
REVOKE ALL ON em_content.native_source_evidence FROM PUBLIC;
ALTER TABLE em_content.native_project_rollups ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.native_project_rollups FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_content.native_project_rollups TO :"owner_role" USING(true) WITH CHECK(true);
CREATE POLICY authorized_read ON em_content.native_project_rollups FOR SELECT TO PUBLIC USING(em_security.allows(library_id,project_id,'read'));
CREATE TRIGGER native_scope BEFORE INSERT OR UPDATE ON em_content.native_project_rollups FOR EACH ROW EXECUTE FUNCTION em_content.check_native_qualification_scope();
REVOKE ALL ON em_content.native_project_rollups FROM PUBLIC;
ALTER TABLE em_security.recall_readiness ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_security.recall_readiness FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_security.recall_readiness TO :"owner_role" USING(true) WITH CHECK(true);
REVOKE ALL ON em_security.recall_readiness FROM PUBLIC;
ALTER TABLE em_content.native_qualification_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.native_qualification_migrations FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_content.native_qualification_migrations TO :"owner_role" USING(true) WITH CHECK(true);
REVOKE ALL ON em_content.native_qualification_migrations FROM PUBLIC;

CREATE FUNCTION em_security.set_native_recall_ready(p_library uuid,p_enabled boolean,p_expected_revision bigint)
RETURNS bigint LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,em_security AS $fn$
DECLARE result bigint;
BEGIN
 -- No grant is ever made to runtime: only the actual NOLOGIN schema owner has
 -- metadata UPDATE permission. The control lease remains the caller's duty.
 IF p_enabled IS NULL OR p_expected_revision IS NULL OR p_expected_revision<1 THEN RAISE EXCEPTION 'invalid_readiness_update'; END IF;
 IF p_enabled AND (to_regclass('em_content.history_schema_migrations') IS NOT NULL
  OR NOT EXISTS(SELECT 1 FROM em_content.native_qualification_migrations WHERE version=1 AND mapping='native-v1')
  OR NOT EXISTS(SELECT 1 FROM em_content.lexical_schema_migrations WHERE version=1 AND library_id=p_library)
  OR NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='em_content.context_layers'::regclass
   AND tgname='context_layers_hash_valid' AND tgenabled='O' AND NOT tgisinternal
   AND tgfoid=to_regprocedure('em_content.assert_context_layer_hash()')))
  THEN RAISE EXCEPTION 'native_readiness_unavailable'; END IF;
 UPDATE em_security.recall_readiness SET enabled=p_enabled,revision=revision+1
 WHERE library_id=p_library AND revision=p_expected_revision AND mode='native-v1' AND schema_version=1
 RETURNING revision INTO result;
 IF NOT FOUND THEN RAISE EXCEPTION 'readiness_revision_conflict'; END IF;
 RETURN result;
END; $fn$;
REVOKE ALL ON FUNCTION em_security.set_native_recall_ready(uuid,boolean,bigint) FROM PUBLIC;

CREATE FUNCTION em_security.recall_context(p_library uuid,p_project uuid)
RETURNS TABLE(schema_version integer,mode text,policy_epoch bigint,route_epoch bigint,readiness_revision bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security AS $fn$
 SELECT r.schema_version,r.mode,c.policy_epoch,c.route_epoch,r.revision
 FROM em_security.session_context() c JOIN em_security.recall_readiness r USING(library_id)
 WHERE c.library_id=p_library AND r.enabled AND r.mode='native-v1' AND r.schema_version=1
  AND current_setting('transaction_isolation')='repeatable read' AND current_setting('transaction_read_only')='on'
  AND em_security.allows(p_library,p_project,'read')
  AND to_regclass('em_content.history_schema_migrations') IS NULL
  AND EXISTS(SELECT 1 FROM em_content.native_qualification_migrations WHERE version=1 AND mapping='native-v1')
  AND EXISTS(SELECT 1 FROM em_content.lexical_schema_migrations WHERE version=1 AND library_id=p_library)
  AND EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid='em_content.context_layers'::regclass
   AND tgname='context_layers_hash_valid' AND tgenabled='O' AND NOT tgisinternal
   AND tgfoid=to_regprocedure('em_content.assert_context_layer_hash()'))
$fn$;
REVOKE ALL ON FUNCTION em_security.recall_context(uuid,uuid) FROM PUBLIC;

-- Invoke for newly provisioned login roles too. Existing safe bindings are
-- granted now; this function itself stays owner-only and cannot create roles.
CREATE FUNCTION em_security.provision_native_recall_reader(p_role name) RETURNS void
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,em_security AS $fn$
DECLARE t text;
BEGIN
 IF NOT em_security._runtime_role_safe(p_role) OR NOT EXISTS(SELECT 1 FROM em_security.principal_bindings WHERE login_role=p_role)
  THEN RAISE EXCEPTION 'unsafe_native_reader'; END IF;
 FOREACH t IN ARRAY ARRAY['native_learning_memories','native_human_decisions','native_source_states',
  'native_unit_derivations','native_source_evidence','native_project_rollups'] LOOP
  EXECUTE format('GRANT SELECT ON em_content.%I TO %I',t,p_role);
 END LOOP;
 EXECUTE format('GRANT EXECUTE ON FUNCTION em_security.recall_context(uuid,uuid) TO %I',p_role);
END; $fn$;
REVOKE ALL ON FUNCTION em_security.provision_native_recall_reader(name) FROM PUBLIC;
DO $readers$ DECLARE r name; BEGIN
 FOR r IN SELECT login_role FROM em_security.principal_bindings ORDER BY login_role LOOP
  PERFORM em_security.provision_native_recall_reader(r);
 END LOOP;
END; $readers$;
INSERT INTO em_content.native_qualification_migrations(version,mapping) VALUES(1,'native-v1');
-- Explicit activation is separate: SELECT em_security.set_native_recall_ready(library_id,true,1).
COMMIT;

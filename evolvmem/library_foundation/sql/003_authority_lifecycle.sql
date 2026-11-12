-- Candidate only. Trusted psql variables: database_name owner_role library_id projector_role.
\set ON_ERROR_STOP on
SELECT current_database()=:'database_name' AND current_user=:'owner_role'
 AND EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
 WHERE r.rolname=current_user AND d.datname=current_database()
 AND NOT(r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb))
 AND EXISTS(SELECT 1 FROM em_security.projector_services WHERE login_role=:'projector_role'
 AND enabled AND library_id=:'library_id'::uuid) AS preflight_ok \gset
\if :preflight_ok
\else
 \quit 3
\endif
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('evolvmem.content.schema',0));
DO $guard$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM em_security.schema_migrations WHERE version=4)
 OR EXISTS(SELECT 1 FROM em_security.schema_migrations WHERE version>=5)
 OR to_regclass('em_security.history_readiness') IS NULL
 OR to_regclass('em_security.history_membership_state') IS NULL
 OR NOT EXISTS(SELECT 1 FROM em_security.library_state WHERE singleton AND kind='legacy'
 AND authority_state='pg_staging' AND acl_state='updating')
 THEN RAISE EXCEPTION 'invalid_authority_content_schema'; END IF;
END $guard$;
CREATE TABLE em_security.authority_applications(
 operation_id uuid PRIMARY KEY,library_id uuid NOT NULL REFERENCES em_security.library_state(library_id),
 plan_json jsonb NOT NULL,plan_hash bytea NOT NULL CHECK(octet_length(plan_hash)=32),prepared_txid bigint NOT NULL,
 state text NOT NULL CHECK(state IN('prepared','applied')),receipt_json jsonb,receipt_hash bytea,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK(sha256(convert_to(plan_json::text,'UTF8'))=plan_hash),
 CHECK((state='prepared' AND receipt_json IS NULL AND receipt_hash IS NULL) OR
 (state='applied' AND receipt_json IS NOT NULL AND receipt_hash IS NOT NULL AND octet_length(receipt_hash)=32
 AND sha256(convert_to(receipt_json::text,'UTF8'))=receipt_hash))
);
CREATE UNIQUE INDEX authority_one_preparation ON em_security.authority_applications(library_id);
ALTER TABLE em_security.authority_applications ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_security.authority_applications FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_security.authority_applications TO :"owner_role" USING(true) WITH CHECK(true);
REVOKE ALL ON em_security.authority_applications FROM PUBLIC;
CREATE FUNCTION em_security._authority_preparation_commit_guard() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,em_security AS $fn$
BEGIN
 -- Query current row, NOT the stale NEW state from the INSERT event.
 IF NOT EXISTS(SELECT 1 FROM em_security.authority_applications a WHERE a.operation_id=NEW.operation_id
 AND a.state='applied' AND a.prepared_txid=txid_current() AND a.receipt_json IS NOT NULL AND a.receipt_hash IS NOT NULL)
 THEN RAISE EXCEPTION USING ERRCODE='23514',MESSAGE='authority_preparation_incomplete'; END IF;
 RETURN NULL;
END $fn$;
CREATE CONSTRAINT TRIGGER authority_preparation_complete AFTER INSERT OR UPDATE ON em_security.authority_applications
 DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION em_security._authority_preparation_commit_guard();
ALTER TABLE em_security.authority_applications ENABLE ALWAYS TRIGGER authority_preparation_complete;
CREATE FUNCTION em_security._authority_plan(p_text text,p_hash bytea) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $fn$
DECLARE p jsonb;i jsonb;
BEGIN
 IF p_text IS NULL OR octet_length(p_text)>2097152 OR p_hash IS NULL OR octet_length(p_hash)<>32
 OR sha256(convert_to(p_text,'UTF8'))<>p_hash THEN RAISE EXCEPTION 'authority_plan_hash_invalid'; END IF;
 p:=p_text::jsonb;
 IF jsonb_typeof(p) IS DISTINCT FROM 'object' OR p::text<>p_text OR NOT(p ?& ARRAY['version','operation_id','library_id','action',
 'connection_ref','owner_principal_id','kind','device_id','project_id','expected_route_epoch','expected_policy_epoch',
 'target_route_epoch','target_policy_epoch','payload_hash','evaluation_at','acl_plan_text','acl_plan_hash'])
 OR (SELECT count(*) FROM jsonb_object_keys(p))<>17 OR p->>'version' IS DISTINCT FROM '1'
 OR EXISTS(SELECT 1 FROM jsonb_each(p) x WHERE x.key IN('version','expected_route_epoch','expected_policy_epoch','target_route_epoch','target_policy_epoch') AND jsonb_typeof(x.value)<>'number')
 OR EXISTS(SELECT 1 FROM jsonb_each(p) x WHERE x.key NOT IN('version','expected_route_epoch','expected_policy_epoch','target_route_epoch','target_policy_epoch','device_id','project_id') AND jsonb_typeof(x.value)<>'string')
 OR p->>'action' IS DISTINCT FROM 'activate' OR p->>'kind' IS DISTINCT FROM 'legacy'
 OR p->'device_id' IS DISTINCT FROM 'null'::jsonb OR p->'project_id' IS DISTINCT FROM 'null'::jsonb
 OR (p->>'operation_id')::uuid IS NULL OR (p->>'library_id')::uuid IS NULL OR (p->>'owner_principal_id')::uuid IS NULL
 OR coalesce(p->>'connection_ref','')!~'^[A-Za-z][A-Za-z0-9_.-]{0,127}$'
 OR (p->>'expected_route_epoch')::bigint IS NULL OR (p->>'expected_route_epoch')::bigint NOT BETWEEN 1 AND 9223372036854775806
 OR (p->>'expected_policy_epoch')::bigint IS NULL OR (p->>'expected_policy_epoch')::bigint NOT BETWEEN 1 AND 9223372036854775806
 OR (p->>'target_route_epoch')::bigint IS DISTINCT FROM (p->>'expected_route_epoch')::bigint+1
 OR (p->>'target_policy_epoch')::bigint IS DISTINCT FROM (p->>'expected_policy_epoch')::bigint+1
 OR coalesce(p->>'payload_hash','')!~'^[0-9a-f]{64}$' OR (p->>'evaluation_at')::timestamptz IS NULL
 OR NOT isfinite((p->>'evaluation_at')::timestamptz)
 OR jsonb_typeof(p->'acl_plan_text') IS DISTINCT FROM 'string'
 OR coalesce(p->>'acl_plan_hash','')!~'^[0-9a-f]{64}$'
 OR encode(sha256(convert_to(p->>'acl_plan_text','UTF8')),'hex') IS DISTINCT FROM p->>'acl_plan_hash'
 THEN RAISE EXCEPTION 'authority_plan_invalid'; END IF;
 i:=(p->>'acl_plan_text')::jsonb;
 IF i::text IS DISTINCT FROM p->>'acl_plan_text' OR jsonb_typeof(i) IS DISTINCT FROM 'object'
 OR NOT(i ?& ARRAY['version','operation_id','library_id','connection_ref','expected_policy_epoch','target_policy_epoch','route_epoch',
 'kind','device_id','project_id','owner_principal_id','payload_hash','evaluation_at','acls','credentials','projects','project_acls'])
 OR (SELECT count(*) FROM jsonb_object_keys(i))<>17 OR i->>'version' IS DISTINCT FROM '2'
 OR (i-ARRAY['version','expected_policy_epoch','target_policy_epoch','route_epoch','acls','credentials','projects','project_acls'])
 IS DISTINCT FROM (p-ARRAY['version','action','expected_route_epoch','expected_policy_epoch','target_route_epoch','target_policy_epoch','acl_plan_text','acl_plan_hash'])
 OR i->'expected_policy_epoch' IS DISTINCT FROM p->'expected_policy_epoch'
 OR i->'target_policy_epoch' IS DISTINCT FROM p->'target_policy_epoch' OR i->'route_epoch' IS DISTINCT FROM p->'target_route_epoch'
 OR jsonb_typeof(i->'acls') IS DISTINCT FROM 'array' OR jsonb_typeof(i->'credentials') IS DISTINCT FROM 'array'
 OR jsonb_typeof(i->'projects') IS DISTINCT FROM 'array' OR jsonb_typeof(i->'project_acls') IS DISTINCT FROM 'array'
 THEN RAISE EXCEPTION 'authority_inner_plan_mismatch'; END IF;
 IF jsonb_array_length(i->'acls')>256 OR jsonb_array_length(i->'credentials')>1024
 OR jsonb_array_length(i->'projects')>2048 OR jsonb_array_length(i->'project_acls')>8192 THEN RAISE EXCEPTION 'authority_projection_too_large'; END IF;
 RETURN p;
END $fn$;
CREATE FUNCTION em_security._authority_readiness(p_library uuid,p_applied boolean) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE r em_security.history_readiness;m em_security.history_membership_state;e em_content.history_catchup_epochs;
 l em_content.history_catchup_latest;s em_security.library_state;ns text;raw jsonb:='{}'::jsonb;d text;t text;v jsonb;n bigint;
BEGIN
 SELECT * INTO s FROM em_security.library_state WHERE library_id=p_library AND singleton FOR UPDATE;
 IF NOT FOUND OR s.kind<>'legacy' THEN RAISE EXCEPTION 'authority_legacy_required'; END IF;
 SELECT * INTO r FROM em_security.history_readiness WHERE library_id=p_library FOR UPDATE;
 IF NOT FOUND OR r.schema_version<>1 OR r.mode<>'history-preserved-v1' THEN RAISE EXCEPTION 'authority_history_readiness_required'; END IF;
 SELECT * INTO m FROM em_security.history_membership_state WHERE library_id=p_library FOR SHARE;
 IF NOT FOUND OR m.dirty OR m.mapping_revision<1 OR coalesce(m.membership_hash,'')!~'^[0-9a-f]{64}$'
 THEN RAISE EXCEPTION 'authority_history_membership_required'; END IF;
 SELECT * INTO l FROM em_content.history_catchup_latest WHERE library_id=p_library FOR SHARE;
 IF NOT FOUND THEN RAISE EXCEPTION 'authority_catchup_complete_required'; END IF;
 SELECT * INTO e FROM em_content.history_catchup_epochs WHERE library_id=p_library AND epoch_id=l.epoch_id FOR SHARE;
 IF NOT FOUND OR e.state<>'complete' OR e.completion_hash IS DISTINCT FROM l.completion_hash
 OR e.generation IS DISTINCT FROM l.generation OR coalesce(e.completion_hash,'')!~'^[0-9a-f]{64}$'
 OR m.completion_epoch IS DISTINCT FROM e.epoch_id OR m.completion_hash IS DISTINCT FROM e.completion_hash
 OR EXISTS(SELECT 1 FROM em_content.history_catchup_epochs WHERE library_id=p_library AND state<>'complete')
 OR coalesce(e.manifest->>'new_seal','')!~'^[0-9a-f]{64}$' THEN RAISE EXCEPTION 'authority_catchup_proof_changed'; END IF;
 ns:=e.manifest->>'namespace';
 IF coalesce(ns,'') NOT IN('owner','kane','public') THEN RAISE EXCEPTION 'authority_namespace_invalid'; END IF;
 FOR d,t IN SELECT * FROM (VALUES('core','history_batch_progress'),('sources','history_source_batch_progress'),
 ('organization','history_organization_progress'),('capture','history_capture_progress'),('governance','history_governance_progress'),
 ('knowledge','history_knowledge_progress'),('capability','history_capability_progress'),('continuity','history_continuity_progress'),
 ('operations','history_operations_progress')) AS fixed(domain_name,table_name) LOOP
 IF d='capture' AND ns='public' THEN CONTINUE; END IF;
 -- Fixed allowlist names only; row locks close concurrent generation mutation.
 EXECUTE format('SELECT count(*) FROM em_content.%I',t) INTO n;
 IF n<>1 THEN RAISE EXCEPTION 'authority_raw_progress_ambiguous'; END IF;
 EXECUTE format('SELECT jsonb_build_object(''run_id'',run_id::text,''generation'',generation) FROM em_content.%I FOR SHARE',t) INTO v;
 IF (v->>'run_id')::uuid IS NULL OR (v->>'generation')::bigint IS NULL OR (v->>'generation')::bigint<0
 THEN RAISE EXCEPTION 'authority_raw_generation_invalid'; END IF;
 raw:=raw||jsonb_build_object(d,v);
 END LOOP;
 IF raw IS DISTINCT FROM e.generation THEN RAISE EXCEPTION 'authority_raw_generation_changed'; END IF;
 IF p_applied THEN
 IF NOT r.enabled OR r.policy_epoch IS DISTINCT FROM s.policy_epoch OR r.route_epoch IS DISTINCT FROM s.route_epoch
 OR r.mapping_revision<>m.mapping_revision OR r.membership_hash IS DISTINCT FROM m.membership_hash
 OR r.completion_epoch IS DISTINCT FROM e.epoch_id OR r.completion_hash IS DISTINCT FROM e.completion_hash
 THEN RAISE EXCEPTION 'authority_applied_readiness_changed'; END IF;
 ELSIF r.enabled THEN RAISE EXCEPTION 'authority_initial_readiness_must_be_disabled'; END IF;
 RETURN jsonb_build_object('kind',r.mode,'enabled',r.enabled,'revision',r.revision,'policy_epoch',r.policy_epoch,'route_epoch',r.route_epoch,
 'namespace',ns,'c_epoch',e.epoch_id,'c_completion_hash',e.completion_hash,'c_source_seal',e.manifest->>'new_seal','raw_generation',raw,
 'membership_revision',m.mapping_revision,'membership_hash',m.membership_hash,'membership_c_epoch',m.completion_epoch,'membership_c_completion_hash',m.completion_hash);
END $fn$;
CREATE FUNCTION em_security.inspect_authority_application(p_operation uuid) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_security AS $fn$
DECLARE service em_security.projector_services;a em_security.authority_applications;snapshot text;ready jsonb;
BEGIN
 service:=em_security._require_projector();
 SELECT * INTO a FROM em_security.authority_applications WHERE operation_id=p_operation FOR UPDATE;
 IF NOT FOUND THEN
 RETURN jsonb_build_object('version',1,'operation_id',p_operation,'library_id',service.library_id,'connection_ref',service.connection_ref,
 'state','absent','plan_hash',NULL,'acl_snapshot_text',em_security.inspect_acl_projection(p_operation),'readiness_text',NULL,
 'authority_receipt_text',NULL,'authority_receipt_hash',NULL); END IF;
 IF a.library_id<>service.library_id OR a.plan_json->>'connection_ref' IS DISTINCT FROM service.connection_ref
 THEN RAISE EXCEPTION 'authority_application_scope_changed'; END IF;
 IF a.state='prepared' AND a.prepared_txid<>txid_current() THEN RAISE EXCEPTION 'authority_preparation_transaction_changed'; END IF;
 ready:=em_security._authority_readiness(service.library_id,a.state='applied');
 snapshot:=em_security.inspect_acl_projection(p_operation);
 IF a.state='applied' AND (a.receipt_json->>'state_hash' IS DISTINCT FROM encode(sha256(convert_to(snapshot,'UTF8')),'hex')
 OR a.receipt_json->'readiness' IS DISTINCT FROM ready OR a.receipt_json->>'projector_login' IS DISTINCT FROM session_user
 OR a.receipt_json->>'content_database' IS DISTINCT FROM current_database()) THEN RAISE EXCEPTION 'authority_applied_readback_changed'; END IF;
 RETURN jsonb_build_object('version',1,'operation_id',p_operation,'library_id',service.library_id,'connection_ref',service.connection_ref,
 'state',a.state,'plan_hash',encode(a.plan_hash,'hex'),'acl_snapshot_text',snapshot,'readiness_text',ready::text,
 'authority_receipt_text',a.receipt_json::text,'authority_receipt_hash',encode(a.receipt_hash,'hex'));
END $fn$;
CREATE FUNCTION em_security.prepare_authority_application(p_plan_text text,p_plan_hash bytea) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_security AS $fn$
DECLARE service em_security.projector_services;p jsonb;s em_security.library_state;ready jsonb;
BEGIN
 service:=em_security._require_projector();p:=em_security._authority_plan(p_plan_text,p_plan_hash);
 IF (p->>'library_id')::uuid IS DISTINCT FROM service.library_id OR p->>'connection_ref' IS DISTINCT FROM service.connection_ref
 THEN RAISE EXCEPTION 'authority_projector_scope_mismatch'; END IF;
 SELECT * INTO s FROM em_security.library_state WHERE singleton FOR UPDATE;
 IF NOT FOUND OR s.library_id<>service.library_id OR s.kind<>'legacy' OR s.device_id IS NOT NULL OR s.project_id IS NOT NULL
 OR s.owner_principal_id IS DISTINCT FROM (p->>'owner_principal_id')::uuid
 OR s.authority_state<>'pg_staging' OR s.acl_state<>'updating'
 OR s.route_epoch IS DISTINCT FROM (p->>'expected_route_epoch')::bigint OR s.policy_epoch IS DISTINCT FROM (p->>'expected_policy_epoch')::bigint
 OR EXISTS(SELECT 1 FROM em_security.authority_applications WHERE library_id=s.library_id)
 THEN RAISE EXCEPTION 'authority_initial_content_state_required'; END IF;
 ready:=em_security._authority_readiness(service.library_id,false);
 INSERT INTO em_security.authority_applications(operation_id,library_id,plan_json,plan_hash,prepared_txid,state)
 VALUES((p->>'operation_id')::uuid,service.library_id,p,p_plan_hash,txid_current(),'prepared');
 UPDATE em_security.library_state SET authority_state='pg_active',acl_state='updating',route_epoch=(p->>'target_route_epoch')::bigint
 WHERE singleton AND library_id=service.library_id;
 RETURN em_security.inspect_authority_application((p->>'operation_id')::uuid);
END $fn$;
CREATE FUNCTION em_security.apply_authority_application(p_plan_text text,p_plan_hash bytea,p_registry_text text,p_registry_hash bytea) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_security AS $fn$
DECLARE service em_security.projector_services;p jsonb;a em_security.authority_applications;snapshot text;ready jsonb;receipt jsonb;revision bigint;
BEGIN
 service:=em_security._require_projector();p:=em_security._authority_plan(p_plan_text,p_plan_hash);
 SELECT * INTO a FROM em_security.authority_applications WHERE operation_id=(p->>'operation_id')::uuid FOR UPDATE;
 IF NOT FOUND OR a.library_id IS DISTINCT FROM service.library_id OR a.plan_json IS DISTINCT FROM p OR a.plan_hash IS DISTINCT FROM p_plan_hash
 OR a.state<>'prepared' OR a.prepared_txid<>txid_current() OR p->>'connection_ref' IS DISTINCT FROM service.connection_ref
 THEN RAISE EXCEPTION 'authority_preparation_required'; END IF;
 PERFORM em_security._authority_readiness(service.library_id,false);
 PERFORM em_security.apply_acl_projection(p->>'acl_plan_text',decode(p->>'acl_plan_hash','hex'),p_registry_text,p_registry_hash);
 SELECT r.revision INTO revision FROM em_security.history_readiness r WHERE r.library_id=service.library_id FOR UPDATE;
 PERFORM em_security.set_history_recall_ready(service.library_id,true,revision);
 ready:=em_security._authority_readiness(service.library_id,true);snapshot:=em_security.inspect_acl_projection(a.operation_id);
 receipt:=jsonb_build_object('version',1,'operation_id',a.operation_id,'library_id',a.library_id,'action','activate','connection_ref',service.connection_ref,
 'kind','legacy','owner_principal_id',p->>'owner_principal_id','device_id',NULL,'expected_route_epoch',p->'expected_route_epoch',
 'expected_policy_epoch',p->'expected_policy_epoch','target_route_epoch',p->'target_route_epoch','target_policy_epoch',p->'target_policy_epoch',
 'plan_hash',encode(p_plan_hash,'hex'),'registry_hash',encode(p_registry_hash,'hex'),'state_hash',encode(sha256(convert_to(snapshot,'UTF8')),'hex'),
 'content_database',current_database(),'projector_login',session_user,'readiness',ready);
 UPDATE em_security.authority_applications SET state='applied',receipt_json=receipt,receipt_hash=sha256(convert_to(receipt::text,'UTF8'))
 WHERE operation_id=a.operation_id;
 RETURN em_security.inspect_authority_application(a.operation_id);
END $fn$;

CREATE OR REPLACE FUNCTION em_security.allows(p_library_id uuid,p_project_id uuid,p_permission text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security AS $fn$
 SELECT EXISTS(
  SELECT 1 FROM em_security.session_context() c
   JOIN em_security.library_state s ON s.library_id=c.library_id
  WHERE c.library_id=p_library_id AND c.authority_state='pg_active' AND c.acl_state='ready'
   AND (s.kind<>'legacy' OR p_permission='read')
   AND CASE p_permission WHEN 'read' THEN c.permission IN('read','write','admin')
    WHEN 'write' THEN c.permission IN('write','admin') WHEN 'admin' THEN c.permission='admin' ELSE false END
   AND ((p_project_id IS NULL AND c.principal_id=s.owner_principal_id)
    OR (p_project_id IS NOT NULL AND (s.kind<>'project' OR s.project_id=p_project_id)
     AND EXISTS(SELECT 1 FROM em_security.project_projection p WHERE p.project_id=p_project_id
      AND p.active AND p.projection_epoch=c.policy_epoch)
     AND (c.principal_id=s.owner_principal_id OR EXISTS(
      SELECT 1 FROM em_security.project_acl_projection a WHERE a.project_id=p_project_id
       AND a.principal_id=c.principal_id AND a.active AND a.projection_epoch=c.policy_epoch
       AND CASE p_permission WHEN 'read' THEN a.permission IN('read','write','admin')
        WHEN 'write' THEN a.permission IN('write','admin') WHEN 'admin' THEN a.permission='admin' ELSE false END))))
 )
$fn$;
REVOKE ALL ON FUNCTION em_security._authority_preparation_commit_guard(),em_security._authority_plan(text,bytea),
 em_security._authority_readiness(uuid,boolean),em_security.inspect_authority_application(uuid),
 em_security.prepare_authority_application(text,bytea),em_security.apply_authority_application(text,bytea,text,bytea) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION em_security.inspect_authority_application(uuid),em_security.prepare_authority_application(text,bytea),
 em_security.apply_authority_application(text,bytea,text,bytea) TO :"projector_role";
INSERT INTO em_security.schema_migrations(version,migration_name) VALUES(5,'003_authority_lifecycle');
COMMIT;

-- Candidate only. Existing dedicated content DB schema 1; no data/role creation.
-- Trusted variables database_name, owner_role. Applied operations are retained.
\set ON_ERROR_STOP on
SELECT current_database()=:'database_name' AND current_user=:'owner_role'
 AND EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
  WHERE r.rolname=current_user AND d.datname=current_database()
   AND NOT(r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreaterole)) AS preflight_ok \gset
\if :preflight_ok
\else
 \quit 3
\endif
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('evolvmem.content.schema',0));
DO $version$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM em_security.schema_migrations WHERE version=1)
  OR EXISTS(SELECT 1 FROM em_security.schema_migrations WHERE version>=2) THEN
  RAISE EXCEPTION 'invalid_content_schema_version'; END IF;
END; $version$;
CREATE TABLE em_security.projector_services(
 login_role name PRIMARY KEY, library_id uuid NOT NULL, connection_ref text NOT NULL,
 enabled boolean NOT NULL DEFAULT false,
 CHECK(connection_ref~'^[A-Za-z][A-Za-z0-9_.-]{0,127}$')
);
ALTER TABLE em_security.projector_services ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_security.projector_services FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_security.projector_services TO :"owner_role" USING(true) WITH CHECK(true);
ALTER TABLE em_security.applied_acl_operations
 ADD COLUMN expected_policy_epoch bigint CHECK(expected_policy_epoch>0),
 ADD COLUMN route_epoch bigint CHECK(route_epoch>0),
 ADD COLUMN plan_hash bytea CHECK(octet_length(plan_hash)=32),
 ADD COLUMN registry_hash bytea CHECK(octet_length(registry_hash)=32);

CREATE FUNCTION em_security._require_projector() RETURNS em_security.projector_services
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_security AS $fn$
DECLARE service em_security.projector_services;
BEGIN
 SELECT s.* INTO service FROM em_security.projector_services s JOIN pg_roles r ON r.rolname=s.login_role
 WHERE s.login_role=session_user AND s.enabled AND r.rolcanlogin
  AND EXISTS(SELECT 1 FROM em_security.library_state state WHERE state.singleton AND state.library_id=s.library_id)
  AND NOT(r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb)
  AND NOT EXISTS(SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid)
  AND NOT pg_has_role(session_user,current_user,'MEMBER')
  AND NOT EXISTS(SELECT 1 FROM em_security.principal_bindings b WHERE b.login_role=session_user)
  AND NOT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname IN('em_security','em_content') AND c.relkind IN('r','p','v','m','f')
    AND has_table_privilege(r.oid,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'));
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='projector_service_forbidden'; END IF;
 RETURN service;
END; $fn$;

CREATE FUNCTION em_security._runtime_role_safe(p_role name) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security AS $fn$
 SELECT EXISTS(SELECT 1 FROM pg_roles r WHERE r.rolname=p_role AND r.rolcanlogin
  AND NOT(r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb)
  AND NOT EXISTS(SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid)
  AND NOT EXISTS(SELECT 1 FROM pg_database d WHERE d.datname=current_database() AND pg_has_role(r.oid,d.datdba,'MEMBER'))
  AND NOT EXISTS(SELECT 1 FROM pg_namespace n WHERE n.nspname IN('em_security','em_content') AND pg_has_role(r.oid,n.nspowner,'MEMBER'))
  AND NOT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname IN('em_security','em_content') AND pg_has_role(r.oid,c.relowner,'MEMBER'))
  AND NOT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname='em_security' AND c.relkind IN('r','p','v','m','f')
    AND has_table_privilege(r.oid,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'))
  AND NOT EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
   WHERE n.nspname='em_security' AND p.proname IN('apply_acl_projection','inspect_acl_projection','_require_projector','_expected_bindings','_runtime_role_safe')
    AND has_function_privilege(r.oid,p.oid,'EXECUTE')))
$fn$;

CREATE FUNCTION em_security._expected_bindings(p_plan jsonb,p_registry jsonb)
RETURNS TABLE(login_role name,principal_id uuid,credential_id uuid,device_id uuid,
 credential_epoch bigint,expires_at timestamptz,enabled boolean)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security AS $fn$
 SELECT r.login_role,r.principal_id,r.credential_id,r.device_id,r.credential_epoch,r.expires_at,
  EXISTS(SELECT 1 FROM jsonb_to_recordset(p_plan->'credentials') c(
    principal_id uuid,credential_id uuid,device_id uuid,credential_epoch bigint,principal_epoch bigint,
    device_epoch bigint,credential_active boolean,principal_active boolean,device_active boolean,expires_at timestamptz)
   JOIN jsonb_to_recordset(p_plan->'acls') a(principal_id uuid,permission text,active boolean,
    principal_active boolean,principal_epoch bigint) ON a.principal_id=c.principal_id
   WHERE c.credential_id=r.credential_id AND c.principal_id=r.principal_id AND c.device_id=r.device_id
    AND c.credential_epoch=r.credential_epoch AND c.expires_at IS NOT DISTINCT FROM r.expires_at
    AND a.active AND a.principal_active AND c.credential_active AND c.principal_active AND c.device_active
    AND(c.expires_at IS NULL OR c.expires_at>(p_plan->>'evaluation_at')::timestamptz)
    AND a.principal_epoch=c.principal_epoch
    AND(p_plan->>'kind'<>'device' OR(c.device_id=(p_plan->>'device_id')::uuid
       AND c.principal_id=(p_plan->>'owner_principal_id')::uuid)))
 FROM jsonb_to_recordset(p_registry) r(login_role name,principal_id uuid,credential_id uuid,device_id uuid,
   credential_epoch bigint,expires_at timestamptz,enabled boolean)
$fn$;

CREATE FUNCTION em_security.inspect_acl_projection(p_operation uuid) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_security AS $fn$
DECLARE service em_security.projector_services; result jsonb;
BEGIN
 service:=em_security._require_projector();
 SELECT jsonb_build_object('database',current_database(),'session_role',session_user,
  'library',(SELECT to_jsonb(s)-'singleton' FROM em_security.library_state s WHERE s.singleton AND s.library_id=service.library_id),
  'acls',(SELECT coalesce(jsonb_agg(to_jsonb(a) ORDER BY a.principal_id),'[]'::jsonb) FROM em_security.acl_projection a),
  'bindings',(SELECT coalesce(jsonb_agg(jsonb_build_object('login_role',b.login_role,'principal_id',b.principal_id,
    'credential_id',b.credential_id,'device_id',b.authenticated_device_id,'credential_epoch',b.credential_epoch,
    'expires_at',CASE WHEN b.expires_at IS NULL THEN NULL ELSE to_char(b.expires_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"') END,
    'enabled',b.enabled,'role_safe',em_security._runtime_role_safe(b.login_role)) ORDER BY b.login_role),'[]'::jsonb)
    FROM em_security.principal_bindings b),
  'applied',(SELECT jsonb_build_object('operation_id',x.operation_id,'target_policy_epoch',x.target_policy_epoch,
    'expected_policy_epoch',x.expected_policy_epoch,'route_epoch',x.route_epoch,'payload_hash',encode(x.payload_hash,'hex'),
    'plan_hash',encode(x.plan_hash,'hex'),'registry_hash',encode(x.registry_hash,'hex'))
    FROM em_security.applied_acl_operations x WHERE x.operation_id=p_operation)) INTO result;
 RETURN result::text;
END; $fn$;

CREATE FUNCTION em_security.apply_acl_projection(p_plan_text text,p_plan_hash bytea,p_registry_text text,p_registry_hash bytea)
RETURNS boolean LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_security AS $fn$
DECLARE service em_security.projector_services; plan jsonb; registry jsonb; state em_security.library_state;
 prior em_security.applied_acl_operations; operation uuid; expected_epoch bigint; target_epoch bigint; route bigint;
 original_hash bytea; already_applied boolean;
BEGIN
 service:=em_security._require_projector();
 IF p_plan_text IS NULL OR p_registry_text IS NULL OR octet_length(p_plan_text)>2097152
  OR octet_length(p_registry_text)>2097152 OR p_plan_hash IS NULL OR octet_length(p_plan_hash)<>32
  OR p_registry_hash IS NULL OR octet_length(p_registry_hash)<>32
  OR sha256(convert_to(p_plan_text,'UTF8'))<>p_plan_hash
  OR sha256(convert_to(p_registry_text,'UTF8'))<>p_registry_hash THEN
  RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='projection_hash_invalid'; END IF;
 plan:=p_plan_text::jsonb; registry:=p_registry_text::jsonb;
 IF jsonb_typeof(plan) IS DISTINCT FROM 'object' OR plan::text<>p_plan_text
  OR NOT(plan ?& ARRAY['version','operation_id','library_id','connection_ref','expected_policy_epoch','target_policy_epoch',
    'route_epoch','kind','device_id','project_id','owner_principal_id','payload_hash','evaluation_at','acls','credentials'])
  OR (SELECT count(*) FROM jsonb_object_keys(plan))<>15
  OR plan->>'version'<>'1' OR jsonb_typeof(plan->'acls') IS DISTINCT FROM 'array'
  OR jsonb_typeof(plan->'credentials') IS DISTINCT FROM 'array' OR jsonb_typeof(registry) IS DISTINCT FROM 'array' THEN
  RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='projection_plan_invalid'; END IF;
 IF jsonb_array_length(plan->'acls')>256 OR jsonb_array_length(plan->'credentials')>1024
  OR jsonb_array_length(registry)>2048 THEN
  RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='projection_plan_too_large'; END IF;
 operation:=(plan->>'operation_id')::uuid; expected_epoch:=(plan->>'expected_policy_epoch')::bigint;
 target_epoch:=(plan->>'target_policy_epoch')::bigint; route:=(plan->>'route_epoch')::bigint;
 original_hash:=decode(plan->>'payload_hash','hex');
 IF operation IS NULL OR expected_epoch IS NULL OR expected_epoch<1 OR target_epoch IS NULL
  OR target_epoch<>expected_epoch+1 OR route IS NULL OR route<1 OR octet_length(original_hash) IS DISTINCT FROM 32
  OR (plan->>'owner_principal_id')::uuid IS NULL OR (plan->>'evaluation_at')::timestamptz IS NULL
  OR NOT isfinite((plan->>'evaluation_at')::timestamptz)
  OR (plan->>'library_id')::uuid IS DISTINCT FROM service.library_id
  OR plan->>'connection_ref' IS DISTINCT FROM service.connection_ref THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='projection_scope_mismatch'; END IF;
 SELECT * INTO state FROM em_security.library_state s WHERE s.singleton FOR UPDATE;
 IF NOT FOUND OR state.library_id<>service.library_id OR state.route_epoch<>route
  OR state.kind IS DISTINCT FROM plan->>'kind'
  OR state.device_id IS DISTINCT FROM (plan->>'device_id')::uuid
  OR state.project_id IS DISTINCT FROM (plan->>'project_id')::uuid
  OR state.authority_state<>'pg_active' OR state.acl_state NOT IN('ready','updating','failed') THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='projection_route_changed'; END IF;
 -- Close metadata insert phantoms as well as updates. Runtime data tables are
 -- untouched; trusted provisioning must wait for this short metadata phase.
 LOCK TABLE em_security.principal_bindings IN SHARE ROW EXCLUSIVE MODE;
 LOCK TABLE em_security.acl_projection IN SHARE ROW EXCLUSIVE MODE;
 PERFORM 1 FROM em_security.principal_bindings b ORDER BY b.login_role FOR UPDATE;
 PERFORM 1 FROM em_security.acl_projection a ORDER BY a.principal_id FOR UPDATE;
 SELECT * INTO prior FROM em_security.applied_acl_operations x WHERE x.operation_id=operation FOR UPDATE;
 already_applied:=FOUND;
 IF already_applied THEN
  IF prior.payload_hash IS DISTINCT FROM original_hash OR prior.plan_hash IS DISTINCT FROM p_plan_hash
   OR prior.registry_hash IS DISTINCT FROM p_registry_hash OR prior.target_policy_epoch<>target_epoch
   OR prior.expected_policy_epoch IS DISTINCT FROM expected_epoch OR prior.route_epoch IS DISTINCT FROM route THEN
   RAISE EXCEPTION USING ERRCODE='23505',MESSAGE='projection_hash_conflict'; END IF;
  IF state.policy_epoch<>target_epoch OR state.acl_state<>'ready' THEN
   RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='applied_projection_state_changed'; END IF;
 ELSIF state.policy_epoch<>expected_epoch THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='projection_policy_changed'; END IF;

 -- Validate complete target ACL and credential sets; never silently truncate.
 IF EXISTS(SELECT 1 FROM jsonb_to_recordset(plan->'acls') a(principal_id uuid,permission text,active boolean,
   principal_active boolean,principal_epoch bigint) WHERE a.principal_id IS NULL OR a.permission IS NULL
    OR a.permission NOT IN('read','write','admin') OR a.active IS NULL OR a.principal_active IS NULL
    OR a.principal_epoch IS NULL OR a.principal_epoch<1)
  OR (SELECT count(DISTINCT a->>'principal_id') FROM jsonb_array_elements(plan->'acls') a)<>jsonb_array_length(plan->'acls')
  OR EXISTS(SELECT 1 FROM em_security.acl_projection a WHERE NOT EXISTS(
    SELECT 1 FROM jsonb_to_recordset(plan->'acls') p(principal_id uuid) WHERE p.principal_id=a.principal_id)) THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='acl_plan_incomplete'; END IF;
 IF EXISTS(SELECT 1 FROM jsonb_to_recordset(plan->'credentials') c(principal_id uuid,credential_id uuid,
   device_id uuid,credential_epoch bigint,principal_epoch bigint,device_epoch bigint,credential_active boolean,
   principal_active boolean,device_active boolean,expires_at timestamptz)
  WHERE c.principal_id IS NULL OR c.credential_id IS NULL OR c.device_id IS NULL OR c.credential_epoch IS NULL
   OR c.credential_epoch<1 OR c.principal_epoch IS NULL OR c.principal_epoch<1 OR c.device_epoch IS NULL OR c.device_epoch<1
   OR c.credential_active IS NULL OR c.principal_active IS NULL OR c.device_active IS NULL
   OR NOT EXISTS(SELECT 1 FROM jsonb_to_recordset(plan->'acls') a(principal_id uuid,principal_active boolean,principal_epoch bigint)
     WHERE a.principal_id=c.principal_id AND a.principal_active=c.principal_active AND a.principal_epoch=c.principal_epoch))
  OR (SELECT count(DISTINCT c->>'credential_id') FROM jsonb_array_elements(plan->'credentials') c)<>jsonb_array_length(plan->'credentials') THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='credential_plan_incomplete'; END IF;
 IF state.kind='device' AND EXISTS(SELECT 1 FROM jsonb_to_recordset(plan->'acls') a(principal_id uuid,active boolean)
  WHERE a.active AND a.principal_id<>(plan->>'owner_principal_id')::uuid) THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='device_library_acl_violation'; END IF;

 IF jsonb_array_length(registry)<>(SELECT count(*) FROM em_security.principal_bindings)
  OR (SELECT count(DISTINCT r->>'login_role') FROM jsonb_array_elements(registry) r)<>jsonb_array_length(registry)
  OR EXISTS(SELECT 1 FROM jsonb_array_elements(registry) r WHERE jsonb_typeof(r->'login_role') IS DISTINCT FROM 'string'
    OR r->>'login_role' IS DISTINCT FROM ((r->>'login_role')::name)::text)
  OR (SELECT count(DISTINCT r.login_role) FROM jsonb_to_recordset(registry) r(login_role name))<>jsonb_array_length(registry)
  OR EXISTS(SELECT 1 FROM em_security.principal_bindings b FULL JOIN jsonb_to_recordset(registry)
    r(login_role name,principal_id uuid,credential_id uuid,device_id uuid,credential_epoch bigint,expires_at timestamptz,enabled boolean)
    ON b.login_role=r.login_role
   WHERE b.login_role IS NULL OR r.login_role IS NULL OR r.enabled IS NULL OR b.principal_id IS DISTINCT FROM r.principal_id
    OR b.credential_id IS DISTINCT FROM r.credential_id OR b.authenticated_device_id IS DISTINCT FROM r.device_id
    OR b.credential_epoch IS DISTINCT FROM r.credential_epoch OR b.expires_at IS DISTINCT FROM r.expires_at
    OR EXISTS(SELECT 1 FROM jsonb_to_recordset(plan->'credentials') c(principal_id uuid,credential_id uuid,
      device_id uuid,credential_epoch bigint,expires_at timestamptz) WHERE c.credential_id=r.credential_id
       AND(c.principal_id IS DISTINCT FROM r.principal_id OR c.device_id IS DISTINCT FROM r.device_id
        OR c.credential_epoch IS DISTINCT FROM r.credential_epoch OR c.expires_at IS DISTINCT FROM r.expires_at))
    OR NOT em_security._runtime_role_safe(b.login_role)) THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='registry_incomplete'; END IF;
 -- Every eligible credential has exactly one existing verified login binding.
 IF EXISTS(SELECT 1 FROM jsonb_to_recordset(plan->'credentials') c(principal_id uuid,credential_id uuid,
    device_id uuid,credential_epoch bigint,principal_epoch bigint,credential_active boolean,principal_active boolean,
    device_active boolean,expires_at timestamptz)
   JOIN jsonb_to_recordset(plan->'acls') a(principal_id uuid,active boolean,principal_active boolean,principal_epoch bigint)
    ON a.principal_id=c.principal_id
   WHERE a.active AND a.principal_active AND c.credential_active AND c.principal_active AND c.device_active
    AND(c.expires_at IS NULL OR c.expires_at>(plan->>'evaluation_at')::timestamptz)
    AND(state.kind<>'device' OR c.device_id=state.device_id)
    AND (SELECT count(*) FROM em_security._expected_bindings(plan,registry) r WHERE r.credential_id=c.credential_id AND r.enabled)<>1)
  OR EXISTS(SELECT 1 FROM em_security._expected_bindings(plan,registry) e
    JOIN jsonb_to_recordset(registry) r(login_role name,enabled boolean) USING(login_role)
    WHERE e.enabled IS DISTINCT FROM r.enabled) THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='eligible_binding_missing_or_expired'; END IF;

 IF NOT already_applied THEN
  INSERT INTO em_security.acl_projection(principal_id,permission,active,projection_epoch)
   SELECT a.principal_id,a.permission,a.active,target_epoch FROM jsonb_to_recordset(plan->'acls') a(principal_id uuid,permission text,active boolean)
   ON CONFLICT ON CONSTRAINT acl_projection_pkey DO UPDATE SET permission=EXCLUDED.permission,
    active=EXCLUDED.active,projection_epoch=EXCLUDED.projection_epoch;
  UPDATE em_security.principal_bindings b SET enabled=e.enabled
   FROM em_security._expected_bindings(plan,registry) e WHERE b.login_role=e.login_role;
  UPDATE em_security.library_state SET policy_epoch=target_epoch,acl_state='ready' WHERE singleton;
  INSERT INTO em_security.applied_acl_operations(operation_id,target_policy_epoch,payload_hash,expected_policy_epoch,route_epoch,plan_hash,registry_hash)
   VALUES(operation,target_epoch,original_hash,expected_epoch,route,p_plan_hash,p_registry_hash);
 END IF;
 -- Replayed operations must still match the entire actual projection.
 IF EXISTS(SELECT 1 FROM em_security.acl_projection a FULL JOIN
   jsonb_to_recordset(plan->'acls') p(principal_id uuid,permission text,active boolean) USING(principal_id)
   WHERE a.principal_id IS NULL OR p.principal_id IS NULL OR a.permission IS DISTINCT FROM p.permission
    OR a.active IS DISTINCT FROM p.active OR a.projection_epoch<>target_epoch)
  OR EXISTS(SELECT 1 FROM em_security.principal_bindings b FULL JOIN em_security._expected_bindings(plan,registry) e
    ON b.login_role=e.login_role WHERE b.login_role IS NULL OR e.login_role IS NULL OR b.enabled IS DISTINCT FROM e.enabled) THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='projection_readback_mismatch'; END IF;
 RETURN NOT already_applied;
END; $fn$;

REVOKE ALL ON em_security.projector_services FROM PUBLIC;
REVOKE ALL ON FUNCTION em_security._require_projector() FROM PUBLIC;
REVOKE ALL ON FUNCTION em_security._runtime_role_safe(name) FROM PUBLIC;
REVOKE ALL ON FUNCTION em_security._expected_bindings(jsonb,jsonb) FROM PUBLIC;
REVOKE ALL ON FUNCTION em_security.inspect_acl_projection(uuid) FROM PUBLIC;
REVOKE ALL ON FUNCTION em_security.apply_acl_projection(text,bytea,text,bytea) FROM PUBLIC;
INSERT INTO em_security.schema_migrations(version,migration_name) VALUES(2,'002_content_acl_projector');
COMMIT;

-- Additive candidate, not installed. Trusted psql variables: database_name,
-- owner_role, coordinator_role. Requires no unfinished v1 ACL operation.
\set ON_ERROR_STOP on
SELECT current_database()=:'database_name' AND current_user=:'owner_role'
 AND EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
  WHERE r.rolname=current_user AND d.datname=current_database()
   AND NOT(r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreaterole))
 AND EXISTS(SELECT 1 FROM em_control.service_roles s
  WHERE s.login_role=:'coordinator_role' AND s.enabled AND s.can_coordinate)
 AS preflight_ok \gset
\if :preflight_ok
\else
 \quit 3
\endif
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('evolvmem.control.schema',0));
DO $version$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM em_control.schema_migrations WHERE version=4)
  OR EXISTS(SELECT 1 FROM em_control.schema_migrations WHERE version>=5)
  OR EXISTS(SELECT 1 FROM em_control.acl_operations WHERE operation_state<>'complete') THEN
  RAISE EXCEPTION 'invalid_control_schema_version_or_pending_acl'; END IF;
END; $version$;

-- A canonical key has one immutable UUID across libraries. Display-name aliases
-- and old-source IDs need a separately reviewed mapping; this is not fuzzy matching.
CREATE TABLE em_control.project_registry(
 project_id uuid PRIMARY KEY,
 project_key text COLLATE "C" NOT NULL UNIQUE CHECK(length(project_key) BETWEEN 1 AND 256
  AND project_key=btrim(project_key) AND project_key!~'[[:cntrl:]]'),
 UNIQUE(project_id,project_key)
);
CREATE TABLE em_control.library_projects(
 library_id uuid NOT NULL REFERENCES em_control.libraries(library_id),
 project_id uuid NOT NULL REFERENCES em_control.project_registry(project_id),
 active boolean NOT NULL DEFAULT true,
 PRIMARY KEY(library_id,project_id)
);
CREATE TABLE em_control.library_project_acl(
 library_id uuid NOT NULL,
 project_id uuid NOT NULL,
 principal_id uuid NOT NULL,
 permission text NOT NULL CHECK(permission IN('read','write','admin')),
 active boolean NOT NULL DEFAULT true,
 policy_epoch bigint NOT NULL CHECK(policy_epoch>0),
 granted_by uuid NOT NULL REFERENCES em_control.principals(principal_id),
 PRIMARY KEY(library_id,project_id,principal_id),
 FOREIGN KEY(library_id,project_id) REFERENCES em_control.library_projects(library_id,project_id),
 FOREIGN KEY(library_id,principal_id) REFERENCES em_control.library_acl(library_id,principal_id)
);
ALTER TABLE em_control.acl_operations
 ADD COLUMN target_project_id uuid,
 ADD COLUMN target_project_key text COLLATE "C",
 ADD CHECK((target_project_id IS NULL)=(target_project_key IS NULL)),
 ADD FOREIGN KEY(target_project_id,target_project_key) REFERENCES em_control.project_registry(project_id,project_key);

ALTER TABLE em_control.project_registry ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.project_registry FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.project_registry TO :"owner_role" USING(true) WITH CHECK(true);
ALTER TABLE em_control.library_projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.library_projects FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.library_projects TO :"owner_role" USING(true) WITH CHECK(true);
ALTER TABLE em_control.library_project_acl ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.library_project_acl FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.library_project_acl TO :"owner_role" USING(true) WITH CHECK(true);

CREATE OR REPLACE FUNCTION em_control._begin_acl_change_v2(p_credential uuid,p_principal uuid,p_device uuid,
 p_credential_epoch bigint,p_principal_epoch bigint,p_device_epoch bigint,p_library uuid,
 p_target uuid,p_permission text,p_expected_epoch bigint,p_operation uuid,p_request_id text,p_payload_hash bytea,p_project uuid,p_project_key text)
RETURNS TABLE(operation_id uuid,library_id uuid,target_policy_epoch bigint,operation_state text,
 expected_route_epoch bigint,connection_ref text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
DECLARE a em_control.authorization_row; prior em_control.acl_operations;
BEGIN
 PERFORM em_control._require_service('coordinate');
 IF (p_project IS NULL)<>(p_project_key IS NULL) OR (p_project_key IS NOT NULL AND
  (length(p_project_key) NOT BETWEEN 1 AND 256 OR p_project_key<>btrim(p_project_key) OR p_project_key~'[[:cntrl:]]')) THEN
  RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid_project_registration'; END IF;
 IF p_operation IS NULL OR p_target IS NULL OR p_request_id IS NULL OR length(p_request_id) NOT BETWEEN 1 AND 128
  OR p_expected_epoch IS NULL OR p_expected_epoch<1 OR p_expected_epoch>=9223372036854775807
  OR p_payload_hash IS NULL OR octet_length(p_payload_hash)<>32
  OR(p_permission IS NOT NULL AND p_permission NOT IN('read','write','admin')) THEN
  RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid_acl_intent'; END IF;
 PERFORM 1 FROM em_control.principals p WHERE p.principal_id=p_principal FOR SHARE;
 PERFORM 1 FROM em_control.devices d WHERE d.device_id=p_device FOR SHARE;
 PERFORM 1 FROM em_control.credentials c WHERE c.credential_id=p_credential FOR SHARE;
 PERFORM 1 FROM em_control.libraries l WHERE l.library_id=p_library FOR UPDATE;
 PERFORM 1 FROM em_control.library_routes r WHERE r.library_id=p_library FOR UPDATE;
 PERFORM em_control._assert_identity(p_credential,p_principal,p_device,p_credential_epoch,p_principal_epoch,p_device_epoch);
 SELECT * INTO prior FROM em_control.acl_operations o
  WHERE o.operation_id=p_operation OR o.request_id=p_request_id FOR UPDATE;
 IF FOUND THEN
  IF prior.operation_id<>p_operation OR prior.request_id<>p_request_id OR prior.library_id<>p_library
   OR prior.actor_principal_id<>p_principal OR prior.actor_credential_id IS DISTINCT FROM p_credential
   OR prior.actor_device_id IS DISTINCT FROM p_device OR prior.target_principal_id<>p_target
   OR prior.actor_credential_epoch IS DISTINCT FROM p_credential_epoch
   OR prior.actor_principal_epoch IS DISTINCT FROM p_principal_epoch
   OR prior.actor_device_epoch IS DISTINCT FROM p_device_epoch
   OR prior.requested_permission IS DISTINCT FROM p_permission OR prior.expected_policy_epoch<>p_expected_epoch
   OR prior.payload_hash<>p_payload_hash OR prior.target_project_id IS DISTINCT FROM p_project
   OR prior.target_project_key IS DISTINCT FROM p_project_key THEN
   RAISE EXCEPTION USING ERRCODE='23505',MESSAGE='acl_request_conflict'; END IF;
  IF prior.operation_state<>'complete' THEN
   -- Fence a resumed operation BEFORE its caller can reapply content projection.
   PERFORM em_control._lock_acl_operation(prior.operation_id);
  END IF;
  RETURN QUERY SELECT prior.operation_id,prior.library_id,prior.target_policy_epoch,prior.operation_state,
   prior.expected_route_epoch,prior.expected_connection_ref;
  RETURN;
 END IF;
 SELECT * INTO a FROM em_control.resolve_library(p_credential,p_principal,p_device,
  p_credential_epoch,p_principal_epoch,p_device_epoch,p_library);
 IF NOT FOUND OR NOT a.library_active
  OR (p_project IS NULL AND a.permission<>'admin')
  OR (p_project IS NOT NULL AND a.owner_principal_id<>p_principal AND NOT EXISTS(
   SELECT 1 FROM em_control.library_project_acl x WHERE x.library_id=p_library AND x.project_id=p_project
    AND x.principal_id=p_principal AND x.active AND x.permission='admin' AND x.policy_epoch=p_expected_epoch
    AND a.permission='admin')) OR a.acl_state<>'ready'
  OR a.policy_epoch<>p_expected_epoch OR a.backend<>'postgres'
  OR a.authority_state<>'pg_active' OR a.write_backend<>'postgres' THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='acl_admin_required'; END IF;
 IF NOT EXISTS(SELECT 1 FROM em_control.principals p WHERE p.principal_id=p_target
  AND(p_permission IS NULL OR p.active)) THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='acl_target_unavailable'; END IF;
 -- Actual ownership is registry data, never a request/GUC assertion. Keep its
 -- admin connection ceiling; only that confirmed owner can see unclassified rows.
 IF p_project IS NULL AND p_target=a.owner_principal_id AND p_permission IS DISTINCT FROM 'admin' THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='owner_acl_immutable'; END IF;
 IF p_project IS NOT NULL THEN
  IF a.kind='project' AND a.project_id<>p_project THEN
   RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='project_library_scope_mismatch'; END IF;
  IF p_permission IS NOT NULL AND NOT EXISTS(SELECT 1 FROM em_control.library_acl x
   WHERE x.library_id=p_library AND x.principal_id=p_target AND x.active AND x.policy_epoch=p_expected_epoch
    AND CASE p_permission WHEN 'read' THEN x.permission IN('read','write','admin')
     WHEN 'write' THEN x.permission IN('write','admin') WHEN 'admin' THEN x.permission='admin' ELSE false END) THEN
   RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='project_grant_exceeds_library_ceiling'; END IF;
  IF EXISTS(SELECT 1 FROM em_control.project_registry x
   WHERE (x.project_id=p_project OR x.project_key=p_project_key)
     AND (x.project_id<>p_project OR x.project_key<>p_project_key)) THEN
   RAISE EXCEPTION USING ERRCODE='23505',MESSAGE='project_registration_conflict'; END IF;
  IF p_permission IS NULL AND NOT EXISTS(SELECT 1 FROM em_control.library_projects x
   WHERE x.library_id=p_library AND x.project_id=p_project) THEN
   RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='project_unregistered'; END IF;
  INSERT INTO em_control.project_registry(project_id,project_key) VALUES(p_project,p_project_key)
   ON CONFLICT DO NOTHING;
  IF NOT EXISTS(SELECT 1 FROM em_control.project_registry x WHERE x.project_id=p_project AND x.project_key=p_project_key) THEN
   RAISE EXCEPTION USING ERRCODE='23505',MESSAGE='project_registration_conflict'; END IF;
  INSERT INTO em_control.library_projects(library_id,project_id) VALUES(p_library,p_project)
   ON CONFLICT DO NOTHING;
  IF NOT EXISTS(SELECT 1 FROM em_control.library_projects x WHERE x.library_id=p_library AND x.project_id=p_project AND x.active) THEN
   RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='project_inactive'; END IF;
 END IF;
 INSERT INTO em_control.acl_operations(operation_id,request_id,library_id,actor_principal_id,target_principal_id,
  requested_permission,expected_policy_epoch,target_policy_epoch,payload_hash,actor_credential_id,actor_device_id,
  actor_credential_epoch,actor_principal_epoch,actor_device_epoch,expected_route_epoch,expected_connection_ref,target_project_id,target_project_key)
 VALUES(p_operation,p_request_id,p_library,p_principal,p_target,p_permission,p_expected_epoch,p_expected_epoch+1,
  p_payload_hash,p_credential,p_device,p_credential_epoch,p_principal_epoch,p_device_epoch,a.route_epoch,a.connection_ref,p_project,p_project_key);
 UPDATE em_control.library_routes SET acl_state='updating' WHERE em_control.library_routes.library_id=p_library;
 RETURN QUERY SELECT p_operation,p_library,p_expected_epoch+1,'pending'::text,a.route_epoch,a.connection_ref;
END $fn$;

CREATE OR REPLACE FUNCTION em_control.begin_acl_change(p_credential uuid,p_principal uuid,p_device uuid,
 p_credential_epoch bigint,p_principal_epoch bigint,p_device_epoch bigint,p_library uuid,
 p_target uuid,p_permission text,p_expected_epoch bigint,p_operation uuid,p_request_id text,p_payload_hash bytea)
RETURNS TABLE(operation_id uuid,library_id uuid,target_policy_epoch bigint,operation_state text,
 expected_route_epoch bigint,connection_ref text)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
 SELECT * FROM em_control._begin_acl_change_v2(p_credential,p_principal,p_device,p_credential_epoch,
  p_principal_epoch,p_device_epoch,p_library,p_target,p_permission,p_expected_epoch,p_operation,p_request_id,p_payload_hash,NULL,NULL);
$fn$;

CREATE OR REPLACE FUNCTION em_control.begin_project_acl_change(p_credential uuid,p_principal uuid,p_device uuid,
 p_credential_epoch bigint,p_principal_epoch bigint,p_device_epoch bigint,p_library uuid,
 p_target uuid,p_permission text,p_expected_epoch bigint,p_operation uuid,p_request_id text,p_payload_hash bytea,p_project uuid,p_project_key text)
RETURNS TABLE(operation_id uuid,library_id uuid,target_policy_epoch bigint,operation_state text,
 expected_route_epoch bigint,connection_ref text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
 BEGIN
 IF p_project IS NULL OR p_project_key IS NULL THEN
  RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid_project_registration'; END IF;
 RETURN QUERY SELECT * FROM em_control._begin_acl_change_v2(p_credential,p_principal,p_device,p_credential_epoch,
  p_principal_epoch,p_device_epoch,p_library,p_target,p_permission,p_expected_epoch,p_operation,p_request_id,p_payload_hash,p_project,p_project_key);
END; $fn$;

CREATE OR REPLACE FUNCTION em_control.finish_acl_change(p_operation uuid)
RETURNS TABLE(operation_id uuid,library_id uuid,target_policy_epoch bigint,operation_state text,
 expected_route_epoch bigint,connection_ref text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
DECLARE o em_control.acl_operations;
BEGIN
 o:=em_control._lock_acl_operation(p_operation);
 IF o.operation_state='complete' THEN
  RETURN QUERY SELECT o.operation_id,o.library_id,o.target_policy_epoch,o.operation_state,
   o.expected_route_epoch,o.expected_connection_ref; RETURN;
 END IF;
 PERFORM 1 FROM em_control.acl_projection_receipts x WHERE x.operation_id=p_operation
  AND x.target_policy_epoch=o.target_policy_epoch FOR UPDATE;
 IF NOT FOUND OR o.operation_state<>'applying' OR NOT EXISTS(
  SELECT 1 FROM em_control.library_routes r WHERE r.library_id=o.library_id
   AND r.policy_epoch=o.expected_policy_epoch AND r.acl_state='updating') THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='projection_not_confirmed'; END IF;
 IF o.target_project_id IS NULL THEN
 IF o.requested_permission IS NULL THEN
  UPDATE em_control.library_acl SET active=false
   WHERE em_control.library_acl.library_id=o.library_id AND principal_id=o.target_principal_id;
 ELSE
  INSERT INTO em_control.library_acl(library_id,principal_id,permission,active,policy_epoch,granted_by)
   VALUES(o.library_id,o.target_principal_id,o.requested_permission,true,o.target_policy_epoch,o.actor_principal_id)
   ON CONFLICT ON CONSTRAINT library_acl_pkey DO UPDATE SET permission=EXCLUDED.permission,active=true,
     policy_epoch=EXCLUDED.policy_epoch,granted_by=EXCLUDED.granted_by;
 END IF;
 ELSE
  IF o.requested_permission IS NULL THEN
   UPDATE em_control.library_project_acl SET active=false
    WHERE em_control.library_project_acl.library_id=o.library_id AND project_id=o.target_project_id AND principal_id=o.target_principal_id;
  ELSE
   INSERT INTO em_control.library_project_acl(library_id,project_id,principal_id,permission,active,policy_epoch,granted_by)
    VALUES(o.library_id,o.target_project_id,o.target_principal_id,o.requested_permission,true,o.target_policy_epoch,o.actor_principal_id)
    ON CONFLICT ON CONSTRAINT library_project_acl_pkey DO UPDATE SET permission=EXCLUDED.permission,
      active=true,policy_epoch=EXCLUDED.policy_epoch,granted_by=EXCLUDED.granted_by;
  END IF;
 END IF;
 -- A library ceiling change and a project change both advance EVERY projection.
 UPDATE em_control.library_project_acl SET policy_epoch=o.target_policy_epoch
  WHERE em_control.library_project_acl.library_id=o.library_id;
 UPDATE em_control.library_acl SET policy_epoch=o.target_policy_epoch
  WHERE em_control.library_acl.library_id=o.library_id;
 UPDATE em_control.library_routes SET policy_epoch=o.target_policy_epoch,acl_state='ready'
  WHERE em_control.library_routes.library_id=o.library_id;
 UPDATE em_control.acl_operations SET operation_state='complete',completed_at=clock_timestamp(),error_code=NULL
  WHERE em_control.acl_operations.operation_id=p_operation;
 RETURN QUERY SELECT o.operation_id,o.library_id,o.target_policy_epoch,'complete'::text,
  o.expected_route_epoch,o.expected_connection_ref;
END $fn$;
CREATE OR REPLACE FUNCTION em_control.get_acl_projection_plan(p_operation uuid)
RETURNS TABLE(plan_text text,plan_hash_hex text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
DECLARE o em_control.acl_operations; l em_control.libraries; document jsonb;
 stored em_control.acl_projection_plans; target_acl jsonb; credential_rows jsonb; hash_value bytea; project_rows jsonb; project_acl_rows jsonb;
BEGIN
 PERFORM em_control._require_service('coordinate');
 SELECT * INTO o FROM em_control.acl_operations x WHERE x.operation_id=p_operation;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='acl_operation_unavailable'; END IF;
 -- Lock all relevant identities before library/route, including inactive rows.
 -- UPDATE locks on parent identities also block FK KEY SHARE from new device/
 -- credential inserts; merely locking existing credential rows misses phantoms.
 -- No online identity mutation API is supported by this projector.
 PERFORM 1 FROM em_control.principals p WHERE p.principal_id=o.target_principal_id
  OR p.principal_id IN(SELECT a.principal_id FROM em_control.library_acl a WHERE a.library_id=o.library_id)
  ORDER BY p.principal_id FOR UPDATE;
 PERFORM 1 FROM em_control.devices d WHERE d.principal_id=o.target_principal_id
  OR d.principal_id IN(SELECT a.principal_id FROM em_control.library_acl a WHERE a.library_id=o.library_id)
  ORDER BY d.device_id FOR UPDATE;
 PERFORM 1 FROM em_control.credentials c WHERE c.principal_id=o.target_principal_id
  OR c.principal_id IN(SELECT a.principal_id FROM em_control.library_acl a WHERE a.library_id=o.library_id)
  ORDER BY c.credential_id FOR SHARE;
 o:=em_control._lock_acl_operation(p_operation);
 IF o.operation_state NOT IN('pending','applying','failed') THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='projection_operation_not_pending'; END IF;
 SELECT * INTO l FROM em_control.libraries x WHERE x.library_id=o.library_id;
 -- Library/route locks also serialize all public project registration/mutation.
 PERFORM 1 FROM em_control.library_projects x WHERE x.library_id=o.library_id ORDER BY x.project_id FOR SHARE;
 PERFORM 1 FROM em_control.library_project_acl x WHERE x.library_id=o.library_id ORDER BY x.project_id,x.principal_id FOR SHARE;

 WITH desired AS(
  SELECT a.principal_id,
   CASE WHEN o.target_project_id IS NULL AND a.principal_id=o.target_principal_id AND o.requested_permission IS NOT NULL
     THEN o.requested_permission ELSE a.permission END AS permission,
   CASE WHEN o.target_project_id IS NULL AND a.principal_id=o.target_principal_id THEN o.requested_permission IS NOT NULL ELSE a.active END AS active
  FROM em_control.library_acl a WHERE a.library_id=o.library_id
  UNION ALL
  SELECT o.target_principal_id,o.requested_permission,true
   WHERE o.target_project_id IS NULL AND o.requested_permission IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM em_control.library_acl a WHERE a.library_id=o.library_id AND a.principal_id=o.target_principal_id)
 )
 SELECT coalesce(jsonb_agg(jsonb_build_object('principal_id',a.principal_id,'permission',a.permission,
  'active',a.active,'principal_active',p.active,'principal_epoch',p.credential_epoch)
  ORDER BY a.principal_id),'[]'::jsonb) INTO target_acl
 FROM desired a JOIN em_control.principals p USING(principal_id);
 IF jsonb_array_length(target_acl)>256 THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='unsupported_acl_projection_scope'; END IF;

 SELECT coalesce(jsonb_agg(jsonb_build_object('principal_id',c.principal_id,'credential_id',c.credential_id,
  'device_id',c.device_id,'credential_epoch',c.credential_epoch,'principal_epoch',p.credential_epoch,
  'device_epoch',d.device_epoch,'credential_active',c.active,'principal_active',p.active,'device_active',d.active,
  'expires_at',CASE WHEN c.expires_at IS NULL THEN NULL ELSE
    to_char(c.expires_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"') END)
  ORDER BY c.credential_id),'[]'::jsonb) INTO credential_rows
 FROM em_control.credentials c JOIN em_control.principals p USING(principal_id)
 JOIN em_control.devices d ON d.device_id=c.device_id AND d.principal_id=c.principal_id
 WHERE c.principal_id IN(SELECT a.principal_id FROM jsonb_to_recordset(target_acl) a(principal_id uuid));
 IF jsonb_array_length(credential_rows)>1024 THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='projection_plan_too_large'; END IF;
 SELECT coalesce(jsonb_agg(jsonb_build_object('project_id',x.project_id,'project_key',r.project_key,'active',x.active)
  ORDER BY x.project_id),'[]'::jsonb) INTO project_rows FROM em_control.library_projects x
  JOIN em_control.project_registry r USING(project_id) WHERE x.library_id=o.library_id;
 WITH desired AS(
  SELECT a.project_id,a.principal_id,
   CASE WHEN a.project_id=o.target_project_id AND a.principal_id=o.target_principal_id AND o.requested_permission IS NOT NULL
    THEN o.requested_permission ELSE a.permission END AS permission,
   CASE WHEN a.project_id=o.target_project_id AND a.principal_id=o.target_principal_id
    THEN o.requested_permission IS NOT NULL ELSE a.active END AS active
  FROM em_control.library_project_acl a WHERE a.library_id=o.library_id
  UNION ALL SELECT o.target_project_id,o.target_principal_id,o.requested_permission,true
   WHERE o.target_project_id IS NOT NULL AND o.requested_permission IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM em_control.library_project_acl a WHERE a.library_id=o.library_id
     AND a.project_id=o.target_project_id AND a.principal_id=o.target_principal_id)
 ) SELECT coalesce(jsonb_agg(jsonb_build_object('project_id',a.project_id,'principal_id',a.principal_id,
   'permission',a.permission,'active',a.active) ORDER BY a.project_id,a.principal_id),'[]'::jsonb)
   INTO project_acl_rows FROM desired a;
 IF jsonb_array_length(project_rows)>2048 OR jsonb_array_length(project_acl_rows)>8192 THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='projection_plan_too_large'; END IF;
 document:=jsonb_build_object('version',2,'operation_id',o.operation_id,'library_id',o.library_id,
  'connection_ref',o.expected_connection_ref,'expected_policy_epoch',o.expected_policy_epoch,
  'target_policy_epoch',o.target_policy_epoch,'route_epoch',o.expected_route_epoch,
  'kind',l.kind,'device_id',l.device_id,'project_id',l.project_id,'owner_principal_id',l.owner_principal_id,
  'evaluation_at',to_char(o.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'payload_hash',encode(o.payload_hash,'hex'),'acls',target_acl,'credentials',credential_rows,'projects',project_rows,'project_acls',project_acl_rows);
 hash_value:=sha256(convert_to(document::text,'UTF8'));
 SELECT * INTO stored FROM em_control.acl_projection_plans x WHERE x.operation_id=p_operation FOR UPDATE;
 IF FOUND THEN
  IF stored.plan_hash<>hash_value OR stored.plan_json<>document THEN
   RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='projection_plan_changed'; END IF;
 ELSE
  INSERT INTO em_control.acl_projection_plans(operation_id,plan_json,plan_hash)
   VALUES(p_operation,document,hash_value);
 END IF;
 RETURN QUERY SELECT document::text,encode(hash_value,'hex');
END; $fn$;
REVOKE ALL ON em_control.project_registry,em_control.library_projects,em_control.library_project_acl FROM PUBLIC;
REVOKE ALL ON FUNCTION em_control._begin_acl_change_v2(uuid,uuid,uuid,bigint,bigint,bigint,uuid,uuid,text,bigint,uuid,text,bytea,uuid,text) FROM PUBLIC;
REVOKE ALL ON FUNCTION em_control.begin_project_acl_change(uuid,uuid,uuid,bigint,bigint,bigint,uuid,uuid,text,bigint,uuid,text,bytea,uuid,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION em_control.begin_project_acl_change(uuid,uuid,uuid,bigint,bigint,bigint,uuid,uuid,text,bigint,uuid,text,bytea,uuid,text) TO :"coordinator_role";
-- Existing begin/lookup/confirm/finish/fail/get-plan grants are preserved by REPLACE.
INSERT INTO em_control.schema_migrations(version,migration_name) VALUES(5,'005_control_project_acl');
COMMIT;

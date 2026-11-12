-- Named forward patch for the reviewed control ACL v5 function.
-- Trusted psql variables: database_name, owner_role, coordinator_role.
\set ON_ERROR_STOP on
\set patch_name 005a_control_acl_identity_share
\set old_body_sha256 04880d38610590ac02f56fadf423c0d4ad4bed0e2614285b5a39735864e63496
\set new_body_sha256 cfbdb5e8e1555c277c2c0aacd483ed60b5fd785ff93d79bb88b324dfe559b207

BEGIN;
SET LOCAL search_path = pg_catalog, em_control, pg_temp;
SELECT (
 current_database()=:'database_name' AND current_user=:'owner_role'
 AND EXISTS(
  SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
  WHERE r.rolname=current_user AND d.datname=current_database()
   AND NOT(r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreaterole))
) AS patch_owner_ok \gset
\if :patch_owner_ok
\else
DO $check$ BEGIN RAISE EXCEPTION 'acl_identity_share_owner_precondition_failed'; END; $check$;
\endif
SELECT pg_advisory_xact_lock(hashtextextended('evolvmem.control.schema',0));

SELECT (
 EXISTS(
  SELECT 1 FROM em_control.schema_migrations
  WHERE version=5 AND migration_name='005_control_project_acl')
 AND NOT EXISTS(
  SELECT 1 FROM em_control.schema_migrations
  WHERE version>5 AND (version<>6 OR migration_name<>'006_authority_lifecycle'))
 AND NOT EXISTS(
  SELECT 1 FROM em_control.acl_operations WHERE operation_state<>'complete')
 AND EXISTS(
  SELECT 1 FROM em_control.service_roles s JOIN pg_roles r ON r.rolname=s.login_role
  WHERE s.login_role=:'coordinator_role' AND s.enabled AND s.can_coordinate
   AND r.rolcanlogin AND NOT(r.rolsuper OR r.rolbypassrls OR r.rolcreaterole)
   AND NOT pg_has_role(r.oid,current_user,'MEMBER'))
 AND NOT EXISTS(
  SELECT 1 FROM em_control.service_roles s JOIN pg_roles r ON r.rolname=s.login_role
  WHERE r.rolsuper OR r.rolbypassrls OR r.rolcreaterole
     OR EXISTS(SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid))
 AND EXISTS(
  SELECT 1 FROM pg_proc p JOIN pg_roles owner ON owner.oid=p.proowner
  JOIN pg_language l ON l.oid=p.prolang
  JOIN pg_roles coordinator ON coordinator.rolname=:'coordinator_role'
  WHERE p.oid=to_regprocedure('em_control.get_acl_projection_plan(uuid)')
   AND owner.rolname=current_user AND l.lanname='plpgsql'
   AND p.prokind='f' AND p.prosecdef AND p.provolatile='v'
   AND NOT(p.proisstrict OR p.proleakproof) AND p.proparallel='u'
   AND p.prosupport=0 AND p.procost=100 AND p.prorows=1000
   AND p.protrftypes IS NULL AND p.probin IS NULL AND p.prosqlbody IS NULL
   AND p.proretset AND p.prorettype='record'::regtype AND p.pronargdefaults=0
   AND p.proargnames=ARRAY['p_operation','plan_text','plan_hash_hex']
   AND p.proargmodes=ARRAY['i','t','t']::"char"[]
   AND p.proallargtypes=ARRAY['uuid'::regtype::oid,'text'::regtype::oid,'text'::regtype::oid]
   AND p.proconfig=ARRAY['search_path=pg_catalog, em_control']
   AND (SELECT count(*)=2 AND count(DISTINCT a.grantee)=2
          AND bool_and(a.grantor=p.proowner AND a.grantee IN(p.proowner,coordinator.oid)
                       AND a.privilege_type='EXECUTE' AND NOT a.is_grantable)
        FROM aclexplode(p.proacl) a)
   AND NOT EXISTS(
    SELECT 1 FROM em_control.service_roles s JOIN pg_roles r ON r.rolname=s.login_role
    WHERE s.login_role<>:'coordinator_role'
     AND (has_function_privilege(r.oid,p.oid,'EXECUTE')
          OR pg_has_role(r.oid,coordinator.oid,'MEMBER')
          OR pg_has_role(r.oid,p.proowner,'MEMBER'))))
 AND (SELECT count(*)=3 AND bool_and(r.rolname=current_user)
      FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner
      WHERE c.oid IN('em_control.principals'::regclass,'em_control.devices'::regclass,
                     'em_control.credentials'::regclass) AND c.relkind='r')
 AND NOT EXISTS(
  SELECT 1 FROM em_control.service_roles s JOIN pg_roles r ON r.rolname=s.login_role
  CROSS JOIN (VALUES ('em_control.principals'::regclass),('em_control.devices'::regclass),
                     ('em_control.credentials'::regclass)) identity_tables(table_oid)
  WHERE has_table_privilege(r.oid,identity_tables.table_oid,'INSERT,UPDATE,DELETE,TRUNCATE,TRIGGER')
     OR has_any_column_privilege(r.oid,identity_tables.table_oid,'INSERT,UPDATE'))
) AS patch_security_ok \gset
\if :patch_security_ok
\else
DO $check$ BEGIN RAISE EXCEPTION 'acl_identity_share_security_precondition_failed'; END; $check$;
\endif

SELECT body_sha256 AS checked_body_sha256, oid::text AS checked_function_oid,
       acl_text AS checked_function_acl,
       body_sha256 IN (:'old_body_sha256', :'new_body_sha256') AS patch_body_ok,
       body_sha256=:'old_body_sha256' AS apply_patch
FROM (
 SELECT p.oid,p.proacl::text AS acl_text,encode(sha256(convert_to(p.prosrc,'UTF8')),'hex') AS body_sha256
 FROM pg_proc p WHERE p.oid=to_regprocedure('em_control.get_acl_projection_plan(uuid)')
) inspected \gset
\if :patch_body_ok
\else
DO $check$ BEGIN RAISE EXCEPTION 'acl_identity_share_unrecognized_body'; END; $check$;
\endif

\if :apply_patch
CREATE OR REPLACE FUNCTION em_control.get_acl_projection_plan(p_operation uuid)
RETURNS TABLE(plan_text text,plan_hash_hex text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
DECLARE o em_control.acl_operations; l em_control.libraries; document jsonb;
 stored em_control.acl_projection_plans; target_acl jsonb; credential_rows jsonb; hash_value bytea; project_rows jsonb; project_acl_rows jsonb;
BEGIN
 PERFORM em_control._require_service('coordinate');
 SELECT * INTO o FROM em_control.acl_operations x WHERE x.operation_id=p_operation;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='acl_operation_unavailable'; END IF;
 -- Table SHARE prevents identity DML and new FK children while allowing pins.
 LOCK TABLE em_control.principals, em_control.devices, em_control.credentials IN SHARE MODE;
 PERFORM 1 FROM em_control.principals p WHERE p.principal_id=o.target_principal_id
  OR p.principal_id IN(SELECT a.principal_id FROM em_control.library_acl a WHERE a.library_id=o.library_id)
  ORDER BY p.principal_id FOR SHARE;
 PERFORM 1 FROM em_control.devices d WHERE d.principal_id=o.target_principal_id
  OR d.principal_id IN(SELECT a.principal_id FROM em_control.library_acl a WHERE a.library_id=o.library_id)
  ORDER BY d.device_id FOR SHARE;
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
\endif

SELECT EXISTS(
 SELECT 1 FROM pg_proc p
 WHERE p.oid=to_regprocedure('em_control.get_acl_projection_plan(uuid)')
   AND p.oid::text=:'checked_function_oid' AND p.proacl::text=:'checked_function_acl'
   AND encode(sha256(convert_to(p.prosrc,'UTF8')),'hex')=:'new_body_sha256'
) AS patch_postcheck_ok \gset
\if :patch_postcheck_ok
\else
DO $check$ BEGIN RAISE EXCEPTION 'acl_identity_share_postcheck_failed'; END; $check$;
\endif
COMMIT;

SELECT :'patch_name' AS migration_name, :'old_body_sha256' AS reviewed_old_body_sha256,
       :'checked_body_sha256' AS prior_body_sha256, :'new_body_sha256' AS installed_body_sha256,
       :'apply_patch'::boolean AS applied;

-- Candidate only. After catalog-v2 schema 3. Trusted psql variables:
-- database_name, owner_role, coordinator_role. No runtime owner connection.
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
 IF NOT EXISTS(SELECT 1 FROM em_control.schema_migrations WHERE version=3)
  OR EXISTS(SELECT 1 FROM em_control.schema_migrations WHERE version>=4) THEN
  RAISE EXCEPTION 'invalid_control_schema_version'; END IF;
END; $version$;

CREATE TABLE em_control.acl_projection_plans(
 operation_id uuid PRIMARY KEY REFERENCES em_control.acl_operations(operation_id),
 plan_json jsonb NOT NULL CHECK(jsonb_typeof(plan_json)='object'),
 plan_hash bytea NOT NULL CHECK(octet_length(plan_hash)=32),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE em_control.acl_projection_plans ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.acl_projection_plans FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.acl_projection_plans TO :"owner_role" USING(true) WITH CHECK(true);

CREATE FUNCTION em_control.get_acl_projection_plan(p_operation uuid)
RETURNS TABLE(plan_text text,plan_hash_hex text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
DECLARE o em_control.acl_operations; l em_control.libraries; document jsonb;
 stored em_control.acl_projection_plans; target_acl jsonb; credential_rows jsonb; hash_value bytea;
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

 WITH desired AS(
  SELECT a.principal_id,
   CASE WHEN a.principal_id=o.target_principal_id AND o.requested_permission IS NOT NULL
     THEN o.requested_permission ELSE a.permission END AS permission,
   CASE WHEN a.principal_id=o.target_principal_id THEN o.requested_permission IS NOT NULL ELSE a.active END AS active
  FROM em_control.library_acl a WHERE a.library_id=o.library_id
  UNION ALL
  SELECT o.target_principal_id,o.requested_permission,true
   WHERE o.requested_permission IS NOT NULL AND NOT EXISTS(
    SELECT 1 FROM em_control.library_acl a WHERE a.library_id=o.library_id AND a.principal_id=o.target_principal_id)
 )
 SELECT coalesce(jsonb_agg(jsonb_build_object('principal_id',a.principal_id,'permission',a.permission,
  'active',a.active,'principal_active',p.active,'principal_epoch',p.credential_epoch)
  ORDER BY a.principal_id),'[]'::jsonb) INTO target_acl
 FROM desired a JOIN em_control.principals p USING(principal_id);
 IF jsonb_array_length(target_acl)>256 OR (l.kind='device' AND EXISTS(
  SELECT 1 FROM jsonb_to_recordset(target_acl) a(principal_id uuid,active boolean)
   WHERE a.active AND a.principal_id<>l.owner_principal_id)) THEN
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
 document:=jsonb_build_object('version',1,'operation_id',o.operation_id,'library_id',o.library_id,
  'connection_ref',o.expected_connection_ref,'expected_policy_epoch',o.expected_policy_epoch,
  'target_policy_epoch',o.target_policy_epoch,'route_epoch',o.expected_route_epoch,
  'kind',l.kind,'device_id',l.device_id,'project_id',l.project_id,'owner_principal_id',l.owner_principal_id,
  'evaluation_at',to_char(o.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'payload_hash',encode(o.payload_hash,'hex'),'acls',target_acl,'credentials',credential_rows);
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

REVOKE ALL ON em_control.acl_projection_plans FROM PUBLIC;
REVOKE ALL ON FUNCTION em_control.get_acl_projection_plan(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION em_control.get_acl_projection_plan(uuid) TO :"coordinator_role";
INSERT INTO em_control.schema_migrations(version,migration_name) VALUES(4,'004_control_projection_plan');
COMMIT;

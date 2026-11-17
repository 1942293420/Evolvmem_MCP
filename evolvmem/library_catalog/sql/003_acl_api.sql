-- Unexecuted candidate. Applies after 002; only the trusted non-login owner.
\set ON_ERROR_STOP on
SELECT current_database()=:'database_name' AND current_user=:'owner_role'
 AND EXISTS(SELECT 1 FROM pg_catalog.pg_roles r JOIN pg_catalog.pg_database d ON d.datdba=r.oid
   WHERE r.rolname=current_user AND d.datname=current_database()
    AND NOT(r.rolsuper OR r.rolbypassrls OR r.rolcanlogin OR r.rolcreaterole)) AS preflight_ok \gset
\if :preflight_ok
\else
 \quit 3
\endif
BEGIN;
SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('evolvmem.control.schema',0));
DO $pre$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM em_control.schema_migrations WHERE version=2)
    OR EXISTS(SELECT 1 FROM em_control.schema_migrations WHERE version>=3) THEN
  RAISE EXCEPTION 'invalid_control_schema_version'; END IF;
END $pre$;

-- Old rows remain intact; missing actor bindings cannot be auto-resumed.
ALTER TABLE em_control.acl_operations
 ADD COLUMN actor_credential_id uuid REFERENCES em_control.credentials(credential_id),
 ADD COLUMN actor_device_id uuid REFERENCES em_control.devices(device_id),
 ADD COLUMN actor_credential_epoch bigint CHECK(actor_credential_epoch>0),
 ADD COLUMN actor_principal_epoch bigint CHECK(actor_principal_epoch>0),
 ADD COLUMN actor_device_epoch bigint CHECK(actor_device_epoch>0),
 ADD COLUMN expected_route_epoch bigint CHECK(expected_route_epoch>0),
 ADD COLUMN expected_connection_ref text;
CREATE TABLE em_control.acl_projection_receipts(
 operation_id uuid PRIMARY KEY REFERENCES em_control.acl_operations(operation_id),
 target_policy_epoch bigint NOT NULL CHECK(target_policy_epoch>0),
 content_receipt_hash bytea NOT NULL CHECK(octet_length(content_receipt_hash)=32),
 confirmed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
ALTER TABLE em_control.acl_projection_receipts ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.acl_projection_receipts FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.acl_projection_receipts TO :"owner_role" USING(true) WITH CHECK(true);

CREATE FUNCTION em_control.lookup_acl_operation(p_credential uuid,p_principal uuid,p_device uuid,
 p_credential_epoch bigint,p_principal_epoch bigint,p_device_epoch bigint,p_operation uuid,p_payload_hash bytea)
RETURNS TABLE(operation_id uuid,library_id uuid,target_policy_epoch bigint,operation_state text,
 expected_route_epoch bigint,connection_ref text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
BEGIN
 PERFORM em_control._require_service('coordinate');
 PERFORM em_control._assert_identity(p_credential,p_principal,p_device,
  p_credential_epoch,p_principal_epoch,p_device_epoch);
 RETURN QUERY SELECT o.operation_id,o.library_id,o.target_policy_epoch,o.operation_state,
  o.expected_route_epoch,o.expected_connection_ref
 FROM em_control.acl_operations o WHERE o.operation_id=p_operation AND o.payload_hash=p_payload_hash
  AND o.actor_principal_id=p_principal AND o.actor_credential_id=p_credential AND o.actor_device_id=p_device;
END; $fn$;

CREATE FUNCTION em_control.begin_acl_change(p_credential uuid,p_principal uuid,p_device uuid,
 p_credential_epoch bigint,p_principal_epoch bigint,p_device_epoch bigint,p_library uuid,
 p_target uuid,p_permission text,p_expected_epoch bigint,p_operation uuid,p_request_id text,p_payload_hash bytea)
RETURNS TABLE(operation_id uuid,library_id uuid,target_policy_epoch bigint,operation_state text,
 expected_route_epoch bigint,connection_ref text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
DECLARE a em_control.authorization_row; prior em_control.acl_operations;
BEGIN
 PERFORM em_control._require_service('coordinate');
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
   OR prior.payload_hash<>p_payload_hash THEN
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
 IF NOT FOUND OR a.permission<>'admin' OR NOT a.library_active OR a.acl_state<>'ready'
  OR a.policy_epoch<>p_expected_epoch OR a.backend<>'postgres'
  OR a.authority_state<>'pg_active' OR a.write_backend<>'postgres' THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='acl_admin_required'; END IF;
 IF NOT EXISTS(SELECT 1 FROM em_control.principals p WHERE p.principal_id=p_target
  AND(p_permission IS NULL OR p.active)) THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='acl_target_unavailable'; END IF;
 INSERT INTO em_control.acl_operations(operation_id,request_id,library_id,actor_principal_id,target_principal_id,
  requested_permission,expected_policy_epoch,target_policy_epoch,payload_hash,actor_credential_id,actor_device_id,
  actor_credential_epoch,actor_principal_epoch,actor_device_epoch,expected_route_epoch,expected_connection_ref)
 VALUES(p_operation,p_request_id,p_library,p_principal,p_target,p_permission,p_expected_epoch,p_expected_epoch+1,
  p_payload_hash,p_credential,p_device,p_credential_epoch,p_principal_epoch,p_device_epoch,a.route_epoch,a.connection_ref);
 UPDATE em_control.library_routes SET acl_state='updating' WHERE em_control.library_routes.library_id=p_library;
 RETURN QUERY SELECT p_operation,p_library,p_expected_epoch+1,'pending'::text,a.route_epoch,a.connection_ref;
END $fn$;

-- Internal helper: same principal/device/credential/library/route order as pin.
-- The operation identity is immutable through every exposed API.
CREATE FUNCTION em_control._lock_acl_operation(p_operation uuid) RETURNS em_control.acl_operations
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
DECLARE o em_control.acl_operations;
BEGIN
 PERFORM em_control._require_service('coordinate');
 SELECT * INTO o FROM em_control.acl_operations x WHERE x.operation_id=p_operation;
 IF NOT FOUND OR o.actor_credential_id IS NULL OR o.expected_route_epoch IS NULL OR o.expected_connection_ref IS NULL THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='acl_operation_unavailable'; END IF;
 PERFORM 1 FROM em_control.principals p WHERE p.principal_id=o.actor_principal_id FOR SHARE;
 PERFORM 1 FROM em_control.devices d WHERE d.device_id=o.actor_device_id FOR SHARE;
 PERFORM 1 FROM em_control.credentials c WHERE c.credential_id=o.actor_credential_id FOR SHARE;
 PERFORM 1 FROM em_control.libraries l WHERE l.library_id=o.library_id FOR UPDATE;
 PERFORM 1 FROM em_control.library_routes r WHERE r.library_id=o.library_id FOR UPDATE;
 SELECT * INTO o FROM em_control.acl_operations x WHERE x.operation_id=p_operation FOR UPDATE;
 PERFORM em_control._assert_identity(o.actor_credential_id,o.actor_principal_id,o.actor_device_id,
  o.actor_credential_epoch,o.actor_principal_epoch,o.actor_device_epoch);
 IF NOT EXISTS(SELECT 1 FROM em_control.libraries l JOIN em_control.library_routes r USING(library_id)
  WHERE l.library_id=o.library_id AND l.active AND l.backend='postgres'
   AND l.connection_ref=o.expected_connection_ref AND r.route_epoch=o.expected_route_epoch
   AND r.authority_state='pg_active' AND r.write_backend='postgres') THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='acl_route_changed'; END IF;
 IF o.operation_state<>'complete' AND NOT EXISTS(SELECT 1 FROM em_control.library_routes r
  WHERE r.library_id=o.library_id AND r.policy_epoch=o.expected_policy_epoch
   AND r.acl_state IN('updating','failed')) THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='acl_operation_stale'; END IF;
 RETURN o;
END $fn$;

CREATE FUNCTION em_control.confirm_acl_projection(p_operation uuid,p_route_epoch bigint,p_connection_ref text,p_content_receipt_hash bytea)
RETURNS TABLE(operation_id uuid,library_id uuid,target_policy_epoch bigint,operation_state text,
 expected_route_epoch bigint,connection_ref text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
DECLARE o em_control.acl_operations; old_hash bytea;
BEGIN
 o:=em_control._lock_acl_operation(p_operation);
 IF p_route_epoch IS DISTINCT FROM o.expected_route_epoch OR p_connection_ref IS DISTINCT FROM o.expected_connection_ref THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='projection_route_mismatch'; END IF;
 -- FOR UPDATE is held on route/operation by _lock_acl_operation until COMMIT.
 IF p_content_receipt_hash IS NULL OR octet_length(p_content_receipt_hash)<>32 THEN
  RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid_projection_receipt'; END IF;
 SELECT x.content_receipt_hash INTO old_hash FROM em_control.acl_projection_receipts x
  WHERE x.operation_id=p_operation FOR UPDATE;
 IF FOUND AND old_hash<>p_content_receipt_hash THEN
  RAISE EXCEPTION USING ERRCODE='23505',MESSAGE='projection_receipt_conflict'; END IF;
 IF o.operation_state='complete' THEN
  IF old_hash IS NULL THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='projection_receipt_missing'; END IF;
  RETURN QUERY SELECT o.operation_id,o.library_id,o.target_policy_epoch,o.operation_state,
   o.expected_route_epoch,o.expected_connection_ref; RETURN;
 END IF;
 IF NOT EXISTS(SELECT 1 FROM em_control.library_routes r WHERE r.library_id=o.library_id
  AND r.policy_epoch=o.expected_policy_epoch AND r.acl_state IN('updating','failed')) THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='acl_operation_stale'; END IF;
 INSERT INTO em_control.acl_projection_receipts(operation_id,target_policy_epoch,content_receipt_hash)
  VALUES(p_operation,o.target_policy_epoch,p_content_receipt_hash)
  ON CONFLICT ON CONSTRAINT acl_projection_receipts_pkey DO NOTHING;
 UPDATE em_control.acl_operations SET operation_state='applying',error_code=NULL
  WHERE em_control.acl_operations.operation_id=p_operation;
 UPDATE em_control.library_routes SET acl_state='updating' WHERE em_control.library_routes.library_id=o.library_id;
 RETURN QUERY SELECT o.operation_id,o.library_id,o.target_policy_epoch,'applying'::text,
  o.expected_route_epoch,o.expected_connection_ref;
END $fn$;

CREATE FUNCTION em_control.finish_acl_change(p_operation uuid)
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
 IF o.requested_permission IS NULL THEN
  UPDATE em_control.library_acl SET active=false
   WHERE em_control.library_acl.library_id=o.library_id AND principal_id=o.target_principal_id;
 ELSE
  INSERT INTO em_control.library_acl(library_id,principal_id,permission,active,policy_epoch,granted_by)
   VALUES(o.library_id,o.target_principal_id,o.requested_permission,true,o.target_policy_epoch,o.actor_principal_id)
   ON CONFLICT ON CONSTRAINT library_acl_pkey DO UPDATE SET permission=EXCLUDED.permission,active=true,
     policy_epoch=EXCLUDED.policy_epoch,granted_by=EXCLUDED.granted_by;
 END IF;
 UPDATE em_control.library_acl SET policy_epoch=o.target_policy_epoch
  WHERE em_control.library_acl.library_id=o.library_id;
 UPDATE em_control.library_routes SET policy_epoch=o.target_policy_epoch,acl_state='ready'
  WHERE em_control.library_routes.library_id=o.library_id;
 UPDATE em_control.acl_operations SET operation_state='complete',completed_at=clock_timestamp(),error_code=NULL
  WHERE em_control.acl_operations.operation_id=p_operation;
 RETURN QUERY SELECT o.operation_id,o.library_id,o.target_policy_epoch,'complete'::text,
  o.expected_route_epoch,o.expected_connection_ref;
END $fn$;

CREATE FUNCTION em_control.fail_acl_change(p_operation uuid,p_error_code text)
RETURNS TABLE(operation_id uuid,library_id uuid,target_policy_epoch bigint,operation_state text,
 expected_route_epoch bigint,connection_ref text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
DECLARE o em_control.acl_operations;
BEGIN
 o:=em_control._lock_acl_operation(p_operation);
 PERFORM 1 FROM em_control.library_routes r WHERE r.library_id=o.library_id FOR UPDATE;
 IF o.operation_state='complete' THEN
  RETURN QUERY SELECT o.operation_id,o.library_id,o.target_policy_epoch,o.operation_state,
   o.expected_route_epoch,o.expected_connection_ref; RETURN;
 END IF;
 IF p_error_code IS NULL OR p_error_code!~'^[a-z][a-z0-9_]{0,63}$' THEN
  RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid_failure_code'; END IF;
 UPDATE em_control.acl_operations SET operation_state='failed',error_code=p_error_code
  WHERE em_control.acl_operations.operation_id=p_operation;
 UPDATE em_control.library_routes SET acl_state='failed' WHERE em_control.library_routes.library_id=o.library_id;
 RETURN QUERY SELECT o.operation_id,o.library_id,o.target_policy_epoch,'failed'::text,
  o.expected_route_epoch,o.expected_connection_ref;
END $fn$;

REVOKE ALL ON ALL TABLES IN SCHEMA em_control FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA em_control FROM PUBLIC;
INSERT INTO em_control.schema_migrations(version,migration_name) VALUES(3,'003_acl_api');
COMMIT;

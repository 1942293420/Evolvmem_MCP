-- Unexecuted candidate. PostgreSQL 17+, psql; trusted database_name/owner_role.
-- Requires foundation control schema v1. Never run as an HTTP connection.
\set ON_ERROR_STOP on
SELECT current_database()=:'database_name' AND current_user=:'owner_role'
 AND EXISTS(SELECT 1 FROM pg_catalog.pg_roles r JOIN pg_catalog.pg_database d ON d.datdba=r.oid
   WHERE r.rolname=current_user AND d.datname=current_database()
     AND NOT r.rolsuper AND NOT r.rolbypassrls AND NOT r.rolcanlogin AND NOT r.rolcreaterole)
 AS preflight_ok \gset
\if :preflight_ok
\else
 \quit 3
\endif
BEGIN;
SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('evolvmem.control.schema',0));
DO $pre$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM em_control.schema_migrations WHERE version=1)
    OR EXISTS(SELECT 1 FROM em_control.schema_migrations WHERE version>=2) THEN
   RAISE EXCEPTION 'invalid_control_schema_version';
 END IF;
END $pre$;

CREATE TABLE em_control.service_roles(
 login_role name PRIMARY KEY,
 can_read boolean NOT NULL DEFAULT false,
 can_coordinate boolean NOT NULL DEFAULT false,
 enabled boolean NOT NULL DEFAULT false,
 CHECK(NOT can_coordinate OR can_read)
);
CREATE TABLE em_control.external_devices(
 principal_id uuid NOT NULL REFERENCES em_control.principals(principal_id),
 external_device_id text COLLATE "C" NOT NULL CHECK(length(external_device_id) BETWEEN 1 AND 256),
 device_id uuid NOT NULL,
 active boolean NOT NULL DEFAULT false,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(principal_id,external_device_id),
 FOREIGN KEY(device_id,principal_id) REFERENCES em_control.devices(device_id,principal_id)
);
ALTER TABLE em_control.service_roles ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.service_roles FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.service_roles TO :"owner_role" USING(true) WITH CHECK(true);
ALTER TABLE em_control.external_devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.external_devices FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.external_devices TO :"owner_role" USING(true) WITH CHECK(true);

CREATE TYPE em_control.auth_row AS(
 credential_id uuid,principal_id uuid,device_id uuid,credential_digest bytea,
 credential_epoch bigint,principal_epoch bigint,device_epoch bigint,
 credential_active boolean,principal_active boolean,device_active boolean,expires_at timestamptz
);
CREATE TYPE em_control.authorization_row AS(
 credential_id uuid,principal_id uuid,device_id uuid,credential_digest bytea,
 credential_epoch bigint,principal_epoch bigint,device_epoch bigint,
 credential_active boolean,principal_active boolean,device_active boolean,expires_at timestamptz,
 library_id uuid,kind text,backend text,connection_ref text,policy_epoch bigint,route_epoch bigint,
 authority_state text,acl_state text,write_backend text,project_id uuid,library_device_id uuid,
 owner_principal_id uuid,library_active boolean,permission text
);
CREATE VIEW em_control.credential_projection WITH(security_barrier=true) AS
 SELECT c.credential_id,c.principal_id,c.device_id,c.credential_digest,
 c.credential_epoch,p.credential_epoch AS principal_epoch,d.device_epoch,
 c.active AS credential_active,p.active AS principal_active,d.active AS device_active,c.expires_at
 FROM em_control.credentials c JOIN em_control.principals p USING(principal_id)
 JOIN em_control.devices d ON d.device_id=c.device_id AND d.principal_id=c.principal_id;
CREATE VIEW em_control.authorization_projection WITH(security_barrier=true) AS
 SELECT c.*,l.library_id,l.kind,l.backend,l.connection_ref,r.policy_epoch,r.route_epoch,
 r.authority_state,r.acl_state,r.write_backend,l.project_id,l.device_id AS library_device_id,
 l.owner_principal_id,l.active AS library_active,a.permission
 FROM em_control.credential_projection c
 JOIN em_control.library_acl a ON a.principal_id=c.principal_id AND a.active
 JOIN em_control.libraries l ON l.library_id=a.library_id
 JOIN em_control.library_routes r ON r.library_id=l.library_id AND r.policy_epoch=a.policy_epoch;

CREATE FUNCTION em_control._require_service(p_capability text) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
BEGIN
 IF NOT EXISTS(SELECT 1 FROM em_control.service_roles s JOIN pg_catalog.pg_roles r ON r.rolname=s.login_role
   WHERE s.login_role=session_user AND s.enabled AND r.rolcanlogin
     AND NOT(r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb)
     AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member=r.oid)
     AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_class c
       JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
       WHERE n.nspname='em_control' AND c.relkind IN('r','p','v','m','f')
        AND pg_catalog.has_table_privilege(r.oid,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'))
     AND NOT pg_catalog.pg_has_role(session_user,current_user,'MEMBER')
     AND CASE p_capability WHEN 'read' THEN s.can_read WHEN 'coordinate' THEN s.can_coordinate ELSE false END)
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='control_service_forbidden'; END IF;
END; $fn$;

CREATE FUNCTION em_control._assert_identity(p_credential uuid,p_principal uuid,p_device uuid,
 p_credential_epoch bigint,p_principal_epoch bigint,p_device_epoch bigint) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
BEGIN
 PERFORM em_control._require_service('read');
 IF NOT EXISTS(SELECT 1 FROM em_control.credential_projection c
  WHERE c.credential_id=p_credential AND c.principal_id=p_principal AND c.device_id=p_device
   AND c.credential_epoch=p_credential_epoch AND c.principal_epoch=p_principal_epoch AND c.device_epoch=p_device_epoch
   AND c.credential_active AND c.principal_active AND c.device_active
   AND(c.expires_at IS NULL OR c.expires_at>clock_timestamp()))
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='credential_binding_invalid'; END IF;
END $fn$;

CREATE FUNCTION em_control.auth_binding(p_digest bytea) RETURNS SETOF em_control.auth_row
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
BEGIN
 PERFORM em_control._require_service('read');
 IF octet_length(p_digest)<>32 THEN RETURN; END IF;
 RETURN QUERY SELECT c.* FROM em_control.credential_projection c WHERE c.credential_digest=p_digest
  AND c.credential_active AND c.principal_active AND c.device_active
  AND(c.expires_at IS NULL OR c.expires_at>clock_timestamp());
END $fn$;

CREATE FUNCTION em_control.resolve_library(p_credential uuid,p_principal uuid,p_device uuid,
 p_credential_epoch bigint,p_principal_epoch bigint,p_device_epoch bigint,p_library uuid)
RETURNS SETOF em_control.authorization_row
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
BEGIN
 PERFORM em_control._assert_identity(p_credential,p_principal,p_device,p_credential_epoch,p_principal_epoch,p_device_epoch);
 RETURN QUERY SELECT a.* FROM em_control.authorization_projection a
  WHERE a.credential_id=p_credential AND a.library_id=p_library;
END $fn$;

CREATE FUNCTION em_control.resolve_external_device(p_credential uuid,p_principal uuid,p_device uuid,
 p_credential_epoch bigint,p_principal_epoch bigint,p_device_epoch bigint,p_external text)
RETURNS TABLE(device_id uuid,external_device_id text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
BEGIN
 PERFORM em_control._assert_identity(p_credential,p_principal,p_device,p_credential_epoch,p_principal_epoch,p_device_epoch);
 RETURN QUERY SELECT x.device_id,x.external_device_id FROM em_control.external_devices x
  WHERE x.principal_id=p_principal AND x.device_id=p_device AND x.external_device_id=p_external COLLATE "C" AND x.active;
END $fn$;

CREATE FUNCTION em_control.list_libraries(p_credential uuid,p_principal uuid,p_device uuid,
 p_credential_epoch bigint,p_principal_epoch bigint,p_device_epoch bigint,p_limit integer,p_after uuid)
RETURNS TABLE(library_id uuid,kind text,backend text,permission text,policy_epoch bigint,route_epoch bigint,
 authority_state text,acl_state text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
BEGIN
 PERFORM em_control._assert_identity(p_credential,p_principal,p_device,p_credential_epoch,p_principal_epoch,p_device_epoch);
 IF p_limit IS NULL OR p_limit<1 OR p_limit>100 THEN
  RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid_catalog_limit'; END IF;
 RETURN QUERY SELECT a.library_id,a.kind,a.backend,a.permission,a.policy_epoch,a.route_epoch,a.authority_state,a.acl_state
 FROM em_control.authorization_projection a WHERE a.credential_id=p_credential AND a.library_active
  AND(p_after IS NULL OR a.library_id>p_after) ORDER BY a.library_id LIMIT p_limit;
END $fn$;

CREATE FUNCTION em_control.pin_library(p_credential uuid,p_principal uuid,p_device uuid,
 p_credential_epoch bigint,p_principal_epoch bigint,p_device_epoch bigint,p_library uuid,
 p_policy_epoch bigint,p_route_epoch bigint,p_action text)
RETURNS SETOF em_control.authorization_row
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
DECLARE a em_control.authorization_row;
BEGIN
 PERFORM em_control._require_service('read');
 IF p_policy_epoch IS NULL OR p_policy_epoch<1 OR p_route_epoch IS NULL OR p_route_epoch<1
  OR p_action IS NULL OR p_action NOT IN('read','write','admin') THEN
  RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid_authority_pin'; END IF;
 -- Exact global order: principal, device, credential, library, route, ACL.
 -- READ COMMITTED + VOLATILE rechecks after lock waits; no stale RR control snapshot.
 PERFORM 1 FROM em_control.principals p WHERE p.principal_id=p_principal FOR SHARE;
 PERFORM 1 FROM em_control.devices d WHERE d.device_id=p_device FOR SHARE;
 PERFORM 1 FROM em_control.credentials c WHERE c.credential_id=p_credential FOR SHARE;
 PERFORM 1 FROM em_control.libraries l WHERE l.library_id=p_library FOR SHARE;
 PERFORM 1 FROM em_control.library_routes r WHERE r.library_id=p_library FOR SHARE;
 PERFORM 1 FROM em_control.library_acl x WHERE x.library_id=p_library AND x.principal_id=p_principal FOR SHARE;
 SELECT * INTO a FROM em_control.resolve_library(p_credential,p_principal,p_device,
  p_credential_epoch,p_principal_epoch,p_device_epoch,p_library);
 IF NOT FOUND OR NOT a.library_active OR a.policy_epoch<>p_policy_epoch OR a.route_epoch<>p_route_epoch
  OR a.acl_state<>'ready' OR NOT((a.backend='postgres' AND a.authority_state='pg_active')
    OR(a.backend='sqlite' AND a.authority_state='sqlite_active'))
  OR NOT (CASE p_action WHEN 'read' THEN a.permission IN('read','write','admin')
     WHEN 'write' THEN a.permission IN('write','admin') AND a.write_backend=a.backend
     WHEN 'admin' THEN a.permission='admin' AND a.write_backend=a.backend ELSE false END)
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='library_authority_changed'; END IF;
 RETURN NEXT a;
END $fn$;

-- Enrollment is explicit, immutable by this API, and cannot rebind a hook label.
CREATE FUNCTION em_control.enroll_external_device(p_principal uuid,p_device uuid,p_external text) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
DECLARE prior uuid;
BEGIN
 PERFORM em_control._require_service('coordinate');
 PERFORM 1 FROM em_control.principals p WHERE p.principal_id=p_principal AND p.active FOR UPDATE;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='principal_unavailable'; END IF;
 PERFORM 1 FROM em_control.devices d WHERE d.device_id=p_device AND d.principal_id=p_principal AND d.active FOR UPDATE;
 IF NOT FOUND OR p_external IS NULL OR length(p_external) NOT BETWEEN 1 AND 256 THEN
  RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='device_enrollment_invalid'; END IF;
 SELECT x.device_id INTO prior FROM em_control.external_devices x
  WHERE x.principal_id=p_principal AND x.external_device_id=p_external COLLATE "C" FOR UPDATE;
 IF FOUND THEN
  IF prior<>p_device THEN RAISE EXCEPTION USING ERRCODE='23505',MESSAGE='device_enrollment_conflict'; END IF;
  RETURN;
 END IF;
 INSERT INTO em_control.external_devices(principal_id,external_device_id,device_id,active)
  VALUES(p_principal,p_external,p_device,true);
END $fn$;

REVOKE ALL ON ALL TABLES IN SCHEMA em_control FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA em_control FROM PUBLIC;
INSERT INTO em_control.schema_migrations(version,migration_name) VALUES(2,'002_catalog_api');
COMMIT;

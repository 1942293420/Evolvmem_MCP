-- Additive companion to control migration v7: Trusted device registration,
-- PG library project enrollment, and per-device project ACL scoping.
-- Trusted psql variables: database_name, owner_role, coordinator_role.
\set ON_ERROR_STOP on
SELECT current_database() = :'database_name' AND current_user = :'owner_role'
  AND EXISTS (SELECT 1 FROM pg_catalog.pg_roles r JOIN pg_catalog.pg_database d ON d.datdba = r.oid
    WHERE r.rolname = current_user AND d.datname = current_database()
      AND NOT (r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb))
  AND EXISTS (SELECT 1 FROM em_control.service_roles s
    WHERE s.login_role = :'coordinator_role' AND s.enabled AND s.can_coordinate)
  AS preflight_ok \gset
\if :preflight_ok
\else
  \quit 3
\endif

BEGIN;
SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('evolvmem.control.schema', 0));
DO $guard$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM em_control.schema_migrations WHERE version = 7) THEN
    RAISE EXCEPTION 'invalid_console_device_project_schema_version';
  END IF;
END $guard$;

CREATE TABLE em_control.device_project_policies (
    device_id uuid PRIMARY KEY,
    principal_id uuid NOT NULL,
    unrestricted_own_projects boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (device_id, principal_id) REFERENCES em_control.devices(device_id, principal_id)
);
CREATE TABLE em_control.device_project_acl (
    device_id uuid NOT NULL,
    principal_id uuid NOT NULL,
    library_id uuid NOT NULL REFERENCES em_control.libraries(library_id),
    project_id uuid NOT NULL REFERENCES em_control.project_registry(project_id),
    permission text NOT NULL CHECK (permission IN ('read', 'write', 'admin')),
    active boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (device_id, library_id, project_id),
    FOREIGN KEY (device_id, principal_id) REFERENCES em_control.devices(device_id, principal_id)
);

ALTER TABLE em_control.device_project_policies ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.device_project_policies FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.device_project_policies TO :"owner_role" USING (true) WITH CHECK (true);
ALTER TABLE em_control.device_project_acl ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.device_project_acl FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.device_project_acl TO :"owner_role" USING (true) WITH CHECK (true);

REVOKE ALL ON ALL TABLES IN SCHEMA em_control FROM PUBLIC;

CREATE FUNCTION em_control.register_trusted_device(
    p_user_id text, p_device_id uuid, p_external_device_id text, p_credential_id uuid,
    p_credential_digest bytea, p_expires_at timestamptz, p_unrestricted_own_projects boolean
) RETURNS uuid
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
DECLARE v_principal uuid; v_enabled boolean;
BEGIN
  PERFORM em_control._require_service('coordinate');
  SELECT principal_id, enabled INTO v_principal, v_enabled
  FROM em_control.console_users WHERE user_id = p_user_id FOR UPDATE;
  IF v_principal IS NULL OR NOT v_enabled THEN
    RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'account_disabled';
  END IF;
  INSERT INTO em_control.devices(device_id, principal_id, device_fingerprint, active, device_epoch)
  VALUES (p_device_id, v_principal, p_user_id || ':' || p_external_device_id, true, 1)
  ON CONFLICT (device_id) DO UPDATE SET active = true;
  INSERT INTO em_control.external_devices(principal_id, external_device_id, device_id, active)
  VALUES (v_principal, p_external_device_id, p_device_id, true)
  ON CONFLICT (principal_id, external_device_id) DO UPDATE SET device_id = EXCLUDED.device_id, active = true;
  INSERT INTO em_control.credentials(credential_id, principal_id, device_id, credential_digest, active, credential_epoch, expires_at)
  VALUES (p_credential_id, v_principal, p_device_id, p_credential_digest, true, 1, p_expires_at);
  INSERT INTO em_control.device_project_policies(device_id, principal_id, unrestricted_own_projects)
  VALUES (p_device_id, v_principal, coalesce(p_unrestricted_own_projects, false))
  ON CONFLICT (device_id) DO UPDATE SET unrestricted_own_projects = EXCLUDED.unrestricted_own_projects, updated_at = clock_timestamp();
  UPDATE em_control.console_users SET lan_enabled = true WHERE user_id = p_user_id;
  RETURN v_principal;
END $fn$;

CREATE FUNCTION em_control.register_pg_library_with_projects(
    p_owner_user_id text, p_library_id uuid, p_connection_ref text, p_projects jsonb
) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
DECLARE v_principal uuid; elem record;
BEGIN
  PERFORM em_control._require_service('coordinate');
  SELECT principal_id INTO v_principal FROM em_control.console_users WHERE user_id = p_owner_user_id;
  IF v_principal IS NULL THEN
    RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'unknown_user';
  END IF;
  INSERT INTO em_control.libraries(library_id, kind, owner_principal_id, backend, connection_ref, schema_version, active)
  VALUES (p_library_id, 'legacy', v_principal, 'postgres', p_connection_ref, 1, true);
  INSERT INTO em_control.library_routes(library_id, route_epoch, policy_epoch, authority_state, write_backend, acl_state)
  VALUES (p_library_id, 1, 1, 'pg_active', 'postgres', 'ready');
  INSERT INTO em_control.library_acl(library_id, principal_id, permission, active, policy_epoch, granted_by)
  VALUES (p_library_id, v_principal, 'admin', true, 1, v_principal);
  FOR elem IN SELECT * FROM jsonb_to_recordset(coalesce(p_projects, '[]'::jsonb)) AS x(project_id uuid, project_key text) LOOP
    INSERT INTO em_control.project_registry(project_id, project_key)
    VALUES (elem.project_id, elem.project_key) ON CONFLICT (project_id) DO NOTHING;
    INSERT INTO em_control.library_projects(library_id, project_id, active)
    VALUES (p_library_id, elem.project_id, true) ON CONFLICT (library_id, project_id) DO UPDATE SET active = true;
  END LOOP;
END $fn$;

CREATE FUNCTION em_control.grant_device_project(
    p_device_id uuid, p_library_id uuid, p_project_id uuid, p_project_key text, p_permission text, p_active boolean
) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
DECLARE v_principal uuid;
BEGIN
  PERFORM em_control._require_service('coordinate');
  SELECT principal_id INTO v_principal FROM em_control.devices WHERE device_id = p_device_id AND active;
  IF v_principal IS NULL THEN
    RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'device_inactive_or_unknown';
  END IF;
  INSERT INTO em_control.project_registry(project_id, project_key)
  VALUES (p_project_id, p_project_key) ON CONFLICT (project_id) DO NOTHING;
  INSERT INTO em_control.device_project_acl(device_id, principal_id, library_id, project_id, permission, active, updated_at)
  VALUES (p_device_id, v_principal, p_library_id, p_project_id, p_permission, p_active, clock_timestamp())
  ON CONFLICT (device_id, library_id, project_id) DO UPDATE
  SET permission = EXCLUDED.permission, active = EXCLUDED.active, updated_at = clock_timestamp();
END $fn$;

CREATE FUNCTION em_control.resolve_device_project_access(
    p_credential uuid, p_principal uuid, p_device uuid,
    p_credential_epoch bigint, p_principal_epoch bigint, p_device_epoch bigint,
    p_library uuid, p_project_key text, p_action text
) RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
DECLARE v_rank int; v_lib_owner uuid; v_proj_id uuid; v_unrestricted boolean;
        v_dev_perm text; v_proj_perm text;
BEGIN
  PERFORM em_control._require_service('read');
  v_rank := CASE p_action WHEN 'read' THEN 1 WHEN 'write' THEN 2 WHEN 'admin' THEN 3 ELSE 99 END;
  IF v_rank = 99 THEN RETURN false; END IF;
  IF NOT EXISTS (
    SELECT 1 FROM em_control.credential_projection c
    WHERE c.credential_id = p_credential AND c.principal_id = p_principal AND c.device_id = p_device
      AND c.credential_epoch = p_credential_epoch AND c.principal_epoch = p_principal_epoch AND c.device_epoch = p_device_epoch
      AND c.credential_active AND c.principal_active AND c.device_active
      AND (c.expires_at IS NULL OR c.expires_at > clock_timestamp())
  ) THEN
    RETURN false;
  END IF;
  SELECT l.owner_principal_id INTO v_lib_owner
  FROM em_control.libraries l WHERE l.library_id = p_library AND l.active;
  IF v_lib_owner IS NULL THEN RETURN false; END IF;
  SELECT pr.project_id INTO v_proj_id
  FROM em_control.project_registry pr JOIN em_control.library_projects lp USING (project_id)
  WHERE lp.library_id = p_library AND lp.active AND pr.project_key = p_project_key;
  IF v_proj_id IS NULL THEN RETURN false; END IF;
  IF v_lib_owner <> p_principal THEN
    SELECT pa.permission INTO v_proj_perm
    FROM em_control.library_project_acl pa JOIN em_control.library_acl la
      ON la.library_id = pa.library_id AND la.principal_id = pa.principal_id AND la.active
    WHERE pa.library_id = p_library AND pa.project_id = v_proj_id AND pa.principal_id = p_principal AND pa.active;
    IF v_proj_perm IS NULL OR (CASE v_proj_perm WHEN 'read' THEN 1 WHEN 'write' THEN 2 WHEN 'admin' THEN 3 ELSE 0 END) < v_rank THEN
      RETURN false;
    END IF;
  END IF;
  SELECT coalesce(dp.unrestricted_own_projects, false) INTO v_unrestricted
  FROM em_control.device_project_policies dp WHERE dp.device_id = p_device AND dp.principal_id = p_principal;
  IF coalesce(v_unrestricted, false) AND v_lib_owner = p_principal THEN
    RETURN true;
  END IF;
  SELECT dpa.permission INTO v_dev_perm
  FROM em_control.device_project_acl dpa
  WHERE dpa.device_id = p_device AND dpa.principal_id = p_principal
    AND dpa.library_id = p_library AND dpa.project_id = v_proj_id AND dpa.active;
  IF v_dev_perm IS NULL THEN RETURN false; END IF;
  RETURN (CASE v_dev_perm WHEN 'read' THEN 1 WHEN 'write' THEN 2 WHEN 'admin' THEN 3 ELSE 0 END) >= v_rank;
END $fn$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA em_control FROM PUBLIC;
COMMIT;

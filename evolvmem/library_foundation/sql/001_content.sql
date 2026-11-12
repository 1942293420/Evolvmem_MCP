-- Candidate v1. EMPTY dedicated content database; already-created non-login owner.
-- Trusted psql variables: database_name, owner_role. No business content tables yet.
\set ON_ERROR_STOP on
SELECT current_database() = :'database_name' AND current_user = :'owner_role'
  AND EXISTS (SELECT 1 FROM pg_catalog.pg_roles r JOIN pg_catalog.pg_database d
    ON d.datdba = r.oid WHERE r.rolname = current_user AND d.datname = current_database()
    AND NOT r.rolsuper AND NOT r.rolbypassrls AND NOT r.rolcanlogin AND NOT r.rolcreaterole)
  AS preflight_ok \gset
\if :preflight_ok
\else
  \echo 'Content migration refused: database/owner role preflight failed.'
  \quit 3
\endif

BEGIN;
SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('evolvmem.content.schema', 0));
REVOKE ALL ON DATABASE :"database_name" FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA em_security AUTHORIZATION :"owner_role";
CREATE SCHEMA em_content AUTHORIZATION :"owner_role";
REVOKE ALL ON SCHEMA em_security FROM PUBLIC;
REVOKE ALL ON SCHEMA em_content FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_role" IN SCHEMA em_security REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_role" IN SCHEMA em_security REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_role" IN SCHEMA em_content REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_role" IN SCHEMA em_content REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE TABLE em_security.schema_migrations (
    version integer PRIMARY KEY CHECK (version > 0),
    migration_name text NOT NULL UNIQUE,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE em_security.library_state (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    library_id uuid NOT NULL UNIQUE,
    kind text NOT NULL CHECK (kind IN ('device', 'project', 'legacy')),
    device_id uuid,
    project_id uuid,
    policy_epoch bigint NOT NULL CHECK (policy_epoch > 0),
    route_epoch bigint NOT NULL CHECK (route_epoch > 0),
    authority_state text NOT NULL CHECK (authority_state IN
        ('sqlite_active', 'pg_staging', 'verify', 'freeze_and_reconcile', 'pg_active')),
    acl_state text NOT NULL DEFAULT 'updating' CHECK (acl_state IN ('ready', 'updating', 'failed')),
    CHECK ((kind = 'device' AND device_id IS NOT NULL AND project_id IS NULL)
        OR (kind = 'project' AND project_id IS NOT NULL AND device_id IS NULL)
        OR (kind = 'legacy' AND project_id IS NULL AND device_id IS NULL))
);
CREATE TABLE em_security.principal_bindings (
    login_role name PRIMARY KEY,
    principal_id uuid NOT NULL,
    credential_id uuid NOT NULL,
    authenticated_device_id uuid NOT NULL,
    credential_epoch bigint NOT NULL CHECK (credential_epoch > 0),
    expires_at timestamptz,
    enabled boolean NOT NULL DEFAULT false
);
CREATE TABLE em_security.acl_projection (
    principal_id uuid PRIMARY KEY,
    permission text NOT NULL CHECK (permission IN ('read', 'write', 'admin')),
    active boolean NOT NULL DEFAULT false,
    projection_epoch bigint NOT NULL CHECK (projection_epoch > 0)
);
CREATE TABLE em_security.applied_acl_operations (
    operation_id uuid PRIMARY KEY,
    target_policy_epoch bigint NOT NULL CHECK (target_policy_epoch > 0),
    payload_hash bytea NOT NULL CHECK (octet_length(payload_hash) = 32),
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

ALTER TABLE em_security.schema_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_security.schema_migrations FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_security.schema_migrations TO :"owner_role" USING (true) WITH CHECK (true);
ALTER TABLE em_security.library_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_security.library_state FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_security.library_state TO :"owner_role" USING (true) WITH CHECK (true);
ALTER TABLE em_security.principal_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_security.principal_bindings FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_security.principal_bindings TO :"owner_role" USING (true) WITH CHECK (true);
ALTER TABLE em_security.acl_projection ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_security.acl_projection FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_security.acl_projection TO :"owner_role" USING (true) WITH CHECK (true);
ALTER TABLE em_security.applied_acl_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_security.applied_acl_operations FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_security.applied_acl_operations TO :"owner_role" USING (true) WITH CHECK (true);

-- SECURITY DEFINER is only a narrow read of owner-only projections. Its owner is
-- the non-login migration owner, search_path is fixed, object names are qualified,
-- there is no caller-supplied principal argument and no caller-controlled GUC.
CREATE FUNCTION em_security.session_context()
RETURNS TABLE(principal_id uuid, library_id uuid, policy_epoch bigint, route_epoch bigint,
              permission text, authority_state text, acl_state text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, em_security
AS $function$
    SELECT b.principal_id, s.library_id, s.policy_epoch, s.route_epoch,
           a.permission, s.authority_state, s.acl_state
    FROM em_security.principal_bindings b
    JOIN em_security.acl_projection a ON a.principal_id = b.principal_id
    CROSS JOIN em_security.library_state s
    WHERE b.login_role = session_user AND b.enabled AND a.active
      AND (b.expires_at IS NULL OR b.expires_at > pg_catalog.statement_timestamp())
      AND a.projection_epoch = s.policy_epoch
      AND EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname = session_user
                  AND NOT r.rolsuper AND NOT r.rolbypassrls AND NOT r.rolcreaterole)
$function$;

-- A second narrow projection binds the pooled login to the precise authenticated
-- credential and device, not merely to a principal who may own several devices.
CREATE FUNCTION em_security.session_credential()
RETURNS TABLE(credential_id uuid, authenticated_device_id uuid, credential_epoch bigint)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, em_security
AS $function$
    SELECT b.credential_id, b.authenticated_device_id, b.credential_epoch
    FROM em_security.principal_bindings b
    WHERE b.login_role = session_user AND b.enabled
      AND EXISTS (SELECT 1 FROM em_security.session_context() c
                  WHERE c.principal_id = b.principal_id)
$function$;

-- Every future content table binds both its library and its fixed project. A
-- business scope='global' string does not enter this authorization calculation.
CREATE FUNCTION em_security.allows(p_library_id uuid, p_project_id uuid, p_permission text)
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, em_security
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM em_security.session_context() c
        JOIN em_security.library_state s ON s.library_id = c.library_id
        WHERE c.library_id = p_library_id
          AND s.project_id IS NOT DISTINCT FROM p_project_id
          AND c.authority_state = 'pg_active' AND c.acl_state = 'ready'
          AND CASE p_permission
              WHEN 'read' THEN c.permission IN ('read', 'write', 'admin')
              WHEN 'write' THEN c.permission IN ('write', 'admin')
              WHEN 'admin' THEN c.permission = 'admin'
              ELSE false END
    )
$function$;

-- Optional local projection barrier. NOT compatible with a READ ONLY transaction.
-- The coordinator must UPDATE this singleton before modifying any ACL/binding.
-- Does not replace the cross-database AuthorityLease or HTTP response boundary.
CREATE FUNCTION em_security.pin_session_context(p_policy_epoch bigint, p_route_epoch bigint)
RETURNS TABLE(principal_id uuid, library_id uuid, policy_epoch bigint, route_epoch bigint,
              permission text, authority_state text, acl_state text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, em_security
AS $function$
BEGIN
    PERFORM 1 FROM em_security.library_state s WHERE s.singleton FOR SHARE;
    IF NOT EXISTS (SELECT 1 FROM em_security.session_context() c
        WHERE c.policy_epoch = p_policy_epoch AND c.route_epoch = p_route_epoch
          AND c.authority_state = 'pg_active' AND c.acl_state = 'ready') THEN
        RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'library_authority_changed';
    END IF;
    RETURN QUERY SELECT c.principal_id, c.library_id, c.policy_epoch, c.route_epoch,
        c.permission, c.authority_state, c.acl_state FROM em_security.session_context() c;
END
$function$;

REVOKE ALL ON ALL TABLES IN SCHEMA em_security FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA em_security FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA em_security FROM PUBLIC;
INSERT INTO em_security.schema_migrations(version, migration_name) VALUES (1, '001_content');
-- Deliberately no library_state/binding/ACL seed: empty databases deny everyone.
COMMIT;

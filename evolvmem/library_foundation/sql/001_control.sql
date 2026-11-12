-- Candidate v1. Run only in an EMPTY, dedicated, already-created control database.
-- Trusted psql variables: database_name, owner_role. No runtime role executes DDL.
-- The invoking session must have already SET ROLE to the non-login database owner.
\set ON_ERROR_STOP on
SELECT current_database() = :'database_name' AND current_user = :'owner_role'
  AND EXISTS (SELECT 1 FROM pg_catalog.pg_roles r JOIN pg_catalog.pg_database d
    ON d.datdba = r.oid WHERE r.rolname = current_user AND d.datname = current_database()
    AND NOT r.rolsuper AND NOT r.rolbypassrls AND NOT r.rolcanlogin AND NOT r.rolcreaterole)
  AS preflight_ok \gset
\if :preflight_ok
\else
  \echo 'Control migration refused: database/owner role preflight failed.'
  \quit 3
\endif

BEGIN;
SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('evolvmem.control.schema', 0));
REVOKE ALL ON DATABASE :"database_name" FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
CREATE SCHEMA em_control AUTHORIZATION :"owner_role";
REVOKE ALL ON SCHEMA em_control FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_role" IN SCHEMA em_control REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner_role" IN SCHEMA em_control REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;

CREATE TABLE em_control.schema_migrations (
    version integer PRIMARY KEY CHECK (version > 0),
    migration_name text NOT NULL UNIQUE,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE em_control.principals (
    principal_id uuid PRIMARY KEY,
    display_name text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 200),
    active boolean NOT NULL DEFAULT false,
    credential_epoch bigint NOT NULL DEFAULT 1 CHECK (credential_epoch > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE em_control.devices (
    device_id uuid PRIMARY KEY,
    principal_id uuid NOT NULL REFERENCES em_control.principals(principal_id),
    device_fingerprint text NOT NULL UNIQUE CHECK (length(device_fingerprint) BETWEEN 1 AND 256),
    active boolean NOT NULL DEFAULT false,
    device_epoch bigint NOT NULL DEFAULT 1 CHECK (device_epoch > 0),
    UNIQUE (device_id, principal_id)
);
CREATE TABLE em_control.credentials (
    credential_id uuid PRIMARY KEY,
    principal_id uuid NOT NULL REFERENCES em_control.principals(principal_id),
    device_id uuid NOT NULL,
    credential_digest bytea NOT NULL UNIQUE CHECK (octet_length(credential_digest) = 32),
    active boolean NOT NULL DEFAULT false,
    credential_epoch bigint NOT NULL DEFAULT 1 CHECK (credential_epoch > 0),
    expires_at timestamptz,
    revoked_at timestamptz,
    FOREIGN KEY (device_id, principal_id) REFERENCES em_control.devices(device_id, principal_id),
    CHECK (NOT active OR revoked_at IS NULL)
);
CREATE TABLE em_control.libraries (
    library_id uuid PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('device', 'project', 'legacy')),
    owner_principal_id uuid NOT NULL REFERENCES em_control.principals(principal_id),
    device_id uuid REFERENCES em_control.devices(device_id),
    project_id uuid,
    backend text NOT NULL CHECK (backend IN ('sqlite', 'postgres')),
    connection_ref text NOT NULL CHECK (connection_ref ~ '^[A-Za-z][A-Za-z0-9_.-]{0,127}$'),
    schema_version integer NOT NULL CHECK (schema_version > 0),
    model_contract jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(model_contract) = 'object'),
    active boolean NOT NULL DEFAULT false,
    FOREIGN KEY (device_id, owner_principal_id) REFERENCES em_control.devices(device_id, principal_id),
    CHECK ((kind = 'device' AND device_id IS NOT NULL AND project_id IS NULL)
        OR (kind = 'project' AND project_id IS NOT NULL AND device_id IS NULL)
        OR (kind = 'legacy' AND device_id IS NULL AND project_id IS NULL))
);
CREATE TABLE em_control.library_routes (
    library_id uuid PRIMARY KEY REFERENCES em_control.libraries(library_id),
    route_epoch bigint NOT NULL DEFAULT 1 CHECK (route_epoch > 0),
    policy_epoch bigint NOT NULL DEFAULT 1 CHECK (policy_epoch > 0),
    authority_state text NOT NULL CHECK (authority_state IN
        ('sqlite_active', 'pg_staging', 'verify', 'freeze_and_reconcile', 'pg_active')),
    write_backend text NOT NULL CHECK (write_backend IN ('sqlite', 'postgres', 'none')),
    acl_state text NOT NULL DEFAULT 'updating' CHECK (acl_state IN ('ready', 'updating', 'failed')),
    CHECK ((authority_state = 'sqlite_active' AND write_backend = 'sqlite')
        OR (authority_state = 'pg_active' AND write_backend = 'postgres')
        OR (authority_state IN ('pg_staging', 'verify', 'freeze_and_reconcile') AND write_backend = 'none'))
);
CREATE TABLE em_control.library_acl (
    library_id uuid NOT NULL REFERENCES em_control.libraries(library_id),
    principal_id uuid NOT NULL REFERENCES em_control.principals(principal_id),
    permission text NOT NULL CHECK (permission IN ('read', 'write', 'admin')),
    active boolean NOT NULL DEFAULT false,
    policy_epoch bigint NOT NULL CHECK (policy_epoch > 0),
    granted_by uuid NOT NULL REFERENCES em_control.principals(principal_id),
    PRIMARY KEY (library_id, principal_id)
);
CREATE TABLE em_control.acl_operations (
    operation_id uuid PRIMARY KEY,
    request_id text NOT NULL UNIQUE CHECK (length(request_id) BETWEEN 1 AND 128),
    library_id uuid NOT NULL REFERENCES em_control.libraries(library_id),
    actor_principal_id uuid NOT NULL REFERENCES em_control.principals(principal_id),
    target_principal_id uuid NOT NULL REFERENCES em_control.principals(principal_id),
    requested_permission text CHECK (requested_permission IN ('read', 'write', 'admin')),
    expected_policy_epoch bigint NOT NULL CHECK (expected_policy_epoch > 0),
    target_policy_epoch bigint NOT NULL CHECK (target_policy_epoch > 0),
    operation_state text NOT NULL DEFAULT 'pending' CHECK (operation_state IN ('pending', 'applying', 'complete', 'failed')),
    payload_hash bytea NOT NULL CHECK (octet_length(payload_hash) = 32),
    error_code text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    CHECK (target_policy_epoch = expected_policy_epoch + 1),
    CHECK ((operation_state = 'complete') = (completed_at IS NOT NULL))
);
CREATE TABLE em_control.migration_runs (
    migration_id uuid PRIMARY KEY,
    library_id uuid NOT NULL REFERENCES em_control.libraries(library_id),
    source_backend text NOT NULL CHECK (source_backend IN ('sqlite', 'postgres')),
    target_backend text NOT NULL CHECK (target_backend IN ('sqlite', 'postgres')),
    expected_route_epoch bigint NOT NULL CHECK (expected_route_epoch > 0),
    run_state text NOT NULL CHECK (run_state IN ('planned', 'running', 'verified', 'failed', 'cutover', 'rolled_back')),
    manifest_ref text,
    source_revision text,
    target_revision text,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

ALTER TABLE em_control.schema_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.schema_migrations FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.schema_migrations TO :"owner_role" USING (true) WITH CHECK (true);
ALTER TABLE em_control.principals ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.principals FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.principals TO :"owner_role" USING (true) WITH CHECK (true);
ALTER TABLE em_control.devices ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.devices FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.devices TO :"owner_role" USING (true) WITH CHECK (true);
ALTER TABLE em_control.credentials ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.credentials FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.credentials TO :"owner_role" USING (true) WITH CHECK (true);
ALTER TABLE em_control.libraries ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.libraries FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.libraries TO :"owner_role" USING (true) WITH CHECK (true);
ALTER TABLE em_control.library_routes ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.library_routes FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.library_routes TO :"owner_role" USING (true) WITH CHECK (true);
ALTER TABLE em_control.library_acl ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.library_acl FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.library_acl TO :"owner_role" USING (true) WITH CHECK (true);
ALTER TABLE em_control.acl_operations ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.acl_operations FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.acl_operations TO :"owner_role" USING (true) WITH CHECK (true);
ALTER TABLE em_control.migration_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.migration_runs FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.migration_runs TO :"owner_role" USING (true) WITH CHECK (true);

REVOKE ALL ON ALL TABLES IN SCHEMA em_control FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA em_control FROM PUBLIC;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA em_control FROM PUBLIC;
INSERT INTO em_control.schema_migrations(version, migration_name) VALUES (1, '001_control');
COMMIT;

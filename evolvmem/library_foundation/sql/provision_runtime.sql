-- Explicit trusted provisioning candidate, NOT a numbered schema migration.
-- Variables: database_name, owner_role, runtime_role, principal_id, credential_id,
--            authenticated_device_id, credential_epoch. All identifiers/UUIDs are synthetic in tests.
-- Preexisting runtime role must be a dedicated login, with no role memberships.
-- No credential secret is accepted here; its connection is resolved outside SQL.
\set ON_ERROR_STOP on
SELECT current_database() = :'database_name' AND current_user = :'owner_role'
  AND EXISTS (SELECT 1 FROM pg_catalog.pg_roles o JOIN pg_catalog.pg_database d
      ON d.datdba = o.oid WHERE o.rolname = current_user AND d.datname = current_database()
      AND NOT o.rolsuper AND NOT o.rolbypassrls AND NOT o.rolcanlogin AND NOT o.rolcreaterole)
  AND EXISTS (SELECT 1 FROM pg_catalog.pg_roles r, pg_catalog.pg_roles o
      WHERE r.rolname = :'runtime_role' AND o.rolname = :'owner_role'
      AND r.rolcanlogin AND NOT (r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb)
      AND r.oid <> o.oid AND NOT pg_catalog.pg_has_role(r.oid, o.oid, 'MEMBER')
      AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member = r.oid)
      AND NOT pg_catalog.has_database_privilege(r.oid, current_database(), 'CREATE')
      AND NOT pg_catalog.has_schema_privilege(r.oid, 'em_security', 'CREATE')
      AND NOT pg_catalog.has_schema_privilege(r.oid, 'em_content', 'CREATE'))
  AS preflight_ok \gset
\if :preflight_ok
\else
  \echo 'Runtime provisioning refused: unsafe or mismatched roles/database.'
  \quit 3
\endif

BEGIN;
INSERT INTO em_security.principal_bindings
    (login_role, principal_id, credential_id, authenticated_device_id, credential_epoch, enabled)
VALUES (:'runtime_role', :'principal_id'::uuid, :'credential_id'::uuid,
        :'authenticated_device_id'::uuid, :'credential_epoch'::bigint, false);
GRANT CONNECT ON DATABASE :"database_name" TO :"runtime_role";
GRANT USAGE ON SCHEMA em_security, em_content TO :"runtime_role";
GRANT EXECUTE ON FUNCTION em_security.session_context() TO :"runtime_role";
GRANT EXECUTE ON FUNCTION em_security.session_credential() TO :"runtime_role";
GRANT EXECUTE ON FUNCTION em_security.allows(uuid, uuid, text) TO :"runtime_role";
GRANT EXECUTE ON FUNCTION em_security.pin_session_context(bigint, bigint) TO :"runtime_role";
-- ACL projection, library state and binding activation are a separate audited
-- owner/coordinator operation. CONNECT alone grants no content access.
COMMIT;

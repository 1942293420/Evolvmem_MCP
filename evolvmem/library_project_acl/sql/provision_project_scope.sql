-- Run after the existing provision_runtime.sql for each newly enrolled login.
-- Trusted variables: database_name, owner_role, runtime_role.
\set ON_ERROR_STOP on
SELECT current_database()=:'database_name' AND current_user=:'owner_role'
 AND em_security._runtime_role_safe(:'runtime_role'::name)
 AND EXISTS(SELECT 1 FROM em_security.principal_bindings WHERE login_role=:'runtime_role'::name)
 AND EXISTS(SELECT 1 FROM em_security.schema_migrations WHERE version=4)
 AS preflight_ok \gset
\if :preflight_ok
\else
 \quit 3
\endif
BEGIN;
GRANT EXECUTE ON FUNCTION em_security.resolve_project_scope(uuid,text,text) TO :"runtime_role";
COMMIT;

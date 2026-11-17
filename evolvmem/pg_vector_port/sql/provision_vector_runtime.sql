\set ON_ERROR_STOP on
SELECT current_database()=:'database_name' AND current_user=:'owner_role'
 AND em_security._runtime_role_safe(:'runtime_role'::name)
 AND EXISTS(SELECT 1 FROM em_security.principal_bindings WHERE login_role=:'runtime_role'::name)
 AND EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid WHERE d.datname=current_database()
  AND r.rolname=current_user AND NOT(r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreaterole)) AS safe_role \gset
\if :safe_role
\else
 \quit 3
\endif
BEGIN;
GRANT EXECUTE ON FUNCTION em_security.vector_context(uuid,uuid,text) TO :"runtime_role";
GRANT SELECT,INSERT,UPDATE ON em_content.vector_projections TO :"runtime_role";
COMMIT;

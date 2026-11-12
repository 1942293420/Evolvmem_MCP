-- Additive candidate after content security v3. Not executed in this artifact.
-- Trusted psql variables: database_name, owner_role.
\set ON_ERROR_STOP on
SELECT current_database()=:'database_name' AND current_user=:'owner_role'
 AND EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
  WHERE r.rolname=current_user AND d.datname=current_database()
   AND NOT(r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreaterole)) AS preflight_ok \gset
\if :preflight_ok
\else
 \quit 3
\endif
BEGIN;
SELECT pg_advisory_xact_lock(pg_catalog.hashtextextended('evolvmem.content.schema',0));
DO $version$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM em_security.schema_migrations WHERE version=3)
  OR EXISTS(SELECT 1 FROM em_security.schema_migrations WHERE version>=4) THEN
  RAISE EXCEPTION 'invalid_content_schema_version'; END IF;
END; $version$;
CREATE FUNCTION em_security.resolve_project_scope(p_library uuid,p_key text,p_permission text)
RETURNS TABLE(project_id uuid,project_key text,policy_epoch bigint,route_epoch bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security AS $fn$
 SELECT p.project_id,p.project_key,c.policy_epoch,c.route_epoch
 FROM em_security.session_context() c
 JOIN em_security.project_projection p ON p.projection_epoch=c.policy_epoch AND p.active
 WHERE c.library_id=p_library AND p.project_key=p_key COLLATE "C"
  AND em_security.allows(p_library,p.project_id,p_permission)
 UNION ALL
 SELECT NULL::uuid,NULL::text,c.policy_epoch,c.route_epoch
 FROM em_security.session_context() c JOIN em_security.library_state s USING(library_id)
 WHERE p_key IS NULL AND p_permission='read' AND c.library_id=p_library
  AND c.principal_id=s.owner_principal_id AND em_security.allows(p_library,NULL,'read')
$fn$;
REVOKE ALL ON FUNCTION em_security.resolve_project_scope(uuid,text,text) FROM PUBLIC;
-- Existing verified login roles receive only this narrow read function. Disabled
-- bindings still fail session_context; no direct metadata table access is added.
DO $grants$ DECLARE runtime_role name; BEGIN
 FOR runtime_role IN SELECT b.login_role FROM em_security.principal_bindings b ORDER BY b.login_role LOOP
  IF NOT em_security._runtime_role_safe(runtime_role) THEN
   RAISE EXCEPTION 'unsafe_runtime_role'; END IF;
  EXECUTE format('GRANT EXECUTE ON FUNCTION em_security.resolve_project_scope(uuid,text,text) TO %I',runtime_role);
 END LOOP;
END; $grants$;
INSERT INTO em_security.schema_migrations(version,migration_name) VALUES(4,'004_content_project_scope');
COMMIT;

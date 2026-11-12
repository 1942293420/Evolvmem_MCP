-- Trusted provisioning only; never creates a LOGIN or accepts a password/DSN.
-- Variables: database_name, owner_role, projector_role, library_id, connection_ref.
\set ON_ERROR_STOP on
SELECT current_database()=:'database_name' AND current_user=:'owner_role'
 AND EXISTS(SELECT 1 FROM pg_roles o JOIN pg_database d ON d.datdba=o.oid
  WHERE o.rolname=current_user AND d.datname=current_database()
   AND NOT(o.rolcanlogin OR o.rolsuper OR o.rolbypassrls OR o.rolcreaterole))
 AND EXISTS(SELECT 1 FROM em_security.schema_migrations WHERE version=2)
 AND EXISTS(SELECT 1 FROM em_security.library_state s WHERE s.singleton AND s.library_id=:'library_id'::uuid)
 AND EXISTS(SELECT 1 FROM pg_roles r WHERE r.rolname=:'projector_role' AND r.rolcanlogin
  AND NOT(r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb)
  AND NOT EXISTS(SELECT 1 FROM pg_auth_members m WHERE m.member=r.oid)
  AND NOT pg_has_role(r.oid,:'owner_role','MEMBER')
  AND NOT has_database_privilege(r.oid,current_database(),'CREATE')
  AND NOT EXISTS(SELECT 1 FROM pg_namespace n WHERE n.nspname IN('em_security','em_content') AND has_schema_privilege(r.oid,n.oid,'CREATE'))
  AND NOT EXISTS(SELECT 1 FROM em_security.principal_bindings b WHERE b.login_role=r.rolname)
  AND NOT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
   WHERE n.nspname IN('em_security','em_content') AND c.relkind IN('r','p','v','m','f')
    AND has_table_privilege(r.oid,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')))
 AS preflight_ok \gset
\if :preflight_ok
\else
 \quit 3
\endif
BEGIN;
INSERT INTO em_security.projector_services(login_role,library_id,connection_ref,enabled)
 VALUES(:'projector_role',:'library_id'::uuid,:'connection_ref',true);
GRANT CONNECT ON DATABASE :"database_name" TO :"projector_role";
GRANT USAGE ON SCHEMA em_security TO :"projector_role";
GRANT EXECUTE ON FUNCTION em_security.inspect_acl_projection(uuid) TO :"projector_role";
GRANT EXECUTE ON FUNCTION em_security.apply_acl_projection(text,bytea,text,bytea) TO :"projector_role";
COMMIT;

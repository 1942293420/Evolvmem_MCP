-- Explicit trusted provisioning; does not create roles/databases or take passwords.
-- Variables: database_name, owner_role, service_role, can_coordinate (true/false).
\set ON_ERROR_STOP on
SELECT current_database()=:'database_name' AND current_user=:'owner_role'
 AND EXISTS(SELECT 1 FROM pg_catalog.pg_roles o JOIN pg_catalog.pg_database d ON d.datdba=o.oid
   WHERE o.rolname=current_user AND d.datname=current_database()
     AND NOT(o.rolsuper OR o.rolbypassrls OR o.rolcanlogin OR o.rolcreaterole))
 AND EXISTS(SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname=:'service_role' AND r.rolcanlogin
   AND NOT(r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb)
   AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member=r.oid)
   AND NOT EXISTS(SELECT 1 FROM pg_catalog.pg_class c
     JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
     WHERE n.nspname='em_control' AND c.relkind IN('r','p','v','m','f')
      AND pg_catalog.has_table_privilege(r.oid,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'))
   AND NOT pg_catalog.pg_has_role(r.oid,:'owner_role','MEMBER')
   AND NOT pg_catalog.has_schema_privilege(r.oid,'em_control','CREATE')
   AND NOT pg_catalog.has_database_privilege(r.oid,current_database(),'CREATE'))
 AS preflight_ok \gset
\if :preflight_ok
\else
 \quit 3
\endif
BEGIN;
INSERT INTO em_control.service_roles(login_role,can_read,can_coordinate,enabled)
 VALUES(:'service_role',true,:'can_coordinate'::boolean,true);
GRANT CONNECT ON DATABASE :"database_name" TO :"service_role";
GRANT USAGE ON SCHEMA em_control TO :"service_role";
GRANT EXECUTE ON FUNCTION em_control.auth_binding(bytea) TO :"service_role";
GRANT EXECUTE ON FUNCTION em_control.resolve_library(uuid,uuid,uuid,bigint,bigint,bigint,uuid) TO :"service_role";
GRANT EXECUTE ON FUNCTION em_control.resolve_external_device(uuid,uuid,uuid,bigint,bigint,bigint,text) TO :"service_role";
GRANT EXECUTE ON FUNCTION em_control.list_libraries(uuid,uuid,uuid,bigint,bigint,bigint,integer,uuid) TO :"service_role";
GRANT EXECUTE ON FUNCTION em_control.pin_library(uuid,uuid,uuid,bigint,bigint,bigint,uuid,bigint,bigint,text) TO :"service_role";
\if :can_coordinate
GRANT EXECUTE ON FUNCTION em_control.enroll_external_device(uuid,uuid,text) TO :"service_role";
GRANT EXECUTE ON FUNCTION em_control.lookup_acl_operation(uuid,uuid,uuid,bigint,bigint,bigint,uuid,bytea) TO :"service_role";
GRANT EXECUTE ON FUNCTION em_control.begin_acl_change(uuid,uuid,uuid,bigint,bigint,bigint,uuid,uuid,text,bigint,uuid,text,bytea) TO :"service_role";
GRANT EXECUTE ON FUNCTION em_control.confirm_acl_projection(uuid,bigint,text,bytea) TO :"service_role";
GRANT EXECUTE ON FUNCTION em_control.finish_acl_change(uuid) TO :"service_role";
GRANT EXECUTE ON FUNCTION em_control.fail_acl_change(uuid,text) TO :"service_role";
\endif
COMMIT;

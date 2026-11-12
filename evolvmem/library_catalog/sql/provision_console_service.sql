-- Explicit trusted provisioning for v7+ console/session/agent/reputation RPCs.
-- Variables: database_name, owner_role, service_role, can_coordinate (true/false).
-- Grants ONLY narrow SECURITY DEFINER RPC EXECUTE privileges, NEVER direct table access.
\set ON_ERROR_STOP on
SELECT current_database() = :'database_name' AND current_user = :'owner_role'
  AND EXISTS (SELECT 1 FROM pg_catalog.pg_roles o JOIN pg_catalog.pg_database d ON d.datdba = o.oid
    WHERE o.rolname = current_user AND d.datname = current_database()
      AND NOT (o.rolsuper OR o.rolbypassrls OR o.rolcanlogin OR o.rolcreaterole))
  AND EXISTS (SELECT 1 FROM pg_catalog.pg_roles r WHERE r.rolname = :'service_role' AND r.rolcanlogin
    AND NOT (r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb)
    AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m WHERE m.member = r.oid)
    AND NOT EXISTS (SELECT 1 FROM pg_catalog.pg_class c
      JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
      WHERE n.nspname = 'em_control' AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
        AND pg_catalog.has_table_privilege(r.oid, c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER'))
    AND NOT pg_catalog.pg_has_role(r.oid, :'owner_role', 'MEMBER')
    AND NOT pg_catalog.has_schema_privilege(r.oid, 'em_control', 'CREATE')
    AND NOT pg_catalog.has_database_privilege(r.oid, current_database(), 'CREATE'))
  AS preflight_ok \gset
\if :preflight_ok
\else
  \quit 3
\endif

BEGIN;
GRANT USAGE ON SCHEMA em_control TO :"service_role";
GRANT EXECUTE ON FUNCTION
    em_control.console_status(text),
    em_control.console_login_allowed(text, text),
    em_control.console_user_enabled(text, text),
    em_control.console_get_users(text),
    em_control.console_get_pending(text),
    em_control.console_get_grants(text),
    em_control.resolve_device_project_access(uuid, uuid, uuid, bigint, bigint, bigint, uuid, text, text),
    em_control.web_session_get(text, text)
  TO :"service_role";

\if :can_coordinate
GRANT EXECUTE ON FUNCTION
    em_control.console_bootstrap(text, uuid, text, text, uuid, uuid, text, jsonb),
    em_control.console_observe_login(text, text, text),
    em_control.console_upsert_user(text, text, text, uuid, text, text, boolean, bigint, boolean),
    em_control.console_configure_library(text, text, text, uuid, uuid, text, jsonb),
    em_control.console_set_grant(text, text, text, text, boolean, boolean, bigint),
    em_control.console_record_audit(text, text, text, jsonb, jsonb, text),
    em_control.register_trusted_device(text, uuid, text, uuid, bytea, timestamptz, boolean),
    em_control.register_pg_library_with_projects(text, uuid, text, jsonb),
    em_control.grant_device_project(uuid, uuid, uuid, text, text, boolean),
    em_control.web_session_put(text, boolean, text, text, text, text, text, timestamptz),
    em_control.web_session_pop(text, text),
    em_control.web_session_prune(timestamptz),
    em_control.web_session_revoke_open_id(text)
  TO :"service_role";
\endif

SELECT EXISTS (SELECT 1 FROM em_control.schema_migrations WHERE version >= 8) AS has_v8 \gset
\if :has_v8
GRANT EXECUTE ON FUNCTION
    em_control.get_agent(text),
    em_control.list_agents_for_device(uuid),
    em_control.get_model_reputation(text, text)
  TO :"service_role";
\if :can_coordinate
GRANT EXECUTE ON FUNCTION
    em_control.upsert_agent_seen(text, uuid, text, text),
    em_control.upsert_model_reputation_policy(text, text, text, integer),
    em_control.record_model_reputation_event(text, text, text, integer, integer, integer, integer)
  TO :"service_role";
\endif
\endif
COMMIT;

-- Successor to applied 005: exact-key materialization before family authorization.
-- Trusted psql variables: database_name, owner_role. No table, row, ACL or epoch changes.
\set ON_ERROR_STOP on
SELECT current_database()=:'database_name' AND current_user=:'owner_role'
 AND EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
 WHERE r.rolname=current_user AND d.datname=current_database()
 AND NOT(r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb)) AS preflight_ok \gset
\if :preflight_ok
\else
 \quit 3
\endif
BEGIN;
SELECT pg_advisory_xact_lock(pg_catalog.hashtextextended('evolvmem.content.schema',0));
DO $guard$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_proc p
 WHERE p.oid=to_regprocedure('em_content.history_identity_family(uuid,uuid,jsonb)')
 AND p.proowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)
 AND p.prosecdef AND p.provolatile='s' AND md5(p.prosrc)='bd5d603833f51adfc05606eb2f2b28d7')
 THEN RAISE EXCEPTION 'history_family_successor_preimage_changed'; END IF;
END; $guard$;
CREATE OR REPLACE FUNCTION em_content.history_identity_family(p_library uuid,p_project uuid,p_keys jsonb)
RETURNS TABLE(identity_key text,project text,scope text,id bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE n integer;
BEGIN
 PERFORM em_security.require_history_scope(p_library,p_project);
 IF p_keys IS NULL OR jsonb_typeof(p_keys)<>'array' OR jsonb_array_length(p_keys)>240
 OR EXISTS(SELECT 1 FROM jsonb_array_elements(p_keys) k WHERE jsonb_typeof(k)<>'array' OR jsonb_array_length(k)<>3
  OR jsonb_typeof(k->0)<>'string' OR jsonb_typeof(k->1)<>'string' OR jsonb_typeof(k->2)<>'string')
 THEN RAISE EXCEPTION 'invalid_history_family_keys'; END IF;
 -- Caller may name only families seeded by this scope. Never disclose foreign existence.
 IF EXISTS(SELECT 1 FROM jsonb_array_elements(p_keys) k WHERE NOT EXISTS(
 WITH seed_family AS MATERIALIZED(SELECT i.id FROM em_content.context_items i
 WHERE i.library_id=p_library AND i.identity_key=k->>0 AND i.project=k->>1 AND i.scope=k->>2)
 SELECT 1 FROM seed_family i WHERE em_content.history_member(p_library,p_project,i.id)))
 THEN RAISE EXCEPTION 'history_family_scope_unavailable'; END IF;
 SELECT count(*) INTO n FROM(SELECT i.id FROM em_content.context_items i WHERE i.library_id=p_library
 AND EXISTS(SELECT 1 FROM jsonb_array_elements(p_keys) k WHERE i.identity_key=k->>0 AND i.project=k->>1 AND i.scope=k->>2)
 LIMIT 513) counted;
 IF n>512 THEN RAISE EXCEPTION 'history_dependency_limit'; END IF;
 IF EXISTS(WITH family_candidates AS MATERIALIZED(
 SELECT i.id FROM em_content.context_items i WHERE i.library_id=p_library
 AND EXISTS(SELECT 1 FROM jsonb_array_elements(p_keys) k WHERE i.identity_key=k->>0 AND i.project=k->>1 AND i.scope=k->>2)
 LIMIT 513)
 SELECT 1 FROM family_candidates i WHERE NOT em_content.history_member(p_library,p_project,i.id))
 THEN RAISE EXCEPTION 'history_family_scope_unavailable'; END IF;
 RETURN QUERY SELECT i.identity_key,i.project,i.scope,i.id FROM em_content.context_items i WHERE i.library_id=p_library
 AND EXISTS(SELECT 1 FROM jsonb_array_elements(p_keys) k WHERE i.identity_key=k->>0 AND i.project=k->>1 AND i.scope=k->>2)
 ORDER BY i.id;
END; $fn$;
-- CREATE OR REPLACE preserves existing owner and exact runtime EXECUTE grants.
REVOKE ALL ON FUNCTION em_content.history_identity_family(uuid,uuid,jsonb) FROM PUBLIC;
COMMIT;

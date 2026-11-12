-- Narrow successor to applied 007. Function-local planning configuration only.
-- Trusted psql variables: database_name, owner_role. No body, ACL or data edits.
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
SELECT pg_advisory_xact_lock(hashtextextended('evolvmem.content.schema',0));
DO $guard$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_proc p
 WHERE p.oid=to_regprocedure('em_content.history_knowledge_inputs(uuid,uuid,bigint[],text)')
 AND p.proowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)
 AND p.prosecdef AND p.provolatile='s'
 AND 'search_path=pg_catalog, em_security, em_content'=ANY(p.proconfig))
 THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='history_knowledge_jit_precondition'; END IF;
END $guard$;
ALTER FUNCTION em_content.history_knowledge_inputs(uuid,uuid,bigint[],text) SET jit TO 'off';
DO $verify$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_proc p
 WHERE p.oid=to_regprocedure('em_content.history_knowledge_inputs(uuid,uuid,bigint[],text)')
 AND p.proowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)
 AND p.prosecdef AND p.provolatile='s'
 AND 'search_path=pg_catalog, em_security, em_content'=ANY(p.proconfig)
 AND 'jit=off'=ANY(p.proconfig))
 THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='history_knowledge_jit_verification_failed'; END IF;
END $verify$;
COMMIT;

-- Additive successor to installed 009. A completed catch-up declares its actual
-- namespace domains: owner/Kane have nine; public has eight and no capture.
-- Fixed whitelist only; no original-row scans, placeholder rows or data edits.
-- Trusted psql variables: database_name, owner_role.
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
DO $guard$ DECLARE f regprocedure; BEGIN
 FOREACH f IN ARRAY ARRAY[
 to_regprocedure('em_security.history_vector_generation()'),
 to_regprocedure('em_content.history_vector_lock_source(uuid,text)')] LOOP
 IF f IS NULL OR NOT EXISTS(SELECT 1 FROM pg_proc p WHERE p.oid=f
 AND p.proowner=(SELECT oid FROM pg_roles WHERE rolname=current_user) AND p.prosecdef
 AND 'search_path=pg_catalog, em_security, em_content'=ANY(p.proconfig))
 THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='history_vector_declared_generation_precondition'; END IF;
 END LOOP;
END $guard$;

CREATE OR REPLACE FUNCTION em_security.history_vector_generation() RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE names text[]:=ARRAY['history_batch_progress','history_source_batch_progress','history_organization_progress','history_capture_progress',
 'history_governance_progress','history_knowledge_progress','history_capability_progress','history_continuity_progress','history_operations_progress'];
 keys text[]:=ARRAY['core','sources','organization','capture','governance','knowledge','capability','continuity','operations'];
 result jsonb:='{}'; declared jsonb; v jsonb; n bigint; k integer; lib uuid;
BEGIN
 -- The caller cannot supply a library or invent domain presence. Resolve the
 -- actual role binding and its already-completed epoch declaration first.
 SELECT c.library_id INTO lib FROM em_security.session_context() c;
 IF lib IS NULL THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_vector_generation_unavailable'; END IF;
 SELECT l.generation INTO declared FROM em_content.history_catchup_latest l
 JOIN em_content.history_catchup_epochs e ON e.library_id=l.library_id AND e.epoch_id=l.epoch_id
 WHERE l.library_id=lib AND e.state='complete' AND e.completion_hash=l.completion_hash AND e.generation=l.generation;
 IF declared IS NULL OR jsonb_typeof(declared)<>'object' OR declared='{}'::jsonb
 OR EXISTS(SELECT 1 FROM jsonb_object_keys(CASE WHEN jsonb_typeof(declared)='object' THEN declared ELSE '{}'::jsonb END)d
 WHERE NOT(d=ANY(keys)))
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_vector_generation_unavailable'; END IF;
 FOR k IN 1..9 LOOP
 IF declared?keys[k] THEN
 IF to_regclass(format('em_content.%I',names[k])) IS NULL
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_vector_generation_unavailable'; END IF;
 EXECUTE format('SELECT count(*),jsonb_agg(jsonb_build_object(''run_id'',run_id,''generation'',generation))->0 FROM em_content.%I',names[k]) INTO n,v;
 IF n<>1 OR (v->>'generation')::bigint<0 OR v IS DISTINCT FROM declared->keys[k]
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_vector_generation_unavailable'; END IF;
 result:=result||jsonb_build_object(keys[k],v);
 END IF;
 END LOOP;
 IF result IS DISTINCT FROM declared
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_vector_generation_unavailable'; END IF;
 RETURN result;
END $fn$;

CREATE OR REPLACE FUNCTION em_content.history_vector_lock_source(p_library uuid,p_contract text) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE names text[]:=ARRAY['history_batch_progress','history_source_batch_progress','history_organization_progress','history_capture_progress',
 'history_governance_progress','history_knowledge_progress','history_capability_progress','history_continuity_progress','history_operations_progress'];
 keys text[]:=ARRAY['core','sources','organization','capture','governance','knowledge','capability','continuity','operations'];
 declared jsonb; k integer; n bigint; v jsonb;
BEGIN
 -- Validation checks the exact expected singleton before any progress lock;
 -- the locked metadata rows remain held through atomic publication/ack commit.
 declared:=em_security.history_vector_generation();
 IF NOT EXISTS(SELECT 1 FROM em_security.session_context() c WHERE c.library_id=p_library)
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_vector_generation_unavailable'; END IF;
 PERFORM 1 FROM em_security.library_state WHERE library_id=p_library FOR SHARE;
 PERFORM 1 FROM em_security.history_readiness WHERE library_id=p_library FOR SHARE;
 PERFORM 1 FROM em_security.history_membership_state WHERE library_id=p_library FOR SHARE;
 PERFORM 1 FROM em_content.history_catchup_latest WHERE library_id=p_library FOR SHARE;
 PERFORM 1 FROM em_content.history_catchup_epochs WHERE library_id=p_library
 AND epoch_id=(SELECT epoch_id FROM em_content.history_catchup_latest WHERE library_id=p_library) FOR SHARE;
 PERFORM 1 FROM em_content.history_vector_models WHERE library_id=p_library AND model_contract=p_contract FOR SHARE;
 FOR k IN 1..9 LOOP
 IF declared?keys[k] THEN
 -- Constant whitelist identifiers; actual declared UUID/generation is checked
 -- again after locking so no missing/different expected row is accepted.
 EXECUTE format('SELECT count(*),jsonb_agg(jsonb_build_object(''run_id'',run_id,''generation'',generation))->0
 FROM (SELECT run_id,generation FROM em_content.%I FOR SHARE) p',names[k]) INTO n,v;
 IF n<>1 OR v IS DISTINCT FROM declared->keys[k]
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_vector_generation_unavailable'; END IF;
 END IF;
 END LOOP;
 -- Source()/index_context() still perform their original latest/epoch exact
 -- generation comparison after this helper, unchanged by this migration.
END $fn$;
-- CREATE OR REPLACE retains existing owner and exact grants. Keep both helpers
-- private; no runtime table privilege or raw-history schema is changed.
REVOKE ALL ON FUNCTION em_security.history_vector_generation(),em_content.history_vector_lock_source(uuid,text) FROM PUBLIC;
COMMIT;

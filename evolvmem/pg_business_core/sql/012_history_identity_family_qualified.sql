-- Additive successor: one opaque denial for a caller-known blocked family.
-- Trusted psql variables: database_name, owner_role. Original 006 is unchanged.
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
 AND p.prosecdef AND p.provolatile='s' AND md5(p.prosrc)='05de17f8d6736e2ee2d9509acfb20172')
 OR to_regprocedure('em_content.history_identity_family_qualified(uuid,uuid,jsonb)') IS NOT NULL
 THEN RAISE EXCEPTION 'history_family_qualified_preimage_changed'; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_proc p
 WHERE p.oid=to_regprocedure('em_security.provision_history_reader(name)')
 AND p.proowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)
 AND p.prolang=(SELECT oid FROM pg_language WHERE lanname='plpgsql')
 AND p.prorettype='pg_catalog.void'::regtype AND p.prokind='f'
 AND NOT p.prosecdef AND p.provolatile='v' AND p.proparallel='u' AND NOT p.proisstrict
 AND p.proconfig=ARRAY['search_path=pg_catalog, em_security']::text[]
 AND md5(p.prosrc)='78ff1f476bdadc07b49e10f2e672cd46'
 AND NOT EXISTS(SELECT 1 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a WHERE a.grantee=0))
 THEN RAISE EXCEPTION 'history_reader_provisioning_preimage_changed'; END IF;
END; $guard$;
CREATE FUNCTION em_content.history_identity_family_qualified(p_library uuid,p_project uuid,p_keys jsonb)
RETURNS TABLE(identity_key text,project text,scope text,id bigint,qualification text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE n integer;
BEGIN
 PERFORM em_security.require_history_scope(p_library,p_project);
 IF p_keys IS NULL OR jsonb_typeof(p_keys)<>'array' OR jsonb_array_length(p_keys)>240
 OR EXISTS(SELECT 1 FROM jsonb_array_elements(p_keys) k WHERE jsonb_typeof(k)<>'array' OR jsonb_array_length(k)<>3
  OR jsonb_typeof(k->0)<>'string' OR jsonb_typeof(k->1)<>'string' OR jsonb_typeof(k->2)<>'string')
 THEN RAISE EXCEPTION 'invalid_history_family_keys'; END IF;
 -- Knowing an arbitrary key is insufficient: each family needs a readable seed.
 IF EXISTS(SELECT 1 FROM jsonb_array_elements(p_keys) k WHERE NOT EXISTS(
 WITH seed_family AS MATERIALIZED(SELECT i.id FROM em_content.context_items i
 WHERE i.library_id=p_library AND i.identity_key=k->>0 AND i.project=k->>1 AND i.scope=k->>2)
 SELECT 1 FROM seed_family i WHERE em_content.history_member(p_library,p_project,i.id)))
 THEN RAISE EXCEPTION 'history_family_scope_unavailable'; END IF;
 -- The original aggregate cap includes blocked rows, before any IDs are returned.
 SELECT count(*) INTO n FROM(SELECT i.id FROM em_content.context_items i WHERE i.library_id=p_library
 AND EXISTS(SELECT 1 FROM jsonb_array_elements(p_keys) k WHERE i.identity_key=k->>0 AND i.project=k->>1 AND i.scope=k->>2)
 LIMIT 513) counted;
 IF n>512 THEN RAISE EXCEPTION 'history_dependency_limit'; END IF;
 -- Relevance is the existing SQL008/canonical temporal candidate rule.
 -- An exited, undated non-decision can never win at any as_of instant.
 RETURN QUERY WITH wanted AS MATERIALIZED(
 SELECT DISTINCT k->>0 AS identity_key,k->>1 AS project,k->>2 AS scope FROM jsonb_array_elements(p_keys) k),
 family_candidates AS MATERIALIZED(
 SELECT i.identity_key,i.project,i.scope,i.id FROM em_content.context_items i WHERE i.library_id=p_library
 AND EXISTS(SELECT 1 FROM wanted w WHERE (i.identity_key,i.project,i.scope)=(w.identity_key,w.project,w.scope))
 AND (i.status='active' OR (i.status='superseded' AND i.content_type='decision'
      AND (i.effective_from IS NOT NULL OR i.occurred_at IS NOT NULL)))
 LIMIT 513), blocked AS MATERIALIZED(
 SELECT w.identity_key,w.project,w.scope FROM wanted w
 WHERE NOT EXISTS(SELECT 1 FROM family_candidates i
      WHERE (i.identity_key,i.project,i.scope)=(w.identity_key,w.project,w.scope))
 OR EXISTS(SELECT 1 FROM family_candidates i
      WHERE (i.identity_key,i.project,i.scope)=(w.identity_key,w.project,w.scope)
      AND NOT em_content.history_member(p_library,p_project,i.id)))
 SELECT i.identity_key,i.project,i.scope,i.id,'admitted'::text FROM family_candidates i
 WHERE NOT EXISTS(SELECT 1 FROM blocked b WHERE (b.identity_key,b.project,b.scope)=(i.identity_key,i.project,i.scope))
 UNION ALL SELECT b.identity_key,b.project,b.scope,NULL::bigint,'denied'::text FROM blocked b
 ORDER BY 1,2,3,4;
END; $fn$;
REVOKE ALL ON FUNCTION em_content.history_identity_family_qualified(uuid,uuid,jsonb) FROM PUBLIC;
-- Copy only the old function's explicit EXECUTE recipients. Never broaden a
-- table/role/schema grant or make the helper callable through PUBLIC.
DO $grants$ DECLARE recipient record;
BEGIN
 -- Clear any non-PUBLIC function defaults before copying the closed old ACL.
 FOR recipient IN SELECT DISTINCT r.rolname FROM pg_proc p
  CROSS JOIN LATERAL aclexplode(p.proacl) a JOIN pg_roles r ON r.oid=a.grantee
  WHERE p.oid=to_regprocedure('em_content.history_identity_family_qualified(uuid,uuid,jsonb)')
  AND a.grantee<>p.proowner
 LOOP
  EXECUTE format('REVOKE ALL ON FUNCTION em_content.history_identity_family_qualified(uuid,uuid,jsonb) FROM %I',recipient.rolname);
 END LOOP;
 IF EXISTS(SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) a
  WHERE p.oid=to_regprocedure('em_content.history_identity_family(uuid,uuid,jsonb)')
  AND (a.grantee=0 OR a.privilege_type<>'EXECUTE' OR a.is_grantable))
 THEN RAISE EXCEPTION 'history_family_qualified_acl_changed'; END IF;
 FOR recipient IN SELECT r.rolname FROM pg_proc p
  CROSS JOIN LATERAL aclexplode(p.proacl) a JOIN pg_roles r ON r.oid=a.grantee
  WHERE p.oid=to_regprocedure('em_content.history_identity_family(uuid,uuid,jsonb)')
  AND a.grantee<>p.proowner AND a.privilege_type='EXECUTE'
 LOOP
  EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_identity_family_qualified(uuid,uuid,jsonb) TO %I',recipient.rolname);
 END LOOP;
END; $grants$;
-- CREATE OR REPLACE preserves the original provisioner owner and EXECUTE ACL.
-- Its invoker checks and table revocations are unchanged; only the new grant is added.
CREATE OR REPLACE FUNCTION em_security.provision_history_reader(p_role name) RETURNS void
LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,em_security AS $fn$
DECLARE t text;
BEGIN
 IF NOT em_security._runtime_role_safe(p_role) OR NOT EXISTS(SELECT 1 FROM em_security.principal_bindings WHERE login_role=p_role)
 THEN RAISE EXCEPTION 'unsafe_history_reader'; END IF;
 -- Revoke old business-table grants as well: native reader privileges are not
 -- the history contract, even though restrictive quarantine would still deny.
 FOREACH t IN ARRAY ARRAY['context_items','context_layers','context_sources','context_evidence','context_project_registry',
 'context_project_resolutions','learning_memories','unit_derivations','organization_units','organization_tasks',
 'organization_context_basis','organization_conversations','knowledge_cleaning_reviews','context_project_rollups',
 'session_archives','session_archive_storage','archive_source_exclusions','session_archive_holds','conversation_history',
 'memories','legacy_memory_migrations','knowledge_metadata','knowledge_source_bindings','knowledge_fact_actions',
 'knowledge_fact_groups','knowledge_objects','knowledge_decisions','knowledge_fact_sources','knowledge_fact_versions',
 'knowledge_fact_choices','knowledge_fact_conditions','knowledge_fact_issues','knowledge_fact_relations',
 'knowledge_fact_separations','knowledge_equivalences','history_read_memberships','history_source_event_proofs'] LOOP
  EXECUTE format('REVOKE ALL ON TABLE em_content.%I FROM %I',t,p_role);
 END LOOP;

 EXECUTE format('GRANT EXECUTE ON FUNCTION em_security.history_recall_context(uuid,uuid) TO %I',p_role);
 EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_item_inputs(uuid,uuid,bigint[]) TO %I',p_role);
 EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_lexical_candidates(uuid,uuid,text,text[],text,float8,boolean,integer,text,jsonb) TO %I',p_role);
 EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_identity_family(uuid,uuid,jsonb) TO %I',p_role);
 EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_identity_family_qualified(uuid,uuid,jsonb) TO %I',p_role);
 EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_layer_read(uuid,uuid,bigint,text) TO %I',p_role);
 EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_knowledge_inputs(uuid,uuid,bigint[],text) TO %I',p_role);
 EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_legacy_projection(uuid,uuid,bigint[]) TO %I',p_role);
END; $fn$;
COMMIT;

-- Additive successor: bounded source pagination for one authorized read snapshot.
-- Trusted psql variables: database_name, owner_role.  Original 005/012 are
-- unchanged.  This migration only adds functions, their explicit ACLs, and the
-- two grants in the existing guarded reader provisioner.
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
 -- Exact original input function preimage: all non-source sections and the
 -- first 64 source rows must still be the accepted SQL005 implementation.
 IF NOT EXISTS(SELECT 1 FROM pg_proc p
  WHERE p.oid=to_regprocedure('em_content.history_item_inputs(uuid,uuid,bigint[])')
  AND p.proowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)
  AND p.prosecdef AND p.provolatile='s'
  AND p.proconfig=ARRAY['search_path=pg_catalog, em_security, em_content']::text[]
  AND md5(p.prosrc)='da05bf9463893fe65b019b13cb7a0636')
 THEN RAISE EXCEPTION 'history_item_inputs_preimage_changed'; END IF;
 -- Exact SQL012 provisioner preimage, including its identity-family grant.
 IF NOT EXISTS(SELECT 1 FROM pg_proc p
  WHERE p.oid=to_regprocedure('em_security.provision_history_reader(name)')
  AND p.proowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)
  AND p.prolang=(SELECT oid FROM pg_language WHERE lanname='plpgsql')
  AND p.prorettype='pg_catalog.void'::regtype AND p.prokind='f'
  AND NOT p.prosecdef AND p.provolatile='v' AND p.proparallel='u' AND NOT p.proisstrict
  AND p.proconfig=ARRAY['search_path=pg_catalog, em_security']::text[]
  AND md5(p.prosrc)='a847bbc4e78827614f7f6ee797c46175'
  AND NOT EXISTS(SELECT 1 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a WHERE a.grantee=0))
 THEN RAISE EXCEPTION 'history_reader_provisioning_preimage_changed'; END IF;
 IF to_regprocedure('em_content.history_item_inputs_first_page(uuid,uuid,bigint[])') IS NOT NULL
  OR to_regprocedure('em_content.history_item_sources_page(uuid,uuid,bigint,bigint)') IS NOT NULL
  OR to_regprocedure('em_content.history_source_payload(uuid,bigint,bigint)') IS NOT NULL
 THEN RAISE EXCEPTION 'history_source_pagination_already_installed'; END IF;
END; $guard$;

-- One source object, exactly the shape assembled by SQL005.  The helper is
-- internal-only: PUBLIC has no EXECUTE and the provisioner does not grant it.
CREATE FUNCTION em_content.history_source_payload(p_library uuid,p_item bigint,p_source bigint)
RETURNS jsonb LANGUAGE sql STABLE SECURITY DEFINER
SET search_path=pg_catalog,em_security,em_content AS $fn$
 SELECT to_jsonb(x) FROM (
  SELECT s.*,
   (SELECT jsonb_build_object('id',a.id,'state',a.state,'payload_sha256',a.payload_sha256)
    FROM em_content.session_archives a
    WHERE a.library_id=s.library_id AND a.id=s.archive_id) AS archive,
   (SELECT to_jsonb(e) FROM em_content.archive_source_exclusions e
    WHERE e.library_id=s.library_id AND e.archive_id=s.archive_id) AS exclusion,
   COALESCE((SELECT jsonb_agg(to_jsonb(p) ORDER BY p.evidence_id) FROM (
    SELECT p.* FROM em_content.history_source_event_proofs p
    JOIN em_content.context_items i ON i.library_id=s.library_id AND i.id=s.item_id
    JOIN em_content.context_evidence e ON e.library_id=p.library_id AND e.id=p.evidence_id
     AND e.source_id=p.source_id AND e.item_id=p.item_id
    JOIN em_content.context_layers l ON l.library_id=p.library_id AND l.item_id=p.item_id AND l.layer='l1'
    JOIN em_content.session_archives a ON a.library_id=p.library_id AND a.id=p.archive_id
    WHERE p.library_id=s.library_id AND p.source_id=s.id AND p.item_id=i.id AND p.state='verified'
     AND p.item_revision=i.revision
     AND p.item_content_hash=l.content_hash
     AND p.evidence_revision=e.revision AND p.experience_version=e.experience_version
     AND p.evidence_note_hash=encode(sha256(convert_to(e.note,'UTF8')),'hex')
     AND p.task_id=e.task_id AND p.source_ref=s.source_ref AND p.extraction_version=s.extraction_version
     AND p.archive_id=s.archive_id AND p.archive_payload_sha256=a.payload_sha256 AND a.state='available'
     AND p.event_ref=s.source_ref
     AND ((s.source_kind='tool_result' AND p.role='tool') OR(s.source_kind='user_confirmation' AND p.role='user'))
    ORDER BY p.evidence_id LIMIT 65)p),'[]'::jsonb) AS event_proofs
  FROM em_content.context_sources s
  WHERE s.library_id=p_library AND s.item_id=p_item AND s.id=p_source
 ) x
$fn$;
REVOKE ALL ON FUNCTION em_content.history_source_payload(uuid,bigint,bigint) FROM PUBLIC;

-- Metadata/first page: every original payload field is copied, sources are
-- capped at 64, and the two cursor columns are derived from the same snapshot.
CREATE FUNCTION em_content.history_item_inputs_first_page(p_library uuid,p_project uuid,p_ids bigint[])
RETURNS TABLE(id bigint,payload jsonb,source_after bigint,source_more boolean)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE total_bytes bigint:=0; rec record;
BEGIN
 PERFORM em_security.require_history_scope(p_library,p_project);
 IF p_ids IS NULL OR cardinality(p_ids)>512 OR EXISTS(SELECT 1 FROM unnest(p_ids) n WHERE n IS NULL OR n<1)
 THEN RAISE EXCEPTION 'invalid_history_ids'; END IF;
 FOR rec IN SELECT i.id,
  jsonb_build_object(
   'item',to_jsonb(i),
   'item_l1_hash',(SELECT l.content_hash FROM em_content.context_layers l WHERE l.library_id=i.library_id AND l.item_id=i.id AND l.layer='l1'),
   'memberships',COALESCE((SELECT jsonb_agg(to_jsonb(m)) FROM em_content.history_read_memberships m WHERE m.library_id=i.library_id AND m.item_id=i.id AND m.project_id=p_project),'[]'::jsonb),
   'layers',COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.layer) FROM (SELECT l.*,octet_length(l.content) AS content_bytes FROM em_content.context_layers l WHERE l.library_id=i.library_id AND l.item_id=i.id ORDER BY l.layer LIMIT 4)x),'[]'::jsonb),
   'learning',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT l.* FROM em_content.learning_memories l WHERE l.library_id=i.library_id AND l.item_id=i.id LIMIT 2)x),'[]'::jsonb),
   'resolution',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT r.* FROM em_content.context_project_resolutions r WHERE r.library_id=i.library_id AND r.item_id=i.id LIMIT 2)x),'[]'::jsonb),
   'registry',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT r.* FROM em_content.context_project_registry r JOIN em_content.history_read_memberships m ON m.library_id=r.library_id AND m.project_key=r.project WHERE r.library_id=i.library_id AND m.item_id=i.id AND m.project_id=p_project LIMIT 2)x),'[]'::jsonb),
   'sources',COALESCE((SELECT jsonb_agg(em_content.history_source_payload(i.library_id,i.id,s.id) ORDER BY s.id)
     FROM (SELECT s.id FROM em_content.context_sources s
      WHERE s.library_id=i.library_id AND s.item_id=i.id ORDER BY s.id LIMIT 64) s),'[]'::jsonb),
   'derivations',COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.unit_task_id,x.unit_digest,x.ordinal) FROM (
     SELECT d.item_id,d.unit_task_id,d.unit_digest,d.kind,d.project AS derivation_project,
     t.id AS task_exists,i.id AS item_exists,t.source_key,t.source_revision,t.status,u.ordinal,u.decision,u.disposition,
     u.project,u.projects,u.knowledge_scope,u.revision,i.project AS item_project,i.legacy_projects_text AS item_projects,i.scope AS item_scope
     FROM em_content.unit_derivations d LEFT JOIN em_content.organization_tasks t ON t.library_id=d.library_id AND t.id=d.unit_task_id
     LEFT JOIN em_content.organization_units u ON u.library_id=d.library_id AND u.task_id=d.unit_task_id AND u.digest=d.unit_digest
     WHERE d.library_id=i.library_id AND d.item_id=i.id ORDER BY d.unit_task_id,d.unit_digest,u.ordinal LIMIT 65)x),'[]'::jsonb),
   'basis',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (
     SELECT to_jsonb(b) AS basis,u.ordinal,u.decision,p.ordinal AS prior_ordinal,p.project AS prior_project,
     p.decision AS prior_decision,p.disposition AS prior_disposition,p.revision AS prior_revision,pt.status AS prior_status,
     pt.source_revision,sa.state AS archive_state,sa.payload_sha256,cr.revision AS cleaning_revision,
     (b.state='active' AND u.decision='auto' AND (p.task_id IS NULL OR pt.id IS NULL OR pt.status='superseded'
       OR sa.id IS NULL OR sa.state<>'available' OR p.project<>b.project OR p.decision NOT IN('auto','manual')
       OR p.disposition NOT IN('keep','history_only') OR p.revision<>b.prior_revision OR pt.source_revision<>b.prior_source_revision
       OR sa.payload_sha256<>b.prior_payload_sha256 OR COALESCE(cr.revision,0)<>b.prior_cleaning_revision)) AS stale
     FROM em_content.organization_context_basis b JOIN em_content.unit_derivations d ON d.library_id=b.library_id AND d.unit_task_id=b.unit_task_id AND d.unit_digest=b.unit_digest
     JOIN em_content.organization_units u ON u.library_id=b.library_id AND u.task_id=b.unit_task_id AND u.digest=b.unit_digest
     LEFT JOIN em_content.organization_units p ON p.library_id=b.library_id AND p.task_id=b.prior_task_id AND p.digest=b.prior_digest
     LEFT JOIN em_content.organization_tasks pt ON pt.library_id=b.library_id AND pt.id=b.prior_task_id
     LEFT JOIN em_content.session_archives sa ON sa.library_id=b.library_id AND sa.id=b.prior_archive_id
     LEFT JOIN em_content.knowledge_cleaning_reviews cr ON cr.library_id=b.library_id AND cr.source_key=b.prior_source_key
     WHERE b.library_id=i.library_id AND d.item_id=i.id ORDER BY b.unit_task_id,b.unit_digest,u.ordinal,p.ordinal LIMIT 65)x),'[]'::jsonb),
   'conversations',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT c.source_key,c.members FROM em_content.organization_conversations c WHERE c.library_id=i.library_id AND EXISTS(SELECT 1 FROM em_content.unit_derivations d JOIN em_content.organization_tasks t ON t.library_id=d.library_id AND t.id=d.unit_task_id WHERE d.library_id=i.library_id AND d.item_id=i.id AND t.source_key=c.source_key) ORDER BY c.source_key LIMIT 65)x),'[]'::jsonb),
   'evidence',COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.id) FROM (SELECT e.* FROM em_content.context_evidence e WHERE e.library_id=i.library_id AND e.item_id=i.id ORDER BY e.id LIMIT 65)x),'[]'::jsonb),
   'rollup',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT r.* FROM em_content.context_project_rollups r WHERE r.library_id=i.library_id AND r.current_context_id=i.id LIMIT 2)x),'[]'::jsonb),
   'knowledge',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT k.* FROM em_content.knowledge_objects k WHERE k.library_id=i.library_id AND k.item_id=i.id ORDER BY k.knowledge_id LIMIT 65)x),'[]'::jsonb)
  ) AS payload,
  (SELECT max(s.id) FROM (SELECT s.id FROM em_content.context_sources s
   WHERE s.library_id=i.library_id AND s.item_id=i.id ORDER BY s.id LIMIT 64) s) AS source_after,
  EXISTS(SELECT 1 FROM em_content.context_sources s
   WHERE s.library_id=i.library_id AND s.item_id=i.id
   AND s.id > COALESCE((SELECT max(s2.id) FROM (SELECT s2.id FROM em_content.context_sources s2
    WHERE s2.library_id=i.library_id AND s2.item_id=i.id ORDER BY s2.id LIMIT 64) s2),0)) AS source_more
 FROM em_content.context_items i
 WHERE i.library_id=p_library AND i.id=ANY(p_ids)
 AND em_content.history_member(p_library,p_project,i.id) ORDER BY i.id LOOP
  rec.payload:=em_content.history_checked(rec.payload);
  IF EXISTS(SELECT 1 FROM jsonb_array_elements(rec.payload->'sources') source
   WHERE jsonb_array_length(source->'event_proofs')>64)
  THEN RAISE EXCEPTION 'history_dependency_limit'; END IF;
  total_bytes:=total_bytes+octet_length(rec.payload::text);
  IF total_bytes>8388608 THEN RAISE EXCEPTION 'history_dependency_limit'; END IF;
  id:=rec.id;payload:=rec.payload;source_after:=rec.source_after;source_more:=rec.source_more;
  RETURN NEXT;
 END LOOP;
END; $fn$;
REVOKE ALL ON FUNCTION em_content.history_item_inputs_first_page(uuid,uuid,bigint[]) FROM PUBLIC;

-- One strict-advance page.  A 65th row is fetched only as a sentinel and is
-- never returned.  An empty page is not permitted by the Python assembler.
CREATE FUNCTION em_content.history_item_sources_page(p_library uuid,p_project uuid,p_item bigint,p_after bigint)
RETURNS TABLE(source_id bigint,payload jsonb,has_more boolean)
LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE total_bytes bigint:=0; rec record;
BEGIN
 PERFORM em_security.require_history_scope(p_library,p_project);
 IF NOT em_content.history_member(p_library,p_project,p_item)
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_read_scope_unavailable'; END IF;
 IF p_after IS NULL OR p_after<1 OR NOT EXISTS(SELECT 1 FROM em_content.context_sources s
  WHERE s.library_id=p_library AND s.item_id=p_item AND s.id=p_after)
 THEN RAISE EXCEPTION 'invalid_history_source_cursor'; END IF;
 FOR rec IN
  WITH candidates AS MATERIALIZED (
   SELECT s.id,row_number() OVER(ORDER BY s.id) AS rn
   FROM em_content.context_sources s
   WHERE s.library_id=p_library AND s.item_id=p_item AND s.id>p_after
   ORDER BY s.id LIMIT 65),
  counted AS (SELECT count(*) AS n FROM candidates)
  SELECT c.id,em_content.history_source_payload(p_library,p_item,c.id) AS payload,counted.n>64 AS more
  FROM candidates c CROSS JOIN counted
  WHERE c.rn<=64 ORDER BY c.id
 LOOP
  rec.payload:=em_content.history_checked(rec.payload);
  -- Original 65th nested proof guard: an over-limit source is rejected, never truncated.
  IF jsonb_array_length(rec.payload->'event_proofs')>64
  THEN RAISE EXCEPTION 'history_dependency_limit'; END IF;
  total_bytes:=total_bytes+octet_length(rec.payload::text);
  IF total_bytes>8388608 THEN RAISE EXCEPTION 'history_dependency_limit'; END IF;
  source_id:=rec.id;payload:=rec.payload;has_more:=rec.more;
  RETURN NEXT;
 END LOOP;
END; $fn$;
REVOKE ALL ON FUNCTION em_content.history_item_sources_page(uuid,uuid,bigint,bigint) FROM PUBLIC;

-- Copy only the old input function's explicit EXECUTE recipients.  PUBLIC is
-- never a recipient; any non-PUBLIC default is revoked first.
DO $grants$ DECLARE recipient record; f text;
BEGIN
 -- Every new function (including the internal helper) is cleared from its OWN
 -- effective ACL, which is its explicit proacl or the default PUBLIC EXECUTE.
 FOREACH f IN ARRAY ARRAY[
  'em_content.history_source_payload(uuid,bigint,bigint)',
  'em_content.history_item_inputs_first_page(uuid,uuid,bigint[])',
  'em_content.history_item_sources_page(uuid,uuid,bigint,bigint)'] LOOP
  FOR recipient IN SELECT DISTINCT a.grantee FROM pg_proc p
   CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
   WHERE p.oid=to_regprocedure(f) AND a.grantee<>p.proowner
  LOOP
   IF recipient.grantee=0 THEN EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f);
   ELSE EXECUTE format('REVOKE ALL ON FUNCTION %s FROM %I',f,
    (SELECT rolname FROM pg_roles WHERE oid=recipient.grantee)); END IF;
  END LOOP;
 END LOOP;
 -- The helper has no scope gate of its own: owner-only, never granted below.
 IF EXISTS(SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
  WHERE p.oid=to_regprocedure('em_content.history_source_payload(uuid,bigint,bigint)') AND a.grantee<>p.proowner)
 THEN RAISE EXCEPTION 'history_source_payload_acl_not_owner_only'; END IF;
 -- Old explicit EXECUTE recipients are copied only to the two external entries.
 IF EXISTS(SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
  WHERE p.oid=to_regprocedure('em_content.history_item_inputs(uuid,uuid,bigint[])')
  AND (a.grantee=0 OR a.privilege_type<>'EXECUTE' OR a.is_grantable))
 THEN RAISE EXCEPTION 'history_item_inputs_acl_changed'; END IF;
 FOREACH f IN ARRAY ARRAY[
  'em_content.history_item_inputs_first_page(uuid,uuid,bigint[])',
  'em_content.history_item_sources_page(uuid,uuid,bigint,bigint)'] LOOP
  FOR recipient IN SELECT r.rolname FROM pg_proc p
   CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a JOIN pg_roles r ON r.oid=a.grantee
   WHERE p.oid=to_regprocedure('em_content.history_item_inputs(uuid,uuid,bigint[])')
   AND a.grantee<>p.proowner AND a.privilege_type='EXECUTE'
  LOOP
   EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I',f,recipient.rolname);
  END LOOP;
 END LOOP;
END; $grants$;

-- CREATE OR REPLACE preserves the SQL012 provisioner owner and ACL.  Its
-- invoker checks and 37-table revocations remain unchanged; only the two new
-- entrypoints are added to its grants.
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
 EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_item_inputs_first_page(uuid,uuid,bigint[]) TO %I',p_role);
 EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_item_sources_page(uuid,uuid,bigint,bigint) TO %I',p_role);
 EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_lexical_candidates(uuid,uuid,text,text[],text,float8,boolean,integer,text,jsonb) TO %I',p_role);
 EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_identity_family(uuid,uuid,jsonb) TO %I',p_role);
 EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_identity_family_qualified(uuid,uuid,jsonb) TO %I',p_role);
 EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_layer_read(uuid,uuid,bigint,text) TO %I',p_role);
 EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_knowledge_inputs(uuid,uuid,bigint[],text) TO %I',p_role);
 EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_legacy_projection(uuid,uuid,bigint[]) TO %I',p_role);
END; $fn$;
COMMIT;

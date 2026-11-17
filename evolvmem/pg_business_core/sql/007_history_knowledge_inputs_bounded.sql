-- Successor to applied 005: exact-key materialization before knowledge authorization.
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
 WHERE p.oid=to_regprocedure('em_content.history_knowledge_inputs(uuid,uuid,bigint[],text)')
 AND p.proowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)
 AND p.prosecdef AND p.provolatile='s' AND md5(p.prosrc)='64e52e9631328264a8fae9d46c40c6c2')
 THEN RAISE EXCEPTION 'history_knowledge_successor_preimage_changed'; END IF;
END; $guard$;
CREATE OR REPLACE FUNCTION em_content.history_knowledge_inputs(p_library uuid,p_project uuid,p_ids bigint[],p_at text)
RETURNS TABLE(payload jsonb) LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE result jsonb; section jsonb;
BEGIN
 PERFORM em_security.require_history_scope(p_library,p_project);
 IF p_ids IS NULL OR cardinality(p_ids)>240 OR EXISTS(SELECT 1 FROM unnest(p_ids) i WHERE i IS NULL OR i<1)
 OR p_at IS NULL OR length(p_at)>40 THEN RAISE EXCEPTION 'invalid_history_knowledge_request'; END IF;
 WITH wanted_candidates AS MATERIALIZED(SELECT k.* FROM em_content.knowledge_objects k
 WHERE k.library_id=p_library AND k.item_id=ANY(p_ids)),
 wanted_objects AS MATERIALIZED(SELECT k.* FROM wanted_candidates k
 WHERE em_content.history_member(p_library,p_project,k.item_id)),
 seed_groups AS MATERIALIZED(SELECT DISTINCT v.group_id FROM em_content.knowledge_fact_sources s
 JOIN em_content.knowledge_fact_versions v ON v.library_id=s.library_id AND v.id=s.version_id
 WHERE s.library_id=p_library AND s.knowledge_id IN(SELECT knowledge_id FROM wanted_objects)),
 base_candidates AS MATERIALIZED(SELECT k.* FROM em_content.knowledge_objects k
 WHERE k.library_id=p_library
 AND(k.knowledge_id IN(SELECT knowledge_id FROM wanted_objects) OR EXISTS(SELECT 1 FROM em_content.knowledge_fact_sources s
 JOIN em_content.knowledge_fact_versions v ON v.library_id=s.library_id AND v.id=s.version_id
 WHERE s.library_id=k.library_id AND s.knowledge_id=k.knowledge_id AND v.group_id IN(SELECT group_id FROM seed_groups)))
),
 base_objects AS MATERIALIZED(SELECT k.* FROM base_candidates k
 WHERE em_content.history_member(p_library,p_project,k.item_id) ORDER BY k.knowledge_id LIMIT 513),
 equivalent_candidates AS MATERIALIZED(SELECT k.* FROM em_content.knowledge_objects k
 WHERE k.library_id=p_library
 AND(k.knowledge_id IN(SELECT knowledge_id FROM base_objects) OR EXISTS(SELECT 1 FROM em_content.knowledge_equivalences e
 WHERE e.library_id=k.library_id AND e.active=1 AND
 ((e.source_knowledge_id=k.knowledge_id AND e.target_knowledge_id IN(SELECT knowledge_id FROM base_objects))
 OR(e.target_knowledge_id=k.knowledge_id AND e.source_knowledge_id IN(SELECT knowledge_id FROM base_objects)))))
),
 equivalent_objects AS MATERIALIZED(SELECT k.* FROM equivalent_candidates k
 WHERE em_content.history_member(p_library,p_project,k.item_id) ORDER BY k.knowledge_id LIMIT 513),
 wanted_groups AS MATERIALIZED(SELECT DISTINCT v.group_id FROM em_content.knowledge_fact_sources s
 JOIN em_content.knowledge_fact_versions v ON v.library_id=s.library_id AND v.id=s.version_id
 WHERE s.library_id=p_library AND s.knowledge_id IN(SELECT knowledge_id FROM equivalent_objects)),
 scoped_candidates AS MATERIALIZED(SELECT k.* FROM em_content.knowledge_objects k
 WHERE k.library_id=p_library
 AND(k.knowledge_id IN(SELECT knowledge_id FROM equivalent_objects) OR EXISTS(SELECT 1 FROM em_content.knowledge_fact_sources s
 JOIN em_content.knowledge_fact_versions v ON v.library_id=s.library_id AND v.id=s.version_id
 WHERE s.library_id=k.library_id AND s.knowledge_id=k.knowledge_id AND v.group_id IN(SELECT group_id FROM wanted_groups)))
),
 scoped_objects AS MATERIALIZED(SELECT k.* FROM scoped_candidates k
 WHERE em_content.history_member(p_library,p_project,k.item_id) ORDER BY k.knowledge_id LIMIT 513),
 versions AS MATERIALIZED(SELECT v.* FROM em_content.knowledge_fact_versions v
 WHERE v.library_id=p_library AND v.group_id IN(SELECT group_id FROM wanted_groups)
 AND EXISTS(SELECT 1 FROM em_content.knowledge_fact_sources s WHERE s.library_id=v.library_id
 AND s.version_id=v.id AND s.active=1 AND s.knowledge_id IN(SELECT knowledge_id FROM scoped_objects)) ORDER BY v.id LIMIT 513),
 derived_evidence_candidates AS MATERIALIZED(SELECT u.knowledge_id,d.item_id FROM em_content.unit_derivations d
   JOIN em_content.organization_units u ON u.library_id=d.library_id AND u.task_id=d.unit_task_id AND u.digest=d.unit_digest
   WHERE d.library_id=p_library AND u.knowledge_id IN(SELECT knowledge_id FROM scoped_objects)
 )
 SELECT jsonb_build_object(
  'knowledge_objects',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT k.* FROM scoped_objects k ORDER BY knowledge_id LIMIT 513)x),'[]'::jsonb),
  'knowledge_fact_groups',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT g.* FROM em_content.knowledge_fact_groups g WHERE g.library_id=p_library AND g.id IN(SELECT group_id FROM versions) ORDER BY id LIMIT 513)x),'[]'::jsonb),
  'knowledge_fact_versions',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT v.* FROM versions v ORDER BY id LIMIT 513)x),'[]'::jsonb),
  'knowledge_decisions',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT d.* FROM em_content.knowledge_decisions d WHERE d.library_id=p_library AND d.knowledge_id IN(SELECT knowledge_id FROM scoped_objects) ORDER BY knowledge_id,revision LIMIT 513)x),'[]'::jsonb),
  'knowledge_equivalences',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT e.* FROM em_content.knowledge_equivalences e WHERE e.library_id=p_library AND e.source_knowledge_id IN(SELECT knowledge_id FROM scoped_objects) AND e.target_knowledge_id IN(SELECT knowledge_id FROM scoped_objects) ORDER BY source_knowledge_id LIMIT 513)x),'[]'::jsonb),
  'knowledge_fact_actions',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT a.* FROM em_content.knowledge_fact_actions a WHERE a.library_id=p_library AND a.group_id IN(SELECT group_id FROM versions) AND a.action IN('choose','confirm') AND a.payload_json::jsonb->>'version_id' IN(SELECT id FROM versions) ORDER BY id LIMIT 513)x),'[]'::jsonb),
  'knowledge_fact_choices',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT c.* FROM em_content.knowledge_fact_choices c WHERE c.library_id=p_library AND c.version_id IN(SELECT id FROM versions) ORDER BY group_id,conditions_json LIMIT 513)x),'[]'::jsonb),
  'knowledge_fact_conditions',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT c.* FROM em_content.knowledge_fact_conditions c WHERE c.library_id=p_library AND c.version_id IN(SELECT id FROM versions) ORDER BY version_id LIMIT 513)x),'[]'::jsonb),
  'knowledge_fact_issues',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT i.library_id,i.project_id,i.source_rowid,i.id,i.group_id,i.conditions_json,COALESCE((SELECT jsonb_agg(v ORDER BY v)::text FROM jsonb_array_elements_text(i.members_json::jsonb) v WHERE v IN(SELECT id FROM versions)),'[]') AS members_json,i.kind,i.state,i.revision,i.updated_at FROM em_content.knowledge_fact_issues i WHERE i.library_id=p_library AND i.group_id IN(SELECT group_id FROM versions) ORDER BY id LIMIT 513)x),'[]'::jsonb),
  'knowledge_fact_relations',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT r.* FROM em_content.knowledge_fact_relations r WHERE r.library_id=p_library AND r.knowledge_id IN(SELECT knowledge_id FROM scoped_objects) AND r.version_id IN(SELECT id FROM versions) ORDER BY id LIMIT 513)x),'[]'::jsonb),
  'knowledge_fact_separations',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT s.* FROM em_content.knowledge_fact_separations s WHERE s.library_id=p_library AND s.knowledge_id IN(SELECT knowledge_id FROM scoped_objects) AND s.group_id IN(SELECT group_id FROM versions) ORDER BY base_group,knowledge_id LIMIT 513)x),'[]'::jsonb),
  'knowledge_fact_sources',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT s.* FROM em_content.knowledge_fact_sources s WHERE s.library_id=p_library AND s.knowledge_id IN(SELECT knowledge_id FROM scoped_objects) AND s.version_id IN(SELECT id FROM versions) ORDER BY version_id,knowledge_id,source_key,start,"end" LIMIT 513)x),'[]'::jsonb),
  'knowledge_metadata',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT m.* FROM em_content.knowledge_metadata m WHERE m.library_id=p_library AND m.item_id IN(SELECT item_id FROM scoped_objects) ORDER BY item_id LIMIT 513)x),'[]'::jsonb),
  'knowledge_source_bindings',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT s.* FROM em_content.knowledge_source_bindings s WHERE s.library_id=p_library AND s.knowledge_id IN(SELECT knowledge_id FROM scoped_objects) ORDER BY knowledge_id,source_key,source_start,source_end LIMIT 513)x),'[]'::jsonb),
  'knowledge_evidence_items',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (
   SELECT k.knowledge_id,k.item_id FROM scoped_objects k
   UNION SELECT d.knowledge_id,d.item_id FROM derived_evidence_candidates d
   WHERE em_content.history_member(p_library,p_project,d.item_id)
   ORDER BY knowledge_id,item_id LIMIT 513)x),'[]'::jsonb),
  'restricted_conflicts',EXISTS(SELECT 1 FROM em_content.knowledge_fact_versions v WHERE v.library_id=p_library
   AND v.group_id IN(SELECT group_id FROM wanted_groups) AND v.id NOT IN(SELECT id FROM versions))
 ) INTO result;
 IF octet_length(result::text)>8388608 THEN RAISE EXCEPTION 'history_dependency_limit'; END IF;
 FOR section IN SELECT value FROM jsonb_each(result) WHERE jsonb_typeof(value)='array' LOOP
  IF jsonb_array_length(section)>512 THEN RAISE EXCEPTION 'history_dependency_limit'; END IF;
 END LOOP;
 payload:=result;RETURN NEXT;
END; $fn$;
-- CREATE OR REPLACE preserves existing owner and exact runtime EXECUTE grants.
REVOKE ALL ON FUNCTION em_content.history_knowledge_inputs(uuid,uuid,bigint[],text) FROM PUBLIC;
COMMIT;

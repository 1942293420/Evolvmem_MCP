-- Successor to applied 005: admit the canonical empty business experience project.
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
 WHERE p.oid=to_regprocedure('em_content.history_lexical_candidates(uuid,uuid,text,text[],text,float8,boolean,integer,text,jsonb)')
 AND p.proowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)
 AND p.prosecdef AND p.provolatile='s' AND md5(p.prosrc)='adda3172b898fedeaa7de44a111ca667')
 THEN RAISE EXCEPTION 'history_experience_project_successor_preimage_changed'; END IF;
END; $guard$;
CREATE OR REPLACE FUNCTION em_content.history_lexical_candidates(p_library uuid,p_project uuid,p_query text,p_types text[],p_moment text,
 p_confidence float8,p_include_reference boolean,p_limit integer,p_experience_project text,p_experience_constraints jsonb)
RETURNS TABLE(item_id bigint,layer text,score real,content_type text,project text,status text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE expression text; query tsquery;
BEGIN
 PERFORM em_security.require_history_scope(p_library,p_project);
 IF p_query IS NULL OR length(btrim(p_query)) NOT BETWEEN 1 AND 4000 OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 240
 OR p_types IS NULL OR cardinality(p_types)>16 OR p_moment IS NULL OR length(p_moment)>40
 OR p_confidence IS NULL OR NOT(p_confidence BETWEEN 0 AND 1) OR p_include_reference IS NULL
 THEN RAISE EXCEPTION 'invalid_history_lexical_request'; END IF;
 IF (p_experience_project IS NULL)<>(p_experience_constraints IS NULL)
 OR (p_experience_project IS NOT NULL AND (length(p_experience_project) NOT BETWEEN 0 AND 256
  OR jsonb_typeof(p_experience_constraints)<>'object'
  OR (SELECT count(*) FROM jsonb_each(CASE WHEN jsonb_typeof(p_experience_constraints)='object'
      THEN p_experience_constraints ELSE '{}'::jsonb END))>20
  OR EXISTS(SELECT 1 FROM jsonb_each(CASE WHEN jsonb_typeof(p_experience_constraints)='object'
      THEN p_experience_constraints ELSE '{}'::jsonb END) condition WHERE jsonb_typeof(condition.value)<>'string')))
 THEN RAISE EXCEPTION 'invalid_history_experience_candidates'; END IF;
 SELECT string_agg(quote_literal(t),' | ' ORDER BY length(t) DESC,t) INTO expression
 FROM(SELECT t FROM unnest(string_to_array(em_content.history_lexical_text(p_query),' ')) t
 WHERE t<>'' GROUP BY t ORDER BY length(t) DESC,t LIMIT 40) terms;
 IF expression IS NULL THEN RAISE EXCEPTION 'invalid_history_lexical_request'; END IF;
 query:=to_tsquery('simple',expression);
 RETURN QUERY SELECT l.item_id,l.layer,ts_rank_cd(to_tsvector('simple',em_content.history_lexical_text(l.content)),query),
 c.content_type,c.project,c.status FROM em_content.context_layers l JOIN em_content.context_items c
 ON c.library_id=l.library_id AND c.id=l.item_id
 WHERE l.library_id=p_library AND em_content.history_member(p_library,p_project,c.id)
 AND l.layer IN('l0','l1') AND c.status IN('active','superseded')
 AND (cardinality(p_types)=0 OR c.content_type=ANY(p_types)) AND c.confidence>=p_confidence
 AND (p_include_reference OR c.tier<>'reference')
 AND (c.expires_at IS NULL OR c.expires_at>p_moment)
 AND (c.effective_from IS NULL OR c.effective_from<=p_moment)
 AND (c.effective_until IS NULL OR c.effective_until>p_moment)
 AND (c.status='active' OR(c.content_type='decision' AND(c.effective_from IS NOT NULL OR c.occurred_at IS NOT NULL)))
 AND to_tsvector('simple',em_content.history_lexical_text(l.content))@@query
 -- Native applicability is part of candidate admission, before the fixed LIMIT.
 AND (p_experience_project IS NULL OR (c.status='active'
        AND jsonb_typeof(c.experience_payload)='object' AND c.experience_payload<>'{}'::jsonb
        AND jsonb_typeof(c.experience_payload->'conditions')='object'
        AND (c.project=p_experience_project OR c.experience_payload->'transferable'='true'::jsonb)
        AND NOT EXISTS (SELECT 1 FROM jsonb_each(CASE
          WHEN jsonb_typeof(c.experience_payload->'conditions')='object'
          THEN c.experience_payload->'conditions' ELSE '{}'::jsonb END) AS evidence_condition(key,value)
          WHERE p_experience_constraints::jsonb ? evidence_condition.key
           AND (p_experience_constraints::jsonb->evidence_condition.key) IS DISTINCT FROM evidence_condition.value)))
 ORDER BY 3 DESC,l.item_id,l.layer LIMIT p_limit;
END; $fn$;
-- CREATE OR REPLACE preserves existing owner and exact runtime EXECUTE grants.
REVOKE ALL ON FUNCTION em_content.history_lexical_candidates(uuid,uuid,text,text[],text,float8,boolean,integer,text,jsonb) FROM PUBLIC;
COMMIT;

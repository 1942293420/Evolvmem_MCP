-- history-preserved-v1. Trusted psql variables: database_name owner_role library_id.
-- Install only in the isolated history staging target, after catch-up epoch DDL.
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
 IF to_regclass('em_content.history_schema_migrations') IS NULL
 OR to_regclass('em_content.history_knowledge_schema_migrations') IS NULL
 OR to_regclass('em_content.history_catchup_latest') IS NULL
 OR to_regclass('em_content.history_catchup_epochs') IS NULL
 OR to_regclass('em_content.native_qualification_migrations') IS NOT NULL
 OR NOT EXISTS(SELECT 1 FROM em_security.library_state WHERE kind='legacy'
 AND authority_state='pg_staging' AND acl_state='updating')
 THEN RAISE EXCEPTION 'history_preserved_install_precondition'; END IF;
END; $guard$;
CREATE TABLE em_content.history_read_memberships(
 library_id uuid NOT NULL,item_id bigint NOT NULL,project_id uuid NOT NULL,project_key text NOT NULL,
 registry_project_uuid uuid,decision_ref text NOT NULL,decision_revision bigint NOT NULL CHECK(decision_revision>=0),
 decision_hash text NOT NULL CHECK(decision_hash~'^[0-9a-f]{64}$'),
 state text NOT NULL CHECK(state IN('confirmed','held')),reason text NOT NULL,
 mapping_revision bigint NOT NULL CHECK(mapping_revision>0),
 PRIMARY KEY(library_id,item_id,project_id),
 FOREIGN KEY(library_id,item_id) REFERENCES em_content.context_items(library_id,id) ON DELETE CASCADE,
 CHECK((state='confirmed' AND reason='' AND registry_project_uuid IS NOT NULL AND decision_revision>0) OR state='held')
);
CREATE INDEX history_membership_project ON em_content.history_read_memberships(library_id,project_id,item_id);
CREATE TABLE em_content.history_source_event_proofs(
 library_id uuid NOT NULL,source_id bigint NOT NULL,item_id bigint NOT NULL,evidence_id bigint NOT NULL,
 evidence_revision bigint NOT NULL CHECK(evidence_revision>0),experience_version bigint NOT NULL CHECK(experience_version>0),
 evidence_note_hash text NOT NULL CHECK(evidence_note_hash~'^[0-9a-f]{64}$'),
 item_revision bigint NOT NULL CHECK(item_revision>0),item_content_hash text NOT NULL CHECK(item_content_hash~'^[0-9a-f]{64}$'),
 source_ref text NOT NULL,extraction_version text NOT NULL,archive_id bigint NOT NULL,
 archive_payload_sha256 text NOT NULL CHECK(archive_payload_sha256~'^[0-9a-f]{64}$'),
 task_id text NOT NULL CHECK(length(task_id) BETWEEN 1 AND 500),role text NOT NULL CHECK(role IN('user','tool')),
 event_hash text NOT NULL CHECK(event_hash~'^[0-9a-f]{64}$'),event_ref text NOT NULL,
 state text NOT NULL CHECK(state IN('verified','withdrawn')),proof_hash text NOT NULL CHECK(proof_hash~'^[0-9a-f]{64}$'),
 PRIMARY KEY(library_id,source_id,evidence_id),
 FOREIGN KEY(library_id,item_id) REFERENCES em_content.context_items(library_id,id) ON DELETE CASCADE,
 FOREIGN KEY(library_id,source_id) REFERENCES em_content.context_sources(library_id,id) ON DELETE CASCADE,
 FOREIGN KEY(library_id,evidence_id) REFERENCES em_content.context_evidence(library_id,id) ON DELETE CASCADE,
 FOREIGN KEY(library_id,archive_id) REFERENCES em_content.session_archives(library_id,id),
 CHECK(extraction_version='experience-v1:'||event_hash)
);
CREATE TABLE em_security.history_membership_state(
 library_id uuid PRIMARY KEY REFERENCES em_security.library_state(library_id),
 mapping_revision bigint NOT NULL DEFAULT 0 CHECK(mapping_revision>=0),membership_hash text,
 completion_epoch uuid,completion_hash text,dirty boolean NOT NULL DEFAULT true
);
CREATE TABLE em_security.history_readiness(
 library_id uuid PRIMARY KEY REFERENCES em_security.library_state(library_id),
 schema_version integer NOT NULL CHECK(schema_version=1),mode text NOT NULL CHECK(mode='history-preserved-v1'),
 enabled boolean NOT NULL DEFAULT false,revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
 mapping_revision bigint NOT NULL DEFAULT 0,membership_hash text,completion_epoch uuid,completion_hash text,
 policy_epoch bigint,route_epoch bigint
);
INSERT INTO em_security.history_membership_state(library_id) VALUES(:'library_id'::uuid);
INSERT INTO em_security.history_readiness(library_id,schema_version,mode) VALUES(:'library_id'::uuid,1,'history-preserved-v1');
DO $policies$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['em_content.history_read_memberships','em_content.history_source_event_proofs','em_security.history_membership_state','em_security.history_readiness'] LOOP
  EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY',t);
  EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY',t);
  EXECUTE format('CREATE POLICY history_owner_only ON %s TO %I USING(true) WITH CHECK(true)',t,current_user);
  EXECUTE format('REVOKE ALL ON %s FROM PUBLIC',t);
 END LOOP;
END; $policies$;
CREATE FUNCTION em_security.invalidate_history_reads() RETURNS trigger LANGUAGE plpgsql
 SET search_path=pg_catalog,em_security AS $fn$
BEGIN
 UPDATE em_security.history_membership_state SET dirty=true;
 UPDATE em_security.history_readiness SET enabled=false,revision=revision+1;
 RETURN NULL;
END; $fn$;
REVOKE ALL ON FUNCTION em_security.invalidate_history_reads() FROM PUBLIC;
-- Only invalidates the independent read gate; historical import proofs stay untouched.
DO $triggers$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['context_items','context_layers','context_sources','context_evidence','context_project_registry',
 'context_project_resolutions','learning_memories','unit_derivations','organization_units','organization_tasks',
 'organization_context_basis','organization_conversations','knowledge_cleaning_reviews','context_project_rollups',
 'session_archives','session_archive_storage','archive_source_exclusions','memories','legacy_memory_migrations','knowledge_metadata','knowledge_source_bindings','knowledge_fact_actions','knowledge_fact_groups','knowledge_objects','knowledge_decisions','knowledge_fact_sources',
 'knowledge_fact_versions','knowledge_fact_choices','knowledge_fact_conditions','knowledge_fact_issues',
 'knowledge_fact_relations','knowledge_fact_separations','knowledge_equivalences','history_read_memberships','history_source_event_proofs'] LOOP
  EXECUTE format('CREATE TRIGGER history_read_invalidation AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE ON em_content.%I FOR EACH STATEMENT EXECUTE FUNCTION em_security.invalidate_history_reads()',t);
  EXECUTE format('ALTER TABLE em_content.%I ENABLE ALWAYS TRIGGER history_read_invalidation',t);
 END LOOP;
END; $triggers$;

CREATE FUNCTION em_security.history_recall_context(p_library uuid,p_project uuid)
RETURNS TABLE(schema_version integer,mode text,policy_epoch bigint,route_epoch bigint,
 readiness_revision bigint,mapping_revision bigint,completion_hash text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
 SELECT r.schema_version,r.mode,c.policy_epoch,c.route_epoch,r.revision,m.mapping_revision,e.completion_hash
 FROM em_security.session_context() c JOIN em_security.history_readiness r USING(library_id)
 JOIN em_security.library_state ls USING(library_id)
 JOIN em_security.history_membership_state m USING(library_id)
 JOIN em_content.history_catchup_latest l USING(library_id)
 JOIN em_content.history_catchup_epochs e ON e.library_id=l.library_id AND e.epoch_id=l.epoch_id
 WHERE c.library_id=p_library AND r.enabled AND NOT m.dirty
 AND r.schema_version=1 AND r.mode='history-preserved-v1' AND e.state='complete'
 AND NOT EXISTS(SELECT 1 FROM em_content.history_catchup_epochs pending WHERE pending.library_id=c.library_id AND pending.state<>'complete')
 AND e.completion_hash=l.completion_hash AND r.completion_epoch=e.epoch_id AND m.completion_epoch=e.epoch_id
 AND r.completion_hash=e.completion_hash AND m.completion_hash=e.completion_hash
 AND r.mapping_revision=m.mapping_revision AND m.mapping_revision>0
 AND r.membership_hash=m.membership_hash AND r.membership_hash~'^[0-9a-f]{64}$'
 AND r.policy_epoch=c.policy_epoch AND r.route_epoch=c.route_epoch
 AND current_setting('transaction_isolation')='repeatable read' AND current_setting('transaction_read_only')='on'
 AND em_security.allows(p_library,p_project,'read')
 AND ((p_project IS NULL AND c.principal_id=ls.owner_principal_id)
  OR (p_project IS NOT NULL AND EXISTS(SELECT 1 FROM em_security.project_projection p WHERE p.project_id=p_project
   AND p.active AND p.projection_epoch=c.policy_epoch)))
$fn$;
REVOKE ALL ON FUNCTION em_security.history_recall_context(uuid,uuid) FROM PUBLIC;

CREATE FUNCTION em_security.require_history_scope(p_library uuid,p_project uuid) RETURNS void
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security AS $fn$
BEGIN
 IF (SELECT count(*) FROM em_security.history_recall_context(p_library,p_project))<>1
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_read_scope_unavailable'; END IF;
END; $fn$;
REVOKE ALL ON FUNCTION em_security.require_history_scope(uuid,uuid) FROM PUBLIC;

CREATE FUNCTION em_content.history_member(p_library uuid,p_project uuid,p_item bigint) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
 -- NULL denotes only original global rows of the actual bound library owner.
 -- It is never a wildcard for preserved project_id=NULL historical rows.
 SELECT CASE WHEN p_project IS NULL THEN EXISTS(
 SELECT 1 FROM em_content.context_items i
 JOIN em_security.library_state ls ON ls.library_id=i.library_id
 JOIN em_security.session_context() c ON c.library_id=i.library_id AND c.principal_id=ls.owner_principal_id
 WHERE i.library_id=p_library AND i.id=p_item AND i.scope='global' AND i.project='' AND i.projects='[]'::jsonb
 AND em_security.allows(p_library,NULL,'read')
 AND NOT EXISTS(SELECT 1 FROM em_content.context_project_resolutions r WHERE r.library_id=i.library_id AND r.item_id=i.id
  AND (r.resolved_project<>'' OR r.review_state IN('pending','rejected','withdrawn')
   OR (r.resolution_state IN('unresolved','conflict') AND NOT(r.decision_source='human' AND r.review_state='accepted'))))
 AND NOT EXISTS(SELECT 1 FROM em_content.knowledge_objects k WHERE k.library_id=i.library_id AND k.item_id=i.id
  AND (k.retired<>0 OR COALESCE(k.decision_json::jsonb->>'action','')<>'include'
   OR COALESCE(NULLIF(k.decision_json::jsonb->>'knowledge_scope',''),NULLIF(k.decision_json::jsonb->>'scope',''),'project')<>'global'
   OR COALESCE(k.decision_json::jsonb->'projects','[]'::jsonb)<>'[]'::jsonb
   OR COALESCE(k.decision_json::jsonb->>'project','')<>'')))
 ELSE EXISTS(SELECT 1 FROM em_content.history_read_memberships m
 JOIN em_security.history_membership_state s USING(library_id)
 JOIN em_content.context_project_registry r ON r.library_id=m.library_id AND r.project=m.project_key
 JOIN em_security.project_projection p ON p.project_id=m.project_id AND p.project_key=m.project_key
 WHERE m.library_id=p_library AND m.project_id=p_project AND m.item_id=p_item
 AND m.state='confirmed' AND m.mapping_revision=s.mapping_revision
 AND r.project_uuid=m.registry_project_uuid AND r.status='active' AND p.active) END
$fn$;
REVOKE ALL ON FUNCTION em_content.history_member(uuid,uuid,bigint) FROM PUBLIC;

CREATE FUNCTION em_content.history_checked(p_payload jsonb) RETURNS jsonb
LANGUAGE plpgsql IMMUTABLE SET search_path=pg_catalog AS $fn$
DECLARE k text; v jsonb;
BEGIN
 IF octet_length(p_payload::text)>8388608 THEN RAISE EXCEPTION 'history_dependency_limit'; END IF;
 FOR k,v IN SELECT key,value FROM jsonb_each(p_payload) LOOP
  IF jsonb_typeof(v)='array' AND jsonb_array_length(v)>64 THEN RAISE EXCEPTION 'history_dependency_limit'; END IF;
 END LOOP;
 RETURN p_payload;
END; $fn$;
REVOKE ALL ON FUNCTION em_content.history_checked(jsonb) FROM PUBLIC;

CREATE FUNCTION em_content.history_item_inputs(p_library uuid,p_project uuid,p_ids bigint[])
RETURNS TABLE(id bigint,payload jsonb) LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE total_bytes bigint:=0; rec record;
BEGIN
 PERFORM em_security.require_history_scope(p_library,p_project);
 IF p_ids IS NULL OR cardinality(p_ids)>512 OR EXISTS(SELECT 1 FROM unnest(p_ids) n WHERE n IS NULL OR n<1)
 THEN RAISE EXCEPTION 'invalid_history_ids'; END IF;
 FOR rec IN SELECT i.id,jsonb_build_object(
  'item',to_jsonb(i),
  'item_l1_hash',(SELECT l.content_hash FROM em_content.context_layers l WHERE l.library_id=i.library_id AND l.item_id=i.id AND l.layer='l1'),
  'memberships',COALESCE((SELECT jsonb_agg(to_jsonb(m)) FROM em_content.history_read_memberships m WHERE m.library_id=i.library_id AND m.item_id=i.id AND m.project_id=p_project),'[]'::jsonb),
  'layers',COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.layer) FROM (SELECT l.*,octet_length(l.content) AS content_bytes FROM em_content.context_layers l WHERE l.library_id=i.library_id AND l.item_id=i.id ORDER BY l.layer LIMIT 4)x),'[]'::jsonb),
  'learning',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT l.* FROM em_content.learning_memories l WHERE l.library_id=i.library_id AND l.item_id=i.id LIMIT 2)x),'[]'::jsonb),
  'resolution',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT r.* FROM em_content.context_project_resolutions r WHERE r.library_id=i.library_id AND r.item_id=i.id LIMIT 2)x),'[]'::jsonb),
  'registry',COALESCE((SELECT jsonb_agg(to_jsonb(x)) FROM (SELECT r.* FROM em_content.context_project_registry r JOIN em_content.history_read_memberships m ON m.library_id=r.library_id AND m.project_key=r.project WHERE r.library_id=i.library_id AND m.item_id=i.id AND m.project_id=p_project LIMIT 2)x),'[]'::jsonb),
  'sources',COALESCE((SELECT jsonb_agg(to_jsonb(x) ORDER BY x.id) FROM (SELECT s.*,
    (SELECT jsonb_build_object('id',a.id,'state',a.state,'payload_sha256',a.payload_sha256) FROM em_content.session_archives a WHERE a.library_id=s.library_id AND a.id=s.archive_id) AS archive,
    (SELECT to_jsonb(e) FROM em_content.archive_source_exclusions e WHERE e.library_id=s.library_id AND e.archive_id=s.archive_id) AS exclusion
    ,COALESCE((SELECT jsonb_agg(to_jsonb(p) ORDER BY p.evidence_id) FROM (
     SELECT p.* FROM em_content.history_source_event_proofs p
     JOIN em_content.context_evidence e ON e.library_id=p.library_id AND e.id=p.evidence_id AND e.source_id=p.source_id AND e.item_id=p.item_id
     JOIN em_content.context_layers l ON l.library_id=p.library_id AND l.item_id=p.item_id AND l.layer='l1'
     JOIN em_content.session_archives a ON a.library_id=p.library_id AND a.id=p.archive_id
     WHERE p.library_id=s.library_id AND p.source_id=s.id AND p.item_id=i.id AND p.state='verified'
      AND p.item_revision=i.revision AND p.item_content_hash=l.content_hash
      AND p.evidence_revision=e.revision AND p.experience_version=e.experience_version
      AND p.evidence_note_hash=encode(sha256(convert_to(e.note,'UTF8')),'hex')
      AND p.task_id=e.task_id AND p.source_ref=s.source_ref AND p.extraction_version=s.extraction_version
      AND p.archive_id=s.archive_id AND p.archive_payload_sha256=a.payload_sha256 AND a.state='available'
      AND p.event_ref=s.source_ref
      AND ((s.source_kind='tool_result' AND p.role='tool') OR(s.source_kind='user_confirmation' AND p.role='user'))
     ORDER BY p.evidence_id LIMIT 65)p),'[]'::jsonb) AS event_proofs
    FROM em_content.context_sources s WHERE s.library_id=i.library_id AND s.item_id=i.id ORDER BY s.id LIMIT 65)x),'[]'::jsonb),
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
 ) AS payload FROM em_content.context_items i WHERE i.library_id=p_library AND i.id=ANY(p_ids)
 AND em_content.history_member(p_library,p_project,i.id) ORDER BY i.id LOOP
  rec.payload:=em_content.history_checked(rec.payload);
  IF EXISTS(SELECT 1 FROM jsonb_array_elements(rec.payload->'sources') source WHERE jsonb_array_length(source->'event_proofs')>64) THEN RAISE EXCEPTION 'history_dependency_limit'; END IF;
  total_bytes:=total_bytes+octet_length(rec.payload::text);
  IF total_bytes>8388608 THEN RAISE EXCEPTION 'history_dependency_limit'; END IF;
  id:=rec.id;payload:=rec.payload;RETURN NEXT;
 END LOOP;
END; $fn$;
REVOKE ALL ON FUNCTION em_content.history_item_inputs(uuid,uuid,bigint[]) FROM PUBLIC;

CREATE FUNCTION em_content.history_lexical_text(value text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE SET search_path=pg_catalog AS $fn$
 WITH words AS(SELECT w FROM unnest(regexp_split_to_array(lower(value),'[^[:alnum:]_一-鿿]+')) w WHERE w<>''),
 tokens AS(SELECT w AS token FROM words UNION ALL SELECT substr(w,p,2) FROM words
 CROSS JOIN LATERAL generate_series(1,length(w)-1) p WHERE w~'[一-鿿]')
 SELECT coalesce(string_agg(token,' ' ORDER BY token),'') FROM tokens
$fn$;
REVOKE ALL ON FUNCTION em_content.history_lexical_text(text) FROM PUBLIC;
CREATE INDEX history_layers_recall_fts ON em_content.context_layers
 USING gin(to_tsvector('simple'::regconfig,em_content.history_lexical_text(content))) WHERE layer IN('l0','l1');
CREATE FUNCTION em_content.history_lexical_candidates(p_library uuid,p_project uuid,p_query text,p_types text[],p_moment text,
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
 OR (p_experience_project IS NOT NULL AND (length(p_experience_project) NOT BETWEEN 1 AND 256
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
REVOKE ALL ON FUNCTION em_content.history_lexical_candidates(uuid,uuid,text,text[],text,float8,boolean,integer,text,jsonb) FROM PUBLIC;

CREATE FUNCTION em_content.history_identity_family(p_library uuid,p_project uuid,p_keys jsonb)
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
 IF EXISTS(SELECT 1 FROM jsonb_array_elements(p_keys) k WHERE NOT EXISTS(SELECT 1 FROM em_content.context_items i
 WHERE i.library_id=p_library AND i.identity_key=k->>0 AND i.project=k->>1 AND i.scope=k->>2
 AND em_content.history_member(p_library,p_project,i.id)))
 THEN RAISE EXCEPTION 'history_family_scope_unavailable'; END IF;
 SELECT count(*) INTO n FROM(SELECT i.id FROM em_content.context_items i WHERE i.library_id=p_library
 AND EXISTS(SELECT 1 FROM jsonb_array_elements(p_keys) k WHERE i.identity_key=k->>0 AND i.project=k->>1 AND i.scope=k->>2)
 LIMIT 513) counted;
 IF n>512 THEN RAISE EXCEPTION 'history_dependency_limit'; END IF;
 IF EXISTS(SELECT 1 FROM em_content.context_items i WHERE i.library_id=p_library
 AND EXISTS(SELECT 1 FROM jsonb_array_elements(p_keys) k WHERE i.identity_key=k->>0 AND i.project=k->>1 AND i.scope=k->>2)
 AND NOT em_content.history_member(p_library,p_project,i.id))
 THEN RAISE EXCEPTION 'history_family_scope_unavailable'; END IF;
 RETURN QUERY SELECT i.identity_key,i.project,i.scope,i.id FROM em_content.context_items i WHERE i.library_id=p_library
 AND EXISTS(SELECT 1 FROM jsonb_array_elements(p_keys) k WHERE i.identity_key=k->>0 AND i.project=k->>1 AND i.scope=k->>2)
 ORDER BY i.id;
END; $fn$;
REVOKE ALL ON FUNCTION em_content.history_identity_family(uuid,uuid,jsonb) FROM PUBLIC;

CREATE FUNCTION em_content.history_layer_read(p_library uuid,p_project uuid,p_item bigint,p_layer text)
RETURNS TABLE(payload jsonb) LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,em_security,em_content AS $fn$
BEGIN
 PERFORM em_security.require_history_scope(p_library,p_project);
 IF p_item IS NULL OR p_item<1 OR p_layer IS NULL OR p_layer NOT IN('l0','l1','l2') THEN RAISE EXCEPTION 'invalid_history_layer'; END IF;
 RETURN QUERY SELECT em_content.history_checked(jsonb_build_object('item',to_jsonb(i),'layer',to_jsonb(l),
 'memberships',COALESCE((SELECT jsonb_agg(to_jsonb(m)) FROM em_content.history_read_memberships m WHERE m.library_id=i.library_id AND m.item_id=i.id AND m.project_id=p_project),'[]'::jsonb),
 'readiness',(SELECT to_jsonb(r) FROM em_security.history_recall_context(p_library,p_project) r)))
 FROM em_content.context_items i JOIN em_content.context_layers l ON l.library_id=i.library_id AND l.item_id=i.id
 WHERE i.library_id=p_library AND i.id=p_item AND l.layer=p_layer AND em_content.history_member(p_library,p_project,i.id);
END; $fn$;
REVOKE ALL ON FUNCTION em_content.history_layer_read(uuid,uuid,bigint,text) FROM PUBLIC;

CREATE FUNCTION em_content.history_legacy_projection(p_library uuid,p_project uuid,p_ids bigint[])
RETURNS TABLE(item_id bigint,payload jsonb) LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE total_bytes bigint:=0; rec record; n integer:=0;
BEGIN
 PERFORM em_security.require_history_scope(p_library,p_project);
 IF p_ids IS NULL OR cardinality(p_ids)>512 OR EXISTS(SELECT 1 FROM unnest(p_ids) i WHERE i IS NULL OR i<1)
 THEN RAISE EXCEPTION 'invalid_history_ids'; END IF;
 FOR rec IN SELECT m.context_item_id AS item_id,to_jsonb(l) AS payload FROM em_content.legacy_memory_migrations m
 JOIN em_content.memories l ON l.library_id=m.library_id AND l.id=m.legacy_memory_id
 WHERE m.library_id=p_library AND m.context_item_id=ANY(p_ids) AND l.status='active'
 AND em_content.history_member(p_library,p_project,m.context_item_id) ORDER BY m.context_item_id,l.id LIMIT 513 LOOP
  n:=n+1;total_bytes:=total_bytes+octet_length(rec.payload::text);
  IF n>512 OR total_bytes>8388608 THEN RAISE EXCEPTION 'history_dependency_limit'; END IF;
  item_id:=rec.item_id;payload:=rec.payload;RETURN NEXT;
 END LOOP;
END; $fn$;
REVOKE ALL ON FUNCTION em_content.history_legacy_projection(uuid,uuid,bigint[]) FROM PUBLIC;

CREATE FUNCTION em_content.history_knowledge_inputs(p_library uuid,p_project uuid,p_ids bigint[],p_at text)
RETURNS TABLE(payload jsonb) LANGUAGE plpgsql STABLE SECURITY DEFINER
SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE result jsonb; section jsonb;
BEGIN
 PERFORM em_security.require_history_scope(p_library,p_project);
 IF p_ids IS NULL OR cardinality(p_ids)>240 OR EXISTS(SELECT 1 FROM unnest(p_ids) i WHERE i IS NULL OR i<1)
 OR p_at IS NULL OR length(p_at)>40 THEN RAISE EXCEPTION 'invalid_history_knowledge_request'; END IF;
 WITH wanted_objects AS MATERIALIZED(SELECT k.* FROM em_content.knowledge_objects k
 WHERE k.library_id=p_library AND k.item_id=ANY(p_ids) AND em_content.history_member(p_library,p_project,k.item_id)),
 seed_groups AS MATERIALIZED(SELECT DISTINCT v.group_id FROM em_content.knowledge_fact_sources s
 JOIN em_content.knowledge_fact_versions v ON v.library_id=s.library_id AND v.id=s.version_id
 WHERE s.library_id=p_library AND s.knowledge_id IN(SELECT knowledge_id FROM wanted_objects)),
 base_objects AS MATERIALIZED(SELECT k.* FROM em_content.knowledge_objects k
 WHERE k.library_id=p_library AND em_content.history_member(p_library,p_project,k.item_id)
 AND(k.knowledge_id IN(SELECT knowledge_id FROM wanted_objects) OR EXISTS(SELECT 1 FROM em_content.knowledge_fact_sources s
 JOIN em_content.knowledge_fact_versions v ON v.library_id=s.library_id AND v.id=s.version_id
 WHERE s.library_id=k.library_id AND s.knowledge_id=k.knowledge_id AND v.group_id IN(SELECT group_id FROM seed_groups)))
 ORDER BY k.knowledge_id LIMIT 513),
 equivalent_objects AS MATERIALIZED(SELECT k.* FROM em_content.knowledge_objects k
 WHERE k.library_id=p_library AND em_content.history_member(p_library,p_project,k.item_id)
 AND(k.knowledge_id IN(SELECT knowledge_id FROM base_objects) OR EXISTS(SELECT 1 FROM em_content.knowledge_equivalences e
 WHERE e.library_id=k.library_id AND e.active=1 AND
 ((e.source_knowledge_id=k.knowledge_id AND e.target_knowledge_id IN(SELECT knowledge_id FROM base_objects))
 OR(e.target_knowledge_id=k.knowledge_id AND e.source_knowledge_id IN(SELECT knowledge_id FROM base_objects)))))
 ORDER BY k.knowledge_id LIMIT 513),
 wanted_groups AS MATERIALIZED(SELECT DISTINCT v.group_id FROM em_content.knowledge_fact_sources s
 JOIN em_content.knowledge_fact_versions v ON v.library_id=s.library_id AND v.id=s.version_id
 WHERE s.library_id=p_library AND s.knowledge_id IN(SELECT knowledge_id FROM equivalent_objects)),
 scoped_objects AS MATERIALIZED(SELECT k.* FROM em_content.knowledge_objects k
 WHERE k.library_id=p_library AND em_content.history_member(p_library,p_project,k.item_id)
 AND(k.knowledge_id IN(SELECT knowledge_id FROM equivalent_objects) OR EXISTS(SELECT 1 FROM em_content.knowledge_fact_sources s
 JOIN em_content.knowledge_fact_versions v ON v.library_id=s.library_id AND v.id=s.version_id
 WHERE s.library_id=k.library_id AND s.knowledge_id=k.knowledge_id AND v.group_id IN(SELECT group_id FROM wanted_groups)))
 ORDER BY k.knowledge_id LIMIT 513),
 versions AS MATERIALIZED(SELECT v.* FROM em_content.knowledge_fact_versions v
 WHERE v.library_id=p_library AND v.group_id IN(SELECT group_id FROM wanted_groups)
 AND EXISTS(SELECT 1 FROM em_content.knowledge_fact_sources s WHERE s.library_id=v.library_id
 AND s.version_id=v.id AND s.active=1 AND s.knowledge_id IN(SELECT knowledge_id FROM scoped_objects)) ORDER BY v.id LIMIT 513)
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
   UNION SELECT u.knowledge_id,d.item_id FROM em_content.unit_derivations d
   JOIN em_content.organization_units u ON u.library_id=d.library_id AND u.task_id=d.unit_task_id AND u.digest=d.unit_digest
   WHERE d.library_id=p_library AND u.knowledge_id IN(SELECT knowledge_id FROM scoped_objects)
   AND em_content.history_member(p_library,p_project,d.item_id)
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
REVOKE ALL ON FUNCTION em_content.history_knowledge_inputs(uuid,uuid,bigint[],text) FROM PUBLIC;

-- Explicit owner-only activation after full projection publication; never called by install.
CREATE FUNCTION em_security.set_history_recall_ready(p_library uuid,p_enabled boolean,p_expected_revision bigint)
RETURNS bigint LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE result bigint;
BEGIN
 IF p_enabled IS NULL OR p_expected_revision IS NULL OR p_expected_revision<1 THEN RAISE EXCEPTION 'invalid_history_readiness_update'; END IF;
 IF p_enabled AND NOT EXISTS(SELECT 1 FROM em_security.history_membership_state m
 JOIN em_content.history_catchup_latest l USING(library_id)
 JOIN em_content.history_catchup_epochs e ON e.library_id=l.library_id AND e.epoch_id=l.epoch_id
 WHERE m.library_id=p_library AND NOT m.dirty AND m.mapping_revision>0
 AND NOT EXISTS(SELECT 1 FROM em_content.history_catchup_epochs pending WHERE pending.library_id=m.library_id AND pending.state<>'complete')
 AND m.membership_hash~'^[0-9a-f]{64}$' AND e.state='complete' AND e.completion_hash=l.completion_hash
 AND m.completion_epoch=e.epoch_id AND m.completion_hash=e.completion_hash)
 THEN RAISE EXCEPTION 'history_projection_not_complete'; END IF;
 UPDATE em_security.history_readiness r SET enabled=p_enabled,revision=r.revision+1,
 mapping_revision=m.mapping_revision,membership_hash=m.membership_hash,completion_epoch=m.completion_epoch,
 completion_hash=m.completion_hash,policy_epoch=s.policy_epoch,route_epoch=s.route_epoch
 FROM em_security.history_membership_state m,em_security.library_state s
 WHERE r.library_id=p_library AND m.library_id=r.library_id AND s.library_id=r.library_id AND r.revision=p_expected_revision
 RETURNING r.revision INTO result;
 IF NOT FOUND THEN RAISE EXCEPTION 'history_readiness_revision_conflict'; END IF;
 RETURN result;
END; $fn$;
REVOKE ALL ON FUNCTION em_security.set_history_recall_ready(uuid,boolean,bigint) FROM PUBLIC;

CREATE FUNCTION em_security.provision_history_reader(p_role name) RETURNS void
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
 EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_layer_read(uuid,uuid,bigint,text) TO %I',p_role);
 EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_knowledge_inputs(uuid,uuid,bigint[],text) TO %I',p_role);
 EXECUTE format('GRANT EXECUTE ON FUNCTION em_content.history_legacy_projection(uuid,uuid,bigint[]) TO %I',p_role);
END; $fn$;
REVOKE ALL ON FUNCTION em_security.provision_history_reader(name) FROM PUBLIC;
DO $readers$ DECLARE r name; BEGIN
 FOR r IN SELECT login_role FROM em_security.principal_bindings ORDER BY login_role LOOP
  PERFORM em_security.provision_history_reader(r);
 END LOOP;
END; $readers$;
COMMIT;

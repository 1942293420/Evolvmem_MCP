-- Historical ANN v1. Additive to applied 005..008; raw history is untouched.
-- Trusted psql variables: database_name, owner_role, library_id.
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
SELECT set_config('evolvmem.history_vector_library',:'library_id',true);
DO $guard$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM em_security.library_state WHERE library_id=current_setting('evolvmem.history_vector_library')::uuid AND kind='legacy')
 OR to_regprocedure('em_content.history_lexical_candidates(uuid,uuid,text,text[],text,double precision,boolean,integer,text,jsonb)') IS NULL
 OR to_regclass('em_content.history_catchup_latest') IS NULL
 OR NOT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector' AND extversion='0.8.6')
 THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='history_vector_install_precondition'; END IF;
END $guard$;

CREATE TABLE em_content.history_vector_schema_migrations(version integer PRIMARY KEY CHECK(version=1),library_id uuid NOT NULL,
 applied_at timestamptz NOT NULL DEFAULT clock_timestamp());
CREATE TABLE em_content.history_vector_models(
 library_id uuid NOT NULL,model_contract text NOT NULL,dimension integer NOT NULL CHECK(dimension=768),
 document_contract_text text NOT NULL,query_prefix text NOT NULL,model_revision text NOT NULL,
 PRIMARY KEY(library_id,model_contract),
 CHECK(model_contract='6431c54de2fa55980f5ac2d688c7db70fd3c0f1fe099435c37927f27ef792bb9'),
 CHECK(document_contract_text='["bge-base-zh-v1.5.f16.gguf@sha256:cbb58f04476cc2e6adf33170f667def5d0e15468ca96f3821b0fee1a454f6c0c",768,"",512]'),
 CHECK(model_contract=encode(sha256(convert_to(document_contract_text,'UTF8')),'hex')),
 CHECK(model_revision='cbb58f04476cc2e6adf33170f667def5d0e15468ca96f3821b0fee1a454f6c0c'),
 CHECK(query_prefix='为这个句子生成表示以用于检索相关文章：')
);
INSERT INTO em_content.history_vector_models VALUES(:'library_id'::uuid,
 '6431c54de2fa55980f5ac2d688c7db70fd3c0f1fe099435c37927f27ef792bb9',768,
 '["bge-base-zh-v1.5.f16.gguf@sha256:cbb58f04476cc2e6adf33170f667def5d0e15468ca96f3821b0fee1a454f6c0c",768,"",512]',
 '为这个句子生成表示以用于检索相关文章：','cbb58f04476cc2e6adf33170f667def5d0e15468ca96f3821b0fee1a454f6c0c');
CREATE TABLE em_content.history_vector_objects(
 library_id uuid NOT NULL,entity_id bigint NOT NULL CHECK(entity_id>0),model_contract text NOT NULL,
 entity_revision bigint NOT NULL CHECK(entity_revision>0),content_hash text NOT NULL CHECK(content_hash~'^[0-9a-f]{64}$'),
 dimension integer NOT NULL DEFAULT 768 CHECK(dimension=768),projection_state text NOT NULL CHECK(projection_state IN('ready','removed')),
 embedding public.vector(768),vector_hash text,updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(library_id,entity_id,model_contract),
 FOREIGN KEY(library_id,model_contract) REFERENCES em_content.history_vector_models(library_id,model_contract),
 CHECK((projection_state='removed' AND embedding IS NULL AND vector_hash IS NULL)
 OR(projection_state='ready' AND embedding IS NOT NULL AND vector_hash IS NOT NULL
 AND public.vector_norm(embedding)>0 AND vector_hash=encode(sha256(public.vector_send(embedding)),'hex')))
) PARTITION BY LIST(library_id);
CREATE TABLE em_content.history_vector_library PARTITION OF em_content.history_vector_objects
 FOR VALUES IN(:'library_id') PARTITION BY LIST(model_contract);
CREATE TABLE em_content.history_vector_current PARTITION OF em_content.history_vector_library
 FOR VALUES IN('6431c54de2fa55980f5ac2d688c7db70fd3c0f1fe099435c37927f27ef792bb9');
CREATE INDEX history_vector_current_hnsw ON em_content.history_vector_current USING hnsw(embedding public.vector_cosine_ops);
CREATE TABLE em_content.history_vector_routes(
 library_id uuid NOT NULL,model_contract text NOT NULL,library_partition oid NOT NULL,model_partition oid NOT NULL,hnsw_index oid NOT NULL,
 PRIMARY KEY(library_id,model_contract),FOREIGN KEY(library_id,model_contract) REFERENCES em_content.history_vector_models(library_id,model_contract));
INSERT INTO em_content.history_vector_routes VALUES(:'library_id'::uuid,
 '6431c54de2fa55980f5ac2d688c7db70fd3c0f1fe099435c37927f27ef792bb9',
 'em_content.history_vector_library'::regclass,'em_content.history_vector_current'::regclass,'em_content.history_vector_current_hnsw'::regclass);
CREATE TABLE em_content.history_vector_state(
 library_id uuid NOT NULL,model_contract text NOT NULL,revision bigint NOT NULL DEFAULT 1 CHECK(revision>0),
 enabled boolean NOT NULL DEFAULT false,coverage text NOT NULL DEFAULT 'pending' CHECK(coverage IN('pending','partial','complete')),
 source_token jsonb,covered_source jsonb,bootstrap_after bigint NOT NULL DEFAULT 0 CHECK(bootstrap_after>=0),bootstrap_complete boolean NOT NULL DEFAULT false,
 delta_epoch uuid,after_ordinal integer NOT NULL DEFAULT -1 CHECK(after_ordinal>=-1),after_change_ordinal integer NOT NULL DEFAULT -1 CHECK(after_change_ordinal>=-1),
 fanout_after_id bigint CHECK(fanout_after_id>=0),delta_complete boolean NOT NULL DEFAULT false,
 indexed_count bigint NOT NULL DEFAULT 0 CHECK(indexed_count>=0),target_count bigint CHECK(target_count>=0),
 PRIMARY KEY(library_id,model_contract),FOREIGN KEY(library_id,model_contract) REFERENCES em_content.history_vector_models(library_id,model_contract));
INSERT INTO em_content.history_vector_state(library_id,model_contract) SELECT library_id,model_contract FROM em_content.history_vector_models;
CREATE TABLE em_content.history_vector_targets(
 library_id uuid NOT NULL,model_contract text NOT NULL,entity_id bigint NOT NULL,entity_revision bigint NOT NULL,content_hash text NOT NULL,
 action text NOT NULL CHECK(action IN('upsert','remove')),
 PRIMARY KEY(library_id,model_contract,entity_id),FOREIGN KEY(library_id,model_contract) REFERENCES em_content.history_vector_models(library_id,model_contract));
CREATE TABLE em_content.history_vector_jobs(
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,library_id uuid NOT NULL,model_contract text NOT NULL,entity_id bigint NOT NULL CHECK(entity_id>0),
 action text NOT NULL CHECK(action IN('upsert','remove')),entity_revision bigint NOT NULL CHECK(entity_revision>0),
 content_hash text NOT NULL CHECK(content_hash~'^[0-9a-f]{64}$'),source_token jsonb NOT NULL,object_token jsonb NOT NULL,
 identity_hash text NOT NULL CHECK(identity_hash~'^[0-9a-f]{64}$'),status text NOT NULL DEFAULT 'pending' CHECK(status IN('pending','completed','stale')),
 result_code text,created_at timestamptz NOT NULL DEFAULT clock_timestamp(),processed_at timestamptz,
 FOREIGN KEY(library_id,model_contract) REFERENCES em_content.history_vector_models(library_id,model_contract),
 UNIQUE(library_id,model_contract,entity_id,identity_hash),
 CHECK((status='pending' AND result_code IS NULL AND processed_at IS NULL)
 OR(status='completed' AND result_code IN('upsert','remove') AND processed_at IS NOT NULL)
 OR(status='stale' AND result_code IN('stale_missing','stale_revision','stale_content','stale_action','stale_source') AND processed_at IS NOT NULL)));
CREATE INDEX history_vector_pending ON em_content.history_vector_jobs(library_id,model_contract,id) WHERE status='pending';
CREATE FUNCTION em_content.history_vector_job_immutable() RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $fn$
BEGIN
 IF OLD.status<>'pending' OR (to_jsonb(NEW)-ARRAY['status','result_code','processed_at']) IS DISTINCT FROM
 (to_jsonb(OLD)-ARRAY['status','result_code','processed_at']) THEN
 RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='immutable_history_vector_job'; END IF; RETURN NEW;
END $fn$;
CREATE TRIGGER history_vector_job_identity BEFORE UPDATE ON em_content.history_vector_jobs
 FOR EACH ROW EXECUTE FUNCTION em_content.history_vector_job_immutable();
REVOKE ALL ON FUNCTION em_content.history_vector_job_immutable() FROM PUBLIC;
DO $private$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['history_vector_schema_migrations','history_vector_models','history_vector_objects','history_vector_library',
 'history_vector_current','history_vector_routes','history_vector_state','history_vector_targets','history_vector_jobs'] LOOP
 EXECUTE format('ALTER TABLE em_content.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE em_content.%I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('CREATE POLICY history_vector_owner ON em_content.%I TO %I USING(true) WITH CHECK(true)',t,current_user);
 EXECUTE format('REVOKE ALL ON em_content.%I FROM PUBLIC',t);
 END LOOP;
END $private$;

-- Fixed nine-row metadata assembly, never an original-row scan.
CREATE FUNCTION em_security.history_vector_generation() RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE names text[]:=ARRAY['history_batch_progress','history_source_batch_progress','history_organization_progress','history_capture_progress',
 'history_governance_progress','history_knowledge_progress','history_capability_progress','history_continuity_progress','history_operations_progress'];
 keys text[]:=ARRAY['core','sources','organization','capture','governance','knowledge','capability','continuity','operations'];
 result jsonb:='{}'; v jsonb; n bigint; k integer;
BEGIN
 FOR k IN 1..9 LOOP
 EXECUTE format('SELECT count(*),jsonb_agg(jsonb_build_object(''run_id'',run_id,''generation'',generation))->0 FROM em_content.%I',names[k]) INTO n,v;
 IF n<>1 OR (v->>'generation')::bigint<0 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_vector_generation_unavailable'; END IF;
 result:=result||jsonb_build_object(keys[k],v);
 END LOOP; RETURN result;
END $fn$;
CREATE FUNCTION em_security.history_vector_source(p_library uuid,p_contract text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE result jsonb; gen jsonb;
BEGIN
 gen:=em_security.history_vector_generation();
 SELECT jsonb_build_object('library_id',c.library_id,'policy_epoch',c.policy_epoch,'route_epoch',c.route_epoch,
 'history_readiness_revision',r.revision,'mapping_revision',m.mapping_revision,'membership_hash',m.membership_hash,
 'completion_epoch',e.epoch_id,'completion_hash',e.completion_hash,'source_seal',e.manifest->>'new_seal',
 'generation',gen,'model_contract',p_contract) INTO result
 FROM em_security.session_context() c JOIN em_security.history_readiness r USING(library_id)
 JOIN em_security.history_membership_state m USING(library_id) JOIN em_content.history_catchup_latest l USING(library_id)
 JOIN em_content.history_catchup_epochs e ON e.library_id=l.library_id AND e.epoch_id=l.epoch_id
 WHERE c.library_id=p_library AND c.authority_state='pg_active' AND c.acl_state='ready'
 AND r.enabled AND NOT m.dirty AND r.mode='history-preserved-v1' AND r.schema_version=1
 AND r.policy_epoch=c.policy_epoch AND r.route_epoch=c.route_epoch AND m.mapping_revision>0
 AND r.mapping_revision=m.mapping_revision AND r.membership_hash=m.membership_hash AND m.membership_hash~'^[0-9a-f]{64}$'
 AND r.completion_epoch=e.epoch_id AND m.completion_epoch=e.epoch_id
 AND r.completion_hash=e.completion_hash AND m.completion_hash=e.completion_hash AND l.completion_hash=e.completion_hash
 AND e.state='complete' AND e.generation=gen AND l.generation=gen AND e.manifest->>'new_seal'~'^[0-9a-f]{64}$'
 AND NOT EXISTS(SELECT 1 FROM em_content.history_catchup_epochs x WHERE x.library_id=p_library AND x.state<>'complete');
 IF result IS NULL THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_vector_source_unavailable'; END IF;
 RETURN result;
END $fn$;
CREATE FUNCTION em_security.history_vector_model(p_library uuid,p_contract text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE r em_content.history_vector_routes; result jsonb;
BEGIN
 SELECT * INTO r FROM em_content.history_vector_routes WHERE library_id=p_library AND model_contract=p_contract;
 IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM em_content.history_vector_schema_migrations WHERE library_id=p_library AND version=1)
 OR NOT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector' AND extversion='0.8.6')
 OR NOT EXISTS(SELECT 1 FROM pg_inherits WHERE inhrelid=r.library_partition AND inhparent='em_content.history_vector_objects'::regclass)
 OR NOT EXISTS(SELECT 1 FROM pg_class WHERE oid=r.library_partition AND pg_get_expr(relpartbound,oid)=format('FOR VALUES IN (%L)',p_library::text))
 OR NOT EXISTS(SELECT 1 FROM pg_inherits WHERE inhrelid=r.model_partition AND inhparent=r.library_partition)
 OR NOT EXISTS(SELECT 1 FROM pg_class WHERE oid=r.model_partition AND pg_get_expr(relpartbound,oid)=format('FOR VALUES IN (%L)',p_contract))
 OR NOT EXISTS(SELECT 1 FROM pg_index i JOIN pg_class ix ON ix.oid=i.indexrelid JOIN pg_am a ON a.oid=ix.relam
 JOIN pg_opclass op ON op.oid=i.indclass[0] WHERE i.indexrelid=r.hnsw_index AND i.indrelid=r.model_partition
 AND i.indisvalid AND i.indisready AND a.amname='hnsw' AND op.opcname='vector_cosine_ops')
 OR (SELECT count(*)=9 AND bool_and(relrowsecurity AND relforcerowsecurity) FROM pg_class
 WHERE oid IN('em_content.history_vector_schema_migrations'::regclass,'em_content.history_vector_models'::regclass,
 'em_content.history_vector_objects'::regclass,'em_content.history_vector_routes'::regclass,'em_content.history_vector_state'::regclass,
 'em_content.history_vector_targets'::regclass,'em_content.history_vector_jobs'::regclass,r.library_partition,r.model_partition)) IS DISTINCT FROM true
 THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='history_vector_model_unavailable'; END IF;
 SELECT jsonb_build_object('runtime_version','history-vector.v1','model_contract',m.model_contract,'dimension',m.dimension,
 'document_contract_text',m.document_contract_text,'query_prefix',m.query_prefix,'model_revision',m.model_revision) INTO result
 FROM em_content.history_vector_models m WHERE m.library_id=p_library AND m.model_contract=p_contract;
 IF result IS NULL THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='history_vector_model_unavailable'; END IF;
 RETURN result;
END $fn$;
CREATE FUNCTION em_security.history_vector_ann(p_library uuid,p_contract text,p_source jsonb) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE s em_content.history_vector_state; ready boolean; cov text;
BEGIN
 SELECT * INTO s FROM em_content.history_vector_state WHERE library_id=p_library AND model_contract=p_contract;
 IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='history_vector_state_missing'; END IF;
 ready:=s.enabled AND s.coverage='complete' AND s.covered_source=p_source AND s.indexed_count=s.target_count;
 cov:=CASE WHEN ready THEN 'complete' WHEN s.source_token IS NULL THEN 'pending' ELSE 'partial' END;
 RETURN jsonb_build_object('revision',s.revision,'enabled',ready,'coverage',cov,'indexed_count',s.indexed_count,
 'target_count',s.target_count,'reason',CASE WHEN ready THEN 'ann_ready' WHEN cov='pending' THEN 'ann_not_built' ELSE 'ann_coverage_partial' END);
END $fn$;
CREATE FUNCTION em_security.history_vector_context(p_library uuid,p_project uuid,p_contract text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE source jsonb; model jsonb;
BEGIN
 PERFORM em_security.require_history_scope(p_library,p_project);
 model:=em_security.history_vector_model(p_library,p_contract);source:=em_security.history_vector_source(p_library,p_contract);
 RETURN jsonb_build_object('model',model,'source',source,'ann',em_security.history_vector_ann(p_library,p_contract,source));
END $fn$;
CREATE FUNCTION em_security.history_vector_index_context(p_library uuid,p_contract text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE source jsonb; model jsonb;
BEGIN
 IF current_setting('transaction_isolation')<>'repeatable read' OR current_setting('transaction_read_only')<>'off'
 OR NOT EXISTS(SELECT 1 FROM em_security.session_context() c JOIN em_security.library_state s USING(library_id)
 WHERE c.library_id=p_library AND s.kind='legacy' AND c.principal_id=s.owner_principal_id AND c.permission='admin'
 AND c.authority_state='pg_active' AND c.acl_state='ready')
 OR (SELECT count(*) FROM em_security.session_credential())<>1 OR NOT em_security._runtime_role_safe(session_user::name)
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_vector_owner_maintenance_required'; END IF;
 model:=em_security.history_vector_model(p_library,p_contract);source:=em_security.history_vector_source(p_library,p_contract);
 RETURN jsonb_build_object('model',model,'source',source);
END $fn$;

CREATE FUNCTION em_content.history_vector_object_token(p_library uuid,p_contract text,p_id bigint) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE i em_content.context_items; l em_content.context_layers; memberships jsonb; eligible boolean;
BEGIN
 SELECT * INTO i FROM em_content.context_items WHERE library_id=p_library AND id=p_id;
 IF NOT FOUND THEN RETURN jsonb_build_object('missing',true); END IF;
 SELECT * INTO l FROM em_content.context_layers WHERE library_id=p_library AND item_id=p_id AND layer='l0';
 IF FOUND AND l.content_hash<>encode(sha256(convert_to(l.content,'UTF8')),'hex')
 THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='context_layer_proof_invalid'; END IF;
 SELECT COALESCE(jsonb_agg(to_jsonb(m)-'mapping_revision' ORDER BY m.project_id),'[]') INTO memberships
 FROM em_content.history_read_memberships m JOIN em_security.history_membership_state s USING(library_id)
 JOIN em_content.context_project_registry r ON r.library_id=m.library_id AND r.project=m.project_key AND r.project_uuid=m.registry_project_uuid
 JOIN em_security.project_projection p ON p.project_id=m.project_id AND p.project_key=m.project_key
 WHERE m.library_id=p_library AND m.item_id=p_id AND m.state='confirmed' AND m.mapping_revision=s.mapping_revision AND r.status='active' AND p.active;
 eligible:=l.content IS NOT NULL AND l.content<>'' AND i.content_type IN('decision','fact','constraint','preference','user_profile','reference','session_summary',
 'experience','playbook','workflow_policy','project_summary') AND(i.status='active' OR(i.status='superseded' AND i.content_type='decision'
 AND(i.effective_from IS NOT NULL OR i.occurred_at IS NOT NULL)))
 AND(i.expires_at IS NULL OR i.expires_at>to_char(statement_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS'))
 AND(jsonb_array_length(memberships)>0 OR em_content.history_member(p_library,NULL,p_id));
 RETURN jsonb_build_object('missing',false,'revision',i.revision,'content_hash',l.content_hash,
 'item_hash',encode(sha256(convert_to(to_jsonb(i)::text,'UTF8')),'hex'),'memberships',memberships,'eligible',eligible,'model_contract',p_contract);
END $fn$;
CREATE FUNCTION em_content.history_vector_make_document(p_library uuid,p_project uuid,p_id bigint) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE i em_content.context_items; l em_content.context_layers;
BEGIN
 SELECT * INTO i FROM em_content.context_items WHERE library_id=p_library AND id=p_id;
 IF NOT FOUND THEN RETURN NULL; END IF;
 SELECT * INTO l FROM em_content.context_layers WHERE library_id=p_library AND item_id=p_id AND layer='l0';
 IF NOT FOUND THEN RETURN NULL; END IF;
 IF l.content_hash<>encode(sha256(convert_to(l.content,'UTF8')),'hex')
 THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='context_layer_proof_invalid'; END IF;
 IF length(l.content)>32768 OR octet_length(l.content)>8388608
 THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='history_vector_document_limit'; END IF;
 RETURN jsonb_build_object('library_id',p_library,'index_kind','context','entity_id',i.id,'revision',i.revision,'text',l.content,
 'status',i.status,'expires_at',i.expires_at,'content_type',i.content_type,'source_state',i.source_state,'projects',i.projects,
 'personal_general',i.personal_general,'has_experience_payload',i.experience_payload IS NOT NULL AND i.experience_payload<>'{}'::jsonb,
 'unsupported_sources',0,'project_id',p_project);
END $fn$;
CREATE FUNCTION em_content.history_vector_job_json(p_job em_content.history_vector_jobs) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
 SELECT jsonb_build_object('id',p_job.id,'library_id',p_job.library_id,'index_kind','context','entity_id',p_job.entity_id,'action',p_job.action,
 'revision',p_job.entity_revision,'content_hash',p_job.content_hash,'model_contract',p_job.model_contract,'project_id',NULL)
$fn$;
CREATE FUNCTION em_content.history_vector_next_job(p_library uuid,p_contract text) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE j em_content.history_vector_jobs;
BEGIN
 PERFORM em_security.history_vector_index_context(p_library,p_contract);
 SELECT * INTO j FROM em_content.history_vector_jobs WHERE library_id=p_library AND model_contract=p_contract AND status='pending' ORDER BY id LIMIT 1;
 RETURN CASE WHEN FOUND THEN em_content.history_vector_job_json(j) ELSE NULL END;
END $fn$;
CREATE FUNCTION em_content.history_vector_document(p_library uuid,p_contract text,p_job_id bigint) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE ctx jsonb; j em_content.history_vector_jobs; tok jsonb;
BEGIN
 ctx:=em_security.history_vector_index_context(p_library,p_contract);
 SELECT * INTO j FROM em_content.history_vector_jobs WHERE library_id=p_library AND model_contract=p_contract AND id=p_job_id AND status='pending';
 IF NOT FOUND OR j.source_token IS DISTINCT FROM ctx->'source' THEN RETURN NULL; END IF;
 tok:=em_content.history_vector_object_token(p_library,p_contract,j.entity_id);
 IF tok IS DISTINCT FROM j.object_token OR tok->>'missing'='true' THEN RETURN NULL; END IF;
 RETURN em_content.history_vector_make_document(p_library,NULL,j.entity_id);
END $fn$;
CREATE FUNCTION em_content.history_vector_documents(p_library uuid,p_project uuid,p_contract text,p_ids bigint[])
RETURNS TABLE(id bigint,document jsonb) LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
BEGIN
 PERFORM em_security.history_vector_context(p_library,p_project,p_contract);
 IF p_ids IS NULL OR cardinality(p_ids)>240 OR cardinality(p_ids)<>(SELECT count(DISTINCT n) FROM unnest(p_ids)n)
 OR EXISTS(SELECT 1 FROM unnest(p_ids)n WHERE n IS NULL OR n<1)
 THEN RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid_history_vector_ids'; END IF;
 RETURN QUERY SELECT i.id,em_content.history_vector_make_document(p_library,p_project,i.id)
 FROM em_content.context_items i WHERE i.library_id=p_library AND i.id=ANY(p_ids)
 AND em_content.history_member(p_library,p_project,i.id) ORDER BY i.id;
END $fn$;
CREATE FUNCTION em_content.history_vector_cache_entry(p_library uuid,p_contract text,p_job_id bigint) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE doc jsonb; result jsonb;
BEGIN
 doc:=em_content.history_vector_document(p_library,p_contract,p_job_id);IF doc IS NULL THEN RETURN NULL; END IF;
 SELECT jsonb_build_object('values',v.embedding::text::jsonb,'document_contract',m.document_contract_text::jsonb,
 'content_hash',v.content_hash,'vector_hash',v.vector_hash) INTO result
 FROM em_content.history_vector_objects v JOIN em_content.history_vector_models m USING(library_id,model_contract)
 WHERE v.library_id=p_library AND v.model_contract=p_contract AND v.entity_id=(doc->>'entity_id')::bigint AND v.projection_state='ready'
 AND v.content_hash=encode(sha256(convert_to(doc->>'text','UTF8')),'hex')
 AND v.vector_hash=encode(sha256(public.vector_send(v.embedding)),'hex') AND public.vector_dims(v.embedding)=768 AND public.vector_norm(v.embedding)>0;
 RETURN result;
END $fn$;

-- Enqueue is private and only called from bounded durable frontier operations.
CREATE FUNCTION em_content.history_vector_queue(p_library uuid,p_contract text,p_id bigint,p_source jsonb) RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE tok jsonb; v em_content.history_vector_objects; rev bigint; hash text; act text; ident text; n integer;
BEGIN
 tok:=em_content.history_vector_object_token(p_library,p_contract,p_id);
 SELECT * INTO v FROM em_content.history_vector_objects WHERE library_id=p_library AND model_contract=p_contract AND entity_id=p_id;
 IF tok->>'eligible'='true' THEN
 PERFORM em_content.history_vector_make_document(p_library,NULL,p_id);
 rev:=(tok->>'revision')::bigint;hash:=tok->>'content_hash';act:='upsert';
 INSERT INTO em_content.history_vector_targets VALUES(p_library,p_contract,p_id,rev,hash,'upsert')
 ON CONFLICT(library_id,model_contract,entity_id) DO UPDATE SET entity_revision=excluded.entity_revision,content_hash=excluded.content_hash,action=excluded.action;
 IF v.projection_state='ready' AND v.entity_revision=rev AND v.content_hash=hash
 AND v.vector_hash=encode(sha256(public.vector_send(v.embedding)),'hex') THEN RETURN 0; END IF;
 ELSE
 IF v.entity_id IS NULL THEN
 DELETE FROM em_content.history_vector_targets WHERE library_id=p_library AND model_contract=p_contract AND entity_id=p_id;RETURN 0; END IF;
 rev:=COALESCE((tok->>'revision')::bigint,v.entity_revision);hash:=COALESCE(tok->>'content_hash',v.content_hash);act:='remove';
 INSERT INTO em_content.history_vector_targets VALUES(p_library,p_contract,p_id,rev,hash,'remove')
 ON CONFLICT(library_id,model_contract,entity_id) DO UPDATE SET entity_revision=excluded.entity_revision,content_hash=excluded.content_hash,action=excluded.action;
 IF v.projection_state='removed' AND v.entity_revision=rev AND v.content_hash=hash THEN RETURN 0; END IF;
 END IF;
 ident:=encode(sha256(convert_to(jsonb_build_array(p_source,tok,rev,hash,act)::text,'UTF8')),'hex');
 INSERT INTO em_content.history_vector_jobs(library_id,model_contract,entity_id,action,entity_revision,content_hash,source_token,object_token,identity_hash)
 VALUES(p_library,p_contract,p_id,act,rev,hash,p_source,tok,ident) ON CONFLICT DO NOTHING;
 GET DIAGNOSTICS n=ROW_COUNT;RETURN n;
END $fn$;
CREATE FUNCTION em_content.history_vector_enqueue_page(p_library uuid,p_contract text,p_after_id bigint,p_limit integer) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE ctx jsonb; s em_content.history_vector_state; ident bigint; last_id bigint; scanned integer:=0; queued integer:=0; done boolean;
BEGIN
 ctx:=em_security.history_vector_index_context(p_library,p_contract);
 IF p_after_id IS NULL OR p_after_id<0 OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 64
 THEN RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid_history_vector_page'; END IF;
 SELECT * INTO s FROM em_content.history_vector_state WHERE library_id=p_library AND model_contract=p_contract FOR UPDATE;
 IF s.covered_source IS NOT NULL OR s.bootstrap_complete OR s.bootstrap_after<>p_after_id
 OR(s.source_token IS NOT NULL AND s.source_token IS DISTINCT FROM ctx->'source')
 THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='history_vector_bootstrap_conflict'; END IF;
 last_id:=p_after_id;
 FOR ident IN SELECT i.id FROM em_content.context_items i WHERE i.library_id=p_library AND i.id>p_after_id ORDER BY i.id LIMIT p_limit LOOP
 scanned:=scanned+1;last_id:=ident;queued:=queued+em_content.history_vector_queue(p_library,p_contract,ident,ctx->'source');
 END LOOP;
 done:=NOT EXISTS(SELECT 1 FROM em_content.context_items i WHERE i.library_id=p_library AND i.id>last_id LIMIT 1);
 UPDATE em_content.history_vector_state SET source_token=ctx->'source',bootstrap_after=last_id,bootstrap_complete=done,
 coverage='partial',enabled=false,revision=revision+CASE WHEN source_token IS NULL THEN 1 ELSE 0 END
 WHERE library_id=p_library AND model_contract=p_contract;
 RETURN jsonb_build_object('index_kind','context','after_id',last_id,'scanned',scanned,'enqueued',queued,'complete',done);
END $fn$;

-- Apply holds the exact metadata fence and current item/membership rows until
-- its projection + immutable-job acknowledgement commit together.
CREATE FUNCTION em_content.history_vector_lock_source(p_library uuid,p_contract text) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE t text;
BEGIN
 PERFORM 1 FROM em_security.library_state WHERE library_id=p_library FOR SHARE;
 PERFORM 1 FROM em_security.history_readiness WHERE library_id=p_library FOR SHARE;
 PERFORM 1 FROM em_security.history_membership_state WHERE library_id=p_library FOR SHARE;
 PERFORM 1 FROM em_content.history_catchup_latest WHERE library_id=p_library FOR SHARE;
 PERFORM 1 FROM em_content.history_catchup_epochs WHERE library_id=p_library
 AND epoch_id=(SELECT epoch_id FROM em_content.history_catchup_latest WHERE library_id=p_library) FOR SHARE;
 PERFORM 1 FROM em_content.history_vector_models WHERE library_id=p_library AND model_contract=p_contract FOR SHARE;
 FOREACH t IN ARRAY ARRAY['history_batch_progress','history_source_batch_progress','history_organization_progress','history_capture_progress',
 'history_governance_progress','history_knowledge_progress','history_capability_progress','history_continuity_progress','history_operations_progress'] LOOP
 EXECUTE format('SELECT 1 FROM em_content.%I FOR SHARE',t);
 END LOOP;
END $fn$;
CREATE FUNCTION em_content.history_vector_apply(p_library uuid,p_contract text,p_job_id bigint,p_job jsonb,p_embedding public.vector,p_vector_hash text) RETURNS text
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE ctx jsonb; j em_content.history_vector_jobs; tok jsonb; outcome text; n integer; expiry_recovery boolean:=false;
BEGIN
 PERFORM em_security.history_vector_index_context(p_library,p_contract);
 PERFORM em_content.history_vector_lock_source(p_library,p_contract);
 ctx:=em_security.history_vector_index_context(p_library,p_contract);
 SELECT * INTO j FROM em_content.history_vector_jobs WHERE library_id=p_library AND model_contract=p_contract AND id=p_job_id
 AND status='pending' FOR UPDATE SKIP LOCKED;
 IF NOT FOUND THEN RETURN 'already_claimed_or_processed'; END IF;
 IF p_job IS DISTINCT FROM em_content.history_vector_job_json(j)
 THEN RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='prepared_job_changed'; END IF;
 IF j.source_token IS DISTINCT FROM ctx->'source' THEN outcome:='stale_source';
 ELSE
 PERFORM 1 FROM em_content.context_items WHERE library_id=p_library AND id=j.entity_id FOR SHARE;
 PERFORM 1 FROM em_content.context_layers WHERE library_id=p_library AND item_id=j.entity_id AND layer='l0' FOR SHARE;
 PERFORM 1 FROM em_content.history_read_memberships WHERE library_id=p_library AND item_id=j.entity_id FOR SHARE;
 tok:=em_content.history_vector_object_token(p_library,p_contract,j.entity_id);
 IF tok IS DISTINCT FROM j.object_token THEN
 -- A queued item can cross its unchanged expires_at without any source write.
 -- Recover only that exact race: every source and per-item proof other than
 -- the time-dependent eligibility bit must still match under the held locks.
 IF j.action='upsert' AND j.object_token->>'eligible'='true' AND tok->>'eligible'='false'
 AND(tok-'eligible')=(j.object_token-'eligible')
 AND EXISTS(SELECT 1 FROM em_content.context_items i WHERE i.library_id=p_library AND i.id=j.entity_id
 AND i.expires_at IS NOT NULL AND i.expires_at<=to_char(statement_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD HH24:MI:SS')) THEN
 outcome:='stale_action';expiry_recovery:=true;
 ELSE
 outcome:=CASE WHEN tok->>'missing'='true' THEN 'stale_missing' WHEN tok->'revision' IS DISTINCT FROM j.object_token->'revision' THEN 'stale_revision'
 WHEN tok->'content_hash' IS DISTINCT FROM j.object_token->'content_hash' THEN 'stale_content' ELSE 'stale_source' END;
 END IF;
 ELSIF j.action='upsert' AND tok->>'eligible'<>'true' THEN outcome:='stale_action';
 ELSE outcome:=j.action;END IF;
 END IF;
 IF outcome='upsert' THEN
 IF p_embedding IS NULL OR p_vector_hash IS NULL OR public.vector_dims(p_embedding)<>768 OR public.vector_norm(p_embedding)<=0
 OR p_vector_hash<>encode(sha256(public.vector_send(p_embedding)),'hex')
 THEN RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='prepared_embedding_invalid'; END IF;
 ELSIF outcome='remove' THEN
 IF p_embedding IS NOT NULL OR p_vector_hash IS NOT NULL
 THEN RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='nonupsert_embedding_refused'; END IF;
 END IF;
 IF outcome IN('upsert','remove') THEN
 INSERT INTO em_content.history_vector_objects(library_id,entity_id,model_contract,entity_revision,content_hash,projection_state,embedding,vector_hash)
 VALUES(p_library,j.entity_id,p_contract,j.entity_revision,j.content_hash,CASE WHEN outcome='upsert' THEN 'ready' ELSE 'removed' END,p_embedding,p_vector_hash)
 ON CONFLICT(library_id,entity_id,model_contract) DO UPDATE SET entity_revision=excluded.entity_revision,content_hash=excluded.content_hash,
 projection_state=excluded.projection_state,embedding=excluded.embedding,vector_hash=excluded.vector_hash,updated_at=clock_timestamp()
 WHERE history_vector_objects.entity_revision<=excluded.entity_revision
 AND(history_vector_objects.entity_revision<excluded.entity_revision OR history_vector_objects.content_hash=excluded.content_hash);
 GET DIAGNOSTICS n=ROW_COUNT;
 IF n<>1 THEN outcome:='stale_revision'; END IF;
 END IF;
 IF expiry_recovery THEN
 -- The same bounded queue operation cancels a target with no numeric object,
 -- or persists a NEW immutable remove job and desired tombstone for an old
 -- numeric object. The stale upsert is never repurposed or left as required
 -- ready coverage. Queue transition and acknowledgement commit atomically.
 PERFORM em_content.history_vector_queue(p_library,p_contract,j.entity_id,ctx->'source');
 END IF;
 UPDATE em_content.history_vector_jobs SET status=CASE WHEN outcome IN('upsert','remove') THEN 'completed' ELSE 'stale' END,
 result_code=outcome,processed_at=clock_timestamp() WHERE id=j.id;
 RETURN outcome;
END $fn$;

CREATE FUNCTION em_content.history_vector_candidates(p_library uuid,p_project uuid,p_contract text,p_query public.vector,
 p_limit integer,p_types text[],p_moment text,p_confidence double precision,p_include_reference boolean,
 p_experience_project text,p_experience_constraints jsonb)
RETURNS TABLE(entity_id bigint,entity_revision bigint,content_hash text,distance double precision)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE ctx jsonb;
BEGIN
 ctx:=em_security.history_vector_context(p_library,p_project,p_contract);
 IF p_query IS NULL OR public.vector_dims(p_query)<>768 OR public.vector_norm(p_query)<=0
 OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 240 OR p_types IS NULL OR cardinality(p_types)>16
 OR p_moment IS NULL OR length(p_moment)>40 OR p_confidence IS NULL OR NOT(p_confidence BETWEEN 0 AND 1) OR p_include_reference IS NULL
 OR EXISTS(SELECT 1 FROM unnest(p_types)t WHERE t IS NULL OR t NOT IN('decision','fact','constraint','preference','user_profile','reference',
 'session_summary','experience','playbook','workflow_policy','project_summary','workstream_checkpoint'))
 THEN RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid_history_vector_candidates'; END IF;
 IF (p_experience_project IS NULL)<>(p_experience_constraints IS NULL)
 OR(p_experience_project IS NOT NULL AND(length(p_experience_project)>256 OR jsonb_typeof(p_experience_constraints)<>'object'
 OR(SELECT count(*) FROM jsonb_each(CASE WHEN jsonb_typeof(p_experience_constraints)='object' THEN p_experience_constraints ELSE '{}' END))>20
 OR EXISTS(SELECT 1 FROM jsonb_each(CASE WHEN jsonb_typeof(p_experience_constraints)='object' THEN p_experience_constraints ELSE '{}' END)c
 WHERE jsonb_typeof(c.value)<>'string')))
 THEN RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid_history_experience_candidates'; END IF;
 IF ctx->'ann'->>'enabled'<>'true' THEN RETURN; END IF;
 PERFORM set_config('hnsw.ef_search',greatest(40,p_limit)::text,true),set_config('hnsw.iterative_scan','strict_order',true),
 set_config('hnsw.max_scan_tuples','2000',true),set_config('hnsw.scan_mem_multiplier','1',true),set_config('work_mem','4MB',true);
 RETURN QUERY SELECT v.entity_id,v.entity_revision,v.content_hash,v.embedding OPERATOR(public.<=>) p_query
 FROM em_content.history_vector_objects v WHERE v.library_id=p_library AND v.model_contract=p_contract AND v.projection_state='ready'
 AND EXISTS(SELECT 1 FROM em_content.context_items c JOIN em_content.context_layers l ON l.library_id=c.library_id AND l.item_id=c.id AND l.layer='l0'
 WHERE c.library_id=v.library_id AND c.id=v.entity_id AND c.revision=v.entity_revision AND l.content_hash=v.content_hash
 AND l.content<>'' AND l.content_hash=encode(sha256(convert_to(l.content,'UTF8')),'hex') AND em_content.history_member(p_library,p_project,c.id)
 AND c.content_type<>'workstream_checkpoint' AND c.status IN('active','superseded')
 AND(c.status='active' OR(c.content_type='decision' AND(c.effective_from IS NOT NULL OR c.occurred_at IS NOT NULL)))
 AND(cardinality(p_types)=0 OR c.content_type=ANY(p_types)) AND c.confidence>=p_confidence
 AND(p_include_reference OR c.tier<>'reference') AND(c.expires_at IS NULL OR c.expires_at>p_moment)
 AND(c.effective_from IS NULL OR c.effective_from<=p_moment) AND(c.effective_until IS NULL OR c.effective_until>p_moment)
 AND(p_experience_project IS NULL OR(c.status='active' AND jsonb_typeof(c.experience_payload)='object' AND c.experience_payload<>'{}'
 AND jsonb_typeof(c.experience_payload->'conditions')='object'
 AND(c.project=p_experience_project OR c.experience_payload->'transferable'='true')
 AND NOT EXISTS(SELECT 1 FROM jsonb_each(CASE WHEN jsonb_typeof(c.experience_payload->'conditions')='object'
 THEN c.experience_payload->'conditions' ELSE '{}' END)e WHERE p_experience_constraints?e.key AND p_experience_constraints->e.key IS DISTINCT FROM e.value))))
 ORDER BY v.embedding OPERATOR(public.<=>) p_query LIMIT p_limit;
END $fn$;
CREATE FUNCTION em_content.history_vector_projection_proof(p_library uuid,p_project uuid,p_contract text,p_ids bigint[],p_query public.vector) RETURNS jsonb
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE ctx jsonb; rows jsonb;
BEGIN
 ctx:=em_security.history_vector_context(p_library,p_project,p_contract);
 IF p_query IS NULL OR public.vector_dims(p_query)<>768 OR public.vector_norm(p_query)<=0 OR p_ids IS NULL OR cardinality(p_ids)>240
 OR cardinality(p_ids)<>(SELECT count(DISTINCT n) FROM unnest(p_ids)n) OR EXISTS(SELECT 1 FROM unnest(p_ids)n WHERE n IS NULL OR n<1)
 THEN RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid_history_vector_proof'; END IF;
 SELECT COALESCE(jsonb_agg(jsonb_build_object('entity_id',v.entity_id,'revision',v.entity_revision,'content_hash',v.content_hash,'dimension',v.dimension,
 'vector_hash',v.vector_hash,'distance',v.embedding OPERATOR(public.<=>) p_query) ORDER BY v.entity_id),'[]') INTO rows
 FROM em_content.history_vector_objects v JOIN em_content.context_items i ON i.library_id=v.library_id AND i.id=v.entity_id
 JOIN em_content.context_layers l ON l.library_id=i.library_id AND l.item_id=i.id AND l.layer='l0'
 WHERE v.library_id=p_library AND v.model_contract=p_contract AND v.entity_id=ANY(p_ids) AND v.projection_state='ready'
 AND i.revision=v.entity_revision AND l.content_hash=v.content_hash AND l.content_hash=encode(sha256(convert_to(l.content,'UTF8')),'hex')
 AND em_content.history_member(p_library,p_project,i.id) AND v.vector_hash=encode(sha256(public.vector_send(v.embedding)),'hex');
 IF jsonb_array_length(rows)<>cardinality(p_ids) THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='recall_vector_changed'; END IF;
 RETURN jsonb_build_object('context',ctx,'query_vector_hash',encode(sha256(public.vector_send(p_query)),'hex'),'rows',rows);
END $fn$;

CREATE FUNCTION em_content.history_vector_delta_page(p_library uuid,p_contract text,p_epoch uuid,p_after_ordinal integer,p_after_change_ordinal integer,p_limit integer) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE ctx jsonb; s em_content.history_vector_state; prev jsonb; entry record; ident bigint; ids bigint[]; keys text[];
 scanned integer:=0;queued integer:=0;cursor_o integer;cursor_c integer;fan bigint;more boolean;done boolean:=false;
BEGIN
 ctx:=em_security.history_vector_index_context(p_library,p_contract);
 IF p_epoch IS NULL OR p_after_ordinal IS NULL OR p_after_change_ordinal IS NULL OR p_after_ordinal< -1 OR p_after_change_ordinal< -1
 OR p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 64 THEN RAISE EXCEPTION USING ERRCODE='22023',MESSAGE='invalid_history_vector_delta'; END IF;
 SELECT * INTO s FROM em_content.history_vector_state WHERE library_id=p_library AND model_contract=p_contract FOR UPDATE;
 IF s.covered_source IS NULL OR p_epoch IS DISTINCT FROM (ctx->'source'->>'completion_epoch')::uuid
 THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='history_vector_delta_anchor_required'; END IF;
 -- A005 has no generated-membership difference journal. This first slice
 -- refuses an automatic rebase when its membership digest changed: the raw
 -- journal alone cannot prove that every new/removed membership is explained.
 -- Retain numerical objects for a separately controlled, cache-reusing rebuild.
 IF s.covered_source->>'membership_hash' IS DISTINCT FROM ctx->'source'->>'membership_hash'
 THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='history_vector_membership_reconciliation_required'; END IF;
 -- ACL/readiness-only refresh at the SAME completed C has no new raw ledger.
 -- The complete current index gate already verified it. Compare every other
 -- SourceToken field exactly; mapping/generation/model/source changes cannot
 -- use this branch or manufacture an empty-journal coverage receipt.
 IF s.covered_source IS DISTINCT FROM ctx->'source'
 AND (s.covered_source-ARRAY['policy_epoch','route_epoch','history_readiness_revision'])=
 ((ctx->'source')-ARRAY['policy_epoch','route_epoch','history_readiness_revision']) THEN
 IF p_after_ordinal<> -1 OR p_after_change_ordinal<> -1 OR s.source_token=ctx->'source'
 THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='history_vector_delta_cursor_conflict'; END IF;
 UPDATE em_content.history_vector_state SET source_token=ctx->'source',delta_epoch=p_epoch,after_ordinal= -1,after_change_ordinal= -1,
 fanout_after_id=NULL,delta_complete=true,enabled=false,coverage='partial',revision=revision+1
 WHERE library_id=p_library AND model_contract=p_contract;
 RETURN jsonb_build_object('epoch_id',p_epoch,'after_ordinal',-1,'after_change_ordinal',-1,'fanout_after_id',NULL,
 'scanned',0,'enqueued',0,'complete',true);
 END IF;
 IF s.delta_epoch IS DISTINCT FROM p_epoch THEN
 SELECT manifest->'previous_proof' INTO prev FROM em_content.history_catchup_epochs WHERE epoch_id=p_epoch AND library_id=p_library;
 IF p_after_ordinal<> -1 OR p_after_change_ordinal<> -1 OR prev->>'kind'<>'epoch'
 OR prev->>'epoch_id' IS DISTINCT FROM s.covered_source->>'completion_epoch'
 OR prev->>'completion_hash' IS DISTINCT FROM s.covered_source->>'completion_hash'
 OR prev->'generation' IS DISTINCT FROM s.covered_source->'generation'
 THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='history_vector_delta_chain_gap'; END IF;
 UPDATE em_content.history_vector_state SET source_token=ctx->'source',delta_epoch=p_epoch,after_ordinal= -1,after_change_ordinal= -1,
 fanout_after_id=NULL,delta_complete=false,enabled=false,coverage='partial',revision=revision+1
 WHERE library_id=p_library AND model_contract=p_contract RETURNING * INTO s;
 END IF;
 IF s.source_token IS DISTINCT FROM ctx->'source' OR s.after_ordinal<>p_after_ordinal OR s.after_change_ordinal<>p_after_change_ordinal OR s.delta_complete
 THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='history_vector_delta_cursor_conflict'; END IF;
 cursor_o:=s.after_ordinal;cursor_c:=s.after_change_ordinal;fan:=s.fanout_after_id;
 -- At most p_limit journal records OR p_limit fanout IDs per call.
 FOR entry IN SELECT * FROM em_content.history_catchup_changes c WHERE c.epoch_id=p_epoch
 AND(c.ordinal,c.change_ordinal)>(cursor_o,cursor_c) ORDER BY c.ordinal,c.change_ordinal LIMIT p_limit LOOP
 IF entry.table_code='context_project_registry' THEN
 keys:=ARRAY[entry.before_row->>'project',entry.after_row->>'project'];
 FOR ident IN SELECT x.id FROM(
 SELECT m.item_id AS id FROM em_content.history_read_memberships m WHERE m.library_id=p_library AND m.project_key=ANY(keys)
 UNION SELECT i.id FROM em_content.context_items i WHERE i.library_id=p_library
 AND(i.project=ANY(keys) OR EXISTS(SELECT 1 FROM unnest(keys)k WHERE i.projects? k)))x
 WHERE x.id>COALESCE(fan,0) ORDER BY x.id LIMIT p_limit-scanned LOOP
 scanned:=scanned+1;fan:=ident;queued:=queued+em_content.history_vector_queue(p_library,p_contract,ident,ctx->'source');
 END LOOP;
 SELECT EXISTS(SELECT 1 FROM(
 SELECT m.item_id AS id FROM em_content.history_read_memberships m WHERE m.library_id=p_library AND m.project_key=ANY(keys)
 UNION SELECT i.id FROM em_content.context_items i WHERE i.library_id=p_library AND(i.project=ANY(keys)
 OR EXISTS(SELECT 1 FROM unnest(keys)k WHERE i.projects? k)))x WHERE x.id>COALESCE(fan,0) LIMIT 1) INTO more;
 IF more THEN EXIT; END IF;
 fan:=NULL;
 ELSE
 IF entry.table_code='context_items' THEN ids:=ARRAY[(entry.before_row->>'id')::bigint,(entry.after_row->>'id')::bigint];
 ELSIF entry.table_code IN('context_layers','context_project_resolutions','knowledge_objects') THEN
 ids:=ARRAY[(entry.before_row->>'item_id')::bigint,(entry.after_row->>'item_id')::bigint];
 ELSE ids:='{}';END IF;
 FOR ident IN SELECT DISTINCT x FROM unnest(ids)x WHERE x IS NOT NULL AND x>COALESCE(fan,0) ORDER BY x LIMIT p_limit-scanned LOOP
 scanned:=scanned+1;fan:=ident;queued:=queued+em_content.history_vector_queue(p_library,p_contract,ident,ctx->'source');
 END LOOP;
 more:=EXISTS(SELECT 1 FROM unnest(ids)x WHERE x IS NOT NULL AND x>COALESCE(fan,0));
 IF more THEN EXIT; END IF;
 IF cardinality(ids)=0 THEN scanned:=scanned+1; END IF;
 fan:=NULL;
 END IF;
 cursor_o:=entry.ordinal;cursor_c:=entry.change_ordinal;
 IF scanned>=p_limit THEN EXIT; END IF;
 END LOOP;
 done:=fan IS NULL AND NOT EXISTS(SELECT 1 FROM em_content.history_catchup_changes c WHERE c.epoch_id=p_epoch
 AND(c.ordinal,c.change_ordinal)>(cursor_o,cursor_c) LIMIT 1);
 UPDATE em_content.history_vector_state SET after_ordinal=cursor_o,after_change_ordinal=cursor_c,fanout_after_id=fan,delta_complete=done
 WHERE library_id=p_library AND model_contract=p_contract;
 RETURN jsonb_build_object('epoch_id',p_epoch,'after_ordinal',cursor_o,'after_change_ordinal',cursor_c,'fanout_after_id',fan,
 'scanned',scanned,'enqueued',queued,'complete',done);
END $fn$;
CREATE FUNCTION em_content.history_vector_finish(p_library uuid,p_contract text) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_security,em_content AS $fn$
DECLARE ctx jsonb; s em_content.history_vector_state; targets bigint; indexed bigint; result jsonb;
BEGIN
 PERFORM em_security.history_vector_index_context(p_library,p_contract);
 PERFORM em_content.history_vector_lock_source(p_library,p_contract);
 ctx:=em_security.history_vector_index_context(p_library,p_contract);
 SELECT * INTO s FROM em_content.history_vector_state WHERE library_id=p_library AND model_contract=p_contract FOR UPDATE;
 IF s.source_token IS DISTINCT FROM ctx->'source' OR(s.covered_source IS NULL AND NOT s.bootstrap_complete)
 OR(s.covered_source IS NOT NULL AND NOT s.delta_complete) OR s.enabled
 OR EXISTS(SELECT 1 FROM em_content.history_vector_jobs WHERE library_id=p_library AND model_contract=p_contract AND status='pending')
 THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='history_vector_coverage_incomplete'; END IF;
 SELECT count(*) INTO targets FROM em_content.history_vector_targets WHERE library_id=p_library AND model_contract=p_contract AND action='upsert';
 SELECT count(*) INTO indexed FROM em_content.history_vector_targets t JOIN em_content.history_vector_objects v
 USING(library_id,model_contract,entity_id) WHERE t.library_id=p_library AND t.model_contract=p_contract AND t.action='upsert' AND v.projection_state='ready'
 AND t.entity_revision=v.entity_revision AND t.content_hash=v.content_hash
 AND v.vector_hash=encode(sha256(public.vector_send(v.embedding)),'hex');
 IF targets<>indexed OR EXISTS(SELECT 1 FROM em_content.history_vector_targets t LEFT JOIN em_content.history_vector_objects v
 USING(library_id,model_contract,entity_id) WHERE t.library_id=p_library AND t.model_contract=p_contract AND t.action='remove'
 AND(v.entity_id IS NULL OR v.projection_state<>'removed' OR v.entity_revision<>t.entity_revision OR v.content_hash<>t.content_hash))
 THEN RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='history_vector_coverage_incomplete'; END IF;
 UPDATE em_content.history_vector_state SET covered_source=ctx->'source',enabled=true,coverage='complete',revision=revision+1,
 indexed_count=indexed,target_count=targets WHERE library_id=p_library AND model_contract=p_contract;
 result:=jsonb_build_object('model',ctx->'model','source',ctx->'source','ann',em_security.history_vector_ann(p_library,p_contract,ctx->'source'));
 RETURN jsonb_build_object('context',result);
END $fn$;

-- Closed grants: the runtime receives no direct table/sequence privileges.
DO $grants$ DECLARE r name; f regprocedure; BEGIN
 FOR f IN SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE n.nspname IN('em_security','em_content') AND p.proname LIKE 'history_vector_%' LOOP
 EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC',f);
 END LOOP;
 FOR r IN SELECT login_role FROM em_security.principal_bindings LOOP
 IF NOT em_security._runtime_role_safe(r) THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='unsafe_history_vector_runtime'; END IF;
 FOR f IN SELECT p.oid::regprocedure FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
 WHERE(n.nspname='em_security' AND p.proname IN('history_vector_context','history_vector_index_context'))
 OR(n.nspname='em_content' AND p.proname IN('history_vector_next_job','history_vector_document','history_vector_documents',
 'history_vector_cache_entry','history_vector_enqueue_page','history_vector_apply','history_vector_candidates',
 'history_vector_projection_proof','history_vector_delta_page','history_vector_finish')) LOOP
 EXECUTE format('GRANT EXECUTE ON FUNCTION %s TO %I',f,r);
 END LOOP;
 END LOOP;
END $grants$;
INSERT INTO em_content.history_vector_schema_migrations VALUES(1,:'library_id'::uuid,clock_timestamp());
COMMIT;

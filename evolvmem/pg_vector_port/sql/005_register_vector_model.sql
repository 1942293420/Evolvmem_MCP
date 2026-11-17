\set ON_ERROR_STOP on
-- Owner-only explicit model registration. Old models/indices/jobs remain intact.
BEGIN;
SELECT pg_catalog.set_config('em_vector.owner',:'owner_role',true),
 pg_catalog.set_config('em_vector.database',:'database_name',true),
 pg_catalog.set_config('em_vector.library',:'library_id',true),
 pg_catalog.set_config('em_vector.contract',:'model_contract',true),
 pg_catalog.set_config('em_vector.document',:'document_contract_text',true),
 pg_catalog.set_config('em_vector.query',:'query_prefix',true);
DO $register$
DECLARE owner_name name:=current_setting('em_vector.owner'); lib uuid:=current_setting('em_vector.library')::uuid;
 model text:=current_setting('em_vector.contract'); doc text:=current_setting('em_vector.document');
 prefix text:='vector_m_'||left(model,24); legacy text:=prefix||'_legacy'; context_name text:=prefix||'_context';
 legacy_idx text:=prefix||'_lhnsw'; context_idx text:=prefix||'_chnsw'; item text;
BEGIN
 IF current_user<>owner_name OR current_database()<>current_setting('em_vector.database')
  OR NOT EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid WHERE r.rolname=current_user
    AND d.datname=current_database() AND NOT(r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreaterole))
  OR NOT EXISTS(SELECT 1 FROM em_security.library_state WHERE singleton AND library_id=lib)
  OR model !~ '^[0-9a-f]{64}$' OR model<>encode(sha256(convert_to(doc,'UTF8')),'hex')
  OR jsonb_typeof(doc::jsonb)<>'array' OR jsonb_array_length(doc::jsonb)<>4
  OR (doc::jsonb->>1)::int<>768 OR (doc::jsonb->>0) !~ '@sha256:[0-9a-f]{64}$'
 THEN RAISE EXCEPTION 'explicit_vector_model_registration_required'; END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('evolvmem.vector.model.'||lib::text,0));
 IF EXISTS(SELECT 1 FROM em_content.vector_models WHERE library_id=lib AND model_contract=model) THEN
  IF NOT EXISTS(SELECT 1 FROM em_content.vector_models m JOIN em_content.vector_model_routes r USING(library_id,model_contract)
   WHERE m.library_id=lib AND m.model_contract=model AND m.document_contract_text=doc
    AND m.query_prefix=current_setting('em_vector.query')) THEN RAISE EXCEPTION 'registered_model_conflict'; END IF;
  RETURN;
 END IF;
 IF (SELECT count(*) FROM em_content.vector_models WHERE library_id=lib)>=4 THEN RAISE EXCEPTION 'vector_model_partition_budget'; END IF;
 INSERT INTO em_content.vector_models(library_id,project_id,model_contract,dimension,document_contract_text,query_prefix)
  VALUES(lib,NULL,model,768,doc,current_setting('em_vector.query'));
 EXECUTE format('CREATE TABLE em_content.%I PARTITION OF em_content.vector_library FOR VALUES IN (%L) PARTITION BY LIST(index_kind)',prefix,model);
 EXECUTE format('CREATE TABLE em_content.%I PARTITION OF em_content.%I FOR VALUES IN (''legacy'')',legacy,prefix);
 EXECUTE format('CREATE TABLE em_content.%I PARTITION OF em_content.%I FOR VALUES IN (''context'')',context_name,prefix);
 EXECUTE format('CREATE INDEX %I ON em_content.%I USING hnsw(embedding public.vector_cosine_ops)',legacy_idx,legacy);
 EXECUTE format('CREATE INDEX %I ON em_content.%I USING hnsw(embedding public.vector_cosine_ops)',context_idx,context_name);
 FOREACH item IN ARRAY ARRAY[prefix,legacy,context_name] LOOP
  EXECUTE format('ALTER TABLE em_content.%I ENABLE ROW LEVEL SECURITY',item);
  EXECUTE format('ALTER TABLE em_content.%I FORCE ROW LEVEL SECURITY',item);
  EXECUTE format('CREATE POLICY partition_owner ON em_content.%I TO %I USING(true) WITH CHECK(true)',item,owner_name);
  EXECUTE format('REVOKE ALL ON em_content.%I FROM PUBLIC',item);
 END LOOP;
 INSERT INTO em_content.vector_model_routes VALUES(lib,model,
  to_regclass('em_content.'||prefix),to_regclass('em_content.'||legacy),to_regclass('em_content.'||context_name),
  to_regclass('em_content.'||legacy_idx),to_regclass('em_content.'||context_idx));
END;
$register$;
COMMIT;

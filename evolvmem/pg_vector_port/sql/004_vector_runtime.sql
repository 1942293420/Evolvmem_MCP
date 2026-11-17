\set ON_ERROR_STOP on
-- Explicit owner provisioning after 003; no runtime DDL or history promotion.
BEGIN;
SELECT current_database()=:'database_name' AND current_user=:'owner_role'
 AND EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
  WHERE d.datname=current_database() AND r.rolname=current_user
   AND NOT(r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreaterole)) AS safe_owner \gset
\if :safe_owner
\else
 \quit 3
\endif
CREATE TABLE em_content.vector_model_routes(
 library_id uuid NOT NULL,model_contract text NOT NULL,
 model_partition oid NOT NULL,legacy_partition oid NOT NULL,context_partition oid NOT NULL,
 legacy_index oid NOT NULL,context_index oid NOT NULL,
 PRIMARY KEY(library_id,model_contract),
 FOREIGN KEY(library_id,model_contract) REFERENCES em_content.vector_models(library_id,model_contract)
);
ALTER TABLE em_content.vector_model_routes ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.vector_model_routes FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_content.vector_model_routes TO :"owner_role" USING(true) WITH CHECK(true);
REVOKE ALL ON em_content.vector_model_routes FROM PUBLIC;
DO $seed_gate$ BEGIN
 IF (SELECT count(*) FROM em_content.vector_models)<>1 THEN
  RAISE EXCEPTION 'initial_vector_model_route_required';
 END IF;
END; $seed_gate$;
INSERT INTO em_content.vector_model_routes
 SELECT library_id,model_contract,'em_content.vector_current'::regclass,'em_content.vector_legacy'::regclass,
  'em_content.vector_context'::regclass,'em_content.vector_legacy_hnsw'::regclass,'em_content.vector_context_hnsw'::regclass
 FROM em_content.vector_models;
CREATE TABLE em_content.vector_active_models(
 library_id uuid PRIMARY KEY,model_contract text NOT NULL,
 FOREIGN KEY(library_id,model_contract) REFERENCES em_content.vector_models(library_id,model_contract)
);
ALTER TABLE em_content.vector_active_models ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.vector_active_models FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_content.vector_active_models TO :"owner_role" USING(true) WITH CHECK(true);
REVOKE ALL ON em_content.vector_active_models FROM PUBLIC;
INSERT INTO em_content.vector_active_models SELECT library_id,model_contract FROM em_content.vector_models;
CREATE FUNCTION em_content.require_active_vector_job() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog AS $fn$
DECLARE active_model text;
BEGIN
 -- Keep this share lock through the business write commit. Activation takes
 -- the conflicting row lock, so a completed switch excludes old writers.
 SELECT model_contract INTO active_model FROM em_content.vector_active_models
  WHERE library_id=NEW.library_id FOR SHARE;
 IF active_model IS NULL OR active_model<>NEW.model_contract THEN
  RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='vector_model_contract_inactive';
 END IF;
 RETURN NEW;
END; $fn$;
REVOKE ALL ON FUNCTION em_content.require_active_vector_job() FROM PUBLIC;
CREATE TRIGGER active_vector_job BEFORE INSERT ON em_content.vector_outbox
 FOR EACH ROW EXECUTE FUNCTION em_content.require_active_vector_job();

CREATE FUNCTION em_security.activate_vector_model(p_library uuid,p_expected text,p_new text) RETURNS text
LANGUAGE plpgsql SET search_path=pg_catalog AS $fn$
DECLARE active_model text;
BEGIN
 IF NOT EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
   WHERE r.rolname=current_user AND d.datname=current_database()
    AND NOT(r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreaterole))
  OR NOT EXISTS(SELECT 1 FROM em_content.vector_models m JOIN em_content.vector_model_routes r USING(library_id,model_contract)
    WHERE m.library_id=p_library AND m.model_contract=p_new
     AND (m.document_contract_text::jsonb->>0) ~ '@sha256:[0-9a-f]{64}$')
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='versioned_vector_owner_activation_required'; END IF;
 SELECT model_contract INTO active_model FROM em_content.vector_active_models WHERE library_id=p_library FOR UPDATE;
 IF active_model IS DISTINCT FROM p_expected THEN
  RAISE EXCEPTION USING ERRCODE='55000',MESSAGE='vector_model_activation_conflict';
 END IF;
 UPDATE em_content.vector_active_models SET model_contract=p_new WHERE library_id=p_library;
 RETURN p_new;
END; $fn$;
REVOKE ALL ON FUNCTION em_security.activate_vector_model(uuid,text,text) FROM PUBLIC;

CREATE FUNCTION em_security.vector_context(p_library uuid,p_project uuid,p_contract text)
RETURNS TABLE(runtime_version text,dimension integer,document_contract_text text,query_prefix text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security AS $fn$
DECLARE epoch bigint; route em_content.vector_model_routes%ROWTYPE;
BEGIN
 SELECT c.policy_epoch INTO epoch FROM em_security.session_context() c WHERE c.library_id=p_library;
 IF epoch IS NULL OR p_project IS NULL OR NOT em_security.allows(p_library,p_project,'read')
  OR NOT EXISTS(SELECT 1 FROM em_security.project_projection p WHERE p.project_id=p_project
                AND p.projection_epoch=epoch AND p.active) THEN RETURN; END IF;
 SELECT r.* INTO route FROM em_content.vector_model_routes r
  WHERE r.library_id=p_library AND r.model_contract=p_contract
   AND EXISTS(SELECT 1 FROM em_content.vector_active_models a WHERE a.library_id=p_library AND a.model_contract=p_contract);
 IF NOT FOUND OR NOT EXISTS(SELECT 1 FROM em_content.vector_schema_migrations WHERE version=1 AND library_id=p_library)
  OR NOT EXISTS(SELECT 1 FROM pg_extension WHERE extname='vector' AND extversion='0.8.6') THEN RETURN; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_inherits WHERE inhrelid=route.model_partition AND inhparent='em_content.vector_library'::regclass)
  OR NOT EXISTS(SELECT 1 FROM pg_class WHERE oid=route.model_partition
     AND pg_get_expr(relpartbound,oid)=format('FOR VALUES IN (%L)',p_contract))
  OR NOT EXISTS(SELECT 1 FROM pg_inherits WHERE inhrelid=route.legacy_partition AND inhparent=route.model_partition)
  OR NOT EXISTS(SELECT 1 FROM pg_inherits WHERE inhrelid=route.context_partition AND inhparent=route.model_partition)
  OR NOT EXISTS(SELECT 1 FROM pg_class WHERE oid=route.legacy_partition AND pg_get_expr(relpartbound,oid)='FOR VALUES IN (''legacy'')')
  OR NOT EXISTS(SELECT 1 FROM pg_class WHERE oid=route.context_partition AND pg_get_expr(relpartbound,oid)='FOR VALUES IN (''context'')')
 THEN RETURN; END IF;
 IF (SELECT count(*)=9 AND bool_and(relrowsecurity AND relforcerowsecurity) FROM pg_class
   WHERE oid IN ('em_content.vector_projections'::regclass,'em_content.vector_library'::regclass,
    'em_content.vector_models'::regclass,'em_content.vector_outbox'::regclass,'em_content.vector_model_routes'::regclass,
    'em_content.vector_active_models'::regclass,
    route.model_partition,route.legacy_partition,route.context_partition)) IS DISTINCT FROM true THEN RETURN; END IF;
 IF (SELECT count(*)=2 AND bool_and(i.indisvalid AND i.indisready AND am.amname='hnsw'
      AND op.opcname='vector_cosine_ops' AND ((i.indexrelid=route.legacy_index AND i.indrelid=route.legacy_partition)
       OR (i.indexrelid=route.context_index AND i.indrelid=route.context_partition)))
    FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_am am ON am.oid=c.relam
    JOIN pg_opclass op ON op.oid=i.indclass[0] WHERE i.indexrelid IN(route.legacy_index,route.context_index))
   IS DISTINCT FROM true THEN RETURN; END IF;
 IF NOT EXISTS(SELECT 1 FROM pg_constraint c WHERE c.conrelid='em_content.vector_outbox'::regclass
    AND c.conname='vector_job_contract_unique' AND c.contype='u' AND c.convalidated
    AND (SELECT array_agg(a.attname::text ORDER BY k.ordinality) FROM unnest(c.conkey) WITH ORDINALITY k(attnum,ordinality)
         JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum)
       =ARRAY['library_id','index_kind','entity_id','entity_revision','action','model_contract']) THEN RETURN; END IF;
 RETURN QUERY SELECT 'vector-runtime.v2'::text,m.dimension,m.document_contract_text,m.query_prefix
  FROM em_content.vector_models m WHERE m.library_id=p_library AND m.model_contract=p_contract;
END;
$fn$;
REVOKE ALL ON FUNCTION em_security.vector_context(uuid,uuid,text) FROM PUBLIC;
DO $grants$ DECLARE login name; BEGIN
 FOR login IN SELECT login_role FROM em_security.principal_bindings LOOP
  IF NOT em_security._runtime_role_safe(login) THEN RAISE EXCEPTION 'unsafe_vector_runtime'; END IF;
  EXECUTE format('GRANT EXECUTE ON FUNCTION em_security.vector_context(uuid,uuid,text) TO %I',login);
  EXECUTE format('GRANT SELECT,INSERT,UPDATE ON em_content.vector_projections TO %I',login);
 END LOOP;
END; $grants$;
COMMIT;

\set ON_ERROR_STOP on
-- Trusted owner provisioning. Does not activate native recall.
BEGIN;
SELECT set_config('em_recall.owner', :'owner_role', true);
SELECT set_config('em_recall.library', :'library_id', true);
DO $gate$ BEGIN
 IF current_user<>current_setting('em_recall.owner')
  OR NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname=current_user
   AND NOT(rolcanlogin OR rolsuper OR rolbypassrls OR rolcreatedb OR rolcreaterole))
  OR NOT EXISTS(SELECT 1 FROM em_content.business_schema_migrations WHERE version=1)
  OR NOT EXISTS(SELECT 1 FROM em_security.library_state WHERE singleton
   AND library_id=current_setting('em_recall.library')::uuid) THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='recall_provisioning_boundary_required';
 END IF;
 -- Existing native layers are checked once at provisioning, then at writes.
 IF EXISTS(SELECT 1 FROM em_content.context_layers
  WHERE content_hash IS DISTINCT FROM encode(sha256(convert_to(content,'UTF8')),'hex')) THEN
  RAISE EXCEPTION 'recall_existing_content_hash_mismatch';
 END IF;
END; $gate$;
CREATE FUNCTION em_content.assert_context_layer_hash() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog AS $fn$
BEGIN
 IF NEW.content_hash IS DISTINCT FROM encode(sha256(convert_to(NEW.content,'UTF8')),'hex') THEN
  RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='context_layer_hash_mismatch';
 END IF;
 RETURN NEW;
END; $fn$;
REVOKE ALL ON FUNCTION em_content.assert_context_layer_hash() FROM PUBLIC;
CREATE TRIGGER context_layers_hash_valid BEFORE INSERT OR UPDATE OF content,content_hash
 ON em_content.context_layers FOR EACH ROW EXECUTE FUNCTION em_content.assert_context_layer_hash();

-- The same deterministic tokenization is used for English identifiers and CJK
-- bigrams. This indexes L0/L1 only, never the full original/L2 archive.
CREATE FUNCTION em_content.recall_lexical_text(value text) RETURNS text
LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE SET search_path=pg_catalog AS $fn$
 WITH words AS (
  SELECT w FROM unnest(regexp_split_to_array(lower(value),'[^[:alnum:]_一-鿿]+')) AS w
  WHERE w<>''
 ), tokens AS (
  SELECT w AS token FROM words
  UNION ALL
  SELECT substr(w,p,2) FROM words CROSS JOIN LATERAL generate_series(1,length(w)-1) AS p
  WHERE w ~ '[一-鿿]'
 ) SELECT coalesce(string_agg(token,' ' ORDER BY token),'') FROM tokens
$fn$;
REVOKE ALL ON FUNCTION em_content.recall_lexical_text(text) FROM PUBLIC;
-- This pure text function contains no data/identity lookup.
GRANT EXECUTE ON FUNCTION em_content.recall_lexical_text(text) TO PUBLIC;
CREATE INDEX context_layers_recall_fts ON em_content.context_layers
 USING gin(to_tsvector('simple'::regconfig,em_content.recall_lexical_text(content)))
 WHERE layer IN ('l0','l1');
CREATE INDEX context_items_recall_family ON em_content.context_items
 (library_id,project_id,identity_key,project,scope,id);
-- Only authorized project names are discoverable; the caller cannot supply
-- an identity or bypass the existing role-bound session/ACL projection.
CREATE FUNCTION em_security.list_recall_projects(p_library uuid,p_action text,p_after text DEFAULT NULL)
RETURNS TABLE(project_id uuid,project_key text)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_security AS $fn$
 SELECT p.project_id,p.project_key FROM em_security.session_context() c
 JOIN em_security.project_projection p ON p.active AND p.projection_epoch=c.policy_epoch
 WHERE c.library_id=p_library AND p_action IN ('read','write')
  AND em_security.allows(p_library,p.project_id,p_action)
  AND (p_after IS NULL OR p.project_key COLLATE "C">p_after COLLATE "C")
 ORDER BY p.project_key COLLATE "C" LIMIT 128
$fn$;
REVOKE ALL ON FUNCTION em_security.list_recall_projects(uuid,text,text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION em_security.list_recall_projects(uuid,text,text) TO PUBLIC;
CREATE TABLE em_content.lexical_schema_migrations(
 version integer PRIMARY KEY CHECK(version=1),library_id uuid NOT NULL,
 applied_at timestamptz NOT NULL DEFAULT clock_timestamp());
ALTER TABLE em_content.lexical_schema_migrations ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_content.lexical_schema_migrations FORCE ROW LEVEL SECURITY;
CREATE POLICY lexical_owner ON em_content.lexical_schema_migrations TO :"owner_role"
 USING(true) WITH CHECK(true);
REVOKE ALL ON em_content.lexical_schema_migrations FROM PUBLIC;
INSERT INTO em_content.lexical_schema_migrations(version,library_id) VALUES(1,:'library_id');
COMMIT;

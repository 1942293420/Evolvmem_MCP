-- Executed only by the explicit local operator prepare under its live control lease.
CREATE TABLE em_security.history_operator_binding(
 singleton boolean PRIMARY KEY CHECK(singleton),
 binding_hash text NOT NULL CHECK(binding_hash ~ '^[0-9a-f]{64}$'),
 manifest jsonb NOT NULL CHECK(jsonb_typeof(manifest)='object')
);
REVOKE ALL ON em_security.history_operator_binding FROM PUBLIC;
ALTER TABLE em_security.history_operator_binding ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_security.history_operator_binding FORCE ROW LEVEL SECURITY;
DO $policy$ BEGIN
 EXECUTE format('CREATE POLICY history_operator_owner ON em_security.history_operator_binding TO %I USING(true) WITH CHECK(true)',current_user);
END $policy$;
CREATE FUNCTION em_security.assert_history_operator_binding() RETURNS void
LANGUAGE plpgsql SET search_path=pg_catalog,em_security AS $binding$
DECLARE b jsonb;
BEGIN
 SELECT manifest INTO STRICT b FROM em_security.history_operator_binding
 WHERE singleton AND binding_hash=current_setting('history.operator_binding',true) FOR SHARE;
 IF b->>'binding_hash' IS DISTINCT FROM current_setting('history.operator_binding',true)
 OR b->'target'->>'database' IS DISTINCT FROM current_database()
 OR b->'target'->>'owner_role' IS DISTINCT FROM current_user
 OR b->'target'->>'login_role' IS DISTINCT FROM session_user
 OR NOT EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
  WHERE r.rolname=current_user AND d.datname=current_database() AND NOT r.rolcanlogin
  AND NOT r.rolsuper AND NOT r.rolbypassrls AND NOT r.rolcreatedb AND NOT r.rolcreaterole)
 OR NOT EXISTS(SELECT 1 FROM em_security.library_state s WHERE s.singleton
  AND s.library_id=(b->'target'->>'library_id')::uuid AND s.kind='legacy'
  AND s.device_id IS NULL AND s.project_id IS NULL AND s.authority_state='pg_staging' AND s.acl_state='updating'
  AND s.route_epoch=(b->'target'->>'route_epoch')::bigint AND s.policy_epoch=(b->'target'->>'policy_epoch')::bigint)
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_operator_binding_mismatch'; END IF;
END $binding$;
REVOKE ALL ON FUNCTION em_security.assert_history_operator_binding() FROM PUBLIC;

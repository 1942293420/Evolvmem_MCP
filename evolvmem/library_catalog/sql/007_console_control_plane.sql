-- Append-only control migration v7: Console accounts, Feishu SSO bindings,
-- Web persistent sessions, default personal write library, and directional grants.
-- Trusted psql variables: database_name, owner_role, coordinator_role.
\set ON_ERROR_STOP on
SELECT current_database() = :'database_name' AND current_user = :'owner_role'
  AND EXISTS (SELECT 1 FROM pg_catalog.pg_roles r JOIN pg_catalog.pg_database d ON d.datdba = r.oid
    WHERE r.rolname = current_user AND d.datname = current_database()
      AND NOT (r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb))
  AND EXISTS (SELECT 1 FROM em_control.service_roles s
    WHERE s.login_role = :'coordinator_role' AND s.enabled AND s.can_coordinate)
  AS preflight_ok \gset
\if :preflight_ok
\else
  \quit 3
\endif

BEGIN;
SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('evolvmem.control.schema', 0));
DO $guard$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM em_control.schema_migrations WHERE version = 6)
     OR EXISTS (SELECT 1 FROM em_control.schema_migrations WHERE version >= 7) THEN
    RAISE EXCEPTION 'invalid_console_control_schema_version';
  END IF;
END $guard$;

CREATE TABLE em_control.console_meta (
    key text PRIMARY KEY CHECK (length(key) BETWEEN 1 AND 64),
    value text NOT NULL CHECK (length(value) BETWEEN 1 AND 256)
);
CREATE TABLE em_control.console_users (
    user_id text PRIMARY KEY CHECK (user_id ~ '^[a-z][a-z0-9_-]{0,63}$'),
    principal_id uuid NOT NULL UNIQUE REFERENCES em_control.principals(principal_id),
    display_name text NOT NULL CHECK (length(btrim(display_name)) BETWEEN 1 AND 100),
    enabled boolean NOT NULL DEFAULT true,
    is_admin boolean NOT NULL DEFAULT false,
    open_id text UNIQUE CHECK (open_id IS NULL OR (length(open_id) BETWEEN 1 AND 200 AND open_id !~ '[[:space:][:cntrl:]]')),
    lan_enabled boolean NOT NULL DEFAULT false,
    default_write_library_id uuid REFERENCES em_control.libraries(library_id),
    revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE em_control.console_pending (
    open_id text PRIMARY KEY CHECK (length(open_id) BETWEEN 1 AND 200 AND open_id !~ '[[:space:][:cntrl:]]'),
    name text NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 100),
    first_seen timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE em_control.console_library_sources (
    user_id text PRIMARY KEY REFERENCES em_control.console_users(user_id),
    experience_library_id uuid REFERENCES em_control.libraries(library_id),
    history_library_id uuid REFERENCES em_control.libraries(library_id),
    data_dir text,
    history_environ jsonb NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(history_environ) = 'object')
);
CREATE TABLE em_control.console_grants (
    viewer_id text NOT NULL REFERENCES em_control.console_users(user_id),
    owner_id text NOT NULL REFERENCES em_control.console_users(user_id),
    history_read boolean NOT NULL DEFAULT false,
    experience_read boolean NOT NULL DEFAULT false,
    revision bigint NOT NULL DEFAULT 1 CHECK (revision >= 1),
    PRIMARY KEY (viewer_id, owner_id),
    CHECK (viewer_id <> owner_id)
);
CREATE TABLE em_control.web_sessions (
    token_hash text PRIMARY KEY CHECK (token_hash ~ '^[0-9a-f]{64}$'),
    auth_scope text NOT NULL CHECK (length(auth_scope) BETWEEN 1 AND 256),
    open_id text NOT NULL CHECK (length(open_id) BETWEEN 1 AND 200 AND open_id !~ '[[:space:][:cntrl:]]'),
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    csrf text NOT NULL CHECK (length(csrf) BETWEEN 1 AND 200 AND csrf !~ '[[:space:][:cntrl:]]'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX web_sessions_open_id_idx ON em_control.web_sessions(open_id);
CREATE INDEX web_sessions_expires_at_idx ON em_control.web_sessions(expires_at);
CREATE TABLE em_control.console_audit (
    audit_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    actor_id text NOT NULL,
    action text NOT NULL,
    target_id text NOT NULL,
    before_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    after_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    outcome text NOT NULL
);

ALTER TABLE em_control.console_meta ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.console_meta FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.console_meta TO :"owner_role" USING (true) WITH CHECK (true);
ALTER TABLE em_control.console_users ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.console_users FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.console_users TO :"owner_role" USING (true) WITH CHECK (true);
ALTER TABLE em_control.console_pending ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.console_pending FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.console_pending TO :"owner_role" USING (true) WITH CHECK (true);
ALTER TABLE em_control.console_library_sources ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.console_library_sources FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.console_library_sources TO :"owner_role" USING (true) WITH CHECK (true);
ALTER TABLE em_control.console_grants ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.console_grants FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.console_grants TO :"owner_role" USING (true) WITH CHECK (true);
ALTER TABLE em_control.web_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.web_sessions FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.web_sessions TO :"owner_role" USING (true) WITH CHECK (true);
ALTER TABLE em_control.console_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.console_audit FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.console_audit TO :"owner_role" USING (true) WITH CHECK (true);

REVOKE ALL ON ALL TABLES IN SCHEMA em_control FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA em_control FROM PUBLIC;

CREATE FUNCTION em_control._require_console_owner(p_expected_owner text) RETURNS void
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
DECLARE v_owner text;
BEGIN
  SELECT m.value INTO v_owner FROM em_control.console_meta m WHERE m.key = 'owner_id';
  IF v_owner IS NULL THEN
    RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'console_not_initialized';
  END IF;
  IF p_expected_owner IS NOT NULL AND v_owner <> p_expected_owner THEN
    RAISE EXCEPTION USING ERRCODE = '55000', MESSAGE = 'invalid_console_registry';
  END IF;
END $fn$;

CREATE FUNCTION em_control.console_bootstrap(
    p_owner_id text, p_owner_principal_id uuid, p_owner_open_id text, p_owner_name text,
    p_owner_exp_lib uuid, p_owner_hist_lib uuid, p_owner_data_dir text, p_history_environ jsonb
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
DECLARE v_meta_owner text; v_existing_open_id text;
BEGIN
  PERFORM em_control._require_service('coordinate');
  SELECT value INTO v_meta_owner FROM em_control.console_meta WHERE key = 'owner_id' FOR UPDATE;
  SELECT open_id INTO v_existing_open_id FROM em_control.console_users WHERE user_id = p_owner_id FOR UPDATE;
  IF (v_meta_owner IS NOT NULL AND v_meta_owner <> p_owner_id)
     OR (v_existing_open_id IS NOT NULL AND v_existing_open_id <> p_owner_open_id) THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'configured_owner_rebind_forbidden';
  END IF;
  INSERT INTO em_control.console_meta(key, value) VALUES ('owner_id', p_owner_id) ON CONFLICT (key) DO NOTHING;
  INSERT INTO em_control.console_meta(key, value) VALUES ('version', '2') ON CONFLICT (key) DO NOTHING;
  IF v_existing_open_id IS NULL THEN
    INSERT INTO em_control.principals(principal_id, display_name, active, credential_epoch)
    VALUES (p_owner_principal_id, p_owner_name, true, 1) ON CONFLICT (principal_id) DO NOTHING;
    INSERT INTO em_control.libraries(library_id, kind, owner_principal_id, backend, connection_ref, schema_version, active)
    VALUES (p_owner_exp_lib, 'legacy', p_owner_principal_id, 'sqlite', 'sqlite.exp.' || p_owner_id, 1, true)
    ON CONFLICT (library_id) DO NOTHING;
    INSERT INTO em_control.library_routes(library_id, route_epoch, policy_epoch, authority_state, write_backend, acl_state)
    VALUES (p_owner_exp_lib, 1, 1, 'sqlite_active', 'sqlite', 'ready') ON CONFLICT (library_id) DO NOTHING;
    INSERT INTO em_control.library_acl(library_id, principal_id, permission, active, policy_epoch, granted_by)
    VALUES (p_owner_exp_lib, p_owner_principal_id, 'admin', true, 1, p_owner_principal_id)
    ON CONFLICT (library_id, principal_id) DO NOTHING;
    INSERT INTO em_control.libraries(library_id, kind, owner_principal_id, backend, connection_ref, schema_version, active)
    VALUES (p_owner_hist_lib, 'legacy', p_owner_principal_id, 'sqlite', 'sqlite.hist.' || p_owner_id, 1, true)
    ON CONFLICT (library_id) DO NOTHING;
    INSERT INTO em_control.library_routes(library_id, route_epoch, policy_epoch, authority_state, write_backend, acl_state)
    VALUES (p_owner_hist_lib, 1, 1, 'sqlite_active', 'sqlite', 'ready') ON CONFLICT (library_id) DO NOTHING;
    INSERT INTO em_control.library_acl(library_id, principal_id, permission, active, policy_epoch, granted_by)
    VALUES (p_owner_hist_lib, p_owner_principal_id, 'admin', true, 1, p_owner_principal_id)
    ON CONFLICT (library_id, principal_id) DO NOTHING;
    INSERT INTO em_control.console_users(
        user_id, principal_id, display_name, enabled, is_admin, open_id, lan_enabled, default_write_library_id
    ) VALUES (
        p_owner_id, p_owner_principal_id, p_owner_name, true, true, p_owner_open_id, true,
        CASE WHEN p_owner_data_dir IS NOT NULL AND length(p_owner_data_dir) > 0 THEN p_owner_exp_lib ELSE NULL END
    );
    INSERT INTO em_control.console_library_sources(
        user_id, experience_library_id, history_library_id, data_dir, history_environ
    ) VALUES (
        p_owner_id, p_owner_exp_lib, p_owner_hist_lib, p_owner_data_dir, coalesce(p_history_environ, '{}'::jsonb)
    );
    INSERT INTO em_control.console_audit(actor_id, action, target_id, outcome)
    VALUES (p_owner_id, 'registry.bootstrap', p_owner_id, 'allowed');
  END IF;
  RETURN true;
END $fn$;

CREATE FUNCTION em_control.console_status(p_expected_owner text)
RETURNS TABLE(ready boolean, owner_id text, version text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
DECLARE v_owner text; v_ver text;
BEGIN
  PERFORM em_control._require_service('read');
  SELECT value INTO v_owner FROM em_control.console_meta WHERE key = 'owner_id';
  SELECT value INTO v_ver FROM em_control.console_meta WHERE key = 'version';
  IF v_owner IS NULL OR v_ver IS NULL THEN
    RETURN QUERY SELECT false, coalesce(v_owner, ''), coalesce(v_ver, '');
    RETURN;
  END IF;
  IF p_expected_owner IS NOT NULL AND v_owner <> p_expected_owner THEN
    RETURN QUERY SELECT false, v_owner, v_ver;
    RETURN;
  END IF;
  RETURN QUERY SELECT (v_ver = '2'), v_owner, v_ver;
END $fn$;

CREATE FUNCTION em_control.console_observe_login(p_expected_owner text, p_open_id text, p_name text)
RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
BEGIN
  PERFORM em_control._require_service('coordinate');
  PERFORM em_control._require_console_owner(p_expected_owner);
  IF EXISTS (SELECT 1 FROM em_control.console_users WHERE open_id = p_open_id) THEN
    RETURN false;
  END IF;
  INSERT INTO em_control.console_pending(open_id, name, first_seen, last_seen)
  VALUES (p_open_id, p_name, clock_timestamp(), clock_timestamp())
  ON CONFLICT (open_id) DO UPDATE SET name = EXCLUDED.name, last_seen = EXCLUDED.last_seen;
  RETURN true;
END $fn$;

CREATE FUNCTION em_control.console_login_allowed(p_expected_owner text, p_open_id text)
RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
DECLARE v_enabled boolean;
BEGIN
  PERFORM em_control._require_service('read');
  PERFORM em_control._require_console_owner(p_expected_owner);
  SELECT u.enabled INTO v_enabled FROM em_control.console_users u WHERE u.open_id = p_open_id;
  IF FOUND THEN
    RETURN v_enabled;
  END IF;
  RETURN EXISTS (SELECT 1 FROM em_control.console_pending p WHERE p.open_id = p_open_id);
END $fn$;

CREATE FUNCTION em_control.console_user_enabled(p_expected_owner text, p_user_id text)
RETURNS boolean
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
DECLARE v_enabled boolean;
BEGIN
  PERFORM em_control._require_service('read');
  PERFORM em_control._require_console_owner(p_expected_owner);
  SELECT u.enabled INTO v_enabled FROM em_control.console_users u WHERE u.user_id = p_user_id;
  RETURN coalesce(v_enabled, false);
END $fn$;

CREATE FUNCTION em_control.console_get_users(p_expected_owner text)
RETURNS TABLE(
    user_id text, principal_id uuid, display_name text, enabled boolean, is_admin boolean,
    open_id text, lan_enabled boolean, default_write_library_id uuid, revision bigint,
    experience_library_id uuid, history_library_id uuid, data_dir text, history_environ jsonb,
    backend text, authority_state text, write_backend text
)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
BEGIN
  PERFORM em_control._require_service('read');
  PERFORM em_control._require_console_owner(p_expected_owner);
  RETURN QUERY
  SELECT u.user_id, u.principal_id, u.display_name, u.enabled, u.is_admin,
         u.open_id, u.lan_enabled, u.default_write_library_id, u.revision,
         s.experience_library_id, s.history_library_id, s.data_dir,
         coalesce(s.history_environ, '{}'::jsonb),
         coalesce(l.backend, 'sqlite'), coalesce(r.authority_state, 'sqlite_active'),
         coalesce(r.write_backend, 'sqlite')
  FROM em_control.console_users u
  LEFT JOIN em_control.console_library_sources s ON s.user_id = u.user_id
  LEFT JOIN em_control.libraries l ON l.library_id = s.experience_library_id
  LEFT JOIN em_control.library_routes r ON r.library_id = l.library_id
  ORDER BY u.user_id;
END $fn$;

CREATE FUNCTION em_control.console_get_pending(p_expected_owner text)
RETURNS TABLE(open_id text, name text, first_seen text, last_seen text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
BEGIN
  PERFORM em_control._require_service('read');
  PERFORM em_control._require_console_owner(p_expected_owner);
  RETURN QUERY
  SELECT p.open_id, p.name,
         to_char(p.first_seen AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"+00:00"'),
         to_char(p.last_seen AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"+00:00"')
  FROM em_control.console_pending p
  ORDER BY p.first_seen, p.open_id;
END $fn$;

CREATE FUNCTION em_control.console_get_grants(p_expected_owner text)
RETURNS TABLE(viewer_id text, owner_id text, history_read boolean, experience_read boolean, revision bigint)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
BEGIN
  PERFORM em_control._require_service('read');
  PERFORM em_control._require_console_owner(p_expected_owner);
  RETURN QUERY
  SELECT g.viewer_id, g.owner_id, g.history_read, g.experience_read, g.revision
  FROM em_control.console_grants g;
END $fn$;

CREATE FUNCTION em_control.console_upsert_user(
    p_expected_owner text, p_actor_id text, p_target_id text, p_principal_id uuid,
    p_display_name text, p_open_id text, p_enabled boolean, p_expected_revision bigint, p_create boolean
) RETURNS TABLE(
    user_id text, display_name text, enabled boolean, is_admin boolean, open_id text, lan_enabled boolean, revision bigint
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
DECLARE old_row em_control.console_users; cur_row em_control.console_users;
BEGIN
  PERFORM em_control._require_service('coordinate');
  PERFORM em_control._require_console_owner(p_expected_owner);
  SELECT * INTO old_row FROM em_control.console_users u WHERE u.user_id = p_target_id FOR UPDATE;
  IF p_create AND FOUND THEN RAISE EXCEPTION USING ERRCODE = '23505', MESSAGE = 'account_exists'; END IF;
  IF NOT p_create AND NOT FOUND THEN RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'account_not_found'; END IF;
  IF NOT p_create AND p_expected_revision IS NOT NULL AND old_row.revision <> p_expected_revision THEN
    RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'revision_conflict';
  END IF;
  IF NOT p_create AND p_target_id = p_expected_owner
     AND (NOT p_enabled OR p_open_id IS DISTINCT FROM old_row.open_id) THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'configured_owner_immutable';
  END IF;
  IF p_open_id IS NOT NULL THEN
    IF EXISTS (SELECT 1 FROM em_control.console_users u WHERE u.open_id = p_open_id AND u.user_id <> p_target_id) THEN
      RAISE EXCEPTION USING ERRCODE = '23505', MESSAGE = 'login_already_bound';
    END IF;
    IF (p_create OR p_open_id IS DISTINCT FROM old_row.open_id)
       AND NOT EXISTS (SELECT 1 FROM em_control.console_pending p WHERE p.open_id = p_open_id) THEN
      RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'login_not_observed';
    END IF;
  END IF;
  IF NOT p_create AND old_row.open_id IS NOT NULL AND (NOT p_enabled OR old_row.open_id IS DISTINCT FROM p_open_id) THEN
    DELETE FROM em_control.web_sessions w WHERE w.open_id = old_row.open_id;
  END IF;
  IF NOT p_enabled AND p_open_id IS NOT NULL THEN
    DELETE FROM em_control.web_sessions w WHERE w.open_id = p_open_id;
  END IF;
  IF p_create THEN
    INSERT INTO em_control.principals(principal_id, display_name, active, credential_epoch)
    VALUES (p_principal_id, p_display_name, p_enabled, 1);
    INSERT INTO em_control.console_users(
        user_id, principal_id, display_name, enabled, is_admin, open_id, lan_enabled, revision
    ) VALUES (p_target_id, p_principal_id, p_display_name, p_enabled, false, p_open_id, false, 1)
    RETURNING * INTO cur_row;
    INSERT INTO em_control.console_library_sources(user_id) VALUES (p_target_id);
  ELSE
    UPDATE em_control.principals
    SET display_name = p_display_name, active = p_enabled,
        credential_epoch = CASE WHEN NOT p_enabled AND old_row.enabled THEN credential_epoch + 1 ELSE credential_epoch END
    WHERE principal_id = old_row.principal_id;
    IF NOT p_enabled THEN
      UPDATE em_control.credentials SET active = false, revoked_at = coalesce(revoked_at, clock_timestamp())
      WHERE principal_id = old_row.principal_id AND active;
    END IF;
    UPDATE em_control.console_users
    SET display_name = p_display_name, enabled = p_enabled, open_id = p_open_id,
        revision = old_row.revision + 1, updated_at = clock_timestamp()
    WHERE em_control.console_users.user_id = p_target_id
    RETURNING * INTO cur_row;
  END IF;
  IF p_open_id IS NOT NULL THEN
    DELETE FROM em_control.console_pending p WHERE p.open_id = p_open_id;
  END IF;
  INSERT INTO em_control.console_audit(actor_id, action, target_id, before_json, after_json, outcome)
  VALUES (
      p_actor_id, CASE WHEN p_create THEN 'account.create' ELSE 'account.update' END, p_target_id,
      CASE WHEN p_create THEN '{}'::jsonb ELSE jsonb_build_object('enabled', old_row.enabled, 'revision', old_row.revision) END,
      jsonb_build_object('enabled', cur_row.enabled, 'revision', cur_row.revision), 'allowed'
  );
  RETURN QUERY SELECT cur_row.user_id, cur_row.display_name, cur_row.enabled, cur_row.is_admin,
                      cur_row.open_id, cur_row.lan_enabled, cur_row.revision;
END $fn$;

CREATE FUNCTION em_control.console_configure_library(
    p_expected_owner text, p_actor_id text, p_user_id text,
    p_exp_lib_id uuid, p_hist_lib_id uuid, p_data_dir text, p_history_environ jsonb
) RETURNS boolean
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
DECLARE u_row em_control.console_users; s_row em_control.console_library_sources;
        v_exp uuid; v_hist uuid;
BEGIN
  PERFORM em_control._require_service('coordinate');
  PERFORM em_control._require_console_owner(p_expected_owner);
  SELECT * INTO u_row FROM em_control.console_users WHERE user_id = p_user_id FOR UPDATE;
  IF NOT FOUND THEN RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'unknown_user'; END IF;
  SELECT * INTO s_row FROM em_control.console_library_sources WHERE user_id = p_user_id FOR UPDATE;
  v_exp := coalesce(s_row.experience_library_id, p_exp_lib_id);
  v_hist := coalesce(s_row.history_library_id, p_hist_lib_id);
  IF p_data_dir IS NOT NULL AND length(p_data_dir) > 0 THEN
    INSERT INTO em_control.libraries(library_id, kind, owner_principal_id, backend, connection_ref, schema_version, active)
    VALUES (v_exp, 'legacy', u_row.principal_id, 'sqlite', 'sqlite.exp.' || p_user_id, 1, true)
    ON CONFLICT (library_id) DO NOTHING;
    INSERT INTO em_control.library_routes(library_id, route_epoch, policy_epoch, authority_state, write_backend, acl_state)
    VALUES (v_exp, 1, 1, 'sqlite_active', 'sqlite', 'ready') ON CONFLICT (library_id) DO NOTHING;
    INSERT INTO em_control.library_acl(library_id, principal_id, permission, active, policy_epoch, granted_by)
    VALUES (v_exp, u_row.principal_id, 'admin', true, 1, u_row.principal_id)
    ON CONFLICT (library_id, principal_id) DO NOTHING;
    UPDATE em_control.console_users SET default_write_library_id = v_exp, updated_at = clock_timestamp()
    WHERE user_id = p_user_id;
  END IF;
  IF p_history_environ ? 'EVOLVMEM_HISTORY_CANDIDATE_DB' THEN
    INSERT INTO em_control.libraries(library_id, kind, owner_principal_id, backend, connection_ref, schema_version, active)
    VALUES (v_hist, 'legacy', u_row.principal_id, 'sqlite', 'sqlite.hist.' || p_user_id, 1, true)
    ON CONFLICT (library_id) DO NOTHING;
    INSERT INTO em_control.library_routes(library_id, route_epoch, policy_epoch, authority_state, write_backend, acl_state)
    VALUES (v_hist, 1, 1, 'sqlite_active', 'sqlite', 'ready') ON CONFLICT (library_id) DO NOTHING;
    INSERT INTO em_control.library_acl(library_id, principal_id, permission, active, policy_epoch, granted_by)
    VALUES (v_hist, u_row.principal_id, 'admin', true, 1, u_row.principal_id)
    ON CONFLICT (library_id, principal_id) DO NOTHING;
  END IF;
  UPDATE em_control.console_library_sources
  SET experience_library_id = CASE WHEN p_data_dir IS NOT NULL AND length(p_data_dir) > 0 THEN v_exp ELSE experience_library_id END,
      history_library_id = CASE WHEN p_history_environ ? 'EVOLVMEM_HISTORY_CANDIDATE_DB' THEN v_hist ELSE history_library_id END,
      data_dir = p_data_dir, history_environ = coalesce(p_history_environ, '{}'::jsonb)
  WHERE user_id = p_user_id;
  INSERT INTO em_control.console_audit(actor_id, action, target_id, after_json, outcome)
  VALUES (p_actor_id, 'library.configure', p_user_id,
          jsonb_build_object('experience_configured', p_data_dir IS NOT NULL, 'history_configured', p_history_environ ? 'EVOLVMEM_HISTORY_CANDIDATE_DB'),
          'allowed');
  RETURN true;
END $fn$;

CREATE FUNCTION em_control.console_set_grant(
    p_expected_owner text, p_actor_id text, p_viewer_id text, p_owner_id text,
    p_history_read boolean, p_experience_read boolean, p_expected_revision bigint
) RETURNS bigint
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
DECLARE v_viewer em_control.console_users; v_owner em_control.console_users;
        v_src em_control.console_library_sources; v_old em_control.console_grants;
        v_next bigint; v_ep bigint;
BEGIN
  PERFORM em_control._require_service('coordinate');
  PERFORM em_control._require_console_owner(p_expected_owner);
  IF p_viewer_id = p_owner_id THEN RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'self_access_immutable'; END IF;
  SELECT * INTO v_viewer FROM em_control.console_users WHERE user_id = p_viewer_id FOR SHARE;
  SELECT * INTO v_owner FROM em_control.console_users WHERE user_id = p_owner_id FOR SHARE;
  IF v_viewer.user_id IS NULL OR v_owner.user_id IS NULL THEN
    RAISE EXCEPTION USING ERRCODE = 'P0002', MESSAGE = 'account_not_found';
  END IF;
  SELECT * INTO v_old FROM em_control.console_grants
  WHERE viewer_id = p_viewer_id AND owner_id = p_owner_id FOR UPDATE;
  IF coalesce(v_old.revision, 0) <> p_expected_revision THEN
    RAISE EXCEPTION USING ERRCODE = '40001', MESSAGE = 'revision_conflict';
  END IF;
  v_next := p_expected_revision + 1;
  INSERT INTO em_control.console_grants(viewer_id, owner_id, history_read, experience_read, revision)
  VALUES (p_viewer_id, p_owner_id, p_history_read, p_experience_read, v_next)
  ON CONFLICT (viewer_id, owner_id) DO UPDATE
  SET history_read = EXCLUDED.history_read, experience_read = EXCLUDED.experience_read, revision = EXCLUDED.revision;
  SELECT * INTO v_src FROM em_control.console_library_sources WHERE user_id = p_owner_id;
  IF v_src.experience_library_id IS NOT NULL THEN
    SELECT policy_epoch INTO v_ep FROM em_control.library_routes WHERE library_id = v_src.experience_library_id;
    INSERT INTO em_control.library_acl(library_id, principal_id, permission, active, policy_epoch, granted_by)
    VALUES (v_src.experience_library_id, v_viewer.principal_id, 'read', p_experience_read, coalesce(v_ep, 1), v_owner.principal_id)
    ON CONFLICT (library_id, principal_id) DO UPDATE SET active = EXCLUDED.active, policy_epoch = EXCLUDED.policy_epoch;
  END IF;
  IF v_src.history_library_id IS NOT NULL THEN
    SELECT policy_epoch INTO v_ep FROM em_control.library_routes WHERE library_id = v_src.history_library_id;
    INSERT INTO em_control.library_acl(library_id, principal_id, permission, active, policy_epoch, granted_by)
    VALUES (v_src.history_library_id, v_viewer.principal_id, 'read', p_history_read, coalesce(v_ep, 1), v_owner.principal_id)
    ON CONFLICT (library_id, principal_id) DO UPDATE SET active = EXCLUDED.active, policy_epoch = EXCLUDED.policy_epoch;
  END IF;
  INSERT INTO em_control.console_audit(actor_id, action, target_id, after_json, outcome)
  VALUES (p_actor_id, 'grant.update', p_owner_id,
          jsonb_build_object('viewer_id', p_viewer_id, 'history_read', p_history_read, 'experience_read', p_experience_read, 'revision', v_next),
          'allowed');
  RETURN v_next;
END $fn$;

CREATE FUNCTION em_control.console_record_audit(
    p_actor_id text, p_action text, p_target_id text, p_before jsonb, p_after jsonb, p_outcome text
) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
BEGIN
  PERFORM em_control._require_service('coordinate');
  INSERT INTO em_control.console_audit(actor_id, action, target_id, before_json, after_json, outcome)
  VALUES (coalesce(p_actor_id, ''), p_action, coalesce(p_target_id, ''),
          coalesce(p_before, '{}'::jsonb), coalesce(p_after, '{}'::jsonb), p_outcome);
END $fn$;

CREATE FUNCTION em_control.web_session_put(
    p_expected_owner text, p_console_mode boolean, p_token_hash text, p_auth_scope text,
    p_open_id text, p_name text, p_csrf text, p_expires_at timestamptz
) RETURNS void
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
DECLARE old_row em_control.web_sessions; v_allowed boolean;
BEGIN
  PERFORM em_control._require_service('coordinate');
  IF p_console_mode THEN
    PERFORM em_control._require_console_owner(p_expected_owner);
    SELECT u.enabled INTO v_allowed FROM em_control.console_users u WHERE u.open_id = p_open_id FOR SHARE;
    IF NOT FOUND THEN
      v_allowed := EXISTS (SELECT 1 FROM em_control.console_pending p WHERE p.open_id = p_open_id FOR SHARE);
    END IF;
    IF NOT coalesce(v_allowed, false) THEN
      RAISE EXCEPTION USING ERRCODE = '42501', MESSAGE = 'session_identity_rejected';
    END IF;
  END IF;
  SELECT * INTO old_row FROM em_control.web_sessions w WHERE w.token_hash = p_token_hash FOR UPDATE;
  IF FOUND THEN
    IF old_row.auth_scope <> p_auth_scope OR old_row.open_id <> p_open_id
       OR old_row.name <> p_name OR old_row.csrf <> p_csrf OR old_row.expires_at <> p_expires_at THEN
      RAISE EXCEPTION USING ERRCODE = '23505', MESSAGE = 'session_token_conflict';
    END IF;
    RETURN;
  END IF;
  INSERT INTO em_control.web_sessions(token_hash, auth_scope, open_id, name, csrf, expires_at)
  VALUES (p_token_hash, p_auth_scope, p_open_id, p_name, p_csrf, p_expires_at);
END $fn$;

CREATE FUNCTION em_control.web_session_get(p_token_hash text, p_auth_scope text)
RETURNS TABLE(open_id text, name text, csrf text, expires_epoch double precision)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
BEGIN
  PERFORM em_control._require_service('read');
  RETURN QUERY
  SELECT w.open_id, w.name, w.csrf, extract(epoch FROM w.expires_at)::double precision
  FROM em_control.web_sessions w
  WHERE w.token_hash = p_token_hash AND w.auth_scope = p_auth_scope
    AND w.expires_at > clock_timestamp();
END $fn$;

CREATE FUNCTION em_control.web_session_pop(p_token_hash text, p_auth_scope text)
RETURNS TABLE(open_id text, name text, csrf text, expires_epoch double precision)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
DECLARE old_row em_control.web_sessions; v_now timestamptz;
BEGIN
  PERFORM em_control._require_service('coordinate');
  v_now := clock_timestamp();
  SELECT * INTO old_row FROM em_control.web_sessions w
  WHERE w.token_hash = p_token_hash AND w.auth_scope = p_auth_scope FOR UPDATE;
  DELETE FROM em_control.web_sessions w WHERE w.token_hash = p_token_hash;
  DELETE FROM em_control.web_sessions w WHERE w.expires_at <= v_now;
  IF old_row.token_hash IS NOT NULL AND old_row.expires_at > v_now THEN
    RETURN QUERY SELECT old_row.open_id, old_row.name, old_row.csrf,
                        extract(epoch FROM old_row.expires_at)::double precision;
  END IF;
END $fn$;

CREATE FUNCTION em_control.web_session_prune(p_before timestamptz) RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
DECLARE v_count integer;
BEGIN
  PERFORM em_control._require_service('coordinate');
  DELETE FROM em_control.web_sessions w WHERE w.expires_at <= coalesce(p_before, clock_timestamp());
  GET DIAGNOSTICS v_count = ROW_COUNT;
  RETURN v_count;
END $fn$;

CREATE FUNCTION em_control.web_session_revoke_open_id(p_open_id text) RETURNS integer
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
DECLARE v_count integer;
BEGIN
  PERFORM em_control._require_service('coordinate');
  DELETE FROM em_control.web_sessions w WHERE w.open_id = p_open_id;
  GET DIAGNOSTICS v_count = ROW_COUNT;
  RETURN v_count;
END $fn$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA em_control FROM PUBLIC;
INSERT INTO em_control.schema_migrations(version, migration_name) VALUES (7, '007_console_control_plane');
COMMIT;

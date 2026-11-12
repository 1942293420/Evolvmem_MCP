-- Append-only control migration v8: Agent/model attribution registry (Task 1.2)
-- and rolling model reputation table (Task 1.3).
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
  IF NOT EXISTS (SELECT 1 FROM em_control.schema_migrations WHERE version = 7)
     OR EXISTS (SELECT 1 FROM em_control.schema_migrations WHERE version >= 8) THEN
    RAISE EXCEPTION 'invalid_agent_reputation_schema_version';
  END IF;
END $guard$;

CREATE TABLE em_control.agents (
    agent_id text PRIMARY KEY CHECK (length(agent_id) BETWEEN 1 AND 128 AND agent_id !~ '[[:space:][:cntrl:]]'),
    device_id uuid REFERENCES em_control.devices(device_id),
    client text NOT NULL CHECK (length(client) BETWEEN 1 AND 64 AND client ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
    model text NOT NULL CHECK (length(model) BETWEEN 1 AND 128 AND model !~ '[[:cntrl:]]'),
    first_seen timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX agents_device_id_idx ON em_control.agents(device_id) WHERE device_id IS NOT NULL;
CREATE INDEX agents_client_model_idx ON em_control.agents(client, model);

CREATE TABLE em_control.model_reputation (
    agent_client text NOT NULL CHECK (length(agent_client) BETWEEN 1 AND 64 AND agent_client ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
    model text NOT NULL CHECK (length(model) BETWEEN 1 AND 128 AND model !~ '[[:cntrl:]]'),
    tier text NOT NULL CHECK (tier IN ('user', 'primary', 'general', 'judge')),
    quota_per_session integer NOT NULL CHECK (quota_per_session >= 0),
    proposed bigint NOT NULL DEFAULT 0 CHECK (proposed >= 0),
    accepted bigint NOT NULL DEFAULT 0 CHECK (accepted >= 0),
    rejected bigint NOT NULL DEFAULT 0 CHECK (rejected >= 0),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (agent_client, model)
);

ALTER TABLE em_control.agents ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.agents FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.agents TO :"owner_role" USING (true) WITH CHECK (true);

ALTER TABLE em_control.model_reputation ENABLE ROW LEVEL SECURITY;
ALTER TABLE em_control.model_reputation FORCE ROW LEVEL SECURITY;
CREATE POLICY owner_only ON em_control.model_reputation TO :"owner_role" USING (true) WITH CHECK (true);

REVOKE ALL ON ALL TABLES IN SCHEMA em_control FROM PUBLIC;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA em_control FROM PUBLIC;

CREATE FUNCTION em_control.upsert_agent_seen(
    p_agent_id text, p_device_id uuid, p_client text, p_model text
) RETURNS TABLE(
    agent_id text, device_id uuid, client text, model text, first_seen timestamptz, last_seen timestamptz
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
DECLARE v_now timestamptz;
BEGIN
  PERFORM em_control._require_service('coordinate');
  v_now := clock_timestamp();
  RETURN QUERY
  INSERT INTO em_control.agents(agent_id, device_id, client, model, first_seen, last_seen)
  VALUES (p_agent_id, p_device_id, p_client, p_model, v_now, v_now)
  ON CONFLICT ON CONSTRAINT agents_pkey DO UPDATE
  SET device_id = coalesce(EXCLUDED.device_id, em_control.agents.device_id),
      client = EXCLUDED.client,
      model = EXCLUDED.model,
      last_seen = v_now
  RETURNING em_control.agents.agent_id, em_control.agents.device_id,
            em_control.agents.client, em_control.agents.model,
            em_control.agents.first_seen, em_control.agents.last_seen;
END $fn$;

CREATE FUNCTION em_control.get_agent(p_agent_id text)
RETURNS TABLE(
    agent_id text, device_id uuid, client text, model text, first_seen timestamptz, last_seen timestamptz
)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
BEGIN
  PERFORM em_control._require_service('read');
  RETURN QUERY
  SELECT a.agent_id, a.device_id, a.client, a.model, a.first_seen, a.last_seen
  FROM em_control.agents a WHERE a.agent_id = p_agent_id;
END $fn$;

CREATE FUNCTION em_control.list_agents_for_device(p_device_id uuid)
RETURNS TABLE(
    agent_id text, device_id uuid, client text, model text, first_seen timestamptz, last_seen timestamptz
)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
BEGIN
  PERFORM em_control._require_service('read');
  RETURN QUERY
  SELECT a.agent_id, a.device_id, a.client, a.model, a.first_seen, a.last_seen
  FROM em_control.agents a WHERE a.device_id = p_device_id
  ORDER BY a.first_seen, a.agent_id;
END $fn$;

CREATE FUNCTION em_control.upsert_model_reputation_policy(
    p_agent_client text, p_model text, p_tier text, p_quota_per_session integer
) RETURNS TABLE(
    agent_client text, model text, tier text, quota_per_session integer,
    proposed bigint, accepted bigint, rejected bigint, updated_at timestamptz
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
BEGIN
  PERFORM em_control._require_service('coordinate');
  IF p_tier = 'user' THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'user_tier_reserved_for_human_auth';
  END IF;
  RETURN QUERY
  INSERT INTO em_control.model_reputation(agent_client, model, tier, quota_per_session, updated_at)
  VALUES (p_agent_client, p_model, p_tier, p_quota_per_session, clock_timestamp())
  ON CONFLICT ON CONSTRAINT model_reputation_pkey DO UPDATE
  SET tier = EXCLUDED.tier,
      quota_per_session = EXCLUDED.quota_per_session,
      updated_at = clock_timestamp()
  RETURNING em_control.model_reputation.agent_client, em_control.model_reputation.model,
            em_control.model_reputation.tier, em_control.model_reputation.quota_per_session,
            em_control.model_reputation.proposed, em_control.model_reputation.accepted,
            em_control.model_reputation.rejected, em_control.model_reputation.updated_at;
END $fn$;

CREATE FUNCTION em_control.record_model_reputation_event(
    p_agent_client text, p_model text, p_default_tier text, p_default_quota integer,
    p_delta_proposed integer, p_delta_accepted integer, p_delta_rejected integer
) RETURNS TABLE(
    agent_client text, model text, tier text, quota_per_session integer,
    proposed bigint, accepted bigint, rejected bigint, updated_at timestamptz
)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
BEGIN
  PERFORM em_control._require_service('coordinate');
  IF p_default_tier = 'user' THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'user_tier_reserved_for_human_auth';
  END IF;
  IF p_delta_proposed < 0 OR p_delta_accepted < 0 OR p_delta_rejected < 0 THEN
    RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'negative_reputation_delta_forbidden';
  END IF;
  RETURN QUERY
  INSERT INTO em_control.model_reputation(
      agent_client, model, tier, quota_per_session, proposed, accepted, rejected, updated_at
  ) VALUES (
      p_agent_client, p_model, p_default_tier, p_default_quota,
      p_delta_proposed, p_delta_accepted, p_delta_rejected, clock_timestamp()
  )
  ON CONFLICT ON CONSTRAINT model_reputation_pkey DO UPDATE
  SET proposed = em_control.model_reputation.proposed + p_delta_proposed,
      accepted = em_control.model_reputation.accepted + p_delta_accepted,
      rejected = em_control.model_reputation.rejected + p_delta_rejected,
      updated_at = clock_timestamp()
  RETURNING em_control.model_reputation.agent_client, em_control.model_reputation.model,
            em_control.model_reputation.tier, em_control.model_reputation.quota_per_session,
            em_control.model_reputation.proposed, em_control.model_reputation.accepted,
            em_control.model_reputation.rejected, em_control.model_reputation.updated_at;
END $fn$;

CREATE FUNCTION em_control.get_model_reputation(p_agent_client text, p_model text)
RETURNS TABLE(
    agent_client text, model text, tier text, quota_per_session integer,
    proposed bigint, accepted bigint, rejected bigint, updated_at timestamptz
)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, em_control AS $fn$
BEGIN
  PERFORM em_control._require_service('read');
  RETURN QUERY
  SELECT r.agent_client, r.model, r.tier, r.quota_per_session,
         r.proposed, r.accepted, r.rejected, r.updated_at
  FROM em_control.model_reputation r
  WHERE r.agent_client = p_agent_client AND r.model = p_model;
END $fn$;

REVOKE ALL ON ALL FUNCTIONS IN SCHEMA em_control FROM PUBLIC;
INSERT INTO em_control.schema_migrations(version, migration_name) VALUES (8, '008_agents_and_model_reputation');
COMMIT;

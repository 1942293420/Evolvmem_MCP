-- Separate append-only delta evidence. Execute only via the original bound
-- OperatorRuntime factory. No old import proof, binding or route is rewritten.
SELECT em_security.assert_history_operator_binding();
CREATE TABLE IF NOT EXISTS em_content.history_catchup_epochs(
 epoch_id uuid PRIMARY KEY, library_id uuid NOT NULL,
 plan_hash text NOT NULL CHECK(plan_hash ~ '^[0-9a-f]{64}$'),
 manifest jsonb NOT NULL, state text NOT NULL CHECK(state IN ('applying','verifying','complete')),
 completion_hash text CHECK(completion_hash ~ '^[0-9a-f]{64}$'),
 caught_up_to text NOT NULL, generation jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(), completed_at timestamptz,
 UNIQUE(library_id,plan_hash)
);
CREATE TABLE IF NOT EXISTS em_content.history_catchup_progress(
 epoch_id uuid PRIMARY KEY REFERENCES em_content.history_catchup_epochs(epoch_id),
 phase text NOT NULL CHECK(phase IN ('baseline','applying','verifying','ready','complete')),
 next_page integer NOT NULL DEFAULT 0 CHECK(next_page>=0),
 verify_table integer NOT NULL DEFAULT 0 CHECK(verify_table>=0),
 after_key jsonb, check_ordinal integer NOT NULL DEFAULT 0,
 generation jsonb NOT NULL, last_attempt uuid
);
CREATE TABLE IF NOT EXISTS em_content.history_catchup_pages(
 epoch_id uuid NOT NULL REFERENCES em_content.history_catchup_epochs(epoch_id),
 ordinal integer NOT NULL, attempt_id uuid NOT NULL,
 page_hash text NOT NULL CHECK(page_hash ~ '^[0-9a-f]{64}$'),
 row_count integer NOT NULL CHECK(row_count BETWEEN 1 AND 128),
 input_bytes integer NOT NULL CHECK(input_bytes BETWEEN 1 AND 16777216),
 before_generation jsonb NOT NULL, after_generation jsonb NOT NULL,
 committed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(epoch_id,ordinal), UNIQUE(epoch_id,attempt_id)
);
CREATE TABLE IF NOT EXISTS em_content.history_catchup_changes(
 epoch_id uuid NOT NULL, ordinal integer NOT NULL, change_ordinal integer NOT NULL,
 table_code text NOT NULL, source_key jsonb NOT NULL,
 operation text NOT NULL CHECK(operation IN ('insert','update','delete','delete_insert','restore')),
 before_hash text, after_hash text, before_row jsonb, after_row jsonb,
 -- canonical text additionally preserves numeric storage distinctions such as
 -- INTEGER 1 and REAL 1.0 that PostgreSQL JSONB equality itself collapses.
 before_typed_text text, after_typed_text text,
 PRIMARY KEY(epoch_id,ordinal,change_ordinal),
 FOREIGN KEY(epoch_id,ordinal) REFERENCES em_content.history_catchup_pages(epoch_id,ordinal)
    DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE IF NOT EXISTS em_content.history_catchup_checks(
 epoch_id uuid NOT NULL REFERENCES em_content.history_catchup_epochs(epoch_id),
 phase text NOT NULL CHECK(phase IN ('baseline','verifying')),
 ordinal integer NOT NULL, table_code text NOT NULL, after_key jsonb,
 row_count integer NOT NULL CHECK(row_count BETWEEN 0 AND 128),
 typed_hash text NOT NULL, keys_hash text NOT NULL, generation jsonb NOT NULL,
 PRIMARY KEY(epoch_id,phase,ordinal)
);
CREATE TABLE IF NOT EXISTS em_content.history_catchup_latest(
 library_id uuid PRIMARY KEY, epoch_id uuid NOT NULL REFERENCES em_content.history_catchup_epochs(epoch_id),
 completion_hash text NOT NULL CHECK(completion_hash ~ '^[0-9a-f]{64}$'),
 caught_up_to text NOT NULL, generation jsonb NOT NULL, plan_hash text NOT NULL
);
CREATE OR REPLACE FUNCTION em_content.history_catchup_no_rewrite()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog AS $fn$
BEGIN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='history_catchup_evidence_immutable'; END
$fn$;
REVOKE ALL ON FUNCTION em_content.history_catchup_no_rewrite() FROM PUBLIC;
DO $private$
DECLARE t text;
BEGIN
 FOREACH t IN ARRAY ARRAY['history_catchup_epochs','history_catchup_progress','history_catchup_pages',
   'history_catchup_changes','history_catchup_checks','history_catchup_latest'] LOOP
   EXECUTE format('ALTER TABLE em_content.%I ENABLE ROW LEVEL SECURITY',t);
   EXECUTE format('ALTER TABLE em_content.%I FORCE ROW LEVEL SECURITY',t);
   EXECUTE format('REVOKE ALL ON em_content.%I FROM PUBLIC',t);
   IF NOT EXISTS(SELECT 1 FROM pg_policy WHERE polrelid=('em_content.'||t)::regclass AND polname='history_catchup_owner') THEN
     EXECUTE format('CREATE POLICY history_catchup_owner ON em_content.%I TO %I USING(true) WITH CHECK(true)',t,current_user);
   END IF;
 END LOOP;
 FOREACH t IN ARRAY ARRAY['history_catchup_pages','history_catchup_changes','history_catchup_checks'] LOOP
   IF NOT EXISTS(SELECT 1 FROM pg_trigger WHERE tgrelid=('em_content.'||t)::regclass AND tgname='history_catchup_immutable') THEN
     EXECUTE format('CREATE TRIGGER history_catchup_immutable BEFORE UPDATE OR DELETE ON em_content.%I FOR EACH ROW EXECUTE FUNCTION em_content.history_catchup_no_rewrite()',t);
   END IF;
 END LOOP;
END $private$;

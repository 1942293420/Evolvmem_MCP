\set ON_ERROR_STOP on
-- Explicit owner compare-and-swap. Register first; then activate and page current
-- entities for every authorized project/index. No old job or vector is rewritten.
SELECT current_database()=:'database_name' AND current_user=:'owner_role' AS intended_owner \gset
\if :intended_owner
\else
 \quit 3
\endif
BEGIN;
SET LOCAL lock_timeout='3s';
SET LOCAL statement_timeout='5s';
SELECT em_security.activate_vector_model(:'library_id'::uuid,:'expected_model_contract',:'model_contract');
COMMIT;

-- Candidate only. Trusted psql variables: database_name owner_role coordinator_role.
\set ON_ERROR_STOP on
SELECT current_database()=:'database_name' AND current_user=:'owner_role'
 AND EXISTS(SELECT 1 FROM pg_roles r JOIN pg_database d ON d.datdba=r.oid
 WHERE r.rolname=current_user AND d.datname=current_database()
 AND NOT(r.rolcanlogin OR r.rolsuper OR r.rolbypassrls OR r.rolcreaterole OR r.rolcreatedb))
 AND EXISTS(SELECT 1 FROM em_control.service_roles WHERE login_role=:'coordinator_role' AND enabled AND can_coordinate)
 AS preflight_ok \gset
\if :preflight_ok
\else
 \quit 3
\endif
BEGIN;
SELECT pg_advisory_xact_lock(hashtextextended('evolvmem.control.schema',0));
DO $guard$ BEGIN
 IF NOT EXISTS(SELECT 1 FROM em_control.schema_migrations WHERE version=5)
 OR EXISTS(SELECT 1 FROM em_control.schema_migrations WHERE version>=6)
 OR EXISTS(SELECT 1 FROM em_control.acl_operations WHERE operation_state<>'complete')
 THEN RAISE EXCEPTION 'invalid_authority_control_schema'; END IF;
END $guard$;
CREATE TABLE em_control.authority_operations(
 operation_id uuid PRIMARY KEY,request_id text NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
 library_id uuid NOT NULL REFERENCES em_control.libraries(library_id), action text NOT NULL CHECK(action='activate'),
 actor_credential_id uuid NOT NULL REFERENCES em_control.credentials(credential_id),
 actor_principal_id uuid NOT NULL REFERENCES em_control.principals(principal_id),
 actor_device_id uuid NOT NULL REFERENCES em_control.devices(device_id),
 actor_credential_epoch bigint NOT NULL CHECK(actor_credential_epoch>0),
 actor_principal_epoch bigint NOT NULL CHECK(actor_principal_epoch>0),actor_device_epoch bigint NOT NULL CHECK(actor_device_epoch>0),
 connection_ref text NOT NULL,expected_route_epoch bigint NOT NULL CHECK(expected_route_epoch>0),
 expected_policy_epoch bigint NOT NULL CHECK(expected_policy_epoch>0),
 target_route_epoch bigint NOT NULL CHECK(target_route_epoch=expected_route_epoch+1),
 target_policy_epoch bigint NOT NULL CHECK(target_policy_epoch=expected_policy_epoch+1),
 payload_hash bytea NOT NULL CHECK(octet_length(payload_hash)=32),
 operation_state text NOT NULL CHECK(operation_state IN('pending','applying','complete','failed')),
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),completed_at timestamptz,
 UNIQUE(actor_principal_id,request_id)
);
CREATE UNIQUE INDEX authority_one_pending ON em_control.authority_operations(library_id) WHERE operation_state<>'complete';
CREATE TABLE em_control.authority_plans(
 operation_id uuid PRIMARY KEY REFERENCES em_control.authority_operations(operation_id),
 plan_json jsonb NOT NULL,plan_hash bytea NOT NULL CHECK(octet_length(plan_hash)=32),
 CHECK(sha256(convert_to(plan_json::text,'UTF8'))=plan_hash)
);
CREATE TABLE em_control.authority_receipts(
 operation_id uuid PRIMARY KEY REFERENCES em_control.authority_operations(operation_id),
 receipt_json jsonb NOT NULL,receipt_hash bytea NOT NULL CHECK(octet_length(receipt_hash)=32),
 confirm_txid bigint NOT NULL,confirmed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 CHECK(sha256(convert_to(receipt_json::text,'UTF8'))=receipt_hash)
);
CREATE TABLE em_control.authority_outbox(
 operation_id uuid NOT NULL REFERENCES em_control.authority_operations(operation_id),
 event text NOT NULL CHECK(event IN('intent','complete')),event_json jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT clock_timestamp(),PRIMARY KEY(operation_id,event)
);
DO $policies$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['authority_operations','authority_plans','authority_receipts','authority_outbox'] LOOP
 EXECUTE format('ALTER TABLE em_control.%I ENABLE ROW LEVEL SECURITY',t);
 EXECUTE format('ALTER TABLE em_control.%I FORCE ROW LEVEL SECURITY',t);
 EXECUTE format('CREATE POLICY owner_only ON em_control.%I TO %I USING(true) WITH CHECK(true)',t,current_user);
 EXECUTE format('REVOKE ALL ON em_control.%I FROM PUBLIC',t);
 END LOOP;
END $policies$;
CREATE FUNCTION em_control._authority_json(p_operation uuid) RETURNS jsonb
LANGUAGE sql STABLE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
 SELECT jsonb_build_object('version',1,'operation_id',o.operation_id,'request_id',o.request_id,
 'library_id',o.library_id,'action',o.action,'owner_principal_id',o.actor_principal_id,'kind','legacy','device_id',NULL,
 'connection_ref',o.connection_ref,'expected_route_epoch',o.expected_route_epoch,'expected_policy_epoch',o.expected_policy_epoch,
 'target_route_epoch',o.target_route_epoch,'target_policy_epoch',o.target_policy_epoch,'operation_state',o.operation_state,
 'payload_hash',encode(o.payload_hash,'hex'),'plan_hash',encode(p.plan_hash,'hex'),'receipt_hash',encode(r.receipt_hash,'hex'))
 FROM em_control.authority_operations o LEFT JOIN em_control.authority_plans p USING(operation_id)
 LEFT JOIN em_control.authority_receipts r USING(operation_id) WHERE o.operation_id=p_operation
$fn$;
CREATE FUNCTION em_control._lock_authority_operation(p_operation uuid) RETURNS em_control.authority_operations
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
DECLARE o em_control.authority_operations;
BEGIN
 PERFORM em_control._require_service('coordinate');
 SELECT * INTO o FROM em_control.authority_operations WHERE operation_id=p_operation;
 IF NOT FOUND THEN RAISE EXCEPTION 'authority_operation_unavailable'; END IF;
 -- Parent UPDATE locks close new credential/device FK phantoms; always identities first.
 PERFORM 1 FROM em_control.principals p WHERE p.principal_id=o.actor_principal_id OR p.principal_id IN
 (SELECT principal_id FROM em_control.library_acl WHERE library_id=o.library_id) ORDER BY p.principal_id FOR UPDATE;
 PERFORM 1 FROM em_control.devices d WHERE d.principal_id=o.actor_principal_id OR d.principal_id IN
 (SELECT principal_id FROM em_control.library_acl WHERE library_id=o.library_id) ORDER BY d.device_id FOR UPDATE;
 PERFORM 1 FROM em_control.credentials c WHERE c.principal_id=o.actor_principal_id OR c.principal_id IN
 (SELECT principal_id FROM em_control.library_acl WHERE library_id=o.library_id) ORDER BY c.credential_id FOR SHARE;
 PERFORM em_control._assert_identity(o.actor_credential_id,o.actor_principal_id,o.actor_device_id,
 o.actor_credential_epoch,o.actor_principal_epoch,o.actor_device_epoch);
 PERFORM 1 FROM em_control.libraries WHERE library_id=o.library_id FOR UPDATE;
 PERFORM 1 FROM em_control.library_routes WHERE library_id=o.library_id FOR UPDATE;
 PERFORM 1 FROM em_control.library_acl WHERE library_id=o.library_id ORDER BY principal_id FOR SHARE;
 PERFORM 1 FROM em_control.library_projects WHERE library_id=o.library_id ORDER BY project_id FOR SHARE;
 PERFORM 1 FROM em_control.library_project_acl WHERE library_id=o.library_id ORDER BY project_id,principal_id FOR SHARE;
 SELECT * INTO o FROM em_control.authority_operations WHERE operation_id=p_operation FOR UPDATE;
 IF NOT EXISTS(SELECT 1 FROM em_control.libraries l JOIN em_control.library_routes r USING(library_id)
 JOIN em_control.library_acl a ON a.library_id=l.library_id AND a.principal_id=o.actor_principal_id
 WHERE l.library_id=o.library_id AND l.kind='legacy' AND l.owner_principal_id=o.actor_principal_id
 AND l.device_id IS NULL AND l.project_id IS NULL AND l.backend='postgres' AND l.connection_ref=o.connection_ref
 AND a.active AND a.permission='admin' AND a.policy_epoch=r.policy_epoch
 AND ((o.operation_state='complete' AND l.active AND r.authority_state='pg_active' AND r.write_backend='postgres'
 AND r.acl_state='ready' AND r.route_epoch=o.target_route_epoch AND r.policy_epoch=o.target_policy_epoch)
 OR(o.operation_state<>'complete' AND NOT l.active AND r.authority_state='freeze_and_reconcile'
 AND r.write_backend='none' AND r.acl_state='updating' AND r.route_epoch=o.expected_route_epoch AND r.policy_epoch=o.expected_policy_epoch)))
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='authority_fence_changed'; END IF;
 RETURN o;
END $fn$;
CREATE FUNCTION em_control.begin_authority_change(p_credential uuid,p_principal uuid,p_device uuid,
 p_credential_epoch bigint,p_principal_epoch bigint,p_device_epoch bigint,p_library uuid,p_action text,
 p_expected_route bigint,p_expected_policy bigint,p_connection_ref text,p_operation uuid,p_request_id text,p_payload_hash bytea)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
DECLARE o em_control.authority_operations;
BEGIN
 PERFORM em_control._require_service('coordinate');
 IF p_action IS DISTINCT FROM 'activate' OR p_library IS NULL OR p_operation IS NULL
 OR p_request_id IS NULL OR length(p_request_id) NOT BETWEEN 1 AND 128
 OR p_expected_route IS NULL OR p_expected_route NOT BETWEEN 1 AND 9223372036854775806
 OR p_expected_policy IS NULL OR p_expected_policy NOT BETWEEN 1 AND 9223372036854775806
 OR p_connection_ref IS NULL OR p_connection_ref!~'^[A-Za-z][A-Za-z0-9_.-]{0,127}$'
 OR p_payload_hash IS NULL OR octet_length(p_payload_hash)<>32 THEN RAISE EXCEPTION 'invalid_authority_intent'; END IF;
 PERFORM 1 FROM em_control.principals WHERE principal_id=p_principal OR principal_id IN
 (SELECT principal_id FROM em_control.library_acl WHERE library_id=p_library) ORDER BY principal_id FOR UPDATE;
 PERFORM 1 FROM em_control.devices WHERE principal_id=p_principal OR principal_id IN
 (SELECT principal_id FROM em_control.library_acl WHERE library_id=p_library) ORDER BY device_id FOR UPDATE;
 PERFORM 1 FROM em_control.credentials WHERE principal_id=p_principal OR principal_id IN
 (SELECT principal_id FROM em_control.library_acl WHERE library_id=p_library) ORDER BY credential_id FOR SHARE;
 PERFORM em_control._assert_identity(p_credential,p_principal,p_device,p_credential_epoch,p_principal_epoch,p_device_epoch);
 PERFORM 1 FROM em_control.libraries WHERE library_id=p_library FOR UPDATE;
 PERFORM 1 FROM em_control.library_routes WHERE library_id=p_library FOR UPDATE;
 SELECT * INTO o FROM em_control.authority_operations WHERE operation_id=p_operation OR
 (actor_principal_id=p_principal AND request_id=p_request_id) FOR UPDATE;
 IF FOUND THEN
 IF (o.operation_id,o.library_id,o.action,o.actor_credential_id,o.actor_principal_id,o.actor_device_id,
 o.actor_credential_epoch,o.actor_principal_epoch,o.actor_device_epoch,o.connection_ref,o.expected_route_epoch,o.expected_policy_epoch,o.request_id,o.payload_hash)
 IS DISTINCT FROM (p_operation,p_library,p_action,p_credential,p_principal,p_device,p_credential_epoch,p_principal_epoch,p_device_epoch,
 p_connection_ref,p_expected_route,p_expected_policy,p_request_id,p_payload_hash) THEN RAISE EXCEPTION 'authority_idempotency_conflict'; END IF;
 PERFORM em_control._lock_authority_operation(p_operation); RETURN em_control._authority_json(p_operation); END IF;
 IF NOT EXISTS(SELECT 1 FROM em_control.libraries l JOIN em_control.library_routes r USING(library_id)
 JOIN em_control.library_acl a ON a.library_id=l.library_id AND a.principal_id=p_principal
 WHERE l.library_id=p_library AND NOT l.active AND l.kind='legacy' AND l.owner_principal_id=p_principal
 AND l.device_id IS NULL AND l.project_id IS NULL AND l.backend='postgres' AND l.connection_ref=p_connection_ref
 AND r.route_epoch=p_expected_route AND r.policy_epoch=p_expected_policy AND r.authority_state='pg_staging'
 AND r.write_backend='none' AND r.acl_state='updating' AND a.active AND a.permission='admin' AND a.policy_epoch=p_expected_policy)
 OR EXISTS(SELECT 1 FROM em_control.acl_operations WHERE library_id=p_library AND operation_state<>'complete')
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='authority_initial_state_required'; END IF;
 INSERT INTO em_control.authority_operations VALUES(p_operation,p_request_id,p_library,p_action,p_credential,p_principal,p_device,
 p_credential_epoch,p_principal_epoch,p_device_epoch,p_connection_ref,p_expected_route,p_expected_policy,p_expected_route+1,
 p_expected_policy+1,p_payload_hash,'pending',clock_timestamp(),NULL);
 UPDATE em_control.library_routes SET authority_state='freeze_and_reconcile',write_backend='none',acl_state='updating' WHERE library_id=p_library;
 INSERT INTO em_control.authority_outbox(operation_id,event,event_json) VALUES(p_operation,'intent',em_control._authority_json(p_operation));
 RETURN em_control._authority_json(p_operation);
END $fn$;
CREATE FUNCTION em_control.inspect_authority_operation(p_credential uuid,p_principal uuid,p_device uuid,
 p_credential_epoch bigint,p_principal_epoch bigint,p_device_epoch bigint,p_operation uuid,p_payload_hash bytea)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
DECLARE o em_control.authority_operations;
BEGIN
 PERFORM em_control._require_service('coordinate');
 PERFORM em_control._assert_identity(p_credential,p_principal,p_device,p_credential_epoch,p_principal_epoch,p_device_epoch);
 SELECT * INTO o FROM em_control.authority_operations WHERE operation_id=p_operation;
 IF NOT FOUND THEN RAISE EXCEPTION 'authority_operation_unavailable'; END IF;
 -- Completed status is historical evidence, not a fresh authority/readiness claim.
 IF o.operation_state<>'complete' THEN o:=em_control._lock_authority_operation(p_operation); END IF;
 IF (o.actor_credential_id,o.actor_principal_id,o.actor_device_id,o.actor_credential_epoch,o.actor_principal_epoch,o.actor_device_epoch,o.payload_hash)
 IS DISTINCT FROM (p_credential,p_principal,p_device,p_credential_epoch,p_principal_epoch,p_device_epoch,p_payload_hash)
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='authority_actor_mismatch'; END IF;
 RETURN em_control._authority_json(p_operation);
END $fn$;

CREATE FUNCTION em_control.get_authority_projection_plan(p_operation uuid)
RETURNS TABLE(plan_text text,plan_hash_hex text)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
DECLARE o em_control.authority_operations;l em_control.libraries;acls jsonb;creds jsonb;projects jsonb;pacls jsonb;
 inner_plan jsonb;outer_plan jsonb;stored em_control.authority_plans;
BEGIN
 o:=em_control._lock_authority_operation(p_operation);
 IF o.operation_state='complete' THEN RAISE EXCEPTION 'authority_operation_not_pending'; END IF;
 SELECT * INTO l FROM em_control.libraries WHERE library_id=o.library_id;
 SELECT coalesce(jsonb_agg(jsonb_build_object('principal_id',a.principal_id,'permission',a.permission,
 'active',a.active,'principal_active',p.active,'principal_epoch',p.credential_epoch) ORDER BY a.principal_id),'[]'::jsonb)
 INTO acls FROM em_control.library_acl a JOIN em_control.principals p USING(principal_id) WHERE a.library_id=o.library_id;
 SELECT coalesce(jsonb_agg(jsonb_build_object('principal_id',c.principal_id,'credential_id',c.credential_id,
 'device_id',c.device_id,'credential_epoch',c.credential_epoch,'principal_epoch',p.credential_epoch,'device_epoch',d.device_epoch,
 'credential_active',c.active,'principal_active',p.active,'device_active',d.active,'expires_at',
 CASE WHEN c.expires_at IS NULL THEN NULL ELSE to_char(c.expires_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"') END)
 ORDER BY c.credential_id),'[]'::jsonb) INTO creds FROM em_control.credentials c JOIN em_control.principals p USING(principal_id)
 JOIN em_control.devices d ON d.device_id=c.device_id AND d.principal_id=c.principal_id
 WHERE c.principal_id IN(SELECT principal_id FROM em_control.library_acl WHERE library_id=o.library_id);
 SELECT coalesce(jsonb_agg(jsonb_build_object('project_id',x.project_id,'project_key',r.project_key,'active',x.active)
 ORDER BY x.project_id),'[]'::jsonb) INTO projects FROM em_control.library_projects x
 JOIN em_control.project_registry r USING(project_id) WHERE x.library_id=o.library_id;
 SELECT coalesce(jsonb_agg(jsonb_build_object('project_id',a.project_id,'principal_id',a.principal_id,
 'permission',a.permission,'active',a.active) ORDER BY a.project_id,a.principal_id),'[]'::jsonb)
 INTO pacls FROM em_control.library_project_acl a WHERE a.library_id=o.library_id;
 IF jsonb_array_length(acls)>256 OR jsonb_array_length(creds)>1024 OR jsonb_array_length(projects)>2048
 OR jsonb_array_length(pacls)>8192 THEN RAISE EXCEPTION 'authority_projection_too_large'; END IF;
 inner_plan:=jsonb_build_object('version',2,'operation_id',o.operation_id,'library_id',o.library_id,
 'connection_ref',o.connection_ref,'expected_policy_epoch',o.expected_policy_epoch,'target_policy_epoch',o.target_policy_epoch,
 'route_epoch',o.target_route_epoch,'kind',l.kind,'device_id',l.device_id,'project_id',l.project_id,'owner_principal_id',l.owner_principal_id,
 'payload_hash',encode(o.payload_hash,'hex'),'evaluation_at',to_char(o.created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
 'acls',acls,'credentials',creds,'projects',projects,'project_acls',pacls);
 outer_plan:=jsonb_build_object('version',1,'operation_id',o.operation_id,'library_id',o.library_id,'action',o.action,
 'connection_ref',o.connection_ref,'owner_principal_id',l.owner_principal_id,'kind',l.kind,'device_id',l.device_id,'project_id',l.project_id,
 'expected_route_epoch',o.expected_route_epoch,'expected_policy_epoch',o.expected_policy_epoch,
 'target_route_epoch',o.target_route_epoch,'target_policy_epoch',o.target_policy_epoch,'payload_hash',encode(o.payload_hash,'hex'),
 'evaluation_at',inner_plan->>'evaluation_at','acl_plan_text',inner_plan::text,'acl_plan_hash',encode(sha256(convert_to(inner_plan::text,'UTF8')),'hex'));
 IF octet_length(outer_plan::text)>2097152 THEN RAISE EXCEPTION 'authority_projection_too_large'; END IF;
 SELECT * INTO stored FROM em_control.authority_plans WHERE operation_id=p_operation;
 IF FOUND THEN
 IF stored.plan_json IS DISTINCT FROM outer_plan THEN RAISE EXCEPTION 'authority_projection_changed'; END IF;
 ELSE INSERT INTO em_control.authority_plans VALUES(p_operation,outer_plan,sha256(convert_to(outer_plan::text,'UTF8'))); END IF;
 RETURN QUERY SELECT outer_plan::text,encode(sha256(convert_to(outer_plan::text,'UTF8')),'hex');
END $fn$;
CREATE FUNCTION em_control.confirm_authority_projection(p_operation uuid,p_plan_hash bytea,p_receipt_text text,p_receipt_hash bytea)
RETURNS jsonb LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
DECLARE o em_control.authority_operations;p em_control.authority_plans;v jsonb;r jsonb;prior em_control.authority_receipts;
BEGIN
 o:=em_control._lock_authority_operation(p_operation);
 -- Recompute under these locks even if a service skipped the explicit get-plan call.
 IF o.operation_state<>'complete' THEN PERFORM em_control.get_authority_projection_plan(p_operation); END IF;
 SELECT * INTO p FROM em_control.authority_plans WHERE operation_id=p_operation;
 IF NOT FOUND OR p.plan_hash IS DISTINCT FROM p_plan_hash OR p_receipt_text IS NULL OR octet_length(p_receipt_text)>2097152
 OR p_receipt_hash IS NULL OR octet_length(p_receipt_hash)<>32 OR sha256(convert_to(p_receipt_text,'UTF8'))<>p_receipt_hash
 THEN RAISE EXCEPTION 'authority_receipt_hash_invalid'; END IF;
 v:=p_receipt_text::jsonb;r:=v->'readiness';
 IF jsonb_typeof(v) IS DISTINCT FROM 'object' OR v::text<>p_receipt_text
 OR NOT(v ?& ARRAY['version','operation_id','library_id','action','connection_ref','kind','owner_principal_id','device_id',
 'expected_route_epoch','expected_policy_epoch','target_route_epoch','target_policy_epoch','plan_hash','registry_hash','state_hash',
 'content_database','projector_login','readiness']) OR (SELECT count(*) FROM jsonb_object_keys(v))<>18
 OR EXISTS(SELECT 1 FROM jsonb_each(v) x WHERE x.key IN('version','expected_route_epoch','expected_policy_epoch','target_route_epoch','target_policy_epoch') AND jsonb_typeof(x.value)<>'number')
 OR EXISTS(SELECT 1 FROM jsonb_each(v) x WHERE x.key NOT IN('version','expected_route_epoch','expected_policy_epoch','target_route_epoch','target_policy_epoch','device_id','readiness') AND jsonb_typeof(x.value)<>'string')
 OR v->>'version' IS DISTINCT FROM '1' OR v->>'operation_id' IS DISTINCT FROM o.operation_id::text
 OR v->>'library_id' IS DISTINCT FROM o.library_id::text OR v->>'action' IS DISTINCT FROM 'activate'
 OR v->>'connection_ref' IS DISTINCT FROM o.connection_ref OR v->>'kind' IS DISTINCT FROM 'legacy'
 OR v->>'owner_principal_id' IS DISTINCT FROM o.actor_principal_id::text OR v->'device_id'<>'null'::jsonb
 OR (v->>'expected_route_epoch')::bigint IS DISTINCT FROM o.expected_route_epoch
 OR (v->>'expected_policy_epoch')::bigint IS DISTINCT FROM o.expected_policy_epoch
 OR (v->>'target_route_epoch')::bigint IS DISTINCT FROM o.target_route_epoch
 OR (v->>'target_policy_epoch')::bigint IS DISTINCT FROM o.target_policy_epoch
 OR v->>'plan_hash' IS DISTINCT FROM encode(p.plan_hash,'hex')
 OR coalesce(v->>'registry_hash','')!~'^[0-9a-f]{64}$' OR coalesce(v->>'state_hash','')!~'^[0-9a-f]{64}$'
 OR coalesce(v->>'content_database','')='' OR coalesce(v->>'projector_login','')=''
 OR jsonb_typeof(r) IS DISTINCT FROM 'object'
 OR NOT(r ?& ARRAY['kind','enabled','revision','policy_epoch','route_epoch','namespace','c_epoch','c_completion_hash',
 'c_source_seal','raw_generation','membership_revision','membership_hash','membership_c_epoch','membership_c_completion_hash'])
 OR (SELECT count(*) FROM jsonb_object_keys(r))<>14
 OR EXISTS(SELECT 1 FROM jsonb_each(r) x WHERE x.key IN('revision','policy_epoch','route_epoch','membership_revision') AND jsonb_typeof(x.value)<>'number')
 OR EXISTS(SELECT 1 FROM jsonb_each(r) x WHERE x.key NOT IN('enabled','revision','policy_epoch','route_epoch','membership_revision','raw_generation') AND jsonb_typeof(x.value)<>'string')
 OR r->>'kind' IS DISTINCT FROM 'history-preserved-v1'
 OR r->'enabled' IS DISTINCT FROM 'true'::jsonb OR (r->>'revision')::bigint IS NULL OR (r->>'revision')::bigint<1
 OR (r->>'policy_epoch')::bigint IS DISTINCT FROM o.target_policy_epoch OR (r->>'route_epoch')::bigint IS DISTINCT FROM o.target_route_epoch
 OR coalesce(r->>'namespace','') NOT IN('owner','kane','public') OR (r->>'c_epoch')::uuid IS NULL
 OR coalesce(r->>'c_completion_hash','')!~'^[0-9a-f]{64}$' OR coalesce(r->>'c_source_seal','')!~'^[0-9a-f]{64}$'
 OR (r->>'membership_revision')::bigint IS NULL OR (r->>'membership_revision')::bigint<1
 OR coalesce(r->>'membership_hash','')!~'^[0-9a-f]{64}$'
 OR r->>'membership_c_epoch' IS DISTINCT FROM r->>'c_epoch'
 OR r->>'membership_c_completion_hash' IS DISTINCT FROM r->>'c_completion_hash'
 OR jsonb_typeof(r->'raw_generation') IS DISTINCT FROM 'object'
 OR ((r->'raw_generation')-CASE WHEN r->>'namespace'='public' THEN ARRAY['core','sources','organization','governance','knowledge','capability','continuity','operations'] ELSE ARRAY['core','sources','organization','capture','governance','knowledge','capability','continuity','operations'] END)<>'{}'::jsonb
 OR NOT(r->'raw_generation' ?& CASE WHEN r->>'namespace'='public' THEN ARRAY['core','sources','organization','governance','knowledge','capability','continuity','operations'] ELSE ARRAY['core','sources','organization','capture','governance','knowledge','capability','continuity','operations'] END)
 OR EXISTS(SELECT 1 FROM jsonb_each(r->'raw_generation') g WHERE jsonb_typeof(g.value) IS DISTINCT FROM 'object'
 OR NOT(g.value ?& ARRAY['run_id','generation']) OR (SELECT count(*) FROM jsonb_object_keys(g.value))<>2
 OR jsonb_typeof(g.value->'run_id') IS DISTINCT FROM 'string' OR (g.value->>'run_id')::uuid IS NULL
 OR jsonb_typeof(g.value->'generation') IS DISTINCT FROM 'number' OR (g.value->>'generation')::bigint IS NULL OR (g.value->>'generation')::bigint<0)
 THEN RAISE EXCEPTION USING ERRCODE='42501',MESSAGE='authority_receipt_scope_invalid'; END IF;
 SELECT * INTO prior FROM em_control.authority_receipts WHERE operation_id=p_operation FOR UPDATE;
 IF FOUND THEN
 IF prior.receipt_json IS DISTINCT FROM v OR prior.receipt_hash IS DISTINCT FROM p_receipt_hash THEN RAISE EXCEPTION 'authority_receipt_conflict'; END IF;
 -- Confirm must be repeated under THIS fresh control lock window before finish.
 UPDATE em_control.authority_receipts SET confirm_txid=txid_current() WHERE operation_id=p_operation;
 ELSE INSERT INTO em_control.authority_receipts VALUES(p_operation,v,p_receipt_hash,txid_current(),clock_timestamp()); END IF;
 IF o.operation_state<>'complete' THEN UPDATE em_control.authority_operations SET operation_state='applying' WHERE operation_id=p_operation; END IF;
 RETURN em_control._authority_json(p_operation);
END $fn$;
CREATE FUNCTION em_control.finish_authority_change(p_operation uuid) RETURNS jsonb
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path=pg_catalog,em_control AS $fn$
DECLARE o em_control.authority_operations;r em_control.authority_receipts;
BEGIN
 o:=em_control._lock_authority_operation(p_operation);
 SELECT * INTO r FROM em_control.authority_receipts WHERE operation_id=p_operation;
 IF NOT FOUND OR r.confirm_txid<>txid_current() THEN RAISE EXCEPTION 'authority_fresh_confirm_required'; END IF;
 IF o.operation_state='complete' THEN RETURN em_control._authority_json(p_operation); END IF;
 IF o.operation_state<>'applying' THEN RAISE EXCEPTION 'authority_not_confirmed'; END IF;
 UPDATE em_control.library_acl SET policy_epoch=o.target_policy_epoch WHERE library_id=o.library_id;
 UPDATE em_control.library_project_acl SET policy_epoch=o.target_policy_epoch WHERE library_id=o.library_id;
 UPDATE em_control.library_routes SET route_epoch=o.target_route_epoch,policy_epoch=o.target_policy_epoch,
 authority_state='pg_active',write_backend='postgres',acl_state='ready' WHERE library_id=o.library_id;
 UPDATE em_control.libraries SET active=true WHERE library_id=o.library_id;
 UPDATE em_control.authority_operations SET operation_state='complete',completed_at=clock_timestamp() WHERE operation_id=p_operation;
 INSERT INTO em_control.authority_outbox(operation_id,event,event_json) VALUES(p_operation,'complete',em_control._authority_json(p_operation));
 RETURN em_control._authority_json(p_operation);
END $fn$;
-- No API accepts a caller-controlled receipt or flags as sufficient by itself:
-- only the isolated trusted coordinator receives EXECUTE; runtime receives none.
REVOKE ALL ON FUNCTION em_control._authority_json(uuid),em_control._lock_authority_operation(uuid),
 em_control.begin_authority_change(uuid,uuid,uuid,bigint,bigint,bigint,uuid,text,bigint,bigint,text,uuid,text,bytea),
 em_control.inspect_authority_operation(uuid,uuid,uuid,bigint,bigint,bigint,uuid,bytea),
 em_control.get_authority_projection_plan(uuid),em_control.confirm_authority_projection(uuid,bytea,text,bytea),
 em_control.finish_authority_change(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION em_control.begin_authority_change(uuid,uuid,uuid,bigint,bigint,bigint,uuid,text,bigint,bigint,text,uuid,text,bytea),
 em_control.inspect_authority_operation(uuid,uuid,uuid,bigint,bigint,bigint,uuid,bytea),em_control.get_authority_projection_plan(uuid),
 em_control.confirm_authority_projection(uuid,bytea,text,bytea),em_control.finish_authority_change(uuid) TO :"coordinator_role";
INSERT INTO em_control.schema_migrations(version,migration_name) VALUES(6,'006_authority_lifecycle');
COMMIT;

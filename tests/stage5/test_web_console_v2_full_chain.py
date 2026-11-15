"""End-to-end HTTP + PG integration tests for EvolvMem Web Console v2 and full interception trace."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import threading
import time
import uuid

import psycopg
import pytest

from evolvmem.experience_v2.control.agent_identity import AgentAttribution
from evolvmem.experience_v2.control.pg_console_store import PgConsoleStore, PgControlConfig
from evolvmem.experience_v2.control.pg_web_sessions import PgPersistentSessions
from evolvmem.experience_v2.control.reputation_policy import TrustedChannel
from evolvmem.experience_v2.gate.propose import ProposalService
from evolvmem.experience_v2.gate.review_inbox import ReviewInboxService
from evolvmem.experience_v2.interception_store import record_pipeline_interception, update_decision_meta
from evolvmem.experience_v2.schema import apply_experience_migrations
from evolvmem.experience_v2.writer import ExperienceWriter, Op, TicketIssuer
from evolvmem.history_pg.web_pg_runner import NullLegacyWebService
from evolvmem.knowledge_console import ConsoleService
from evolvmem.web_auth import AuthSettings, WebAuth
from evolvmem.web_server import make_http_server
from tests.stage1 import pg_stage1_harness
from tests.stage5.conftest import STAGE5_PG_CONTAINER, STAGE5_PG_PORT, STAGE5_PW_FILE

OWNER_OPEN_ID = 'ou_owner_v2_test'
PEER_OPEN_ID = 'ou_peer_v2_test'
OWNER_CSRF = 'csrf-owner-v2-token'
PEER_CSRF = 'csrf-peer-v2-token'


def _pg_dsn() -> str:
    pw = STAGE5_PW_FILE.read_text(encoding='utf-8').strip()
    return f'host=127.0.0.1 port={STAGE5_PG_PORT} dbname=postgres user=postgres password={pw}'


def _attr(client: str = 'codex', model: str = 'gpt-5.4') -> AgentAttribution:
    return AgentAttribution(actor='admin_owner', agent_id=f'{client}-linux-01', client=client, model=model)


def _http_req(
    port: int,
    method: str,
    path: str,
    *,
    cookie: str = '',
    csrf: str = '',
    body: dict | None = None,
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    headers: dict[str, str] = {}
    if cookie:
        headers['Cookie'] = f'evolvmem_session={cookie}'
    if csrf:
        headers['X-CSRF-Token'] = csrf
    payload: bytes | None = None
    if body is not None:
        headers['Content-Type'] = 'application/json; charset=utf-8'
        payload = json.dumps(body, ensure_ascii=False).encode('utf-8')
    try:
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        hdrs = {k.lower(): v for k, v in resp.getheaders()}
        return resp.status, hdrs, raw
    finally:
        conn.close()


def _seed_experience_data(conn, *, secret: bytes, owner_id: str) -> dict[str, str]:
    issuer = TicketIssuer(secret, gate_id='test_seed')
    writer = ExperienceWriter(secret)
    t1 = issuer.issue(proposal_uid='seed:act1', decision_id='seed:d_act1', lane='user_fast')
    res_act = writer.apply(
        conn,
        t1,
        Op(
            action='create',
            owner_id=owner_id,
            actor_id=owner_id,
            request_id='seed-active-0001',
            category='系统环境经验',
            kind='pitfall',
            problem='PG RLS 隔离库迁移必须导出无密码角色',
            solution='先执行 pg_dumpall --roles-only --no-role-passwords 再做恢复演练',
            reusable_experience='带有 RLS 策略的 PG 库备份必须包含 roles 脚本',
            origin='manual',
        ),
    )
    active_id = res_act['experience']['id']

    t2 = issuer.issue(proposal_uid='seed:ret1', decision_id='seed:d_ret1', lane='user_fast')
    res_ret = writer.apply(
        conn,
        t2,
        Op(
            action='create',
            owner_id=owner_id,
            actor_id=owner_id,
            request_id='seed-retire-src-0001',
            category='业务经验',
            kind='procedure',
            problem='旧版脚本占位经验即将退役',
            solution='保留此条后立刻执行 retire 操作用于验证 peer_readonly 隔离',
            reusable_experience='非 active 状态条目绝不可暴露给只读访客',
            origin='manual',
        ),
    )
    ret_id = res_ret['experience']['id']
    t3 = issuer.issue(proposal_uid='seed:ret2', decision_id='seed:d_ret2', lane='user_fast')
    writer.apply(
        conn,
        t3,
        Op(
            action='retire',
            owner_id=owner_id,
            actor_id=owner_id,
            request_id='seed-retire-do-0001',
            note_id=ret_id,
            expected_revision=1,
        ),
    )

    m_id = record_pipeline_interception(
        conn, owner_id=owner_id, track='live', session_id='sess-m-01',
        intercept_layer='Moments', reason_code='no_moments', detail='Pure casual chitchat skipped at 0-LLM stage',
        agent_client='codex', model='none',
    )
    ex_id = record_pipeline_interception(
        conn, owner_id=owner_id, track='live', session_id='sess-ex-01',
        intercept_layer='Extract', reason_code='extract_skipped', detail='Single-turn shell typo has low reuse',
        agent_client='codex', model='deepseek-chat',
        candidate_json={
            'category': '系统环境经验',
            'kind': 'pitfall',
            'problem': '容器 DNS 解析偶发超时需显式指定 --dns',
            'solution': '在 docker run 与 daemon.json 显式配置内网 DNS 服务器地址',
            'reusable_experience': '定位内网容器网络故障优先核对 resolv.conf 与宿主 DNS 转发',
        },
        evidence_json=[{'ref': 'archive:8801', 'quote': 'docker run failed resolving internal host'}],
    )
    record_pipeline_interception(
        conn, owner_id=owner_id, track='backfill', session_id='sess-q-01',
        intercept_layer='Quota', reason_code='quota_exceeded', detail='Daily backfill token budget hit cap',
        agent_client='claude', model='unknown-exp-model',
    )

    proposer = ProposalService()
    def _mk_prop(req: str, layer: str, verdict: str, rcode: str, state: str, meta: dict, *, track: str = 'live', target: str | None = None, model: str = 'gpt-5.4') -> str:
        p = proposer.propose(
            conn, owner_id=owner_id, actor_id=owner_id, channel=TrustedChannel.MCP_CLIENT,
            attribution=_attr(model=model),
            body={
                'op': 'add', 'request_id': req, 'source_track': track,
                'session_id': f'sess-{req}',
                'category': '前端经验' if layer == 'L2a' else '系统环境经验',
                'kind': 'pitfall',
                'problem': f'[{layer}] 验证问题 {req}',
                'solution': f'[{layer}] 验证解法 {req}',
                'reusable_experience': f'[{layer}] 可复用准则 {req}',
                'evidence': [{'ref': 'archive:8802', 'quote': f'quote {req}'}],
            },
            source_kind='pipeline',
        )
        uid = p['uid']
        did = f'dec-{req}'
        conn.execute(
            """
            INSERT INTO proposal_decisions (decision_id, proposal_uid, layer, verdict, reason_code, confidence, judge_model, detail, target_note_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (did, uid, layer, verdict, rcode, 0.91, model, f'detail {layer}', target),
        )
        update_decision_meta(conn, did, meta)
        conn.execute('UPDATE experience_proposals SET state = %s, merged_into_uid = %s WHERE uid = %s', (state, target, uid))
        return uid

    p_l0 = _mk_prop('prop-req-l0-001', 'L0', 'reject', 'missing_evidence', 'rejected', {'field_lengths': {'problem': 18, 'solution': 22, 'reusable_experience': 20}, 'limits': {'problem': 120, 'solution': 240, 'reusable_experience': 120}, 'evidence_count': 0})
    p_l1 = _mk_prop('prop-req-l1-001', 'L1', 'merge', 'semantic_duplicate', 'merged', {'cosine_similarity': 0.923, 'threshold': 0.88, 'nearest_note_id': active_id}, target=active_id)
    p_l2a = _mk_prop('prop-req-l2a-001', 'L2a', 'reject', 'flash_blind_solved', 'rejected', {'blind_prompt': '如何在 React Effect 中避免过期闭包?', 'flash_prediction': '使用 ref 或将依赖写入数组'})
    p_l2b = _mk_prop('prop-req-l2b-001', 'L2b', 'reject', 'strong_blind_solved', 'rejected', {'blind_prompt': 'PG 恢复 RLS 策略报错 role missing 如何修复?', 'model_prediction': '先运行 pg_dumpall --roles-only'})
    p_l3 = _mk_prop('prop-req-l3-001', 'L3', 'merge', 'challenger_trial', 'merged', {'relation': 'refines'}, target=active_id)
    conn.execute(
        """
        INSERT INTO challenger_trials (owner_id, challenger_note_id, incumbent_note_id, relation, proposal_uid, required_successes, success_count, status)
        VALUES (%s, %s, %s, 'refines', %s, 2, 1, 'trial')
        """,
        (owner_id, f'chal-{p_l3}', active_id, p_l3),
    )
    p_l4 = _mk_prop('prop-req-l4-001', 'L2b', 'pass', 'high_value', 'review', {'blind_prompt': 'l4 sample'})
    inbox_svc = ReviewInboxService(ticket_secret=secret, shadow_mode=True)
    inbox_svc.decide(conn, owner_id=owner_id, reviewer_id=owner_id, proposal_uid=p_l4, user_verdict='reject', user_reason='weak_evidence', note='L4 reject sample')

    p_live_pending = _mk_prop('prop-req-live-inbox-001', 'L2b', 'pass', 'novel_high_value', 'review', {'blind_prompt': 'blind live q'}, model='bad-roll-model')
    p_back_pending = _mk_prop('prop-req-back-inbox-001', 'L2b', 'pass', 'novel_backfill', 'review', {'blind_prompt': 'blind back q'}, track='backfill')
    conn.execute(
        """
        INSERT INTO llm_calls (track, stage, model, session_id, input_tokens, output_tokens, cost_estimate, outcome, proposal_uid, is_shadow)
        VALUES ('live', 'L2b', 'gpt-5.4', 'sess-ex-01', 1200, 180, 0.0024, 'reject', %s, false)
        """,
        (p_l2b,),
    )
    conn.execute(
        """
        INSERT INTO legacy_cleanup_registry (
            cleanup_id, owner_id, approved_by, approved_at, authorization_note,
            backup_archive_path, backup_sha256, backup_bytes, retain_until, deleted_total, migrated_keep_total
        ) VALUES ('clean-stage4-v2', %s, %s, now(), 'ok', '/tmp/b.tgz', 'sha', 1024, now() + interval '30 day', 9134, 707)
        """,
        (owner_id, owner_id),
    )
    conn.commit()
    return {
        'active_id': active_id,
        'retired_id': ret_id,
        'moments_id': m_id,
        'extract_id': ex_id,
        'prop_l0': p_l0,
        'prop_l1': p_l1,
        'prop_l2a': p_l2a,
        'prop_l2b': p_l2b,
        'prop_l3': p_l3,
        'prop_l4': p_l4,
        'prop_live_pending': p_live_pending,
        'prop_back_pending': p_back_pending,
    }


@pytest.fixture()
def v2_live_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(pg_stage1_harness, 'TEST_PG_PORT', STAGE5_PG_PORT)
    monkeypatch.setattr(pg_stage1_harness, 'TEST_PG_CONTAINER', STAGE5_PG_CONTAINER)
    monkeypatch.setattr(pg_stage1_harness, 'TEST_PG_PW_FILE', STAGE5_PW_FILE)

    ctrl_env = pg_stage1_harness.create_isolated_control_db(tmp_path, up_to_version=7)
    base_dsn = _pg_dsn()
    exp_schema = f's5v2_{uuid.uuid4().hex[:10]}'
    with psycopg.connect(base_dsn, autocommit=True) as admin:
        admin.execute('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public')
        admin.execute(f'CREATE SCHEMA "{exp_schema}"')

    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    secret = b'stage5-web-v2-test-ticket-secret-key'
    secret_file = tmp_path / 'ticket.secret'
    secret_file.write_bytes(secret)
    os.chmod(secret_file, 0o600)

    policy_file = tmp_path / 'product-policy.json'
    policy_file.write_text(
        json.dumps({
            'console_enabled': True,
            'legacy_web_enabled': False,
            'ai_processing_enabled': False,
            'experience_v2_enabled': True,
            'experience_pipeline_enabled': True,
            'experience_backfill_enabled': True,
            'history_pg_enabled': True,
            'experience_gate_mode': 'review',
        }),
        encoding='utf-8',
    )
    auth_file = tmp_path / 'web_auth.json'
    auth_file.write_text(
        json.dumps({
            'enabled': True,
            'app_id': 'cli_v2_test',
            'app_secret': 'sec_v2_test',
            'owner_open_id': OWNER_OPEN_ID,
            'redirect_uri': 'http://127.0.0.1:9377/auth/feishu/callback',
        }),
        encoding='utf-8',
    )

    monkeypatch.setenv('EVOLVMEM_CONTROL_BACKEND', 'postgres')
    monkeypatch.setenv('EVOLVMEM_CONSOLE_PG_CONFIG', str(ctrl_env.config_file))
    monkeypatch.setenv('EVOLVMEM_CONSOLE_OWNER_ID', 'admin_owner')
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_DSN', base_dsn)
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_SCHEMA', exp_schema)
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_TICKET_SECRET_FILE', str(secret_file))
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(policy_file))
    monkeypatch.setenv('EVOLVMEM_WEB_AUTH_FILE', str(auth_file))

    try:
        with psycopg.connect(base_dsn, options=f'-c search_path={exp_schema},public') as exp_conn:
            apply_experience_migrations(exp_conn)
            seeds = _seed_experience_data(exp_conn, secret=secret, owner_id='admin_owner')

        ctrl_cfg = PgControlConfig.from_file(ctrl_env.config_file)
        ctrl_store = PgConsoleStore(ctrl_cfg)
        svc = ConsoleService(tmp_path / 'unused.db', 'admin_owner', data_dir, tmp_path / 'lan', {}, pg_control=ctrl_store)
        svc.bootstrap(OWNER_OPEN_ID, 'OwnerMain')
        owner_sess = {'open_id': OWNER_OPEN_ID, 'name': 'OwnerMain', 'csrf': OWNER_CSRF}
        svc.observe_login(PEER_OPEN_ID, 'PeerViewer')
        st_u, _, _ = svc.dispatch('POST', '/users', {'id': 'peer_viewer', 'display_name': 'PeerViewer', 'open_id': PEER_OPEN_ID}, owner_sess)
        assert st_u == 201
        st_g, _, _ = svc.dispatch(
            'POST', '/grants',
            {'viewer_id': 'peer_viewer', 'owner_id': 'admin_owner', 'history_read': True, 'experience_read': True, 'revision': 0},
            owner_sess,
        )
        assert st_g == 200

        scope = WebAuth(AuthSettings.load(data_dir)).persistence_scope()
        sessions = PgPersistentSessions(ctrl_cfg, scope, expected_owner_id='admin_owner', console_mode=True)
        sessions.initialize()
        exp_ts = time.time() + 28800
        sessions.put('tok-owner-v2', {'open_id': OWNER_OPEN_ID, 'name': 'OwnerMain', 'csrf': OWNER_CSRF, 'expires': exp_ts})
        sessions.put('tok-peer-v2', {'open_id': PEER_OPEN_ID, 'name': 'PeerViewer', 'csrf': PEER_CSRF, 'expires': exp_ts})

        srv = make_http_server(NullLegacyWebService(data_dir, host='127.0.0.1', port=0), host='127.0.0.1', port=0)
        port = int(srv.server_address[1])
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            yield {'port': port, 'seeds': seeds, 'dsn': base_dsn, 'schema': exp_schema}
        finally:
            srv.shutdown()
            srv.server_close()
            t.join(timeout=3)
    finally:
        with psycopg.connect(base_dsn, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{exp_schema}" CASCADE')
        pg_stage1_harness.drop_isolated_db(
            ctrl_env.database, (ctrl_env.owner_role, ctrl_env.coordinator_role, ctrl_env.reader_role)
        )


def test_web_console_v2_static_api_interceptions_rescue_shadow_and_acl(v2_live_server: dict) -> None:
    port = v2_live_server['port']
    seeds = v2_live_server['seeds']

    # 1. Static / and /v2 routes: unauthenticated redirects to /login (302); / serves v2 + /v1 fallback
    st_anon_root, hdr_anon_root, _ = _http_req(port, 'GET', '/')
    assert st_anon_root == 302 and hdr_anon_root.get('location') == '/login'
    st_anon, _, _ = _http_req(port, 'GET', '/v2')
    assert st_anon == 302

    for static_path in ('/', '/v2'):
        st, hdrs, raw = _http_req(port, 'GET', static_path, cookie='tok-owner-v2')
        html = raw.decode('utf-8')
        assert st == 200 and '<base href="/v2/">' in html and 'EvolvMem Neural Matrix' in html
        assert hdrs.get('cache-control') == 'no-store' and hdrs.get('x-content-type-options') == 'nosniff'

    st_v1, _, raw_v1 = _http_req(port, 'GET', '/v1', cookie='tok-owner-v2')
    assert st_v1 == 200 and 'knowledge-console' in raw_v1.decode('utf-8')

    st, _, js_raw = _http_req(port, 'GET', '/v2/js/render-interceptions.js', cookie='tok-owner-v2')
    assert st == 200 and 'renderInterceptionsView' in js_raw.decode('utf-8')

    st, _, _ = _http_req(port, 'GET', '/v2/../pyproject.toml', cookie='tok-owner-v2')
    assert st in (403, 404)

    st, _, _ = _http_req(port, 'GET', '/api/console-v2/me')
    assert st == 401

    # 2. Owner bootstrap & history routes (including UTF-8/space project_id validation)
    from urllib.parse import quote
    for ep in ('/me', '/overview', '/pipeline', '/evolution-and-skills', '/control', '/history?view=projects', '/history?view=project&project_id=' + quote('海外仓  仓储费')):
        st, _, raw = _http_req(port, 'GET', f'/api/console-v2{ep}', cookie='tok-owner-v2')
        assert st == 200 and json.loads(raw)['ok'] is True

    # 3. 10-layer interception funnel + per-layer filter + L3 trace (meta_json + llm_calls)
    st, _, raw = _http_req(port, 'GET', '/api/console-v2/interceptions', cookie='tok-owner-v2')
    assert st == 200
    data_int = json.loads(raw)
    layers = data_int['funnel']['layers']
    for k in ('Moments', 'Extract', 'Quota', 'L0', 'L1', 'L2a', 'L2b', 'L3', 'L4', 'Stage4'):
        assert layers.get(k, 0) >= 1, f'Missing layer count for {k}: {layers}'

    st, _, raw = _http_req(port, 'GET', '/api/console-v2/interceptions?layer=L2b', cookie='tok-owner-v2')
    l2b_items = json.loads(raw)['items']
    assert len(l2b_items) == 1 and l2b_items[0]['id'] == seeds['prop_l2b']

    st, _, raw = _http_req(port, 'GET', f"/api/console-v2/interceptions/{seeds['prop_l2b']}/trace", cookie='tok-owner-v2')
    tr_l2b = json.loads(raw)['item']
    assert tr_l2b['layer'] == 'L2b'
    assert tr_l2b['gate_chain'][-1]['meta']['model_prediction'].startswith('先运行')
    assert len(tr_l2b['llm_calls']) == 1

    # 4. Owner rescue on both pre-proposal (int-*) and gate-rejected (prop-*) items
    st, _, raw = _http_req(
        port, 'POST', '/api/console-v2/interceptions/rescue',
        cookie='tok-owner-v2', csrf=OWNER_CSRF,
        body={'item_id': seeds['extract_id'], 'note': 'rescue extract item'},
    )
    assert st == 200
    res_ex = json.loads(raw)
    rescued_ex_id = res_ex['note']['id']

    st, _, raw = _http_req(
        port, 'POST', '/api/console-v2/interceptions/rescue',
        cookie='tok-owner-v2', csrf=OWNER_CSRF,
        body={
            'item_id': seeds['prop_l0'],
            'edited_payload': {
                'category': '系统环境经验',
                'problem': '人工补充：容器构建失败需清理过期缓存',
                'solution': '执行 builder prune 清理陈旧层后重新构建',
                'reusable_experience': 'CI 构建报 checksum mismatch 时先清 builder 缓存',
            },
            'note': 'rescue L0 after fixing evidence',
        },
    )
    assert st == 200
    rescued_l0_id = json.loads(raw)['note']['id']
    assert rescued_ex_id != rescued_l0_id

    st, _, raw = _http_req(port, 'GET', '/api/console-v2/interceptions', cookie='tok-owner-v2')
    assert json.loads(raw)['funnel']['rescued_total'] >= 2

    st, _, raw = _http_req(port, 'GET', f'/api/console-v2/experiences/{rescued_l0_id}/trace', cookie='tok-owner-v2')
    exp_trace = json.loads(raw)
    assert exp_trace['ok'] is True and exp_trace['note']['status'] == 'active'
    assert len(exp_trace['events']) >= 1 and len(exp_trace['gate_chain']['decisions']) >= 1

    # 5. Shadow mode blinding on undecided proposal -> reveal post decision + independent track agreement
    st, _, raw = _http_req(
        port, 'GET', '/api/console-v2/review-inbox?source_track=live&include_decided=1',
        cookie='tok-owner-v2',
    )
    inbox_before = json.loads(raw)
    assert inbox_before['shadow_mode'] is True
    live_item = next(x for x in inbox_before['items'] if x['uid'] == seeds['prop_live_pending'])
    assert live_item['gate_verdict'] is None and live_item['gate_chain'] is None

    st, _, raw = _http_req(
        port, 'POST', '/api/console-v2/review-inbox/decide',
        cookie='tok-owner-v2', csrf=OWNER_CSRF,
        body={'proposal_uid': seeds['prop_live_pending'], 'user_verdict': 'accept'},
    )
    assert st == 200

    st, _, raw = _http_req(
        port, 'GET', '/api/console-v2/review-inbox?source_track=live&include_decided=1',
        cookie='tok-owner-v2',
    )
    inbox_after = json.loads(raw)
    live_after = next(x for x in inbox_after['items'] if x['uid'] == seeds['prop_live_pending'])
    assert live_after['gate_verdict'] == 'pass' and isinstance(live_after['gate_chain'], list)
    assert inbox_after['track_agreement']['live']['evaluable_total'] >= 2
    assert inbox_after['track_agreement']['backfill']['evaluable_total'] == 0

    # 6. Manual write + two-step model rollback (dry_run -> execute)
    st, _, raw = _http_req(
        port, 'POST', '/api/console-v2/experiences/write',
        cookie='tok-owner-v2', csrf=OWNER_CSRF,
        body={
            'action': 'create', 'request_id': 'v2-manual-http-0001',
            'category': '业务经验', 'kind': 'procedure',
            'problem': '手工写入通道验证', 'solution': '经 TicketIssuer 与 Writer 落盘', 'reusable_experience': '所有写操作必须签名',
        },
    )
    assert st == 201

    st, _, raw = _http_req(
        port, 'POST', '/api/console-v2/experiences/rollback',
        cookie='tok-owner-v2', csrf=OWNER_CSRF,
        body={'mode': 'dry_run', 'agent_client': 'codex', 'model': 'bad-roll-model'},
    )
    assert st == 200
    plan = json.loads(raw)['plan']
    assert len(plan['operations']) >= 1

    st, _, raw = _http_req(
        port, 'POST', '/api/console-v2/experiences/rollback',
        cookie='tok-owner-v2', csrf=OWNER_CSRF,
        body={'mode': 'execute', 'plan': plan},
    )
    assert st == 200 and json.loads(raw)['ok'] is True

    # 7. Peer readonly ACL & CSRF checks
    st, _, _ = _http_req(port, 'POST', '/api/console-v2/interceptions/rescue', cookie='tok-owner-v2', body={'item_id': seeds['moments_id']})
    assert st == 403

    st, _, raw = _http_req(port, 'GET', '/api/console-v2/experiences?owner_id=admin_owner', cookie='tok-peer-v2')
    peer_exps = json.loads(raw)
    assert st == 200 and peer_exps['interception_funnel'] is None
    assert all(x['status'] == 'active' for x in peer_exps['items'])
    assert all(x['id'] != seeds['retired_id'] for x in peer_exps['items'])

    assert _http_req(port, 'GET', '/api/console-v2/experiences?owner_id=admin_owner&status=retired', cookie='tok-peer-v2')[0] == 403
    assert _http_req(port, 'GET', '/api/console-v2/interceptions?owner_id=admin_owner', cookie='tok-peer-v2')[0] == 403
    assert _http_req(port, 'GET', '/api/console-v2/review-inbox?owner_id=admin_owner', cookie='tok-peer-v2')[0] == 403
    assert _http_req(
        port, 'POST', '/api/console-v2/interceptions/rescue',
        cookie='tok-peer-v2', csrf=PEER_CSRF,
        body={'owner_id': 'admin_owner', 'item_id': seeds['extract_id']},
    )[0] == 403
    # 8. Pre-005 legacy empty meta_json backfill (L0/L1/L3 deterministic + L2a/L2b metered LLM) -> HTTP API
    from evolvmem.experience_v2.gate.backfill_decision_meta import backfill_historical_decision_meta

    dsn, exp_schema = v2_live_server['dsn'], v2_live_server['schema']
    _conn_factory = lambda: psycopg.connect(dsn, options=f'-c search_path={exp_schema},public')
    with _conn_factory() as conn:
        proposer = ProposalService()
        lp = proposer.propose(
            conn, owner_id='admin_owner', actor_id='admin_owner', channel=TrustedChannel.MCP_CLIENT,
            attribution=_attr(model='deepseek-chat'),
            body={
                'op': 'add', 'request_id': 'req-legacy-pre005-l2b', 'source_track': 'live',
                'session_id': 'sess-legacy-pre005', 'category': '业务经验', 'kind': 'pitfall',
                'problem': 'pre-005 历史 L2 拦截未记录盲猜文本',
                'solution': '回填 L2a/L2b 盲猜输出并写入 meta_json',
                'reusable_experience': '所有 L2 裁决必须携带 model_prediction 供前后端对照',
                'evidence': [{'ref': 'archive:8899', 'quote': 'pre005 sample'}],
            },
            source_kind='pipeline',
        )
        legacy_uid = lp['uid']
        conn.execute(
            """
            INSERT INTO proposal_decisions (decision_id, proposal_uid, layer, verdict, reason_code, confidence, judge_model, detail)
            VALUES ('dec-legacy-l2a', %s, 'L2a', 'pass', NULL, 0.41, 'deepseek-v4-flash', 'flash missed'),
                   ('dec-legacy-l2b', %s, 'L2b', 'reject', 'common_knowledge', 0.90, 'deepseek-chat', 'strong solved')
            """,
            (legacy_uid, legacy_uid),
        )
        conn.execute("UPDATE experience_proposals SET state = 'rejected' WHERE uid = %s", (legacy_uid,))
        conn.commit()

    def _mock_tr(model: str, messages: list[dict], max_toks: int) -> dict:
        tag = 'FLASH' if 'flash' in model else 'STRONG'
        return {'text': f'{tag}_BLIND_PRED: direct diagnosis for prompt len={len(messages[-1]["content"])}', 'input_tokens': 45, 'output_tokens': 18}

    bf_summary = backfill_historical_decision_meta(
        _conn_factory, flash_transport=_mock_tr, strong_transport=_mock_tr, max_workers=2,
    )
    assert bf_summary['l2_errors'] == 0 and bf_summary['l2_decisions_updated'] >= 2

    with _conn_factory() as conn:
        calls_cnt = conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE proposal_uid = %s AND stage IN ('l2a_predict_backfill', 'l2b_predict_backfill')",
            (legacy_uid,),
        ).fetchone()[0]
        assert calls_cnt == 2

    st, _, raw = _http_req(port, 'GET', '/api/console-v2/interceptions?layer=L2b', cookie='tok-owner-v2')
    assert st == 200
    l2b_hit = next(x for x in json.loads(raw)['items'] if x['id'] == legacy_uid)
    m2a = next(d['meta'] for d in l2b_hit['gate_chain'] if d['layer'] == 'L2a')
    m2b = next(d['meta'] for d in l2b_hit['gate_chain'] if d['layer'] == 'L2b')
    assert m2a['blind_prompt'] and m2a['model_prediction'].startswith('FLASH_BLIND_PRED:')
    assert m2b['blind_prompt'] and m2b['model_prediction'].startswith('STRONG_BLIND_PRED:')
    assert m2b['flash_prediction'].startswith('FLASH_BLIND_PRED:')

    st, _, raw = _http_req(port, 'GET', '/api/console-v2/review-inbox?source_track=live&include_decided=1', cookie='tok-owner-v2')
    assert st == 200
    inbox_l2_item = next(x for x in json.loads(raw)['items'] if x['uid'] == seeds['prop_live_pending'])
    assert any(d.get('meta', {}).get('model_prediction') for d in (inbox_l2_item.get('gate_chain') or []) if d['layer'] == 'L2b')

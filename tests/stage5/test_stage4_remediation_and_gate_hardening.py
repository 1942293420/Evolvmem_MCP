"""Stage 5 tests for L0/L2 completeness gate hardening & Stage 4 legacy remediation."""
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

from evolvmem.experience_v2.control.pg_console_store import PgConsoleStore, PgControlConfig
from evolvmem.experience_v2.control.pg_web_sessions import PgPersistentSessions
from evolvmem.experience_v2.gate.remediate_legacy_notes import remediate_legacy_notes
from evolvmem.experience_v2.gate.rules import evaluate_l0_fields
from evolvmem.experience_v2.gate.value import assess_value_cascade
from evolvmem.experience_v2.schema import apply_experience_migrations
from evolvmem.experience_v2.writer import ExperienceWriter, Op, TicketIssuer
from evolvmem.history_pg.web_pg_runner import NullLegacyWebService
from evolvmem.knowledge_console import ConsoleService
from evolvmem.web_auth import AuthSettings, WebAuth
from evolvmem.web_server import make_http_server
from tests.stage1 import pg_stage1_harness
from tests.stage5.conftest import STAGE5_PG_CONTAINER, STAGE5_PG_PORT, STAGE5_PW_FILE

OWNER_OPEN_ID = 'ou_owner_s4rem_test'
OWNER_CSRF = 'csrf-owner-s4rem'


def _dsn() -> str:
    pw = STAGE5_PW_FILE.read_text(encoding='utf-8').strip()
    return f'host=127.0.0.1 port={STAGE5_PG_PORT} dbname=postgres user=postgres password={pw}'


def _get_json(port: int, path: str, cookie: str) -> tuple[int, dict]:
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        conn.request('GET', path, headers={'Cookie': f'evolvmem_session={cookie}'})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read().decode('utf-8'))
    finally:
        conn.close()


def test_l0_and_l2_gate_hardening_rejects_incomplete_and_echoes() -> None:

    # 1. Duplicate problem == reusable_experience rejected at L0
    v1, rc1, _ = evaluate_l0_fields({
        'category': '系统环境经验', 'kind': 'pitfall',
        'problem': 'EvolvMem召回性能问题核查：存在超时与排队',
        'solution': '经压测定位共享锁阻塞了健康检查线程，已将检查改为异步',
        'reusable_experience': 'EvolvMem召回性能问题核查：存在超时与排队',
        'evidence': [{'ref': 'archive:1', 'quote': 'q'}],
    }, require_evidence=False)
    assert v1 == 'reject' and rc1 == 'weak_evidence'

    # 2. Mechanically truncated solution ending in ellipsis rejected at L0
    v2, rc2, _ = evaluate_l0_fields({
        'category': '系统环境经验', 'kind': 'pitfall',
        'problem': '召回路径偶发超时导致状态查询排队',
        'solution': '已确认共享锁阻塞，该召回路径没有调用大模…',
        'reusable_experience': '召回共享锁严禁持有过久',
        'evidence': [{'ref': 'archive:1', 'quote': 'q'}],
    }, require_evidence=False)
    assert v2 == 'reject' and rc2 == 'weak_evidence'

    # 3. Speculative / incomplete check log phrases rejected at L0
    v3, rc3, _ = evaluate_l0_fields({
        'category': '业务经验', 'kind': 'procedure',
        'problem': '查询响应慢需定位原因',
        'solution': '推测：多因素叠加导致慢查询，需逐项分解排查，正式优化尚未实施',
        'reusable_experience': '慢查询需逐项排查索引与重排序开销',
        'evidence': [{'ref': 'archive:1', 'quote': 'q'}],
    }, require_evidence=False)
    assert v3 == 'reject' and rc3 == 'weak_evidence'

    v4, rc4, _ = evaluate_l0_fields({
        'category': '系统环境经验', 'kind': 'pitfall',
        'problem': '召回接口耗时79秒排查记录',
        'solution': '健康检查耗时13秒其余66秒，现有日志还不能精确区分最慢步骤，本轮只做核查，未改代码',
        'reusable_experience': '长耗时接口先拆日志再优化',
        'evidence': [{'ref': 'archive:1', 'quote': 'q'}],
    }, require_evidence=False)
    assert v4 == 'reject' and rc4 == 'weak_evidence'

    # 4. L2 judge returning weak_evidence propagates reason_code cleanly
    def _mock_l2(model: str, messages: list[dict], max_toks: int) -> dict:
        if '对比参考答案' in messages[0]['content']:
            return {
                'text': json.dumps({
                    'consistent': True,
                    'reason_code': 'weak_evidence',
                    'confidence': 0.94,
                    'detail': '参考解法仅列排查方向无具体根因与实证结果',
                }),
                'input_tokens': 70, 'output_tokens': 25,
            }
        return {'text': '盲测答：检查索引失效或缓存未命中', 'input_tokens': 40, 'output_tokens': 15}

    base_dsn = _dsn()
    schema = f's4gate_{uuid.uuid4().hex[:10]}'
    with psycopg.connect(base_dsn, autocommit=True) as admin:
        admin.execute('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public')
        admin.execute(f'CREATE SCHEMA "{schema}"')
    try:
        with psycopg.connect(base_dsn, options=f'-c search_path={schema},public') as conn:
            apply_experience_migrations(conn)
            out = assess_value_cascade(
                conn,
                problem='查询接口慢需定位',
                solution='分解排查索引与重排耗时分布，尚待进一步验证',
                scope={'project': 'evolvmem'},
                flash_model='flash-test',
                strong_model='strong-test',
                flash_transport=_mock_l2,
                track='live',
                owner_id='u1',
                proposal_uid='prop-weak-l2',
            )
            assert out.verdict == 'reject' and out.layer == 'L2a' and out.reason_code == 'weak_evidence'
    finally:
        with psycopg.connect(base_dsn, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_remediate_legacy_notes_retires_incomplete_and_redistills_truncated_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(pg_stage1_harness, 'TEST_PG_PORT', STAGE5_PG_PORT)
    monkeypatch.setattr(pg_stage1_harness, 'TEST_PG_CONTAINER', STAGE5_PG_CONTAINER)
    monkeypatch.setattr(pg_stage1_harness, 'TEST_PG_PW_FILE', STAGE5_PW_FILE)

    ctrl_env = pg_stage1_harness.create_isolated_control_db(tmp_path, up_to_version=7)
    base_dsn = _dsn()
    exp_schema = f's4rem_{uuid.uuid4().hex[:10]}'
    with psycopg.connect(base_dsn, autocommit=True) as admin:
        admin.execute('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public')
        admin.execute(f'CREATE SCHEMA "{exp_schema}"')

    data_dir = tmp_path / 'data'
    data_dir.mkdir(parents=True, exist_ok=True)
    secret = b'stage5-remediate-test-ticket-secret-key'
    sec_file = tmp_path / 'ticket.secret'
    sec_file.write_bytes(secret)
    os.chmod(sec_file, 0o600)
    policy_file = tmp_path / 'policy.json'
    policy_file.write_text(json.dumps({
        'console_enabled': True, 'legacy_web_enabled': False, 'ai_processing_enabled': False,
        'experience_v2_enabled': True, 'experience_pipeline_enabled': True,
        'experience_backfill_enabled': True, 'history_pg_enabled': True, 'experience_gate_mode': 'review',
    }), encoding='utf-8')
    auth_file = tmp_path / 'web_auth.json'
    auth_file.write_text(json.dumps({
        'enabled': True, 'app_id': 'c1', 'app_secret': 's1', 'owner_open_id': OWNER_OPEN_ID,
        'redirect_uri': 'http://127.0.0.1:9377/auth/feishu/callback',
    }), encoding='utf-8')

    monkeypatch.setenv('EVOLVMEM_CONTROL_BACKEND', 'postgres')
    monkeypatch.setenv('EVOLVMEM_CONSOLE_PG_CONFIG', str(ctrl_env.config_file))
    monkeypatch.setenv('EVOLVMEM_CONSOLE_OWNER_ID', 'admin_owner')
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_DSN', base_dsn)
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_SCHEMA', exp_schema)
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_TICKET_SECRET_FILE', str(sec_file))
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(policy_file))
    monkeypatch.setenv('EVOLVMEM_WEB_AUTH_FILE', str(auth_file))

    def _cf():
        return psycopg.connect(base_dsn, options=f'-c search_path={exp_schema},public')

    try:
        with _cf() as conn:
            apply_experience_migrations(conn)
            writer = ExperienceWriter(secret)
            issuer = TicketIssuer(secret)
            seed_items = [
                ('exp-remtest-3808', 'capability_memories:3808', '系统环境经验', 'pitfall',
                 'EvolvMem召回性能问题核查：能返回结果但存在超时与排队',
                 '对EvolvMem召回功能复测发现健康检查13秒业务66秒，该召回路径没有调用大模…',
                 'EvolvMem召回性能问题核查：能返回结果但存在超时与排队'),
                ('exp-remtest-17043', 'context_items:17043', '业务经验', 'procedure',
                 '查询响应慢，需定位原因',
                 '推测：多因素叠加导致慢查询，需逐项分解，正式优化尚未实施',
                 '推测：多因素叠加导致慢查询，需逐项分解'),
                ('exp-remtest-keep1', 'capability_memories:9001', '系统环境经验', 'pitfall',
                 'systemd 切换后主页面仍连旧版数据库',
                 '前期观察发现 systemd 服务端口未变化但数据仍写向旧库，进一步对照 drop-in 加载顺序后确认…',
                 'systemd 切换后主页面仍连旧版数据库'),
            ]
            for idx, (nid, lid, cat, kind, prob, sol, reus) in enumerate(seed_items):
                t = issuer.issue(proposal_uid=f'seed:{idx}', decision_id=f'seed:d{idx}', lane='user_fast')
                writer.apply(conn, t, Op(
                    action='create', owner_id='admin_owner', actor_id='admin_owner', request_id=f'seed-{nid}',
                    note_id=nid, category=cat, kind=kind, problem=prob, solution=sol, reusable_experience=reus,
                    origin='legacy', scope={'origin_stage': 'stage4_legacy_clean', 'legacy_id': lid, 'project': 'evolvmem'},
                    evidence=[{'kind': 'legacy_migration', 'legacy_id': lid}],
                ))
            conn.commit()

        full_sources = {
            'capability_memories:3808': {
                'legacy_id': 'capability_memories:3808', 'source_table': 'capability_memories',
                'legacy_status': 'active', 'legacy_category': 'quality', 'project': 'evolvmem',
                'title': 'EvolvMem召回性能问题核查',
                'full_text': '健康检查13秒、共享锁排队48秒。现有日志还不能精确区分。本轮只做核查，未改代码。',
            },
            'context_items:17043': {
                'legacy_id': 'context_items:17043', 'source_table': 'context_items',
                'legacy_status': 'candidate', 'legacy_category': 'experience', 'project': 'evolvmem',
                'title': '查询响应慢，需定位原因',
                'full_text': '{"rationale":"推测：多因素叠加导致慢查询","exclusions":["未验证优化效果"]}',
            },
            'capability_memories:9001': {
                'legacy_id': 'capability_memories:9001', 'source_table': 'capability_memories',
                'legacy_status': 'active', 'legacy_category': 'backend', 'project': 'evolvmem',
                'title': 'systemd 切换后主页面仍连旧版数据库',
                'full_text': '前期观察发现端口未变化。最终定位根因为 user service drop-in zz-stage5-pg-runtime.conf 文件名顺序晚于旧 conf 才会覆盖环境变量，将旧 drop-in 删除并 daemon-reload 重启后写入目标库恢复正常。',
            },
        }

        def _mock_redistill(model: str, messages: list[dict], max_toks: int) -> dict:
            return {
                'text': json.dumps({
                    'keep': True, 'reason_code': '', 'detail': '完整原文含精确 drop-in 覆盖根因与修复动作',
                    'problem': 'systemd 切库重启后服务环境变量仍指向旧数据库',
                    'solution': '根因为 systemd drop-in 按字母序加载覆盖环境变量，删除旧 90-*.conf 并保留 zz-*.conf 后执行 daemon-reload 重启即切换成功',
                    'reusable_experience': '修改 systemd user 服务环境前先用 systemctl show 核对 DropInPaths 加载顺序并清理旧前缀 conf',
                }, ensure_ascii=False),
                'input_tokens': 110, 'output_tokens': 65,
            }

        summary = remediate_legacy_notes(
            _cf,
            ticket_secret=secret,
            full_sources=full_sources,
            batch_encode_fn=lambda texts: [[0.5] + [0.0] * 767 for _ in texts],
            flash_model='flash-test',
            flash_transport=_mock_redistill,
            max_workers=2,
        )
        assert summary['errors'] == 0 and summary['retired'] == 2 and summary['updated'] == 1

        # Second call must be 100% idempotent
        summary2 = remediate_legacy_notes(
            _cf, ticket_secret=secret, full_sources=full_sources, flash_model='flash-test', flash_transport=_mock_redistill,
        )
        assert summary2['already_remediated'] == 1 and summary2['retired'] == 0 and summary2['updated'] == 0

        ctrl_cfg = PgControlConfig.from_file(ctrl_env.config_file)
        ctrl_store = PgConsoleStore(ctrl_cfg)
        ConsoleService(tmp_path / 'u.db', 'admin_owner', data_dir, tmp_path / 'lan', {}, pg_control=ctrl_store).bootstrap(OWNER_OPEN_ID, 'Owner')
        sessions = PgPersistentSessions(ctrl_cfg, WebAuth(AuthSettings.load(data_dir)).persistence_scope(), expected_owner_id='admin_owner', console_mode=True)
        sessions.initialize()
        sessions.put('tok-s4rem', {'open_id': OWNER_OPEN_ID, 'name': 'Owner', 'csrf': OWNER_CSRF, 'expires': time.time() + 3600})

        srv = make_http_server(NullLegacyWebService(data_dir, host='127.0.0.1', port=0), host='127.0.0.1', port=0)
        port = int(srv.server_address[1])
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            st_exp, exp_body = _get_json(port, '/api/console-v2/experiences', 'tok-s4rem')
            assert st_exp == 200
            active_ids = [it['id'] for it in exp_body['items']]
            assert active_ids == ['exp-remtest-keep1']
            kept = exp_body['items'][0]
            assert not kept['solution'].endswith('…')
            assert kept['problem'] != kept['reusable_experience']
            assert kept['scope'].get('stage4_remediated_v2') == 'true'

            st_l0, l0_body = _get_json(port, '/api/console-v2/interceptions?layer=L0', 'tok-s4rem')
            assert st_l0 == 200 and len(l0_body['items']) == 2
            assert {it['note_id'] for it in l0_body['items']} == {'exp-remtest-3808', 'exp-remtest-17043'}
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

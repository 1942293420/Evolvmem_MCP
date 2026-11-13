"""Task 2.6: L0 deterministic hard rules, evidence pointer verifier, and 40 anonymized real samples."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import uuid

from evolvmem.experience_v2.control.agent_identity import AgentAttribution
from evolvmem.experience_v2.control.reputation_policy import TrustedChannel
from evolvmem.experience_v2.gate.propose import ProposalService
from evolvmem.experience_v2.schema import apply_experience_migrations

FIXTURE_PATH = Path(__file__).parent / 'fixtures' / 'real_40_anonymized_samples.json'


def _make_history_db(db_path: Path) -> Path:
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE session_archives(id INTEGER PRIMARY KEY, project TEXT, adapter TEXT, external_session_id TEXT);
            CREATE TABLE conversation_history(archive_id INTEGER PRIMARY KEY, messages TEXT);
            CREATE TABLE context_items(id INTEGER PRIMARY KEY, identity_key TEXT);
            CREATE TABLE project_events(event_id TEXT PRIMARY KEY, project TEXT);
            INSERT INTO session_archives VALUES (1, 'proj-alpha', 'codex', 'ext-sess-001');
            INSERT INTO conversation_history VALUES (1, '[]');
            INSERT INTO context_items VALUES (77, 'ctx-77');
            INSERT INTO project_events VALUES ('evt-abc', 'proj-alpha');
            """
        )
        conn.commit()
    finally:
        conn.close()
    return db_path


def _insert_proposal(conn, svc: ProposalService, owner: str, body: dict) -> str:
    attr = AgentAttribution(actor=owner, agent_id='agent-l0', client='claude', model='claude-sonnet-4')
    res = svc.propose(
        conn,
        owner_id=owner,
        actor_id=owner,
        channel=TrustedChannel.MCP_CLIENT,
        attribution=attr,
        body={**body, 'session_id': f'sess-{uuid.uuid4().hex[:8]}', 'request_id': f'l0-{uuid.uuid4().hex[:12]}'},
    )
    return res['uid']


def test_l0_rules_length_secrets_and_history_evidence_verifier(pg_exp_schema, tmp_path: Path):
    from evolvmem.experience_v2.gate.rules import SqliteHistoryEvidenceVerifier, run_l0_gate

    conn, _schema, _dsn = pg_exp_schema
    apply_experience_migrations(conn)
    conn.commit()

    hist_db = _make_history_db(tmp_path / 'history.db')
    verifier = SqliteHistoryEvidenceVerifier(hist_db)
    svc = ProposalService()

    # 1. Nonexistent evidence pointer -> weak_evidence
    uid_bad_ev = _insert_proposal(
        conn,
        svc,
        'u1',
        {
            'category': '系统环境经验',
            'kind': 'pitfall',
            'problem': '容器启动时端口冲突报错',
            'solution': '检查 55432 端口占用后切换独立测试端口',
            'reusable_experience': '测试环境固定使用独立端口区间',
            'evidence': [{'library': 'personal', 'source': 'archive:999999'}],
        },
    )
    d_ev = run_l0_gate(conn, uid_bad_ev, evidence_verifier=verifier)
    assert d_ev['verdict'] == 'reject'
    assert d_ev['reason_code'] == 'weak_evidence'

    # 2. Secret leak -> reject with reason_code='wrong' and secret redacted
    uid_sec = _insert_proposal(
        conn,
        svc,
        'u1',
        {
            'category': '系统环境经验',
            'kind': 'env_fact',
            'problem': '接口认证头配置方式',
            'solution': '使用 Bearer sk-ant-secret1234567890abcdef token 调用',
            'reusable_experience': '凭据不要硬编码在源码中',
            'evidence': [{'library': 'personal', 'source': 'archive:1'}],
        },
    )
    d_sec = run_l0_gate(conn, uid_sec, evidence_verifier=verifier)
    assert d_sec['verdict'] == 'reject'
    assert d_sec['reason_code'] == 'wrong'
    assert 'secret1234567890' not in d_sec['detail']

    # 3. Over-length reusable_experience (> 50 chars) -> reject
    uid_len = _insert_proposal(
        conn,
        svc,
        'u1',
        {
            'category': '前端经验',
            'kind': 'pitfall',
            'problem': '短问题',
            'solution': '短解决方案',
            'reusable_experience': '长' * 55,
            'evidence': [{'library': 'personal', 'source': 'context:77'}],
        },
    )
    d_len = run_l0_gate(conn, uid_len, evidence_verifier=verifier)
    assert d_len['verdict'] == 'reject'

    # 4. Valid evidence pointers (context:77 / event:evt-abc / session:ext-sess-001) pass L0
    uid_ok = _insert_proposal(
        conn,
        svc,
        'u1',
        {
            'category': '系统环境经验',
            'kind': 'procedure',
            'problem': '孤立测试数据库未清理导致端口占满',
            'solution': '测试结束执行容器与卷销毁并在启动前做存活探活',
            'reusable_experience': '集成测试需独立命名空间并在退出时清理',
            'evidence': [{'library': 'personal', 'source': 'event:evt-abc'}],
        },
    )
    d_ok = run_l0_gate(conn, uid_ok, evidence_verifier=verifier)
    assert d_ok['verdict'] == 'pass'
    assert conn.execute('SELECT count(*) FROM proposal_decisions WHERE proposal_uid = %s', (uid_ok,)).fetchone()[0] == 1


def test_l0_rejects_100_percent_task_logs_across_40_anonymized_real_samples(pg_exp_schema, tmp_path: Path):
    from evolvmem.experience_v2.gate.rules import SqliteHistoryEvidenceVerifier, run_l0_gate

    conn, _schema, _dsn = pg_exp_schema
    apply_experience_migrations(conn)
    conn.commit()

    samples = json.loads(FIXTURE_PATH.read_text(encoding='utf-8'))
    assert len(samples) == 40
    task_logs = [s for s in samples if s['is_task_log']]
    assert len(task_logs) >= 15

    hist_db = _make_history_db(tmp_path / 'history.db')
    verifier = SqliteHistoryEvidenceVerifier(hist_db)
    svc = ProposalService()

    for s in samples:
        uid = _insert_proposal(conn, svc, 'u1', s)
        verdict = run_l0_gate(conn, uid, evidence_verifier=verifier)
        if s['is_task_log']:
            assert verdict['verdict'] == 'reject', f"Task log slipped through L0: {s['sample_id']}"
            assert verdict['reason_code'] == 'task_log'
        else:
            assert verdict['verdict'] == 'pass', f"Valid rule unexpectedly rejected: {s['sample_id']} -> {verdict}"

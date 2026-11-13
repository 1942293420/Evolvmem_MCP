"""Stage 2 Task 2.10 tests: PG LISTEN/NOTIFY, experience_changes_since, and PG experience_recall."""
from __future__ import annotations

import json
from pathlib import Path

from evolvmem.config import Config
from evolvmem.experience_v2.schema import apply_experience_migrations
from evolvmem.experience_v2.writer import (
    ExperienceWriter,
    Op,
    TicketIssuer,
    scan_unauthorized_experience_notes_writes,
)
from evolvmem.knowledge_console import ConsoleService
from evolvmem.mcp_server import MemoryMCPServer

REPO_ROOT = Path(__file__).resolve().parents[2]


def _setup_mcp_env(tmp_path: Path, schema: str, dsn: str, monkeypatch, *, exp_v2: bool = True):
    owner_dir = tmp_path / 'owner_data'
    owner_dir.mkdir(parents=True)
    registry = tmp_path / 'console' / 'users.db'
    svc = ConsoleService(registry, 'jiangli', owner_dir, tmp_path / 'lan', {})
    svc.bootstrap('test-owner-openid', 'Owner')

    secret_file = tmp_path / 'ticket.secret'
    secret_file.write_text('stage2-task210-secret', encoding='utf-8')
    policy_file = tmp_path / 'policy.json'
    policy_file.write_text(json.dumps({
        'console_enabled': True,
        'legacy_web_enabled': False,
        'ai_processing_enabled': False,
        'experience_v2_enabled': exp_v2,
    }), encoding='utf-8')

    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(policy_file))
    monkeypatch.setenv('EVOLVMEM_CONSOLE_REGISTRY', str(registry))
    monkeypatch.setenv('EVOLVMEM_CONSOLE_OWNER_ID', 'jiangli')
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_DSN', str(dsn))
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_SCHEMA', schema)
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_TICKET_SECRET_FILE', str(secret_file))

    cfg = Config(data_dir=owner_dir)
    server = MemoryMCPServer(cfg, embedding_engine=None, rebuild_legacy_vectors_on_start=False)
    return server, secret_file.read_bytes()


def _rpc_call(server: MemoryMCPServer, tool_name: str, args: dict, *, client_info: dict | None = None):
    if client_info is not None:
        server._handle_request({
            'jsonrpc': '2.0',
            'id': 1,
            'method': 'initialize',
            'params': {'clientInfo': client_info},
        })
    resp = server._handle_request({
        'jsonrpc': '2.0',
        'id': 2,
        'method': 'tools/call',
        'params': {'name': tool_name, 'arguments': args},
    })
    assert 'result' in resp, resp
    txt = resp['result']['content'][0]['text']
    return json.loads(txt)


def test_pg_listen_notify_wakes_on_experience_write(pg_exp_schema):
    from evolvmem.experience_v2.changes_sync import ExperienceChangeListener

    conn, schema, dsn = pg_exp_schema
    apply_experience_migrations(conn)
    conn.commit()
    secret = b'stage2-task210-notify-secret'
    writer = ExperienceWriter(secret)
    ticket = TicketIssuer(secret, gate_id='manual').issue(
        proposal_uid='user:n1', decision_id='user:n1', lane='user_fast',
    )

    with ExperienceChangeListener(str(dsn)) as listener:
        res = writer.apply(
            conn,
            ticket,
            Op(
                action='create',
                owner_id='jiangli',
                actor_id='jiangli',
                request_id='notify-req-1',
                category='系统环境经验',
                problem='PG LISTEN/NOTIFY verification probe',
                solution='Use dedicated autocommit listener connection',
                reusable_experience='Triggers wake sibling readers within 1 second',
                origin='manual',
            ),
        )
        conn.commit()
        events = listener.poll(timeout=1.0)
        assert len(events) >= 1
        assert any(
            ev.get('note_id') == res['experience']['id'] and ev.get('owner_id') == 'jiangli'
            for ev in events
        )


def test_cross_agent_changes_since_recall_impressions_and_fail_closed(pg_exp_schema, tmp_path, monkeypatch):
    conn, schema, dsn = pg_exp_schema
    apply_experience_migrations(conn)
    conn.commit()

    server, secret = _setup_mcp_env(tmp_path, schema, str(dsn), monkeypatch, exp_v2=True)
    writer = ExperienceWriter(secret)

    # Agent A writes N1
    t1 = TicketIssuer(secret, gate_id='manual').issue(proposal_uid='u1', decision_id='u1', lane='user_fast')
    n1 = writer.apply(
        conn,
        t1,
        Op(
            action='create',
            owner_id='jiangli',
            actor_id='jiangli',
            request_id='agent-a-n1',
            category='系统环境经验',
            problem='PostgreSQL isolated schema search_path failure',
            solution='Set -c search_path=<schema>,public on psycopg connect',
            reusable_experience='Keep shared extensions in public and isolate tables per schema',
            origin='manual',
            importance=9,
            agent_client='codex',
            model='gpt-5-codex',
        ),
    )['experience']
    conn.commit()

    # Agent B bootstraps cursor -> empty delta, next_cursor >= 1
    boot = _rpc_call(
        server,
        'experience_changes_since',
        {'cursor': 0, 'bootstrap_only': True},
        client_info={'name': 'claude-code', 'version': '1.0', 'model': 'claude-sonnet-4-5'},
    )
    assert boot['ok'] is True
    assert boot['changes'] == []
    cursor_s1 = boot['next_cursor']
    assert cursor_s1 >= 1

    # Agent A writes N2
    t2 = TicketIssuer(secret, gate_id='manual').issue(proposal_uid='u2', decision_id='u2', lane='user_fast')
    n2 = writer.apply(
        conn,
        t2,
        Op(
            action='create',
            owner_id='jiangli',
            actor_id='jiangli',
            request_id='agent-a-n2',
            category='业务经验',
            problem='Windows hook cursor advances before empty turn',
            solution='Persist cursor atomically only after valid delta response',
            reusable_experience='Never advance client cursor when MCP call errors',
            origin='manual',
            importance=8,
            agent_client='codex',
            model='gpt-5-codex',
        ),
    )['experience']
    conn.commit()

    # Agent B pulls incremental delta using cursor_s1 -> receives ONLY N2
    delta = _rpc_call(
        server,
        'experience_changes_since',
        {'cursor': cursor_s1},
        client_info={'name': 'claude-code', 'version': '1.0', 'model': 'claude-sonnet-4-5'},
    )
    assert delta['ok'] is True
    assert [c['note_id'] for c in delta['changes']] == [n2['id']]
    assert delta['next_cursor'] > cursor_s1

    # Next turn with updated cursor -> empty delta
    delta_empty = _rpc_call(
        server,
        'experience_changes_since',
        {'cursor': delta['next_cursor']},
        client_info={'name': 'claude-code', 'version': '1.0', 'model': 'claude-sonnet-4-5'},
    )
    assert delta_empty['changes'] == []
    assert delta_empty['next_cursor'] == delta['next_cursor']

    # Agent B calls experience_recall -> PG results + retrieval_impressions row
    recalled = _rpc_call(
        server,
        'experience_recall',
        {'project': 'evolvmem', 'query': 'PostgreSQL isolated schema search_path', 'mode': 'hook'},
        client_info={'name': 'claude-code', 'version': '1.0', 'model': 'claude-sonnet-4-5'},
    )
    assert any(r['id'] == n1['id'] for r in recalled.get('results', []))
    imps = conn.execute(
        'SELECT item_uid, channel, agent_client, model FROM retrieval_impressions WHERE owner_id = %s',
        ('jiangli',),
    ).fetchall()
    assert any(row[0] == n1['id'] and row[1] == 'mcp' and row[2] == 'claude' for row in imps)

    # Fail closed when PG becomes unreachable (no SQLite fallback)
    monkeypatch.setenv(
        'EVOLVMEM_EXPERIENCE_PG_DSN',
        'host=127.0.0.1 port=59999 dbname=postgres user=postgres password=x connect_timeout=1',
    )
    err_changes = _rpc_call(server, 'experience_changes_since', {'cursor': 0})
    assert err_changes['error'] == 'pg_experience_unavailable'
    err_recall = _rpc_call(server, 'experience_recall', {'project': 'evolvmem', 'query': 'search_path'})
    assert err_recall['error'] == 'pg_experience_unavailable'


def test_task210_ast_and_file_line_limits():
    assert scan_unauthorized_experience_notes_writes(REPO_ROOT) == []
    for rel in (
        'evolvmem/experience_v2/changes_sync.py',
        'evolvmem/experience_v2/mcp_tools.py',
        'scripts/windows/evolvmem-experience-sync.ps1',
    ):
        p = REPO_ROOT / rel
        if p.exists():
            assert len(p.read_text(encoding='utf-8').splitlines()) <= 600
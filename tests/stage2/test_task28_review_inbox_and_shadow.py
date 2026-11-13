"""Stage 2 Task 2.8 tests: Web Review Inbox, live/backfill track isolation, and shadow mode."""
from __future__ import annotations

import http.client
import importlib.util
import json
from pathlib import Path
import uuid

import pytest

from evolvmem.experience_v2.gate.rules import REASON_CODES
from evolvmem.experience_v2.schema import apply_experience_migrations
from evolvmem.experience_v2.writer import scan_unauthorized_experience_notes_writes

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_fixture_module():
    spec = importlib.util.spec_from_file_location(
        'console_web_fixture',
        REPO_ROOT / 'tests' / 'browser' / 'launch_console_fixture.py',
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _http(port: int, path: str, *, actor: str | None = 'owner', method: str = 'GET', body=None, csrf: bool | str = False):
    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
    headers = {}
    if actor:
        headers['Cookie'] = f'evolvmem_session=test-{actor}-cookie'
    if csrf:
        headers['X-CSRF-Token'] = f'test-{actor}-csrf' if csrf is True else str(csrf)
    raw = None
    if body is not None:
        headers['Content-Type'] = 'application/json'
        raw = json.dumps(body)
    conn.request(method, path, body=raw, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    mime = resp.getheader('Content-Type', '')
    status = resp.status
    conn.close()
    return status, (json.loads(data) if 'application/json' in mime else data.decode('utf-8'))


def _seed_proposal(
    conn,
    *,
    owner_id: str = 'jiangli',
    source_track: str = 'live',
    state: str = 'review',
    problem: str = 'PG migration dry-run requires isolated schema',
    gate_verdict: str = 'pass',
    gate_reason: str = 'low_confidence',
    gate_confidence: float = 0.72,
) -> str:
    uid = f'prop-{uuid.uuid4().hex[:12]}'
    ev = [{'kind': 'command_output', 'ref': f'session://{uid[:8]}', 'quote': 'pytest tests/stage2 -> passed'}]
    conn.execute(
        """
        INSERT INTO experience_proposals (
            uid, owner_id, op, category, kind, problem, solution, reusable_experience,
            misbelief, scope, payload_json, evidence_json, source_kind, source_track,
            agent_client, model, device_id, actor_id, session_id, request_id, state, importance
        ) VALUES (
            %s, %s, 'add', '系统环境经验', 'pitfall', %s,
            'Create an isolated test schema via UUID prefix and drop it in finally.',
            'Always isolate PG integration tests per schema rather than touching production schema.',
            'Sharing one static schema avoids cleanup.', '{"repo":"evolvmem"}'::jsonb, %s::jsonb, %s::jsonb,
            'agent', %s, 'codex', 'gpt-5-codex', 'dev-01', %s, %s, %s, %s, 7
        )
        """,
        (
            uid, owner_id, problem,
            json.dumps({'problem': problem}, ensure_ascii=False),
            json.dumps(ev, ensure_ascii=False),
            source_track, owner_id, f'sess-{uid[:8]}', f'req-{uid}', state,
        ),
    )
    conn.execute(
        """
        INSERT INTO proposal_decisions (
            decision_id, proposal_uid, layer, verdict, reason_code, confidence, judge_model, detail
        ) VALUES (%s, %s, 'L2b', %s, %s, %s, 'test-judge', 'internal gate verdict')
        """,
        (f'dec-{uuid.uuid4().hex[:12]}', uid, gate_verdict, gate_reason, gate_confidence),
    )
    conn.commit()
    return uid


def test_review_inbox_tracks_shadow_mode_and_decisions(pg_exp_schema):
    from evolvmem.experience_v2.gate.review_inbox import ReviewInboxService

    conn, schema, dsn = pg_exp_schema
    apply_experience_migrations(conn)
    secret = b'test-stage2-task28-secret'

    p_live_1 = _seed_proposal(conn, source_track='live', state='review', gate_verdict='pass', gate_reason='weak_evidence', gate_confidence=0.61)
    p_live_2 = _seed_proposal(conn, source_track='live', state='review', gate_verdict='reject', gate_reason='wrong_scope', gate_confidence=0.88)
    p_live_edit = _seed_proposal(conn, source_track='live', state='pending', gate_verdict='review', gate_reason='low_confidence', gate_confidence=0.52)
    p_backfill_1 = _seed_proposal(conn, source_track='backfill', state='rejected', gate_verdict='reject', gate_reason='task_log', gate_confidence=0.91)
    p_backfill_2 = _seed_proposal(conn, source_track='backfill', state='quarantined', gate_verdict='reject', gate_reason='other', gate_confidence=0.95)

    svc = ReviewInboxService(ticket_secret=secret, shadow_mode=True)

    # Track isolation + shadow masking
    live_res = svc.list_inbox(conn, owner_id='jiangli', source_track='live')
    assert live_res['shadow_mode'] is True
    assert live_res['counts'] == {'live': 3, 'backfill': 2}
    assert {item['uid'] for item in live_res['items']} == {p_live_1, p_live_2, p_live_edit}
    assert len(live_res['reason_codes']) == len(REASON_CODES) == 8
    for item in live_res['items']:
        assert item['source_track'] == 'live'
        assert item['evidence_preview']
        assert item.get('gate_verdict') is None
        assert item.get('gate_confidence') is None
        assert item.get('gate_reason') is None

    backfill_res = svc.list_inbox(conn, owner_id='jiangli', source_track='backfill')
    assert {item['uid'] for item in backfill_res['items']} == {p_backfill_1, p_backfill_2}

    # Accept -> writes PG experience_notes through gated AdmissionTicket
    res_acc = svc.decide(conn, owner_id='jiangli', reviewer_id='jiangli', proposal_uid=p_live_1, user_verdict='accept', note='verified in review')
    assert res_acc['ok'] is True
    assert res_acc['note']['id'].startswith('exp-')
    # Internal gate judgment was persisted into review_decisions after user submitted decision
    rd = conn.execute(
        'SELECT gate_verdict, gate_confidence, gate_reason, user_verdict, source_track FROM review_decisions WHERE proposal_uid = %s',
        (p_live_1,),
    ).fetchone()
    assert rd == ('pass', pytest.approx(0.61, abs=1e-4), 'weak_evidence', 'accept', 'live')

    # Edit accept -> updates content saved in experience_notes & edited_payload
    res_edit = svc.decide(
        conn,
        owner_id='jiangli',
        reviewer_id='jiangli',
        proposal_uid=p_live_edit,
        user_verdict='edit_accept',
        note='clarified problem statement',
        edited_payload={
            'category': '业务经验',
            'problem': 'Edited problem statement for experience v2',
            'solution': 'Edited actionable verification steps.',
            'reusable_experience': 'Edited reusable checklist before PG cutover.',
            'importance': 9,
        },
    )
    assert res_edit['note']['category'] == '业务经验'
    assert res_edit['note']['problem'] == 'Edited problem statement for experience v2'

    from evolvmem.experience_v2.gate.review_inbox import ReviewInboxError
    with pytest.raises(ReviewInboxError) as bad_reason:
        svc.decide(conn, owner_id='jiangli', reviewer_id='jiangli', proposal_uid=p_live_2, user_verdict='reject', user_reason='not_a_valid_code')
    assert bad_reason.value.code == 'invalid_reason_code'
    # Reject -> does NOT write to experience_notes
    note_count_before = conn.execute('SELECT COUNT(*) FROM experience_notes').fetchone()[0]
    res_rej = svc.decide(conn, owner_id='jiangli', reviewer_id='jiangli', proposal_uid=p_live_2, user_verdict='reject', user_reason='wrong_scope')
    assert res_rej['state'] == 'rejected'
    assert conn.execute('SELECT COUNT(*) FROM experience_notes').fetchone()[0] == note_count_before

    # Rescue -> admits rejected backfill item via L4 ticket without leakage into live stats
    res_rescue = svc.decide(conn, owner_id='jiangli', reviewer_id='jiangli', proposal_uid=p_backfill_1, user_verdict='rescue', note='rescued legitimate pitfall')
    assert res_rescue['note']['id'].startswith('exp-')
    assert res_rescue['state'] == 'accepted'

    # Batch reject remaining quarantined backfill item
    batch_res = svc.batch_decide(
        conn,
        owner_id='jiangli',
        reviewer_id='jiangli',
        proposal_uids=[p_backfill_2],
        user_verdict='reject',
        user_reason='other',
    )
    assert batch_res['processed'] == 1
    assert svc.list_inbox(conn, owner_id='jiangli', source_track='live')['items'] == []


def test_review_inbox_http_routes_auth_csrf_and_fail_closed(pg_exp_schema, tmp_path, monkeypatch):
    conn, schema, dsn = pg_exp_schema
    apply_experience_migrations(conn)
    p_live = _seed_proposal(conn, owner_id='jiangli', source_track='live', state='review')
    p_back = _seed_proposal(conn, owner_id='jiangli', source_track='backfill', state='review')

    secret_file = tmp_path / 'ticket.secret'
    secret_file.write_text('task28-http-ticket-secret', encoding='utf-8')
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_DSN', str(dsn))
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_SCHEMA', schema)
    monkeypatch.setenv('EVOLVMEM_EXPERIENCE_TICKET_SECRET_FILE', str(secret_file))

    fixture_mod = _load_fixture_module()
    fix = fixture_mod.Fixture(tmp_path / 'console')
    (tmp_path / 'console' / 'product-policy.json').write_text(json.dumps({
        'console_enabled': True,
        'legacy_web_enabled': False,
        'ai_processing_enabled': False,
        'experience_v2_enabled': True,
        'experience_gate_mode': 'review',
    }), encoding='utf-8')
    fix.start()
    try:
        port = fix.server.server_port
        # Static assets & HTML shell include review inbox
        s_js, body_js = _http(port, '/review-inbox.js')
        s_css, _ = _http(port, '/review-inbox.css')
        s_html, body_html = _http(port, '/')
        assert s_js == 200 and 'review-inbox' in body_js
        assert s_css == 200
        assert s_html == 200 and 'review-inbox.js' in body_html

        # 401 when unauthenticated
        s_unauth, _ = _http(port, '/api/knowledge-console/review-inbox?source_track=live', actor=None)
        assert s_unauth == 401

        # Owner gets masked shadow-mode items separated by track
        s_list, payload = _http(port, '/api/knowledge-console/review-inbox?source_track=live', actor='owner')
        assert s_list == 200
        assert payload['shadow_mode'] is True
        assert [i['uid'] for i in payload['items']] == [p_live]
        assert payload['items'][0].get('gate_verdict') is None

        s_back, back_payload = _http(port, '/api/knowledge-console/review-inbox?source_track=backfill', actor='owner')
        assert s_back == 200
        assert [i['uid'] for i in back_payload['items']] == [p_back]

        # Member cannot see or decide owner proposals
        s_mem, mem_payload = _http(port, '/api/knowledge-console/review-inbox?source_track=live', actor='member')
        assert s_mem == 200
        assert mem_payload['items'] == []
        s_mem_dec, _ = _http(
            port,
            '/api/knowledge-console/review-inbox/decide',
            actor='member',
            method='POST',
            csrf=True,
            body={'proposal_uid': p_live, 'user_verdict': 'accept'},
        )
        assert s_mem_dec == 404

        # POST requires valid CSRF
        s_nocsrf, err_nocsrf = _http(
            port,
            '/api/knowledge-console/review-inbox/decide',
            actor='owner',
            method='POST',
            csrf=False,
            body={'proposal_uid': p_live, 'user_verdict': 'accept'},
        )
        assert s_nocsrf == 403 and err_nocsrf['error'] == 'invalid_csrf'

        # Owner POST decide creates note in PG
        s_ok, ok_body = _http(
            port,
            '/api/knowledge-console/review-inbox/decide',
            actor='owner',
            method='POST',
            csrf=True,
            body={'proposal_uid': p_live, 'user_verdict': 'accept', 'note': 'verified'},
        )
        assert s_ok == 200 and ok_body['note']['id'].startswith('exp-')

        # Fail closed (503) when PG unreachable while experience_v2_enabled=True
        monkeypatch.setenv('EVOLVMEM_EXPERIENCE_PG_DSN', 'host=127.0.0.1 port=59999 dbname=postgres user=postgres password=x connect_timeout=1')
        s_503, err_503 = _http(port, '/api/knowledge-console/review-inbox?source_track=live', actor='owner')
        assert s_503 == 503 and err_503['error'] == 'pg_experience_unavailable'
    finally:
        fix.close()


def test_task28_ast_and_file_line_limits():
    assert scan_unauthorized_experience_notes_writes(REPO_ROOT) == []
    for rel in (
        'evolvmem/experience_v2/gate/review_inbox.py',
        'evolvmem/experience_v2/gate/review_http.py',
        'evolvmem/web_static/review-inbox.js',
        'evolvmem/web_static/review-inbox.css',
        'evolvmem/web_static/knowledge-console.html',
    ):
        p = REPO_ROOT / rel
        if p.exists():
            assert len(p.read_text(encoding='utf-8').splitlines()) <= 600
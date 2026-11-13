"""Behavioral + AST guard tests for Task 2.2: AdmissionTicket & ExperienceWriter."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from evolvmem.experience_v2.schema import apply_experience_migrations
from evolvmem.experience_v2.writer import (
    AdmissionTicket,
    ExperienceWriteError,
    ExperienceWriter,
    Op,
    TicketIssuer,
    TicketVerificationError,
    scan_unauthorized_experience_notes_writes,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET = b'test-stage2-hmac-secret-key-0001'


def _seed_proposal_and_decision(conn, *, proposal_uid: str, decision_id: str, verdict: str = 'pass'):
    conn.execute(
        """
        INSERT INTO experience_proposals(
            uid, owner_id, op, category, kind, problem, solution, reusable_experience,
            payload_json, evidence_json, source_kind, source_track, agent_client, model,
            actor_id, request_id, state
        ) VALUES (
            %s, 'user-1', 'add', '系统环境经验', 'pitfall',
            '触发环境问题', '具体解决办法步骤', '通用复用经验规则',
            '{}'::jsonb, '[{"source":"archive:1","event":1}]'::jsonb,
            'agent', 'live', 'codex', 'gpt-5.4', 'user-1', %s, 'review'
        )
        """,
        (proposal_uid, f'prop-{proposal_uid}'),
    )
    conn.execute(
        """
        INSERT INTO proposal_decisions(decision_id, proposal_uid, layer, verdict, reason_code, confidence, judge_model)
        VALUES (%s, %s, 'L3', %s, NULL, 0.95, 'judge-model')
        """,
        (decision_id, proposal_uid, verdict),
    )


def test_missing_or_forged_ticket_is_rejected(pg_exp_schema):
    conn, _, _ = pg_exp_schema
    apply_experience_migrations(conn)
    issuer = TicketIssuer(SECRET)
    writer = ExperienceWriter(SECRET)

    op = Op(
        action='create',
        owner_id='user-1',
        actor_id='user-1',
        request_id='req-no-ticket-1',
        category='前端经验',
        problem='按钮宽度错位',
        solution='改为 flex-shrink: 0',
        reusable_experience='固定操作区禁用 flex 收缩',
        origin='manual',
    )

    with pytest.raises(TicketVerificationError):
        writer.apply(conn, None, op)

    valid_fast = issuer.issue(proposal_uid='fast-req-1', decision_id='dec-fast-1', lane='user_fast')
    tampered = replace(valid_fast, proposal_uid='fast-req-evil')
    with pytest.raises(TicketVerificationError):
        writer.apply(conn, tampered, op)

    # Agent/pipeline origin cannot bypass gate using user_fast lane
    agent_op = replace(op, request_id='req-agent-bypass-1', origin='agent', agent_client='codex', model='gpt-5.4')
    with pytest.raises(TicketVerificationError):
        writer.apply(conn, valid_fast, agent_op)

    # Even valid HMAC with gated lane fails if proposal_decisions has no pass/merge decision
    forged_gated = issuer.issue(proposal_uid='prop-missing', decision_id='dec-missing', lane='gated')
    with pytest.raises(TicketVerificationError):
        writer.apply(conn, forged_gated, agent_op)

    # Reject verdict in proposal_decisions is also blocked
    _seed_proposal_and_decision(conn, proposal_uid='prop-rej-1', decision_id='dec-rej-1', verdict='reject')
    rej_ticket = issuer.issue(proposal_uid='prop-rej-1', decision_id='dec-rej-1', lane='gated')
    with pytest.raises(TicketVerificationError):
        writer.apply(conn, rej_ticket, agent_op)

    assert conn.execute('SELECT count(*) FROM experience_notes').fetchone()[0] == 0


def test_writer_crud_cas_idempotency_and_gated_commit(pg_exp_schema):
    conn, _, _ = pg_exp_schema
    apply_experience_migrations(conn)
    issuer = TicketIssuer(SECRET)
    writer = ExperienceWriter(SECRET)

    t1 = issuer.issue(proposal_uid='fast-c-1', decision_id='dec-c-1', lane='user_fast')
    create_op = Op(
        action='create',
        owner_id='user-1',
        actor_id='user-1',
        request_id='req-create-0001',
        category='前端经验',
        problem='窄屏容器溢出',
        solution='设置 min-width: 0 与 overflow-wrap: anywhere',
        reusable_experience='Flex 子项需显式允许收缩换行',
        origin='manual',
    )
    res1 = writer.apply(conn, t1, create_op)
    assert res1['http_status'] == 201 and res1['replayed'] is False
    note = res1['experience']
    assert note['id'].startswith('manual-') and note['revision'] == 1

    # Replay same request_id returns cached response without duplicate event
    res1_replay = writer.apply(conn, t1, create_op)
    assert res1_replay['replayed'] is True and res1_replay['experience']['id'] == note['id']
    assert conn.execute('SELECT count(*) FROM experience_events').fetchone()[0] == 1

    # Conflict on same request_id with altered payload
    with pytest.raises(ExperienceWriteError) as exc_info:
        writer.apply(conn, t1, replace(create_op, problem='篡改内容'))
    assert exc_info.value.status == 409

    # Update + stale revision CAS conflict + soft delete + restore
    t2 = issuer.issue(proposal_uid='fast-u-1', decision_id='dec-u-1', lane='user_fast')
    res2 = writer.apply(
        conn,
        t2,
        Op(
            action='update',
            owner_id='user-1',
            actor_id='user-1',
            request_id='req-update-0001',
            note_id=note['id'],
            expected_revision=1,
            category='业务经验',
            problem='更新后问题',
            solution='更新后办法',
            reusable_experience='更新后规则',
        ),
    )
    assert res2['experience']['revision'] == 2

    with pytest.raises(ExperienceWriteError) as stale_exc:
        writer.apply(
            conn,
            t2,
            Op(
                action='update',
                owner_id='user-1',
                actor_id='user-1',
                request_id='req-update-stale',
                note_id=note['id'],
                expected_revision=1,
                category='业务经验',
                problem='旧版本覆盖',
                solution='x',
                reusable_experience='y',
            ),
        )
    assert stale_exc.value.status == 409

    t3 = issuer.issue(proposal_uid='fast-d-1', decision_id='dec-d-1', lane='user_fast')
    res3 = writer.apply(
        conn,
        t3,
        Op(
            action='delete',
            owner_id='user-1',
            actor_id='user-1',
            request_id='req-delete-0001',
            note_id=note['id'],
            expected_revision=2,
        ),
    )
    assert res3['experience']['revision'] == 3 and res3['experience']['deleted_at'] is not None

    t4 = issuer.issue(proposal_uid='fast-r-1', decision_id='dec-r-1', lane='user_fast')
    res4 = writer.apply(
        conn,
        t4,
        Op(
            action='restore',
            owner_id='user-1',
            actor_id='user-1',
            request_id='req-restore-0001',
            note_id=note['id'],
            expected_revision=3,
        ),
    )
    assert res4['experience']['revision'] == 4 and res4['experience']['deleted_at'] is None

    # Valid gated agent write with decision in DB succeeds with exp-... id
    _seed_proposal_and_decision(conn, proposal_uid='prop-ok-1', decision_id='dec-ok-1', verdict='pass')
    tg = issuer.issue(proposal_uid='prop-ok-1', decision_id='dec-ok-1', lane='gated')
    res_g = writer.apply(
        conn,
        tg,
        Op(
            action='create',
            owner_id='user-1',
            actor_id='user-1',
            request_id='req-gated-0001',
            category='系统环境经验',
            problem='页面退后台定时器挂起',
            solution='计时放在 Windows 客户端执行',
            reusable_experience='后台浏览器标签不可依赖 setTimeout',
            origin='agent',
            kind='env_fact',
            agent_client='codex',
            model='gpt-5.4',
        ),
    )
    assert res_g['experience']['id'].startswith('exp-')
    assert res_g['experience']['proposal_uid'] == 'prop-ok-1'


def test_repo_ast_guard_blocks_unauthorized_experience_notes_writes(tmp_path):
    assert scan_unauthorized_experience_notes_writes(REPO_ROOT) == []

    # Synthetic repo violation checks (string literal and f-string)
    (tmp_path / 'evolvmem' / 'experience_v2').mkdir(parents=True)
    (tmp_path / 'evolvmem' / 'experience_v2' / 'writer.py').write_text(
        'SQL = "INSERT INTO experience_notes(id) VALUES (%s)"\n',
        encoding='utf-8',
    )
    (tmp_path / 'evolvmem' / 'rogue.py').write_text(
        'def bypass(c, t):\n    c.execute(f"UPDATE {t} SET status=\'active\'"); c.execute("DELETE FROM experience_notes")\n',
        encoding='utf-8',
    )
    (tmp_path / 'evolvmem' / 'rogue2.py').write_text(
        'def bypass2(c, col):\n    c.execute(f"UPDATE experience_notes SET problem={col} WHERE id=1")\n',
        encoding='utf-8',
    )
    findings = scan_unauthorized_experience_notes_writes(tmp_path)
    assert len(findings) == 2
    assert any('rogue.py' in f for f in findings)
    assert any('rogue2.py' in f for f in findings)

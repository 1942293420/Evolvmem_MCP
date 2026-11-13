"""TDD tests for Stage 3 Task 3.1: 002 migration + metered_llm + budget + AST/grep guard."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from evolvmem.experience_v2.schema import apply_experience_migrations
from evolvmem.product_policy import load_policy


def test_migration_002_applies_without_mutating_001(pg_s3_schema):
    conn, _, _ = pg_s3_schema
    applied = apply_experience_migrations(conn)
    assert '001_experience_v2_schema.sql' in applied
    assert '002_stage3_pipeline_schema.sql' in applied

    for table_name in (
        'llm_calls',
        'pipeline_segment_runs',
        'pipeline_circuit_state',
        'challenger_trials',
        'shadow_pilot_runs',
        'shadow_pilot_items',
    ):
        row = conn.execute('SELECT to_regclass(%s)', (table_name,)).fetchone()
        assert row and row[0] is not None, f'missing table {table_name}'

    wt = Path(__file__).resolve().parents[2]
    commits = subprocess.check_output(
        ['git', '-C', str(wt), 'log', '--oneline', '--', 'evolvmem/experience_v2/migrations/001_experience_v2_schema.sql'],
        text=True,
    ).strip().splitlines()
    assert len(commits) == 1


def test_metered_llm_records_usage_and_enforces_conn(pg_s3_migrated):
    from evolvmem.experience_v2.llm_meter import MeteringBypassError, metered_llm

    conn, _, _ = pg_s3_migrated

    def fake_transport(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        return {
            'text': '{"ok": true}',
            'input_tokens': 120,
            'output_tokens': 45,
            'cost_estimate': 0.000165,
        }

    with pytest.raises(MeteringBypassError):
        metered_llm(
            track='live',
            stage='extract',
            model='deepseek-v4-flash',
            messages=[{'role': 'user', 'content': 'ping'}],
            session_id='sess-1',
            max_output_tokens=200,
            conn=None,
            transport=fake_transport,
        )

    res = metered_llm(
        track='live',
        stage='extract',
        model='deepseek-v4-flash',
        messages=[{'role': 'user', 'content': 'ping'}],
        session_id='sess-1',
        max_output_tokens=200,
        conn=conn,
        proposal_uid='prop-1',
        is_shadow=True,
        transport=fake_transport,
    )
    assert res.text == '{"ok": true}'
    assert res.input_tokens == 120
    assert res.output_tokens == 45
    assert res.total_tokens == 165

    row = conn.execute(
        'SELECT track, stage, model, session_id, input_tokens, output_tokens, outcome, proposal_uid, is_shadow '
        'FROM llm_calls WHERE id = %s',
        (res.call_id,),
    ).fetchone()
    assert row == ('live', 'extract', 'deepseek-v4-flash', 'sess-1', 120, 45, 'ok', 'prop-1', True)


def test_metered_llm_budget_deferral_daily_stage_session_and_circuit(pg_s3_migrated):
    from evolvmem.experience_v2.llm_meter import BudgetConfig, BudgetDeferred, metered_llm

    conn, _, _ = pg_s3_migrated
    calls = {'count': 0}

    def fake_transport(model: str, messages: list[dict], max_output_tokens: int) -> dict:
        calls['count'] += 1
        return {'text': '{}', 'input_tokens': 250, 'output_tokens': 100, 'cost_estimate': 0.00035}

    cfg = BudgetConfig(
        live_daily_tokens=1000,
        backfill_daily_tokens=500,
        session_max_tokens=500,
        stage_daily_limits={'l2b': 300},
    )

    # 1. Session cap: 350 tokens OK, next request with max_output_tokens=200 (350+est>=500) deferred BEFORE transport!
    metered_llm(
        track='live', stage='extract', model='deepseek-v4-flash',
        messages=[{'role': 'user', 'content': 'hello'}],
        session_id='sess-cap', max_output_tokens=100, conn=conn,
        budget=cfg, transport=fake_transport,
    )
    assert calls['count'] == 1
    with pytest.raises(BudgetDeferred) as exc_sess:
        metered_llm(
            track='live', stage='extract', model='deepseek-v4-flash',
            messages=[{'role': 'user', 'content': 'hello'}],
            session_id='sess-cap', max_output_tokens=200, conn=conn,
            budget=cfg, transport=fake_transport,
        )
    assert exc_sess.value.scope == 'session'
    assert calls['count'] == 1

    # 2. Stage cap: l2b cap 300
    metered_llm(
        track='live', stage='l2b', model='strong-judge',
        messages=[{'role': 'user', 'content': 'hi'}],
        session_id='sess-s1', max_output_tokens=40, conn=conn,
        budget=cfg, transport=fake_transport,
    )
    with pytest.raises(BudgetDeferred) as exc_stage:
        metered_llm(
            track='live', stage='l2b', model='strong-judge',
            messages=[{'role': 'user', 'content': 'hi'}],
            session_id='sess-s2', max_output_tokens=40, conn=conn,
            budget=cfg, transport=fake_transport,
        )
    assert exc_stage.value.scope == 'stage'

    # 3. Daily live cap: 700 already used on live; one more live call hits 1050 then next deferred, backfill still works!
    metered_llm(
        track='live', stage='extract', model='deepseek-v4-flash',
        messages=[{'role': 'user', 'content': 'hi'}],
        session_id='sess-d1', max_output_tokens=50, conn=conn,
        budget=cfg, transport=fake_transport,
    )
    with pytest.raises(BudgetDeferred) as exc_daily:
        metered_llm(
            track='live', stage='extract', model='deepseek-v4-flash',
            messages=[{'role': 'user', 'content': 'hi'}],
            session_id='sess-d2', max_output_tokens=50, conn=conn,
            budget=cfg, transport=fake_transport,
        )
    assert exc_daily.value.scope == 'daily'

    backfill_res = metered_llm(
        track='backfill', stage='extract', model='deepseek-v4-flash',
        messages=[{'role': 'user', 'content': 'backfill ok'}],
        session_id='sess-bf-1', max_output_tokens=50, conn=conn,
        budget=cfg, transport=fake_transport,
    )
    assert backfill_res.total_tokens == 350

    # 4. Paused circuit blocks track calls before transport
    conn.execute(
        "INSERT INTO pipeline_circuit_state(owner_id, track, paused, pause_reason) VALUES ('u1', 'backfill', true, 'manual_pause')"
    )
    before = calls['count']
    with pytest.raises(BudgetDeferred) as exc_cb:
        metered_llm(
            track='backfill', stage='extract', model='deepseek-v4-flash',
            messages=[{'role': 'user', 'content': 'hi'}],
            session_id='sess-bf-2', owner_id='u1', max_output_tokens=50, conn=conn,
            budget=cfg, transport=fake_transport,
        )
    assert exc_cb.value.scope == 'circuit_paused'
    assert calls['count'] == before


def test_scan_unmetered_llm_calls_guard(tmp_path):
    from evolvmem.experience_v2.llm_meter import scan_unmetered_llm_calls

    exp_root = Path(__file__).resolve().parents[2] / 'evolvmem' / 'experience_v2'
    assert scan_unmetered_llm_calls(exp_root) == []

    bad = tmp_path / 'rogue.py'
    bad.write_text(
        'import urllib.request\ndef call():\n    return urllib.request.urlopen("http://localhost:8000/v1/chat/completions")\n',
        encoding='utf-8',
    )
    hits = scan_unmetered_llm_calls(tmp_path)
    assert hits and any('rogue.py' in h['file'] for h in hits)


def test_pipeline_switch_decoupled_from_legacy_ai_processing(tmp_path, monkeypatch):
    pfile = tmp_path / 'policy.json'
    pfile.write_text(
        json.dumps({
            'console_enabled': True,
            'legacy_web_enabled': False,
            'ai_processing_enabled': False,
            'experience_v2_enabled': True,
            'experience_pipeline_enabled': True,
            'experience_backfill_enabled': False,
        }),
        encoding='utf-8',
    )
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(pfile))
    policy = load_policy()
    assert policy.valid is True
    assert policy.ai_processing_enabled is False
    assert policy.experience_pipeline_enabled is True
    assert policy.experience_backfill_enabled is False

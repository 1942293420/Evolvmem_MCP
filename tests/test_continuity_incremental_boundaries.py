"""Final acceptance gaps: source identity survives incremental scans; reads have a byte budget."""
import json
import os
from pathlib import Path

import evolvmem.continuity_backfill as backfill
from tests.test_continuity_review_regressions import env, user, call, output, append_event


def test_mixed_project_exclusion_survives_later_append(env):
    foreign = env.root / 'another-project'
    foreign.mkdir()
    _, path = env.rollout([
        user('Work for first project'),
        {'type': 'turn_context', 'payload': {'cwd': str(foreign)}},
        user('Now work on the other project'),
        call('foreign1', 'foreign_export'), output('foreign1'),
    ])
    first = env.backfill()
    assert len(env.rows()) == 0, first
    append_event(path, call('foreign2', 'foreign_export_later'))
    append_event(path, output('foreign2'))
    second = env.backfill()
    assert len(env.rows()) == 0, second
    assert all(r.action not in {'create', 'update'} for r in second)


def test_subagent_exclusion_survives_later_append(env):
    _, path = env.rollout([user('Synthetic reviewer internal request')])
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]['payload']['source'] = {'subagent': {'other': 'guardian'}}
    path.write_text(''.join(json.dumps(r) + '\n' for r in rows))
    os.utime(path, (100, 100))
    env.backfill()
    assert len(env.rows()) == 0
    append_event(path, call('late-review', 'review_internal'))
    append_event(path, output('late-review'))
    later = env.backfill()
    assert len(env.rows()) == 0, later


def test_confirmed_checkpoint_id_is_reused_even_when_objective_is_paraphrased(env):
    manual = env.begin('Implement approval dictionary display')
    confirmed = {'workstream_id': manual.workstream_id, 'checkpoint_revision': manual.checkpoint_revision, 'state_version': manual.state_version, 'status': 'open'}
    _, path = env.rollout([
        user('Continue the previous topic: these labels in the approval form need fixing'),
        {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'name': 'functions.exec', 'call_id': 'save', 'input': 'text(await tools.mcp__evolvmem__continuity_checkpoint(args));'}},
        {'type': 'response_item', 'payload': {'type': 'custom_tool_call_output', 'call_id': 'save', 'output': json.dumps({'content': [{'type': 'text', 'text': json.dumps(confirmed)}]})}},
    ])
    first = env.backfill()
    assert len(env.rows()) == 1, first
    append_event(path, call('later', 'inspect_fixture'))
    append_event(path, output('later'))
    later = env.backfill()
    assert len(env.rows()) == 1, later
    assert env.payload(manual.workstream_id)['checkpoint_revision'] == manual.checkpoint_revision


def test_oversized_lines_obey_byte_budget_and_eventually_progress(env, monkeypatch):
    budget = 2 * backfill.MAX_LINE_BYTES
    monkeypatch.setattr(backfill, 'MAX_BYTES_PER_SCAN', budget, raising=False)
    ident, path = env.rollout([user('Recover a synthetic long rollout'), call('pending')])
    with path.open('a') as stream:
        for _ in range(6):
            stream.write('x' * (backfill.MAX_LINE_BYTES + 37) + '\n')
    os.utime(path, (100, 100))
    env.backfill()
    state = json.loads(env.state_path.read_text())
    first = state[ident]['offset']
    assert 0 < first < path.stat().st_size, {'offset': first, 'size': path.stat().st_size}
    assert first <= budget + backfill.MAX_LINE_BYTES
    previous = first
    for _ in range(12):
        if previous == path.stat().st_size:
            break
        env.backfill()
        now = json.loads(env.state_path.read_text())[ident]['offset']
        assert now > previous, {'previous': previous, 'current': now}
        previous = now
    assert previous == path.stat().st_size
    append_event(path, output('pending'))
    env.backfill()
    row = env.rows()[0]
    saved = env.payload(row['id'])
    assert saved['blockers'] == []
    assert f'L{len(path.read_text().splitlines())}' in saved['current_step']

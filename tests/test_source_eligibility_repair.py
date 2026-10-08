"""System sub-sessions, empty archives and superseded snapshots stay out of the queue.

Server-side contract under test (all data is synthetic, under pytest's temporary
directory; the real Codex tree, database, credentials and models are untouched):

* a native system sub-session (``source={"subagent": {...}}``) is archived as a
  replayable receipt but never queued, extracted, backfilled or classified, and
  its project stays blank so archive-based project recall is never polluted;
* a plain ``forked_from_id`` user fork is still an ordinary session;
* a valid archive with no dialogue is a truthful skip, while an unreadable
  archive stays an error;
* an older whole-snapshot version never keeps retrying after the head advanced;
  independent local incremental batches never supersede each other;
* existing automatic outputs of a superseded source stop being current while a
  human decision and a shared output are preserved.
"""
import hashlib
import json
import time

from tests.test_lan_capture import transcript, upload
from tests.test_lan_sharing import lan  # noqa: F401  (pytest fixture)
from tests.test_web_server import _make_service
from tests.unit_model_fixture import model_for, run_worker, use_model
from evolvmem.config import Config
from evolvmem.context_models import ContextMode
from evolvmem.knowledge_cleaning import record, source_revision
from evolvmem.session_archive import SessionArchiver
from evolvmem.session_identity import is_incremental_batch

import pytest

GUARDIAN = {'subagent': {'other': 'guardian'}}
OTHER = 'parent-0000'


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------- synthetic input

def session_rows(session_id, payload, *, cwd=r'C:\work\demo'):
    rows = [{'type': 'session_meta', 'payload': {'id': session_id, 'cwd': cwd, **payload}}]
    if payload.get('parent_thread_id'):
        rows.append({'type': 'session_meta', 'payload': {
            'id': payload['parent_thread_id'], 'cwd': cwd, 'source': 'cli'}})
    return rows


def system_subagent_transcript(session_id='guardian-1', *, source=GUARDIAN, parent=OTHER,
                               text='系统守护子会话不应进入整理队列。'):
    rows = session_rows(session_id, {'source': source, 'parent_thread_id': parent})
    if text:
        rows.append({'type': 'response_item', 'payload': {
            'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': text}]}})
    return ('\n'.join(json.dumps(row, ensure_ascii=False) for row in rows) + '\n').encode()


def user_fork_transcript(session_id='fork-1', parent='user-parent'):
    rows = [{'type': 'session_meta', 'payload': {
        'id': session_id, 'forked_from_id': parent, 'cwd': r'C:\work\demo',
        'source': 'cli'}},
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
         'content': [{'type': 'input_text', 'text': '用户主动分叉的会话仍然有效。'}]}}]
    return ('\n'.join(json.dumps(row, ensure_ascii=False) for row in rows) + '\n').encode()


def payload_for(text, *, conversation=None):
    if conversation is None:
        conversation = [] if text is None else [{'role': 'user', 'content': text}]
    return json.dumps({'conversation': conversation, 'source': 'client_reported',
                       'session_id': 'windows-session', 'transcript': ''}, ensure_ascii=False)


def windows_external(identity, digest):
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest() + ':' + digest


# ----------------------------------------------------------------- LAN exclusions

def conn(runtime):
    return runtime.server_for('jiangli').context_service.store._connection()


def archiver(runtime):
    server = runtime.server_for('jiangli')
    return SessionArchiver(server.config, server.context_service.store)


def test_lan_guardian_subagent_is_archived_but_never_queued_or_classified(lan):
    runtime, adapter = lan
    raw = system_subagent_transcript()

    saved = upload(adapter, raw, session='guardian-1', extract=True)

    assert saved.get('status') == 'archived', saved
    assert saved['attribution_reason'] == 'subagent_session'
    assert saved['project'] == '', 'an excluded system source must not be attributed'
    assert saved['extraction_status'] != 'pending', saved
    # Raw evidence stays replayable.
    assert json.loads(archiver(runtime).read_payload(saved['archive_id']))['transcript'] == raw.decode()
    assert adapter.process_pending() == 0
    assert adapter.process_backfills(now=time.time() + 1900) == 0
    assert adapter.captures['jiangli'].claim_pending() is None
    assert adapter.captures['jiangli'].backfill_candidate(before=time.time() + 1900) is None

    # The explicit retry/assign paths are guarded server-side too.
    identity = {'device_id': 'windows-main', 'session_id': 'guardian-1'}
    retried = adapter.call_tool('jiangli', 'session_archive_retry',
                                {**identity, 'request_id': 'retry-guardian'})
    assert retried.get('error') == 'archive_source_excluded', retried
    assigned = adapter.call_tool('jiangli', 'session_archive_assign', {
        **identity, 'project': 'demo', 'request_id': 'assign-guardian'})
    assert assigned.get('error') == 'archive_source_excluded', assigned
    row = conn(runtime).execute(
        'SELECT extraction_status, project FROM lan_session_uploads WHERE sha256=?',
        (hashlib.sha256(raw).hexdigest(),)).fetchone()
    assert row['extraction_status'] != 'pending', dict(row)
    assert row['project'] == ''


def test_lan_user_fork_and_plain_session_stay_ordinary(lan):
    runtime, adapter = lan
    fork = user_fork_transcript()

    saved = upload(adapter, fork, session='fork-1', extract=True)

    assert saved.get('status') == 'archived', saved
    assert saved['attribution_reason'] == ''
    assert saved['project'] == 'demo'
    assert saved['extraction_status'] == 'pending'
    claimed = adapter.captures['jiangli'].claim_pending()
    assert claimed is not None and claimed['sha256'] == saved['source_sha256']


def test_source_subagent_without_a_parent_is_excluded_too(lan):
    _, adapter = lan
    saved = upload(adapter, system_subagent_transcript(parent=None, session_id='guardian-1'),
                   session='guardian-1', extract=True)
    assert saved['attribution_reason'] == 'subagent_session', saved
    assert saved['project'] == ''


def test_excluded_source_is_absent_from_the_unassigned_queue(lan):
    from evolvmem.history_organization import _unassigned_archives, source_refs

    runtime, adapter = lan
    saved = upload(adapter, system_subagent_transcript(), session='guardian-1', extract=True)
    service = runtime.server_for('jiangli').context_service

    assert saved['archive_id'] not in {row['id'] for row in _unassigned_archives(service)}
    assert f"archive:{saved['archive_id']}" not in {ref['key'] for ref in source_refs(service)}


# -------------------------------------------------------------- empty vs unreadable

@pytest.fixture
def config(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    return Config(data_dir=data, apply_environment=False)


@pytest.fixture
def service(config):
    instance = _make_service(config, mode=ContextMode.SHADOW)
    instance.knowledge().save_project({'project': 'demo'})
    yield instance
    instance.close()


def discover(service):
    from evolvmem.organization_arrival import discover_new
    return discover_new(service)


def auto_new(service):
    from evolvmem.organization_arrival import update_settings
    return update_settings(service, {'auto_new': True})


def tasks(service):
    return {row['source_key']: dict(row) for row in service.store._connection().execute(
        'SELECT * FROM organization_tasks ORDER BY id')}


def test_empty_valid_archive_is_skipped_without_retries(service, config):
    from evolvmem.history_organization import source_refs

    archiver = SessionArchiver(config, service.store)
    auto_new(service)
    empty = archiver.archive_session('', 'codex', windows_external(['w-device', 'w-empty'], 'a' * 64),
                                     payload_for(None))

    row = record(service, f'archive:{empty.id}')
    assert row['body'] == ''
    assert row['available'] is False, 'no dialogue is not an unreadable source'

    found = discover(service)
    task = tasks(service)[f'archive:{empty.id}']

    assert task['status'] == 'completed', task
    assert task['error_code'] == 'source_no_dialogue', task
    assert task['attempts'] == 0, 'an empty archive must never start provider retries'
    assert found['created'] == 1 and found['items'][0]['status'] == 'completed'
    # Re-discovery is idempotent and never queues provider work again.
    again = discover(service)
    assert again['created'] == 0
    assert tasks(service)[f'archive:{empty.id}']['attempts'] == 0
    assert f'archive:{empty.id}' not in {ref['key'] for ref in source_refs(service)}


def test_unreadable_archive_stays_an_error(service, config):
    archiver = SessionArchiver(config, service.store)
    auto_new(service)
    missing = archiver.archive_session('', 'codex', windows_external(['w-device', 'w-gone'], 'b' * 64),
                                       payload_for('这一段正文会被移除。'))
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE session_archives SET state='purged' WHERE id=?", (missing.id,))

    discover(service)

    task = tasks(service)[f'archive:{missing.id}']
    assert task['status'] != 'completed', task
    assert task['error_code'] != 'source_no_dialogue', task


# --------------------------------------------------------- superseded snapshots

def older_failed_task(service, archive_id, *, status='failed', attempts=3):
    key = f'archive:{archive_id}'
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE organization_tasks SET status=?,attempts=?,error_code='organization_failed',"
            "stage='queued',finished_at=?,updated_at=? WHERE source_key=?",
            (status, attempts, '2026-06-01 00:00:00', '2026-06-01 00:00:00', key))


def test_older_whole_snapshot_tasks_retire_when_the_head_advances(lan):
    """The real Windows path: a queued older whole snapshot stops retrying."""
    from evolvmem.organization_arrival import discover_new, update_settings

    runtime, adapter = lan
    service = runtime.server_for('jiangli').context_service
    update_settings(service, {'auto_new': True})
    first = upload(adapter, transcript(text='旧整份快照的正文。'), project='')
    # The older version is queued while it is still the visible current snapshot.
    assert discover_new(service)['created'] == 1
    older_failed_task(service, first['archive_id'])
    assert tasks(service)[f"archive:{first['archive_id']}"]['status'] == 'failed'
    second = upload(adapter, transcript(text='新整份快照的正文。'), project='',
                    source_order=2, current_sha256=sha(transcript(text='新整份快照的正文。')),
                    extract=True)
    assert second['archive_id'] != first['archive_id']

    found = discover_new(service)

    first_task = tasks(service)[f"archive:{first['archive_id']}"]
    second_task = tasks(service)[f"archive:{second['archive_id']}"]
    # The outdated version is marked superseded with its known successor and
    # never enters the retry loop.
    assert first_task['status'] == 'superseded', first_task
    assert first_task['error_code'] == 'source_superseded'
    assert first_task['superseded_by'] == second_task['id']
    assert first_task['attempts'] == 3, 'the historical provider attempts are not repeated'
    assert second_task['status'] == 'pending'
    assert found['created'] == 1
    assert found['reconcile']['model_calls'] == 0
    # The older version is not a pending extraction job any more; the newest one
    # is truthfully waiting for project attribution instead of a provider retry.
    older_row = service.store._connection().execute(
        'SELECT extraction_status FROM lan_session_uploads WHERE archive_id=?',
        (first['archive_id'],)).fetchone()
    newer_row = service.store._connection().execute(
        'SELECT extraction_status FROM lan_session_uploads WHERE archive_id=?',
        (second['archive_id'],)).fetchone()
    assert older_row['extraction_status'] != 'pending', dict(older_row)
    assert newer_row['extraction_status'] == 'unassigned', dict(newer_row)
    assert adapter.process_pending() == 0
    assert service.store.get_session_archive(first['archive_id'])['state'] == 'available'


def test_existing_failed_old_snapshot_task_is_retired_without_provider_work(lan):
    """A historical failed task of an older version is reconciled, not retried."""
    from evolvmem.auto_organization import reconcile_sources
    from evolvmem.organization_arrival import discover_new, update_settings

    runtime, adapter = lan
    service = runtime.server_for('jiangli').context_service
    update_settings(service, {'auto_new': True})
    first = upload(adapter, transcript(text='历史失败的旧整份快照。'), project='')
    second = upload(adapter, transcript(text='已经可用的新整份快照。'), project='', source_order=2,
                    current_sha256=sha(transcript(text='已经可用的新整份快照。')))
    assert first['archive_id'] != second['archive_id']

    # The historical shape: the superseded older version kept its failed task and
    # its own extraction result instead of being re-enqueued.
    from evolvmem.knowledge_cleaning import source_revision
    _insert_failed_task(service, f"archive:{first['archive_id']}",
                        source_revision(service, f"archive:{first['archive_id']}"))
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE lan_session_uploads SET extraction_status='failed',error='extraction_provider_unavailable' "
            "WHERE archive_id=?", (first['archive_id'],))
    assert discover_new(service)['created'] == 1

    summary = reconcile_sources(service)

    rows = tasks(service)
    assert rows[f"archive:{first['archive_id']}"]['status'] == 'superseded'
    assert rows[f"archive:{first['archive_id']}"]['superseded_by'] == rows[f"archive:{second['archive_id']}"]['id']
    assert rows[f"archive:{first['archive_id']}"]['attempts'] == 3, 'no provider work is spent on an old version'
    # discover_new already retired the old version; this explicit pass confirms
    # the state is settled and spends no model call.
    assert summary['model_calls'] == 0
    assert service.store.get_session_archive(first['archive_id'])['state'] == 'available'
    assert adapter.process_pending() == 0


def _insert_failed_task(service, source_key, revision, *, attempts=3, status='failed'):
    """The historical task shape of one superseded version, without a model call."""
    with service.store.transaction():
        service.store._connection().execute(
            'INSERT INTO organization_tasks(source_key,source_revision,rule_revision,source_snapshot,'
            "source_title,stage,status,error_code,attempts,created_at,updated_at,finished_at) "
            "VALUES(?,?,?,?,?, 'queued',?,'organization_failed',?,?,?,?)",
            (source_key, revision, 'legacy-rule-revision', '', source_key, status, attempts,
             '2026-06-01 00:00:00', '2026-06-01 00:00:00', '2026-06-01 00:00:00'))


def test_independent_incremental_batches_never_supersede_each_other(service, config):
    from evolvmem.auto_organization import reconcile_sources

    archiver = SessionArchiver(config, service.store)
    head = 'f' * 32
    first_identity = f'{head}:0-10:{"a" * 16}'
    second_identity = f'{head}:10-20:{"b" * 16}'
    assert is_incremental_batch(first_identity) and is_incremental_batch(second_identity)
    auto_new(service)
    first = archiver.archive_session('', 'codex', first_identity, payload_for('本地增量批次一。'))
    second = archiver.archive_session('', 'codex', second_identity, payload_for('本地增量批次二。'))
    assert discover(service)['created'] == 2

    summary = reconcile_sources(service)

    rows = tasks(service)
    assert rows[f'archive:{first.id}']['status'] == 'pending'
    assert rows[f'archive:{second.id}']['status'] == 'pending'
    assert summary['superseded'] == 0


def test_changed_source_for_other_reasons_is_not_silently_superseded(service, config):
    from evolvmem.auto_organization import reconcile_sources

    archiver = SessionArchiver(config, service.store)
    auto_new(service)
    changed = archiver.archive_session('', 'codex', windows_external(['w-device', 'w-lone'], 'e' * 64),
                                       payload_for('没有后继版本的可读正文。'))
    newer = archiver.archive_session('', 'codex', windows_external(['w-other', 'w-other-1'], 'f' * 64),
                                     payload_for('另一个会话的正文，不是同一来源。'))
    assert discover(service)['created'] == 2

    summary = reconcile_sources(service)

    rows = tasks(service)
    assert rows[f'archive:{changed.id}']['status'] == 'pending'
    assert rows[f'archive:{newer.id}']['status'] == 'pending'
    assert summary['superseded'] == 0


# ------------------------------------------------------------ old outputs hygiene

# Two labelled user messages, so the same source really produces two units: one
# auto-assigned and one the human re-confirms.
UNIT_ONE = '用户要求：Evo 演示项目先明确验收条件。'
UNIT_TWO = '用户要求：Evo 演示项目还要保留旧版本的原始归档。'
SNAPSHOT_TEXT = UNIT_ONE
MULTI_UNIT = [{'role': 'user', 'content': UNIT_ONE}, {'role': 'user', 'content': UNIT_TWO}]


def test_superseded_source_outputs_are_not_current_but_human_decision_survives(
        service, config, monkeypatch):
    """Existing unit_derivations safeguards: a human-confirmed output stays."""
    from evolvmem import unit_derivations
    from evolvmem.auto_organization import correct_one, task_view, unit_views

    archiver = SessionArchiver(config, service.store)
    identity = ['w-device', 'w-shared']
    auto_new(service)
    old = archiver.archive_session('', 'codex', windows_external(identity, '1' * 64),
                                   payload_for(None, conversation=MULTI_UNIT))
    assert discover(service)['created'] == 1
    settled = run_worker(service, monkeypatch, model_for(service, projects={'demo'}))
    old_task = next(t for t in settled if t['source_key'] == f'archive:{old.id}')
    units = service.store._connection().execute(
        'SELECT digest,text,item_id FROM organization_units WHERE task_id=? ORDER BY ordinal',
        (old_task['id'],)).fetchall()
    assert units, 'the fixture must produce at least one unit'

    # A human confirms one unit; that confirmation is recorded in the same
    # project-resolution row the cleanup policy reads.
    digest = units[0]['digest']
    service.knowledge().save_project({'project': 'other'})
    unit = next(u for u in unit_views(service, old_task['id']) if u['digest'] == digest)
    assert correct_one(service, {'task_id': old_task['id'], 'digest': digest, 'project': 'other',
                                 'expected_revision': unit['revision'],
                                 'reason': '人工确认保留'})['ok'] is True
    manual_item = service.store._connection().execute(
        'SELECT item_id FROM organization_units WHERE task_id=? AND digest=?',
        (old_task['id'], digest)).fetchone()['item_id']
    assert manual_item
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE context_project_resolutions SET decision_source='human' WHERE item_id=?",
            (manual_item,))
    unit_derivations.record_derivation(service, old_task['id'], digest, manual_item,
                                       kind='knowledge', project='other')

    newer = archiver.archive_session('', 'codex', windows_external(identity, '2' * 64),
                                     payload_for(None, conversation=MULTI_UNIT))
    # Normal bounded discovery reconciles the old task, but a task that carries a
    # human decision on any unit is preserved whatever its status is.
    assert discover(service)['created'] == 1
    rows = tasks(service)
    assert rows[f'archive:{old.id}']['status'] == 'pending', rows[f'archive:{old.id}']
    assert rows[f'archive:{old.id}']['superseded_by'] is None
    assert task_view(service, old_task['id'])['status'] == 'pending'

    # The human-confirmed unit and its output survive untouched.
    manual = service.store._connection().execute(
        'SELECT decision FROM organization_units WHERE task_id=? AND digest=?',
        (old_task['id'], digest)).fetchone()
    assert manual['decision'] == 'manual'
    assert service.store._connection().execute(
        'SELECT decision_source FROM context_project_resolutions WHERE item_id=?',
        (manual_item,)).fetchone()['decision_source'] == 'human'
    kept = service.store._connection().execute(
        'SELECT status FROM context_items WHERE id=?', (manual_item,)).fetchone()
    assert kept['status'] == 'active', kept
    assert rows[f'archive:{newer.id}']['status'] == 'pending'


def test_superseded_automatic_output_is_downgraded_not_left_eligible(service, config, monkeypatch):
    """An automatic output of a retired source stops being current.

    The automatic sibling is a real item row linked to the retired task through
    ``organization_units``; the existing stale-output policy must downgrade it
    even though the old task row still exists. The human-confirmed output keeps
    its status and nothing is deleted.
    """
    from evolvmem.auto_organization import correct_one, reconcile_sources, unit_views

    archiver = SessionArchiver(config, service.store)
    identity = ['w-device', 'w-downgrade']
    auto_new(service)
    old = archiver.archive_session('', 'codex', windows_external(identity, '1' * 64),
                                   payload_for(SNAPSHOT_TEXT))
    assert discover(service)['created'] == 1
    settled = run_worker(service, monkeypatch, model_for(service, projects={'demo'}))
    task = next(t for t in settled if t['source_key'] == f'archive:{old.id}')
    unit = unit_views(service, task['id'])[0]
    service.knowledge().save_project({'project': 'other'})
    assert correct_one(service, {'task_id': task['id'], 'digest': unit['digest'],
                                 'project': 'other', 'expected_revision': unit['revision'],
                                 'reason': '人工确认保留'})['ok'] is True
    conn = service.store._connection()
    manual_item = conn.execute(
        'SELECT item_id FROM organization_units WHERE task_id=? AND digest=?',
        (task['id'], unit['digest'])).fetchone()['item_id']
    assert manual_item, 'the confirmed unit must have one output item'
    with service.store.transaction():
        # The recorded human confirmation is what the stale-output policy reads.
        conn.execute("UPDATE context_project_resolutions SET decision_source='human' WHERE item_id=?",
                     (manual_item,))
        # The automatic sibling belongs to a historical (already superseded) task
        # of the same source and has no human resolution.
        conn.execute(
            "INSERT INTO organization_tasks(id,source_key,source_revision,rule_revision,source_snapshot,"
            "source_title,stage,status,error_code,attempts,created_at,updated_at,finished_at) "
            "VALUES(900,?,?,'legacy-rule','','older','queued','superseded','source_superseded',3,?,?,?)",
            (f'archive:{old.id}', source_revision(service, f'archive:{old.id}'),
             '2026-06-01 00:00:00', '2026-06-01 00:00:00', '2026-06-01 00:00:00'))
        cursor = conn.execute(
            "INSERT INTO context_items(identity_key,content_type,project,scope,status,tier,"
            "created_at,updated_at) SELECT ?,content_type,project,scope,'active',tier,?,? "
            "FROM context_items WHERE id=?", (f'auto-sibling:{task["id"]}', '2026-06-01 00:00:00',
                                              '2026-06-01 00:00:00', manual_item))
        auto_item = cursor.lastrowid
        conn.execute(
            "INSERT INTO organization_units(task_id,ordinal,digest,title,text,cleaned_text,source_start,"
            "source_end,category,role,disposition,disposition_reason,project_hint,project,decision,reason,"
            "evidence_quote,extraction_stage,item_id,created_at,updated_at) VALUES(?,1,'auto-sibling','','','',0,1,"
            "'reference','user','keep','','','demo','auto','','','done',?,?,?)",
            (900, auto_item, '2026-06-01 00:00:00', '2026-06-01 00:00:00'))
        # The worker's own task is already retired (the historical shape) while a
        # newer version of the family is current.
        conn.execute("UPDATE organization_tasks SET status='superseded',error_code='source_superseded',"
                     "superseded_by=901 WHERE id=?", (task['id'],))
    newer = archiver.archive_session('', 'codex', windows_external(identity, '2' * 64),
                                     payload_for(SNAPSHOT_TEXT))

    summary = reconcile_sources(service)

    # The already-superseded row still settles its own automatic output.
    assert summary['examined'] >= 1, summary
    states = {row['id']: row['status'] for row in conn.execute('SELECT id,status FROM context_items')}
    assert states[auto_item] == 'candidate', states
    assert states[manual_item] == 'active', 'a human-confirmed output keeps its status'
    assert conn.execute('SELECT status FROM organization_tasks WHERE id=900').fetchone()['status'] == 'superseded'
    # The existing legacy projection moves with the downgrade, never silently.
    if service.store.legacy_memory_table_exists():
        legacy = conn.execute(
            'SELECT m.status FROM memories m JOIN legacy_memory_migrations mm '
            'ON mm.legacy_memory_id=m.id WHERE mm.context_item_id=?', (auto_item,)).fetchone()
        if legacy is not None:
            assert legacy['status'] == 'candidate', dict(legacy)
    assert service.store.get_session_archive(old.id)['state'] == 'available'
    assert service.store.get_session_archive(newer.id)['state'] == 'available'
    assert conn.execute('SELECT count(*) c FROM organization_units').fetchone()['c'] == 2


def test_root_repair_helper_is_bounded_and_model_free(service, config):
    """The explicit repair entry reuses the same bounded, model-free reconcile."""
    from evolvmem.auto_organization import dispatch, reconcile_sources

    archiver = SessionArchiver(config, service.store)
    auto_new(service)
    empty = archiver.archive_session('', 'codex', windows_external(['w-device', 'w-repair'], '7' * 64),
                                     payload_for(None))
    assert discover(service)['created'] == 1

    result = dispatch(service, 'POST', '/repair', {'limit': 5})

    assert result['model_calls'] == 0
    assert set(result) >= {'superseded', 'no_dialogue', 'excluded', 'linked', 'examined', 'model_calls'}
    assert result['examined'] <= 5
    assert reconcile_sources(service, limit=1)['examined'] <= 1
    assert tasks(service)[f'archive:{empty.id}']['error_code'] == 'source_no_dialogue'


# ------------------------------------------------- review finding regressions

def test_actionable_candidates_are_never_starved_by_earlier_review_tasks(service, config):
    """Unrelated early pending/review rows must not consume the scan budget."""
    from evolvmem.auto_organization import reconcile_sources

    archiver = SessionArchiver(config, service.store)
    auto_new(service)
    decoys = []
    for index in range(25):
        decoy = archiver.archive_session(
            '', 'codex', windows_external(['decoy-device', f'decoy-{index}'], f'{index:064x}'),
            payload_for(f'与来源版本无关的待确认资料 {index}。'))
        # Real early review rows: current, readable, unrelated to any retirement.
        _insert_failed_task(service, f'archive:{decoy.id}',
                            source_revision(service, f'archive:{decoy.id}'), status='review')
        decoys.append(decoy.id)
    identity = ['starve-device', 'starve-session']
    old = archiver.archive_session('', 'codex', windows_external(identity, 'a' * 64),
                                   payload_for('将被替代的旧版本。'))
    new = archiver.archive_session('', 'codex', windows_external(identity, 'b' * 64),
                                   payload_for('当前的最新版本。'))
    # The decoys above already consumed the lowest task ids and stay in 'review'.
    _insert_failed_task(service, f'archive:{old.id}', source_revision(service, f'archive:{old.id}'),
                        status='review')
    _insert_failed_task(service, f'archive:{new.id}', source_revision(service, f'archive:{new.id}'),
                        status='pending')
    new_task = tasks(service)[f'archive:{new.id}']['id']

    summary = reconcile_sources(service, limit=5)

    rows = tasks(service)
    # One small-limit tick still reaches the later actionable row.
    assert summary['superseded'] == 1, summary
    assert summary['examined'] <= 5
    assert rows[f'archive:{old.id}']['status'] == 'superseded'
    assert rows[f'archive:{old.id}']['superseded_by'] == new_task
    assert all(rows[f'archive:{decoy}']['status'] == 'review' for decoy in decoys)
    # Repeated bounded ticks stay cheap and make no further changes.
    assert reconcile_sources(service, limit=5)['superseded'] == 0


def test_newer_history_only_version_never_retires_the_current_head(lan):
    """Head authority, not the largest archive id, decides the current version."""
    from evolvmem.auto_organization import reconcile_sources
    from evolvmem.organization_arrival import discover_new, update_settings

    runtime, adapter = lan
    service = runtime.server_for('jiangli').context_service
    update_settings(service, {'auto_new': True})
    first = upload(adapter, transcript(text='当前头版本。'), project='', source_order=1,
                   current_sha256=sha(transcript(text='当前头版本。')))
    assert discover_new(service)['created'] == 1
    first_task = tasks(service)[f"archive:{first['archive_id']}"]

    # A later history-only upload (higher archive id, no current declaration)
    # never moves the head, so it must not retire the head's own task.
    history = upload(adapter, transcript(text='迟到的历史归档版本。'), project='', source_order=2)
    assert history['archive_id'] > first['archive_id']
    _insert_failed_task(service, f"archive:{history['archive_id']}",
                        source_revision(service, f"archive:{history['archive_id']}"), status='review')

    summary = reconcile_sources(service)

    rows = tasks(service)
    assert summary['superseded'] == 1, summary
    assert rows[f"archive:{first['archive_id']}"]['status'] == 'pending'
    assert rows[f"archive:{history['archive_id']}"]['status'] == 'superseded'
    assert rows[f"archive:{history['archive_id']}"]['superseded_by'] == first_task['id']


def test_restored_earlier_archive_becomes_authoritative_again(lan):
    """A newer source_order can restore an earlier archive; the later one retires."""
    from evolvmem.auto_organization import reconcile_sources
    from evolvmem.organization_arrival import discover_new, update_settings

    runtime, adapter = lan
    service = runtime.server_for('jiangli').context_service
    update_settings(service, {'auto_new': True})
    early_raw = transcript(text='被重新捕获恢复的较早版本。')
    middle_raw = transcript(text='中间成为当前的版本。')
    early = upload(adapter, early_raw, project='', source_order=1, current_sha256=sha(early_raw))
    # The early version is queued while it is still the head.
    assert discover_new(service)['created'] == 1
    early_task = tasks(service)[f"archive:{early['archive_id']}"]
    middle = upload(adapter, middle_raw, project='', source_order=2, current_sha256=sha(middle_raw))
    _insert_failed_task(service, f"archive:{middle['archive_id']}",
                        source_revision(service, f"archive:{middle['archive_id']}"), status='pending')

    # Re-capture the earlier archive with a newer order: it is current again.
    restored = upload(adapter, early_raw, project='', source_order=3, current_sha256=sha(early_raw))
    assert restored['archive_id'] == early['archive_id'] and restored['current'] is True

    summary = reconcile_sources(service)

    rows = tasks(service)
    assert summary['superseded'] == 1, summary
    assert rows[f"archive:{early['archive_id']}"]['status'] == 'pending'
    assert rows[f"archive:{middle['archive_id']}"]['status'] == 'superseded'
    assert rows[f"archive:{middle['archive_id']}"]['superseded_by'] == early_task['id']


def test_empty_row_with_missing_ciphertext_stays_an_error(service, config, monkeypatch):
    """An available row whose payload is gone is an error, never a silent skip."""
    from evolvmem.organization_arrival import update_settings

    archiver = SessionArchiver(config, service.store)
    auto_new(service)
    empty = archiver.archive_session('', 'codex', windows_external(['w-device', 'w-corrupt'], '9' * 64),
                                     payload_for(None))
    payload_path = config.data_dir / service.store.get_session_archive(empty.id)['payload_path']
    payload_path.unlink()

    found = discover(service)

    task = tasks(service)[f'archive:{empty.id}']
    assert task['error_code'] != 'source_no_dialogue', task
    assert task['status'] != 'completed', task
    model = model_for(service, projects={'demo'})
    settled = run_worker(service, monkeypatch, model)
    final = next(t for t in settled if t['source_key'] == f'archive:{empty.id}')
    assert model.calls == [], 'an unreadable archive must fail before any model call'
    assert final['status'] == 'failed', final
    assert final['error_code'] == 'cleaning_source_unavailable', final
    assert found['items'][0]['error_code'] != 'source_no_dialogue'


def test_policy_filtered_to_empty_is_a_safe_skip_only_when_readable(service, config):
    """A saved cleaning policy may legitimately empty a readable source."""
    from evolvmem.organization_arrival import update_settings
    from evolvmem.pipeline_skills import read, save

    archiver = SessionArchiver(config, service.store)
    auto_new(service)
    source = archiver.archive_session('', 'codex', windows_external(['w-device', 'w-policy'], '5' * 64),
                                      payload_for('固定提示'))
    skill = read(service, 'cleaning')
    save(service, 'cleaning', {'expected_revision': skill['revision'],
                               'settings': {'cleaning_drop_lines': ['固定提示'],
                                            'cleaning_collapse_duplicates': True}})

    discover(service)

    task = tasks(service)[f'archive:{source.id}']
    assert task['status'] == 'completed', task
    assert task['error_code'] == 'source_no_dialogue', task
    assert task['attempts'] == 0


def test_excluded_archive_id_never_hides_an_unrelated_item(lan):
    """The excluded archive-id set must not be applied to item IDs."""
    from evolvmem.history_organization import source_refs

    runtime, adapter = lan
    service = runtime.server_for('jiangli').context_service
    saved = upload(adapter, system_subagent_transcript(), session='guardian-1')
    assert saved['attribution_reason'] == 'subagent_session'
    with service.store.transaction():
        service.store._connection().execute(
            "INSERT INTO context_items(id,identity_key,content_type,project,scope,status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (saved['archive_id'], 'plain-item', 'reference', '', 'project', 'candidate',
             '2026-06-01 00:00:00', '2026-06-01 00:00:00'))

    keys = {ref['key'] for ref in source_refs(service)}

    assert f"item:{saved['archive_id']}" in keys, keys
    assert f"archive:{saved['archive_id']}" not in keys


def test_reupload_of_excluded_source_clears_auto_project_but_keeps_human_choice(lan):
    runtime, adapter = lan
    service = runtime.server_for('jiangli').context_service
    raw = system_subagent_transcript()
    saved = upload(adapter, raw, session='guardian-1')
    archive_id = saved['archive_id']
    conn = service.store._connection()
    with service.store.transaction():
        conn.execute("UPDATE session_archives SET project='demo' WHERE id=?", (archive_id,))
        conn.execute("UPDATE lan_session_uploads SET project='demo' WHERE archive_id=?", (archive_id,))

    again = upload(adapter, raw, session='guardian-1')

    assert again['project'] == ''
    assert service.store.get_session_archive(archive_id)['project'] == ''
    # A human-saved cleaning decision is the archive's manual protection.
    with service.store.transaction():
        conn.execute("INSERT INTO knowledge_cleaning_reviews(source_key,source_revision,source_text,"
                     "cleaned_text,category,rule_revision,state,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     (f'archive:{archive_id}', '', '', '人工确认稿', 'reference', '', 'ready',
                      '2026-06-01 00:00:00'))
        conn.execute("UPDATE session_archives SET project='demo' WHERE id=?", (archive_id,))

    kept = upload(adapter, raw, session='guardian-1')

    assert service.store.get_session_archive(archive_id)['project'] == 'demo'
    assert kept['project'] == ''


def test_no_dialogue_skip_is_idempotent_with_the_rule_revision(service, config):
    """The recorded skip carries the rule revision and is never re-created."""
    archiver = SessionArchiver(config, service.store)
    auto_new(service)
    empty = archiver.archive_session('', 'codex', windows_external(['w-device', 'w-once'], '4' * 64),
                                     payload_for(None))

    assert discover(service)['created'] == 1
    task = tasks(service)[f'archive:{empty.id}']
    assert task['rule_revision'] == service.knowledge().rules.read()['revision'], task
    assert task['attempts'] == 0 and task['status'] == 'completed'

    assert discover(service)['created'] == 0

    rows = [row for row in service.store._connection().execute(
        'SELECT id FROM organization_tasks WHERE source_key=?', (f'archive:{empty.id}',))].__len__()
    assert rows == 1, 'a re-discovery must never duplicate the skip task'


def test_worker_preflight_settles_a_retired_source_without_attempts_or_model(
        service, config, monkeypatch):
    """The claim preflight catches a source retired after discovery."""
    from evolvmem.auto_organization import OrganizationWorker

    archiver = SessionArchiver(config, service.store)
    auto_new(service)
    old = archiver.archive_session('', 'codex', windows_external(['w-device', 'w-claim'], '3' * 64),
                                   payload_for('声明后又被替代的版本。'))
    assert discover(service)['created'] == 1
    task = tasks(service)[f'archive:{old.id}']
    newer = archiver.archive_session('', 'codex', windows_external(['w-device', 'w-claim'], '2' * 64),
                                     payload_for('替代后的当前版本。'))
    _insert_failed_task(service, f'archive:{newer.id}', source_revision(service, f'archive:{newer.id}'),
                        status='pending')
    newer_task = tasks(service)[f'archive:{newer.id}']['id']
    model = model_for(service, projects={'demo'})
    use_model(monkeypatch, model)

    worker = OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    try:
        claimed = worker._claim(worker._connect())
    finally:
        worker.stop()

    # The retired version is settled first and never claimed; the current one is.
    assert claimed is not None and claimed['source_key'] == f'archive:{newer.id}', claimed
    row = tasks(service)[f'archive:{old.id}']
    assert row['status'] == 'superseded', row
    assert row['attempts'] == 0, 'the preflight never spends an attempt'
    assert row['superseded_by'] == newer_task
    assert model.calls == []
    assert task['id'] == row['id']


def test_sql_identity_mirror_matches_logical_identity():
    """The SQL candidate filter must agree with logical_identity where it matters."""
    import sqlite3
    from evolvmem.session_identity import (SQL_LOGICAL_IDENTITY, is_incremental_batch,
                                           logical_identity)
    from evolvmem.local_codex_capture import batch_external_id

    # last entry: an uppercase near-miss the SQL filter treats as an independent
    # batch (the conservative direction: it is never retired by reconciliation).
    samples = [('codex', 'f' * 64 + ':' + 'a' * 64),
               ('codex', batch_external_id('5' * 64, 3, 9, 'b' * 64)),
               ('codex', 'session-without-colon'),
               ('kimi', 'session-a:part-1'),
               ('codex', 'A' * 32 + ':3-9:' + 'c' * 16)]
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TEMP TABLE t(adapter TEXT, external_session_id TEXT)')
    for adapter, value in samples:
        conn.execute('DELETE FROM t')
        conn.execute('INSERT INTO t VALUES(?,?)', (adapter, value))
        got = conn.execute('SELECT ' + SQL_LOGICAL_IDENTITY.format(alias='t') + ' FROM t').fetchone()[0]
        if is_incremental_batch(value) or got == value:
            # Conservative direction: a batch is never collapsed into a family.
            assert got == value, (adapter, value, got)
        else:
            assert got == logical_identity(adapter, value), (adapter, value, got)
    conn.close()

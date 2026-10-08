"""Cross-batch project context for independent Linux Codex incremental batches.

One rollout is archived as several immutable batches. A later batch that keeps
talking about the same project usually no longer repeats the project name, so
the organizer must connect it to the nearest verified predecessor *of the same
session* without merging archives, rewriting bytes or reusing the prior text as
this batch's evidence.

Server-side contract under test (all data is synthetic and lives under pytest's
temporary directory; the real Codex tree, database, credentials, private review
directories and models are never touched):

* a continuation batch inherits the predecessor's established project, with the
  source archive, range, unit and locatable quote shown, while the cleaned body,
  the frozen snapshot and every raw offset stay exactly as they were;
* a chain A→B→C stays stable, an explicit switch or a vague new topic breaks it,
  and a different session never inherits anything;
* missing payload, wrong session id, overlap/reverse order, a whole Windows
  snapshot, a system sub-session, a pure model hint and a string ``"true"`` are
  all refused, even when the model claims a continuation;
* 25 unrelated archives sitting between the two batches neither hide the real
  predecessor nor cause any other body to be decrypted;
* a tool gesture ("交给 DSH 执行") never moves an explicit business project, while
  a genuine switch to that tool's own project wins;
* a manual correction or withdrawal of the predecessor sends the dependent
  automatic unit back to review, keeps the human decision and its own derived
  output, and does not repeat itself on the next tick.
"""
import hashlib
import json
import os
import time

import pytest

from evolvmem.config import Config
from evolvmem.context_models import ContextMode
from evolvmem.knowledge_api import dispatch
from evolvmem.local_codex_capture import LocalCodexCapture, batch_external_id
from evolvmem.session_archive import SessionArchiver
from tests.test_incremental_archive_identity import line, write_capture_config
from tests.test_web_server import _make_service
from tests.unit_model_fixture import CONTEXT_MARKER, model_for, run_worker

SESSION = '01a0fb28-893d-7250-9445-1a2c2fe6a0ab'
OTHER_SESSION = '02b1fc39-904e-8361-a556-2b3d3ff9b1bc'
THIRD_SESSION = '03c2ad4a-a15f-9472-b667-3c4e4aa0c2cd'


@pytest.fixture
def config(tmp_path):
    data = tmp_path / 'data'
    data.mkdir()
    return Config(data_dir=data, apply_environment=False)


@pytest.fixture
def service(config):
    instance = _make_service(config, mode=ContextMode.SHADOW)
    instance.knowledge().save_project({'project': 'demo', 'display_name': 'Demo 业务项目'})
    instance.knowledge().save_project({'project': 'other'})
    instance.knowledge().save_project({'project': 'dsh', 'display_name': 'DSH 工具'})
    yield instance
    instance.close()


def org(service, route='', body=None):
    return dispatch(service, 'GET' if body is None else 'POST', 'organization' + route, body)


# ---------------------------------------------------------------- runtime capture

def meta(session):
    return line('session_meta', {'id': session, 'cwd': '/home/u/demo'})


def turn(text, answer='已按批次归档。'):
    return [
        line('response_item', {'type': 'message', 'role': 'user', 'id': 'evt-user',
                               'content': [{'type': 'input_text', 'text': text}]}),
        line('response_item', {'type': 'message', 'role': 'assistant', 'id': 'evt-ai',
                               'content': [{'type': 'output_text', 'text': answer}]}),
    ]


def rollout(service, config, tmp_path, session=SESSION):
    """A real capture root with one rollout file for ``session``."""
    roots = tmp_path / f'codex-roots-{session}'
    roots.mkdir(exist_ok=True)
    write_capture_config(config, roots)
    directory = roots / '2026' / '06' / '01'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'rollout-2026-06-01T00-00-00-{session}.jsonl'
    if not path.exists():
        path.write_text(meta(session), encoding='utf-8')
    return path


def append(path, lines):
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(''.join(lines))
    os.utime(path, (time.time(), time.time()))


def scan(service):
    return LocalCodexCapture(service.config, service.store).scan()


def head(session):
    return hashlib.sha256(session.encode()).hexdigest()[:32]


def session_archives(service, session):
    return [dict(row) for row in service.store._connection().execute(
        'SELECT * FROM session_archives WHERE external_session_id LIKE ? ORDER BY id',
        (head(session) + ':%',))]


def latest(service, session):
    rows = session_archives(service, session)
    assert rows, 'no archive was captured for this session'
    return rows[-1]['id']


def enqueue(service, archive_id):
    return org(service, '/tasks', {'items': [{'key': f'archive:{archive_id}'}]})['items'][0]['id']


def units(service, task_id):
    return org(service, '/units', {'task_id': task_id})['items']


def basis_of(service, task_id):
    return org(service, '/detail', {'task_id': task_id})['context']


# ------------------------------------------------------------- crafted archives

def craft(service, *, session, start, end, text, adapter='codex',
          kind='local_codex_jsonl', declare_range=True, subagent=False, head_session=None):
    """Archive one structurally exact local incremental batch, by hand.

    The payload mirrors what ``local_codex_capture.build_batch`` writes, so the
    refusal matrix can be driven precisely while the production identity rules
    still apply unchanged.
    """
    transcript = ''.join([meta(session)] + turn(text))
    if subagent:
        transcript = line('session_meta', {
            'id': session, 'cwd': '/home/u/demo',
            'source': {'subagent': {'other': 'guardian'}},
            'parent_thread_id': 'parent-0000'}) + ''.join(turn(text))
    digest = hashlib.sha256(transcript.encode()).hexdigest()
    payload = json.dumps({
        'conversation': [{'role': 'user', 'content': text},
                         {'role': 'assistant', 'content': '已按批次归档。'}],
        'transcript': transcript,
        'project': '',
        'source_sha256': digest,
        'source': {'kind': kind, 'adapter': adapter, 'session_id': head_session or session,
                   'file': f'/tmp/{session}.jsonl', 'start_offset': 0,
                   'end_offset': len(transcript.encode()),
                   'start_line': start if declare_range else start + 100,
                   'end_line': end if declare_range else end + 100,
                   'event_ids': []},
        'line_locations': [],
    }, ensure_ascii=False)
    external = batch_external_id(session, start, end, digest)
    return SessionArchiver(service.config, service.store).archive_session(
        '', 'codex', external, payload)


# ============================================================ A: the core contract

def test_continuation_batch_inherits_the_project_with_full_evidence(
        service, config, tmp_path, monkeypatch):
    from evolvmem import history_memory, memory_recall

    path = rollout(service, config, tmp_path)
    append(path, turn('用户要求：demo 项目先明确验收条件。'))
    assert scan(service)['archives'] == 1
    first = latest(service, SESSION)

    provider = model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement'),
        ('继续补充：批量删除还要支持撤销。', '', 'task_requirement', True),
    ])
    task_a = enqueue(service, first)
    run_worker(service, monkeypatch, provider)
    unit_a = units(service, task_a)[0]
    assert (unit_a['project'], unit_a['decision']) == ('demo', 'auto')
    assert unit_a['context'] == {}, 'the first batch has nothing to inherit'
    snapshot_a = org(service, '/detail', {'task_id': task_a})['source_text']
    conn = service.store._connection()
    history_a = conn.execute('SELECT body FROM conversation_history WHERE archive_id=?',
                             (first,)).fetchone()['body']

    # The session keeps writing; the next batch no longer repeats the project.
    append(path, turn('继续补充：批量删除还要支持撤销。'))
    assert scan(service)['archives'] == 1
    second = [row['id'] for row in session_archives(service, SESSION) if row['id'] != first][0]
    history_b = conn.execute('SELECT body FROM conversation_history WHERE archive_id=?',
                             (second,)).fetchone()['body']
    task_b = enqueue(service, second)
    run_worker(service, monkeypatch, provider)

    # The bounded background block reached the segmentation prompt.
    assert any(CONTEXT_MARKER in prompt for prompt in provider.calls)
    unit_b = units(service, task_b)[0]
    assert (unit_b['project'], unit_b['decision']) == ('demo', 'auto'), unit_b['reason']
    assert '沿用同会话前文' in unit_b['reason']
    assert f'archive:{first}' in unit_b['reason'], 'the reason must name the predecessor'
    basis = unit_b['context']
    assert basis['source_key'] == f'archive:{first}'
    assert basis['archive_id'] == first
    assert basis['project'] == 'demo'
    assert basis['unit_digest'] == unit_a['digest']
    assert (basis['start_line'], basis['end_line']) == (1, 3), basis
    assert basis['quote'] and basis['quote'] in unit_a['text']
    assert basis_of(service, task_b)['state'] == 'ready'
    # The raw offsets of this batch still index its own frozen snapshot.
    snapshot_b = org(service, '/detail', {'task_id': task_b})['source_text']
    for unit in units(service, task_b):
        assert snapshot_b[unit['source_start']:unit['source_end']] == unit['text']
    # Cleaned bodies and the older snapshot are untouched by organization.
    assert conn.execute('SELECT body FROM conversation_history WHERE archive_id=?',
                        (first,)).fetchone()['body'] == history_a
    assert conn.execute('SELECT body FROM conversation_history WHERE archive_id=?',
                        (second,)).fetchone()['body'] == history_b
    assert snapshot_a == org(service, '/detail', {'task_id': task_a})['source_text']
    # This batch's evidence locates in this batch, never in the predecessor.
    assert unit_b['evidence_quote'] and unit_b['evidence_quote'] in unit_b['text']
    assert unit_a['text'] not in unit_b['text']

    # The continuation is real history for the same project and is recallable.
    listed = [row for row in history_memory.sessions(service, 'demo') if row['summary']]
    assert any(unit_b['cleaned_text'] in row['summary'] for row in listed)
    recall = memory_recall.recall(service, {'project': 'demo', 'kind': 'history',
                                            'query': '批量删除还要支持撤销'})
    assert recall['history']
    assert any(unit_b['cleaned_text'] in row['summary'] for row in recall['history'])
    # Extraction only ever saw this batch's own messages.
    assert provider.extract_calls
    assert unit_a['text'] not in '\n'.join(provider.extract_calls)
    # Nothing was promoted by the inheritance: no verification claim exists and
    # the generated knowledge stays a candidate.
    assert conn.execute(
        "SELECT COUNT(*) c FROM context_evidence e JOIN context_items i ON i.id=e.item_id "
        "WHERE i.project='demo'").fetchone()['c'] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM context_items WHERE project='demo' AND success_count>0"
    ).fetchone()['c'] == 0
    assert conn.execute(
        "SELECT COUNT(*) c FROM context_items WHERE project='demo' AND content_type='experience' "
        "AND status='active'").fetchone()['c'] == 0

    # Repeating the batch never duplicates history or knowledge.
    before = conn.execute("SELECT COUNT(*) c FROM context_items WHERE project='demo'").fetchone()['c']
    org(service, '/retry', {'task_id': task_b})
    run_worker(service, monkeypatch, provider)
    after = conn.execute("SELECT COUNT(*) c FROM context_items WHERE project='demo'").fetchone()['c']
    assert after == before, 'a retried batch must not duplicate its outputs'
    assert units(service, task_b)[0]['project'] == 'demo'


def test_a_batch_naming_its_own_project_records_no_context_dependency(
        service, config, tmp_path, monkeypatch):
    from evolvmem import organization_context as context

    path = rollout(service, config, tmp_path)
    provider = model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement'),
        ('demo 项目第二批：撤销也要逐条确认。', '', 'task_requirement', True),
    ])
    append(path, turn('用户要求：demo 项目先明确验收条件。'))
    scan(service)
    ids = [latest(service, SESSION)]
    task_a = enqueue(service, ids[0])
    run_worker(service, monkeypatch, provider)
    assert units(service, task_a)[0]['project'] == 'demo'

    append(path, turn('demo 项目第二批：撤销也要逐条确认。'))
    scan(service)
    ids.append(latest(service, SESSION))
    task_b = enqueue(service, ids[1])
    run_worker(service, monkeypatch, provider)
    unit_b = units(service, task_b)[0]
    # The batch names the project itself: the answer is its own, not the
    # predecessor's, so it must not become a dependency that a later edit of the
    # predecessor could drag back to review.
    assert (unit_b['project'], unit_b['decision']) == ('demo', 'auto')
    assert '正文只点名一个已登记项目' in unit_b['reason'], unit_b['reason']
    assert unit_b['context_basis'] == ''
    assert context.dependency_rows(service, task_b) == []
    assert context.invalidate_dependents(service, limit=20)['invalidated'] == 0


def test_the_backlog_orders_same_session_batches_before_organizing(
        service, config, tmp_path, monkeypatch):
    """A whole session enqueued at once is still organized oldest batch first."""
    path = rollout(service, config, tmp_path)
    ids = []
    for text in ('用户要求：demo 项目先明确验收条件。', '第一步：批量删除必须逐条确认。',
                 '第二步：撤销也要逐条确认。'):
        append(path, turn(text))
        assert scan(service)['archives'] == 1
        ids.append(latest(service, SESSION))
    result = org(service, '/backlog', {'limit': 20})
    assert [item['source_key'] for item in result['items']] == [f'archive:{i}' for i in ids]
    provider = model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement'),
        ('第一步：批量删除必须逐条确认。', '', 'task_requirement', True),
        ('第二步：撤销也要逐条确认。', '', 'task_requirement', True),
    ])
    run_worker(service, monkeypatch, provider)
    got = [units(service, item['id']) for item in result['items']]
    assert [unit[0]['project'] for unit in got] == ['demo'] * 3, [u[0]['reason'] for u in got]


def test_a_three_batch_chain_stays_stable(service, config, tmp_path, monkeypatch):
    path = rollout(service, config, tmp_path)
    provider = model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement'),
        ('第一步：批量删除必须逐条确认。', '', 'task_requirement', True),
        ('第二步：撤销也要逐条确认。', '', 'task_requirement', True),
    ])
    ids = []
    for text in ('用户要求：demo 项目先明确验收条件。', '第一步：批量删除必须逐条确认。',
                 '第二步：撤销也要逐条确认。'):
        append(path, turn(text))
        assert scan(service)['archives'] == 1
        ids.append(latest(service, SESSION))
    task_a, task_b, task_c = (enqueue(service, archive) for archive in ids)
    run_worker(service, monkeypatch, provider)
    # The worker drains the queue oldest first, so all three settle together.
    unit_a, unit_b, unit_c = (units(service, task)[0] for task in (task_a, task_b, task_c))
    assert [unit_a['project'], unit_b['project'], unit_c['project']] == ['demo'] * 3
    assert unit_a['context'] == {}
    assert unit_b['context']['source_key'] == f'archive:{ids[0]}'
    # C rests on B, which in turn rests on A: a real chain, not a guess.
    assert unit_c['context']['source_key'] == f'archive:{ids[1]}'
    assert unit_c['context']['unit_digest'] == unit_b['digest']
    assert unit_c['context']['project'] == 'demo'


def test_an_explicit_switch_repoints_the_context(service, config, tmp_path, monkeypatch):
    from evolvmem import organization_context as context

    provider = model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement'),
        ('现在转到 other 项目，先冻结接口。', '', 'task_requirement'),
        ('接下来继续补充那批删除规则。', '', 'task_requirement', True),
    ])
    path = rollout(service, config, tmp_path)
    append(path, turn('用户要求：demo 项目先明确验收条件。'))
    scan(service)
    ids = [latest(service, SESSION)]
    task_a = enqueue(service, ids[0])
    run_worker(service, monkeypatch, provider)
    assert units(service, task_a)[0]['project'] == 'demo'

    append(path, turn('现在转到 other 项目，先冻结接口。'))
    scan(service)
    ids.append(latest(service, SESSION))
    task_x = enqueue(service, ids[1])
    run_worker(service, monkeypatch, provider)
    unit_x = units(service, task_x)[0]
    assert (unit_x['project'], unit_x['decision']) == ('other', 'auto')
    assert unit_x['context'] == {}, 'an explicit switch must not inherit the old basis'

    append(path, turn('接下来继续补充那批删除规则。'))
    scan(service)
    ids.append(latest(service, SESSION))
    task_c = enqueue(service, ids[2])
    run_worker(service, monkeypatch, provider)
    unit_c = units(service, task_c)[0]
    # The old demo context is broken: C continues X, so it continues `other`.
    assert unit_c['project'] == 'other', unit_c['reason']
    assert unit_c['context']['source_key'] == f'archive:{ids[1]}'
    assert context.validate(service, unit_c['context']) is True


def test_a_vague_new_topic_ends_the_context(service, config, tmp_path, monkeypatch):
    provider = model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement'),
        ('换个话题，先聊聊天气和行程。', '', 'reference'),
        ('继续补充：批量删除还要支持撤销。', '', 'task_requirement', True),
    ])
    path = rollout(service, config, tmp_path)
    ids = []
    for text in ('用户要求：demo 项目先明确验收条件。', '换个话题，先聊聊天气和行程。',
                 '继续补充：批量删除还要支持撤销。'):
        append(path, turn(text))
        scan(service)
        ids.append(latest(service, SESSION))
    task_a = enqueue(service, ids[0])
    run_worker(service, monkeypatch, provider)
    assert units(service, task_a)[0]['project'] == 'demo'
    task_y = enqueue(service, ids[1])
    run_worker(service, monkeypatch, provider)
    assert units(service, task_y)[0]['decision'] == 'review'
    task_c = enqueue(service, ids[2])
    run_worker(service, monkeypatch, provider)
    unit_c = units(service, task_c)[0]
    # The vague topic is the immediate predecessor and it established nothing.
    assert (unit_c['project'], unit_c['decision']) == ('', 'review'), unit_c['reason']
    assert unit_c['context'] == {}
    assert units(service, task_a)[0]['project'] == 'demo', 'the earlier batch is untouched'


# ================================================== B: refusals are conservative

def test_a_different_session_never_inherits(service, config, tmp_path, monkeypatch):
    provider = model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement'),
        ('继续补充：批量删除还要支持撤销。', '', 'task_requirement', True),
    ])
    first_path = rollout(service, config, tmp_path, session=SESSION)
    append(first_path, turn('用户要求：demo 项目先明确验收条件。'))
    scan(service)
    task_a = enqueue(service, latest(service, SESSION))
    run_worker(service, monkeypatch, provider)
    assert units(service, task_a)[0]['project'] == 'demo'

    # A second rollout, a different session, with no predecessor of its own.
    other_path = rollout(service, config, tmp_path, session=OTHER_SESSION)
    append(other_path, turn('继续补充：批量删除还要支持撤销。'))
    scan(service)
    task_b = enqueue(service, latest(service, OTHER_SESSION))
    run_worker(service, monkeypatch, provider)
    unit_b = units(service, task_b)[0]
    assert (unit_b['project'], unit_b['decision']) == ('', 'review')
    assert unit_b['context'] == {}
    assert unit_b['context_basis'] == ''


def test_a_wrong_session_identity_is_refused(service):
    from evolvmem import organization_context as context

    batch = craft(service, session=SESSION, start=1, end=2,
                  text='用户要求：demo 项目先明确验收条件。', head_session=OTHER_SESSION)
    assert context.archive_info(service, f'archive:{batch.id}') is None
    assert context.build_basis(service, {'source_key': f'archive:{batch.id}'}) == {}


def test_an_overlapping_sibling_makes_the_order_untrustworthy(service):
    from evolvmem import organization_context as context

    # A(1-2) is a valid predecessor for C(5-6), until D(4-6) overlaps C.
    first = craft(service, session=SESSION, start=1, end=2,
                  text='用户要求：demo 项目先明确验收条件。')
    current = craft(service, session=SESSION, start=5, end=6,
                    text='继续补充：批量删除还要支持撤销。')
    assert context.preceding_batch(
        service, context.archive_info(service, f'archive:{current.id}'))['archive_id'] == first.id

    overlapping = craft(service, session=SESSION, start=4, end=6, text='重叠的一段。')
    assert context.build_basis(service, {'source_key': f'archive:{current.id}'}) == {}
    assert context.preceding_batch(
        service, context.archive_info(service, f'archive:{current.id}')) is None
    assert overlapping.id != current.id


def test_a_reversed_predecessor_is_refused(service):
    from evolvmem import organization_context as context

    craft(service, session=SESSION, start=9, end=10, text='后写的一段。')
    current = craft(service, session=SESSION, start=1, end=2, text='更早的一段。')
    assert context.preceding_batch(
        service, context.archive_info(service, f'archive:{current.id}')) is None
    assert context.build_basis(service, {'source_key': f'archive:{current.id}'}) == {}


def test_a_whole_windows_snapshot_is_never_a_predecessor(service):
    from evolvmem import organization_context as context

    identity = hashlib.sha256(json.dumps([SESSION]).encode()).hexdigest()
    SessionArchiver(service.config, service.store).archive_session(
        '', 'codex', f'{identity}:{"c" * 64}',
        json.dumps({'messages': [{'role': 'user', 'content': '整份快照。'}]}, ensure_ascii=False))
    snapshot = max(row['id'] for row in
                   (dict(r) for r in service.store._connection().execute(
                       'SELECT id FROM session_archives')))
    assert context.archive_info(service, f'archive:{snapshot}') is None
    current = craft(service, session=SESSION, start=3, end=4, text='继续补充删除规则。')
    assert context.build_basis(service, {'source_key': f'archive:{current.id}'}) == {}


def test_a_system_sub_session_batch_is_never_a_predecessor(service):
    from evolvmem import organization_context as context

    batch = craft(service, session=SESSION, start=1, end=2, text='系统子会话的一段。',
                  subagent=True)
    assert context.archive_info(service, f'archive:{batch.id}') is None
    assert context.build_basis(service, {'source_key': f'archive:{batch.id}'}) == {}


def test_a_declared_range_that_contradicts_the_identity_is_refused(service):
    from evolvmem import organization_context as context

    batch = craft(service, session=SESSION, start=1, end=2, text='范围不一致的一段。',
                  declare_range=False)
    assert context.archive_info(service, f'archive:{batch.id}') is None


def test_an_unreadable_predecessor_is_refused(service, config):
    from evolvmem import organization_context as context

    batch = craft(service, session=SESSION, start=1, end=2,
                  text='用户要求：demo 项目先明确验收条件。')
    row = service.store.get_session_archive(batch.id)
    (config.data_dir / row['payload_path']).unlink()
    assert context.archive_info(service, f'archive:{batch.id}') is None
    assert context.build_basis(service, {'source_key': f'archive:{batch.id}'}) == {}


def test_a_pure_model_hint_and_a_string_true_never_inherit(
        service, config, tmp_path, monkeypatch):
    from evolvmem import organization_context as context
    from evolvmem import topic_segmentation as segmentation

    path = rollout(service, config, tmp_path)
    append(path, turn('用户要求：demo 项目先明确验收条件。'))
    scan(service)
    first = latest(service, SESSION)
    task_a = enqueue(service, first)
    run_worker(service, monkeypatch, model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement')]))
    assert units(service, task_a)[0]['project'] == 'demo'

    append(path, turn('继续补充：批量删除还要支持撤销。'))
    scan(service)
    ids = [row['id'] for row in session_archives(service, SESSION) if row['id'] != first]

    class StringTrue:
        """A provider hinting the project and returning the string "true"."""

        def __call__(self, prompt, *args, **kwargs):
            if '整理分段助手' in prompt:
                return json.dumps({'units': [
                    {'start_id': 1, 'end_id': 2, 'title': '续写',
                     'cleaned_summary': '继续补充批量删除。', 'category': 'task_requirement',
                     'project_hint': 'demo', 'evidence_quote': '继续补充：批量删除还要支持撤销。',
                     'disposition': 'keep', 'disposition_reason': '',
                     'continues_context': 'true'}]}, ensure_ascii=False)
            raise AssertionError('unexpected prompt')

    task_b = enqueue(service, ids[0])
    run_worker(service, monkeypatch, StringTrue())
    unit_b = units(service, task_b)[0]
    assert unit_b['context_basis'] == '', 'a string "true" must never enable inheritance'
    # The model hint alone is refused because the text names no project.
    assert unit_b['decision'] == 'review', unit_b['reason']
    assert '正文项目证据' in unit_b['reason'] or '工具提及' in unit_b['reason'], unit_b['reason']
    parsed = segmentation._parse(json.dumps({'units': [
        {'title': 't', 'continues_context': 'true', 'start_id': 1, 'end_id': 1}]}))
    assert parsed[0]['continues_context'] is False
    assert context.STATE_READY == 'ready'


# ================================================= C: bounded neighbour selection

def test_twenty_five_unrelated_archives_between_batches_are_never_read(
        service, config, tmp_path, monkeypatch):
    path = rollout(service, config, tmp_path)
    append(path, turn('用户要求：demo 项目先明确验收条件。'))
    scan(service)
    first = latest(service, SESSION)
    provider = model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement'),
        ('继续补充：批量删除还要支持撤销。', '', 'task_requirement', True),
    ])
    task_a = enqueue(service, first)
    run_worker(service, monkeypatch, provider)

    # 25 other sessions with whole-snapshot codex identities, inserted between.
    archiver = SessionArchiver(service.config, service.store)
    for index in range(25):
        identity = hashlib.sha256(f'other-session-{index:02d}'.encode()).hexdigest()
        archiver.archive_session('', 'codex', f'{identity}:{"f" * 64}',
                                 json.dumps({'messages': [{'role': 'user',
                                                           'content': f'无关会话 {index}。'}]},
                                            ensure_ascii=False))
    others = {row['id'] for row in service.store._connection().execute(
        'SELECT id FROM session_archives')} - {first}

    reads = []
    original = SessionArchiver.read_payload

    def spy(self, archive_id):
        reads.append(archive_id)
        return original(self, archive_id)

    monkeypatch.setattr(SessionArchiver, 'read_payload', spy)
    append(path, turn('继续补充：批量删除还要支持撤销。'))
    assert scan(service)['archives'] == 1
    newest = latest(service, SESSION)
    assert newest not in others
    task_b = enqueue(service, newest)
    run_worker(service, monkeypatch, provider)

    unit_b = units(service, task_b)[0]
    assert unit_b['project'] == 'demo', unit_b['reason']
    assert unit_b['context']['archive_id'] == first
    # Only the batch itself and its real predecessor were ever decrypted.
    assert set(reads) <= {first, newest}, reads
    assert not (set(reads) & others), reads
    assert len(reads) <= 12, reads


def test_the_neighbour_window_stays_bounded(service, config, tmp_path, monkeypatch):
    from evolvmem import organization_context as context

    path = rollout(service, config, tmp_path)
    append(path, turn('用户要求：demo 项目先明确验收条件。'))
    scan(service)
    first = latest(service, SESSION)
    provider = model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement'),
        ('继续补充：批量删除还要支持撤销。', '', 'task_requirement', True),
    ])
    task_a = enqueue(service, first)
    run_worker(service, monkeypatch, provider)
    for index in range(context.SESSION_WINDOW + 3):
        append(path, turn(f'第 {index} 段无关记录，先记录一下。'))
        scan(service)
    # Many later batches exist, yet the predecessor is still the nearest one.
    rows = session_archives(service, SESSION)
    assert len(rows) >= context.SESSION_WINDOW + 3
    newest = rows[-1]['id']
    basis = context.build_basis(service, {'source_key': f'archive:{newest}'})
    assert basis['archive_id'] == rows[-2]['id']
    assert units(service, task_a)[0]['project'] == 'demo'


# ======================================================= D: tool mentions vs switch

def test_a_tool_mention_never_moves_an_explicit_business_project(
        service, config, tmp_path, monkeypatch):
    from evolvmem import organization_context as context

    path = rollout(service, config, tmp_path)
    provider = model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement'),
        ('这批整理交给 DSH 执行。', '', 'task_requirement', True),
        ('现在修改 DSH 工具，先补验收条件。', '', 'task_requirement', True),
    ])
    append(path, turn('用户要求：demo 项目先明确验收条件。'))
    scan(service)
    ids = [latest(service, SESSION)]
    task_a = enqueue(service, ids[0])
    run_worker(service, monkeypatch, provider)
    assert units(service, task_a)[0]['project'] == 'demo'

    append(path, turn('这批整理交给 DSH 执行。'))
    scan(service)
    ids.append(latest(service, SESSION))
    task_b = enqueue(service, ids[1])
    run_worker(service, monkeypatch, provider)
    unit_b = units(service, task_b)[0]
    assert unit_b['project'] == 'demo', 'a delegation to a tool is not a project switch'
    assert '沿用同会话前文' in unit_b['reason']
    assert unit_b['context']['archive_id'] == ids[0]
    assert context.mention_kind('这批整理交给 DSH 执行。', 'dsh') == 'tool_only'

    append(path, turn('现在修改 DSH 工具，先补验收条件。'))
    scan(service)
    ids.append(latest(service, SESSION))
    task_c = enqueue(service, ids[2])
    run_worker(service, monkeypatch, provider)
    unit_c = units(service, task_c)[0]
    assert unit_c['project'] == 'dsh', 'a genuine switch must beat the old context'
    assert unit_c['context_basis'] == ''
    assert context.mention_kind('现在修改 DSH 工具，先补验收条件。', 'dsh') == 'decisive'


# ============================================== E: bounded dependency invalidation

def _two_batch_chain(service, config, tmp_path, monkeypatch):
    path = rollout(service, config, tmp_path)
    provider = model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement'),
        ('继续补充：批量删除还要支持撤销。', '', 'task_requirement', True),
    ])
    append(path, turn('用户要求：demo 项目先明确验收条件。'))
    scan(service)
    first = latest(service, SESSION)
    task_a = enqueue(service, first)
    run_worker(service, monkeypatch, provider)
    append(path, turn('继续补充：批量删除还要支持撤销。'))
    scan(service)
    second = [row['id'] for row in session_archives(service, SESSION) if row['id'] != first][0]
    task_b = enqueue(service, second)
    run_worker(service, monkeypatch, provider)
    assert units(service, task_b)[0]['project'] == 'demo'
    return task_a, task_b, first, second


def test_a_corrected_predecessor_sends_the_dependent_unit_back_to_review(
        service, config, tmp_path, monkeypatch):
    from evolvmem import history_memory, organization_context as context

    task_a, task_b, _first, _second = _two_batch_chain(service, config, tmp_path, monkeypatch)
    unit_a, unit_b = units(service, task_a)[0], units(service, task_b)[0]
    assert history_memory.sessions(service, 'demo')
    assert context.dependency_rows(service, task_b)

    corrected = org(service, '/correct', {
        'task_id': task_a, 'digest': unit_a['digest'], 'project': 'other',
        'expected_revision': unit_a['revision'], 'reason': '人工核对后属于 other'})
    assert corrected['ok']
    # The human decision itself is untouched and still authoritative.
    after_a = units(service, task_a)[0]
    assert (after_a['project'], after_a['decision']) == ('other', 'manual')

    dependent = units(service, task_b)[0]
    assert dependent['decision'] == 'review', dependent['reason']
    assert dependent['project'] == ''
    assert '前文依据' in dependent['reason']
    assert dependent['revision'] != unit_b['revision']
    # Its old derived output left current use but was not deleted.
    assert not any(unit_b['cleaned_text'] in row['summary']
                   for row in history_memory.sessions(service, 'demo'))
    derivative = service.store._connection().execute(
        "SELECT COUNT(*) c FROM unit_derivations d JOIN context_items i ON i.id=d.item_id "
        'WHERE d.unit_task_id=? AND d.unit_digest=?', (task_b, unit_b['digest'])).fetchone()['c']
    assert derivative >= 1, 'history stays stored, only its current use changes'
    assert context.validate(service, dependent['context']) is False

    # The bounded pass is idempotent: a second tick changes nothing.
    again = context.invalidate_dependents(service, limit=20)
    assert again['invalidated'] == 0
    assert units(service, task_b)[0]['revision'] == dependent['revision']
    assert units(service, task_a)[0]['decision'] == 'manual'


def test_a_withdrawn_predecessor_invalidates_its_dependent(
        service, config, tmp_path, monkeypatch):
    from evolvmem import organization_context as context

    task_a, task_b, _first, _second = _two_batch_chain(service, config, tmp_path, monkeypatch)
    unit_a = units(service, task_a)[0]
    org(service, '/disposition', {'task_id': task_a, 'digest': unit_a['digest'],
                                  'disposition': 'set_aside', 'reason': '这段不算项目依据',
                                  'expected_revision': unit_a['revision']})
    dependent = units(service, task_b)[0]
    assert dependent['decision'] == 'review', dependent['reason']
    assert dependent['project'] == ''
    assert context.validate(service, dependent['context']) is False
    assert context.invalidate_dependents(service, limit=20)['invalidated'] == 0
    # Setting the predecessor aside is itself preserved.
    assert units(service, task_a)[0]['disposition'] == 'set_aside'


def test_a_manual_dependent_is_never_overwritten(service, config, tmp_path, monkeypatch):
    from evolvmem import organization_context as context

    task_a, task_b, _first, _second = _two_batch_chain(service, config, tmp_path, monkeypatch)
    unit_a, unit_b = units(service, task_a)[0], units(service, task_b)[0]
    # A human confirms the dependent's own project before the predecessor moves.
    confirmed = org(service, '/correct', {
        'task_id': task_b, 'digest': unit_b['digest'], 'project': 'demo',
        'expected_revision': unit_b['revision'], 'reason': '人工确认'})
    assert confirmed['ok']
    org(service, '/correct', {
        'task_id': task_a, 'digest': unit_a['digest'], 'project': 'other',
        'expected_revision': units(service, task_a)[0]['revision'], 'reason': '改归属'})
    after = units(service, task_b)[0]
    assert (after['project'], after['decision']) == ('demo', 'manual'), after['reason']
    assert context.invalidate_dependents(service, limit=20)['invalidated'] == 0


def test_a_retired_predecessor_archive_invalidates_its_dependent(
        service, config, tmp_path, monkeypatch):
    from evolvmem import organization_context as context

    task_a, task_b, first, _second = _two_batch_chain(service, config, tmp_path, monkeypatch)
    # A permanent delete retires the predecessor's payload while the row stays.
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE session_archives SET state='purged' WHERE id=?", (first,))
    assert context.archive_info(service, f'archive:{first}') is None
    assert context.invalidate_dependents(service, limit=20)['invalidated'] == 1
    dependent = units(service, task_b)[0]
    assert dependent['decision'] == 'review', dependent['reason']
    assert dependent['project'] == ''
    # The predecessor's own human-free automatic unit is not disturbed by this.
    assert units(service, task_a)[0]['decision'] == 'auto'


def test_the_repair_entry_point_runs_the_bounded_dependency_pass(service):
    from evolvmem.auto_organization import repair_sources

    result = repair_sources(service, limit=5)
    assert result['model_calls'] == 0
    assert 'context_invalidated' in result


# ========================================== F: production ordering and waiting

def test_discovery_across_the_five_source_limit_keeps_session_order(
        service, config, tmp_path, monkeypatch):
    """The real arrival tick, not a hand-ordered enqueue, must keep the chain."""
    from evolvmem.organization_arrival import DISCOVER_LIMIT, discover_new, update_settings

    update_settings(service, {'auto_new': True, 'expected_revision': 0})
    path = rollout(service, config, tmp_path)
    texts = ['用户要求：demo 项目先明确验收条件。']
    texts += [f'第 {index} 步：导出必须保留原始编号与日期格式。' for index in range(1, 8)]
    ids = []
    for text in texts:
        append(path, turn(text))
        assert scan(service)['archives'] == 1
        ids.append(latest(service, SESSION))
    topics = [(texts[0], '', 'task_requirement')]
    topics += [(text, '', 'task_requirement', True) for text in texts[1:]]
    provider = model_for(service, projects={'demo', 'other', 'dsh'}, topics=topics)

    first = discover_new(service)
    assert first['created'] == DISCOVER_LIMIT
    assert [item['source_key'] for item in first['items']] == \
        [f'archive:{archive}' for archive in ids[:DISCOVER_LIMIT]], 'oldest batch first'
    # The production tick keeps feeding the remaining batches, so the chain must
    # survive the limit without any manual enqueue. The loop drives the real
    # ``discover_new`` and the worker's own ``tick`` one explicit step at a time
    # and waits for every expected source to reach a terminal status; the shared
    # ``run_worker`` helper is deliberately not used here, because its stop can
    # race the worker's own idle discovery and leave the last batches unprocessed.
    settled = _drive_to_settled(service, monkeypatch, provider, ids)
    assert set(settled) == {f'archive:{archive}' for archive in ids}, settled
    conn = service.store._connection()
    assert conn.execute('SELECT COUNT(*) c FROM organization_tasks').fetchone()['c'] == len(ids)
    rows = [dict(row) for row in conn.execute(
        'SELECT project,decision FROM organization_units ORDER BY task_id')]
    assert len(rows) == len(ids)
    assert all(row['project'] == 'demo' and row['decision'] == 'auto' for row in rows), rows


TERMINAL_STATUSES = ('completed', 'review', 'failed', 'superseded')


def _drive_to_settled(service, monkeypatch, provider, archives):
    """Bounded synchronous production loop over the real discovery and worker.

    Each iteration runs one ``discover_new`` step and one worker ``tick`` — the
    same two steps the background thread performs — and stops only once every
    expected archive has a task that reached a terminal status. The bound is
    derived from the expected work (two passes per source, plus one discovery per
    source), never from a longer wait, and no task is treated as finished early.
    """
    from evolvmem.auto_organization import OrganizationWorker
    from evolvmem.context_models import ContextMode
    from evolvmem.organization_arrival import discover_new
    from tests.unit_model_fixture import use_model

    use_model(monkeypatch, provider)
    worker = OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    connection = worker._connect()
    expected = {f'archive:{archive}' for archive in archives}
    seen = {}
    try:
        for _step in range(3 * len(expected) + 5):
            discover_new(connection)
            seen = {task['source_key']: task['status'] for task in worker.tasks()}
            done = {key for key, status in seen.items() if status in TERMINAL_STATUSES}
            if expected <= done:
                return {key: seen[key] for key in expected}
            worker.tick()
        raise AssertionError('the expected sources never all settled: %r' % (seen,))
    finally:
        worker.stop()


def test_a_reversed_batch_waits_instead_of_burning_attempts(
        service, config, tmp_path, monkeypatch):
    """A continuation queued before its predecessor is deferred, not reviewed."""
    path = rollout(service, config, tmp_path)
    provider = model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement'),
        ('继续补充：批量删除还要支持撤销。', '', 'task_requirement', True),
    ])
    append(path, turn('用户要求：demo 项目先明确验收条件。'))
    scan(service)
    first = latest(service, SESSION)
    append(path, turn('继续补充：批量删除还要支持撤销。'))
    scan(service)
    second = [row['id'] for row in session_archives(service, SESSION) if row['id'] != first][0]
    # The continuation is queued first, so its task id is the lower one.
    task_b = enqueue(service, second)
    task_a = enqueue(service, first)
    assert task_b < task_a
    from evolvmem.auto_organization import OrganizationWorker
    from evolvmem.context_models import ContextMode
    worker = OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    claimed = worker._claim(worker._connect())
    assert claimed['id'] == task_a, 'the predecessor must be claimed, not its continuation'
    assert claimed['attempts'] == 1
    worker.stop()
    run_worker(service, monkeypatch, provider)
    tasks = {task['id']: task for task in org(service)['items']}
    # A deferred batch spends no extra attempt: it still settles in its normal
    # two passes (segment, then assign), never a retry caused by the wait.
    assert tasks[task_b]['attempts'] == 2, tasks
    assert not tasks[task_b]['error_code'] and not tasks[task_b]['error_detail']
    unit_b = units(service, task_b)[0]
    assert (unit_b['project'], unit_b['decision']) == ('demo', 'auto'), unit_b['reason']
    assert unit_b['context']['archive_id'] == first


# ============================================== G: changed-basis protection

def test_a_basis_changed_during_the_model_call_is_not_written(
        service, config, tmp_path, monkeypatch):
    """The predecessor is re-checked after the provider answers."""
    from tests.unit_model_fixture import UnitProvider, use_model

    path = rollout(service, config, tmp_path)
    base = model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement'),
        ('继续补充：批量删除还要支持撤销。', '', 'task_requirement', True),
    ])
    append(path, turn('用户要求：demo 项目先明确验收条件。'))
    scan(service)
    first = latest(service, SESSION)
    task_a = enqueue(service, first)
    run_worker(service, monkeypatch, base)

    class Mutating(UnitProvider):
        def segment(self, prompt):
            answer = super().segment(prompt)
            if '继续补充' in prompt:
                with service.store.transaction():
                    service.store._connection().execute(
                        'UPDATE organization_units SET revision=revision+1 WHERE task_id=?', (task_a,))
            return answer

    append(path, turn('继续补充：批量删除还要支持撤销。'))
    scan(service)
    second = [row['id'] for row in session_archives(service, SESSION) if row['id'] != first][0]
    task_b = enqueue(service, second)
    run_worker(service, monkeypatch, Mutating(projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement'),
        ('继续补充：批量删除还要支持撤销。', '', 'task_requirement', True)]))
    unit_b = units(service, task_b)[0]
    assert unit_b['decision'] == 'review', unit_b['reason']
    assert unit_b['context_basis'] == '', 'a basis changed mid-call must not be stored'
    assert org(service, '/detail', {'task_id': task_b})['context'] == {}


def test_a_source_revision_change_and_a_two_level_cascade_are_caught(
        service, config, tmp_path, monkeypatch):
    from evolvmem import organization_context as context

    path = rollout(service, config, tmp_path)
    provider = model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement'),
        ('第一步：批量删除必须逐条确认。', '', 'task_requirement', True),
        ('第二步：撤销也要逐条确认。', '', 'task_requirement', True),
    ])
    ids = []
    for text in ('用户要求：demo 项目先明确验收条件。', '第一步：批量删除必须逐条确认。',
                 '第二步：撤销也要逐条确认。'):
        append(path, turn(text))
        scan(service)
        ids.append(latest(service, SESSION))
    task_a, task_b, task_c = (enqueue(service, archive) for archive in ids)
    run_worker(service, monkeypatch, provider)
    assert [units(service, task)[0]['project'] for task in (task_a, task_b, task_c)] == ['demo'] * 3

    # A source-revision drift on the first predecessor is caught by the bounded
    # SQL candidate predicate, without decrypting anything.
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE organization_tasks SET source_revision='drifted' WHERE id=?", (task_a,))
    summary = context.invalidate_dependents(service, limit=20)
    assert summary['invalidated'] == 2, summary
    assert [units(service, task)[0]['decision'] for task in (task_b, task_c)] == ['review'] * 2
    # Idempotent: a second bounded pass finds nothing actionable.
    assert context.invalidate_dependents(service, limit=20)['invalidated'] == 0
    assert units(service, task_a)[0]['decision'] == 'auto', 'the predecessor itself is not disturbed'


# ================================================= H: derived results leave use

def test_an_invalidated_dependent_leaves_qa_history_and_recall(
        service, config, tmp_path, monkeypatch):
    from evolvmem import history_memory, memory_recall, qa_memory
    from evolvmem import organization_context as context
    from evolvmem.context_models import ContextSearchRequest

    task_a, task_b, _first, _second = _two_batch_chain(service, config, tmp_path, monkeypatch)
    unit_a, unit_b = units(service, task_a)[0], units(service, task_b)[0]
    conn = service.store._connection()
    derived = [row['item_id'] for row in conn.execute(
        'SELECT item_id FROM unit_derivations WHERE unit_task_id=?', (task_b,))]
    assert derived
    assert [row['id'] for row in qa_memory.list_items(service, {'project': 'demo'})['items']]
    assert [row['id'] for row in history_memory.sessions(service, 'demo') if row['summary']]
    hits = service.search(ContextSearchRequest(query='批量删除还要支持撤销', project='demo', top_k=10))
    assert hits, 'the derived knowledge must be recallable before the change'

    org(service, '/correct', {
        'task_id': task_a, 'digest': unit_a['digest'], 'project': 'other',
        'expected_revision': unit_a['revision'], 'reason': '人工核对后属于 other'})
    assert units(service, task_b)[0]['decision'] == 'review'

    # Every read path the user reaches drops the unconfirmed derivation.
    assert [row['id'] for row in qa_memory.list_items(service, {'project': 'demo'})['items']] == []
    assert not [row for row in history_memory.sessions(service, 'demo') if row['summary']]
    assert not service.search(ContextSearchRequest(query='批量删除还要支持撤销',
                                                   project='demo', top_k=10))
    recall = memory_recall.recall(service, {'project': 'demo', 'kind': 'both',
                                            'query': '批量删除还要支持撤销'})
    assert not recall.get('history') and not recall.get('qa')
    # Nothing was deleted: the rows and their provenance are still stored.
    assert conn.execute('SELECT COUNT(*) c FROM context_items WHERE id IN (%s)'
                        % ','.join('?' * len(derived)), derived).fetchone()['c'] == len(derived)
    assert derived and context.validate(service, units(service, task_b)[0]['context']) is False


def test_shared_and_human_backed_items_survive_an_invalidation(
        service, config, tmp_path, monkeypatch):
    from evolvmem import history_memory, qa_memory
    from evolvmem.unit_derivations import currently_backed

    task_a, task_b, first, _second = _two_batch_chain(service, config, tmp_path, monkeypatch)
    unit_a, unit_b = units(service, task_a)[0], units(service, task_b)[0]
    conn = service.store._connection()
    knowledge = [row['item_id'] for row in conn.execute(
        "SELECT item_id FROM unit_derivations WHERE unit_task_id=? AND kind='knowledge'", (task_b,))]
    assert knowledge
    # An independent source also backs one of them: it must keep its current use.
    shared = knowledge[0]
    with service.store.transaction():
        conn.execute(
            "INSERT OR IGNORE INTO context_sources(item_id,archive_id,source_kind,source_ref,"
            "extraction_version,created_at) VALUES(?,?,'session',?,'independent.v1',?)",
            (shared, first, f'independent-{shared}', '2026-01-01 00:00:00'))
    assert currently_backed(service.store, shared) is True

    org(service, '/correct', {
        'task_id': task_a, 'digest': unit_a['digest'], 'project': 'other',
        'expected_revision': unit_a['revision'], 'reason': '人工核对后属于 other'})
    assert units(service, task_b)[0]['decision'] == 'review'
    # The shared item stays current; the purely unit-derived one does not.
    assert currently_backed(service.store, shared) is True
    assert shared in [row['id'] for row in qa_memory.list_items(service, {'project': 'demo'})['items']]
    remaining = [row['item_id'] for row in conn.execute(
        "SELECT item_id FROM unit_derivations WHERE unit_task_id=? AND kind='knowledge'", (task_b,))]
    assert set(remaining) <= {shared} | {item for item in remaining if currently_backed(service.store, item)}


# ================================================ I: bytes and archive integrity

def test_observably_damaged_batch_bytes_are_never_background(
        service, config, tmp_path, monkeypatch):
    from evolvmem import organization_context as context

    path = rollout(service, config, tmp_path)
    append(path, turn('用户要求：demo 项目先明确验收条件。'))
    scan(service)
    first = latest(service, SESSION)
    task_a = enqueue(service, first)
    run_worker(service, monkeypatch, model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement')]))

    # The stored transcript no longer matches the digest the payload declares and
    # the digest its own identity is built from.
    archiver = SessionArchiver(service.config, service.store)
    row = service.store.get_session_archive(first)
    payload = json.loads(archiver.read_payload(first))
    payload['transcript'] = payload['transcript'] + '\n{"type":"response_item"}\n'
    archiver.archive_session(row['project'], row['adapter'], row['external_session_id'],
                             json.dumps(payload, ensure_ascii=False))
    assert context.archive_info(service, f'archive:{first}') is None
    assert context.build_basis(service, {'source_key': f'archive:{first}'}) == {}

    append(path, turn('继续补充：批量删除还要支持撤销。'))
    scan(service)
    second = [r['id'] for r in session_archives(service, SESSION) if r['id'] != first][0]
    task_b = enqueue(service, second)
    run_worker(service, monkeypatch, model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement'),
        ('继续补充：批量删除还要支持撤销。', '', 'task_requirement', True)]))
    unit_b = units(service, task_b)[0]
    assert unit_b['decision'] == 'review'
    assert unit_b['context_basis'] == ''
    assert task_a  # the first batch keeps its own stored result, untouched


# ==================================== J: assistant progress vs a tool mention

def test_history_only_progress_preserves_project_continuation_and_withdrawal(
        service, config, tmp_path, monkeypatch):
    """Routing progress away from extraction must not erase its verified project."""
    path = rollout(service, config, tmp_path)
    provider = model_for(service, projects={'demo','other'}, topics=[
        ('用户要求：demo 项目先明确验收条件。','','task_requirement'),
        ('正在检查上一轮的按钮','','reference',True),
        ('继续刚才的任务，原始行号必须保留','','task_requirement',True)])
    segment = provider.segment
    def tagged(prompt):
        result = json.loads(segment(prompt))
        for unit in result['units']:
            if '正在检查上一轮的按钮' in unit['title']:
                unit.update(disposition='history_only', disposition_reason='只有助手进度')
        return json.dumps(result,ensure_ascii=False)
    monkeypatch.setattr(provider,'segment',tagged)
    append(path,turn('用户要求：demo 项目先明确验收条件。'));scan(service)
    a=enqueue(service,latest(service,SESSION));run_worker(service,monkeypatch,provider)
    append(path,[line('response_item',{'type':'message','role':'assistant','id':'progress',
        'content':[{'type':'output_text','text':'正在检查上一轮的按钮，接下来继续核对。'}]})]);scan(service)
    b=enqueue(service,latest(service,SESSION));run_worker(service,monkeypatch,provider)
    assert units(service,b)[0]['disposition']=='history_only'
    assert units(service,b)[0]['project']=='demo'
    append(path,turn('继续刚才的任务，原始行号必须保留。'));scan(service)
    c=enqueue(service,latest(service,SESSION));run_worker(service,monkeypatch,provider)
    assert units(service,c)[0]['project']=='demo'
    first=units(service,a)[0]
    org(service,'/correct',{'task_id':a,'digest':first['digest'],
        'expected_revision':first['revision'],'project':'other'})
    for _ in range(3):org(service,'/repair',{})
    assert units(service,c)[0]['decision']=='review'

def test_an_assistant_progress_report_continues_the_business_project(
        service, config, tmp_path, monkeypatch):
    """The synthetic shape of the real-model regression: completed work + tool."""
    from evolvmem import organization_context as context

    path = rollout(service, config, tmp_path)
    progress = ('继续刚才的报表导出任务，已经完成保留原始编号和日期格式的修改。'
                '这次使用 DSH 执行了导出改动。')
    provider = model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement'),
        # A model that reads the supplied background fills the continuation and
        # names the tool it used as the project hint.
        (progress, 'dsh', 'task_requirement', True),
    ])
    append(path, turn('用户要求：demo 项目先明确验收条件。'))
    scan(service)
    first = latest(service, SESSION)
    task_a = enqueue(service, first)
    run_worker(service, monkeypatch, provider)

    append(path, turn(progress, answer='已按要求完成。'))
    scan(service)
    second = [row['id'] for row in session_archives(service, SESSION) if row['id'] != first][0]
    task_b = enqueue(service, second)
    run_worker(service, monkeypatch, provider)
    unit_b = units(service, task_b)[0]
    assert (unit_b['project'], unit_b['decision']) == ('demo', 'auto'), unit_b['reason']
    assert '沿用同会话前文' in unit_b['reason']
    assert unit_b['context']['archive_id'] == first
    assert context.validate(service, unit_b['context']) is True
    # The knowledge it produced is still only a candidate, never a verified claim.
    conn = service.store._connection()
    assert conn.execute("SELECT COUNT(*) c FROM context_items WHERE project='demo' "
                        'AND success_count>0').fetchone()['c'] == 0


def test_the_same_progress_report_without_a_continuation_judgement_stays_review(
        service, config, tmp_path, monkeypatch):
    """The program never invents the continuation the model did not declare."""
    path = rollout(service, config, tmp_path)
    progress = '继续刚才的报表导出任务，已经完成保留原始编号和日期格式的修改。这次使用 DSH 执行了导出改动。'
    append(path, turn('用户要求：demo 项目先明确验收条件。'))
    scan(service)
    first = latest(service, SESSION)
    task_a = enqueue(service, first)
    run_worker(service, monkeypatch, model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement')]))

    append(path, turn(progress, answer='已按要求完成。'))
    scan(service)
    second = [row['id'] for row in session_archives(service, SESSION) if row['id'] != first][0]
    task_b = enqueue(service, second)
    run_worker(service, monkeypatch, model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        ('用户要求：demo 项目先明确验收条件。', '', 'task_requirement'),
        (progress, 'dsh', 'task_requirement', False)]))
    unit_b = units(service, task_b)[0]
    assert unit_b['decision'] == 'review', unit_b['reason']
    assert unit_b['context_basis'] == ''
    assert task_a


def test_a_rewritten_predecessor_payload_invalidates_without_decrypting(
        service, config, tmp_path, monkeypatch):
    """A changed payload digest is caught by the bounded SQL scan itself."""
    from evolvmem import organization_context as context

    task_a, task_b, first, _second = _two_batch_chain(service, config, tmp_path, monkeypatch)
    assert units(service, task_b)[0]['project'] == 'demo'
    # The same identity is re-archived with different bytes: still 'available',
    # same project, same decisions, same revisions.
    archiver = SessionArchiver(service.config, service.store)
    row = service.store.get_session_archive(first)
    payload = json.loads(archiver.read_payload(first))
    payload['conversation'] = [{'role': 'user', 'content': '整段被改写过的正文。'}]
    archiver.archive_session(row['project'], row['adapter'], row['external_session_id'],
                             json.dumps(payload, ensure_ascii=False))
    assert service.store.get_session_archive(first)['state'] == 'available'
    assert context.invalidate_dependents(service, limit=20)['invalidated'] == 1
    dependent = units(service, task_b)[0]
    assert dependent['decision'] == 'review', dependent['reason']
    assert dependent['project'] == ''
    assert task_a


# ================================ K: hint paths share the business/tool split

def _decide(service, unit, hint, basis=None, names=('demo', 'other', 'dsh', 'execution-hub')):
    from evolvmem.auto_organization import OrganizationWorker
    return OrganizationWorker(service.config)._decide(
        service, unit, hint, service.knowledge().rules.read(), set(names), basis)


def test_a_tool_only_hint_never_conflicts_with_the_batchs_real_project(service):
    """The decided hint branch must reuse the tool/business split, not the raw set."""
    unit = {'text': 'demo 项目要求保留编号，使用 DSH 执行修改。',
            'evidence_quote': 'demo 项目要求保留编号', 'continues_context': 0}
    assert _decide(service, unit, 'demo') == (
        'demo', '正文与已登记项目一致，按规则自动归属', 'auto')


def test_a_tool_only_hint_never_overrides_a_real_project_switch(service, config, tmp_path, monkeypatch):
    """A real switch beats the tool gesture even when the model hints the tool."""
    _task_a, task_b, _first, _second = _two_batch_chain(service, config, tmp_path, monkeypatch)
    unit = units(service, task_b)[0]
    unit.update(text='现在切换到 other 项目，使用 DSH 执行编号修复。',
                evidence_quote='', continues_context=1)
    basis = unit['context']
    result = _decide(service, unit, 'dsh', basis)
    assert result[0] != 'demo', result
    assert result[0] == 'other' and result[2] == 'auto', result


def test_two_real_project_candidates_still_conflict_with_a_hint(service):
    """Opening the tool path must not open every hint."""
    unit = {'text': 'demo 项目与 other 项目都要保留编号。', 'evidence_quote': '',
            'continues_context': 0}
    result = _decide(service, unit, 'demo')
    assert result[2] == 'review', result
    assert '人工拆分' in result[1] or '冲突' in result[1], result


def test_a_business_project_plus_a_tool_mention_can_supply_the_next_context(
        service, config, tmp_path, monkeypatch):
    """A predecessor whose own text names a real project plus a tool stays usable."""
    from evolvmem import organization_context as context

    path = rollout(service, config, tmp_path)
    text = 'demo 项目要求保留编号，使用 DSH 执行修改。'
    provider = model_for(service, projects={'demo', 'other', 'dsh'}, topics=[
        (text, '', 'task_requirement'),
        ('继续补充：批量删除还要支持撤销。', '', 'task_requirement', True),
    ])
    append(path, turn(text))
    scan(service)
    first = latest(service, SESSION)
    task_a = enqueue(service, first)
    run_worker(service, monkeypatch, provider)
    assert units(service, task_a)[0]['project'] == 'demo'

    append(path, turn('继续补充：批量删除还要支持撤销。'))
    scan(service)
    second = [row['id'] for row in session_archives(service, SESSION) if row['id'] != first][0]
    task_b = enqueue(service, second)
    run_worker(service, monkeypatch, provider)
    unit_b = units(service, task_b)[0]
    assert unit_b['project'] == 'demo', unit_b['reason']
    assert context.validate(service, unit_b['context']) is True


def test_the_claim_window_rotates_without_changing_normal_order(
        service, config, tmp_path, monkeypatch):
    """A window of waiting tasks rotates across ticks; a claim restarts at the oldest."""
    from evolvmem.auto_organization import CLAIM_SCAN_LIMIT, OrganizationWorker
    from evolvmem.context_models import ContextMode

    path = rollout(service, config, tmp_path)
    texts = ['用户要求：demo 项目先明确验收条件。']
    texts += [f'第 {index} 步：导出必须保留原始编号。' for index in range(1, CLAIM_SCAN_LIMIT + 2)]
    archives = []
    for text in texts:
        append(path, turn(text))
        scan(service)
        archives.append(latest(service, SESSION))
    # Newest first: every task in the first scan window waits for a predecessor
    # outside that window.
    tasks = [enqueue(service, archive) for archive in reversed(archives)]
    worker = OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    claimed = None
    for _ in range(3):
        claimed = worker._claim(worker._connect())
        if claimed is not None:
            break
    assert claimed is not None, 'the real predecessor must stay reachable'
    assert claimed['source_key'] == f'archive:{archives[0]}'
    assert claimed['attempts'] == 1, 'waiting must never consume an attempt'
    worker.stop()
    # Nothing was failed or completed by the wait itself.
    conn = service.store._connection()
    rows = {row['id']: row['status'] for row in conn.execute(
        'SELECT id,status FROM organization_tasks')}
    assert rows[tasks[-1]] == 'running'
    assert all(rows[task] == 'pending' for task in tasks[:-1])


def test_a_cleaning_edit_on_the_predecessor_invalidates_the_saved_context(
        service, config, tmp_path, monkeypatch):
    """The live cleaned source is the authority, not the cached task column."""
    from evolvmem import organization_context as context
    from evolvmem.knowledge_api import dispatch

    task_a, task_b, first, _second = _two_batch_chain(service, config, tmp_path, monkeypatch)
    basis = units(service, task_b)[0]['context']
    assert context.validate(service, basis) is True
    saved = dispatch(service, 'GET', 'cleaning/detail', {'key': f'archive:{first}'})
    dispatch(service, 'POST', 'cleaning/save', {'items': [{
        'key': saved['key'], 'expected_revision': saved['expected_revision'],
        'cleaned_text': 'demo 项目：原要求撤回，原始行顺序限制已经取消。', 'category': 'reference'}]})
    # The task row's cached revision is untouched; the live source is not.
    assert service.store._connection().execute(
        'SELECT source_revision FROM organization_tasks WHERE id=?', (task_a,)).fetchone()
    assert context.validate(service, basis) is False
    assert context.invalidate_dependents(service, limit=20)['invalidated'] == 1
    dependent = units(service, task_b)[0]
    assert dependent['decision'] == 'review', dependent['reason']
    assert dependent['project'] == ''

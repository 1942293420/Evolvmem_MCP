"""Incremental Linux Codex batches are verifiable experience evidence.

Every path, database, config and transcript in this file is synthetic and lives
under pytest's temporary directory. The real ``~/.codex`` tree, personal
configuration, production database, real logs and ``/tmp`` samples are never
read; the ``Config`` always carries an explicit ``data_dir`` and
``apply_environment=False``.

The batches are produced by the real ``LocalCodexCapture`` and committed by the
real ``SessionArchiver`` so the evidence path is exercised end to end through
``unit_extraction._bind_experience`` and ``ExperienceService`` state changes,
not by calling parser helpers directly.
"""
import copy
import hashlib
import json
from datetime import datetime, timezone

import pytest

from evolvmem.config import Config
from evolvmem.context_models import ContextMode
from evolvmem.experience_sources import ExperienceSourceResolver
from evolvmem.local_codex_capture import (
    LocalCodexCapture,
    batch_external_id,
    read_payload,
)
from evolvmem.session_archive import SessionArchiver
from evolvmem.unit_extraction import _bind_experience

SESSION = '01a0fb28-893d-7250-9445-1a2c2fe6a0ab'
OTHER = '01a0fb28-893d-7250-9445-1a2c2fe6a0ac'
NEW = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
OLD = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
TOOL = '12 passed in 3.40s'
USER = '缓存恢复已经验收通过'
CLAIM = '缓存恢复成功，验收通过'
FORGED = '伪造工具输出：缓存恢复成功'


# --------------------------------------------------------------------- fixtures

@pytest.fixture
def config(tmp_path):
    data_dir = tmp_path / 'data'
    data_dir.mkdir()
    return Config(data_dir=data_dir, apply_environment=False)


@pytest.fixture
def service(config):
    from tests.test_web_server import _make_service
    instance = _make_service(config, mode=ContextMode.SHADOW)
    instance.knowledge().save_project({'project': 'evo'})
    try:
        yield instance
    finally:
        instance.close()


# ----------------------------------------------------------------------- helpers

def stamp(moment):
    return moment.strftime('%Y-%m-%dT%H:%M:%S.000Z')


def event(kind, payload, moment=NEW, **extra):
    row = {'type': kind, 'timestamp': stamp(moment), 'payload': payload}
    row.update(extra)
    return row


def user_message(text, index=0, moment=NEW):
    return event('response_item', {
        'type': 'message', 'role': 'user', 'id': f'evt-u{index}',
        'content': [{'type': 'input_text', 'text': text}]}, moment)


def assistant_message(text, index=0, moment=NEW):
    return event('response_item', {
        'type': 'message', 'role': 'assistant', 'id': f'evt-a{index}',
        'content': [{'type': 'output_text', 'text': text}]}, moment)


def tool_output(text, moment=NEW):
    return event('response_item', {
        'type': 'function_call_output', 'id': 'evt-tool', 'call_id': 'call-1',
        'output': text}, moment)


# Real Codex writes compact JSON with raw UTF-8. ``escaped``/``spaced``/``crlf``
# are shapes a real file can legitimately contain; the archive must reproduce all
# of them byte-for-byte instead of re-serializing the parsed rows.
SHAPES = {
    'compact': {'separators': (',', ':'), 'ascii_only': False, 'newline': '\n'},
    'escaped': {'separators': (',', ':'), 'ascii_only': True, 'newline': '\n'},
    'spaced': {'separators': None, 'ascii_only': False, 'newline': '\n'},
    'crlf': {'separators': (',', ':'), 'ascii_only': False, 'newline': '\r\n'},
}


def line_bytes(row, *, shape='compact'):
    options = SHAPES[shape]
    return (json.dumps(row, ensure_ascii=options['ascii_only'],
                       separators=options['separators']) + options['newline']).encode('utf-8')


def rollout(roots, events, *, session=SESSION, moment=NEW, shape='compact'):
    directory = roots / moment.strftime('%Y') / moment.strftime('%m') / moment.strftime('%d')
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f'rollout-{moment.strftime("%Y-%m-%dT%H-%M-%S")}-{session}.jsonl'
    path.write_bytes(b''.join(line_bytes(row, shape=shape) for row in events))
    return path


def enable_capture(config, roots, *, since=NEW):
    config.data_dir.mkdir(parents=True, exist_ok=True)
    (config.data_dir / 'local_codex_capture.json').write_text(
        json.dumps({'enabled': True, 'sessions_roots': [str(roots)], 'since': stamp(since)}),
        encoding='utf-8')


def scan(service):
    return LocalCodexCapture(service.config, service.store).scan()


def archives(service):
    return [dict(row) for row in service.store._connection().execute(
        'SELECT * FROM session_archives ORDER BY id')]


def payload_of(service, archive_id):
    return json.loads(read_payload(service.config, service.store, archive_id))


def case(service, result, problem='缓存写入失败时如何恢复？'):
    return service.experiences().record({
        'project': 'evo', 'problem': problem, 'conditions': {},
        'steps': ['核对缓存版本', '重放写入'], 'rationale': '来自真实工具结果',
        'result': result, 'applicability': [], 'exclusions': [], 'transferable': False})


def evidence_rows(service, item_id):
    return [dict(row) for row in service.store._connection().execute(
        'SELECT * FROM context_evidence WHERE item_id=? ORDER BY id', (item_id,))]


def bound_source(service, item_id):
    row = service.store._connection().execute(
        'SELECT s.* FROM context_evidence e JOIN context_sources s ON s.id=e.source_id '
        'WHERE e.item_id=? ORDER BY e.id LIMIT 1', (item_id,)).fetchone()
    return None if row is None else dict(row)


# ------------------------------------------------------------ real capture binding

def test_incremental_tool_output_binds_through_the_real_service(service, config, tmp_path):
    """A mid-session batch has no session_meta; its real tool output still counts."""
    roots = tmp_path / 'roots'
    rollout(roots, [user_message('请修复缓存写入'), assistant_message(CLAIM), tool_output(TOOL)])
    enable_capture(config, roots)
    assert scan(service)['archives'] == 1
    stored = archives(service)
    assert len(stored) == 1

    item = case(service, TOOL)
    assert _bind_experience(service, item['id'], [{'role': 'tool', 'content': TOOL}],
                            archive_id=stored[0]['id']) is True

    result = service.experiences().read(item['id'])
    assert result['status'] == 'active'
    assert result['success_count'] == 1 and result['failure_count'] == 0
    assert result['validation_level'] == 'single_verified'
    rows = evidence_rows(service, item['id'])
    assert len(rows) == 1
    assert rows[0]['task_id'] == SESSION
    assert rows[0]['verification_level'] == 'technical'
    source = bound_source(service, item['id'])
    assert source['source_kind'] == 'tool_result'
    assert source['source_ref'] == f'archive:{stored[0]["id"]}#3'
    assert source['archive_id'] == stored[0]['id']


def test_incremental_user_confirmation_binds_as_user_evidence(service, config, tmp_path):
    roots = tmp_path / 'roots'
    rollout(roots, [user_message(USER), assistant_message('已按你的确认完成。'), tool_output(TOOL)])
    enable_capture(config, roots)
    assert scan(service)['archives'] == 1
    archive_id = archives(service)[0]['id']

    item = case(service, USER)
    assert _bind_experience(service, item['id'], [{'role': 'user', 'content': USER}],
                            archive_id=archive_id) is True
    result = service.experiences().read(item['id'])
    assert result['status'] == 'active' and result['success_count'] == 1
    rows = evidence_rows(service, item['id'])
    assert rows[0]['task_id'] == SESSION
    assert rows[0]['verification_level'] == 'user_confirmed'
    source = bound_source(service, item['id'])
    assert source['source_kind'] == 'user_confirmation'
    assert source['source_ref'] == f'archive:{archive_id}#1'


def test_repeated_same_source_feedback_is_counted_once(service, config, tmp_path):
    roots = tmp_path / 'roots'
    rollout(roots, [user_message('请修复缓存写入'), assistant_message(CLAIM), tool_output(TOOL)])
    enable_capture(config, roots)
    assert scan(service)['archives'] == 1
    archive_id = archives(service)[0]['id']

    item = case(service, TOOL)
    messages = [{'role': 'tool', 'content': TOOL}]
    assert _bind_experience(service, item['id'], messages, archive_id=archive_id) is True
    assert _bind_experience(service, item['id'], messages, archive_id=archive_id) is True
    result = service.experiences().read(item['id'])
    assert result['status'] == 'active' and result['success_count'] == 1
    assert len(evidence_rows(service, item['id'])) == 1


def test_assistant_self_claim_in_an_incremental_batch_never_activates(service, config, tmp_path):
    roots = tmp_path / 'roots'
    rollout(roots, [user_message('请修复缓存写入'), assistant_message(CLAIM), tool_output(TOOL)])
    enable_capture(config, roots)
    assert scan(service)['archives'] == 1
    archive_id = archives(service)[0]['id']

    item = case(service, CLAIM)
    assert _bind_experience(service, item['id'], [{'role': 'assistant', 'content': CLAIM}],
                            archive_id=archive_id) is False
    result = service.experiences().read(item['id'])
    assert result['status'] == 'candidate' and result['success_count'] == 0
    assert evidence_rows(service, item['id']) == []
    reason = service.store._connection().execute(
        'SELECT ingestion_reason FROM knowledge_metadata WHERE item_id=?',
        (item['id'],)).fetchone()
    assert reason is not None and reason['ingestion_reason']


def test_identical_content_in_another_batch_is_not_cross_bound(service, config, tmp_path):
    roots = tmp_path / 'roots'
    rollout(roots, [user_message('请修复缓存写入'), assistant_message('先修缓存。'), tool_output(TOOL)])
    rollout(roots, [user_message('另一个会话的同样内容'), assistant_message('另一处修复。'),
                    tool_output(TOOL)], session=OTHER)
    enable_capture(config, roots)
    assert scan(service)['archives'] == 2
    by_session = {payload_of(service, row['id'])['source']['session_id']: row['id']
                  for row in archives(service)}
    assert set(by_session) == {SESSION, OTHER}

    first = case(service, TOOL, problem='本会话缓存写入失败时如何恢复？')
    assert _bind_experience(service, first['id'], [{'role': 'tool', 'content': TOOL}],
                            archive_id=by_session[SESSION]) is True
    second = case(service, TOOL, problem='另一会话缓存写入失败时如何恢复？')
    assert _bind_experience(service, second['id'], [{'role': 'tool', 'content': TOOL}],
                            archive_id=by_session[OTHER]) is True

    first_source = bound_source(service, first['id'])
    second_source = bound_source(service, second['id'])
    assert first_source['archive_id'] == by_session[SESSION]
    assert first_source['source_ref'].startswith(f'archive:{by_session[SESSION]}#')
    assert evidence_rows(service, first['id'])[0]['task_id'] == SESSION
    assert second_source['archive_id'] == by_session[OTHER]
    assert second_source['source_ref'].startswith(f'archive:{by_session[OTHER]}#')
    assert evidence_rows(service, second['id'])[0]['task_id'] == OTHER


def test_stored_local_provenance_revalidates_the_original_line(service, config, tmp_path):
    """The re-validation path used on later evidence must accept the same line."""
    roots = tmp_path / 'roots'
    rollout(roots, [user_message('请修复缓存写入'), assistant_message(CLAIM), tool_output(TOOL)])
    enable_capture(config, roots)
    assert scan(service)['archives'] == 1
    archive_id = archives(service)[0]['id']

    item = case(service, TOOL)
    assert _bind_experience(service, item['id'], [{'role': 'tool', 'content': TOOL}],
                            archive_id=archive_id) is True
    source = bound_source(service, item['id'])
    assert source['source_ref'] == f'archive:{archive_id}#3'
    resolver = ExperienceSourceResolver(archiver=SessionArchiver(config, service.store))
    assert resolver.validate_stored(
        source_kind='tool_result', source_ref=source['source_ref'], task_id=SESSION,
        extraction_version=source['extraction_version']) is True
    assert resolver.validate_stored(
        source_kind='tool_result', source_ref=source['source_ref'], task_id=SESSION,
        extraction_version='experience-v1:' + '0' * 64) is False
    assert resolver.validate_stored(
        source_kind='tool_result', source_ref=source['source_ref'], task_id=OTHER,
        extraction_version=source['extraction_version']) is False


def test_batch_whose_read_window_starts_before_activation_still_binds(service, config, tmp_path):
    """Filtered pre-activation records shift the window, not the line identities."""
    roots = tmp_path / 'roots'
    rollout(roots, [
        event('session_meta', {'id': SESSION, 'cwd': '/home/u/demo'}, OLD),
        user_message('采集开启前的历史内容。', moment=OLD),
        assistant_message('历史回答。', moment=OLD),
        user_message('请修复缓存写入'),
        assistant_message(CLAIM),
        tool_output(TOOL)])
    enable_capture(config, roots)
    assert scan(service)['archives'] == 1
    archive_id = archives(service)[0]['id']
    body = payload_of(service, archive_id)
    assert body['source']['start_line'] == 4 and body['source']['end_line'] == 6
    # Pre-activation bytes were dropped, so the first located line starts later
    # than the file head; the declared range still covers every located line.
    assert body['line_locations'][0]['start_offset'] > 0
    assert body['source']['start_offset'] == body['line_locations'][0]['start_offset']
    assert body['source']['end_offset'] == body['line_locations'][-1]['end_offset']

    item = case(service, TOOL)
    assert _bind_experience(service, item['id'], [{'role': 'tool', 'content': TOOL}],
                            archive_id=archive_id) is True
    assert evidence_rows(service, item['id'])[0]['task_id'] == SESSION


# ------------------------------------------------- real-shaped source bytes

def assert_payload_is_the_original_file_bytes(service, archive_id, path):
    """The archived transcript is the source file's own bytes, line by line.

    This is the check that a compact real Codex file enforces: re-serializing the
    parsed rows (spacing, escaped unicode, CRLF) would change these bytes and the
    resolver's per-location length check would refuse the batch.
    """
    body = payload_of(service, archive_id)
    raw = path.read_bytes()
    assert body['transcript'].encode('utf-8') == raw[
        body['source']['start_offset']:body['source']['end_offset']]
    assert hashlib.sha256(body['transcript'].encode('utf-8')).hexdigest() == body['source_sha256']
    for location in body['line_locations']:
        line = raw[location['start_offset']:location['end_offset']]
        assert line.endswith(b'\n')
        if location['type']:
            assert json.loads(line.decode('utf-8'))['type'] == location['type']
    return body


@pytest.mark.parametrize('shape', ['compact', 'escaped', 'spaced', 'crlf'])
def test_real_shaped_batch_binds_tool_evidence_byte_exactly(service, config, tmp_path, shape):
    """Every real line shape archives byte-for-byte and still binds tool evidence."""
    roots = tmp_path / 'roots'
    path = rollout(roots, [user_message('请修复缓存写入'), assistant_message(CLAIM),
                           tool_output(TOOL)], shape=shape)
    enable_capture(config, roots)
    assert scan(service)['archives'] == 1
    stored = archives(service)
    assert len(stored) == 1
    body = assert_payload_is_the_original_file_bytes(service, stored[0]['id'], path)
    # Visible dialogue keeps only real user/assistant turns; the tool result is
    # evidence in the stored transcript and locations, not spoken dialogue.
    assert [item['role'] for item in body['conversation']] == ['user', 'assistant']
    tool_line = next(location for location in body['line_locations']
                     if location['event_id'] == 'evt-tool')
    assert path.read_bytes()[tool_line['start_offset']:tool_line['end_offset']].decode('utf-8').find(TOOL) > 0

    # The real evidence path accepts the batch and its tool line.
    item = case(service, TOOL)
    assert _bind_experience(service, item['id'], [{'role': 'tool', 'content': TOOL}],
                            archive_id=stored[0]['id']) is True
    result = service.experiences().read(item['id'])
    assert result['status'] == 'active' and result['success_count'] == 1
    source = bound_source(service, item['id'])
    assert source['source_kind'] == 'tool_result'
    assert source['source_ref'] == f'archive:{stored[0]["id"]}#3'
    resolver = ExperienceSourceResolver(archiver=SessionArchiver(config, service.store))
    assert resolver.validate_stored(
        source_kind='tool_result', source_ref=source['source_ref'], task_id=SESSION,
        extraction_version=source['extraction_version']) is True

    # A repeated scan does not duplicate the batch or change the transcript.
    assert scan(service)['archives'] == 0
    assert len(archives(service)) == 1
    again = assert_payload_is_the_original_file_bytes(service, stored[0]['id'], path)
    assert again['transcript'] == body['transcript']
    assert again['source_sha256'] == body['source_sha256']


@pytest.mark.parametrize('shape', ['compact', 'escaped'])
def test_escaped_and_compact_shapes_keep_utf8_dialogue_intact(service, config, tmp_path, shape):
    """Escaped unicode is decoded for reading while the stored bytes stay escaped."""
    roots = tmp_path / 'roots'
    path = rollout(roots, [user_message(USER), assistant_message('已按你的确认完成。'),
                           tool_output(TOOL)], shape=shape)
    enable_capture(config, roots)
    assert scan(service)['archives'] == 1
    archive_id = archives(service)[0]['id']
    body = assert_payload_is_the_original_file_bytes(service, archive_id, path)
    assert USER in [item['content'] for item in body['conversation'] if item['role'] == 'user']
    assert body['transcript'].count(USER) == (0 if shape == 'escaped' else 1)
    item = case(service, USER)
    assert _bind_experience(service, item['id'], [{'role': 'user', 'content': USER}],
                            archive_id=archive_id) is True
    assert evidence_rows(service, item['id'])[0]['verification_level'] == 'user_confirmed'


# ------------------------------------------------------------- tampered metadata

def test_unverifiable_incremental_batch_metadata_is_refused(service, config, tmp_path):
    roots = tmp_path / 'roots'
    rollout(roots, [user_message('请修复缓存写入'), assistant_message(CLAIM), tool_output(TOOL)])
    enable_capture(config, roots)
    assert scan(service)['archives'] == 1
    original = payload_of(service, archives(service)[0]['id'])

    def external(body):
        source = body['source']
        return batch_external_id(source['session_id'], source['start_line'],
                                 source['end_line'], body['source_sha256'])

    def digest_changed(body):
        body['source_sha256'] = 'f' * 64
        return 'codex', None

    def offsets_changed(body):
        body['line_locations'][0]['start_offset'] += 1
        return 'codex', None

    def identity_changed(body):
        source = body['source']
        return 'codex', batch_external_id(source['session_id'], source['start_line'],
                                          source['end_line'] + 1, body['source_sha256'])

    def adapter_changed(body):
        return 'kimi', None

    def session_format_changed(body):
        body['source']['session_id'] = 'not-a-uuid'
        return 'codex', None

    def line_type_changed(body):
        body['transcript'] = body['transcript'].replace(
            'response_item', 'responsX_item', 1)
        body['source_sha256'] = hashlib.sha256(body['transcript'].encode('utf-8')).hexdigest()
        return 'codex', None

    def location_session_changed(body):
        body['line_locations'][0]['session_id'] = OTHER
        return 'codex', None

    def conversation_forged(body):
        body['conversation'] = list(body['conversation']) + [{'role': 'tool', 'content': FORGED}]
        return 'codex', None

    variants = {
        'digest': (digest_changed, TOOL),
        'offsets': (offsets_changed, TOOL),
        'archive_identity': (identity_changed, TOOL),
        'adapter': (adapter_changed, TOOL),
        'session_format': (session_format_changed, TOOL),
        'line_location_type': (line_type_changed, TOOL),
        'line_location_session': (location_session_changed, TOOL),
        'cleaned_conversation': (conversation_forged, FORGED),
    }
    for name, (mutate, quote) in variants.items():
        body = copy.deepcopy(original)
        adapter, target = mutate(body)
        archived = SessionArchiver(config, service.store).archive_session(
            '', adapter, target or external(body), json.dumps(body, ensure_ascii=False))
        assert archived is not None, name
        item = case(service, quote)
        assert _bind_experience(service, item['id'], [{'role': 'tool', 'content': quote}],
                                archive_id=archived.id) is False, name
        assert service.experiences().read(item['id'])['success_count'] == 0, name


def test_forged_session_meta_in_an_incremental_batch_is_refused(service, config, tmp_path):
    """The batch identity comes from the capture, never from a rewritten meta line."""
    roots = tmp_path / 'roots'
    rollout(roots, [event('session_meta', {'id': OTHER, 'cwd': '/home/u/demo'}),
                    user_message('请修复缓存写入'), tool_output(TOOL)])
    enable_capture(config, roots)
    assert scan(service)['archives'] == 1
    archive_id = archives(service)[0]['id']
    body = payload_of(service, archive_id)
    assert body['source']['session_id'] == SESSION

    item = case(service, TOOL)
    assert _bind_experience(service, item['id'], [{'role': 'tool', 'content': TOOL}],
                            archive_id=archive_id) is False
    assert service.experiences().read(item['id'])['success_count'] == 0


# ------------------------------------------------------- Windows archive regression

def test_windows_client_reported_archive_contract_is_unchanged(service, config):
    rows = [
        {'type': 'session_meta', 'payload': {'id': 'session-1', 'cwd': r'C:\work\demo'}},
        {'type': 'response_item', 'payload': {
            'type': 'message', 'role': 'user',
            'content': [{'type': 'input_text', 'text': '客户确认保留完整会话归档。'}]}},
        {'type': 'response_item', 'payload': {
            'type': 'function_call_output', 'call_id': 'call-1', 'output': '1 passed in 0.10s'}},
    ]
    transcript = ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows)
    archiver = SessionArchiver(config, service.store)
    archived = archiver.archive_session('', 'codex', 'windows-native', json.dumps({
        'source': 'client_reported', 'device_id': 'synthetic', 'session_id': 'session-1',
        'transcript': transcript}))
    resolver = ExperienceSourceResolver(archiver=archiver)
    found = resolver.resolve(source_kind='tool_result',
                             source_ref=f'archive:{archived.id}#3',
                             task_id='session-1', quote='1 passed in 0.10s')
    assert found.source_ref == f'archive:{archived.id}#3'

    # The client-reported path still requires the leading session_meta.
    broken = archiver.archive_session('', 'codex', 'windows-no-meta', json.dumps({
        'source': 'client_reported', 'device_id': 'synthetic', 'session_id': 'session-1',
        'transcript': json.dumps(rows[1], ensure_ascii=False) + '\n'}))
    with pytest.raises(ValueError, match='invalid archive transcript'):
        resolver.resolve(source_kind='user_confirmation',
                         source_ref=f'archive:{broken.id}#1', task_id='session-1',
                         quote='客户确认保留完整会话归档。')

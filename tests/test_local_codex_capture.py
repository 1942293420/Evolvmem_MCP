"""Local Linux Codex JSONL capture: incremental, bounded, restart-safe.

All data here is synthetic and lives under pytest's temporary directory; the real
``~/.codex`` directory, the real database and any business data are never read.
"""
import contextlib
import hashlib
import json
import os
import sqlite3
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer

import pytest

from evolvmem import local_codex_capture as capture
from evolvmem.context_models import ContextMode
from evolvmem.context_service import ContextService
from evolvmem.local_codex_capture import LocalCodexCapture

SESSION = '01a0fb28-893d-7250-9445-1a2c2fe6a0ab'
OTHER = '01a0fb28-893d-7250-9445-1a2c2fe6a0ac'
OLD = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
NEW = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
TEXT_HEAD = '导出任务必须保留完整会话归档。'
TEXT_AI = '已实现增量归档与去重。'


def stamp(moment):
    return moment.strftime('%Y-%m-%dT%H:%M:%S.000Z')


def event(kind, payload, moment=NEW, **extra):
    """One record in the real observed shape: timestamp on the record root."""
    row = {'type': kind, 'timestamp': stamp(moment), 'payload': payload}
    row.update(extra)
    return row


def message(index, moment=NEW):
    user = index % 2 == 0
    return event('response_item', {
        'type': 'message', 'role': 'user' if user else 'assistant', 'id': f'evt-{index}',
        'content': [{'type': 'input_text' if user else 'output_text',
                     'text': f'第{index}条消息内容。'}]}, moment)


def rows(*, session=SESSION, moment=NEW, cwd='/home/u/demo', tool=False):
    """One structural Codex rollout in the real observed shape."""
    events = [
        event('session_meta', {'id': session, 'timestamp': stamp(moment), 'cwd': cwd}, moment),
        event('response_item', {'type': 'message', 'role': 'user', 'id': 'evt-user-1',
              'content': [{'type': 'input_text', 'text': TEXT_HEAD}]}, moment),
        event('response_item', {'type': 'message', 'role': 'assistant', 'id': 'evt-ai-1',
              'content': [{'type': 'output_text', 'text': TEXT_AI}]}, moment),
    ]
    if tool:
        events.insert(2, event('response_item', {'type': 'function_call', 'id': 'evt-call-1',
                      'name': 'shell', 'arguments': '{"command":"pytest -q"}'}, moment))
        events.insert(3, event('response_item', {'type': 'function_call_output', 'id': 'evt-out-1',
                      'call_id': 'evt-call-1', 'output': '12 passed in 3.40s'}, moment))
        events.insert(4, event('response_item', {'type': 'reasoning', 'id': 'evt-reason-1',
                      'summary': [{'type': 'summary_text', 'text': '内部推理：先读注册表。'}]}, moment))
        events.append(event('response_item', {'type': 'message', 'role': 'user', 'id': 'evt-inject-1',
                      'content': [{'type': 'input_text',
                      'text': '<environment_context><cwd>/home/u/demo</cwd></environment_context>真实需求：按批归档。'}]}, moment))
    return events


def line_bytes(item, *, ascii_only=False, compact=True, newline='\n'):
    """One rollout line exactly as Codex writes it: compact JSON, LF by default."""
    separators = (',', ':') if compact else None
    return (json.dumps(item, ensure_ascii=ascii_only, separators=separators) + newline).encode('utf-8')


def jsonl(events, **options):
    return b''.join(line_bytes(item, **options) for item in events).decode('utf-8')


@pytest.fixture
def service(test_config):
    instance = ContextService(test_config)
    instance.initialize(mode=ContextMode.SHADOW, adapter='local-capture-test')
    yield instance
    instance.close()


@pytest.fixture
def roots(tmp_path):
    directory = tmp_path / 'codex-roots'
    directory.mkdir()
    return directory


def write_config(service, roots, *, enabled=True, since=NEW, extra=None):
    body = {'enabled': enabled, 'sessions_roots': [str(roots)], 'since': stamp(since)}
    if extra:
        body.update(extra)
    (service.config.data_dir / 'local_codex_capture.json').write_text(
        json.dumps(body, ensure_ascii=False), encoding='utf-8')


def write_session(roots, events, name='rollout-2026-06-01T00-00-00-%s.jsonl' % SESSION, **options):
    path = roots / '2026' / '06' / '01'
    path.mkdir(parents=True, exist_ok=True)
    target = path / name
    target.write_bytes(b''.join(line_bytes(item, **options) for item in events))
    return target


def scan(service):
    return LocalCodexCapture(service.config, service.store).scan()


def status(service):
    return capture.status(service.config, service.store)


def archives(service):
    return service.store._connection().execute(
        'SELECT * FROM session_archives ORDER BY id').fetchall()


def payloads(service):
    return [json.loads(capture.read_payload(service.config, service.store, row['id']))
            for row in archives(service)]


def set_auto_new(service, enabled=True):
    from evolvmem.organization_arrival import settings, update_settings
    if not settings(service)['auto_new']:
        update_settings(service, {'auto_new': enabled})


# ------------------------------------------------------------------ timestamps

def test_the_root_timestamp_wins_over_the_payload_timestamp():
    """The parent-reported shape: ``_event_time`` must read the record root."""
    row = {'timestamp': '2026-10-07T18:00:00Z', 'type': 'response_item',
           'payload': {'type': 'message', 'role': 'user',
                       'content': [{'type': 'input_text', 'text': 'synthetic'}]}}
    assert capture._event_time(row) == pytest.approx(
        datetime(2026, 10, 7, 18, 0, 0, tzinfo=timezone.utc).timestamp())
    # session_meta also carries the whole session's start in its payload, but the
    # record's own root timestamp is authoritative.
    meta = {'timestamp': stamp(NEW), 'type': 'session_meta',
            'payload': {'id': SESSION, 'timestamp': stamp(OLD)}}
    assert capture._event_time(meta) == pytest.approx(NEW.timestamp())
    # Only a payload timestamp is available: it is still a usable lower bound.
    assert capture._event_time({'type': 'session_meta', 'payload': {'timestamp': stamp(OLD)}}) \
        == pytest.approx(OLD.timestamp())
    assert capture._event_time({'type': 'response_item', 'payload': {'type': 'message'}}) is None


# --------------------------------------------------- shared parser boundary

def test_the_shared_row_parser_matches_parse_transcript_exactly():
    """The capture reuses the whole-transcript rules, byte for byte.

    ``parse_transcript`` now delegates to ``dialogue_messages``; this pins the
    noise boundary (mirror collapse, tool tagging, analysis channel, empty
    content) so the local capture can never widen or narrow it by accident.
    """
    from evolvmem.codex_transcript import dialogue_messages, parse_transcript
    events = [
        event('session_meta', {'id': SESSION, 'cwd': '/home/u/demo'}),
        event('response_item', {'type': 'message', 'role': 'user', 'id': 'm1',
              'content': [{'type': 'input_text', 'text': '同一条消息。'}]}),
        # The native stream mirrors the same message in event_msg.
        event('event_msg', {'type': 'user_message', 'id': 'm1', 'message': '同一条消息。'}),
        event('response_item', {'type': 'function_call_output', 'call_id': 'c1', 'output': 'ok'}),
        event('response_item', {'type': 'message', 'role': 'assistant', 'id': 'm2',
              'channel': 'analysis', 'content': [{'type': 'output_text', 'text': '内部推理。'}]}),
        event('response_item', {'type': 'message', 'role': 'assistant', 'id': 'm3',
              'content': [{'type': 'output_text', 'text': '可见回答。'}]}),
        event('response_item', {'type': 'message', 'role': 'user', 'id': 'm4',
              'content': [{'type': 'input_text', 'text': '   '}]}),
    ]
    raw = jsonl(events).encode('utf-8')
    rows_, messages = parse_transcript(raw, SESSION)
    assert messages == dialogue_messages(rows_)
    assert messages == [
        {'role': 'user', 'content': '同一条消息。'},
        {'role': 'tool', 'content': 'ok'},
        {'role': 'assistant', 'content': '内部推理。', 'channel': 'analysis'},
        {'role': 'assistant', 'content': '可见回答。'},
    ]


# --------------------------------------------------------------- configuration

def test_missing_configuration_never_collects(service, roots):
    write_session(roots, rows())
    result = scan(service)
    assert result['skipped'] == 'not_configured'
    assert archives(service) == []


def test_configuration_without_roots_is_reported_as_an_error(service):
    (service.config.data_dir / 'local_codex_capture.json').write_text(
        json.dumps({'enabled': True, 'sessions_roots': [], 'since': stamp(NEW)}), encoding='utf-8')
    state = status(service)
    assert state['enabled'] is False and state['error'] == 'config_error'
    assert state['config']['problems']


def test_relative_root_is_rejected_instead_of_guessed(service):
    (service.config.data_dir / 'local_codex_capture.json').write_text(
        json.dumps({'enabled': True, 'sessions_roots': ['relative/codex'], 'since': stamp(NEW)}),
        encoding='utf-8')
    assert status(service)['error'] == 'config_error'


def test_a_config_without_since_is_refused(service, roots):
    """Without the activation line the gate would import the whole backlog."""
    (service.config.data_dir / 'local_codex_capture.json').write_text(
        json.dumps({'enabled': True, 'sessions_roots': [str(roots)]}), encoding='utf-8')
    state = status(service)
    assert state['enabled'] is False and state['error'] == 'config_error'
    assert 'since_missing' in state['config']['problems']
    assert scan(service)['skipped'] == 'not_configured'


def test_documented_scan_budgets_reach_the_running_scan(service, roots):
    """A documented JSON key must never be silently ignored."""
    write_session(roots, rows(), name='rollout-2026-06-01T00-00-00-%s.jsonl' % SESSION)
    write_session(roots, rows(session=OTHER), name='rollout-2026-06-01T00-01-00-%s.jsonl' % OTHER)
    write_config(service, roots, extra={'max_files_per_scan': 1, 'max_lines_per_scan': 3,
                                       'max_seconds_per_scan': 60, 'batch_lines': 3})
    settings = capture.load_config(service.config.data_dir)
    assert settings.max_files_per_scan == 1
    assert settings.max_lines_per_scan == 3
    assert settings.max_seconds_per_scan == 60
    first = scan(service)
    assert first['archives'] == 1 and first['files_read'] == 1
    second = scan(service)
    assert second['archives'] == 1 and second['files_read'] == 1
    assert len(archives(service)) == 2


# ------------------------------------------------------------------ since gate

def test_events_before_since_are_never_imported(service, roots):
    old = rows(moment=OLD)
    old.insert(1, event('response_item', {'type': 'message', 'role': 'user', 'id': 'evt-old',
               'content': [{'type': 'input_text', 'text': '这是采集开启前的历史内容。'}]}, OLD))
    path = write_session(roots, old)
    write_config(service, roots, since=NEW)
    scan(service)
    assert archives(service) == []
    state = status(service)['sessions'][0]
    assert state['filtered_events'] > 0
    assert state['scanned_offset_bytes'] == path.stat().st_size
    assert scan(service)['scanned_bytes'] == 0


def test_a_record_without_a_timestamp_and_no_lower_bound_is_not_imported(service, roots):
    """Unknown time plus no observed floor must never be imported."""
    path = write_session(roots, [
        {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'id': 'evt-1',
         'content': [{'type': 'input_text', 'text': '没有时间的记录。'}]}}])
    write_config(service, roots, since=NEW)
    result = scan(service)
    assert result['archives'] == 0 and archives(service) == []
    assert result['filtered_events'] >= 1
    # Consumed, not stuck: the same bytes are never rescanned.
    assert result['bytes_read'] == path.stat().st_size
    assert scan(service)['bytes_read'] == 0


def test_cross_scan_boundary_imports_only_records_after_activation(service, roots):
    """An old session_meta plus new root timestamps, spanning two scans."""
    path = write_session(roots, [
        event('session_meta', {'id': SESSION, 'timestamp': stamp(OLD), 'cwd': '/home/u/demo'}, OLD),
        event('response_item', {'type': 'message', 'role': 'user', 'id': 'evt-old',
              'content': [{'type': 'input_text', 'text': '这是采集开启前的历史内容。'}]}, OLD)])
    write_config(service, roots, since=NEW)
    first = scan(service)
    assert first['archives'] == 0 and archives(service) == []
    assert first['filtered_events'] >= 2
    # The same old session keeps running after activation.
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(jsonl(rows()[-2:]))
    second = scan(service)
    assert second['archives'] == 1
    stored = payloads(service)
    assert len(stored) == 1
    assert '这是采集开启前的历史内容。' not in stored[0]['transcript']
    assert TEXT_HEAD in stored[0]['transcript'] and TEXT_AI in stored[0]['transcript']
    assert stored[0]['source']['start_line'] == 3 and stored[0]['source']['end_line'] == 4
    assert stored[0]['source']['session_id'] == SESSION
    _assert_source_range_matches_locations(stored[0])
    _assert_transcript_is_the_original_file_bytes(stored[0], path)


def test_new_content_after_since_is_archived_once(service, roots):
    path = write_session(roots, rows())
    write_config(service, roots, since=NEW)
    result = scan(service)
    assert result['archives'] == 1 and result['bytes_read'] > 0
    stored = archives(service)
    assert len(stored) == 1 and stored[0]['project'] == '' and stored[0]['adapter'] == 'codex'
    body = json.loads(service.store._connection().execute(
        'SELECT messages FROM conversation_history WHERE archive_id=?', (stored[0]['id'],)).fetchone()['messages'])
    assert [item['role'] for item in body] == ['user', 'assistant']
    assert TEXT_HEAD in body[0]['content']
    payload = json.loads(capture.read_payload(service.config, service.store, stored[0]['id']))
    assert payload['source']['file'].endswith(path.name)
    assert payload['source']['session_id'] == SESSION
    assert payload['source']['start_line'] == 1 and payload['source']['end_line'] == 3
    assert payload['source']['start_offset'] == 0
    assert payload['source']['end_offset'] == path.stat().st_size
    assert [location['line'] for location in payload['line_locations']] == [1, 2, 3]
    assert payload['line_locations'][1]['event_id'] == 'evt-user-1'
    assert payload['line_locations'][0]['event_id'] == SESSION
    assert payload['line_locations'][1]['event_time'] == pytest.approx(NEW.timestamp())
    assert payload['transcript'].startswith('{"type":"session_meta"')
    _assert_source_range_matches_locations(payload)
    _assert_transcript_is_the_original_file_bytes(payload, path)


def test_repeated_scan_and_restart_never_duplicate(service, roots):
    write_session(roots, rows())
    write_config(service, roots, since=NEW)
    assert scan(service)['archives'] == 1
    first = [row['id'] for row in archives(service)]
    assert LocalCodexCapture(service.config, service.store).scan()['archives'] == 0
    assert scan(service)['archives'] == 0
    assert [row['id'] for row in archives(service)] == first


def test_half_written_line_waits_for_its_newline(service, roots):
    path = write_session(roots, rows())
    write_config(service, roots, since=NEW)
    scan(service)
    assert status(service)['sessions'][0]['scanned_offset_bytes'] == path.stat().st_size
    partial = line_bytes(event('response_item', {'type': 'message', 'role': 'user', 'id': 'evt-user-2',
              'content': [{'type': 'input_text', 'text': '半行先不要归档。'}]})).decode('utf-8').rstrip('\n')
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(partial)
    assert scan(service)['archives'] == 0
    assert status(service)['sessions'][0]['pending_bytes'] == len(partial.encode())
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write('\n' + jsonl(rows()[-2:]))
    result = scan(service)
    assert result['archives'] == 1
    stored = archives(service)
    assert len(stored) == 2
    payload = json.loads(capture.read_payload(service.config, service.store, stored[-1]['id']))
    assert payload['source']['start_line'] == 4 and payload['source']['end_line'] == 6
    assert '半行先不要归档。' in payload['transcript']


def _assert_transcript_is_the_original_file_bytes(payload, path):
    """The archived transcript is the source file's own bytes, never a rewrite.

    This is what a compact-JSON Codex file proves: re-serializing the parsed rows
    (spaces, key order, escaped unicode, CRLF) would change the bytes and break
    the evidence path's per-line length check.
    """
    raw = path.read_bytes()
    declared = raw[payload['source']['start_offset']:payload['source']['end_offset']]
    assert payload['transcript'].encode('utf-8') == declared
    for location in payload['line_locations']:
        line = raw[location['start_offset']:location['end_offset']]
        assert line.endswith(b'\n')
        assert len(line) == location['end_offset'] - location['start_offset']
    assert hashlib.sha256(payload['transcript'].encode('utf-8')).hexdigest() \
        == payload['source_sha256']


def _assert_source_range_matches_locations(payload):
    """The payload's source range describes exactly the archived records."""
    locations = payload['line_locations']
    assert payload['source']['start_line'] == locations[0]['line']
    assert payload['source']['end_line'] == locations[-1]['line']
    assert payload['source']['start_offset'] == locations[0]['start_offset']
    assert payload['source']['end_offset'] == locations[-1]['end_offset']
    assert payload['source']['start_offset'] < payload['source']['end_offset']
    # The digest covers exactly the stored transcript, so evidence stays verifiable.
    assert hashlib.sha256(payload['transcript'].encode('utf-8')).hexdigest() \
        == payload['source_sha256']


def test_a_batch_that_drops_pre_activation_lines_reports_the_kept_range(service, roots):
    """Cross-since batch: offsets, lines and locations must all agree."""
    events = [
        event('session_meta', {'id': SESSION, 'timestamp': stamp(OLD), 'cwd': '/home/u/demo'}, OLD),
        event('response_item', {'type': 'message', 'role': 'user', 'id': 'evt-old-1',
              'content': [{'type': 'input_text', 'text': '开启前的第一条历史。'}]}, OLD),
        event('response_item', {'type': 'message', 'role': 'assistant', 'id': 'evt-old-2',
              'content': [{'type': 'output_text', 'text': '开启前的第二条历史。'}]}, OLD),
        event('response_item', {'type': 'message', 'role': 'user', 'id': 'evt-new-1',
              'content': [{'type': 'input_text', 'text': TEXT_HEAD}]}, NEW),
        event('response_item', {'type': 'message', 'role': 'assistant', 'id': 'evt-new-2',
              'content': [{'type': 'output_text', 'text': TEXT_AI}]}, NEW),
    ]
    path = write_session(roots, events)
    write_config(service, roots, since=NEW)
    result = scan(service)
    assert result['archives'] == 1
    assert result['filtered_events'] == 3          # session_meta plus two old lines
    payload = payloads(service)[0]
    _assert_source_range_matches_locations(payload)
    _assert_transcript_is_the_original_file_bytes(payload, path)
    assert payload['source']['start_line'] == 4
    # The scanned round started at byte 0; the archived range starts later, which
    # is exactly the mismatch this pins down.
    assert payload['source']['start_offset'] > 0
    assert payload['source']['start_offset'] == path.read_bytes().index(line_bytes(events[3]))
    assert all(location['line'] >= 4 for location in payload['line_locations'])
    assert '开启前的第一条历史。' not in payload['transcript']


# ---------------------------------------------- real-shaped source bytes

REAL_SHAPES = {
    # Real Codex writes compact JSON with raw UTF-8; the other three shapes are
    # variants a real file can legitimately contain (escaping, spacing, CRLF).
    # ``required`` must be present, ``forbidden`` must not.
    'compact_chinese': ({}, b'"type":"session_meta"', b'"type": "'),
    'escaped_unicode': ({'ascii_only': True}, b'\\u4f1a', b'"type": "'),
    'spaced_separators': ({'compact': False}, b'"type": "', b'"type":"session_meta"'),
    'crlf': ({'newline': '\r\n'}, b'"type":"session_meta"', b'"type": "'),
}


@pytest.mark.parametrize('shape', sorted(REAL_SHAPES))
def test_real_shaped_bytes_survive_into_the_archive(service, roots, shape):
    """Compact/escaped/spaced/CRLF lines archive byte-for-byte and stay idempotent."""
    options, required, forbidden = REAL_SHAPES[shape]
    path = write_session(roots, rows(tool=True),
                         name='rollout-2026-06-01T00-00-00-%s-%s.jsonl' % (SESSION, shape), **options)
    raw = path.read_bytes()
    assert required in raw, shape
    assert forbidden not in raw, shape
    if shape == 'crlf':
        assert b'\r\n' in raw
    write_config(service, roots, since=NEW)
    assert scan(service)['archives'] == 1
    payload = payloads(service)[0]
    _assert_source_range_matches_locations(payload)
    _assert_transcript_is_the_original_file_bytes(payload, path)
    # Visible dialogue and the tool evidence line keep their real positions.
    assert [item['role'] for item in payload['conversation']] == ['user', 'assistant', 'user']
    tool = next(location for location in payload['line_locations'] if location['event_id'] == 'evt-out-1')
    assert tool['type'] == 'response_item'
    assert raw[tool['start_offset']:tool['end_offset']].decode('utf-8').find('12 passed in 3.40s') > 0
    # A repeated scan neither duplicates the archive nor rewrites the transcript.
    assert scan(service)['archives'] == 0
    assert len(archives(service)) == 1
    assert payloads(service)[0]['transcript'] == payload['transcript']


# ------------------------------------------------------------- content hygiene

def test_tool_and_injected_records_carry_no_business_material(service, roots):
    write_session(roots, rows(tool=True))
    write_config(service, roots, since=NEW)
    assert scan(service)['archives'] == 1
    stored = archives(service)[0]
    payload = json.loads(capture.read_payload(service.config, service.store, stored['id']))
    assert [item['role'] for item in payload['conversation']] == ['user', 'assistant', 'user']
    assert all('pytest -q' not in item['content'] for item in payload['conversation'])
    assert all('内部推理' not in item['content'] for item in payload['conversation'])
    assert all('environment_context' not in item['content'] for item in payload['conversation'])
    assert '真实需求：按批归档。' in payload['conversation'][-1]['content']
    assert 'evt-out-1' in payload['transcript']


def test_pure_tool_batch_creates_no_empty_archive(service, roots):
    events = [
        event('response_item', {'type': 'function_call_output', 'call_id': 'c1', 'id': 'evt-tool-1',
              'output': 'ok'}),
        event('response_item', {'type': 'function_call', 'id': 'evt-call-1', 'name': 'shell',
              'arguments': '{"command":"pytest -q"}'}),
    ]
    path = write_session(roots, events)
    write_config(service, roots, since=NEW)
    result = scan(service)
    assert result['archives'] == 0 and archives(service) == []
    assert result['bytes_read'] == path.stat().st_size
    assert status(service)['sessions'][0]['scanned_offset_bytes'] == path.stat().st_size


# ------------------------------------------------------------------- budgets

def test_a_single_line_larger_than_the_batch_budget_is_consumed_whole(service, roots):
    """The real maximum line (~2.45MB) must cross the default 2MiB budget."""
    big = '填充内容' * 204800  # ~2.45MB of UTF-8 text in one record
    path = write_session(roots, [rows()[0], event('response_item', {
        'type': 'message', 'role': 'user', 'id': 'evt-big',
        'content': [{'type': 'input_text', 'text': big}]})])
    assert path.stat().st_size > capture.DEFAULT_BATCH_BYTES
    write_config(service, roots, since=NEW)
    result = scan(service)
    assert result['archives'] == 1
    assert result['bytes_read'] == path.stat().st_size
    assert result['bytes_read'] > capture.DEFAULT_BATCH_BYTES
    # Nothing is left half-read, and the next round is idle instead of looping.
    assert status(service)['sessions'][0]['pending_bytes'] == 0
    assert scan(service)['bytes_read'] == 0


def test_a_line_over_the_cap_blocks_explicitly_and_resumes_when_it_is_raised(service, roots):
    path = write_session(roots, [rows()[0], event('response_item', {
        'type': 'message', 'role': 'user', 'id': 'evt-huge',
        'content': [{'type': 'input_text', 'text': '超长填充' * 40000}]})])
    write_config(service, roots, extra={'max_line_bytes': 4096})
    first = scan(service)
    assert first['archives'] == 0
    assert first['errors'] == ['line_too_long']
    state = status(service)
    assert state['needs_review'] == 1
    assert state['sessions'][0]['file_state'] == 'blocked'
    assert state['sessions'][0]['blocked_reason'] == 'line_too_long'
    # The cursor stays before the offending line instead of pretending progress.
    assert state['sessions'][0]['scanned_offset_bytes'] == 0
    # A blocked file is not re-opened every poll: no infinite half-read loop.
    second = scan(service)
    assert second['bytes_read'] == 0 and second['files_blocked'] == 1
    # Raising the cap explicitly resumes exactly where it stopped.
    write_config(service, roots, extra={'max_line_bytes': 8 * 1024 * 1024})
    third = scan(service)
    assert third['archives'] == 1
    assert third['bytes_read'] == path.stat().st_size
    assert status(service)['needs_review'] == 0


def test_large_file_is_read_in_bounded_rounds(service, roots):
    events = [rows()[0]]
    for index in range(4000):
        events.append(event('response_item', {'type': 'message', 'role': 'user', 'id': f'evt-{index}',
                      'content': [{'type': 'input_text', 'text': '填充内容 ' * 20}]}))
    path = write_session(roots, events)
    write_config(service, roots, extra={'batch_bytes': 64 * 1024, 'batch_lines': 200,
                                       'max_files_per_scan': 1, 'max_lines_per_scan': 200,
                                       'max_seconds_per_scan': 60})
    overshoot = 4096
    first = scan(service)
    assert first['bytes_read'] <= 64 * 1024 + overshoot
    assert first['bytes_read'] < path.stat().st_size
    rounds, total = 1, first['bytes_read']
    while True:
        step = scan(service)
        if not step['bytes_read']:
            break
        assert step['bytes_read'] <= 64 * 1024 + overshoot
        rounds += 1
        total += step['bytes_read']
        assert rounds < 4000
    assert rounds > 1
    assert total == path.stat().st_size
    assert path.stat().st_size > 1024 * 1024
    assert len(archives(service)) == rounds


# ------------------------------------------------------------- file discovery

def test_append_only_growth_keeps_the_cursor_and_imports_only_the_tail(service, roots):
    path = write_session(roots, rows())
    write_config(service, roots, since=NEW)
    assert scan(service)['archives'] == 1
    before = [row['id'] for row in archives(service)]
    appended = rows()
    appended.append(event('response_item', {'type': 'message', 'role': 'user', 'id': 'evt-new',
                    'content': [{'type': 'input_text', 'text': '追加的新内容。'}]}))
    path.write_text(jsonl(appended), encoding='utf-8')
    result = scan(service)
    assert result['archives'] == 1 and result['files_blocked'] == 0
    stored = archives(service)
    assert [row['id'] for row in stored][:len(before)] == before
    added = [json.loads(capture.read_payload(service.config, service.store, row['id']))
             for row in stored[len(before):]]
    assert any('追加的新内容。' in item['transcript'] for item in added)
    assert all(item['source']['start_line'] == 4 for item in added)
    assert scan(service)['scanned_bytes'] == 0


def test_more_than_512_files_are_enumerated_and_old_ones_stay_unread(service, roots):
    """670 pre-activation files plus one new file: nothing may hide the new one."""
    for index in range(670):
        day = '01' if index < 500 else '02'
        directory = roots / '2026' / '01' / day
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / ('rollout-2026-01-%sT00-%02d-%02d-%s.jsonl'
                              % (day, index // 60, index % 60, OTHER))
        target.write_text(jsonl([event('session_meta', {'id': OTHER, 'cwd': '/home/u/demo'}, OLD),
                                 event('response_item', {'type': 'message', 'role': 'user',
                                       'id': f'old-{index}',
                                       'content': [{'type': 'input_text', 'text': '旧会话内容。'}]}, OLD)]),
                          encoding='utf-8')
        os.utime(target, (OLD.timestamp(), OLD.timestamp()))
    new_path = write_session(roots, rows(), name='rollout-2026-06-01T00-00-00-%s.jsonl' % SESSION)
    write_config(service, roots, since=NEW)
    result = scan(service)
    assert result['files_seen'] == 671            # complete enumeration, no 512 cap
    assert result['files_read'] == 1
    assert result['archives'] == 1
    assert result['bytes_read'] == new_path.stat().st_size
    stored = payloads(service)
    assert len(stored) == 1 and stored[0]['source']['file'].endswith(new_path.name)
    assert service.store._connection().execute(
        'SELECT COUNT(*) AS c FROM local_capture_files').fetchone()['c'] == 1
    # The direct discovery API is likewise uncapped and time-filtered.
    everything, enumerated = capture.discover_files([str(roots)], since=0.0, known=set())
    assert enumerated == 671 and len(everything) == 671
    fresh, _ = capture.discover_files([str(roots)], since=NEW.timestamp(), known=set())
    assert [path for path, _ in fresh] == [str(new_path)]


def test_an_old_session_appended_after_activation_is_discovered(service, roots):
    path = write_session(roots, [
        event('session_meta', {'id': SESSION, 'timestamp': stamp(OLD), 'cwd': '/home/u/demo'}, OLD),
        event('response_item', {'type': 'message', 'role': 'user', 'id': 'evt-old',
              'content': [{'type': 'input_text', 'text': '这是采集开启前的历史内容。'}]}, OLD)])
    os.utime(path, (OLD.timestamp(), OLD.timestamp()))
    write_config(service, roots, since=NEW)
    first = scan(service)
    assert first['archives'] == 0 and first['files_read'] == 0
    assert status(service)['files'] == 0
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(jsonl(rows()[-2:]))
    os.utime(path, (time.time(), time.time()))
    second = scan(service)
    assert second['archives'] == 1
    stored = payloads(service)
    assert '这是采集开启前的历史内容。' not in stored[0]['transcript']
    assert TEXT_HEAD in stored[0]['transcript']


# -------------------------------------------------------------- file rewriting

def test_a_same_length_in_place_rewrite_is_detected_and_stops(service, roots):
    path = write_session(roots, rows())
    write_config(service, roots, since=NEW)
    assert scan(service)['archives'] == 1
    size = path.stat().st_size
    rewritten = path.read_text(encoding='utf-8').replace(TEXT_AI, '已实现增量归档与折返。')
    assert len(rewritten.encode()) == size
    path.write_text(rewritten, encoding='utf-8')
    state = status(service)
    assert state['needs_review'] == 1
    assert state['sessions'][0]['file_state'] == 'blocked'
    assert state['sessions'][0]['blocked_reason'] == 'content_changed_needs_review'
    result = scan(service)
    assert result['archives'] == 0 and result['files_blocked'] == 1
    # No silent second import and no fake tail-following.
    assert len(archives(service)) == 1


def test_a_truncated_file_is_frozen_with_its_archived_batches_intact(service, roots):
    path = write_session(roots, rows())
    write_config(service, roots, since=NEW)
    assert scan(service)['archives'] == 1
    before = [row['id'] for row in archives(service)]
    cursor = status(service)['sessions'][0]['scanned_offset_bytes']
    path.write_text(jsonl(rows()[:2]), encoding='utf-8')
    state = status(service)
    assert state['sessions'][0]['blocked_reason'] == 'file_truncated_needs_review'
    assert scan(service)['archives'] == 0
    frozen = status(service)['sessions'][0]
    # The old offset is never reused silently on the new, shorter file.
    assert frozen['scanned_offset_bytes'] == cursor
    assert [row['id'] for row in archives(service)] == before


def test_the_optin_restart_reprocesses_changed_ranges_without_duplicates(service, roots):
    events = [rows()[0]] + [message(index) for index in range(5)]
    path = write_session(roots, events)
    write_config(service, roots, extra={'batch_lines': 3})
    assert scan(service)['archives'] == 1          # lines 1-3
    assert scan(service)['archives'] == 1          # lines 4-6
    stable = [row['external_session_id'] for row in archives(service)]
    path.write_text(path.read_text(encoding='utf-8').replace('第4条消息内容。', '第4条消息内容？'),
                    encoding='utf-8')
    write_config(service, roots, extra={'batch_lines': 3, 'restart_changed_files': True})
    assert scan(service)['archives'] == 0          # identical first range is skipped
    assert scan(service)['archives'] == 1          # only the changed range re-archives
    stored = [row['external_session_id'] for row in archives(service)]
    assert stored[:2] == stable
    assert len(stored) == len(set(stored)) == 3
    assert status(service)['sessions'][0]['rewrite_count'] == 1


# ------------------------------------------------------------------ failure path

def test_archive_failure_keeps_the_cursor_for_a_retry(service, roots, monkeypatch):
    path = write_session(roots, rows())
    write_config(service, roots, since=NEW)
    original = capture.SessionArchiver.archive_session

    def broken(*args, **kwargs):
        return None

    monkeypatch.setattr(capture.SessionArchiver, 'archive_session', broken)
    assert scan(service)['archives'] == 0
    state = status(service)['sessions'][0]
    assert state['scanned_offset_bytes'] == 0
    assert state['last_error'] == 'archive_failed:RuntimeError'
    assert state['failed_batches'] == 1
    assert state['archived_batches'] == 0
    monkeypatch.setattr(capture.SessionArchiver, 'archive_session', original)
    assert scan(service)['archives'] == 1
    assert status(service)['sessions'][0]['scanned_offset_bytes'] == path.stat().st_size


def test_one_broken_file_does_not_stop_the_round(service, roots, monkeypatch):
    write_session(roots, rows(), name='rollout-2026-06-01T00-00-00-%s.jsonl' % SESSION)
    write_session(roots, rows(session=OTHER), name='rollout-2026-06-01T00-01-00-%s.jsonl' % OTHER)
    write_config(service, roots, since=NEW)
    real = capture._scan_file
    calls = {'count': 0}

    def flaky(capture_instance, config, row, round_, deadline):
        calls['count'] += 1
        if calls['count'] == 1:
            raise RuntimeError('synthetic failure')
        return real(capture_instance, config, row, round_, deadline)

    monkeypatch.setattr(capture, '_scan_file', flaky)
    result = scan(service)
    assert result['errors'] == ['RuntimeError']
    assert result['archives'] == 1
    assert len(archives(service)) == 1


# ------------------------------------------------- system sub-session filter

GUARDIAN = {'subagent': {'other': 'guardian'}}


def meta(payload, moment=NEW):
    return event('session_meta', {'timestamp': stamp(moment), 'cwd': '/home/u/demo', **payload}, moment)


def conversation(meta_row):
    """A session body whose messages would be collected if the gate let them in."""
    return [meta_row,
            event('response_item', {'type': 'message', 'role': 'user', 'id': 'evt-user-1',
                  'content': [{'type': 'input_text', 'text': TEXT_HEAD}]}),
            event('response_item', {'type': 'message', 'role': 'assistant', 'id': 'evt-ai-1',
                  'content': [{'type': 'output_text', 'text': TEXT_AI}]})]


def track_untouched_cursor(service, path, session_id=SESSION):
    """A cursor row as a pre-filter install would have written it."""
    capture.ensure_schema(service.store)
    stat = path.stat()
    with service.store.transaction():
        service.store._connection().execute(
            'INSERT INTO local_capture_files (session_id,path,device,inode,file_size,file_mtime,'
            'first_seen_at,updated_at) VALUES (?,?,?,?,?,?,?,?)',
            (session_id, str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime,
             capture._now(), capture._now()))


def cursor_row(service, path):
    stat = path.stat()
    return service.store._connection().execute(
        'SELECT * FROM local_capture_files WHERE device=? AND inode=?',
        (stat.st_dev, stat.st_ino)).fetchone()


def test_a_guardian_subagent_session_is_never_archived(service, roots):
    """A system guardian sub-session is not new user chat and stays out."""
    path = write_session(roots, conversation(meta({'id': SESSION, 'source': GUARDIAN,
                                                   'parent_thread_id': OTHER})))
    write_config(service, roots, since=NEW)
    result = scan(service)
    assert result['archives'] == 0 and archives(service) == []
    assert result['ignored_subagent_sessions'] == 1
    assert result['files_read'] == 0 and result['bytes_read'] == 0
    assert cursor_row(service, path) is None, 'an ignored session needs no cursor'
    # An untracked sub-session leaves no state at all, so the read-only view has
    # no backlog, no failure and nothing to exclude; only a *known* sub-session
    # (a cursor from before this filter) is counted and parked.
    state = status(service)
    assert state['ignored_subagent_sessions'] == 0 and state['pending_bytes'] == 0
    assert state['needs_review'] == 0 and state['failed_batches'] == 0 and state['last_error'] == ''
    # A repeated scan ignores it again and never creates a cursor.
    assert scan(service)['ignored_subagent_sessions'] == 1
    assert cursor_row(service, path) is None


def test_source_subagent_without_a_parent_is_ignored(service, roots):
    path = write_session(roots, conversation(meta({'id': SESSION, 'source': GUARDIAN})))
    write_config(service, roots, since=NEW)
    assert scan(service)['ignored_subagent_sessions'] == 1
    assert archives(service) == [] and cursor_row(service, path) is None


def test_only_a_parent_thread_marker_is_ignored(service, roots):
    path = write_session(roots, conversation(meta({'id': SESSION, 'parent_thread_id': OTHER})))
    write_config(service, roots, since=NEW)
    assert scan(service)['ignored_subagent_sessions'] == 1
    assert archives(service) == [] and cursor_row(service, path) is None


def test_a_tracked_subagent_session_never_advances_or_archives(service, roots):
    """A cursor created before this filter is parked, never consumed."""
    path = write_session(roots, conversation(meta({'id': SESSION, 'source': GUARDIAN,
                                                   'parent_thread_id': OTHER})))
    write_config(service, roots, since=NEW)
    track_untouched_cursor(service, path)
    first = scan(service)
    assert first['archives'] == 0 and first['ignored_subagent_sessions'] == 1
    row = cursor_row(service, path)
    assert row['file_state'] == capture.STATE_IGNORED_SUBAGENT
    assert row['scanned_offset_bytes'] == 0, 'the cursor must not move'
    # The sub-session keeps appending; nothing may be archived or consumed.
    with open(path, 'a', encoding='utf-8') as handle:
        handle.write(jsonl(rows()[-2:]))
    second = scan(service)
    assert second['archives'] == 0 and second['bytes_read'] == 0
    assert second['ignored_subagent_sessions'] == 1
    assert cursor_row(service, path)['scanned_offset_bytes'] == 0
    state = status(service)
    assert state['pending_bytes'] == 0, 'an ignored session is never backlog'
    session = state['sessions'][0]
    assert session['ignored_subagent'] is True and session['pending_bytes'] == 0
    assert session['needs_review'] is False and session['last_error'] == ''


def test_real_user_sessions_keep_being_archived(service, roots):
    """vscode/cli sessions and plain user forks stay in scope."""
    shapes = {
        'vscode': {'id': SESSION, 'source': {'vscode': {'task': 'code'}}},
        'cli': {'id': SESSION, 'source': {'cli': 'local'}},
        'forked': {'id': SESSION, 'forked_from_id': OTHER},
        'plain': {'id': SESSION},
    }
    for name, payload in shapes.items():
        session = '01a0fb28-893d-7250-9445-1a2c2fe6a0%02d' % len(name)
        path = write_session(roots, conversation(meta({**payload, 'id': session})),
                             name='rollout-2026-06-01T00-00-00-%s-%s.jsonl' % (session, name))
        write_config(service, roots, since=NEW)
        assert capture.subagent_session(path) is False, name
        write_config(service, roots, since=NEW)
        result = scan(service)
        assert result['ignored_subagent_sessions'] == 0, name
        assert result['archives'] == 1, name
        stored = payloads(service)
        assert stored[-1]['source']['session_id'] == session, name
        assert TEXT_HEAD in stored[-1]['transcript'], name


def test_a_session_without_any_session_meta_is_still_collected(service, roots):
    """The existing mid-session batch contract is unchanged (no meta, no marker)."""
    path = write_session(roots, [event('response_item', {'type': 'message', 'role': 'user',
                                                         'id': 'evt-user-1',
                                                         'content': [{'type': 'input_text', 'text': TEXT_HEAD}]}),
                                 event('response_item', {'type': 'message', 'role': 'assistant',
                                                         'id': 'evt-ai-1',
                                                         'content': [{'type': 'output_text', 'text': TEXT_AI}]})])
    write_config(service, roots, since=NEW)
    assert capture.subagent_session(path) is False
    assert scan(service)['archives'] == 1
    assert status(service)['ignored_subagent_sessions'] == 0


def test_the_subagent_check_reads_only_the_first_line(service, roots, monkeypatch):
    """The verdict is a bounded first-line read, cached in the existing state."""
    path = write_session(roots, conversation(meta({'id': SESSION, 'source': GUARDIAN,
                                                   'parent_thread_id': OTHER})))
    write_config(service, roots, since=NEW)
    track_untouched_cursor(service, path)
    reads = []
    real_open = open

    def counting_open(name, *args, **kwargs):
        if str(name) == str(path):
            reads.append(name)
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr('builtins.open', counting_open)
    assert scan(service)['ignored_subagent_sessions'] == 1
    assert len(reads) == 1, 'only the first-line probe may open the file'
    # The cached verdict means later scans do not open it at all.
    reads.clear()
    assert scan(service)['ignored_subagent_sessions'] == 1
    assert reads == []
    status(service)
    assert reads == []


# ------------------------------------------------------------------ discovery

def test_archived_batch_is_discoverable_by_auto_new(service, roots):
    write_session(roots, rows())
    write_config(service, roots, since=NEW)
    set_auto_new(service, True)
    assert status(service)['auto_new'] is True and status(service)['active'] is True
    scan(service)
    from evolvmem.organization_arrival import discover_new
    result = discover_new(service)
    assert result['enabled'] is True and result['created'] == 1
    assert result['items'][0]['source_key'] == 'archive:%d' % archives(service)[-1]['id']


def test_disabled_config_is_never_scanned(service, roots):
    write_session(roots, rows())
    write_config(service, roots, enabled=False, since=NEW)
    assert scan(service)['skipped'] == 'disabled'
    assert archives(service) == []


def test_arrival_switch_off_stops_worker_collection(service, roots):
    write_session(roots, rows())
    write_config(service, roots, enabled=True, since=NEW)
    from evolvmem.local_codex_capture import capture_once
    state = {}
    assert capture_once(service, state, now=1000.0) == 0
    assert archives(service) == []
    set_auto_new(service, True)
    assert capture_once(service, state, now=1000.0) == 1
    assert len(archives(service)) == 1
    assert capture_once(service, state, now=1000.0) == 0
    assert len(archives(service)) == 1


# --------------------------------------------------------------------- status

def test_status_carries_no_body_text_or_roots(service, roots):
    write_session(roots, rows())
    write_config(service, roots, since=NEW)
    scan(service)
    state = status(service)
    body = json.dumps(state, ensure_ascii=False)
    assert TEXT_HEAD not in body and TEXT_AI not in body
    assert 'sessions_roots' not in body and str(roots) not in body
    assert state['enabled'] is True and state['archived_batches'] == 1
    assert state['auto_new'] is False and state['active'] is False
    assert state['needs_review'] == 0 and state['files'] == 1
    assert state['last_success_at']
    assert state['sessions'][0]['archived_events'] == 2
    assert state['config']['since_epoch'] == pytest.approx(NEW.timestamp())


# ------------------------------------------------- worker and read-only route

def test_worker_loop_archives_then_discovers_without_a_second_scan(service, roots):
    write_session(roots, rows())
    write_config(service, roots, since=NEW)
    set_auto_new(service, True)
    from evolvmem.auto_organization import OrganizationWorker
    worker = OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    worker._service = service  # single bounded call; the real thread owns its own connection
    assert worker.capture_once() is True
    assert len(archives(service)) == 1
    assert worker.capture_once() is False
    assert len(archives(service)) == 1
    from evolvmem.organization_arrival import discover_new
    assert discover_new(service)['created'] == 1


def test_worker_round_is_silent_when_arrival_is_off(service, roots):
    write_session(roots, rows())
    write_config(service, roots, since=NEW)
    from evolvmem.auto_organization import OrganizationWorker
    worker = OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    worker._service = service
    assert worker.capture_once() is False
    assert archives(service) == []


def test_a_capture_failure_never_escapes_the_worker(service, monkeypatch):
    from evolvmem.auto_organization import OrganizationWorker
    worker = OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    worker._service = service

    def broken(*args, **kwargs):
        raise RuntimeError('synthetic capture failure')

    monkeypatch.setattr(capture, 'capture_once', broken)
    assert worker.capture_once() is False


def test_the_worker_thread_survives_a_failing_capture_round(service, monkeypatch):
    from evolvmem import auto_organization
    from evolvmem.auto_organization import OrganizationWorker
    monkeypatch.setattr(auto_organization, 'POLL_SECONDS', 0.05)

    def broken(self):
        raise RuntimeError('synthetic capture failure')

    monkeypatch.setattr(OrganizationWorker, 'capture_once', broken)
    worker = OrganizationWorker(service.config, mode=ContextMode.SHADOW).start()
    try:
        time.sleep(0.4)
        assert worker._thread.is_alive()
    finally:
        assert worker.stop() is True


def test_the_schema_marker_is_bounded_to_one_connection():
    class _Store:
        def __init__(self, connection):
            self._conn = connection

        def _connection(self):
            return self._conn

        def transaction(self):
            return contextlib.nullcontext()

    connections = [sqlite3.connect(':memory:') for _ in range(3)]
    try:
        for connection in connections:
            capture.ensure_schema(_Store(connection))
        # Exactly one connection is remembered: no registry of every closed one.
        assert capture._SCHEMA_CONNECTION is connections[-1]
        assert not isinstance(capture._SCHEMA_CONNECTION, (dict, list, set, tuple))
    finally:
        for connection in connections:
            connection.close()


@pytest.fixture
def http_server(service):
    holder, ready = {}, threading.Event()

    def serve():
        from evolvmem.web_server import make_handler
        server = HTTPServer(('127.0.0.1', 0), make_handler(service))
        holder['server'] = server
        ready.set()
        server.serve_forever()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    ready.wait(timeout=5)
    yield 'http://127.0.0.1:%d' % holder['server'].server_address[1]
    holder['server'].shutdown()
    holder['server'].server_close()
    thread.join(timeout=5)


def test_read_only_route_reports_status_without_body_text(service, roots, http_server):
    write_session(roots, rows())
    write_config(service, roots, since=NEW)
    set_auto_new(service, True)
    scan(service)
    with urllib.request.urlopen(http_server + '/api/local-capture') as response:
        payload = json.loads(response.read().decode('utf-8'))
    assert payload['ok'] is True and payload['enabled'] is True
    assert payload['auto_new'] is True and payload['active'] is True
    assert payload['archived_batches'] == 1
    assert payload['sessions'][0]['session_id'] == SESSION
    assert payload['last_success_at']
    body = json.dumps(payload, ensure_ascii=False)
    assert TEXT_HEAD not in body and TEXT_AI not in body
    assert 'sessions_roots' not in body and str(roots) not in body


def test_read_only_route_says_disabled_without_a_config(service, http_server):
    with urllib.request.urlopen(http_server + '/api/local-capture') as response:
        payload = json.loads(response.read().decode('utf-8'))
    assert payload['ok'] is True and payload['enabled'] is False
    assert payload['configured'] is False and payload['active'] is False
    assert payload['archived_batches'] == 0

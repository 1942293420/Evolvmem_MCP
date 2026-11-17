"""Canonical import tests. SQLite is real; continuity ports are explicit effects."""
from contextlib import contextmanager
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import weakref

from evolvmem import codex_transcript as CT, lan_backfill as LB
from evolvmem.continuity_models import ContinuityImportResult
from evolvmem.lan_sharing import LanError


def line(row):
    return json.dumps(row, ensure_ascii=False).encode('utf-8')


def meta(identity='session', **extra):
    return {'type': 'session_meta', 'payload': {'id': identity, 'cwd': 'C:/work/demo', **extra}}


def message(text='保留完整记录'):
    return {'type': 'event_msg', 'payload': {'type': 'user_message', 'message': text}}


class WholeDecodeForbidden(bytes):
    def decode(self, *args, **kwargs):
        raise AssertionError('whole transcript decode allocates an avoidable full copy')


@contextmanager
def import_fixture(raw, *, project='demo'):
    conn = sqlite3.connect(':memory:')
    conn.execute('CREATE TABLE effects(objective TEXT)')
    conn.execute('CREATE TABLE lan_session_backfills(device_id TEXT, session_id TEXT, applied_revision INTEGER, workstream_id TEXT, PRIMARY KEY(device_id,session_id))')
    payload = json.dumps({'transcript': raw.decode('utf-8')}, ensure_ascii=False)
    calls = []
    class Service:
        def _resolve_project(self, *args):
            calls.append('resolve')
            return 'demo'
        def _workstream_row(self, *args):
            return None
        def import_interrupted(self, request):
            calls.append('write')
            conn.execute('INSERT INTO effects VALUES(?)', (request.objective,))
            return ContinuityImportResult(code='created', workstream_id=request.workstream_id, checkpoint_revision=1)
    service = Service()
    capture = SimpleNamespace(
        archiver=SimpleNamespace(read_payload=lambda _: payload), conn=conn,
        store=SimpleNamespace(transaction=lambda: conn),
        server=SimpleNamespace(_continuity=lambda: service))
    row = dict(archive_id=1, session_id='session', device_id='device', project=project)
    @contextmanager
    def remote(*args):
        calls.append('remote')
        yield SimpleNamespace(resolve=lambda _: SimpleNamespace(fingerprint='fixed'))
    try:
        with patch.object(LB, 'remote_context', remote), patch.object(LB, '_objective_matched_workstream', return_value=None):
            yield capture, row, calls
    finally:
        conn.close()


class SinglePassTests(unittest.TestCase):
    def test_parser_never_decodes_entire_transcript(self):
        raw = WholeDecodeForbidden(line(meta()) + b'\n' + line(message()) + b'\n')
        rows, messages = CT.parse_transcript(raw, 'session')
        self.assertEqual(len(rows), 2)
        self.assertEqual(messages, [{'role': 'user', 'content': '保留完整记录'}])

    def test_backfill_decodes_each_record_once_and_does_not_build_dialogue(self):
        encoded = [line(meta()), line(message('single parse objective')), line({'type': 'ignored', 'payload': {}})]
        raw = b'\n'.join(encoded) + b'\n'
        original = json.loads
        seen = []
        def tracked(value, *args, **kwargs):
            normalized = value.decode() if isinstance(value, bytes) else value
            if normalized in [item.decode() for item in encoded]:
                seen.append(normalized)
            return original(value, *args, **kwargs)
        with import_fixture(raw) as (capture, row, calls):
            with patch.object(CT.json, 'loads', tracked), patch.object(CT, 'dialogue_messages', wraps=CT.dialogue_messages) as dialogue:
                result = LB._import(capture, row)
            self.assertEqual(result['code'], 'created')
            self.assertEqual(capture.conn.execute('SELECT COUNT(*) FROM effects').fetchone()[0], 1)
            self.assertEqual(len(seen), len(encoded))
            self.assertEqual(dialogue.call_count, 0)

    def test_event_rows_are_the_original_objects_with_physical_line_numbers(self):
        raw = b'\n' + line(meta()) + b'\r\n\t \n' + line(message()) + b'\n'
        rows, events = CT.parse_transcript_events(raw, 'session', max_line_bytes=LB.MAX_LINE_BYTES)
        self.assertEqual([number for number, _ in events], [2, 4])
        self.assertEqual(len(rows), 2)
        self.assertIs(events[0][1], rows[0])
        self.assertIs(events[1][1], rows[1])

    def test_limit_applies_to_events_not_complete_validation_or_rows(self):
        limit = 128
        base = line(message(''))
        exact = line(message('x' * (limit - len(base))))
        oversized = line(message('y' * (limit - len(base) + 1)))
        self.assertEqual(len(exact), limit)
        raw = line(meta()) + b'\n' + exact + b'\r\n' + oversized + b'\n'
        rows, events = CT.parse_transcript_events(raw, 'session', max_line_bytes=limit)
        self.assertEqual(len(rows), 3)
        self.assertEqual([number for number, _ in events], [1, 2])
        self.assertEqual(rows[2], json.loads(oversized))
        with self.assertRaisesRegex(LanError, 'invalid_transcript'):
            CT.parse_transcript_events(raw + b'{"bad":"' + b'z' * 129 + b'\n', 'session', max_line_bytes=limit)

    def test_unicode_separators_are_content_not_record_boundaries(self):
        text = 'left\u0085middle\u2028more\u2029right'
        raw = line(meta()) + b'\n' + line(message(text)) + b'\n'
        rows, messages = CT.parse_transcript(raw, 'session')
        self.assertEqual(messages[0]['content'], text)
        rows2, events = CT.parse_transcript_events(raw, 'session', max_line_bytes=LB.MAX_LINE_BYTES)
        self.assertEqual(rows, rows2)
        self.assertEqual([n for n, _ in events], [1, 2])
        # bytes.splitlines, used in the old second pass, never split Unicode separators.
        self.assertEqual(len(raw.splitlines()), 2)

    def test_fork_keeps_first_meta_identity_and_subagent_attribution(self):
        raw = b'\n'.join([line(meta(forked_from_id='parent')), line(meta('parent')), line(message())]) + b'\n'
        rows, _ = CT.parse_transcript(raw, 'session')
        self.assertFalse(CT.workspace_details(rows)['subagent'])
        with self.assertRaisesRegex(LanError, 'session_id_mismatch'):
            CT.parse_transcript(raw, 'parent')
        child = line(meta(parent_thread_id='parent')) + b'\n' + line(message()) + b'\n'
        with import_fixture(child) as (capture, row, calls):
            self.assertEqual(LB._import(capture, row), dict(code='skipped', reason='subagent_session'))
            self.assertEqual(calls, [])

    def test_dialogue_mirror_channels_and_recipient_are_unchanged(self):
        rows = [meta(), {'type': 'response_item', 'payload': {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'same'}]}}, message('same'), {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'channel': 'analysis', 'recipient': 'tool', 'content': 'think'}}]
        raw = b'\n'.join(map(line, rows)) + b'\n'
        actual_rows, messages = CT.parse_transcript(raw, 'session')
        self.assertEqual(actual_rows, rows)
        self.assertEqual(messages, [{'role': 'user', 'content': 'same'}, {'role': 'assistant', 'content': 'think', 'channel': 'analysis', 'recipient': 'tool'}])

    def test_invalid_input_codes_are_preserved(self):
        valid = line(meta()) + b'\n'
        cases = [(b'', 'invalid_transcript'), (valid[:-1], 'invalid_transcript'), (b'\n\t\n', 'invalid_transcript'), (valid + b'[]\n', 'invalid_transcript'), (valid + b'{bad}\n', 'invalid_transcript'), (valid + b'\xff\n', 'invalid_transcript'), (b'\xef\xbb\xbf' + valid, 'invalid_transcript'), (line(message()) + b'\n', 'session_id_mismatch'), (line(meta('other')) + b'\n', 'session_id_mismatch')]
        for raw, code in cases:
            with self.subTest(code=code, raw_kind=repr(raw[:8])):
                with self.assertRaisesRegex(LanError, '^' + code + '$'):
                    CT.parse_transcript(raw, 'session')

    def test_late_invalid_json_never_enters_workspace_or_persistent_effect(self):
        raw = line(meta()) + b'\n' + line(message()) + b'\n' + b'{"broken":\n'
        with import_fixture(raw) as (capture, row, calls):
            with self.assertRaisesRegex(LanError, 'invalid_transcript'):
                LB._import(capture, row)
            self.assertEqual(calls, [])
            self.assertEqual(capture.conn.execute('SELECT COUNT(*) FROM effects').fetchone()[0], 0)
            self.assertEqual(capture.conn.execute('SELECT COUNT(*) FROM lan_session_backfills').fetchone()[0], 0)

    def test_bare_cr_whitespace_uses_lf_records_not_old_second_pass_splits(self):
        raw = line(meta()) + b'\n\r\n' + b'{\r"type":"event_msg","payload":{"type":"user_message","message":"physical LF objective"}}\n'
        rows, _ = CT.parse_transcript(raw, 'session')
        self.assertEqual(len(rows), 2)
        with import_fixture(raw) as (capture, row, calls):
            result = LB._import(capture, row)
            self.assertEqual(result['code'], 'created')
        rows, events = CT.parse_transcript_events(raw, 'session', max_line_bytes=LB.MAX_LINE_BYTES)
        self.assertEqual([n for n, _ in events], [1, 3])

    def test_parsed_row_objects_retire_before_any_continuity_write(self):
        raw = line(meta()) + b'\n' + line(message('bounded objective')) + b'\n'
        references = []
        original = json.loads
        class TrackedRow(dict):
            pass
        def tracked(value, *args, **kwargs):
            result = original(value, *args, **kwargs)
            if isinstance(result, dict) and 'type' in result:
                result = TrackedRow(result)
                references.append(weakref.ref(result))
            return result
        with import_fixture(raw) as (capture, row, calls):
            service = capture.server._continuity()
            original_write = service.import_interrupted
            def write(request):
                self.assertGreaterEqual(len(references), 2)
                self.assertTrue(all(ref() is None for ref in references), 'full parsed rows retained across persistent effect')
                return original_write(request)
            with patch.object(CT.json, 'loads', tracked), patch.object(service, 'import_interrupted', write):
                self.assertEqual(LB._import(capture, row)['code'], 'created')

    def test_real_backfill_limit_does_not_discard_archive_or_bypass_attribution(self):
        oversized = line(message('x' * LB.MAX_LINE_BYTES))
        raw = line(meta()) + b'\n' + oversized + b'\n\n' + line(message('last valid objective')) + b'\n'
        original_parse = LB._parse_events
        seen = []
        def parse(path, parsed, events, **kwargs):
            seen.extend(number for number, _ in events)
            return original_parse(path, parsed, events, **kwargs)
        with import_fixture(raw) as (capture, row, calls), patch.object(LB, '_parse_events', parse):
            self.assertEqual(LB._import(capture, row)['code'], 'created')
            self.assertEqual(seen, [1, 4])
            self.assertEqual(capture.conn.execute('SELECT objective FROM effects').fetchone()[0], 'last valid objective')
        changed = line({'type': 'turn_context', 'payload': {'cwd': 'D:/elsewhere', 'padding': 'x' * LB.MAX_LINE_BYTES}})
        raw = line(meta()) + b'\n' + changed + b'\n' + line(message()) + b'\n'
        with import_fixture(raw) as (capture, row, calls):
            self.assertEqual(LB._import(capture, row), dict(code='candidate', reason='mixed_workspace'))
            self.assertEqual(calls, [])

    def test_original_event_algorithm_has_same_checkpoint_fields_for_valid_common_framing(self):
        rows = [meta(forked_from_id='parent'), meta('parent'), message('preserve objective\u2028and decisions'),
                {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'c', 'name': 'exec_command', 'arguments': '{"cmd":"run tests"}'}},
                {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'c', 'output': '{"exit_code":0}'}},
                {'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant', 'content': 'all work is completed'}}]
        for ending in (b'\n', b'\r\n'):
            with self.subTest(ending=ending):
                raw = ending + ending.join(line(value) for value in rows) + ending
                # Independent old second-pass algorithm, never candidate helper.
                old = [(i, json.loads(value)) for i, value in enumerate(raw.splitlines(), 1)
                       if value.strip() and len(value) <= LB.MAX_LINE_BYTES]
                new_rows, new = CT.parse_transcript_events(raw, 'session', max_line_bytes=LB.MAX_LINE_BYTES)
                self.assertEqual(new_rows, rows)
                self.assertEqual(new, old)
                before = LB._ParsedSession('session', '', [], {}, 0)
                after = LB._ParsedSession('session', '', [], {}, 0)
                LB._parse_events(Path('.'), before, old, same_workspace=CT.same_client_workspace)
                LB._parse_events(Path('.'), after, new, same_workspace=CT.same_client_workspace)
                self.assertEqual(asdict(after), asdict(before))
                self.assertEqual(LB._checkpoint_fields(after), LB._checkpoint_fields(before))

    def test_unicode_whitespace_only_line_is_validated_once_consistently(self):
        raw = line(meta()) + b'\n' + '\u2028'.encode() + b'\n' + line(message()) + b'\n'
        self.assertEqual(len(CT.parse_transcript(raw, 'session')[0]), 2)
        # Old bytes.strip did not consider this blank; its second loads rejected it.
        with self.assertRaises(ValueError):
            [json.loads(value) for value in raw.splitlines() if value.strip()]
        with import_fixture(raw) as (capture, row, calls):
            self.assertEqual(LB._import(capture, row)['code'], 'created')


if __name__ == '__main__':
    unittest.main()

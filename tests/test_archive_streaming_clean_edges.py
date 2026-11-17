"""Differential native-event and recovery safety boundaries on synthetic data."""
import hashlib
import json
from pathlib import Path

import pytest

from evolvmem import archive_streaming_clean as streaming
from evolvmem.conversation import from_payload
from evolvmem.extraction_policy import redact_messages
from evolvmem.lan_sharing import LanError
from evolvmem.session_archive import SessionArchiver
from tests.test_governance_migration import service, base_service, plan

SESSION = 'e81beaa1-134a-4f32-9527-6be5022ac572'


def meta(**extra):
    return {'type': 'session_meta', 'payload': {'id': SESSION, 'cwd': '/tmp/edge', **extra}}


def message(text, *, role='user', kind='response_item', **extra):
    if kind == 'event_msg':
        return {'type': kind, 'payload': {'type': 'user_message' if role == 'user' else 'agent_message', 'message': text, **extra}}
    return {'type': kind, 'payload': {'type': 'message', 'role': role, 'content': [{'type': 'input_text', 'text': text}], **extra}}


def payload(rows):
    return {'session_id': SESSION, 'transcript': ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows)}


def read_native(tmp_path, data, policy=None, **kwargs):
    target = tmp_path / 'native.jsonl'
    target.write_text(data['transcript'], encoding='utf-8', newline='')
    return streaming.native_dialogue(target, data['session_id'], policy or {}, **kwargs)[0]


@pytest.mark.parametrize('joiner', ['', ' ', '\t'])
def test_jsonl_rejects_multiple_objects_on_one_physical_line(tmp_path, joiner):
    data = {'session_id': SESSION, 'transcript': json.dumps(meta()) + joiner + json.dumps(message('business')) + '\n'}
    with pytest.raises(LanError):
        from_payload(data)
    with pytest.raises(ValueError, match='invalid_transcript'):
        read_native(tmp_path, data)


def test_jsonl_rejects_pretty_printed_multiline_record(tmp_path):
    data = {'session_id': SESSION, 'transcript': json.dumps(meta(), indent=2) + '\n' + json.dumps(message('business')) + '\n'}
    with pytest.raises(LanError):
        from_payload(data)
    with pytest.raises(ValueError, match='invalid_transcript'):
        read_native(tmp_path, data)


@pytest.mark.parametrize('bad_payload', [None, [], 'wrong metadata', False])
def test_first_invalid_session_meta_cannot_be_replaced_by_later_valid_one(tmp_path, bad_payload):
    data = payload([{'type': 'session_meta', 'payload': bad_payload}, meta(), message('business')])
    with pytest.raises(LanError):
        from_payload(data)
    with pytest.raises(ValueError, match='session_id_mismatch'):
        read_native(tmp_path, data)


@pytest.mark.parametrize('collapse', [False, True])
def test_duplicate_collapse_happens_before_redaction(tmp_path, collapse):
    data = payload([meta(), message('token=synthetic_one'), message('token=synthetic_two')])
    policy = {'settings': {'cleaning_collapse_duplicates': collapse}}
    expected, _ = redact_messages(from_payload(data, policy=policy))
    assert len(expected) == 2 and expected[0] == expected[1]
    assert read_native(tmp_path, data, policy) == expected


@pytest.mark.parametrize('output', [' ', '\t\r\n', '\u2003\u3000'])
def test_whitespace_only_tool_output_does_not_break_native_mirror_collapse(tmp_path, output):
    data = payload([meta(), message('same business message'),
                    {'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': output}},
                    message('same business message', kind='event_msg')])
    expected, _ = redact_messages(from_payload(data))
    assert len(expected) == 1
    assert read_native(tmp_path, data) == expected


@pytest.mark.parametrize('collapse', [False, True])
@pytest.mark.parametrize('line_ending', ['\n', '\r\n'])
def test_business_dialogue_filtering_mirrors_cleaning_and_preserves_unicode(tmp_path, collapse, line_ending):
    text = '业务要求\u2028保留段落\u2029和中文🌌'
    rows = [meta(), message(text), message(text, kind='event_msg'),
            message('private thought', role='assistant', channel='analysis'),
            message('private recipient', role='assistant', recipient='python'),
            {'type': 'response_item', 'payload': {'type': 'function_call_output', 'output': {'large': [False, None, 1]}}},
            message('运输消息 <INSTRUCTIONS>injected</INSTRUCTIONS>\nDROP\n剩余正文'),
            message('引用 `<INSTRUCTIONS>必须保留</INSTRUCTIONS>`', role='assistant'),
            message('重复正文'), message('重复正文'),
            {'type': 'session_meta', 'payload': {'id': 'old-lineage', 'parent_thread_id': 'old-parent'}},
            message('结尾')]
    data = payload(rows)
    data['transcript'] = '\t\n' + data['transcript'].replace('\n', line_ending) + '  \n'
    policy = {'settings': {'cleaning_collapse_duplicates': collapse, 'cleaning_drop_lines': ['DROP']}}
    expected, _ = redact_messages(from_payload(data, policy=policy))
    assert read_native(tmp_path, data, policy) == expected


@pytest.mark.parametrize('identity', [{'source': {'subagent': {}}}, {'parent_thread_id': 'real-parent'}])
def test_native_subagent_metadata_never_emits_business_dialogue(tmp_path, identity):
    data = payload([meta(**identity), message('属于系统子会话')])
    assert read_native(tmp_path, data) == from_payload(data) == []


def test_native_stream_does_not_accumulate_unknown_root_key_metadata(tmp_path, monkeypatch):
    original = streaming.JsonReader
    counts = []

    class ObservedReader(original):
        def value(self, keep=True, depth=0):
            value = super().value(keep, depth)
            if depth == 0:
                counts.append(len(self.root_keys))
            return value

    monkeypatch.setattr(streaming, 'JsonReader', ObservedReader)
    data = payload([meta(), *({'type': 'unknown', f'unique_{index}': 'ignored'} for index in range(100))])
    assert read_native(tmp_path, data) == []
    assert counts and max(counts) <= 2


def archive(service):
    data = payload([meta(), message('业务来源必须保留完整证据。'), message('正常答复', role='assistant')])
    result = SessionArchiver(service.config, service.store).archive_session('', 'codex', SESSION, json.dumps(data, ensure_ascii=False))
    conn = service.store._connection()
    row = dict(conn.execute('SELECT * FROM session_archives WHERE id=?', (result.id,)).fetchone())
    physical = dict(conn.execute('SELECT * FROM session_archive_storage WHERE archive_id=?', (result.id,)).fetchone())
    return row, physical


def evidence_hashes(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for folder in ('session_archives', 'session_chunks') for path in (directory / folder).rglob('*') if path.is_file()}


def test_temporary_cleartext_is_private_and_removed_after_failed_cleaning(service, monkeypatch, tmp_path):
    row, physical = archive(service)
    parent = tmp_path / 'private-temporary'; parent.mkdir()
    monkeypatch.setattr(streaming.tempfile, 'tempdir', str(parent))
    original = streaming.authenticated_spool
    seen = []

    def inspect(*args):
        result = original(*args)
        target = args[-1]
        assert target.parent.stat().st_mode & 0o777 == 0o700
        assert target.stat().st_mode & 0o777 == 0o600
        assert (target.parent / 'transcript.jsonl').stat().st_mode & 0o777 == 0o600
        seen.append(target.parent)
        return result

    monkeypatch.setattr(streaming, 'authenticated_spool', inspect)
    before = evidence_hashes(service.config.data_dir)
    assert before
    with pytest.raises(ValueError, match='cleaned_archive_budget_exceeded'):
        streaming.derive(service.config, row, physical, {}, max_clean_bytes=1)
    assert seen and not seen[0].exists()
    assert before == evidence_hashes(service.config.data_dir)


def test_policy_revision_change_during_derivation_refuses_write(service, monkeypatch):
    row, physical = archive(service)
    conn = service.store._connection()
    conn.execute('DELETE FROM conversation_history WHERE archive_id=?', (row['id'],)); conn.commit()
    prepared = plan(service)
    real = streaming.derive
    rules = service.knowledge().rules
    old_read = rules.read

    def race(*args, **kwargs):
        result = real(*args, **kwargs)
        monkeypatch.setattr(type(rules), 'read', lambda _: {**old_read(), 'revision': 'concurrent-rule-edit'})
        return result

    monkeypatch.setattr(streaming, 'derive', race)
    with pytest.raises(ValueError, match='migration_revision_conflict'):
        streaming.repair(service, prepared, f"archive:{row['id']}", apply=True)
    assert conn.execute('SELECT count(*) FROM conversation_history WHERE archive_id=?', (row['id'],)).fetchone()[0] == 0


def test_manifest_byte_change_during_derivation_refuses_write(service, monkeypatch):
    row, physical = archive(service)
    conn = service.store._connection()
    conn.execute('DELETE FROM conversation_history WHERE archive_id=?', (row['id'],)); conn.commit()
    prepared = plan(service)
    real = streaming.derive

    def race(*args, **kwargs):
        result = real(*args, **kwargs)
        target = service.config.data_dir / row['payload_path']
        target.write_bytes(target.read_bytes() + b'changed')
        return result

    monkeypatch.setattr(streaming, 'derive', race)
    with pytest.raises(ValueError, match='archive_manifest_changed'):
        streaming.repair(service, prepared, f"archive:{row['id']}", apply=True)
    assert conn.execute('SELECT count(*) FROM conversation_history WHERE archive_id=?', (row['id'],)).fetchone()[0] == 0


def test_manifest_growth_after_derivation_is_rejected_before_unbounded_read(service, monkeypatch):
    row, physical = archive(service)
    conn = service.store._connection()
    conn.execute('DELETE FROM conversation_history WHERE archive_id=?', (row['id'],)); conn.commit()
    prepared = plan(service)
    target = service.config.data_dir / row['payload_path']
    real = streaming.derive
    original_read = Path.read_bytes

    def guarded_read(path):
        if path == target:
            pytest.fail('oversized replacement must be rejected before reading bytes')
        return original_read(path)

    def race(*args, **kwargs):
        result = real(*args, **kwargs)
        with target.open('r+b') as output:
            output.truncate(streaming.MAX_MANIFEST_BYTES + 1)
        monkeypatch.setattr(Path, 'read_bytes', guarded_read)
        return result

    monkeypatch.setattr(streaming, 'derive', race)
    with pytest.raises(ValueError, match='archive_manifest_changed'):
        streaming.repair(service, prepared, f"archive:{row['id']}", apply=True)
    assert conn.execute('SELECT count(*) FROM conversation_history WHERE archive_id=?', (row['id'],)).fetchone()[0] == 0


def test_source_metadata_change_during_derivation_refuses_write(service, monkeypatch):
    row, physical = archive(service)
    conn = service.store._connection()
    conn.execute('DELETE FROM conversation_history WHERE archive_id=?', (row['id'],)); conn.commit()
    prepared = plan(service)
    real = streaming.derive

    def race(*args, **kwargs):
        result = real(*args, **kwargs)
        with service.store.transaction():
            conn.execute('UPDATE session_archives SET payload_sha256=? WHERE id=?', ('concurrent-content', row['id']))
        return result

    monkeypatch.setattr(streaming, 'derive', race)
    with pytest.raises(ValueError, match='migration_revision_conflict'):
        streaming.repair(service, prepared, f"archive:{row['id']}", apply=True)
    assert conn.execute('SELECT count(*) FROM conversation_history WHERE archive_id=?', (row['id'],)).fetchone()[0] == 0


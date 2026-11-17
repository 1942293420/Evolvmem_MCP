"""Upload append CPU stays linear while encrypted evidence remains durable."""
import hashlib
import json
import os
import pytest
from tests.test_lan_sharing import lan
from tests.test_lan_capture import transcript, upload
import evolvmem.lan_capture as staging


def test_sequential_upload_does_not_decode_the_entire_prefix_for_each_chunk(lan, monkeypatch):
    _, adapter = lan
    raw = transcript(text='Keep encrypted evidence and durable progress. ' * 80_000)
    decompressed = []
    original = staging.zlib.decompressobj
    class ObservedDecoder:
        def __init__(self, decoder):
            self.decoder = decoder
        def __getattr__(self, name):
            return getattr(self.decoder, name)
        def decompress(self, *args, **kwargs):
            value = self.decoder.decompress(*args, **kwargs)
            decompressed.append(len(value))
            return value
    monkeypatch.setattr(staging.zlib, 'decompressobj', lambda *a, **kw: ObservedDecoder(original(*a, **kw)))
    step = 262_144
    for offset in range(0, len(raw), step):
        result = upload(adapter, raw, start=offset, end=min(offset + step, len(raw)))
        assert 'error' not in result, result
    assert result['status'] == 'archived'
    assert sum(decompressed) <= len(raw) * 2, 'received prefix was repeatedly decompressed'
    capture = adapter.captures['jiangli']
    assert json.loads(capture.archiver.read_payload(result['archive_id']))['transcript'].encode() == raw


def test_final_upload_parses_the_same_transcript_only_once(lan, monkeypatch):
    _, adapter = lan
    parsed = []
    original = staging.parse_transcript
    def observed(*args, **kwargs):
        parsed.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(staging, 'parse_transcript', observed)
    assert upload(adapter, transcript())['status'] == 'archived'
    assert len(parsed) == 1


def stage_path(capture, raw):
    return capture._stage(('windows-main', 'session-1', hashlib.sha256(raw).hexdigest()))


@pytest.mark.parametrize('damage', ['middle_ciphertext', 'old_complete_prefix'])
def test_append_authenticates_prefix_and_never_rewinds_progress(lan, damage):
    _, adapter = lan
    raw = transcript(text='Authenticate even an ordinary nonfinal append. ' * 28_000)
    step = 262_144
    capture = adapter.captures['jiangli']
    first = upload(adapter, raw, end=step)
    path = stage_path(capture, raw)
    old = path.read_bytes()
    assert upload(adapter, raw, start=step, end=2 * step)['next_offset'] == 2 * step
    if damage == 'old_complete_prefix':
        path.write_bytes(old)
    else:
        blob = bytearray(path.read_bytes())
        blob[len(staging._CHUNKED_STAGE_MAGIC) + 25] ^= 1
        path.write_bytes(blob)
    result = upload(adapter, raw, start=2 * step, end=3 * step)
    assert result.get('error') == 'upload_staging_unavailable'
    row = capture.conn.execute('SELECT received_bytes FROM lan_session_uploads WHERE sha256=?', (hashlib.sha256(raw).hexdigest(),)).fetchone()
    assert row['received_bytes'] == 2 * step


def test_v3_encrypted_prefix_migrates_and_resumes_without_changing_original(lan):
    _, adapter = lan
    raw = transcript(text='Version three already exists on production. ' * 24_000)
    step = 262_144
    assert upload(adapter, raw, end=step)['next_offset'] == step
    capture = adapter.captures['jiangli']
    path = stage_path(capture, raw)
    aes = staging.AESGCM(capture.archiver._load_or_create_key())
    magic = b'EVOM-UPLOAD\x03\r\n'
    blob = bytearray(magic)
    nonce = os.urandom(12)
    frame = nonce + aes.encrypt(nonce, staging.zlib.compress(raw[:step], 3), magic + path.name.encode() + b'D' + (0).to_bytes(8, 'big'))
    blob.extend(b'D' + len(frame).to_bytes(4, 'big') + frame)
    nonce = os.urandom(12)
    footer = (1).to_bytes(8, 'big') + step.to_bytes(8, 'big')
    footer = nonce + aes.encrypt(nonce, footer, magic + path.name.encode() + b'F')
    blob.extend(b'F' + len(footer).to_bytes(4, 'big') + footer)
    path.write_bytes(blob)
    for offset in range(step, len(raw), step):
        result = upload(adapter, raw, start=offset, end=min(offset + step, len(raw)))
        assert 'error' not in result, result
    assert result['status'] == 'archived'
    assert json.loads(capture.archiver.read_payload(result['archive_id']))['transcript'].encode() == raw


def test_fast_append_does_not_decrypt_previous_data_frames(lan, monkeypatch):
    _, adapter = lan
    raw = transcript(text='Do not decode authenticated prefix frames again. ' * 24_000)
    step = 262_144
    assert upload(adapter, raw, end=step)['next_offset'] == step
    capture = adapter.captures['jiangli']
    path = stage_path(capture, raw)
    decrypted_data = []
    original = staging.AESGCM
    class ObservedAES:
        def __init__(self, key):
            self.aes = original(key)
        def encrypt(self, *args):
            return self.aes.encrypt(*args)
        def decrypt(self, nonce, data, aad):
            if aad is not None and aad[-9:-8] == b'D':
                decrypted_data.append(1)
            return self.aes.decrypt(nonce, data, aad)
    monkeypatch.setattr(staging, 'AESGCM', ObservedAES)
    assert upload(adapter, raw, start=step, end=2 * step)['next_offset'] == 2 * step
    assert decrypted_data == []


def test_missing_staging_file_never_rewinds_acknowledged_progress(lan):
    _, adapter = lan
    raw = transcript(text='Keep acknowledged progress even if the staging file is missing. ' * 18_000)
    step = 262_144
    capture = adapter.captures['jiangli']
    assert upload(adapter, raw, end=step)['next_offset'] == step
    stage_path(capture, raw).unlink()
    result = upload(adapter, raw, end=step // 2)
    assert result.get('error') == 'upload_staging_unavailable'
    row = capture.conn.execute('SELECT received_bytes FROM lan_session_uploads WHERE sha256=?', (hashlib.sha256(raw).hexdigest(),)).fetchone()
    assert row['received_bytes'] == step


def test_file_ahead_of_database_recovers_through_full_validation(lan, monkeypatch):
    _, adapter = lan
    raw = transcript(text='File fsync may precede the database receipt commit. ' * 24_000)
    step = 262_144
    capture = adapter.captures['jiangli']
    assert upload(adapter, raw, end=step)['next_offset'] == step
    assert upload(adapter, raw, start=step, end=2 * step)['next_offset'] == 2 * step
    with capture.store.transaction():
        capture.conn.execute('UPDATE lan_session_uploads SET received_bytes=? WHERE sha256=?', (step, hashlib.sha256(raw).hexdigest()))
    validated = []
    original = capture._read_stage_with_prefix
    def observed(*a, **kw):
        validated.append(1)
        return original(*a, **kw)
    monkeypatch.setattr(capture, '_read_stage_with_prefix', observed)
    result = upload(adapter, raw, start=2 * step, end=3 * step)
    assert result['next_offset'] == 3 * step
    assert validated == [1]


def test_fast_append_survives_file_commit_before_receipt_commit(lan, monkeypatch):
    import base64
    from contextlib import contextmanager
    _, adapter = lan
    raw = transcript(text='A receipt failure must not discard the durable next chunk. ' * 22_000)
    step = 262_144
    capture = adapter.captures['jiangli']
    assert upload(adapter, raw, end=step)['next_offset'] == step
    @contextmanager
    def failed_commit(*args, **kwargs):
        raise OSError('synthetic receipt failure')
        yield
    with monkeypatch.context() as patch:
        patch.setattr(capture.store, 'transaction', failed_commit)
        args = {'device_id':'windows-main','session_id':'session-1','project':'demo',
                'sha256':hashlib.sha256(raw).hexdigest(),'total_bytes':len(raw),'offset':step,
                'content_b64':base64.b64encode(raw[step:2 * step]).decode(),'extract':False}
        with pytest.raises(OSError, match='synthetic receipt failure'):
            capture.upload(args)
    assert capture._read_stage(stage_path(capture, raw)) == raw[:2 * step]
    result = upload(adapter, raw, start=step, end=2 * step)
    assert result['next_offset'] == 2 * step
    row = capture.conn.execute('SELECT received_bytes FROM lan_session_uploads WHERE sha256=?', (hashlib.sha256(raw).hexdigest(),)).fetchone()
    assert row['received_bytes'] == 2 * step


def test_fast_append_low_space_keeps_file_and_receipt(lan, monkeypatch):
    from types import SimpleNamespace
    import evolvmem.archive_storage as storage
    _, adapter = lan
    raw = transcript(text='Low disk space must preserve the confirmed staging prefix. ' * 22_000)
    step = 262_144
    capture = adapter.captures['jiangli']
    assert upload(adapter, raw, end=step)['next_offset'] == step
    path = stage_path(capture, raw)
    previous = path.read_bytes()
    reserve = capture.server.config.context_archive_min_free_mb * 1024 * 1024
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda _: SimpleNamespace(free=reserve + 65_536 + 2 * step + 1))
    result = upload(adapter, raw, start=step, end=2 * step)
    assert result.get('error') == 'archive_storage_space_low'
    assert path.read_bytes() == previous
    row = capture.conn.execute('SELECT received_bytes FROM lan_session_uploads WHERE sha256=?', (hashlib.sha256(raw).hexdigest(),)).fetchone()
    assert row['received_bytes'] == step

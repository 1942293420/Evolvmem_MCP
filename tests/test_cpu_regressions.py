"""CPU regressions use only synthetic archives and temporary SQLite databases."""
import pytest

from tests.test_lan_sharing import lan  # noqa: F401
from tests.test_lan_capture import transcript, upload
from evolvmem.auto_organization import _reconcile_candidates


@pytest.mark.parametrize('user', ['jiangli', 'kane'])
def test_archive_intake_does_not_scan_context_health(lan, monkeypatch, user):
    runtime, adapter = lan
    server = runtime.server_for(user)
    evaluations = []
    original = server._evaluate_live_status

    def observed_health():
        evaluations.append(True)
        return original()

    monkeypatch.setattr(server, '_evaluate_live_status', observed_health)
    saved = upload(adapter, transcript(), user=user)
    assert saved['status'] == 'archived'
    own = adapter.call_tool(user, 'session_archive_status',
                           {'device_id': 'windows-main', 'session_id': 'session-1'})
    assert own['archive_id'] == saved['archive_id']
    assert evaluations == [], 'archive storage must not scan unrelated context layers and vectors'


def test_empty_reconcile_does_not_rescan_unrelated_archive_families(lan):
    runtime, adapter = lan
    saved = upload(adapter, transcript())
    service = runtime.server_for('jiangli').context_service
    conn = service.store._connection()
    seed = dict(conn.execute('SELECT * FROM session_archives WHERE id=?',
                             (saved['archive_id'],)).fetchone())
    columns = [key for key in seed if key != 'id']
    insert = 'INSERT INTO session_archives (' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')'
    with service.store.transaction():
        ids = []
        for number in range(800):
            row = dict(seed, external_session_id=f'independent-family-{number}')
            ids.append(conn.execute(insert, tuple(row[key] for key in columns)).lastrowid)
        conn.executemany(
            "INSERT INTO organization_tasks(source_key,source_revision,rule_revision,attempts,created_at,updated_at) VALUES(?,?,?,1,?,?)",
            [(f'archive:{ident}', 'source-v1', 'rule-v1', '2026-10-21 00:00:00', '2026-10-21 00:00:00') for ident in ids[:80]])
        conn.executemany(
            "INSERT INTO lan_session_uploads(device_id,session_id,sha256,project,declared_project,received_at,total_bytes) VALUES(?,?,?,?,?,?,?)",
            [('other-device', f'other-session-{number}', f'{number:064x}', '', '', 0, 1) for number in range(1200)])
    callbacks = []
    conn.set_progress_handler(lambda: callbacks.append(1) or 0, 1000)
    try:
        assert _reconcile_candidates(service, limit=20) == []
    finally:
        conn.set_progress_handler(None, 0)
    steps = len(callbacks) * 1000
    assert steps < 500_000, f'empty reconciliation used {steps} SQLite VM steps for unrelated source families'


def test_chunked_upload_compresses_each_received_byte_once(lan, monkeypatch):
    from evolvmem.lan_capture import LanCapture
    import evolvmem.lan_capture as staging
    runtime, adapter = lan
    raw = transcript(text='Chunked encrypted evidence must survive every restart. ' * 24_000)
    capture = adapter.captures['jiangli']
    compressed_sizes = []
    compress = staging.zlib.compress
    writing = []
    def observed_compress(value, *args, **kwargs):
        if writing:
            compressed_sizes.append(len(value))
        return compress(value, *args, **kwargs)
    monkeypatch.setattr(staging.zlib, 'compress', observed_compress)
    def instrument(capture):
        write = capture._write_stage_parts
        def observed_write(*args, **kwargs):
            writing.append(True)
            try:
                return write(*args, **kwargs)
            finally:
                writing.pop()
        monkeypatch.setattr(capture, '_write_stage_parts', observed_write)
    instrument(capture)
    step = 65_536
    first = upload(adapter, raw, end=step)
    assert first['next_offset'] == step
    assert upload(adapter, raw, end=step)['next_offset'] == step
    # No resident compression state may be required to resume an encrypted file.
    for offset in range(step, len(raw), step):
        if offset % (3 * step) == 0:
            capture = LanCapture(runtime.server_for('jiangli'))
            adapter.captures['jiangli'] = capture
            instrument(capture)
        result = upload(adapter, raw, start=offset, end=min(offset + step, len(raw)))
        assert 'error' not in result, result
    assert result['status'] == 'archived'
    payload = capture.archiver.read_payload(result['archive_id'])
    import json
    assert json.loads(payload)['transcript'].encode() == raw
    assert max(compressed_sizes) <= 262_144
    assert sum(compressed_sizes) == len(raw), 'already committed prefix was recompressed'


@pytest.mark.parametrize('compressed', [False, True])
def test_legacy_staging_resumes_into_chunked_upload(lan, compressed):
    import os
    import evolvmem.lan_capture as staging
    runtime, adapter = lan
    raw = transcript(text='Legacy prefix and immutable evidence. ' * 12_000)
    step = 65_536
    assert upload(adapter, raw, end=step)['next_offset'] == step
    capture = adapter.captures['jiangli']
    import hashlib
    path = capture._stage(('windows-main', 'session-1', hashlib.sha256(raw).hexdigest()))
    nonce = os.urandom(12)
    aad = staging._STAGE_MAGIC if compressed else None
    body = staging.zlib.compress(raw[:step], 3) if compressed else raw[:step]
    encrypted = nonce + staging.AESGCM(capture.archiver._load_or_create_key()).encrypt(nonce, body, aad)
    path.write_bytes((aad or b'') + encrypted)
    for offset in range(step, len(raw), step):
        result = upload(adapter, raw, start=offset, end=min(offset + step, len(raw)))
        assert 'error' not in result, result
    import json
    assert json.loads(capture.archiver.read_payload(result['archive_id']))['transcript'].encode() == raw


def test_staging_atomic_failure_keeps_verified_prefix(lan, monkeypatch):
    import evolvmem.lan_capture as staging
    _, adapter = lan
    capture = adapter.captures['jiangli']
    path = capture._stage(('test', 'session', 'synthetic'))
    prefix = b'Private encrypted staging prefix. ' * 20_000
    capture._write_stage(path, prefix)
    encrypted = path.read_bytes()
    def fail_replace(*args):
        raise OSError('synthetic replace failure')
    monkeypatch.setattr(staging.os, 'replace', fail_replace)
    with pytest.raises(OSError, match='synthetic replace failure'):
        capture._write_stage(path, prefix + b'next chunk')
    assert path.read_bytes() == encrypted
    assert capture._read_stage(path) == prefix
    assert not path.with_suffix('.tmp').exists()


@pytest.mark.parametrize('damage', ['ciphertext', 'truncation'])
def test_staging_rejects_damaged_chunk(lan, damage):
    from evolvmem.lan_sharing import LanError
    _, adapter = lan
    capture = adapter.captures['jiangli']
    path = capture._stage(('test', 'session', 'synthetic'))
    capture._write_stage(path, b'Private encrypted staging prefix. ' * 20_000)
    blob = bytearray(path.read_bytes())
    if damage == 'ciphertext':
        blob[-20] ^= 1
    else:
        del blob[-1]
    path.write_bytes(blob)
    with pytest.raises(LanError, match='upload_staging_unavailable'):
        capture._read_stage(path)


@pytest.mark.parametrize('damage', ['frame_boundary', 'header_only', 'legacy_shorter'])
def test_upload_never_loses_acknowledged_staging_progress(lan, damage):
    import hashlib, os
    import evolvmem.lan_capture as staging
    _, adapter = lan
    raw = transcript(text='Acknowledged encrypted bytes remain durable. ' * 24_000)
    step = 262_144
    assert upload(adapter, raw, end=step)['next_offset'] == step
    assert upload(adapter, raw, start=step, end=2 * step)['next_offset'] == 2 * step
    capture = adapter.captures['jiangli']
    digest = hashlib.sha256(raw).hexdigest()
    path = capture._stage(('windows-main', 'session-1', digest))
    blob = path.read_bytes()
    boundary = len(staging._CHUNKED_STAGE_MAGIC)
    if damage == 'frame_boundary':
        # Find a complete data-frame boundary in either development encoding.
        width = 5 if blob[boundary:boundary + 1] == b'D' else 4
        size = int.from_bytes(blob[boundary + width - 4:boundary + width], 'big')
        path.write_bytes(blob[:boundary + width + size])
    elif damage == 'header_only':
        path.write_bytes(blob[:boundary])
    else:
        nonce = os.urandom(12)
        path.write_bytes(nonce + staging.AESGCM(capture.archiver._load_or_create_key()).encrypt(nonce, raw[:step], None))
    result = upload(adapter, raw, end=step)
    assert result.get('error') == 'upload_staging_unavailable'
    row = capture.conn.execute('SELECT received_bytes FROM lan_session_uploads WHERE sha256=?', (digest,)).fetchone()
    assert row['received_bytes'] == 2 * step


def test_staging_checks_actual_ciphertext_space_before_replace(lan, monkeypatch):
    from types import SimpleNamespace
    import evolvmem.archive_storage as storage
    from evolvmem.lan_sharing import LanError
    _, adapter = lan
    capture = adapter.captures['jiangli']
    path = capture._stage(('test', 'session', 'synthetic'))
    capture._write_stage(path, b'a')
    encrypted = path.read_bytes()
    raw, previous = capture._read_stage_with_prefix(path)
    reserve = capture.server.config.context_archive_min_free_mb * 1024 * 1024
    # Enough for the plaintext estimate, insufficient for the encrypted frame
    # metadata and tags. A failed append must retain the committed prefix.
    free = reserve + 65_536 + 2 * len(raw + b'b') + 1
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda _: SimpleNamespace(free=free))
    with pytest.raises(LanError, match='archive_storage_space_low'):
        capture._write_stage(path, raw + b'b', previous=previous)
    assert path.read_bytes() == encrypted

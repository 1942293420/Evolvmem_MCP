"""Synthetic reproduction + fix verification for the production transcript_fork deadlock.

Diagnostic artefact only. It runs against an isolated tmp_path LanRuntime - never the
live runtime, live DB or live caches - and it does NOT modify any file under
evolvmem/; the proposed fix is injected as replacement functions at runtime so the
real dispatch path (LanTools -> LanCapture.upload) is still exercised.

Run:  .venv/bin/python -m pytest windows/test_transcript_fork_repro.py -q -p no:cacheprovider
"""
import base64
import hashlib
import json

from evolvmem.lan_capture import LanCapture, capture_specs
from evolvmem.lan_sharing import LanError
from tests.test_lan_sharing import lan, settings_for  # noqa: F401  (fixture reuse)

CHUNK = 262144


# --------------------------------------------------------------------------- data
def _rows(session_id, count, filler='x', header_extra=''):
    rows = [{'type': 'session_meta', 'payload': {'id': session_id, 'cwd': r'C:\work\demo',
                                                 'source': 'cli', 'cli_version': '0.20.0'}}]
    if header_extra:
        rows[0]['payload']['rewrite'] = header_extra
    rows += [{'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                                                   'content': [{'type': 'input_text',
                                                                'text': f'line {i} ' + filler * 900}]}}
             for i in range(count)]
    rows.append({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                 'content': [{'type': 'output_text', 'text': 'done'}]}})
    return ('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n').encode()


def _append_rows(raw, count, filler='x'):
    """True append (after the trailing row) - mirrors an append-only rollout."""
    extra = b''.join((json.dumps({'type': 'response_item',
        'payload': {'type': 'message', 'role': 'user',
                    'content': [{'type': 'input_text', 'text': f'extra {i} ' + filler * 900}]}},
        ensure_ascii=False) + '\n').encode() for i in range(count))
    return raw + extra


def _upload(adapter, raw, *, start=0, end=None, session, **extra):
    digest = hashlib.sha256(raw).hexdigest()
    args = dict(device_id='windows-main', session_id=session, project='demo', sha256=digest,
                total_bytes=len(raw), offset=start,
                content_b64=base64.b64encode(raw[start:end]).decode(), extract=False,
                request_id=f"archive-{digest[:12]}-{digest[:24]}-{start}")
    args.update(extra)
    return adapter.call_tool('jiangli', 'session_archive_upload', args)


def _send_chunks(adapter, raw, session, **extra):
    """Send chunks exactly like the Windows client; stop at the first error.

    Returns (receipts, offset_of_last_attempt).
    """
    receipts, offset = [], 0
    while offset < len(raw):
        end = min(offset + CHUNK, len(raw))
        receipt = _upload(adapter, raw, start=offset, end=end, session=session, **extra)
        receipts.append(receipt)
        if 'error' in receipt:
            return receipts, offset
        offset = receipt['next_offset']
    return receipts, offset


# ------------------------------------------------------- proposed fix (not applied)
def fixed_latest(self, identity):
    """ORDER BY receipt time instead of ORDER BY total_bytes DESC."""
    return self.conn.execute('''SELECT * FROM lan_session_uploads
        WHERE device_id=? AND session_id=? AND archive_id IS NOT NULL
        ORDER BY received_at DESC, rowid DESC LIMIT 1''', identity).fetchone()


def fixed_capture_specs():
    """Publish parent_sha256 as an OPTIONAL upload argument (old clients keep working)."""
    specs = capture_specs()
    upload = specs['session_archive_upload']['inputSchema']
    upload['properties']['parent_sha256'] = {'type': 'string', 'maxLength': 64}
    upload['required'] = [k for k in upload['required'] if k != 'parent_sha256']
    return specs


def fixed_upload(self, args):
    """Verbatim copy of LanCapture.upload with the three marked changes."""
    identity = self._identity(args)
    digest = args['sha256']
    if not __import__('re').fullmatch(r'[0-9a-f]{64}', digest):
        raise LanError('invalid_transcript_hash')
    key = (*identity, digest)
    total, offset = args['total_bytes'], args['offset']
    lineage_root = args.get('parent_sha256') == ''      # CHANGE 1: explicit new lineage
    try:
        chunk = base64.b64decode(args['content_b64'], validate=True)
    except (ValueError, TypeError):
        raise LanError('invalid_chunk') from None
    if not chunk or len(chunk) > 262144 or offset + len(chunk) > total:
        raise LanError('invalid_chunk')
    row = self.conn.execute('SELECT * FROM lan_session_uploads WHERE device_id=? AND session_id=? AND sha256=?', key).fetchone()
    if row is not None and (row['total_bytes'] != total or row['declared_project'] != args['project']):
        raise LanError('upload_metadata_conflict')
    path = self._stage(key)
    if row is not None and row['archive_id']:
        payload = self.archiver.read_payload(row['archive_id'])
        if payload is None:
            raise LanError('archive_payload_unavailable')
        raw = json.loads(payload)['transcript'].encode('utf-8')
    elif path.exists():
        raw = self._read_stage(path)
    else:
        raw = b''
    if offset > len(raw) or (offset < len(raw) and raw[offset:offset + len(chunk)] != chunk):
        raise LanError('invalid_chunk')
    if offset == len(raw):
        raw += chunk
    if len(raw) > total:
        raise LanError('invalid_chunk')
    complete = len(raw) == total
    archive_id = row['archive_id'] if row is not None else None
    project = row['project'] if row is not None else args['project']
    details = {}
    reason = row['attribution_reason'] if row is not None else ''
    new_root = False
    if complete:
        if hashlib.sha256(raw).hexdigest() != digest:
            raise LanError('transcript_hash_mismatch')
        rows, _ = (__import__('evolvmem.codex_transcript', fromlist=['x']).parse_transcript(raw, identity[1]))
        details = __import__('evolvmem.codex_transcript', fromlist=['x']).workspace_details(rows)
        reason = details['attribution_reason']
        if reason and not archive_id:
            project = ''
        latest = self._latest(identity)
        if latest is not None and latest['sha256'] != digest:
            previous = self.archiver.read_payload(latest['archive_id'])
            if previous is None:
                prior_length = int(latest['total_bytes'])
                if len(raw) < prior_length:
                    raise LanError('previous_archive_unavailable')
                if hashlib.sha256(raw[:prior_length]).hexdigest() != latest['sha256']:
                    raise LanError('transcript_fork')
                prior = None
            else:
                prior = json.loads(previous)['transcript'].encode('utf-8')
            if prior is not None and prior.startswith(raw):
                path.unlink(missing_ok=True)
                return self._stale_receipt(latest, submitted_sha256=digest, submitted_total_bytes=total)
            if prior is not None and not raw.startswith(prior):
                if not lineage_root:                       # CHANGE 2: honour the declaration
                    raise LanError('transcript_fork')
                new_root = True
    if not archive_id:
        self._write_stage(path, raw)
    if complete and not archive_id:
        payload = json.dumps({'source': 'client_reported', 'device_id': identity[0],
                              'session_id': identity[1], 'source_sha256': digest,
                              'parent_session_id': details.get('parent_session_id', ''),
                              'transcript': raw.decode('utf-8')}, ensure_ascii=False)
        external = hashlib.sha256(json.dumps(identity).encode()).hexdigest() + ':' + digest
        archive = self.archiver.archive_session(project, 'codex', external, payload)
        if archive is None:
            raise LanError('archive_unavailable')
        archive_id = archive.id
    extraction = row['extraction_status'] if row is not None else 'not_requested'
    if args['extract'] and extraction == 'not_requested':
        extraction = 'pending' if project else 'unassigned'
    if complete and not project and extraction == 'pending':
        extraction = 'unassigned'
    with self.store.transaction():
        self.conn.execute('''INSERT INTO lan_session_uploads
            (device_id,session_id,sha256,project,declared_project,attribution_reason,received_at,
             total_bytes,received_bytes,archive_id,extraction_status)
            VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(device_id,session_id,sha256) DO UPDATE SET
            received_bytes=excluded.received_bytes,archive_id=excluded.archive_id,
            project=excluded.project,attribution_reason=excluded.attribution_reason,
            received_at=CASE WHEN lan_session_uploads.archive_id IS NULL THEN excluded.received_at ELSE lan_session_uploads.received_at END,
            extraction_status=excluded.extraction_status''',
            (*key, project, args['project'], reason, __import__('time').time(), total, len(raw), archive_id, extraction))
        if complete:
            self.conn.execute("""UPDATE lan_session_uploads SET extraction_status='superseded'
                WHERE device_id=? AND session_id=? AND total_bytes<? AND extraction_status='pending'""",
                (*identity, total))
    if complete:
        path.unlink(missing_ok=True)
    receipt = self._receipt(self.conn.execute(
        'SELECT * FROM lan_session_uploads WHERE device_id=? AND session_id=? AND sha256=?', key).fetchone())
    if new_root:                                           # CHANGE 3: make it visible
        receipt['lineage'] = 'new_root'
    return receipt


def _apply_fix(monkeypatch):
    import evolvmem.lan_capture as lc
    import evolvmem.lan_tools as lt
    monkeypatch.setattr(lc.LanCapture, 'upload', fixed_upload)
    monkeypatch.setattr(lc.LanCapture, '_latest', fixed_latest)
    monkeypatch.setattr(lt, 'capture_specs', fixed_capture_specs)


# --------------------------------------------------------------------------- tests
def test_repro_rewritten_version_deadlocks_on_final_chunk(lan):  # noqa: F811
    """Production shape: only the FINAL chunk is rejected, and it stays rejected."""
    _, adapter = lan
    v1 = _rows('s1', 300)
    rewrite = _rows('s1', 301, header_extra=1)   # Codex rewrote the rollout header
    assert rewrite != v1 and len(rewrite) > len(v1)

    first, _ = _send_chunks(adapter, v1, 's1')
    assert first[-1]['status'] == 'archived' and first[-1]['archive_id'] is not None

    receipts, last_offset = _send_chunks(adapter, rewrite, 's1')
    assert len(receipts) > 1, 'synthetic version must span more than one chunk'
    assert [r['status'] for r in receipts[:-1]] == ['receiving'] * (len(receipts) - 1)
    final = receipts[-1]
    assert final['error'] == 'transcript_fork', final
    assert last_offset == (len(receipts) - 1) * CHUNK

    # Identical replay (same offset, same request_id) fails forever -> permanent stall.
    replay = _upload(adapter, rewrite, start=last_offset, session='s1')
    assert replay['error'] == 'transcript_fork'
    # status keeps reporting the OLD version; the stuck version has no row at all.
    status = adapter.call_tool('jiangli', 'session_archive_status',
                               {'device_id': 'windows-main', 'session_id': 's1'})
    assert status['status'] == 'archived'
    assert status['archive_id'] == first[-1]['archive_id']
    assert status['source_sha256'] == hashlib.sha256(v1).hexdigest()


def test_largest_is_not_newest_so_a_smaller_new_lineage_can_never_win(lan):  # noqa: F811
    _, adapter = lan
    big = _rows('s2', 300, filler='y')
    small_rewrite = _rows('s2', 50, filler='y')
    assert _send_chunks(adapter, big, 's2')[0][-1]['status'] == 'archived'
    capture = adapter.captures['jiangli']
    assert LanCapture._latest(capture, ('windows-main', 's2'))['total_bytes'] == len(big)
    assert _send_chunks(adapter, small_rewrite, 's2')[0][-1]['error'] == 'transcript_fork'


def test_current_code_rejects_an_undeclared_extra_argument(lan):  # noqa: F811
    _, adapter = lan
    raw = _rows('s3', 3)
    assert _upload(adapter, raw, session='s3', parent_sha256='')['error'] == 'invalid_arguments'


def test_proposed_fix_accepts_declared_root_and_keeps_the_old_archive(lan, monkeypatch):  # noqa: F811
    _apply_fix(monkeypatch)
    runtime, adapter = lan
    v1 = _rows('s5', 300, filler='z')
    v2 = _rows('s5', 301, filler='z', header_extra=1)   # divergent + larger
    v3 = _append_rows(v2, 1, 'z')                       # true append on the NEW lineage
    assert len(v3) > len(v2) > len(v1) and v2 != v1

    assert _send_chunks(adapter, v1, 's5')[0][-1]['status'] == 'archived'
    receipts, _ = _send_chunks(adapter, v2, 's5', parent_sha256='')
    assert receipts[-1]['status'] == 'archived', receipts[-1]
    assert receipts[-1]['lineage'] == 'new_root'
    # An append on the new lineage compares against the NEW root, not the biggest one.
    assert _send_chunks(adapter, v3, 's5')[0][-1]['status'] == 'archived'

    # The guard still rejects an UNDECLARED divergent version.
    assert _send_chunks(adapter, _rows('s6', 300, filler='w'), 's6')[0][-1]['status'] == 'archived'
    assert _send_chunks(adapter, _rows('s6', 301, filler='w', header_extra=1),
                        's6')[0][-1]['error'] == 'transcript_fork'

    store = runtime.server_for('jiangli').context_service.store._connection()
    assert store.execute("SELECT COUNT(*) FROM session_archives WHERE project='demo'").fetchone()[0] == 4
    shas = {r[0] for r in store.execute(
        "SELECT sha256 FROM lan_session_uploads WHERE session_id='s5' AND archive_id IS NOT NULL")}
    assert shas == {hashlib.sha256(v1).hexdigest(), hashlib.sha256(v2).hexdigest(),
                    hashlib.sha256(v3).hexdigest()}

"""Synthetic reproduction of the production transcript_fork deadlock, on the adopted protocol.

Diagnostic artefact only. It runs against an isolated tmp_path LanRuntime - never the
live runtime, live DB or live caches - and it never modifies evolvmem/ or monkeypatches
a candidate upload implementation: every case goes through the real dispatch path
(LanTools.call_tool -> LanCapture.upload).

History: the first version of this file reproduced the stall and then injected a
*proposed* fix (an optional ``parent_sha256`` lineage-root argument plus a
``LanCapture._latest`` ordering change). That candidate was never adopted, ``_latest``
no longer exists, and the field is not part of the upload schema. The adopted protocol
sends two OPTIONAL fields instead - ``source_order`` (fixed client-side capture order)
and ``current_sha256`` (this upload is the client's own current snapshot, only valid
together with an order) - and keeps the current snapshot in ``lan_session_heads``.
The cases below keep the valuable shapes from the original repro (multi-chunk uploads,
the final-chunk stall, a smaller rewrite, a normal append) expressed on that protocol.

Run:  .venv/bin/python -m pytest windows/test_transcript_fork_repro.py -q -p no:cacheprovider
"""
import base64
import hashlib
import json

from evolvmem.lan_capture import capture_specs
from evolvmem.session_archive import SessionArchiver
from tests.test_lan_sharing import lan  # noqa: F401  (pytest fixture reuse)

CHUNK = 262144


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


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
    digest = sha(raw)
    args = dict(device_id='windows-main', session_id=session, project='demo', sha256=digest,
                total_bytes=len(raw), offset=start,
                content_b64=base64.b64encode(raw[start:end]).decode(), extract=False,
                request_id=f"archive-{digest[:12]}-{digest[:24]}-{start}")
    args.update(extra)
    return adapter.call_tool('jiangli', 'session_archive_upload', args)


def _send_chunks(adapter, raw, session, *, order=None, current=False, **extra):
    """Send chunks like the Windows client; stop at the first error.

    The deployed client attaches ``source_order`` to every chunk of a version and
    ``current_sha256`` only when the locally persisted current snapshot is this
    exact sha, so the intermediate chunks of a claimed-current version carry both.

    Returns (receipts, offset_of_last_attempt).
    """
    receipts, offset = [], 0
    while offset < len(raw):
        end = min(offset + CHUNK, len(raw))
        arguments = dict(extra)
        if order is not None:
            arguments['source_order'] = order
        if current:
            arguments['current_sha256'] = sha(raw)
        receipt = _upload(adapter, raw, start=offset, end=end, session=session, **arguments)
        receipts.append(receipt)
        if 'error' in receipt:
            return receipts, offset
        offset = receipt['next_offset']
    return receipts, offset


def _server(runtime, user='jiangli'):
    return runtime.server_for(user)


def _store(runtime, user='jiangli'):
    return _server(runtime, user).context_service.store


def _archiver(runtime, user='jiangli'):
    server = _server(runtime, user)
    return SessionArchiver(server.config, server.context_service.store)


def _status(adapter, session):
    return adapter.call_tool('jiangli', 'session_archive_status',
                             {'device_id': 'windows-main', 'session_id': session})


# --------------------------------------------------------------------------- tests
def test_repro_rewritten_version_deadlocks_on_final_chunk(lan):  # noqa: F811
    """Legacy shape, unchanged for order-less clients: only the FINAL chunk is rejected.

    A client that never declares source_order/current_sha256 keeps the original
    append/stale/fork semantics, so an undeclared rewrite still stalls forever.
    """
    runtime, adapter = lan
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
    # The rejected rewrite never produced an archive of its own.
    stuck = _store(runtime)._connection().execute(
        'SELECT archive_id FROM lan_session_uploads WHERE device_id=? AND session_id=? AND sha256=?',
        ('windows-main', 's1', sha(rewrite))).fetchone()
    assert stuck is None or stuck['archive_id'] is None
    # status keeps reporting the OLD version; the stuck version has no archive.
    status = _status(adapter, 's1')
    assert status['status'] == 'archived'
    assert status['archive_id'] == first[-1]['archive_id']
    assert status['source_sha256'] == sha(v1)


def test_declared_smaller_rewrite_becomes_current_across_chunks(lan):  # noqa: F811
    """The retired size rule must not matter: a smaller declared current wins.

    ``LanCapture._latest`` used to order by ``total_bytes DESC``, so the largest
    version was mistaken for the newest and a smaller rewrite could never win.
    With ``source_order``/``current_sha256`` any verified version is an immutable
    history archive and only the newest declared current moves the head.
    """
    runtime, adapter = lan
    big = _rows('s2', 600, filler='y')
    small_rewrite = _rows('s2', 300, filler='y')
    assert len(small_rewrite) < len(big)

    first, _ = _send_chunks(adapter, big, 's2', order=1, current=True)
    assert len(first) > 1, 'the archived current must span more than one chunk'
    assert first[-1]['status'] == 'archived'
    assert first[-1]['current'] is True

    receipts, _ = _send_chunks(adapter, small_rewrite, 's2', order=2, current=True)
    assert len(receipts) > 1, 'the smaller rewrite must also span more than one chunk'
    assert [r['status'] for r in receipts[:-1]] == ['receiving'] * (len(receipts) - 1)
    final = receipts[-1]
    assert final['status'] == 'archived', final
    assert final['current'] is True
    assert final['source_sha256'] == sha(small_rewrite)
    assert final['archive_id'] != first[-1]['archive_id']

    status = _status(adapter, 's2')
    assert status['source_sha256'] == sha(small_rewrite)
    assert status['archive_id'] == final['archive_id']
    # The larger history version keeps its own immutable payload.
    assert json.loads(_archiver(runtime).read_payload(first[-1]['archive_id']))['transcript'] == big.decode()


def test_chunked_append_on_the_declared_current_is_not_a_fork(lan):  # noqa: F811
    """A true append after a declared rewrite keeps advancing the current snapshot."""
    runtime, adapter = lan
    v1 = _rows('s3', 300, filler='q')
    v2 = _rows('s3', 301, filler='q', header_extra=1)   # divergent rewrite
    v3 = _append_rows(v2, 2, 'q')                       # true append on the new current
    assert len(v3) > len(v2) > len(v1) and v2 != v1

    assert _send_chunks(adapter, v1, 's3', order=1, current=True)[0][-1]['status'] == 'archived'
    assert _send_chunks(adapter, v2, 's3', order=2, current=True)[0][-1]['status'] == 'archived'

    receipts, _ = _send_chunks(adapter, v3, 's3', order=3, current=True)
    assert len(receipts) > 1, 'the append must span more than one chunk'
    assert receipts[-1]['status'] == 'archived', receipts[-1]
    assert receipts[-1]['current'] is True
    assert receipts[-1]['source_sha256'] == sha(v3)
    assert 'error' not in receipts[-1]

    status = _status(adapter, 's3')
    assert status['source_sha256'] == sha(v3)
    assert status['next_offset'] == len(v3)
    # Re-sending the same append is idempotent: same archive, no duplicate version.
    again = _send_chunks(adapter, v3, 's3', order=3, current=True)[0][-1]
    assert again['archive_id'] == receipts[-1]['archive_id']
    shas = {row[0] for row in _store(runtime)._connection().execute(
        "SELECT sha256 FROM lan_session_uploads WHERE session_id='s3' AND archive_id IS NOT NULL")}
    assert shas == {sha(v1), sha(v2), sha(v3)}
    assert _store(runtime)._connection().execute(
        "SELECT COUNT(*) FROM session_archives WHERE project='demo'").fetchone()[0] == 3


def test_upload_spec_declares_the_adopted_fields_and_forbids_extras():
    """The adopted protocol is read from the public ``capture_specs`` declaration only.

    No request is ever built with the retired candidate field: this contract checks
    the declared upload property set and the ``additionalProperties: False``
    declaration that makes any undeclared field (the retired one included) invalid.
    """
    upload = capture_specs()['session_archive_upload']['inputSchema']
    assert upload['additionalProperties'] is False
    assert set(upload['properties']) == {
        'device_id', 'session_id', 'project', 'sha256', 'total_bytes', 'offset',
        'content_b64', 'extract', 'request_id', 'source_order', 'current_sha256'}
    assert set(upload['required']) == {'device_id', 'session_id', 'project', 'sha256',
                                       'total_bytes', 'offset', 'content_b64', 'extract',
                                       'request_id'}

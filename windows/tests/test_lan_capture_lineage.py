"""Regression tests for rewritten Codex rollouts under the adopted version protocol.

History: this file originally required ``windows/patch-1-server-lineage-root.patch``,
a candidate that added an optional ``parent_sha256`` lineage-root argument and changed
``LanCapture._latest`` ordering. That candidate was never adopted and is now retired
(see windows-report.md). Production instead publishes two OPTIONAL upload fields -
``source_order`` and ``current_sha256`` - and keeps the current snapshot in
``lan_session_heads``; ``LanCapture._latest`` no longer exists. Every case below goes
through the real dispatch path (LanTools.call_tool -> LanCapture.upload) and no
candidate implementation is monkeypatched.

Run:  .venv/bin/python -m pytest windows/tests/test_lan_capture_lineage.py -q -p no:cacheprovider
"""
import base64
import hashlib
import json

from evolvmem.session_archive import SessionArchiver
from tests.test_lan_sharing import lan  # noqa: F401  (fixture reuse)

CHUNK = 262144
IDENTITY = {'device_id': 'windows-main'}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _rows(session_id, count, filler='x', header_extra=False):
    header = {'id': session_id, 'cwd': r'C:\work\demo', 'source': 'cli'}
    if header_extra:
        header['cli_version'] = '0.20.0'
    rows = [{'type': 'session_meta', 'payload': header}]
    rows += [{'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                                                   'content': [{'type': 'input_text',
                                                                'text': f'line {i} ' + filler * 900}]}}
             for i in range(count)]
    rows.append({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                 'content': [{'type': 'output_text', 'text': 'done'}]}})
    return ('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows) + '\n').encode()


def _append(raw, count, filler='x'):
    extra = b''.join((json.dumps({'type': 'response_item',
        'payload': {'type': 'message', 'role': 'user',
                    'content': [{'type': 'input_text', 'text': f'extra {i} ' + filler * 900}]}},
        ensure_ascii=False) + '\n').encode() for i in range(count))
    return raw + extra


def _upload(adapter, raw, *, session, start=0, end=None, **extra):
    digest = sha(raw)
    args = dict(device_id='windows-main', session_id=session, project='demo', sha256=digest,
                total_bytes=len(raw), offset=start,
                content_b64=base64.b64encode(raw[start:end]).decode(), extract=False,
                request_id=f'archive-{digest[:12]}-{digest[:24]}-{start}')
    args.update(extra)
    return adapter.call_tool('jiangli', 'session_archive_upload', args)


def _send(adapter, raw, session, *, order=None, current=False, extract=False):
    """Upload every chunk of one version; ``current=True`` declares it the client's present.

    Deployed clients send source_order on each chunk and current_sha256 only when the
    locally persisted current snapshot is this exact sha.
    """
    receipts, offset = [], 0
    while offset < len(raw):
        arguments = {'extract': extract}
        if order is not None:
            arguments['source_order'] = order
        if current:
            arguments['current_sha256'] = sha(raw)
        receipt = _upload(adapter, raw, session=session, start=offset,
                          end=min(offset + CHUNK, len(raw)), **arguments)
        receipts.append(receipt)
        if 'error' in receipt:
            break
        offset = receipt['next_offset']
    return receipts


def _store(runtime):
    return runtime.server_for('jiangli').context_service.store


def _archiver(runtime):
    server = runtime.server_for('jiangli')
    return SessionArchiver(server.config, server.context_service.store)


def _status(adapter, session):
    return adapter.call_tool('jiangli', 'session_archive_status', {**IDENTITY, 'session_id': session})


def _head(runtime, session):
    row = _store(runtime)._connection().execute(
        'SELECT sha256, source_order FROM lan_session_heads WHERE device_id=? AND session_id=?',
        ('windows-main', session)).fetchone()
    return tuple(row) if row is not None else None


def test_declared_rewrite_is_archived_and_the_prior_archive_survives(lan):  # noqa: F811
    runtime, adapter = lan
    v1 = _rows('fork-2', 300, header_extra=False)
    v2 = _rows('fork-2', 301, header_extra=True)   # rewritten header, longer

    first = _send(adapter, v1, 'fork-2', order=1, current=True)[-1]
    assert first['status'] == 'archived', first
    second = _send(adapter, v2, 'fork-2', order=2, current=True)[-1]

    assert second['status'] == 'archived', second
    assert second['current'] is True
    assert second['source_sha256'] == sha(v2)
    assert second['archive_id'] and second['archive_id'] != first['archive_id']
    assert _head(runtime, 'fork-2') == (sha(v2), 2)
    # The replaced version stays a complete immutable archive with its own payload.
    assert _store(runtime).get_session_archive(first['archive_id']) is not None
    assert json.loads(_archiver(runtime).read_payload(first['archive_id']))['transcript'] == v1.decode()
    # One immutable archive per distinct version; the rewrite is not a duplicate.
    assert _store(runtime)._connection().execute(
        "SELECT COUNT(*) FROM session_archives WHERE project='demo'").fetchone()[0] == 2
    status = _status(adapter, 'fork-2')
    assert status['archive_id'] == second['archive_id']
    assert status['source_sha256'] == sha(v2)


def test_late_lower_order_rewrite_never_covers_the_current(lan):  # noqa: F811
    runtime, adapter = lan
    old_large = _rows('fork-3', 300, filler='q')
    new_small = _rows('fork-3', 40, filler='q', header_extra=True)
    assert len(new_small) < len(old_large)

    first = _send(adapter, old_large, 'fork-3', order=5, current=True)[-1]
    latest = _send(adapter, new_small, 'fork-3', order=8, current=True)[-1]
    assert first['current'] is True
    assert latest['current'] is True and latest['archive_id'] != first['archive_id']

    # A delayed retry of the older version is archived as history, never as current.
    delayed = _send(adapter, old_large, 'fork-3', order=5, current=True)[-1]

    assert delayed['status'] == 'archived', delayed
    assert delayed['current'] is False
    assert delayed['source_order'] == 5
    assert delayed['archive_id'] == first['archive_id']
    assert delayed['extraction_status'] == 'not_requested'
    assert _head(runtime, 'fork-3') == (sha(new_small), 8)
    status = _status(adapter, 'fork-3')
    assert status['archive_id'] == latest['archive_id']
    assert status['source_sha256'] == sha(new_small)
    assert json.loads(_archiver(runtime).read_payload(first['archive_id']))['transcript'] == old_large.decode()


def test_smaller_rewrite_becomes_current_and_is_claimable(lan):  # noqa: F811
    """The size inversion that the retired ``ORDER BY total_bytes DESC`` could not handle."""
    runtime, adapter = lan
    big = _rows('fork-5', 300, filler='m')
    small = _rows('fork-5', 25, filler='m', header_extra=True)
    assert len(small) < len(big)

    first = _send(adapter, big, 'fork-5', order=3, current=True, extract=True)[-1]
    assert first['status'] == 'archived' and first['archive_id']

    saved = _send(adapter, small, 'fork-5', order=4, current=True, extract=True)[-1]

    assert saved['status'] == 'archived', saved
    assert saved['current'] is True
    assert saved['extraction_status'] == 'pending'
    assert saved['archive_id'] != first['archive_id']
    # The larger history version must not block the new current from being claimed.
    claimed = adapter.captures['jiangli'].claim_pending()
    assert claimed is not None
    assert claimed['sha256'] == sha(small)
    assert claimed['archive_id'] == saved['archive_id']
    big_row = _store(runtime)._connection().execute(
        'SELECT extraction_status FROM lan_session_uploads WHERE sha256=?', (sha(big),)).fetchone()
    assert big_row['extraction_status'] == 'superseded'
    assert _status(adapter, 'fork-5')['source_sha256'] == sha(small)


def test_version_metadata_stays_optional_for_deployed_clients(lan):  # noqa: F811
    runtime, adapter = lan
    schema = adapter._specs('jiangli')['session_archive_upload']['inputSchema']
    assert schema['properties']['source_order'] == {'type': 'integer', 'minimum': 1}
    assert schema['properties']['current_sha256'] == {'type': 'string', 'minLength': 64, 'maxLength': 64}
    assert 'source_order' not in schema['required']
    assert 'current_sha256' not in schema['required']
    # The retired candidate field is gone, and the required set is the deployed one.
    assert 'parent_sha256' not in schema['properties']
    assert set(schema['required']) == {'device_id', 'session_id', 'project', 'sha256',
                                       'total_bytes', 'offset', 'content_b64', 'extract',
                                       'request_id'}

    # An already deployed order-less client keeps the original append/stale/fork path.
    raw = _rows('fork-6', 5)
    legacy = _send(adapter, raw, 'fork-6')[-1]
    assert legacy['status'] == 'archived', legacy
    assert legacy['source_order'] is None
    assert legacy['current'] is True
    assert _head(runtime, 'fork-6') is None
    assert _status(adapter, 'fork-6')['archive_id'] == legacy['archive_id']

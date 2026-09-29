"""Regression tests for rewritten Codex rollouts (transcript_fork deadlock).

Drop-in location in the repository: tests/test_lan_capture_lineage.py
Requires windows/patch-1-server-lineage-root.patch (declared lineage root) applied.
"""
import base64
import hashlib
import json

from tests.test_lan_sharing import lan  # noqa: F401  (fixture reuse)

CHUNK = 262144


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
    digest = hashlib.sha256(raw).hexdigest()
    args = dict(device_id='windows-main', session_id=session, project='demo', sha256=digest,
                total_bytes=len(raw), offset=start,
                content_b64=base64.b64encode(raw[start:end]).decode(), extract=False,
                request_id=f'archive-{digest[:12]}-{digest[:24]}-{start}')
    args.update(extra)
    return adapter.call_tool('jiangli', 'session_archive_upload', args)


def _send(adapter, raw, session, **extra):
    receipts, offset = [], 0
    while offset < len(raw):
        receipt = _upload(adapter, raw, session=session, start=offset,
                          end=min(offset + CHUNK, len(raw)), **extra)
        receipts.append(receipt)
        if 'error' in receipt:
            break
        offset = receipt['next_offset']
    return receipts


def test_rewritten_rollout_without_declaration_is_still_refused(lan):  # noqa: F811
    _, adapter = lan
    v1 = _rows('fork-1', 300)
    v2 = _rows('fork-1', 301, header_extra=True)
    assert _send(adapter, v1, 'fork-1')[-1]['status'] == 'archived'
    assert _send(adapter, v2, 'fork-1')[-1]['error'] == 'transcript_fork'
    # The guard fires only on the final chunk, which is exactly the production stall.
    assert _send(adapter, v2, 'fork-1')[-1]['error'] == 'transcript_fork'


def test_declared_lineage_root_is_archived_and_prior_archive_survives(lan):  # noqa: F811
    runtime, adapter = lan
    v1 = _rows('fork-2', 300, header_extra=False)
    v2 = _rows('fork-2', 301, header_extra=True)
    first = _send(adapter, v1, 'fork-2')[-1]
    assert first['status'] == 'archived'
    second = _send(adapter, v2, 'fork-2', parent_sha256='')[-1]
    assert second['status'] == 'archived', second
    assert second['lineage'] == 'new_root'
    assert second['source_sha256'] == hashlib.sha256(v2).hexdigest()
    store = runtime.server_for('jiangli').context_service.store
    assert store.get_session_archive(first['archive_id']) is not None
    status = adapter.call_tool('jiangli', 'session_archive_status',
                               {'device_id': 'windows-main', 'session_id': 'fork-2'})
    assert status['archive_id'] == second['archive_id']


def test_append_after_a_lineage_root_uses_the_new_root(lan):  # noqa: F811
    _, adapter = lan
    v1 = _rows('fork-3', 20, filler='q')           # small, one chunk
    v2 = _rows('fork-3', 300, filler='q')          # rewritten and much larger
    assert _send(adapter, v1, 'fork-3')[-1]['status'] == 'archived'
    assert _send(adapter, v2, 'fork-3', parent_sha256='')[-1]['status'] == 'archived'
    v3 = _append(v2, 1, 'q')
    # _latest must track the newest archive, not the biggest one.
    assert _send(adapter, v3, 'fork-3')[-1]['status'] == 'archived'


def test_parent_sha256_is_optional_for_deployed_clients(lan):  # noqa: F811
    _, adapter = lan
    specs = adapter._specs('jiangli')
    schema = specs['session_archive_upload']['inputSchema']
    assert 'parent_sha256' in schema['properties']
    assert 'parent_sha256' not in schema['required']
    assert set(schema['required']) == {'device_id', 'session_id', 'project', 'sha256',
                                       'total_bytes', 'offset', 'content_b64', 'extract',
                                       'request_id'}


def test_declared_root_that_is_actually_an_append_still_archives_once(lan):  # noqa: F811
    runtime, adapter = lan
    v1 = _rows('fork-4', 300, filler='k')
    v2 = _append(v1, 1, 'k')
    assert _send(adapter, v1, 'fork-4')[-1]['status'] == 'archived'
    receipt = _send(adapter, v2, 'fork-4', parent_sha256='')[-1]
    assert receipt['status'] == 'archived'
    assert 'lineage' not in receipt  # a real extension is not marked as a new root
    store = runtime.server_for('jiangli').context_service.store._connection()
    assert store.execute("SELECT COUNT(*) FROM session_archives WHERE project='demo'").fetchone()[0] == 2

import hashlib
import os
import json
import tempfile
import unittest
from pathlib import Path

import importlib.util
import sys
if os.name != "nt":
    raise unittest.SkipTest("Windows DPAPI is required for DSH archiver tests")
try:
    import zstandard
except ImportError:
    raise unittest.SkipTest("Install zstandard to test the Windows DSH archiver")
script = Path(__file__).parents[2] / 'scripts/windows/dsh_archive.py'
spec = importlib.util.spec_from_file_location('dsh_archive', script)
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)


def native(sid='session-test', count=2, seeded=False):
    rows = [dict(type='session', version=4, id=sid, createdAt=1,
                 isSeeded=seeded, delegationDepth=0, cwd='C:\\work')]
    rows += [dict(type='user/message', seq=i, time=i,
                  data=dict(role='user', source=dict(kind='user'),
                            content=[dict(type='text', text='hello\u0085world')])) for i in range(count)]
    return ('\n'.join(json.dumps(r, ensure_ascii=False) for r in rows)+'\n').encode()


class Server:
    def __init__(self):
        self.parts = {}; self.requests = []; self.fail_after = None; self.bad_sha = False
    def call(self, name, args, request_id=None):
        if name == 'memory_status': return {'authenticated_user': 'test'}
        self.requests.append((dict(args), request_id))
        if self.fail_after is not None and len(self.requests) > self.fail_after:
            raise app.ArchiveError('transport_unavailable')
        key = args['sha256']; buf=self.parts.setdefault(key, bytearray())
        chunk=app.base64.b64decode(args['content_b64']); offset=args['offset']
        if offset == len(buf): buf.extend(chunk)
        elif bytes(buf[offset:offset+len(chunk)]) != chunk: raise AssertionError('non-idempotent retry')
        done=len(buf)==args['total_bytes']
        return dict(authenticated_user='test', status='archived' if done else 'uploading',
                    next_offset=len(buf), source_sha256='0'*64 if self.bad_sha else key,
                    source_format=app.FORMAT, source_order=args['source_order'], current=True,
                    archive_id=123 if done else None, total_bytes=args['total_bytes'], extraction_status='not_requested')


class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.source=self.root/'source';self.source.mkdir()
        self.path=self.source/'session-test'/'session.v4.jsonl.zstd';self.path.parent.mkdir()
        self.state=self.root/'state';self.server=Server()
    def tearDown(self):self.tmp.cleanup()
    def write(self, raw):self.path.write_bytes(zstandard.ZstdCompressor().compress(raw))
    def worker(self):return app.Collector(self.source, self.state, 'win-test', self.server,
                                         chunk_bytes=120, max_pending_per_session=2)
    def test_native_unicode_and_exact_bytes(self):
        raw=native();self.write(raw);w=self.worker();w.scan();w.upload(100,60)
        self.assertEqual(bytes(self.server.parts[hashlib.sha256(raw).hexdigest()]),raw)
        self.assertTrue(all(not a['extract'] and a['project']=='' and a['source_format']==app.FORMAT
                            for a,_ in self.server.requests))
        self.assertEqual(w.summary()['pending'],0)
        self.assertEqual(w.summary()['confirmed_sessions'],1)
    def test_resume_after_transport_failure_and_restart(self):
        raw=native(count=5);self.write(raw);w=self.worker();w.scan();self.server.fail_after=2
        w.upload(100,60);self.assertEqual(w.summary()['pending'],1)
        self.server.fail_after=None;w=self.worker();w.upload(100,60,ignore_backoff=True)
        self.assertEqual(w.summary()['pending'],0)
        self.assertEqual(bytes(self.server.parts[hashlib.sha256(raw).hexdigest()]),raw)
    def test_ack_mismatch_keeps_encrypted_payload(self):
        self.write(native());w=self.worker();w.scan();self.server.bad_sha=True;w.upload(100,60)
        self.assertEqual(w.summary()['pending'],1)
        self.assertEqual(w.summary()['confirmed_sessions'],0)
        blobs=list((self.state/'queue').glob('*.dpapi'))
        self.assertEqual(len(blobs),1);self.assertNotIn(b'hello',blobs[0].read_bytes())
    def test_duplicate_scan_is_noop_and_queue_is_bounded(self):
        self.write(native());w=self.worker();w.scan();w.scan();self.assertEqual(w.summary()['pending'],1)
        self.write(native(count=3));w.scan();self.write(native(count=4));w.scan()
        self.assertEqual(w.summary()['pending'],2)
        self.assertEqual(w.summary()['deferred'],1)
        w.upload(100,60);w.scan();w.upload(100,60);w.upload(100,60)
        self.assertEqual(w.summary()['pending'],0)
        self.assertEqual(w.summary()['deferred'],0)
    def test_seeded_and_legacy_are_visible_not_mutated(self):
        raw=native(seeded=True);self.write(raw);w=self.worker();w.scan()
        self.assertEqual(w.summary()['pending'],0)
        self.assertEqual(w.summary()['unsupported'],1)
        self.assertEqual(app.decode_source(self.path),raw)
    def test_partial_zstd_and_discontinuous_seq_rejected(self):
        self.write(native());self.path.write_bytes(self.path.read_bytes()[:-2]);w=self.worker();w.scan()
        self.assertEqual(w.summary()['pending'],0)
        with self.assertRaises(app.ArchiveError):app.validate_native(native().replace(b'"seq": 1',b'"seq": 9'))
    def test_receipt_recovery_after_commit_before_manifest_removal(self):
        self.write(native());w=self.worker();w.scan();m=list((self.state/'queue').glob('*.json'))[0]
        saved=m.read_bytes();blob=m.with_suffix('.dpapi').read_bytes();w.upload(100,60)
        m.write_bytes(saved);m.with_suffix('.dpapi').write_bytes(blob)
        w=self.worker();w.upload(100,60)
        self.assertEqual(w.summary()['pending'],0)
        self.assertEqual(w.summary()['confirmed_sessions'],1)
    def test_new_snapshot_changes_retry_key_when_current_declaration_changes(self):
        self.write(native());w=self.worker();w.scan();self.server.fail_after=0;w.upload(100,60)
        first_args,first_id=self.server.requests[0]
        self.write(native(count=4));w.scan();self.server.fail_after=None
        w.upload(100,60,ignore_backoff=True)
        retry_args,retry_id=self.server.requests[1]
        self.assertEqual(first_args['sha256'],retry_args['sha256'])
        self.assertNotEqual(first_id,retry_id)
        self.assertIn('current_sha256',first_args);self.assertNotIn('current_sha256',retry_args)
    def test_newer_shorter_snapshot_cannot_overtake_pending_predecessor(self):
        self.write(native(count=5));w=self.worker();w.scan();first=native(count=5)
        self.write(native(count=1));w.scan();w.upload(1,60)
        self.assertEqual(self.server.requests[0][0]['sha256'],hashlib.sha256(first).hexdigest())
    def test_revisited_same_sha_has_new_order_and_confirmation(self):
        self.write(native(count=1));w=self.worker();w.scan();w.upload(100,60)
        first_order=self.server.requests[-1][0]['source_order']
        self.write(native(count=2));w.scan();w.upload(100,60)
        self.write(native(count=1));w.scan();w.upload(100,60)
        self.assertGreater(self.server.requests[-1][0]['source_order'],first_order)
        self.assertEqual(w.summary()['confirmed_sessions'],1)
        self.assertEqual(w.summary()['pending'],0)
    def test_missing_source_root_cannot_report_healthy(self):
        w=self.worker();w.source=self.root/'absent'
        with self.assertRaisesRegex(app.ArchiveError,'source_root_unavailable'):w.scan()
    def test_legacy_migration_requires_matching_dialogue_and_archived_v4(self):
        self.write(native());old=self.path.with_name('session.jsonl.zstd')
        old.write_bytes(zstandard.ZstdCompressor().compress(native().replace(b'"version": 4',b'"version": 0')))
        w=self.worker();w.scan()
        self.assertEqual(w.summary()['legacy_versions'],1)
        self.assertEqual(w.summary()['legacy_with_archived_native_v4'],0)
        w.upload(100,60);w.scan()
        self.assertEqual(w.summary()['legacy_with_archived_native_v4'],1)
        self.assertEqual(w.summary()['unsupported'],0)
        old.write_bytes(zstandard.ZstdCompressor().compress(native().replace(b'"version": 4',b'"version": 0').replace(b'hello',b'missing')))
        w.scan();self.assertEqual(w.summary()['unsupported'],1)
        self.assertEqual(w.summary()['legacy_with_archived_native_v4'],0)
    def test_uncommitted_payload_is_retained_and_counts_toward_disk_budget(self):
        self.write(native());w=self.worker();w.max_queue=100
        orphan=self.state/'queue'/'interrupted.dpapi';orphan.write_bytes(b'x'*100)
        w.scan();self.assertEqual(w.summary()['pending'],0)
        self.assertTrue(orphan.exists())
        self.assertEqual(w.summary()['issues'][0]['code'],'queue_size_limit')


if __name__ == '__main__':unittest.main()

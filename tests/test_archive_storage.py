"""Storage growth and evidence preservation, using only synthetic transcripts."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import random
from types import SimpleNamespace

import pytest

from evolvmem.context_models import ContextContentType, ContextItemDraft, ContextLayers
from evolvmem.context_store import ContextStore
from evolvmem.session_archive import AESGCM, SessionArchiver

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def env(test_config):
    with ContextStore(test_config) as store:
        yield test_config, store, SessionArchiver(test_config, store)


def payload(text, serial=0):
    return json.dumps({'source': 'client_reported', 'device_id': 'test',
                      'session_id': 'test-session', 'source_sha256': str(serial),
                      'conversation': [{'role': 'user', 'content': text[:1000]}],
                      'transcript': text}, ensure_ascii=False)


def noise(size=300_000):
    return base64.b64encode(random.Random(47).randbytes(size)).decode() + '\n中文原文\n'


def disk_bytes(root):
    return sum(p.stat().st_size for name in ('session_archives', 'session_chunks')
               for p in (root / name).rglob('*') if p.is_file())


def legacy(env, text, external='legacy'):
    config, store, archiver = env
    key = archiver._load_or_create_key()
    nonce = os.urandom(12)
    blob = nonce + AESGCM(key).encrypt(nonce, text.encode(), None)
    sha = hashlib.sha256(blob).hexdigest()
    path = f'session_archives/{sha}.bin'
    (config.data_dir / 'session_archives').mkdir(exist_ok=True)
    (config.data_dir / path).write_bytes(blob)
    with store.transaction():
        aid = store.upsert_session_archive('proj', 'codex', external,
            payload_path=path, payload_sha256=sha,
            expires_at='2026-01-31 00:00:00', recorded_at='2026-01-01 00:00:00')
    return aid


def link_knowledge(store, aid):
    item = store.create_item(ContextItemDraft(
        identity_key='proj:test:preserved-source', content_type=ContextContentType.EXPERIENCE,
        project='proj', layers=ContextLayers(l0='Verified rule', l1='Supporting detail',
            l2='Original evidence', generator='test')))
    with store.transaction():
        store.record_session_source(item.id, aid, extraction_version='test')
    return item


def test_growing_versions_share_storage_and_restore_exact_originals(env):
    config, store, archiver = env
    text = noise()
    versions = []
    for serial in range(24):
        text += f'追加第 {serial} 条记录\n'
        original = payload(text, serial)
        row = archiver.archive_session('proj', 'codex', f'session:{serial}', original)
        versions.append((row.id, original))
    assert disk_bytes(config.data_dir) < sum(len(s.encode()) for _, s in versions) / 4
    reader = SessionArchiver(config, store)
    assert [reader.read_payload(aid) for aid, _ in versions] == [s for _, s in versions]


def test_middle_edit_and_append_preserve_both_versions(env):
    config, _, archiver = env
    original = noise()
    changed = original[:100_000] + '改正原来的要求' + original[100_015:] + '\n新结论'
    rows = [archiver.archive_session('proj', 'codex', name, payload(text))
            for name, text in [('before', original), ('after', changed)]]
    from evolvmem.archive_maintenance import collect
    collect(archiver, grace_seconds=0)
    assert [archiver.read_payload(row.id) for row in rows] == [payload(original), payload(changed)]


def test_legacy_compaction_preserves_evidence_version_and_source_links(env):
    config, store, archiver = env
    original = payload(noise())
    aid = legacy(env, original)
    before = store.get_session_archive(aid)
    item = link_knowledge(store, aid)
    from evolvmem.archive_maintenance import compact
    report = compact(archiver, limit=10)
    after = store.get_session_archive(aid)
    assert report['compacted'] == 1
    assert archiver.read_payload(aid) == original
    assert after['payload_sha256'] == before['payload_sha256']
    assert after['created_at'] == before['created_at']
    assert store.get_item(item.id).source_state == 'available'
    assert compact(archiver, limit=10)['compacted'] == 0


def test_automatic_expiry_keeps_knowledge_sources(env):
    _, store, archiver = env
    record = archiver.archive_session('proj', 'codex', 'source', '有用的原文', now=NOW)
    item = link_knowledge(store, record.id)
    report = archiver.sweep_expired(now=NOW + timedelta(days=31))
    assert record.id in report.held_archive_ids
    assert archiver.read_payload(record.id) == '有用的原文'
    assert store.get_item(item.id).source_state == 'available'


def test_automatic_expiry_keeps_current_upload_and_organization_sources(env):
    config, store, archiver = env
    from evolvmem.lan_capture import LanCapture
    capture = LanCapture(SimpleNamespace(config=config, context_service=SimpleNamespace(store=store)))
    head = archiver.archive_session('proj', 'codex', 'head', '当前会话', now=NOW)
    reviewed = archiver.archive_session('proj', 'codex', 'review', '审核原文', now=NOW)
    with store.transaction():
        capture.conn.execute("INSERT INTO lan_session_uploads(device_id,session_id,sha256,project,declared_project,attribution_reason,received_at,total_bytes,received_bytes,archive_id) VALUES('pc','s','sha','proj','proj','test',0,1,1,?)", (head.id,))
        capture.conn.execute("INSERT INTO lan_session_heads VALUES('pc','s','sha',1)")
        capture.conn.execute("INSERT INTO organization_tasks(source_key,source_revision,rule_revision,created_at,updated_at) VALUES(?,'v1','r1','now','now')", (f'archive:{reviewed.id}',))
    report = archiver.sweep_expired(now=NOW + timedelta(days=31))
    assert {head.id, reviewed.id} <= set(report.held_archive_ids)


def test_failed_write_orphans_are_collected_without_deleting_shared_content(env, monkeypatch):
    config, store, archiver = env
    first = archiver.archive_session('proj', 'codex', 'good', payload(noise()))
    expected = archiver.read_payload(first.id)
    before = disk_bytes(config.data_dir)
    def fail(*args, **kwargs):
        raise RuntimeError('simulated commit failure')
    with monkeypatch.context() as patch:
        patch.setattr(store, 'upsert_session_archive', fail)
        with pytest.raises(RuntimeError):
            archiver.archive_session('proj', 'codex', 'failed', payload(noise() + noise(90_000)))
    from evolvmem.archive_maintenance import collect
    result = collect(archiver, grace_seconds=0)
    assert result['reclaimed_bytes'] > 0
    assert disk_bytes(config.data_dir) == before
    assert archiver.read_payload(first.id) == expected


def test_missing_or_corrupt_manifest_prevents_chunk_collection(env):
    config, _, archiver = env
    first = archiver.archive_session('proj', 'codex', 'good', payload(noise()))
    manifest = config.data_dir / first.payload_path
    manifest.write_bytes(b'corrupted manifest')
    from evolvmem.archive_maintenance import collect
    before = disk_bytes(config.data_dir)
    report = collect(archiver, grace_seconds=0)
    assert report['errors'] > 0
    assert disk_bytes(config.data_dir) == before
    assert archiver.read_payload(first.id) is None


def test_low_disk_reserve_keeps_existing_data_and_defers_new_writes(env, monkeypatch):
    _, _, archiver = env
    previous = archiver.archive_session('proj', 'codex', 'existing', '原文')
    import evolvmem.archive_storage as storage
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda _: SimpleNamespace(free=1024))
    assert archiver.archive_session('proj', 'codex', 'new', '新增原文') is None
    assert archiver.read_payload(previous.id) == '原文'


def test_outer_transaction_rollback_keeps_previous_version(env):
    config, store, archiver = env
    before = archiver.archive_session('proj', 'codex', 'same', payload(noise()))
    with pytest.raises(RuntimeError):
        with store.transaction():
            archiver.archive_session('proj', 'codex', 'same', '未提交的内容')
            raise RuntimeError('rollback')
    assert archiver.read_payload(before.id) == payload(noise())
    from evolvmem.archive_maintenance import collect
    assert collect(archiver, grace_seconds=0)['removed_files'] == 1
    assert (config.data_dir / before.payload_path).is_file()


def test_compaction_failure_keeps_legacy_original(env, monkeypatch):
    _, store, archiver = env
    aid = legacy(env, payload(noise()))
    before = store.get_session_archive(aid)
    def fail(*args):
        raise RuntimeError('database failure')
    monkeypatch.setattr(archiver, '_register_storage', fail)
    from evolvmem.archive_maintenance import compact, collect
    assert compact(archiver)['errors'] == 1
    collect(archiver, grace_seconds=0)
    assert store.get_session_archive(aid) == before
    assert archiver.read_payload(aid) == payload(noise())


def test_stale_purge_cannot_remove_updated_version(env):
    _, store, archiver = env
    first = archiver.archive_session('proj', 'codex', 'same', '旧内容')
    stale = store.get_session_archive(first.id)
    archiver.archive_session('proj', 'codex', 'same', '新内容')
    report = archiver._purge_rows([stale], purged_at='2026-02-01 00:00:00')
    assert not report.purged_archive_ids
    assert archiver.read_payload(first.id) == '新内容'


def test_staging_compression_legacy_reads_and_low_space_retry(env, monkeypatch):
    config, store, _ = env
    from evolvmem.lan_capture import LanCapture
    from evolvmem.lan_sharing import LanError
    capture = LanCapture(SimpleNamespace(config=config, context_service=SimpleNamespace(store=store)))
    raw = ('重复的会话文字\n' * 30_000).encode()
    path = capture._stage(('pc', 'session', 'sha'))
    capture._write_stage(path, raw)
    assert path.stat().st_size < len(raw) / 4
    assert capture._read_stage(path) == raw
    nonce = os.urandom(12)
    path.write_bytes(nonce + AESGCM(capture.archiver._load_or_create_key()).encrypt(nonce, raw, None))
    assert capture._read_stage(path) == raw
    import evolvmem.archive_storage as storage
    monkeypatch.setattr(storage.shutil, 'disk_usage', lambda _: SimpleNamespace(free=1024))
    with pytest.raises(LanError, match='archive_storage_space_low'):
        capture._write_stage(path, raw + b'more')
    assert capture._read_stage(path) == raw


def test_pruning_only_rebuildable_old_cache_preserves_original_and_sources(env):
    config, store, archiver = env
    from evolvmem.lan_capture import LanCapture
    from evolvmem.archive_maintenance import maintain
    capture = LanCapture(SimpleNamespace(config=config, context_service=SimpleNamespace(store=store)))
    rows = [archiver.archive_session('proj', 'codex', str(i), payload('正文' + str(i))) for i in range(3)]
    link_knowledge(store, rows[1].id)
    with store.transaction():
        for i, row in enumerate(rows):
            capture.conn.execute("INSERT INTO lan_session_uploads(device_id,session_id,sha256,project,declared_project,received_at,total_bytes,received_bytes,archive_id,backfill_status) VALUES('pc','s',?,'proj','proj',0,1,1,?,'done')", (str(i), row.id))
        capture.conn.execute("INSERT INTO lan_session_heads VALUES('pc','s','2',3)")
    assert maintain(archiver)['pruned_history_caches'] == 1
    assert {r[0] for r in capture.conn.execute('SELECT archive_id FROM conversation_history')} == {rows[1].id, rows[2].id}
    assert [archiver.read_payload(r.id) for r in rows] == [payload('正文' + str(i)) for i in range(3)]


def test_maintenance_runs_bounded_batches_and_backs_off_after_errors(env, monkeypatch):
    import threading
    import evolvmem.archive_maintenance as maintenance
    config, store, _ = env
    server = SimpleNamespace(config=config, context_service=SimpleNamespace(store=store))
    adapter = SimpleNamespace(lock=threading.RLock(), runtime=SimpleNamespace(server_for=lambda *a: server))
    calls = []
    def run(archiver):
        calls.append(archiver)
        return dict(errors=0, remaining=1, history_has_more=False)
    monkeypatch.setattr(maintenance, 'maintain', run)
    clock = [100.0]
    job = maintenance.LanArchiveMaintenance(adapter, now=lambda: clock[0])
    assert job.check()
    assert not job.check()
    assert len(calls) == 3
    clock[0] += 60
    assert job.check()
    def fail(archiver):
        raise RuntimeError('failure')
    monkeypatch.setattr(maintenance, 'maintain', fail)
    clock[0] += 60
    job.check()
    assert job.next_check >= clock[0] + 3600
    assert json.loads((config.data_dir / 'archive_maintenance.json').read_text())['report']['errors'] == 1


def test_compaction_does_not_lock_writers_or_overwrite_concurrent_update(env, monkeypatch):
    config, store, archiver = env
    aid = legacy(env, payload(noise()))
    storage = archiver._storage()
    original_pack = storage.pack
    def concurrent_change(text):
        # This independent writer must succeed while compression is running.
        with ContextStore(config) as other:
            other._connection().execute('PRAGMA busy_timeout=10')
            SessionArchiver(config, other).archive_session('proj', 'codex', 'legacy', 'newer version')
        return original_pack(text)
    monkeypatch.setattr(storage, 'pack', concurrent_change)
    monkeypatch.setattr(archiver, '_storage', lambda: storage)
    from evolvmem.archive_maintenance import compact, collect
    result = compact(archiver)
    assert result['errors'] == 0
    assert result['compacted'] == 0
    assert archiver.read_payload(aid) == 'newer version'
    collect(archiver, grace_seconds=0)
    assert archiver.read_payload(aid) == 'newer version'


def _old_history_candidate(env):
    config, store, archiver = env
    from evolvmem.lan_capture import LanCapture
    capture = LanCapture(SimpleNamespace(config=config, context_service=SimpleNamespace(store=store)))
    old = archiver.archive_session("proj", "codex", "old-cache", payload("old cache dialogue"))
    head = archiver.archive_session("proj", "codex", "current-cache", payload("current dialogue"))
    with store.transaction():
        for digest, row in (("old", old), ("new", head)):
            capture.conn.execute(
                "INSERT INTO lan_session_uploads(device_id,session_id,sha256,project,declared_project,"
                "received_at,total_bytes,received_bytes,archive_id,backfill_status) "
                "VALUES('pc','maintenance-race',?,'proj','proj',0,1,1,?,'done')", (digest, row.id))
        capture.conn.execute("INSERT INTO lan_session_heads VALUES('pc','maintenance-race','new',2)")
    return old


def test_cache_protection_scan_releases_writer_and_preserves_new_reference(env, monkeypatch):
    import sqlite3
    import evolvmem.archive_maintenance as maintenance
    config, store, archiver = env
    old = _old_history_candidate(env)
    original = maintenance.protected_archive_ids
    wrote = []
    def concurrent_reference(scanned_store):
        # Capture the old protected set before a genuine independent commit.
        protected = original(scanned_store)
        with sqlite3.connect(config.db_path, timeout=0.02) as writer:
            writer.execute(
                "INSERT INTO organization_tasks(source_key,source_revision,rule_revision,created_at,updated_at) "
                "VALUES(?,'v1','r1','now','now')", (f"archive:{old.id}",))
        wrote.append(True)
        return protected
    monkeypatch.setattr(maintenance, "protected_archive_ids", concurrent_reference)

    result = maintenance.prune_history_cache(archiver)

    assert wrote == [True]
    assert result["pruned_history_caches"] == 0
    assert store._connection().execute(
        "SELECT 1 FROM conversation_history WHERE archive_id=?", (old.id,)).fetchone()
    assert archiver.read_payload(old.id) == payload("old cache dialogue")


def test_gc_manifest_scan_allows_new_archive_and_keeps_its_reused_blocks(env, monkeypatch):
    import evolvmem.archive_maintenance as maintenance
    config, store, archiver = env
    original = archiver.archive_session("proj", "codex", "before-gc", payload(noise(90_000)))
    storage = archiver._storage()
    new_payload = payload(noise(120_000) + "new retained source")
    # Old unreferenced blocks are precisely what GC might delete.
    storage.pack(new_payload)
    read_manifest = storage.manifest
    saved = []
    def concurrent_archive(blob):
        if not saved:
            other = ContextStore(config)
            other.initialize(create_schema=False)
            try:
                other._connection().execute("PRAGMA busy_timeout=20")
                row = SessionArchiver(config, other).archive_session(
                    "proj", "codex", "during-gc", new_payload)
                saved.append(row.id)
            finally:
                other.close()
        return read_manifest(blob)
    monkeypatch.setattr(storage, "manifest", concurrent_archive)
    monkeypatch.setattr(archiver, "_storage", lambda: storage)

    result = maintenance.collect(archiver, grace_seconds=0)

    assert saved
    assert result["removed_files"] == 0
    assert archiver.read_payload(saved[0]) == new_payload
    assert archiver.read_payload(original.id) == payload(noise(90_000))

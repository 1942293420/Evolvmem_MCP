"""Explicit current-version pointer: rewritten snapshots must remain uploadable.

Server-side contract under test:
- ``source_order``/``current_sha256`` are optional upload metadata, never required;
- ``lan_session_heads`` is the only current pointer; a history version can never
  win just because it is larger, arrived later, or has no order;
- a rewritten (even shorter) snapshot becomes current only when the client
  declares ``current_sha256 == sha256`` and its order is strictly newer;
- legacy (order-less) clients keep the original append/stale/fork behaviour.
"""
import hashlib
import json
import time

from tests.test_lan_capture import transcript, upload
from tests.test_lan_sharing import lan  # noqa: F401  (pytest fixture)
from evolvmem.session_archive import SessionArchiver

IDENTITY = {'device_id': 'windows-main', 'session_id': 'session-1'}


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def version(adapter, raw, order, *, current=False, **changes):
    changes['source_order'] = order
    if current:
        changes['current_sha256'] = sha(raw)
    return upload(adapter, raw, **changes)


def conn(runtime):
    return runtime.server_for('jiangli').context_service.store._connection()


def archiver(runtime):
    server = runtime.server_for('jiangli')
    return SessionArchiver(server.config, server.context_service.store)


def head(runtime):
    return conn(runtime).execute(
        'SELECT sha256, source_order FROM lan_session_heads WHERE device_id=? AND session_id=?',
        (IDENTITY['device_id'], IDENTITY['session_id'])).fetchone()


def capture(adapter):
    return adapter.captures['jiangli']


def status(adapter):
    return adapter.call_tool('jiangli', 'session_archive_status', dict(IDENTITY))


def count(runtime, table, where='', args=()):
    return conn(runtime).execute(f'SELECT COUNT(*) FROM {table} {where}', args).fetchone()[0]


def test_smaller_rewrite_becomes_current_and_claimable(lan):
    runtime, adapter = lan
    big = transcript(text='旧的大版本快照内容' * 30)
    small = transcript(text='重写后的更小当前快照')
    assert len(small) < len(big)
    saved_big = upload(adapter, big, extract=True)
    assert saved_big['status'] == 'archived'

    saved_small = version(adapter, small, 2, current=True, extract=True)

    assert saved_small['status'] == 'archived', saved_small
    assert saved_small.get('current') is True
    assert saved_small['source_sha256'] == sha(small)
    assert saved_small['archive_id'] != saved_big['archive_id']
    claimed = capture(adapter).claim_pending()
    assert claimed is not None, 'an older larger history version must not block the new current'
    assert claimed['sha256'] == sha(small)
    assert claimed['archive_id'] == saved_small['archive_id']
    assert json.loads(archiver(runtime).read_payload(saved_big['archive_id']))['transcript'] == big.decode()
    current = status(adapter)
    assert current['archive_id'] == saved_small['archive_id']
    assert current['source_sha256'] == sha(small)


def test_late_large_fork_is_archived_without_replacing_current(lan):
    runtime, adapter = lan
    current_raw = transcript(text='当前快照')
    late_fork = transcript(text='迟到的旧分叉内容' * 30) + b'{"type":"event_msg","payload":{"type":"token_count"}}\n'
    assert len(late_fork) > len(current_raw)
    current = version(adapter, current_raw, 100, current=True)

    late = version(adapter, late_fork, 50, extract=True)

    assert late['status'] == 'archived', late
    assert late.get('current') is False
    assert late['archive_id'] and late['archive_id'] != current['archive_id']
    assert late['source_sha256'] == sha(late_fork)
    assert late['next_offset'] == len(late_fork)
    assert late['extraction_status'] == 'not_requested'
    assert json.loads(archiver(runtime).read_payload(late['archive_id']))['transcript'] == late_fork.decode()
    assert head(runtime)['sha256'] == sha(current_raw)
    current_status = status(adapter)
    assert current_status['archive_id'] == current['archive_id']
    assert current_status['source_sha256'] == sha(current_raw)
    retried = adapter.call_tool('jiangli', 'session_archive_retry', {**IDENTITY, 'request_id': 'retry-current'})
    assert retried['archive_id'] == current['archive_id']


def test_old_large_version_does_not_block_new_small_extraction_or_backfill(lan):
    runtime, adapter = lan
    begun = adapter.call_tool('jiangli', 'continuity_begin', {
        'workspace_path': r'C:\work\demo', 'device_id': 'windows-main', 'project': 'demo',
        'objective': '保留人工创建的项目焦点', 'request_id': 'manual-focus'})
    assert 'workstream_id' in begun, begun
    big = transcript(text='旧的大版本补录内容' * 30)
    big += b'{"type":"response_item","payload":{"type":"function_call","name":"exec_command","call_id":"unfinished-big","arguments":"{}"}}\n'
    assert upload(adapter, big, extract=True)['status'] == 'archived'
    assert adapter.process_backfills(now=time.time()) == 0

    small = transcript(text='新的小版本补录内容')
    small += b'{"type":"response_item","payload":{"type":"function_call","name":"exec_command","call_id":"unfinished-small","arguments":"{}"}}\n'
    saved = version(adapter, small, 5, current=True, extract=True)

    assert adapter.process_backfills(now=time.time() + 1900) == 1
    current = status(adapter)
    assert current['source_sha256'] == sha(small), current
    assert current['backfill']['code'] == 'created', current
    big_row = conn(runtime).execute(
        'SELECT extraction_status, backfill_status FROM lan_session_uploads WHERE sha256=?', (sha(big),)).fetchone()
    assert big_row['extraction_status'] == 'superseded'
    assert big_row['backfill_status'] != 'created'
    claimed = capture(adapter).claim_pending()
    assert claimed is not None and claimed['archive_id'] == saved['archive_id']


def test_repeated_final_chunk_is_idempotent(lan):
    runtime, adapter = lan
    raw = transcript()
    first = version(adapter, raw, 9, current=True)

    assert version(adapter, raw, 9, current=True) == first
    assert count(runtime, 'session_archives') == 1
    assert count(runtime, 'lan_session_uploads') == 1
    assert tuple(head(runtime)) == (sha(raw), 9)
    assert capture(adapter).claim_pending() is None

    queued = version(adapter, raw, 9, current=True, extract=True)
    assert queued['archive_id'] == first['archive_id']
    assert queued['extraction_status'] == 'pending'
    claimed = capture(adapter).claim_pending()
    assert claimed is not None and claimed['archive_id'] == first['archive_id']
    assert claimed['sha256'] == sha(raw)


def test_same_order_with_different_hash_is_rejected(lan):
    runtime, adapter = lan
    first_raw = transcript(text='顺序七的第一版')
    other_raw = transcript(text='顺序七的改写版')
    first = version(adapter, first_raw, 7, current=True)

    rejected = version(adapter, other_raw, 7, current=True)

    assert rejected.get('error') == 'upload_metadata_conflict', rejected
    assert count(runtime, 'session_archives') == 1
    assert count(runtime, 'lan_session_uploads', 'WHERE sha256=?', (sha(other_raw),)) == 0
    assert tuple(head(runtime)) == (sha(first_raw), 7)
    assert status(adapter)['archive_id'] == first['archive_id']
    # A version keeps its order while it is only retried: without a current
    # declaration (or below its own recorded order) the order must stay fixed.
    assert version(adapter, first_raw, 8).get('error') == 'upload_metadata_conflict'
    assert version(adapter, first_raw, 6, current=True).get('error') == 'upload_metadata_conflict'


def test_recaptured_archived_version_reuses_archive_and_promotes(lan):
    """A real B->A re-capture selects the old immutable sha with a newer order."""
    runtime, adapter = lan
    first_raw = transcript(text='回退目标的第一版内容')
    other_raw = transcript(text='中间版本的内容')
    first = version(adapter, first_raw, 5, current=True)
    middle = version(adapter, other_raw, 6, current=True)
    assert tuple(head(runtime)) == (sha(other_raw), 6)

    recaptured = version(adapter, first_raw, 7, current=True, extract=True)

    assert recaptured['status'] == 'archived', recaptured
    assert recaptured.get('current') is True
    assert recaptured['source_order'] == 7
    assert recaptured['source_sha256'] == sha(first_raw)
    assert recaptured['archive_id'] == first['archive_id'], 'a re-capture must not duplicate the archive'
    assert recaptured['archive_id'] != middle['archive_id']
    assert count(runtime, 'session_archives') == 2
    assert count(runtime, 'lan_session_uploads') == 2
    assert tuple(head(runtime)) == (sha(first_raw), 7)
    # The row records the newest capture order instead of keeping the old one.
    assert conn(runtime).execute('SELECT source_order FROM lan_session_uploads WHERE sha256=?',
                                 (sha(first_raw),)).fetchone()[0] == 7
    assert json.loads(archiver(runtime).read_payload(first['archive_id']))['transcript'] == first_raw.decode()
    current = status(adapter)
    assert current['archive_id'] == first['archive_id']
    assert current['source_order'] == 7
    assert current['current'] is True
    claimed = capture(adapter).claim_pending()
    assert claimed is not None and claimed['archive_id'] == first['archive_id']


def test_delayed_low_order_request_never_rolls_back_head(lan):
    runtime, adapter = lan
    late_raw = transcript(text='迟到重试的旧捕获内容')
    latest_raw = transcript(text='顺序更高的新捕获内容')
    late = version(adapter, late_raw, 5, current=True)
    latest = version(adapter, latest_raw, 8, current=True)

    delayed = version(adapter, late_raw, 5, current=True, extract=True)

    assert delayed['status'] == 'archived', delayed
    assert delayed.get('current') is False
    assert delayed['source_order'] == 5
    assert delayed['archive_id'] == late['archive_id']
    assert delayed['extraction_status'] == 'not_requested'
    assert tuple(head(runtime)) == (sha(latest_raw), 8)
    assert status(adapter)['archive_id'] == latest['archive_id']
    assert status(adapter)['source_sha256'] == sha(latest_raw)


def test_history_version_order_change_is_rejected(lan):
    runtime, adapter = lan
    old_raw = transcript(text='已经成为历史的旧版本')
    new_raw = transcript(text='顺序更高的当前版本')
    version(adapter, old_raw, 5, current=True)
    version(adapter, new_raw, 6, current=True)

    forwarded = version(adapter, old_raw, 8)
    lowered = version(adapter, old_raw, 4, current=True)
    equal_to_head = version(adapter, old_raw, 6, current=True)

    for rejected in (forwarded, lowered, equal_to_head):
        assert rejected.get('error') == 'upload_metadata_conflict', rejected
    assert count(runtime, 'session_archives') == 2
    assert tuple(head(runtime)) == (sha(new_raw), 6)
    assert conn(runtime).execute('SELECT source_order FROM lan_session_uploads WHERE sha256=?',
                                 (sha(old_raw),)).fetchone()[0] == 5


def test_current_declaration_below_head_order_never_promotes(lan):
    runtime, adapter = lan
    history_raw = transcript(text='只作为历史归档的版本')
    current_raw = transcript(text='顺序更高的当前版本')
    history = version(adapter, history_raw, 5)
    current = version(adapter, current_raw, 8, current=True)

    replayed = version(adapter, history_raw, 5, current=True, extract=True)

    assert replayed['status'] == 'archived'
    assert replayed.get('current') is False
    assert replayed['source_order'] == 5
    assert replayed['archive_id'] == history['archive_id']
    assert replayed['extraction_status'] == 'not_requested'
    assert tuple(head(runtime)) == (sha(current_raw), 8)
    assert status(adapter)['archive_id'] == current['archive_id']


def test_repeated_recapture_is_idempotent(lan):
    runtime, adapter = lan
    first_raw = transcript(text='重复重捕获的同一个版本')
    other_raw = transcript(text='中间版本的内容')
    first = version(adapter, first_raw, 5, current=True)
    version(adapter, other_raw, 6, current=True)
    recaptured = version(adapter, first_raw, 7, current=True)

    again = version(adapter, first_raw, 7, current=True)

    assert again == recaptured
    assert again['archive_id'] == first['archive_id']
    assert again.get('current') is True
    assert count(runtime, 'session_archives') == 2
    assert count(runtime, 'lan_session_uploads') == 2
    assert tuple(head(runtime)) == (sha(first_raw), 7)


def test_recapture_receipt_carries_order_and_current_for_client_ack(lan):
    """The client only confirms its anchor from current=true and the new order."""
    runtime, adapter = lan
    first_raw = transcript(text='回执校验的目标版本')
    other_raw = transcript(text='回执校验的中间版本')
    version(adapter, first_raw, 5, current=True)
    version(adapter, other_raw, 6, current=True)

    receipt = version(adapter, first_raw, 7, current=True)

    assert receipt['current'] is True
    assert receipt['source_order'] == 7
    assert receipt['source_sha256'] == sha(first_raw)
    assert receipt['archive_id']
    # A superseded version still reports its own order with current=false, so a
    # delayed receipt can never confirm the anchor.
    delayed = version(adapter, other_raw, 6, current=True)
    assert delayed['current'] is False
    assert delayed['source_order'] == 6
    assert tuple(head(runtime)) == (sha(first_raw), 7)


def test_legacy_archived_row_cannot_reuse_head_order(lan):
    """An archived order-less row no longer bypasses the head-order uniqueness."""
    runtime, adapter = lan
    legacy_raw = transcript(text='旧客户端归档的历史版本')
    legacy = upload(adapter, legacy_raw)
    head_raw = transcript(text='新协议的当前版本')
    current = version(adapter, head_raw, 10, current=True)

    rejected = version(adapter, legacy_raw, 10)
    claimed = version(adapter, legacy_raw, 10, current=True)

    assert rejected.get('error') == 'upload_metadata_conflict', rejected
    assert claimed.get('error') == 'upload_metadata_conflict', claimed
    assert count(runtime, 'session_archives') == 2
    assert tuple(head(runtime)) == (sha(head_raw), 10)
    assert status(adapter)['archive_id'] == current['archive_id']
    assert conn(runtime).execute('SELECT source_order FROM lan_session_uploads WHERE sha256=?',
                                 (sha(legacy_raw),)).fetchone()[0] is None


def test_recapture_of_legacy_archived_version_records_new_order(lan):
    runtime, adapter = lan
    legacy_raw = transcript(text='旧客户端归档的回退目标')
    other_raw = transcript(text='新协议的中间版本')
    legacy = upload(adapter, legacy_raw)
    version(adapter, other_raw, 10, current=True)

    recaptured = version(adapter, legacy_raw, 12, current=True)

    assert recaptured['status'] == 'archived', recaptured
    assert recaptured.get('current') is True
    assert recaptured['source_order'] == 12
    assert recaptured['archive_id'] == legacy['archive_id']
    assert count(runtime, 'session_archives') == 2
    assert tuple(head(runtime)) == (sha(legacy_raw), 12)
    assert conn(runtime).execute('SELECT source_order FROM lan_session_uploads WHERE sha256=?',
                                 (sha(legacy_raw),)).fetchone()[0] == 12


def test_legacy_migration_anchor_then_old_queue_never_downgrades(lan):
    runtime, adapter = lan
    legacy = transcript(text='旧客户端已归档的当前缓存')
    legacy_saved = upload(adapter, legacy)

    anchor = version(adapter, legacy, 1000, current=True)

    assert anchor['archive_id'] == legacy_saved['archive_id']
    assert anchor.get('current') is True
    assert tuple(head(runtime)) == (sha(legacy), 1000)
    assert conn(runtime).execute('SELECT source_order FROM lan_session_uploads WHERE sha256=?',
                                 (sha(legacy),)).fetchone()[0] == 1000
    older = version(adapter, transcript(text='旧队列中的更旧快照'), 5, extract=True)
    assert older['status'] == 'archived' and older.get('current') is False
    assert older['extraction_status'] == 'not_requested'
    unconfirmed = version(adapter, transcript(text='顺序更大但未声明当前'), 2000, extract=True)
    assert unconfirmed['status'] == 'archived' and unconfirmed.get('current') is False
    assert unconfirmed['extraction_status'] == 'not_requested'
    assert tuple(head(runtime)) == (sha(legacy), 1000)
    assert status(adapter)['source_sha256'] == sha(legacy)

    appended = legacy + b'{"type":"event_msg","payload":{"type":"token_count"}}\n'
    assert upload(adapter, appended)['status'] == 'archived'

    assert tuple(head(runtime)) == (sha(appended), 1000), 'a legacy append must not lower the order floor'
    lower = version(adapter, transcript(text='顺序更小的新协议快照'), 999, current=True, extract=True)
    assert lower.get('current') is False
    assert lower['extraction_status'] == 'not_requested'
    assert status(adapter)['source_sha256'] == sha(appended)


def test_shorter_prefix_rewrite_promotes_under_new_protocol(lan):
    runtime, adapter = lan
    base = transcript()
    longer = base + b'{"type":"event_msg","payload":{"type":"token_count"}}\n'
    current = version(adapter, longer, 3, current=True)

    shrunk = version(adapter, base, 4, current=True)

    assert shrunk['status'] == 'archived', shrunk
    assert shrunk.get('current') is True
    assert shrunk['archive_id'] != current['archive_id']
    assert status(adapter)['source_sha256'] == sha(base)
    assert capture(adapter).claim_pending() is None
    assert json.loads(archiver(runtime).read_payload(current['archive_id']))['transcript'] == longer.decode()


def test_legacy_shorter_prefix_without_new_fields_stays_stale(lan):
    runtime, adapter = lan
    base = transcript()
    longer = base + b'{"type":"event_msg","payload":{"type":"token_count"}}\n'
    upload(adapter, longer)

    stale = upload(adapter, base)

    assert stale['status'] == 'stale'
    assert stale['submitted_sha256'] == sha(base)
    assert status(adapter)['source_sha256'] == sha(longer)


def test_purged_previous_payload_does_not_block_new_current_snapshot(lan):
    runtime, adapter = lan
    old = transcript(text='将被清理的旧版本')
    first = upload(adapter, old)
    assert archiver(runtime).purge_project('demo').purged_archive_ids == (first['archive_id'],)

    fresh = transcript(text='清理后独立的当前快照')
    saved = version(adapter, fresh, 2, current=True)

    assert saved['status'] == 'archived', saved
    assert saved.get('current') is True
    assert json.loads(archiver(runtime).read_payload(saved['archive_id']))['transcript'] == fresh.decode()
    assert conn(runtime).execute('SELECT state FROM session_archives WHERE id=?',
                                 (first['archive_id'],)).fetchone()[0] == 'purged'
    assert archiver(runtime).read_payload(first['archive_id']) is None


def test_new_protocol_still_rejects_wrong_session_and_hash(lan):
    runtime, adapter = lan
    raw = transcript()

    wrong_session = version(adapter, raw, 1, current=True, session='other-session')
    wrong_hash = version(adapter, raw, 1, current=True, sha256='0' * 64)

    assert wrong_session.get('error') == 'session_id_mismatch', wrong_session
    assert wrong_hash.get('error') == 'transcript_hash_mismatch', wrong_hash
    assert count(runtime, 'session_archives') == 0
    assert count(runtime, 'lan_session_heads') == 0


def test_history_only_identity_reports_current_snapshot_unavailable(lan):
    runtime, adapter = lan
    raw = transcript(text='只作为历史归档')
    late = version(adapter, raw, 5)

    assert late['status'] == 'archived'
    assert late.get('current') is False
    assert late['archive_id']
    assert late['source_sha256'] == sha(raw)
    assert late['next_offset'] == len(raw)
    assert late['extraction_status'] == 'not_requested'
    current = status(adapter)
    assert current['status'] == 'current_snapshot_unavailable', current
    assert 'archive_id' not in current
    assert capture(adapter).claim_pending() is None
    assert adapter.process_backfills(now=time.time() + 1900) == 0


def test_history_only_upload_anchors_head_at_legacy_current(lan):
    runtime, adapter = lan
    legacy = transcript(text='旧客户端归档的当前缓存')
    saved = upload(adapter, legacy)

    late = version(adapter, transcript(text='新协议仅历史归档'), 5)

    assert late['status'] == 'archived' and late.get('current') is False
    assert tuple(head(runtime)) == (sha(legacy), None)
    current = status(adapter)
    assert current['archive_id'] == saved['archive_id']
    assert current['source_sha256'] == sha(legacy)


def test_superseded_extraction_is_not_persisted(lan, monkeypatch):
    from evolvmem import kimi_hooks
    runtime, adapter = lan
    old = transcript(text='正在整理的旧版本')
    upload(adapter, old, extract=True)
    credentials = runtime.settings.owner_data_dir / 'llm_credentials.json'
    credentials.write_text(json.dumps({'provider': 'deepseek', 'api_key': 'fixture-only'}))
    replacement = transcript(text='整理期间成为当前的新版本')
    calls = []

    def model_response(prompt, config, **_kwargs):
        if not calls:
            version(adapter, replacement, 2, current=True, extract=True)
        calls.append(prompt)
        return json.dumps({'memories': [{'key': 'SESSION_SUMMARY', 'value': '不应在失效版本上写入'}],
                           'l0': 'x', 'l1': 'y', 'l2': 'z'}, ensure_ascii=False)

    monkeypatch.setattr(kimi_hooks, '_call_llm_with_retry', model_response)

    assert adapter.process_pending() == 1

    assert calls, 'the extraction model must have run before the version was replaced'
    old_row = conn(runtime).execute(
        'SELECT extraction_status, extraction_result, error FROM lan_session_uploads WHERE sha256=?',
        (sha(old),)).fetchone()
    assert old_row['extraction_status'] == 'superseded', dict(old_row)
    assert old_row['extraction_result'] == '{}'
    assert old_row['error'] == 'version_superseded'
    claimed = capture(adapter).claim_pending()
    assert claimed is not None and claimed['sha256'] == sha(replacement)


def test_existing_uploads_table_migrates_to_optional_order_column(lan):
    """An old database gains the head table and nullable column without inventing orders."""
    from evolvmem.lan_capture import LanCapture
    runtime, adapter = lan
    server = runtime.server_for('jiangli')
    with server.context_service.store.transaction():
        server.context_service.store._connection().execute('ALTER TABLE lan_session_uploads DROP COLUMN source_order')
        server.context_service.store._connection().execute('DROP TABLE lan_session_heads')

    LanCapture(server)

    assert 'source_order' in {row[1] for row in conn(runtime).execute('PRAGMA table_info(lan_session_uploads)')}
    assert {row[1] for row in conn(runtime).execute('PRAGMA table_info(lan_session_heads)')} == {
        'device_id', 'session_id', 'sha256', 'source_order'}
    saved = upload(adapter, transcript())
    assert saved['status'] == 'archived'
    assert saved['source_order'] is None
    assert count(runtime, 'lan_session_heads') == 0, 'an order-less client never creates a head'


def test_upload_schema_keeps_version_fields_optional(lan):
    _, adapter = lan
    spec = adapter._specs('jiangli')['session_archive_upload']['inputSchema']
    assert spec['properties']['source_order'] == {'type': 'integer', 'minimum': 1}
    assert spec['properties']['current_sha256'] == {'type': 'string', 'minLength': 64, 'maxLength': 64}
    assert 'source_order' not in spec['required']
    assert 'current_sha256' not in spec['required']

    raw = transcript()
    missing_order = upload(adapter, raw, current_sha256=sha(raw))
    assert missing_order.get('error') == 'invalid_source_order', missing_order
    assert version(adapter, raw, 0).get('error') == 'invalid_arguments'

"""Synthetic private archives prove handoff without writing or invoking AI."""
import hashlib
import json

import pytest

from tests.test_lan_sharing import lan
from tests.test_lan_capture import transcript, upload


@pytest.fixture(autouse=True)
def policy(tmp_path, monkeypatch):
    path = tmp_path / 'policy.json'
    path.write_text(json.dumps({'ai_processing_enabled': True, 'legacy_web_enabled': True}))
    monkeypatch.setenv('EVOLVMEM_PRODUCT_POLICY_FILE', str(path))
    return path


def status(adapter, raw, *, user='jiangli', project='demo', session='session-1'):
    return adapter.call_tool(user, 'session_archive_status', {
        'device_id': 'windows-main', 'session_id': session,
        'handoff': {'sha256': hashlib.sha256(raw).hexdigest(), 'project': project}})


def seed(lan, *, session='session-1', title='Synthetic verified feature'):
    from evolvmem import conversation_tasks, auto_organization as org
    from evolvmem.context_store import _now_iso
    from evolvmem.project_archive_pipeline import ProjectArchivePipeline
    runtime, adapter = lan
    service = runtime.server_for('jiangli').context_service
    service.knowledge().save_project({'project': 'demo'})
    raw = transcript(session, text='Synthetic project decision: retain source proof and add an export feature.')
    receipt = upload(adapter, raw, session=session)
    from evolvmem import governance_sources
    governance_sources.apply_plan(service, governance_sources.build_plan(service.store._connection(), keys=[f"archive:{receipt['archive_id']}"]))
    task_id = conversation_tasks.enqueue(service, [f"archive:{receipt['archive_id']}"])['items'][0]['id']
    task = org._task_row(service, task_id)
    body = task['source_snapshot']
    digest = org.unit_digest(task['source_key'], 0, len(body), body)
    now = _now_iso()
    with service.store.transaction():
        service.store._connection().execute(
            'INSERT INTO organization_units(task_id,ordinal,digest,title,text,source_start,source_end,'
            'project,projects,knowledge_scope,decision,disposition,created_at,updated_at) '
            "VALUES(?,0,?,'Synthetic unit',?,0,?,'demo','[\"demo\"]','project','manual','keep',?,?)",
            (task_id, digest, body, len(body), now, now))
    result = ProjectArchivePipeline(service).process_task(task, provider=lambda _: {'events': [
        {'event_type': 'feature', 'title': title, 'content': 'Synthetic feature has verified tests.', 'occurred_at': None}]})
    with service.store.transaction():
        service.store._connection().execute("UPDATE organization_tasks SET status='completed' WHERE id=?", (task_id,))
    return service, raw, receipt, task, result


def test_default_unchanged_and_missing_proof_is_explicit(lan):
    runtime, adapter = lan
    raw = transcript()
    upload(adapter, raw)
    service = runtime.server_for('jiangli').context_service
    service.knowledge().save_project({'project': 'demo'})
    conn = service.store._connection()
    before = conn.total_changes
    basic = adapter.call_tool('jiangli', 'session_archive_status', {'device_id': 'windows-main', 'session_id': 'session-1'})
    result = status(adapter, raw)
    assert 'error' not in result, result
    proof = result.pop('handoff')
    assert result == basic
    assert proof['state'] == 'pending' and proof['reason'] == 'organization_task_missing'
    assert conn.total_changes == before
    assert conn.execute('PRAGMA query_only').fetchone()[0] == 0
    assert not conn.in_transaction


def test_ready_proves_current_completed_task_and_exact_summary_without_text(lan):
    service, raw, receipt, task, result = seed(lan)
    before = service.store._connection().total_changes
    proof = status(lan[1], raw)['handoff']
    assert proof['state'] == 'ready', proof
    assert proof['archive_id'] == receipt['archive_id']
    assert proof['task_id'] == task['id'] and proof['source_revision'] == task['source_revision']
    assert proof['source_current'] and proof['covers_source']
    assert proof['summary']['event_ids'] == result['event_ids']
    assert 'Synthetic' not in json.dumps(proof)
    assert service.store._connection().total_changes == before


def test_newer_same_project_watermark_keeps_earlier_source_covered(lan):
    service, raw, _, task, first = seed(lan)
    _, _, _, later, _ = seed(lan, session='session-2', title='Another synthetic feature')
    proof = status(lan[1], raw)['handoff']
    assert proof['state'] == 'ready', proof
    assert proof['task_id'] == task['id'] != later['id']
    assert proof['summary']['event_ids'] == first['event_ids']
    assert proof['summary']['revision'] >= 2


@pytest.mark.parametrize('change,reason', [
    ("UPDATE organization_tasks SET status='running'", 'organization_task_incomplete'),
    ('DELETE FROM extraction_receipts', 'history_receipt_missing'),
    ("UPDATE project_events SET status='ignored'", 'history_event_not_current'),
    ('DELETE FROM project_event_sources', 'history_binding_missing'),
    ("UPDATE extraction_receipts SET payload='{\"events\":[]}'", 'history_events_empty'),
    ("UPDATE organization_tasks SET source_revision='changed'", 'organization_source_changed'),
])
def test_incomplete_or_stale_proof_fails_closed(lan, change, reason):
    service, raw, *_ = seed(lan)
    with service.store.transaction():
        service.store._connection().execute(change)
    proof = status(lan[1], raw)['handoff']
    assert proof['state'] != 'ready' and proof['reason'] == reason, proof


def test_identity_project_hash_and_pause_fail_closed(lan, policy):
    service, raw, *_ = seed(lan)
    other = status(lan[1], raw, user='kane')
    assert other['status'] == 'not_found'
    assert other['handoff']['reason'] == 'archive_missing'
    assert status(lan[1], raw, project='wrong')['handoff']['reason'] == 'archive_project_mismatch'
    assert status(lan[1], raw + b' ')['handoff']['reason'] == 'archive_sha256_mismatch'
    policy.write_text(json.dumps({'ai_processing_enabled': False}))
    proof = status(lan[1], raw)['handoff']
    assert proof['state'] == 'blocked' and proof['reason'] == 'ai_processing_disabled'


def test_new_current_archive_invalidates_old_proof(lan):
    _, raw, *_ = seed(lan)
    newer = raw + b'{"type":"event_msg","payload":{"type":"token_count"}}\n'
    upload(lan[1], newer, source_order=2, current_sha256=hashlib.sha256(newer).hexdigest())
    proof = status(lan[1], raw)['handoff']
    assert proof['state'] == 'blocked' and proof['reason'] == 'archive_sha256_mismatch'


def test_broken_source_slice_and_ownership_never_reuse_old_event(lan):
    service, raw, *_ = seed(lan)
    with service.store.transaction():
        service.store._connection().execute("UPDATE organization_units SET text=text || 'changed'")
    proof = status(lan[1], raw)['handoff']
    assert proof['state'] != 'ready'


def test_pending_or_paused_has_no_processing_side_effects(lan, policy, monkeypatch):
    from evolvmem.project_archive_pipeline import ProjectArchivePipeline
    service, raw, *_ = seed(lan)
    def forbidden(*args, **kwargs):
        raise AssertionError('status must never process')
    monkeypatch.setattr(ProjectArchivePipeline, 'process_task', forbidden)
    conn = service.store._connection()
    policy.write_text(json.dumps({'ai_processing_enabled': False}))
    before = conn.total_changes
    assert status(lan[1], raw)['handoff']['reason'] == 'ai_processing_disabled'
    assert conn.total_changes == before


def test_query_only_snapshot_blocks_accidental_write_and_restores_connection(lan, monkeypatch):
    from evolvmem.project_archive_store import ProjectArchiveStore
    service, raw, *_ = seed(lan)
    conn = service.store._connection()
    def accidental_write(self, project, **kwargs):
        conn.execute("DELETE FROM project_events")
        return []
    monkeypatch.setattr(ProjectArchiveStore, 'events', accidental_write)
    before = conn.total_changes
    proof = status(lan[1], raw)['handoff']
    assert proof['state'] == 'blocked' and proof['reason'] == 'proof_unavailable'
    assert conn.total_changes == before
    assert conn.execute('SELECT COUNT(*) FROM project_events').fetchone()[0] == 1
    assert conn.execute('PRAGMA query_only').fetchone()[0] == 0 and not conn.in_transaction

"""Explicit, metadata-only proof that a current archive reached project history.

Never enqueues work, enrolls sources, initializes schema, calls a provider, or
returns source prose. All evidence is checked in one query-only SQLite snapshot.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3

from evolvmem.capability_provenance import provenance_snapshot
from evolvmem.lan_sharing import LanError


def handoff_status(capture, args):
    expected = args.get('handoff')
    if (not isinstance(expected, dict) or not {'sha256', 'project'} <= set(expected)
            or set(expected) - {'sha256', 'project', 'source_format'}
            or expected.get('source_format', 'codex-jsonl') not in {'codex-jsonl', 'dsh-session-jsonl-v1'}
            or not isinstance(expected['sha256'], str)
            or not re.fullmatch('[0-9a-f]{64}', expected['sha256'])
            or not isinstance(expected['project'], str)
            or not 1 <= len(expected['project']) <= 128
            or expected['project'].strip() != expected['project']):
        raise LanError('invalid_handoff_request')
    identity = {key: args[key] for key in ('device_id', 'session_id')}
    capture._identity(identity)
    base = {'schema_version': 1, 'project': expected['project'],
            'source_sha256': expected['sha256'], 'source_format': expected.get('source_format', 'codex-jsonl'),
            'covers_source': False}
    try:
        with provenance_snapshot(capture.store) as read:
            result = capture.status(identity)
            proof = _proof(capture, result, expected, read, base)
        return {**result, 'handoff': proof}
    except (sqlite3.DatabaseError, ValueError, KeyError, TypeError, RuntimeError):
        # No exception bodies: source text and database paths are private.
        result = capture.status(identity)
        return {**result, 'handoff': {**base, 'state': 'blocked', 'reason': 'proof_unavailable'}}


def _proof(capture, receipt, expected, read, base):
    from evolvmem.capability_pipeline import _units, source_ref
    from evolvmem.pipeline_state import cached_payload
    from evolvmem.product_policy import load_policy
    from evolvmem.project_archive_pipeline import history_projects
    from evolvmem.project_archive_store import ProjectArchiveStore, ProjectEventDraft, render_summary

    conn, service = capture.conn, capture.server.context_service
    proof = dict(base)

    def stop(reason, state='blocked'):
        return {**proof, 'state': state, 'reason': reason}

    if receipt.get('status') in {'not_found', 'current_snapshot_unavailable'}:
        return stop('archive_missing', 'pending')
    if receipt.get('source_format', 'codex-jsonl') != expected.get('source_format', 'codex-jsonl'):
        return stop('archive_source_format_mismatch')
    if receipt.get('source_sha256') != expected['sha256']:
        return stop('archive_sha256_mismatch')
    if receipt.get('project') != expected['project']:
        return stop('archive_project_mismatch')
    if receipt.get('status') != 'archived' or not receipt.get('archive_id'):
        return stop('archive_incomplete', 'pending')
    proof['archive_id'] = receipt['archive_id']
    if (receipt.get('current') is not True or receipt.get('payload_state') != 'available'
            or receipt.get('next_offset') != receipt.get('total_bytes')):
        return stop('archive_not_current_or_available')
    if expected['project'] not in read.active_projects:
        return stop('project_not_registered')
    if capture.source_excluded({'attribution_reason': receipt.get('attribution_reason', '')}):
        return stop('archive_excluded')
    policy = load_policy()
    if not policy.valid:
        return stop('policy_unavailable')

    key = f"archive:{receipt['archive_id']}"
    # The manifest, never a guessed hash or latest project watermark, maps an
    # exact uploaded archive to its primary organization source.
    tasks = [dict(row) for row in conn.execute(
        "SELECT t.* FROM organization_tasks t WHERE t.status!='superseded' AND t.superseded_by IS NULL "
        "AND (t.source_key=? OR t.source_key IN (SELECT c.source_key FROM organization_conversations c "
        "WHERE EXISTS(SELECT 1 FROM json_each(c.members) m WHERE m.value=?))) ORDER BY t.id DESC", (key, key))]
    if not tasks:
        if not policy.ai_processing_enabled:
            return stop('ai_processing_disabled')
        if not policy.legacy_web_enabled:
            return stop('project_history_processing_disabled')
        return stop('organization_task_missing', 'pending')
    current = [task for task in tasks if read.source_revision(task['source_key']) == (task['source_revision'], '')]
    if not current:
        return stop('organization_source_changed')
    if len(current) != 1:
        return stop('organization_task_ambiguous')
    task = current[0]
    from evolvmem.task_history_processing import completed_grant
    scoped = completed_grant(conn, receipt, task, expected['project'])
    if not scoped and not policy.ai_processing_enabled:
        return stop('ai_processing_disabled')
    if not scoped and not policy.legacy_web_enabled:
        return stop('project_history_processing_disabled')
    proof.update(source_key=task['source_key'], task_id=task['id'],
                 source_revision=task['source_revision'], task_status=task['status'], source_current=True)
    if task['status'] != 'completed':
        return stop('organization_task_incomplete', 'pending' if task['status'] in {'queued', 'running', 'pending'} else 'blocked')

    units = [u for u in _units(service, task) if expected['project'] in history_projects(service, u)]
    if not units:
        return stop('project_history_units_missing')
    archives = ProjectArchiveStore(capture.store)
    event_ids = set()
    for unit in units:
        payload = cached_payload(service, task, unit, 'project-history:v2')
        if payload is None:
            return stop('history_receipt_missing', 'pending')
        if not isinstance(payload, dict) or not isinstance(payload.get('events'), list):
            return stop('history_receipt_invalid')
        for event in payload['events']:
            if (not isinstance(event, dict) or event.get('event_type') not in {'feature', 'major_change', 'problem', 'milestone'}
                    or not all(isinstance(event.get(k), str) and event[k].strip() for k in ('title', 'content'))):
                return stop('history_receipt_invalid')
            fingerprint = ProjectEventDraft(expected['project'], event['event_type'], event['title'], event['content']).fingerprint()
            row = conn.execute('SELECT event_id,status FROM project_events WHERE fingerprint=? AND project=?',
                               (fingerprint, expected['project'])).fetchone()
            if row is None:
                return stop('history_event_missing', 'pending')
            if row['status'] != 'active':
                return stop('history_event_not_current')
            sources = [s for s in archives.sources(row['event_id'])
                       if s['source_kind'] == 'unit' and s['task_id'] == task['id']
                       and s['unit_digest'] == unit['digest'] and s['unit_revision'] == unit['revision']
                       and s['source_revision'] == task['source_revision']
                       and s['source_key'] == f"{task['source_key']}#{unit['digest']}"
                       and s['source_ref'] == source_ref(task, unit)]
            if not sources:
                return stop('history_binding_missing')
            if not any((fact := read.evaluate(s))['valid'] and fact['ownership_confirmed']
                       and expected['project'] in fact['projects'] for s in sources):
                return stop('history_binding_not_current')
            event_ids.add(row['event_id'])
    # A completed empty extraction is explicit evidence, but cannot prove the
    # requested summary update. No synthetic milestone is manufactured here.
    if not event_ids:
        return stop('history_events_empty')
    summary_row = conn.execute('SELECT revision FROM project_archives WHERE project=?', (expected['project'],)).fetchone()
    if summary_row is None or summary_row['revision'] < 1:
        return stop('project_summary_missing', 'pending')
    events = archives.events(expected['project'])
    if not event_ids <= {e['event_id'] for e in events}:
        return stop('summary_source_not_current')
    summary = render_summary(archives.display_name(expected['project']), events)
    proof.update(state='ready', reason='', covers_source=True,
                 summary={'project': expected['project'], 'revision': summary_row['revision'],
                          'sha256': hashlib.sha256(summary.encode()).hexdigest(), 'event_ids': sorted(event_ids)})
    return proof

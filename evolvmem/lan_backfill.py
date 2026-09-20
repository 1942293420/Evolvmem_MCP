"""Conservative continuation imports from idle, authenticated client archives."""
from dataclasses import asdict
import json
from pathlib import Path
import time

from evolvmem.codex_transcript import parse_transcript, same_client_workspace, workspace_details
from evolvmem.continuity_backfill import (
    _ParsedSession, _parse_events, _checkpoint_fields, _objective_matched_workstream,
    deterministic_workstream_id, MAX_LINE_BYTES,
)
from evolvmem.continuity_models import ContinuityError, ContinuityImportRequest, _TERMINAL_STATUSES
from evolvmem.lan_context import remote_context, validate_snapshot


def _import(capture, row):
    payload = capture.archiver.read_payload(row['archive_id'])
    if payload is None:
        return dict(code='candidate', reason='archive_payload_unavailable')
    raw = json.loads(payload)['transcript'].encode('utf-8')
    rows, _ = parse_transcript(raw, row['session_id'])
    details = workspace_details(rows)
    reason = details['attribution_reason']
    if details['subagent']:
        return dict(code='skipped', reason='subagent_session')
    if reason or not row['project']:
        return dict(code='candidate', reason=reason or 'project_unassigned')
    parsed = _ParsedSession(row['session_id'], '', [], {}, 0)
    events = [(i, json.loads(line)) for i, line in enumerate(raw.splitlines(), 1)
              if line.strip() and len(line) <= MAX_LINE_BYTES]
    _parse_events(Path('.'), parsed, events, same_workspace=same_client_workspace)
    if not parsed.users:
        return dict(code='skipped', reason='no_objective')
    with remote_context(capture.server, row['device_id'], validate_snapshot(None)) as provider:
        service = capture.server._continuity()
        fingerprint = provider.resolve(details['cwd']).fingerprint
        # Reuse only a unique existing binding; raw paths never register a project.
        project = service._resolve_project(fingerprint, '')
        if project != row['project']:
            return dict(code='candidate', reason='workspace_unbound')
        fields = _checkpoint_fields(parsed)
        workstream_id = deterministic_workstream_id(row['session_id'])
        existing = service._workstream_row(parsed.confirmed_id) if parsed.confirmed_id else None
        if existing is not None and (existing['project'] != project or existing['workspace_fingerprint'] != fingerprint):
            existing = None
        if existing is None:
            existing = _objective_matched_workstream(service, details['cwd'], project,
                fields['objective'], exclude_id=workstream_id)
        if existing is not None:
            return dict(code='terminal_kept' if existing['status'] in _TERMINAL_STATUSES else 'existing',
                        workstream_id=existing['id'], checkpoint_revision=existing['checkpoint_revision'])
        saved = capture.conn.execute('SELECT applied_revision FROM lan_session_backfills WHERE device_id=? AND session_id=?',
                                     (row['device_id'], row['session_id'])).fetchone()
        try:
            result = service.import_interrupted(ContinuityImportRequest(
                workspace_path=details['cwd'], project_hint=project, workstream_id=workstream_id,
                applied_revision=saved[0] if saved else 0, **fields))
        except ContinuityError as exc:
            return dict(code='candidate', reason=exc.code)
        if result.code in ('created', 'updated', 'unchanged'):
            with capture.store.transaction():
                capture.conn.execute('''INSERT INTO lan_session_backfills VALUES(?,?,?,?)
                    ON CONFLICT(device_id,session_id) DO UPDATE SET
                    applied_revision=excluded.applied_revision,workstream_id=excluded.workstream_id''',
                    (row['device_id'], row['session_id'], result.checkpoint_revision, result.workstream_id))
        return asdict(result)


def process_backfill(capture, *, now=None):
    """One current-snapshot version, idle for >=30 minutes. Caller holds dispatch lock."""
    row = capture.backfill_candidate(before=(time.time() if now is None else now) - 1800)
    if row is None:
        return 0
    try:
        result = _import(capture, row)
    except Exception:
        result = dict(code='candidate', reason='backfill_unavailable')
    with capture.store.transaction():
        capture.conn.execute('''UPDATE lan_session_uploads SET backfill_status=?,backfill_result=?
            WHERE device_id=? AND session_id=? AND sha256=?''',
            (result['code'], json.dumps(result), row['device_id'], row['session_id'], row['sha256']))
    return 1

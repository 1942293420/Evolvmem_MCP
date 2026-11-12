"""Reviewable, offline, date-only repair of mapped historical project events.

Default is a read-only dry run. Apply requires the saved plan and an unchanged
source snapshot; no model, source upload, reclassification or context rewrite.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from evolvmem.context_store import _now_iso
from evolvmem.project_archive_store import earliest_event_date, render_summary, source_progress_date

_VERSION = 1


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(plan):
    return hashlib.sha256(_json({k: v for k, v in plan.items() if k != 'plan_sha256'}).encode()).hexdigest()


def build_plan(conn, project: str) -> dict:
    if not isinstance(project, str) or not project.strip():
        raise ValueError('project_required')
    registry = conn.execute('SELECT * FROM context_project_registry WHERE project=?', (project,)).fetchone()
    if registry is None:
        raise ValueError('project_not_registered')
    changes, skipped = [], []
    rows = conn.execute('SELECT * FROM project_events WHERE project=? ORDER BY event_id', (project,)).fetchall()
    for event_row in rows:
        event = dict(event_row)
        reason = ''
        if event['status'] != 'active':
            reason = 'event_not_active'
        elif event['occurred_at'] is not None:
            reason = 'existing_occurrence_preserved'
        mappings = [dict(row) for row in conn.execute(
            'SELECT m.*,c.identity_key,c.occurred_at AS source_occurred_at,c.project AS source_project,'
            'c.status AS source_status,c.content_type FROM knowledge_restructure_map m '
            "JOIN context_items c ON c.id=m.legacy_item_id WHERE m.target_kind='event' AND m.target_id=? "
            'ORDER BY m.legacy_item_id', (str(event['event_id']),))]
        if not reason and not mappings:
            reason = 'no_legacy_mapping'
        dates = {source_progress_date(source['identity_key']) for source in mappings}
        if not reason and any(source['source_occurred_at'] is not None for source in mappings):
            reason = 'explicit_source_occurrence_requires_separate_review'
        if not reason and any(source['content_type'] not in {'session_summary', 'project_summary', 'workstream_checkpoint'} for source in mappings):
            reason = 'source_not_historical'
        if not reason and (not dates or None in dates):
            reason = 'no_verifiable_source_date'
        if not reason and len(dates) != 1:
            reason = 'conflicting_source_dates'
        if reason:
            skipped.append({'event_id': event['event_id'], 'reason': reason})
            continue
        changes.append({
            'event_id': event['event_id'], 'before': event,
            'new_occurred_at': next(iter(dates)), 'precision': 'date', 'timezone': None,
            'date_basis': 'legacy_progress_log_identity', 'sources': mappings,
        })
    archive_row = conn.execute('SELECT * FROM project_archives WHERE project=?', (project,)).fetchone()
    archive_before = dict(archive_row) if archive_row else None
    proposed = {c['event_id']: c['new_occurred_at'] for c in changes}
    events = [{**dict(row), 'occurred_at': proposed.get(row['event_id'], row['occurred_at'])}
              for row in rows if row['status'] == 'active']
    name = registry['display_name'] or project
    archive_after = {'display_name': name, 'started_at': earliest_event_date(events),
                     'summary': render_summary(name, events)} if archive_before and events else None
    plan = {'schema_version': _VERSION, 'project': project, 'mode': 'dry_run',
            'registry_before': dict(registry), 'changes': changes, 'skipped': skipped,
            'archive_before': archive_before, 'archive_after': archive_after}
    plan['plan_sha256'] = _digest(plan)
    return plan


def apply_plan(conn, plan: dict) -> dict:
    if (not isinstance(plan, dict) or plan.get('schema_version') != _VERSION
            or plan.get('mode') != 'dry_run' or plan.get('plan_sha256') != _digest(plan)):
        raise ValueError('invalid_repair_plan')
    if conn.in_transaction:
        raise ValueError('repair_requires_own_transaction')
    conn.execute('BEGIN IMMEDIATE')
    try:
        audit_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='project_history_date_repairs'").fetchone()
        if audit_exists and conn.execute('SELECT 1 FROM project_history_date_repairs WHERE plan_sha256=?', (plan['plan_sha256'],)).fetchone():
            conn.rollback()
            return {'mode': 'apply', 'applied': 0, 'already_applied': True, 'plan_sha256': plan['plan_sha256']}
        if build_plan(conn, plan['project']) != plan:
            raise ValueError('stale_repair_plan')
        if not plan['changes']:
            conn.rollback()
            return {'mode': 'apply', 'applied': 0, 'already_applied': False, 'plan_sha256': plan['plan_sha256']}
        conn.execute('CREATE TABLE IF NOT EXISTS project_history_date_repairs('
                     'plan_sha256 TEXT PRIMARY KEY,project TEXT NOT NULL,applied_at TEXT NOT NULL,plan_json TEXT NOT NULL)')
        now = _now_iso()
        conn.execute('INSERT INTO project_history_date_repairs VALUES(?,?,?,?)',
                     (plan['plan_sha256'], plan['project'], now, _json(plan)))
        for change in plan['changes']:
            cursor = conn.execute("UPDATE project_events SET occurred_at=?,updated_at=? WHERE event_id=? AND project=? AND occurred_at IS NULL AND status='active'",
                                  (change['new_occurred_at'], now, change['event_id'], plan['project']))
            if cursor.rowcount != 1:
                raise ValueError('stale_repair_plan')
        after = plan['archive_after']
        if after:
            conn.execute('UPDATE project_archives SET display_name=?,started_at=?,summary=?,revision=revision+1,updated_at=? WHERE project=?',
                         (after['display_name'], after['started_at'], after['summary'], now, plan['project']))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {'mode': 'apply', 'applied': len(plan['changes']), 'already_applied': False, 'plan_sha256': plan['plan_sha256']}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path)
    parser.add_argument('--project', required=True, help='Exact canonical project; never all projects')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--output', type=Path, help='Write the dry-run plan for review; must not already exist')
    group.add_argument('--apply-plan', type=Path, help='Apply exactly this saved, unchanged dry-run plan')
    args = parser.parse_args(argv)
    database = args.database.resolve(strict=True)
    conn = sqlite3.connect(database.as_uri() + ('?mode=rw' if args.apply_plan else '?mode=ro'), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if args.apply_plan:
            plan = json.loads(args.apply_plan.read_text(encoding='utf-8'))
            if plan.get('project') != args.project:
                raise ValueError('repair_project_mismatch')
            result = apply_plan(conn, plan)
        else:
            conn.execute('BEGIN')
            result = build_plan(conn, args.project)
            conn.rollback()
            if args.output:
                with args.output.open('x', encoding='utf-8') as stream:
                    json.dump(result, stream, ensure_ascii=False, indent=2)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

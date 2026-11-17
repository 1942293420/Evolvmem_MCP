"""Deterministic offline repair of missing current typed ownership provenance.

Default: read-only plan. Writes require --apply-plan or --rollback-plan and an
explicit existing database. No ContextStore initialization, models, raw archives,
project inference, old task replay, or existing resolution updates.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from evolvmem.project_models import ProjectResolutionDecision

_VERSION = 1
_RESOLVER_VERSION = 'typed-ownership-repair.v1'
_TYPES = ('workstream_checkpoint', 'project_summary')
_ITEM_COLUMNS = ('id', 'identity_key', 'content_type', 'project', 'scope',
                 'status', 'supersedes', 'superseded_by', 'created_at', 'updated_at')
_AUDIT_TABLE = 'typed_ownership_repairs'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _digest(plan):
    return hashlib.sha256(_json({k: v for k, v in plan.items() if k != 'plan_sha256'}).encode()).hexdigest()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _row(conn, sql, params=()):
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row is not None else None


def _candidate(conn, item_id, *, ignore_resolution=False):
    """Read current authority only; return a complete CAS precondition or reason."""
    item = _row(conn, f"SELECT {','.join(_ITEM_COLUMNS)} FROM context_items WHERE id=?", (item_id,))
    if item is None:
        return None, 'item_missing'
    if item['content_type'] not in _TYPES:
        return None, 'unsupported_type'
    if not ignore_resolution and _row(conn, 'SELECT item_id FROM context_project_resolutions WHERE item_id=?', (item_id,)):
        return None, 'existing_resolution_preserved'
    if item['status'] != 'active':
        return None, 'item_not_active'
    if item['superseded_by'] is not None:
        return None, 'item_superseded'
    if item['scope'] != 'project':
        return None, 'non_project_scope'
    if not item['project']:
        return None, 'empty_project'
    pointers = []
    for table in ('continuity_workstreams', 'context_project_rollups'):
        pointers.extend((table, dict(row)) for row in conn.execute(
            f'SELECT * FROM {table} WHERE current_context_id=?', (item_id,)))
    if not pointers:
        return None, 'missing_current_pointer'
    if len(pointers) != 1:
        return None, 'ambiguous_current_pointer'
    table, source = pointers[0]
    expected_table = 'continuity_workstreams' if item['content_type'] == 'workstream_checkpoint' else 'context_project_rollups'
    if table != expected_table:
        return None, 'pointer_type_mismatch'
    if source['project'] != item['project']:
        return None, 'project_mismatch'
    if table == 'context_project_rollups' and source['status'] not in {'ready', 'vector_dirty'}:
        return None, 'source_not_ready'
    registry = _row(conn, 'SELECT * FROM context_project_registry WHERE project=?', (item['project'],))
    if registry is None:
        return None, 'project_not_registered'
    if registry['status'] != 'active':
        return None, 'project_not_active'
    # An exact canonical registry key is required. Aliases and free text are
    # deliberately not used; checkpoint/rollup projects must already agree.
    return {
        'item_id': item_id, 'project': item['project'],
        'expected_resolution_revision': 0, 'resolution_before': None,
        'item_before': item, 'source_table': table, 'source_before': source,
        'registry_before': registry,
    }, ''


def build_plan(conn) -> dict:
    """SELECT-only; callers should use a read transaction for a stable snapshot."""
    changes, skipped = [], []
    ids = conn.execute('SELECT id FROM context_items WHERE content_type IN (?,?) ORDER BY id', _TYPES).fetchall()
    for row in ids:
        change, reason = _candidate(conn, row['id'])
        if change is None:
            skipped.append({'item_id': row['id'], 'reason': reason})
        else:
            changes.append(change)
    experiences = conn.execute(
        "SELECT count(*) FROM context_items c LEFT JOIN context_project_resolutions r ON r.item_id=c.id "
        "WHERE c.content_type='experience' AND c.status='active' AND r.item_id IS NULL").fetchone()[0]
    summary = {
        'scanned_typed_count': len(ids), 'eligible_count': len(changes),
        'by_type': dict(sorted(Counter(c['item_before']['content_type'] for c in changes).items())),
        'skipped_by_reason': dict(sorted(Counter(c['reason'] for c in skipped).items())),
        'unresolved_experience_count': experiences,
    }
    plan = {'schema_version': _VERSION, 'repair_kind': 'missing_current_typed_ownership',
            'resolver_version': _RESOLVER_VERSION, 'mode': 'dry_run',
            'changes': changes, 'skipped': skipped, 'summary': summary}
    plan['plan_sha256'] = _digest(plan)
    return plan


def _validate_plan(plan):
    if (not isinstance(plan, dict) or plan.get('schema_version') != _VERSION
            or plan.get('repair_kind') != 'missing_current_typed_ownership'
            or plan.get('resolver_version') != _RESOLVER_VERSION
            or plan.get('mode') != 'dry_run' or plan.get('plan_sha256') != _digest(plan)
            or not isinstance(plan.get('changes'), list)):
        raise ValueError('invalid_repair_plan')
    ids = [c.get('item_id') for c in plan['changes'] if isinstance(c, dict)]
    if (len(ids) != len(plan['changes']) or any(type(i) is not int or i < 1 for i in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError('invalid_repair_plan')


def _audit(conn, plan):
    exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_AUDIT_TABLE,)).fetchone()
    if not exists:
        return None
    row = _row(conn, f'SELECT * FROM {_AUDIT_TABLE} WHERE plan_sha256=?', (plan['plan_sha256'],))
    if row is not None and json.loads(row['plan_json']) != plan:
        raise ValueError('repair_audit_mismatch')
    return row


def _resolution(change, now):
    source = change['source_before']
    is_checkpoint = change['source_table'] == 'continuity_workstreams'
    version = (f"checkpoint:{source['checkpoint_revision']}:state:{source['state_version']}"
               if is_checkpoint else str(source['revision']))
    source_id = source['id'] if is_checkpoint else source['project']
    evidence = ({'source': f"{change['source_table']}:{source_id}",
                 'type': 'typed_current_pointer', 'source_version': version,
                 'normalized_value': change['project']},)
    # Reuse the same model/field contract as record_trusted_resolution; do not
    # call its upsert, which could overwrite a preexisting human resolution.
    decision = ProjectResolutionDecision.resolved(
        change['project'], 'explicit_project', _RESOLVER_VERSION, evidence)
    return {'item_id': change['item_id'], 'resolution_state': decision.state.value,
            'decision_source': 'automatic', 'review_state': 'not_required',
            'proposed_project': decision.proposed_project, 'resolved_project': decision.resolved_project,
            'confidence': decision.confidence, 'method': decision.method,
            'evidence_json': _json(list(decision.evidence)), 'resolver_version': decision.resolver_version,
            'revision': 1, 'reviewed_at': None, 'created_at': now, 'updated_at': now}


def apply_plan(conn, plan: dict) -> dict:
    """Insert only absent resolution rows, atomically with full before/after audit."""
    _validate_plan(plan)
    if conn.in_transaction:
        raise ValueError('repair_requires_own_transaction')
    result = {'mode': 'apply', 'applied': 0, 'already_applied': False, 'plan_sha256': plan['plan_sha256']}
    conn.execute('BEGIN IMMEDIATE')
    try:
        audit = _audit(conn, plan)
        if audit:
            if audit['rolled_back_at'] is not None:
                raise ValueError('repair_previously_rolled_back')
            conn.rollback()
            return {**result, 'already_applied': True}
        for change in plan['changes']:
            current, _ = _candidate(conn, change['item_id'])
            if current != change:
                raise ValueError('stale_repair_plan')
        if not plan['changes']:
            conn.rollback()
            return result
        conn.execute(f'CREATE TABLE IF NOT EXISTS {_AUDIT_TABLE}('
                     'plan_sha256 TEXT PRIMARY KEY,applied_at TEXT NOT NULL,plan_json TEXT NOT NULL,'
                     'before_json TEXT NOT NULL,after_json TEXT NOT NULL,rolled_back_at TEXT)')
        now = _now()
        after = []
        for change in plan['changes']:
            row = _resolution(change, now)
            columns = ','.join(row)
            placeholders = ','.join('?' for _ in row)
            conn.execute(f'INSERT INTO context_project_resolutions({columns}) VALUES({placeholders})', tuple(row.values()))
            # Actual persisted values are audited, including any database trigger
            # effects. Existing rows cannot be updated by this insert-only path.
            actual = _row(conn, 'SELECT * FROM context_project_resolutions WHERE item_id=?', (change['item_id'],))
            if actual != row:
                raise ValueError('unexpected_resolution_write')
            after.append(actual)
        conn.execute(f'INSERT INTO {_AUDIT_TABLE} VALUES(?,?,?,?,?,NULL)',
                     (plan['plan_sha256'], now, _json(plan), _json([None] * len(after)), _json(after)))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {**result, 'applied': len(after)}


def rollback_plan(conn, plan: dict) -> dict:
    """Remove only this repair's unchanged rows; preserve audit and later edits."""
    _validate_plan(plan)
    if conn.in_transaction:
        raise ValueError('repair_requires_own_transaction')
    result = {'mode': 'rollback', 'rolled_back': 0, 'already_rolled_back': False,
              'plan_sha256': plan['plan_sha256']}
    conn.execute('BEGIN IMMEDIATE')
    try:
        audit = _audit(conn, plan)
        if audit is None:
            raise ValueError('repair_not_applied')
        if audit['rolled_back_at'] is not None:
            conn.rollback()
            return {**result, 'already_rolled_back': True}
        after = json.loads(audit['after_json'])
        if len(after) != len(plan['changes']) or json.loads(audit['before_json']) != [None] * len(after):
            raise ValueError('repair_audit_mismatch')
        for change, expected in zip(plan['changes'], after):
            current, _ = _candidate(conn, change['item_id'], ignore_resolution=True)
            actual = _row(conn, 'SELECT * FROM context_project_resolutions WHERE item_id=?', (change['item_id'],))
            if current != change or actual != expected or expected != _resolution(change, audit['applied_at']):
                raise ValueError('rollback_conflict')
        for change in plan['changes']:
            cursor = conn.execute('DELETE FROM context_project_resolutions WHERE item_id=? AND revision=1 AND resolver_version=?',
                                  (change['item_id'], _RESOLVER_VERSION))
            if cursor.rowcount != 1:
                raise ValueError('rollback_conflict')
        conn.execute(f'UPDATE {_AUDIT_TABLE} SET rolled_back_at=? WHERE plan_sha256=?', (_now(), plan['plan_sha256']))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {**result, 'rolled_back': len(after)}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, type=Path, help='Explicit existing SQLite file; no configured default')
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--output', type=Path, help='Save the read-only plan; file must not exist')
    group.add_argument('--apply-plan', type=Path, help='Apply exactly this saved plan with source CAS')
    group.add_argument('--rollback-plan', type=Path, help='Revert this applied plan only when every repaired row and source is unchanged')
    args = parser.parse_args(argv)
    database = args.database.resolve(strict=True)
    writing = args.apply_plan is not None or args.rollback_plan is not None
    conn = sqlite3.connect(database.as_uri() + ('?mode=rw' if writing else '?mode=ro'), uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA foreign_keys=ON')
        if writing:
            plan = json.loads((args.apply_plan or args.rollback_plan).read_text(encoding='utf-8'))
            result = apply_plan(conn, plan) if args.apply_plan else rollback_plan(conn, plan)
        else:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            result = build_plan(conn)
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

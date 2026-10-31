"""Offline provenance backfill; default is a read-only plan, never inference."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from evolvmem.capability_provenance import ProvenanceRead, ReadStore, _row, _has
from evolvmem.typed_ownership_repair import _digest


_VERSION = 1
_AUDIT = 'capability_provenance_repairs'


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))


def _source_snapshot(read, source):
    if source['source_kind'] == 'unit':
        task, units = read.unit(source['task_id'], source['unit_digest'])
        revision, reason = read.source_revision(task['source_key']) if task else ('', 'task_missing')
        return {'task': task, 'units': units, 'current_source_revision': revision, 'source_reason': reason}
    item_id = source['context_item_id']
    item = _row(read.conn, 'SELECT id,identity_key,content_type,project,projects,scope,status,superseded_by,updated_at,success_count,expires_at,effective_from,effective_until FROM context_items WHERE id=?', (item_id,))
    resolution = _row(read.conn, 'SELECT * FROM context_project_resolutions WHERE item_id=?', (item_id,))
    return {'item': item, 'resolution': resolution, 'mapping': source.get('mapping_before')}


def build_plan(conn):
    read = ProvenanceRead(ReadStore(conn))
    changes, inventory = [], []
    rows = conn.execute('SELECT memory_id,status,source_ref,source_project,fingerprint,revision,updated_at FROM capability_memories ORDER BY memory_id')
    for row in rows:
        memory = dict(row)
        fact, sources = read.memory(memory)
        inventory.append(fact)
        if not fact['legacy_fallback']:
            continue  # Never replace an existing explicit binding.
        for source, verdict in zip(sources, fact['sources']):
            if source.get('source_id') is not None:
                continue  # Mixed legacy/explicit memories only need missing origins.
            if source['source_kind'] not in {'context', 'unit'}:
                continue  # Ambiguous/historical-only refs remain visible in inventory.
            binding = {'memory_id': memory['memory_id'], 'source_kind': source['source_kind'],
                       'source_key': source['source_key'], 'source_ref': source['source_ref'],
                       'source_revision': source.get('source_revision', ''),
                       'context_item_id': source.get('context_item_id'), 'task_id': source.get('task_id'),
                       'unit_digest': source.get('unit_digest', ''), 'unit_revision': source.get('unit_revision'),
                       'projects': _json(verdict['projects']), 'knowledge_scope': verdict['knowledge_scope'],
                       'evidence_status': 'confirmed' if verdict['ownership_confirmed'] else 'unverified'}
            changes.append({'memory_before': memory, 'source_before': _source_snapshot(read, source),
                            'expected_binding_revision': 0, 'binding': binding})
    summary = {'memory_count': len(inventory), 'eligible_count': sum(r['eligible'] for r in inventory),
               'ownership_confirmed_count': sum(r['ownership_confirmed'] for r in inventory),
               'proposed_bindings': len(changes), 'by_kind': dict(Counter(c['binding']['source_kind'] for c in changes)),
               'invalid_reasons': dict(sorted(Counter(s['reason'] for r in inventory for s in r['sources'] if not s['valid']).items()))}
    # Registry metadata is deliberately included, not merely current project
    # labels, so even an intervening rename/revision is a reviewable CAS conflict.
    registry = [dict(r) for r in conn.execute('SELECT * FROM context_project_registry ORDER BY project')]
    plan = {'schema_version': _VERSION, 'repair_kind': 'capability_provenance', 'mode': 'dry_run',
            'changes': changes, 'inventory': inventory, 'summary': summary, 'registry_before': registry}
    plan['plan_sha256'] = _digest(plan)
    return plan


def _validate(plan):
    if (not isinstance(plan, dict) or plan.get('schema_version') != _VERSION
            or plan.get('repair_kind') != 'capability_provenance' or plan.get('mode') != 'dry_run'
            or plan.get('plan_sha256') != _digest(plan)):
        raise ValueError('invalid_repair_plan')


def apply_plan(conn, plan):
    _validate(plan)
    if conn.in_transaction:
        raise ValueError('repair_requires_own_transaction')
    if not _has(conn, 'capability_sources'):
        raise ValueError('capability_sources_schema_required')
    result = {'mode': 'apply', 'applied': 0, 'already_applied': False, 'plan_sha256': plan['plan_sha256']}
    conn.execute('BEGIN IMMEDIATE')
    try:
        audit = _row(conn, f'SELECT * FROM {_AUDIT} WHERE plan_sha256=?', (plan['plan_sha256'],)) if _has(conn, _AUDIT) else None
        if audit:
            if json.loads(audit['plan_json']) != plan:
                raise ValueError('repair_audit_mismatch')
            if audit.get('rolled_back_at') is not None:
                raise ValueError('repair_previously_rolled_back')
            conn.rollback()
            return {**result, 'already_applied': True}
        if build_plan(conn) != plan:
            raise ValueError('stale_repair_plan')
        if not plan['changes']:
            conn.rollback()
            return result
        conn.execute(f'CREATE TABLE IF NOT EXISTS {_AUDIT}(plan_sha256 TEXT PRIMARY KEY,applied_at TEXT NOT NULL,plan_json TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,rolled_back_at TEXT)')
        now = datetime.now(timezone.utc).isoformat(timespec='microseconds')
        after = []
        for change in plan['changes']:
            binding = {**change['binding'], 'created_at': now, 'updated_at': now, 'revision': 1}
            columns = ','.join(binding)
            cursor = conn.execute(f"INSERT INTO capability_sources({columns}) VALUES({','.join('?' for _ in binding)})", tuple(binding.values()))
            actual = _row(conn, 'SELECT * FROM capability_sources WHERE source_id=?', (cursor.lastrowid,))
            if {key: value for key, value in actual.items() if key != 'source_id'} != binding:
                raise ValueError('unexpected_binding_write')
            after.append(actual)
        conn.execute(f'INSERT INTO {_AUDIT} VALUES(?,?,?,?,?,NULL)', (plan['plan_sha256'], now, _json(plan), _json([None] * len(after)), _json(after)))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {**result, 'applied': len(after)}


def rollback_plan(conn, plan):
    """CAS-delete only this batch's unchanged source bindings, never memories."""
    _validate(plan)
    if conn.in_transaction:
        raise ValueError('repair_requires_own_transaction')
    result = {'mode': 'rollback', 'rolled_back': 0, 'already_rolled_back': False,
              'plan_sha256': plan['plan_sha256']}
    conn.execute('BEGIN IMMEDIATE')
    try:
        audit = _row(conn, f'SELECT * FROM {_AUDIT} WHERE plan_sha256=?', (plan['plan_sha256'],)) if _has(conn, _AUDIT) else None
        if audit is None:
            raise ValueError('repair_not_applied')
        if json.loads(audit['plan_json']) != plan:
            raise ValueError('repair_audit_mismatch')
        if 'rolled_back_at' not in audit:
            raise ValueError('repair_audit_schema_required')
        if audit['rolled_back_at'] is not None:
            conn.rollback()
            return {**result, 'already_rolled_back': True}
        after = json.loads(audit['after_json'])
        if len(after) != len(plan['changes']) or json.loads(audit['before_json']) != [None] * len(after):
            raise ValueError('repair_audit_mismatch')
        registry = [dict(r) for r in conn.execute('SELECT * FROM context_project_registry ORDER BY project')]
        if registry != plan['registry_before']:
            raise ValueError('rollback_conflict')
        read = ProvenanceRead(ReadStore(conn))
        for change, expected in zip(plan['changes'], after):
            binding = change['binding']
            actual = _row(conn, 'SELECT * FROM capability_sources WHERE source_id=?', (expected['source_id'],))
            intended = {**binding, 'source_id': expected['source_id'], 'created_at': audit['applied_at'],
                        'updated_at': audit['applied_at'], 'revision': 1}
            memory = _row(conn, 'SELECT memory_id,status,source_ref,source_project,fingerprint,revision,updated_at FROM capability_memories WHERE memory_id=?', (binding['memory_id'],))
            source = dict(binding)
            if binding['source_kind'] == 'context':
                source['mapping_before'] = _row(conn,
                    "SELECT * FROM knowledge_restructure_map WHERE legacy_item_id=? AND target_kind='capability' AND target_id=?",
                    (binding['context_item_id'], str(binding['memory_id'])))
            if (actual != expected or expected != intended or memory != change['memory_before']
                    or _source_snapshot(read, source) != change['source_before']):
                raise ValueError('rollback_conflict')
        for expected in after:
            cursor = conn.execute('DELETE FROM capability_sources WHERE source_id=? AND memory_id=? AND revision=?',
                                  (expected['source_id'], expected['memory_id'], expected['revision']))
            if cursor.rowcount != 1:
                raise ValueError('rollback_conflict')
        conn.execute(f'UPDATE {_AUDIT} SET rolled_back_at=? WHERE plan_sha256=?',
                     (datetime.now(timezone.utc).isoformat(timespec='microseconds'), plan['plan_sha256']))
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return {**result, 'rolled_back': len(after)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--output', type=Path)
    group.add_argument('--apply-plan', type=Path)
    group.add_argument('--rollback-plan', type=Path)
    args = parser.parse_args(argv)
    path = args.database.resolve(strict=True)
    writing = args.apply_plan is not None or args.rollback_plan is not None
    conn = sqlite3.connect(path.as_uri() + ('?mode=rw' if writing else '?mode=ro'), uri=True)
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

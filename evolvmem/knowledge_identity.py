"""Stable, source-backed knowledge identity, independent of segmentation runs.

Only explicit human decisions are reusable. Same-source coverage is exact;
cross-source reuse is limited to verbatim facts naming their confirmed project.
No title, model summary, similarity score or processing batch grants ownership.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from evolvmem import knowledge_scope as ks, topic_segmentation
from evolvmem.context_store import _now_iso

SCHEMA = (
    """CREATE TABLE IF NOT EXISTS knowledge_objects (
        knowledge_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL,
        category TEXT NOT NULL, item_id INTEGER, history_json TEXT NOT NULL DEFAULT '{}',
        decision_json TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 0,
        retired INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        parent_id TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE INDEX IF NOT EXISTS idx_knowledge_fingerprint
        ON knowledge_objects(fingerprint, retired)""",
    """CREATE TABLE IF NOT EXISTS knowledge_source_bindings (
        knowledge_id TEXT NOT NULL REFERENCES knowledge_objects(knowledge_id),
        source_key TEXT NOT NULL, source_start INTEGER NOT NULL, source_end INTEGER NOT NULL,
        quote TEXT NOT NULL, quote_hash TEXT NOT NULL, task_id INTEGER NOT NULL,
        snapshot_hash TEXT NOT NULL, created_at TEXT NOT NULL, approved INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(knowledge_id, source_key, source_start, source_end, quote_hash)
    )""",
    """CREATE INDEX IF NOT EXISTS idx_knowledge_binding_source
        ON knowledge_source_bindings(source_key)""",
    """CREATE TABLE IF NOT EXISTS knowledge_decisions (
        knowledge_id TEXT NOT NULL REFERENCES knowledge_objects(knowledge_id),
        revision INTEGER NOT NULL, decision_json TEXT NOT NULL, coverage_json TEXT NOT NULL,
        task_id INTEGER NOT NULL, unit_digest TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(knowledge_id, revision)
    )""",
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _hash(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def _object(service, knowledge_id):
    row = service.store._connection().execute(
        'SELECT * FROM knowledge_objects WHERE knowledge_id=? AND retired=0',
        (knowledge_id,)).fetchone()
    return dict(row) if row else None


def _parts(service, task):
    from evolvmem import conversation_tasks
    body = task['source_snapshot']
    if conversation_tasks.is_conversation(task['source_key']):
        current = conversation_tasks.snapshot(service, task['source_key'])
        if not current['body'].startswith(body):
            return []
        return current['records']
    return [{'source_key': task['source_key'], 'start': 0, 'end': len(body), 'text': body}]


def _anchors(task, unit, parts):
    body = task['source_snapshot']
    ranges = topic_segmentation.unit_ranges(unit) or [[unit['source_start'], unit['source_end']]]
    # Do not turn a model-written body or stale source snapshot into evidence.
    if '\n\n'.join(body[a:b] for a, b in ranges).strip() != unit['text'].strip():
        return []
    result = []
    for part in parts:
        for start, end in ranges:
            a, b = max(start, part['start']), min(end, part['end'])
            if a < b and body[a:b].strip():
                result.append({'key': part['source_key'], 'start': a-part['start'],
                               'end': b-part['start'], 'quote': body[a:b]})
    return result


def _bind(service, task, unit, knowledge_id, anchors, *, approved=False):
    conn = service.store._connection()
    conn.execute('UPDATE organization_units SET knowledge_id=? WHERE task_id=? AND digest=?',
                 (knowledge_id, task['id'], unit['digest']))
    for a in anchors:
        conn.execute(
            'INSERT INTO knowledge_source_bindings(knowledge_id,source_key,source_start,source_end,quote,'
            'quote_hash,task_id,snapshot_hash,created_at,approved) VALUES(?,?,?,?,?,?,?,?,?,?) '
            'ON CONFLICT(knowledge_id,source_key,source_start,source_end,quote_hash) '
            'DO UPDATE SET approved=MAX(approved,excluded.approved)',
            (knowledge_id, a['key'], a['start'], a['end'], a['quote'], _hash(a['quote']),
             task['id'], _hash(task['source_snapshot']), _now_iso(), int(approved)))


def _new(service, unit):
    kid = 'kn_' + uuid.uuid4().hex
    service.store._connection().execute(
        'INSERT INTO knowledge_objects(knowledge_id,fingerprint,category,created_at,updated_at) '
        'VALUES(?,?,?,?,?)', (kid, _hash(unit['text'].strip()), unit['category'], _now_iso(), _now_iso()))
    return kid


def _decision(unit):
    return {**ks.fields(unit), 'disposition': unit['disposition'],
            'action': ('defer' if unit['disposition'] == 'set_aside' else
                       'include' if unit['decision'] == 'manual' and ks.assigned(unit) else 'review')}


def _remember(service, task, unit, anchors):
    conn = service.store._connection()
    kid = unit.get('knowledge_id')
    # A split retired the aggregate deliberately. Its historical unit rows
    # remain for provenance; they must never bootstrap a new authority.
    if kid and not _object(service, kid):
        return None
    if not kid:
        kid = _new(service, unit)
        _bind(service, task, unit, kid, anchors)
    saved = _object(service, kid)
    if not saved['decision_json'] or saved['decision_json'] == '{}':
        if unit['decision'] == 'manual' or unit.get('disposition_reason') == '用户选择暂不归档':
            record_decision(service, task, {**unit, 'knowledge_id': kid}, anchors=anchors)
    if unit.get('item_id') and not saved['item_id']:
        conn.execute('UPDATE knowledge_objects SET item_id=? WHERE knowledge_id=?',
                     (unit['item_id'], kid))
    return kid


def capture_existing(service, task):
    """Backfill only the source currently being processed, before units are replaced."""
    conn = service.store._connection()
    parts = _parts(service, task)
    keys = list(dict.fromkeys([task['source_key'], *(p['source_key'] for p in parts)]))
    marks = ','.join('?' for _ in keys)
    rows = conn.execute(
        'SELECT u.*,t.source_key,t.source_snapshot FROM organization_units u '
        'JOIN organization_tasks t ON t.id=u.task_id WHERE t.source_key IN ('+marks+') '
        "AND (u.knowledge_id='' OR u.decision='manual' OR u.disposition='set_aside') "
        'ORDER BY t.id,u.ordinal', keys).fetchall()
    for raw in rows:
        unit = dict(raw)
        old_task = {'id': unit['task_id'], 'source_key': unit['source_key'],
                    'source_snapshot': unit['source_snapshot']}
        old_parts = (parts if old_task['source_key'] == task['source_key']
                     and task['source_snapshot'].startswith(old_task['source_snapshot'])
                     else _parts(service, old_task))
        anchors = _anchors(old_task, unit, old_parts)
        if anchors:
            _remember(service, old_task, unit, anchors)


def _covering(service, anchors, parts):
    conn = service.store._connection()
    texts = {p['source_key']: p['text'] for p in parts}
    candidates = None
    for a in anchors:
        matches = set()
        for raw in conn.execute(
                'SELECT b.*,t.source_snapshot FROM knowledge_source_bindings b '
                'JOIN knowledge_objects k ON k.knowledge_id=b.knowledge_id AND k.retired=0 '
                'JOIN organization_tasks t ON t.id=b.task_id WHERE b.source_key=?', (a['key'],)):
            row = dict(raw)
            if _hash(row['source_snapshot']) != row['snapshot_hash']:
                continue
            body, quote = texts[a['key']], row['quote']
            pos = row['source_start']
            if body[pos:pos+len(quote)] != quote:
                if body.count(quote) != 1:
                    continue
                pos = body.index(quote)
            if pos <= a['start'] and a['end'] <= pos+len(quote):
                matches.add(row['knowledge_id'])
        candidates = matches if candidates is None else candidates & matches
    return candidates or set()


def _verbatim_fact(service, task, unit):
    if unit['category'] not in ('environment', 'decision', 'project_convention', 'habit'):
        return set()
    conn = service.store._connection()
    # Lazily adopt exact legacy manual records too. No model call, source
    # rewrite or global historical reprocessing is needed for this upgrade.
    for raw in conn.execute(
            'SELECT u.*,t.source_key,t.source_snapshot FROM organization_units u '
            "JOIN organization_tasks t ON t.id=u.task_id WHERE u.knowledge_id='' "
            "AND u.decision='manual' AND u.text=?", (unit['text'],)).fetchall():
        old = dict(raw)
        old_task = {'id': old['task_id'], 'source_key': old['source_key'],
                    'source_snapshot': old['source_snapshot']}
        anchors = _anchors(old_task, old, _parts(service, old_task))
        if anchors:
            _remember(service, old_task, old, anchors)
    matches = set()
    for raw in conn.execute(
            "SELECT * FROM knowledge_objects WHERE fingerprint=? AND retired=0 AND decision_json!='{}'",
            (_hash(unit['text'].strip()),)):
        row = dict(raw)
        decision = json.loads(row['decision_json'])
        saved = conn.execute('SELECT d.coverage_json,t.source_snapshot FROM knowledge_decisions d '
                             'JOIN organization_tasks t ON t.id=d.task_id WHERE d.knowledge_id=? AND d.revision=?',
                             (row['knowledge_id'], row['revision'])).fetchone()
        if not saved:
            continue
        coverage = json.loads(saved['coverage_json'])
        if _hash(saved['source_snapshot']) != coverage['snapshot_hash']:
            continue
        covered_text = '\n\n'.join(a['quote'] for a in coverage['anchors'])
        if _hash(covered_text.strip()) != row['fingerprint']:
            continue
        projects = ks.projects(decision)
        if not projects or decision['action'] != 'include':
            continue
        if unit.get('project_hint') and unit['project_hint'] not in projects:
            continue
        # Exact project names in the fact are necessary: a generic instruction
        # repeated in two unrelated projects is not the same knowledge.
        if not all(re.search(r'(?<![a-z0-9_-])'+re.escape(p.casefold())+r'(?![a-z0-9_-])',
                             unit['text'].casefold()) for p in projects):
            continue
        archive = (service.store.get_session_archive(int(task['source_key'].split(':')[1]))
                   if task['source_key'].startswith('archive:') else None)
        if archive and archive['project'] and archive['project'] not in projects:
            continue
        matches.add(row['knowledge_id'])
    return matches


def _decision_covers(service, task, obj, anchors):
    parts = {p['source_key']: p['text'] for p in _parts(service, task)}
    covered = {}
    rows = service.store._connection().execute(
        'SELECT b.*,t.source_snapshot FROM knowledge_source_bindings b '
        'JOIN organization_tasks t ON t.id=b.task_id WHERE b.knowledge_id=? AND b.approved=1',
        (obj['knowledge_id'],))
    for old in rows:
        if _hash(old['source_snapshot']) != old['snapshot_hash']:
            continue
        body = parts.get(old['source_key'], '')
        pos, quote = old['source_start'], old['quote']
        if body[pos:pos+len(quote)] != quote:
            if not quote or body.count(quote) != 1:
                continue
            pos = body.index(quote)
        covered.setdefault(old['source_key'], []).append([pos, pos+len(quote)])
    from evolvmem.conversation_tasks import _covered
    return bool(anchors) and all(_covered(parts[a['key']], [[a['start'], a['end']]],
                                         sorted(covered.get(a['key'], []))) for a in anchors)


def _excluded(service, unit, decision):
    from evolvmem import organization_exclusions
    conn = service.store._connection()
    for row in conn.execute(
            'SELECT u.*,t.source_key,t.source_revision,t.rule_revision FROM organization_units u '
            'JOIN organization_tasks t ON t.id=u.task_id WHERE u.knowledge_id=?',
            (unit['knowledge_id'],)):
        if organization_exclusions.active(service, dict(row), decision):
            return True
    return False


def _apply(service, task, unit, obj, anchors, *, verified=False):
    conn = service.store._connection()
    _bind(service, task, unit, obj['knowledge_id'], anchors)
    decision = json.loads(obj['decision_json'])
    if not decision or decision['action'] == 'review':
        return
    if not verified and not _decision_covers(service, task, obj, anchors):
        return
    if _excluded(service, {**unit, 'knowledge_id': obj['knowledge_id']}, decision):
        return
    item = (conn.execute('SELECT status FROM context_items WHERE id=?', (obj['item_id'],)).fetchone()
            if obj['item_id'] else None)
    if item and item['status'] in ('deleted', 'archived', 'rejected', 'superseded'):
        return
    _bind(service, task, unit, obj['knowledge_id'], anchors, approved=True)
    conn.execute(
        "UPDATE organization_units SET decision='manual',project=?,projects=?,knowledge_scope=?,"
        "disposition=?,disposition_reason=?,item_id=?,revision=revision+1 WHERE task_id=? AND digest=?",
        (decision['project'], _json(decision['projects']), decision['knowledge_scope'],
         decision['disposition'], '用户选择暂不归档' if decision['action']=='defer' else '',
         obj['item_id'], task['id'], unit['digest']))


def bind_task(service, task):
    parts = _parts(service, task)
    conn = service.store._connection()
    for raw in conn.execute('SELECT * FROM organization_units WHERE task_id=?', (task['id'],)).fetchall():
        unit = dict(raw)
        anchors = _anchors(task, unit, parts)
        matches = _covering(service, anchors, parts)
        # Splitting an unreviewed aggregate must not let approval of one piece
        # approve its siblings through the aggregate's identity.
        matches = {k for k in matches if _object(service, k)['decision_json'] != '{}'
                   or _object(service, k)['fingerprint'] == _hash(unit['text'].strip())}
        verbatim = False
        if not matches and anchors:
            matches = _verbatim_fact(service, task, unit)
            verbatim = len(matches) == 1
        kid = next(iter(matches)) if len(matches) == 1 else _new(service, unit)
        _apply(service, task, unit, _object(service, kid), anchors, verified=verbatim)


def record_decision(service, task, unit, *, anchors=None):
    """Called only from a human save (or a legacy manual-decision backfill)."""
    conn = service.store._connection()
    if 'source_snapshot' not in task:
        task = dict(conn.execute('SELECT * FROM organization_tasks WHERE id=?', (task['id'],)).fetchone())
    anchors = _anchors(task, unit, _parts(service, task)) if anchors is None else anchors
    unit = _split_changed_scope(service, task, unit, anchors)
    kid = unit.get('knowledge_id') or _new(service, unit)
    obj = _object(service, kid)
    decision = _decision(unit)
    value = _json(decision)
    covered = _decision_covers(service, task, obj, anchors)
    _bind(service, task, unit, kid, anchors, approved=decision['action'] != 'review')
    if obj['decision_json'] == value and covered:
        return
    revision = obj['revision'] + 1
    conn.execute('INSERT INTO knowledge_decisions VALUES(?,?,?,?,?,?,?)',
                 (kid, revision, value, _json({'anchors': anchors, 'snapshot_hash': _hash(task['source_snapshot'])}),
                  task['id'], unit['digest'], _now_iso()))
    conn.execute('UPDATE knowledge_objects SET decision_json=?,revision=?,updated_at=? WHERE knowledge_id=?',
                 (value, revision, _now_iso(), kid))
    affected = conn.execute(
        'SELECT task_id,digest FROM organization_units WHERE knowledge_id=? AND '
        "task_id IN (SELECT id FROM organization_tasks WHERE status!='superseded')", (kid,)).fetchall()
    conn.execute(
        "UPDATE organization_units SET extraction_stage=CASE WHEN project!=? OR projects!=? "
        "OR knowledge_scope!=? OR disposition!=? THEN 'pending' ELSE extraction_stage END,"
        "project=?,projects=?,knowledge_scope=?,decision=?,disposition=?,disposition_reason=?,"
        "revision=revision+1,context_basis='' WHERE knowledge_id=? AND "
        "task_id IN (SELECT id FROM organization_tasks WHERE status!='superseded')",
        (decision['project'], _json(decision['projects']), decision['knowledge_scope'], decision['disposition'],
         decision['project'], _json(decision['projects']), decision['knowledge_scope'],
         'review' if decision['action']=='review' else 'manual', decision['disposition'],
         '用户选择暂不归档' if decision['action']=='defer' else '', kid))
    from evolvmem import auto_organization as org, organization_context
    for row in affected:
        organization_context.clear_dependency(service, row['task_id'], row['digest'], conn=conn)
    for tid in {r['task_id'] for r in affected} - {task['id']}:
        org._refresh_task_state(service, tid)


def _whole_object(service, unit, obj, anchors):
    if _hash(unit['text'].strip()) == obj['fingerprint']:
        return True
    saved = service.store._connection().execute(
        'SELECT coverage_json FROM knowledge_decisions WHERE knowledge_id=? AND revision=?',
        (obj['knowledge_id'], obj['revision'])).fetchone()
    if not saved:
        return False
    original = json.loads(saved['coverage_json'])['anchors']
    return bool(original) and all(any(
        a['key'] == old['key'] and a['start'] <= old['start'] and old['end'] <= a['end']
        and a['quote'][old['start']-a['start']:old['end']-a['start']] == old['quote']
        for a in anchors) for old in original)


def _split_changed_scope(service, task, unit, anchors):
    """An explicit correction to a piece must never move its unsaved siblings."""
    obj = _object(service, unit.get('knowledge_id'))
    if not obj or obj['decision_json'] in ('{}', _json(_decision(unit))):
        return unit
    if _whole_object(service, unit, obj, anchors):
        return unit
    from evolvmem import auto_organization as org, unit_derivations
    conn = service.store._connection()
    rows = conn.execute(
        'SELECT u.* FROM organization_units u JOIN organization_tasks t ON t.id=u.task_id '
        "WHERE u.knowledge_id=? AND t.status!='superseded'", (obj['knowledge_id'],)).fetchall()
    conn.execute('UPDATE knowledge_objects SET retired=1 WHERE knowledge_id=?', (obj['knowledge_id'],))
    children = {}
    for raw in rows:
        piece = dict(raw)
        own_task = org._task_row(service, piece['task_id'])
        own_anchors = _anchors(own_task, piece, _parts(service, own_task))
        token = (_hash(piece['text'].strip()), _json(_decision(piece)))
        kid = children.get(token)
        if not kid:
            kid = children[token] = _new(service, piece)
            conn.execute('UPDATE knowledge_objects SET parent_id=? WHERE knowledge_id=?',
                         (obj['knowledge_id'], kid))
        _bind(service, own_task, piece, kid, own_anchors)
        conn.execute('UPDATE organization_units SET item_id=NULL,revision=revision+1 WHERE task_id=? AND digest=?',
                     (piece['task_id'], piece['digest']))
        if token[0] == obj['fingerprint']:
            # A full replica now contains pieces with different decisions.
            conn.execute(
                "UPDATE organization_units SET decision='review',project='',projects='[]',"
                "reason='部分内容的归属已修改，请按当前内容确认' WHERE task_id=? AND digest=?",
                (piece['task_id'], piece['digest']))
        elif (piece['task_id'], piece['digest']) != (unit['task_id'], unit['digest']):
            refreshed = org.find_unit(service, piece['task_id'], piece['digest'])
            record_decision(service, own_task, refreshed, anchors=own_anchors)
            if refreshed['decision'] == 'manual' and refreshed['disposition'] == 'keep' and ks.assigned(refreshed):
                org._ensure_unit_item(service, own_task, refreshed)
    if obj['item_id']:
        unit_derivations.supersede(service, obj['item_id'], '已按部分内容的处理决定拆开，原记录保留')
    return org.find_unit(service, unit['task_id'], unit['digest'])


def inherit(service, task, unit, original):
    """Reuse the identity only after the existing exact turn-coverage check passed."""
    kid = original.get('knowledge_id')
    obj = _object(service, kid) if kid else None
    if not obj:
        return
    if 'source_snapshot' not in task:
        task = dict(service.store._connection().execute(
            'SELECT * FROM organization_tasks WHERE id=?', (task['id'],)).fetchone())
    anchors = _anchors(task, unit, _parts(service, task))
    _apply(service, task, unit, obj, anchors, verified=True)


def history_item(service, unit):
    obj = _object(service, unit.get('knowledge_id'))
    if not obj or not obj['item_id']:
        return None
    row = service.store._connection().execute('SELECT status FROM context_items WHERE id=?',
                                              (obj['item_id'],)).fetchone()
    return obj['item_id'] if row and row['status'] == 'active' else None


def history_summary(service, unit, fallback):
    """The source-linked canonical summary stays stable through view rewording."""
    from dataclasses import asdict
    from evolvmem.legacy_models import LegacyExtractionItem
    obj = _object(service, unit.get('knowledge_id'))
    if not obj:
        return fallback
    saved = json.loads(obj['history_json'])
    # Legacy installations have an item before this identity layer exists.
    # A user may also have moved or edited that item since the last extraction.
    # Read its actual active payload; a new model summary must not replace it.
    item = service.store._connection().execute(
        "SELECT identity_key FROM context_items WHERE id=? AND status='active'", (obj['item_id'],)).fetchone()
    active = next((r for r in service._legacy_reader().get_by_key(item['identity_key'])
                   if r['status'] == 'active'), None) if item else None
    if active:
        payload = asdict(LegacyExtractionItem(
            key=active['key'], value=active['value'], attribute=active['attribute'],
            tags=tuple(filter(None, active['tags'].split(','))), importance=active['importance'],
            tier=active['tier'], expires_at=active.get('expires_at'), project_hint=unit['project']))
    elif saved:
        payload = saved
        payload['project_hint'] = unit['project']
    else:
        payload = asdict(fallback)
        payload['key'] = (f"project:{unit['project']}" if unit['project'] else 'user') + ':progress:log:' + obj['knowledge_id']
    payload['tags'] = tuple(payload['tags'])
    with service.store.transaction():
        service.store._connection().execute('UPDATE knowledge_objects SET history_json=? WHERE knowledge_id=?',
                                             (_json(payload), obj['knowledge_id']))
    return LegacyExtractionItem(**payload)


def attach_history(service, unit, item_id):
    if unit.get('knowledge_id'):
        with service.store.transaction():
            service.store._connection().execute('UPDATE knowledge_objects SET item_id=? WHERE knowledge_id=?',
                                                 (item_id, unit['knowledge_id']))

"""Read-only capability backing, separate from live project ownership.

Legacy relations are resolved without schema initialization. No claim in
capability_memories.source_project or capability_sources.projects is authority.
"""
from __future__ import annotations

import hashlib
import json
import re
from types import SimpleNamespace

from evolvmem import knowledge_scope as ks
from evolvmem.project_ownership import classify


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _row(conn, sql, args=()):
    row = conn.execute(sql, args).fetchone()
    return dict(row) if row is not None else None


def _has(conn, table):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


class ReadStore:
    """Minimal SELECT-only adapter, including for databases predating sources."""
    def __init__(self, conn):
        self.conn = conn

    def _connection(self):
        return self.conn

    def list_item_sources(self, item_id):
        return [dict(row) for row in self.conn.execute('SELECT * FROM context_sources WHERE item_id=?', (item_id,))]


def context_revision(conn, item_id):
    """Content identity only; ownership edits are independently read live."""
    row = _row(conn, 'SELECT id,content_type FROM context_items WHERE id=?', (item_id,))
    if row is None:
        return ''
    layers = [tuple(r) for r in conn.execute(
        'SELECT layer,content_hash FROM context_layers WHERE item_id=? ORDER BY layer', (item_id,))]
    return 'context-content-v1:' + _hash([row, layers])


class ProvenanceRead:
    """One request's caches; never retained across mutations or requests."""
    def __init__(self, store):
        self.store = store
        self.conn = store._connection()
        self.active_projects = {r[0] for r in self.conn.execute("SELECT project FROM context_project_registry WHERE status='active'")}
        self.sources_exist = _has(self.conn, 'capability_sources')
        # PRAGMA data_version tracks other connections; total_changes tracks
        # this connection. Do not share across an open transaction: total_changes
        # does not rewind on rollback/savepoint rollback, which could otherwise
        # let an uncommitted body's hash poison the next read.
        token = (id(self.conn), self.conn.execute('PRAGMA data_version').fetchone()[0], self.conn.total_changes)
        cached = getattr(store, '_capability_provenance_cache', None)
        if self.conn.in_transaction or not cached or cached['token'] != token or len(cached['revisions']) > 4096:
            cached = {'token': token, 'revisions': {}, 'visible': None}
            if not self.conn.in_transaction:
                store._capability_provenance_cache = cached
        self.cache = cached
        self.revisions = cached['revisions']
        self.contexts = {}
        self.units = {}
        self.legacy_units = {}
        self.bound_sources = None
        self.visible = cached['visible']

    def source_revision(self, key, visiting=()):
        if key in self.revisions:
            return self.revisions[key]
        if key in visiting:
            return '', 'source_cycle'
        saved = _row(self.conn, 'SELECT state,revision FROM knowledge_cleaning_reviews WHERE source_key=?', (key,))
        if saved and saved['state'] == 'deleted':
            result = ('', 'source_deleted')
        elif key.startswith('conversation:'):
            manifest = _row(self.conn, 'SELECT members FROM organization_conversations WHERE source_key=?', (key,))
            if not manifest:
                result = ('', 'conversation_missing')
            else:
                try:
                    members = json.loads(manifest['members'])
                    if not isinstance(members, list) or not members or any(not isinstance(k, str) for k in members):
                        raise ValueError()
                    values = []
                    reason = ''
                    for member in members:
                        revision, reason = self.source_revision(member, (*visiting, key))
                        if reason:
                            break
                        values.append([member, revision])
                    result = ('', reason) if reason else (self._review_revision(_hash(['complete-tasks-v1', values]), saved), '')
                except (ValueError, TypeError):
                    result = ('', 'invalid_conversation_manifest')
        elif re.fullmatch(r'archive:[1-9][0-9]*', key):
            identity = int(key.split(':')[1])
            archive = _row(self.conn, 'SELECT payload_sha256,state FROM session_archives WHERE id=?', (identity,))
            if not archive or archive['state'] != 'available':
                result = ('', 'archive_unavailable')
            else:
                if self.visible is None:
                    # Existing metadata-only current-version/system-exclusion
                    # oracle. It never decrypts or changes an archive.
                    from evolvmem.governance_sources import visible_ids
                    self.visible = visible_ids(SimpleNamespace(store=self.store))
                    self.cache['visible'] = self.visible
                clean = _row(self.conn, 'SELECT body FROM conversation_history WHERE archive_id=?', (identity,))
                if identity not in self.visible:
                    result = ('', 'archive_not_current')
                elif not clean or not str(clean['body'] or '').strip():
                    result = ('', 'source_body_unverifiable')
                else:
                    raw = _hash([archive['payload_sha256'], archive['state'], clean['body']])
                    result = (self._review_revision(raw, saved), '')
        elif re.fullmatch(r'item:[1-9][0-9]*', key):
            identity = int(key.split(':')[1])
            item = _row(self.conn, 'SELECT * FROM context_items WHERE id=?', (identity,))
            if not item or item['project'] or item['scope'] == 'global' or item['status'] not in {'active', 'candidate'}:
                result = ('', 'item_source_not_current')
            else:
                # Match KnowledgeBase.detail's editor revision exactly, without
                # constructing ContextService or calling a provider.
                from evolvmem.memory_learning import MemoryLearning
                layers = {r['layer']: r['content'] for r in self.conn.execute('SELECT layer,content FROM context_layers WHERE item_id=?', (identity,))}
                meta = _row(self.conn, 'SELECT * FROM knowledge_metadata WHERE item_id=?', (identity,))
                ownership = _row(self.conn, 'SELECT * FROM context_project_resolutions WHERE item_id=?', (identity,))
                learning = MemoryLearning(SimpleNamespace(store=self.store)).metadata(identity, item)
                revision_row = {k: v for k, v in item.items() if k not in ('access_count', 'last_accessed')}
                raw = _hash([revision_row, layers, meta, ownership, learning])
                managed = item['content_type'] in {'workstream_checkpoint', 'project_summary'} or bool(item.get('experience_payload'))
                body = (layers.get('l1') or layers.get('l0', '')) if managed else (layers.get('l2') or layers.get('l1') or layers.get('l0', ''))
                result = (self._review_revision(raw, saved), '') if body.strip() else ('', 'source_body_unverifiable')
        else:
            result = ('', 'source_revision_unverifiable')
        self.revisions[key] = result
        return result

    @staticmethod
    def _review_revision(raw, saved):
        return hashlib.sha256((raw + ':' + str(saved['revision'] if saved else 0)).encode()).hexdigest()

    def unit(self, task_id, digest):
        key = (task_id, digest)
        if key not in self.units:
            task = _row(self.conn, 'SELECT id,source_key,source_revision,rule_revision,status,superseded_by,error_code FROM organization_tasks WHERE id=?', (task_id,))
            units = [dict(r) for r in self.conn.execute(
                'SELECT task_id,ordinal,digest,revision,decision,disposition,project,projects,knowledge_scope,source_start,source_end,text,source_ranges '
                'FROM organization_units WHERE task_id=? AND digest=?', key)]
            from evolvmem.auto_organization import unit_digest
            for unit in units:
                from evolvmem.capability_source_proof import prove_unit_row
                unit['source_text_proven'] = bool(task and prove_unit_row(self.conn, task, unit)['valid'])
                # Never retain or export historical prose in the repair plan.
                text = unit.pop('text')
                unit['computed_digest'] = unit_digest(task['source_key'], unit['source_start'], unit['source_end'], text) if task else ''
            self.units[key] = (task, units)
        return self.units[key]

    def legacy_sources(self, memory):
        sources = []
        mappings = [dict(r) for r in self.conn.execute(
            "SELECT * FROM knowledge_restructure_map WHERE target_kind='capability' AND target_id=? ORDER BY legacy_item_id", (str(memory['memory_id']),))]
        for mapping in mappings:
            identity = mapping['legacy_item_id']
            sources.append({'source_kind': 'context', 'source_key': f'context:{identity}',
                            'source_ref': f'context_item:{identity}', 'context_item_id': identity,
                            'source_revision': context_revision(self.conn, identity), 'mapping_before': mapping})
        ref = memory['source_ref']
        parsed = re.fullmatch(r'((?:conversation|archive|item):[^#]+)#([^@#]+)(?:@([0-9]+)-([0-9]+))?', ref)
        if parsed:
            if ref not in self.legacy_units:
                key, digest = parsed[1], parsed[2]
                candidates = [dict(r) for r in self.conn.execute(
                    'SELECT t.id AS task_id,u.revision FROM organization_tasks t JOIN organization_units u ON u.task_id=t.id '
                    "WHERE t.source_key=? AND u.digest=? AND t.status!='superseded' ORDER BY t.id,u.ordinal", (key, digest))]
                historical = 0
                if not candidates:
                    historical = self.conn.execute(
                        'SELECT count(*) FROM organization_tasks t JOIN organization_units u ON u.task_id=t.id '
                        "WHERE t.source_key=? AND u.digest=? AND t.status='superseded'", (key, digest)).fetchone()[0]
                if len(candidates) == 1:
                    task_id, unit = candidates[0]['task_id'], candidates[0]
                    task, _ = self.unit(task_id, digest)
                    source = {'source_kind': 'unit', 'source_key': f'{key}#{digest}', 'source_ref': ref,
                              'task_id': task_id, 'unit_digest': digest, 'unit_revision': unit['revision'],
                              'source_revision': task['source_revision']}
                else:
                    reason = 'ambiguous_current_unit' if candidates else 'historical_unit_only' if historical else 'source_not_found'
                    source = {'source_kind': 'reference', 'source_key': ref, 'source_ref': ref,
                              'legacy_reason': reason, 'historical_unit_count': historical}
                self.legacy_units[ref] = source
            sources.append(dict(self.legacy_units[ref]))
        if not sources:
            sources.append({'source_kind': 'reference', 'source_key': ref or f"memory:{memory['memory_id']}",
                            'source_ref': ref, 'legacy_reason': 'untraceable_legacy_source'})
        return sources

    def evaluate(self, source):
        kind = source['source_kind']
        result = {'source_kind': kind, 'source_key': source['source_key'], 'valid': False,
                  'reason': '', 'ownership_confirmed': False, 'projects': [], 'knowledge_scope': 'project'}
        for key in ('source_id', 'task_id', 'unit_digest', 'context_item_id', 'historical_unit_count'):
            if key in source:
                result[key] = source[key]
        def invalid(reason):
            return {**result, 'reason': reason}
        if kind == 'context':
            identity = source.get('context_item_id')
            row = _row(self.conn, 'SELECT id,content_type,status,project,projects,scope,superseded_by,expires_at,effective_from,effective_until FROM context_items WHERE id=?', (identity,))
            if not row or row['status'] != 'active' or row['superseded_by'] is not None:
                return invalid('context_not_active')
            if not source.get('source_revision') or source['source_revision'] != context_revision(self.conn, identity):
                return invalid('context_revision_changed')
            from evolvmem.context_store import _now_iso
            from evolvmem.context_temporal import window_contains
            now = _now_iso()
            if (row['expires_at'] and row['expires_at'] <= now) or not window_contains(row['effective_from'], row['effective_until'], now):
                return invalid('context_expired')
            from evolvmem.memory_eligibility import eligible
            if identity not in self.contexts:
                self.contexts[identity] = eligible(self.store, identity)
            if not self.contexts[identity]:
                return invalid('context_not_eligible')
            resolution = _row(self.conn, 'SELECT * FROM context_project_resolutions WHERE item_id=?', (identity,))
            projects = []
            confirmed = False
            if resolution:
                fact = classify(resolution['resolution_state'], resolution['review_state'], resolution['decision_source'], resolution['method'])
                target = resolution['resolved_project']
                confirmed = fact.confirmed and row['scope'] == 'project' and target == row['project'] and target in self.active_projects
                if confirmed:
                    projects = [target]
            return {**result, 'valid': True, 'ownership_confirmed': confirmed,
                    'projects': projects, 'knowledge_scope': row['scope']}
        if kind != 'unit':
            return invalid(source.get('legacy_reason') or 'unanchored_reference')
        task, units = self.unit(source.get('task_id'), source.get('unit_digest'))
        if not task or len(units) != 1:
            return invalid('unit_missing_or_ambiguous')
        unit = units[0]
        if task['status'] == 'superseded' or task['superseded_by'] is not None:
            return invalid('task_superseded')
        if unit['disposition'] not in {'keep', 'history_only'}:
            return invalid('unit_withdrawn')
        if type(source.get('unit_revision')) is not int or source['unit_revision'] > unit['revision']:
            return invalid('unit_revision_changed')
        if unit['computed_digest'] != unit['digest']:
            return invalid('unit_content_changed')
        if not unit['source_text_proven']:
            return invalid('unit_source_text_unproven')
        if source['source_key'] != f"{task['source_key']}#{unit['digest']}":
            return invalid('unit_source_mismatch')
        if '@' in source.get('source_ref', ''):
            expected_ref = f"{task['source_key']}#{unit['digest']}@{unit['source_start']}-{unit['source_end']}"
            if source['source_ref'] != expected_ref:
                return invalid('source_range_changed')
        if source.get('source_revision') != task['source_revision']:
            return invalid('source_revision_changed')
        revision, reason = self.source_revision(task['source_key'])
        if reason:
            return invalid(reason)
        if revision != task['source_revision']:
            return invalid('source_revision_changed')
        projects = ks.projects(unit)
        scope = ks.scope(unit)
        confirmed = unit['decision'] in {'auto', 'manual'}
        if scope == 'global':
            confirmed = confirmed and not projects
        else:
            confirmed = confirmed and bool(projects) and set(projects) <= self.active_projects
        return {**result, 'valid': True, 'ownership_confirmed': confirmed and scope == 'project',
                'ownership_updated': source['unit_revision'] != unit['revision'],
                'projects': projects if confirmed else [], 'knowledge_scope': scope if confirmed else 'project'}

    def _origin_identity(self, source):
        """Match logical origins across old refs, binding IDs and task revisions.

        An explicit claim owns assessment for that origin, including when it is
        stale or invalid. A parallel legacy None source ID must not revive it.
        """
        kind = source['source_kind']
        if kind == 'context':
            return ('context', source.get('context_item_id'))
        if kind == 'unit':
            task, _ = self.unit(source.get('task_id'), source.get('unit_digest'))
            if task:
                return ('unit', task['source_key'], source.get('unit_digest'))
        parsed = re.fullmatch(r'((?:conversation|archive|item):[^#]+)#([^@#]+)(?:@[0-9]+-[0-9]+)?',
                              source.get('source_ref') or source.get('source_key', ''))
        if parsed:
            return ('unit', parsed[1], parsed[2])
        return (kind, source['source_key'])

    def memory(self, memory):
        if self.bound_sources is not None:
            sources = list(self.bound_sources.get(memory['memory_id'], []))
        else:
            sources = [dict(r) for r in self.conn.execute('SELECT * FROM capability_sources WHERE memory_id=? ORDER BY source_id', (memory['memory_id'],))] if self.sources_exist else []
        legacy = not sources
        historical = self.legacy_sources(memory)
        if not sources:
            sources = historical
        else:
            # New bindings cannot erase an independent older mapping/ref. Keep
            # the real origins, but do not invent a placeholder alongside them.
            known = {self._origin_identity(source) for source in sources}
            for source in historical:
                if source.get('legacy_reason') == 'untraceable_legacy_source':
                    continue
                identity = self._origin_identity(source)
                if identity not in known:
                    sources.append(source)
                    known.add(identity)
                    legacy = True
        facts = [self.evaluate(s) for s in sources]
        eligible = memory['status'] == 'active' and any(s['valid'] for s in facts)
        projects = sorted({p for s in facts if s['valid'] for p in s['projects']}) if eligible else []
        return {'memory_id': memory['memory_id'], 'eligible': eligible,
                'content_valid': any(s['valid'] for s in facts),
                'ownership_confirmed': bool(projects), 'source_projects': projects,
                'legacy_fallback': legacy, 'sources': facts}, sources


def annotate(context_store, ids=None):
    """Return content/ownership facts keyed by capability IDs (never Context IDs)."""
    read = ProvenanceRead(context_store)
    conn = context_store._connection()
    selected = None if ids is None else sorted({int(i) for i in ids})
    rows = []
    read.bound_sources = {}
    batches = [None] if selected is None else [selected[i:i+400] for i in range(0, len(selected), 400)]
    for batch in batches:
        where = '' if batch is None else ' WHERE memory_id IN (' + ','.join('?' for _ in batch) + ')'
        rows.extend(conn.execute('SELECT memory_id,status,source_ref FROM capability_memories' + where + ' ORDER BY memory_id', batch or ()))
        if read.sources_exist:
            for source in conn.execute('SELECT * FROM capability_sources' + where + ' ORDER BY source_id', batch or ()):
                read.bound_sources.setdefault(source['memory_id'], []).append(dict(source))
    return {row['memory_id']: read.memory(dict(row))[0] for row in rows}


def eligible_memory_ids(context_store, ids=None):
    return {identity for identity, row in annotate(context_store, ids).items() if row['eligible']}

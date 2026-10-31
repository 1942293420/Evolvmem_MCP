"""Explicit project business modules, independent from technical categories.

SCHEMA is installed by the owner. Constructing this store never initializes a
database. Candidate scores cannot create modules or establish a binding.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
import re
import sqlite3
import unicodedata
import uuid

from evolvmem.capability_provenance import provenance_reader, _hash, _row


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS project_modules(
        module_id TEXT PRIMARY KEY,project TEXT NOT NULL REFERENCES context_project_registry(project),
        name TEXT NOT NULL,name_key TEXT NOT NULL,aliases TEXT NOT NULL DEFAULT '[]',
        description TEXT NOT NULL DEFAULT '',status TEXT NOT NULL CHECK(status IN ('pending','active','inactive')),
        revision INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
        UNIQUE(project,name_key))""",
    """CREATE TABLE IF NOT EXISTS project_module_names(
        project TEXT NOT NULL,name_key TEXT NOT NULL,module_id TEXT NOT NULL REFERENCES project_modules(module_id),
        PRIMARY KEY(project,name_key))""",
    """CREATE TABLE IF NOT EXISTS project_module_bindings(
        binding_id INTEGER PRIMARY KEY AUTOINCREMENT,module_id TEXT NOT NULL REFERENCES project_modules(module_id),
        source_kind TEXT NOT NULL CHECK(source_kind IN ('unit','capability_source')),source_key TEXT NOT NULL,
        task_id INTEGER,unit_digest TEXT NOT NULL DEFAULT '',capability_source_id INTEGER,
        source_revision TEXT NOT NULL,unit_revision INTEGER,source_binding_revision INTEGER,
        source_fingerprint TEXT NOT NULL,origin_anchor TEXT NOT NULL,
        quote TEXT NOT NULL,role TEXT NOT NULL,chain_json TEXT NOT NULL DEFAULT '{}',
        decision TEXT NOT NULL CHECK(decision IN ('auto','manual','review')),
        status TEXT NOT NULL CHECK(status IN ('active','inactive')),revision INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(module_id,source_key))""",
    "CREATE INDEX IF NOT EXISTS idx_module_bindings_source ON project_module_bindings(source_key)",
    """CREATE TABLE IF NOT EXISTS project_module_audit(
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,module_id TEXT NOT NULL,binding_id INTEGER,
        action TEXT NOT NULL,actor TEXT NOT NULL,before_json TEXT NOT NULL,after_json TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def _key(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def _explicit(value):
    if value is not True:
        raise ValueError('explicit_module_operation_required')


def _quote_valid(text, quote):
    return isinstance(quote, str) and bool(quote.strip()) and len(quote) <= 2000 and text.count(quote) == 1


class ProjectModuleStore:
    def __init__(self, context_store):
        self.store = context_store

    @property
    def conn(self):
        return self.store._connection()

    @contextmanager
    def _write(self):
        with self.store.transaction():
            self.conn.execute('SAVEPOINT project_module_write')
            try:
                yield
            except BaseException:
                self.conn.execute('ROLLBACK TO project_module_write')
                self.conn.execute('RELEASE project_module_write')
                raise
            else:
                self.conn.execute('RELEASE project_module_write')

    def _project(self, project):
        row = _row(self.conn, "SELECT project FROM context_project_registry WHERE project=? AND status='active'", (project,))
        if row is None:
            raise ValueError('active_project_required')

    @staticmethod
    def _fields(name, aliases, description):
        if not isinstance(name, str) or not name.strip() or len(name) > 100:
            raise ValueError('invalid_module_name')
        if not isinstance(aliases, (list, tuple)) or len(aliases) > 20 or any(not isinstance(a, str) or not a.strip() or len(a) > 100 for a in aliases):
            raise ValueError('invalid_module_aliases')
        if not isinstance(description, str) or len(description) > 2000:
            raise ValueError('invalid_module_description')
        return name.strip(), list(dict.fromkeys(a.strip() for a in aliases)), description.strip()

    def _names(self, row):
        self.conn.execute('DELETE FROM project_module_names WHERE module_id=?', (row['module_id'],))
        names = {_key(row['name']), *(_key(a) for a in json.loads(row['aliases']))}
        try:
            self.conn.executemany('INSERT INTO project_module_names VALUES(?,?,?)',
                                  [(row['project'], key, row['module_id']) for key in sorted(names)])
        except sqlite3.IntegrityError as exc:
            raise ValueError('module_name_conflict') from exc

    def _audit(self, module_id, action, before, after, *, actor, binding_id=None):
        self.conn.execute('INSERT INTO project_module_audit(module_id,binding_id,action,actor,before_json,after_json,created_at) VALUES(?,?,?,?,?,?,?)',
                          (module_id, binding_id, action, actor, _json(before), _json(after), _now()))

    def get(self, module_id):
        row = _row(self.conn, 'SELECT * FROM project_modules WHERE module_id=?', (module_id,))
        if row is None:
            raise ValueError('module_not_found')
        row['aliases'] = json.loads(row['aliases'])
        return row

    def list(self, project, *, include_inactive=False):
        # An empty installed registry is a valid state, not a request to infer
        # business modules from projects or seven technical categories.
        self._project(project) if not include_inactive else None
        condition = '' if include_inactive else " AND status='active'"
        return [self.get(r[0]) for r in self.conn.execute('SELECT module_id FROM project_modules WHERE project=?' + condition + ' ORDER BY name_key,module_id', (project,))]

    def _create(self, project, name, aliases, description, status):
        name, aliases, description = self._fields(name, aliases, description)
        with self._write():
            self._project(project)
            now, identity = _now(), 'mod_' + uuid.uuid4().hex
            try:
                self.conn.execute('INSERT INTO project_modules VALUES(?,?,?,?,?,?,?,?,?,?)',
                                  (identity, project, name, _key(name), _json(aliases), description, status, 1, now, now))
            except sqlite3.IntegrityError as exc:
                raise ValueError('module_name_conflict') from exc
            raw = _row(self.conn, 'SELECT * FROM project_modules WHERE module_id=?', (identity,))
            self._names(raw)
            self._audit(identity, 'suggest' if status == 'pending' else 'create', None, self.get(identity),
                        actor='automatic' if status == 'pending' else 'explicit')
            return self.get(identity)

    def create(self, project, name, *, aliases=(), description='', explicit=False):
        _explicit(explicit)
        return self._create(project, name, aliases, description, 'active')

    def suggest(self, project, name, *, aliases=(), description=''):
        name, aliases, description = self._fields(name, aliases, description)
        with self._write():
            self._project(project)
            existing = _row(self.conn, 'SELECT module_id FROM project_module_names WHERE project=? AND name_key=?', (project, _key(name)))
            if existing:
                return self.get(existing['module_id'])
            return self._create(project, name, aliases, description, 'pending')

    def update(self, module_id, *, expected_revision, name=None, aliases=None, description=None, status=None, explicit=False, _action='update'):
        _explicit(explicit)
        with self._write():
            before = self.get(module_id)
            if type(expected_revision) is not int or expected_revision != before['revision']:
                raise ValueError('revision_conflict')
            name, aliases, description = self._fields(name if name is not None else before['name'],
                aliases if aliases is not None else before['aliases'], description if description is not None else before['description'])
            status = status if status is not None else before['status']
            if status not in {'active', 'pending', 'inactive'}:
                raise ValueError('invalid_module_status')
            if status != 'inactive':
                self._project(before['project'])
            try:
                cursor = self.conn.execute('UPDATE project_modules SET name=?,name_key=?,aliases=?,description=?,status=?,revision=revision+1,updated_at=? WHERE module_id=? AND revision=?',
                    (name, _key(name), _json(aliases), description, status, _now(), module_id, expected_revision))
            except sqlite3.IntegrityError as exc:
                raise ValueError('module_name_conflict') from exc
            if cursor.rowcount != 1:
                raise ValueError('revision_conflict')
            self._names(_row(self.conn, 'SELECT * FROM project_modules WHERE module_id=?', (module_id,)))
            after = self.get(module_id)
            self._audit(module_id, _action, before, after, actor='explicit')
            return after

    def activate(self, module_id, *, expected_revision, explicit=False):
        return self.update(module_id, expected_revision=expected_revision, explicit=explicit, status='active', _action='activate')

    def deactivate(self, module_id, *, expected_revision, explicit=False):
        return self.update(module_id, expected_revision=expected_revision, explicit=explicit, status='inactive', _action='deactivate')

    def candidates(self, project, query, *, vector_scores=None, limit=10):
        rows = self.list(project)
        if not isinstance(query, str) or not query.strip():
            return []
        if callable(vector_scores):
            vector_scores = vector_scores(query, tuple(r['module_id'] for r in rows))
        vector_scores = vector_scores or {}
        words = set(re.findall(r'[a-z0-9_]+|[\u4e00-\u9fff]{1,4}', _key(query)))
        result = []
        for row in rows:
            text = _key(' '.join([row['name'], *row['aliases'], row['description']]))
            lexical = sum(word in text for word in words) / max(1, len(words))
            vector = vector_scores.get(row['module_id'], 0.0)
            vector = min(1.0, max(0.0, float(vector))) if isinstance(vector, (int, float)) and math.isfinite(vector) else 0.0
            if lexical or vector:
                result.append({**row, 'lexical_score': lexical, 'vector_score': vector,
                               'score': 0.7 * lexical + 0.3 * vector, 'binding_authorized': False})
        return sorted(result, key=lambda r: (-r['score'], r['module_id']))[:max(1, min(50, int(limit)))]

    def _origin(self, source_kind, source_key, read=None):
        read = read or provenance_reader(self.store)
        not_recalled = False
        if source_kind == 'unit':
            _, task_id, digest = source_key.split(':', 2)
            task_id = int(task_id)
            task, units = read.unit(task_id, digest)
            if not task or len(units) != 1:
                return {'valid': False}
            source = {'source_kind': 'unit', 'source_key': f"{task['source_key']}#{digest}",
                      'source_ref': f"{task['source_key']}#{digest}@{units[0]['source_start']}-{units[0]['source_end']}",
                      'task_id': task_id, 'unit_digest': digest, 'unit_revision': units[0]['revision'],
                      'source_revision': task['source_revision']}
            claim_revision = None
        else:
            source = _row(self.conn, 'SELECT * FROM capability_sources WHERE source_id=?', (int(source_key.split(':')[1]),))
            if source is None:
                return {'valid': False}
            memory = _row(self.conn, 'SELECT status FROM capability_memories WHERE memory_id=?', (source['memory_id'],))
            if not memory or memory['status'] not in {'active', 'pending'}:
                return {'valid': False}
            not_recalled = memory['status'] == 'pending'
            claim_revision = source['revision']
        verdict = read.evaluate(source)
        if not verdict['valid']:
            return {'valid': False}
        if source['source_kind'] == 'unit':
            unit = _row(self.conn, 'SELECT text FROM organization_units WHERE task_id=? AND digest=?', (source['task_id'], source['unit_digest']))
            text = unit['text'] if unit else ''
            content_key = source['unit_digest']
            anchor = f"unit:{source['task_id']}:{source['unit_digest']}"
        elif source['source_kind'] == 'context':
            layers = {r['layer']: r['content'] for r in self.conn.execute('SELECT layer,content FROM context_layers WHERE item_id=?', (source['context_item_id'],))}
            text = layers.get('l2') or layers.get('l1') or layers.get('l0', '')
            content_key = source['source_revision']
            anchor = f"context:{source['context_item_id']}"
        else:
            return {'valid': False}
        proof = [source_kind, source_key, source['source_kind'], source['source_key'],
                 source['source_ref'], source['source_revision'], content_key]
        return {'valid': True, 'projects': verdict['projects'], 'text': text,
                'not_recalled': not_recalled,
                'origin_anchor': anchor,
                'source_binding_revision': claim_revision,
                'source_revision': source['source_revision'], 'unit_revision': source.get('unit_revision'),
                'fingerprint': _hash(proof)}

    @staticmethod
    def _evidence(text, quote, role, decision, chain):
        if not _quote_valid(text, quote):
            raise ValueError('quote_not_verifiable')
        if decision not in {'auto', 'manual', 'review'}:
            raise ValueError('invalid_module_decision')
        if role not in {'owner', 'applicable', 'related', 'uncertain'}:
            raise ValueError('module_role_evidence_required')
        if not isinstance(chain, dict) or any(k not in {'need', 'analysis', 'change', 'result'} or not _quote_valid(text, q) for k, q in chain.items()):
            raise ValueError('module_role_evidence_required')
        if decision != 'review' and (role not in {'owner', 'applicable'} or (decision == 'auto' and not chain)):
            raise ValueError('module_role_evidence_required')

    def _bind(self, module_id, source_kind, source_key, *, quote, role, decision, expected_revision, chain=None, explicit=False):
        if decision == 'manual':
            _explicit(explicit)
        chain = {} if chain is None else chain
        with self._write():
            module = self.get(module_id)
            if module['status'] != 'active':
                raise ValueError('module_not_active')
            self._project(module['project'])
            old = _row(self.conn, 'SELECT * FROM project_module_bindings WHERE module_id=? AND source_key=?', (module_id, source_key))
            if old and old['decision'] == 'manual' and decision != 'manual':
                raise ValueError('manual_module_binding_protected')
            if type(expected_revision) is not int or expected_revision != (old['revision'] if old else 0):
                raise ValueError('revision_conflict')
            origin = self._origin(source_kind, source_key)
            if not origin['valid']:
                raise ValueError('source_invalid')
            if decision != 'manual' and self.conn.execute(
                    "SELECT 1 FROM project_module_bindings WHERE module_id=? AND origin_anchor=? AND decision='manual'",
                    (module_id, origin['origin_anchor'])).fetchone():
                raise ValueError('manual_module_binding_protected')
            if module['project'] not in origin['projects']:
                raise ValueError('module_project_mismatch')
            self._evidence(origin['text'], quote, role, decision, chain)
            task_id = int(source_key.split(':')[1]) if source_kind == 'unit' else None
            digest = source_key.split(':', 2)[2] if source_kind == 'unit' else ''
            source_id = int(source_key.split(':')[1]) if source_kind == 'capability_source' else None
            values = (origin['source_revision'], origin['unit_revision'], origin['source_binding_revision'], origin['fingerprint'], origin['origin_anchor'], quote, role, _json(chain), decision, _now())
            if old:
                cursor = self.conn.execute("UPDATE project_module_bindings SET source_revision=?,unit_revision=?,source_binding_revision=?,source_fingerprint=?,origin_anchor=?,quote=?,role=?,chain_json=?,decision=?,updated_at=?,status='active',revision=revision+1 WHERE binding_id=? AND revision=?", (*values, old['binding_id'], expected_revision))
                if cursor.rowcount != 1:
                    raise ValueError('revision_conflict')
                binding_id = old['binding_id']
            else:
                cursor = self.conn.execute("INSERT INTO project_module_bindings(module_id,source_kind,source_key,task_id,unit_digest,capability_source_id,source_revision,unit_revision,source_binding_revision,source_fingerprint,origin_anchor,quote,role,chain_json,decision,updated_at,status,revision,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'active',1,?)",
                    (module_id, source_kind, source_key, task_id, digest, source_id, *values, values[-1]))
                binding_id = cursor.lastrowid
            after = _row(self.conn, 'SELECT * FROM project_module_bindings WHERE binding_id=?', (binding_id,))
            self._audit(module_id, 'bind', old, after, actor=decision, binding_id=binding_id)
            return after

    def bind_unit(self, module_id, task_id, digest, **kwargs):
        if type(task_id) is not int or task_id < 1 or not isinstance(digest, str) or not digest or ':' in digest:
            raise ValueError('invalid_module_source')
        return self._bind(module_id, 'unit', f'unit:{task_id}:{digest}', **kwargs)

    def bind_source(self, module_id, capability_source_id, **kwargs):
        if type(capability_source_id) is not int or capability_source_id < 1:
            raise ValueError('invalid_module_source')
        return self._bind(module_id, 'capability_source', f'capability_source:{capability_source_id}', **kwargs)

    def unbind(self, binding_id, *, expected_revision, explicit=False):
        _explicit(explicit)
        with self._write():
            before = _row(self.conn, 'SELECT * FROM project_module_bindings WHERE binding_id=?', (binding_id,))
            if not before or type(expected_revision) is not int or before['revision'] != expected_revision:
                raise ValueError('revision_conflict')
            self.conn.execute("UPDATE project_module_bindings SET status='inactive',decision='manual',revision=revision+1,updated_at=? WHERE binding_id=? AND revision=?", (_now(), binding_id, expected_revision))
            after = _row(self.conn, 'SELECT * FROM project_module_bindings WHERE binding_id=?', (binding_id,))
            self._audit(before['module_id'], 'unbind', before, after, actor='manual', binding_id=binding_id)
            return after

    def binding_status(self, binding_id, *, _read=None):
        row = _row(self.conn, 'SELECT * FROM project_module_bindings WHERE binding_id=?', (binding_id,))
        if row is None:
            raise ValueError('module_binding_not_found')
        return self._binding_status(row, self.get(row['module_id']), _read)

    def _binding_status(self, row, module, read):
        reason = ''
        not_recalled = False
        if module['status'] != 'active':
            reason = 'module_not_active'
        elif not self.conn.execute("SELECT 1 FROM context_project_registry WHERE project=? AND status='active'", (module['project'],)).fetchone():
            reason = 'module_project_inactive'
        elif row['status'] != 'active' or row['decision'] == 'review':
            reason = 'binding_not_active'
        elif row['decision'] != 'manual' and self.conn.execute(
                "SELECT 1 FROM project_module_bindings WHERE module_id=? AND origin_anchor=? AND decision='manual'",
                (row['module_id'], row['origin_anchor'])).fetchone():
            reason = 'manual_module_binding_protected'
        else:
            origin = self._origin(row['source_kind'], row['source_key'], read)
            not_recalled = origin.get('not_recalled', False)
            if not origin['valid']:
                reason = 'source_invalid'
            elif module['project'] not in origin['projects']:
                reason = 'module_project_mismatch'
            elif origin['fingerprint'] != row['source_fingerprint']:
                reason = 'source_version_changed'
            else:
                try:
                    self._evidence(origin['text'], row['quote'], row['role'], row['decision'], json.loads(row['chain_json']))
                except (ValueError, TypeError):
                    reason = 'module_evidence_invalid'
        return {**row, 'valid': not reason, 'reason': reason, 'not_recalled': bool(reason) or not_recalled}

    def _for_keys(self, keys, *, read=None):
        return self._for_key_groups({0: keys}, read=read)[0]

    def _for_key_groups(self, groups, *, read=None):
        read = read or provenance_reader(self.store)
        targets = {}
        for identity, keys in groups.items():
            for key in keys:
                targets.setdefault(key, []).append(identity)
        keys, bindings = sorted(targets), []
        for offset in range(0, len(keys), 400):
            batch = keys[offset:offset+400]
            bindings.extend(dict(row) for row in self.conn.execute(
                'SELECT * FROM project_module_bindings WHERE source_key IN (' + ','.join('?' for _ in batch) + ') ORDER BY binding_id', batch))
        merged = {identity: {} for identity in groups}
        modules = {}
        for row in sorted(bindings, key=lambda value: value['binding_id']):
            module_id = row['module_id']
            if module_id not in modules:
                modules[module_id] = self.get(module_id)
            status = self._binding_status(row, modules[module_id], read)
            if not status['valid']:
                continue
            for identity in targets[status['source_key']]:
                target = merged[identity].setdefault(module_id, {**modules[module_id], 'binding_ids': [], 'source_keys': []})
                target['binding_ids'].append(status['binding_id'])
                target['source_keys'].append(status['source_key'])
        return {identity: sorted(rows.values(), key=lambda row: (row['name_key'], row['module_id']))
                for identity, rows in merged.items()}

    def modules_for_unit(self, task_id, digest):
        return self._for_keys({f'unit:{task_id}:{digest}'})

    def modules_for_memory(self, memory_id, allowed_source_ids=None, *, allowed_sources=None):
        memory = _row(self.conn, 'SELECT memory_id,status,source_ref FROM capability_memories WHERE memory_id=?', (memory_id,))
        if memory is None or memory['status'] not in {'active', 'pending'}:
            return []
        read = provenance_reader(self.store)
        keys = self._memory_keys(memory, read, allowed_source_ids, allowed_sources)
        return [{**row, 'not_recalled': memory['status'] != 'active'} for row in self._for_keys(keys, read=read)]

    def _memory_keys(self, memory, read, allowed_source_ids=None, allowed_sources=None):
        fact, sources = read.memory(memory)
        keys = set()
        allowed = None if allowed_source_ids is None else set(allowed_source_ids)
        # Legacy origins may all lack a source_id. Match their precise origin
        # as well so a reviewed origin cannot authorize a newly added mapping.
        def origin_key(value):
            return (value.get('source_kind'), value.get('source_key'), value.get('source_id'))
        allowed_origins = None if allowed_sources is None else {origin_key(s) for s in allowed_sources}
        for source, verdict in zip(sources, fact['sources']):
            if not verdict['valid']:
                continue
            if allowed_origins is not None and origin_key(verdict) not in allowed_origins:
                continue
            if allowed is not None and source.get('source_id') not in allowed:
                continue
            if source['source_kind'] == 'unit':
                keys.add(f"unit:{source['task_id']}:{source['unit_digest']}")
            if source.get('source_id'):
                keys.add(f"capability_source:{source['source_id']}")
        return keys

    def modules_for_memories(self, memory_ids, *, allowed_sources=None):
        """Batch the current bindings; keep each memory's exact allowed origins."""
        identities = sorted({int(value) for value in memory_ids})
        read = provenance_reader(self.store)
        previous = read.bound_sources
        read.bound_sources = {}
        memories, groups = {}, {}
        try:
            for offset in range(0, len(identities), 400):
                batch = identities[offset:offset+400]
                where = ' WHERE memory_id IN (' + ','.join('?' for _ in batch) + ')'
                memories.update((row['memory_id'], dict(row)) for row in self.conn.execute(
                    'SELECT memory_id,status,source_ref FROM capability_memories' + where, batch))
                if read.sources_exist:
                    for source in self.conn.execute('SELECT * FROM capability_sources' + where + ' ORDER BY source_id', batch):
                        read.bound_sources.setdefault(source['memory_id'], []).append(dict(source))
            for identity in identities:
                memory = memories.get(identity)
                groups[identity] = (self._memory_keys(memory, read, allowed_sources=(
                    None if allowed_sources is None else allowed_sources.get(identity, ())))
                    if memory and memory['status'] in {'active', 'pending'} else set())
        finally:
            read.bound_sources = previous
        result = self._for_key_groups(groups, read=read)
        return {identity: [{**row, 'not_recalled': memories[identity]['status'] != 'active'} for row in rows]
                for identity, rows in result.items()}

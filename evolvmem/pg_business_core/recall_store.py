"""Bounded, transaction-owned reads for the supported ordinary-write domain.

Not a history/organization eligibility adapter. A completed write receipt is
only an origin marker; current shape, bytes, sources and whole families remain
independent checks. Introducing an unsupported qualification domain disables
this slice until its canonical rules have an actual PostgreSQL implementation.
"""
from dataclasses import fields
import hashlib
import json

from evolvmem.context_models import (ContextContentType, ContextItem, ContextLayer,
    ContextRetrievalRecord, ContextScope, ContextStatus, ContextTier)
from evolvmem.context_temporal import temporal_rank
from evolvmem.context_retrieval_inputs import _weight
from evolvmem.library_foundation import FoundationError
from evolvmem.library_foundation.sessions import LibrarySession

MAX_ROWS = 512
MAX_BYTES = 8 * 1024 * 1024
UNSUPPORTED_DOMAINS = ('unit_derivations', 'learning_memories', 'knowledge_metadata',
    'knowledge_objects', 'knowledge_qa', 'context_evidence', 'lan_publications')


def _proof(value):
    # Retention/serialization budget, not a promise about driver allocation RSS.
    if _weight(value, MAX_BYTES) is None: raise FoundationError('recall_dependency_limit')
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


class PgRecallStore:
    def __init__(self, session):
        if not isinstance(session, LibrarySession) or session.route.action != 'read':
            raise FoundationError('read_session_required')
        self.session = session
        self.library = session.route.library
        self.lib = self.library.library_id
        self.rows, self.layers, self.sources, self.origins = {}, {}, {}, {}
        self._remaining = MAX_BYTES
        self.families = {}
        marks = ','.join('%s' for _ in UNSUPPORTED_DOMAINS)
        present = session.execute('SELECT c.relname FROM pg_catalog.pg_class c '
            'JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace '
            f"WHERE n.nspname='em_content' AND c.relname IN ({marks})", UNSUPPORTED_DOMAINS)
        if present: raise FoundationError('unsupported_recall_qualification_domain')

    def _load(self, ids):
        ids = tuple(dict.fromkeys(ids))
        if len(ids) > MAX_ROWS or len(set(ids) | self.rows.keys()) > MAX_ROWS:
            raise FoundationError('recall_dependency_limit')
        missing = [i for i in ids if i not in self.rows]
        if not missing: return
        marks = ','.join('%s' for _ in missing)
        rows = self.session.execute('SELECT to_jsonb(c) FROM em_content.context_items c '
            f'WHERE library_id=%s AND id IN ({marks}) ORDER BY id', (self.lib, *missing))
        for (row,) in rows:
            if _weight(row, self._remaining) is None: raise FoundationError('recall_dependency_limit')
            i = row['id']
            if (row['library_id'] != self.lib or row['project_id'] != self.library.project_id
                    or row['content_type'] not in ('reference', 'fact')
                    or row['experience_payload'] is not None or row['source_state'] != 'none'
                    or row['projects'] or row['personal_general']
                    or row['success_count'] or row['failure_count']):
                raise FoundationError('unsupported_context_evidence')
            if self.library.kind == 'project':
                registry = self.session.execute('SELECT project_uuid::text FROM em_content.context_project_registry '
                    "WHERE library_id=%s AND project=%s AND status='active'", (self.lib, row['project']))
                if registry != [(self.library.project_id,)]:
                    raise FoundationError('unsupported_project_scope_mismatch')
            receipt = self.session.execute('SELECT tool,result_json,project_id::text FROM em_content.lan_write_requests '
                "WHERE library_id=%s AND principal_id=%s AND completed_at IS NOT NULL AND tool IN ('memory_add','memory_replace') "
                "AND result_json->>'context_id'=%s ORDER BY completed_at,request_id LIMIT 1",
                (self.lib, self.session.route.identity.principal_id, str(i)))
            if (not receipt or not isinstance(receipt[0][1], dict)
                    or receipt[0][2] != self.library.project_id
                    or receipt[0][1].get('context_id') != i
                    or receipt[0][1].get('status') != {'memory_add':'added','memory_replace':'replaced'}[receipt[0][0]]):
                raise FoundationError('unsupported_recall_origin')
            layers = self.session.execute('SELECT layer,content,content_hash,generator FROM em_content.context_layers '
                'WHERE library_id=%s AND item_id=%s ORDER BY layer LIMIT 4', (self.lib, i))
            if (len(layers) != 3 or tuple(r[0] for r in layers) != ('l0', 'l1', 'l2')
                    or any(not isinstance(r[1], str) or len(r[1]) > MAX_BYTES // 4 for r in layers)):
                raise FoundationError('unsupported_context_layers')
            if any(hashlib.sha256(r[1].encode('utf-8')).hexdigest() != r[2] for r in layers):
                raise FoundationError('recall_content_hash_mismatch')
            sources = self.session.execute('SELECT id,project_id::text,source_kind,source_ref,extraction_version,archive_id '
                'FROM em_content.context_sources WHERE library_id=%s AND item_id=%s ORDER BY id LIMIT 513', (self.lib, i))
            if len(sources) > MAX_ROWS: raise FoundationError('recall_dependency_limit')
            if (len(sources) != row['source_count'] or any(r[1] != self.library.project_id
                    or r[2] != 'migration' or not isinstance(r[3], str) or not r[3].strip()
                    or r[4] != 'legacy-write.v1' or r[5] is not None for r in sources)):
                raise FoundationError('unsupported_context_evidence')
            retained = _weight((row, layers, sources, receipt), self._remaining)
            if retained is None: raise FoundationError('recall_dependency_limit')
            self._remaining -= retained
            self.rows[i], self.layers[i], self.sources[i], self.origins[i] = row, layers, sources, receipt

    def _item(self, row):
        excluded = {'layers', 'content_type', 'scope', 'status', 'tier', 'tags', 'projects'}
        args = {f.name: row[f.name] for f in fields(ContextItem) if f.name not in excluded}
        return ContextItem(**args, content_type=ContextContentType(row['content_type']),
            scope=ContextScope(row['scope']), status=ContextStatus(row['status']),
            tier=ContextTier(row['tier']), tags=tuple(row['tags']), projects=tuple(row['projects']), layers=None)

    def get_retrieval_records(self, ids):
        self._load(ids)
        return tuple(ContextRetrievalRecord(self._item(self.rows[i]), self.layers[i][0][1],
            tuple(ContextLayer(r[0]) for r in self.layers[i])) for i in dict.fromkeys(ids) if i in self.rows)

    def get_by_identity(self, identity_key, *, project, scope, include_layers=False):
        key = (identity_key, project, scope.value)
        if key not in self.families:
            ids = tuple(r[0] for r in self.session.execute('SELECT id FROM em_content.context_items '
                'WHERE library_id=%s AND identity_key=%s AND project=%s AND scope=%s ORDER BY id LIMIT 513', (self.lib, *key)))
            self._load(ids)
            self.families[key] = ids
        return tuple(self._item(self.rows[i]) for i in self.families[key])

    def successor_ranks(self, ids):
        self._load(ids)
        links = {i: self.rows[i]['superseded_by'] for i in ids if i in self.rows and self.rows[i]['superseded_by'] is not None}
        self._load(links.values())
        return {i: temporal_rank(self.rows[n]['effective_from'], self.rows[n]['occurred_at'])
            for i, n in links.items() if n in self.rows}

    def retrieval_fingerprints(self, ids):
        self._load(ids)
        # SQLite's ordinary non-experience representation is the JSON text '{}'.
        return {i: (tuple(('{}', *(self.rows[i][k] for k in ('effective_from','effective_until','occurred_at','expires_at')),
            layer[0], layer[2]) for layer in self.layers[i]), None) for i in ids if i in self.rows}

    def eligible(self, item_id):
        self._load((item_id,))
        # Only this proved new-write domain is supported; this is NOT a fallback
        # implementation of missing organization/learning eligibility tables.
        return item_id in self.rows

    def proof(self):
        return _proof((self.rows, self.layers, self.sources, self.origins, sorted(self.families.items())))

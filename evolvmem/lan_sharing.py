"""Curated public Context items and durable remote write intents.

All calls run under LanTools' runtime-wide dispatch lock. SQLite is authoritative;
vector cache writes run only after the public transaction commits.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid

from evolvmem.context_models import (
    ContextContentType, ContextItemDraft, ContextLayers, ContextScope,
    ContextSearchRequest, ContextStatus,
)
from evolvmem.context_service import _VectorAftermath
from evolvmem.context_store import _now_iso
from evolvmem.extraction_policy import contains_sensitive_text


class LanError(ValueError):
    """Content-free error code safe for the remote transport."""


def validate_request_id(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 128
            or not all(33 <= ord(c) <= 126 for c in value)
            or contains_sensitive_text(value)):
        raise LanError('invalid_request_id')


class LanSharing:
    def __init__(self, runtime):
        self.runtime = runtime
        self.server = runtime.server_for('jiangli', 'public')
        self.service = self.server.context_service
        self.store = self.service.store
        with self.store.transaction():
            self.conn.execute('''CREATE TABLE IF NOT EXISTS lan_publications (
                context_id INTEGER PRIMARY KEY REFERENCES context_items(id),
                author TEXT NOT NULL CHECK(author IN ('jiangli','kane')),
                active INTEGER NOT NULL CHECK(active IN (0,1)),
                title TEXT NOT NULL, summary TEXT NOT NULL, project TEXT NOT NULL)''')
        for user in ('jiangli', 'kane'):
            personal = runtime.server_for(user).context_service.store
            with personal.transaction():
                personal._connection().execute('''CREATE TABLE IF NOT EXISTS lan_write_requests (
                user TEXT NOT NULL, tool TEXT NOT NULL, request_id TEXT NOT NULL,
                payload_hash TEXT NOT NULL, result TEXT,
                PRIMARY KEY(user, tool, request_id))''')

    @property
    def conn(self):
        return self.store._connection()

    def execute_once(self, user, tool, request_id, arguments, operation):
        store = self.runtime.server_for(user).context_service.store
        conn = store._connection()
        validate_request_id(request_id)
        payload = json.dumps(arguments, sort_keys=True, separators=(',', ':'), allow_nan=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        key = (user, tool, request_id)
        with store.transaction():
            row = conn.execute('SELECT payload_hash,result FROM lan_write_requests WHERE user=? AND tool=? AND request_id=?', key).fetchone()
            if row:
                if row['payload_hash'] != digest:
                    raise LanError('request_id_conflict')
                if row['result'] is None:
                    raise LanError('request_indeterminate')
                return json.loads(row['result'])
            conn.execute('INSERT INTO lan_write_requests(user,tool,request_id,payload_hash) VALUES(?,?,?,?)', (*key, digest))
        # The committed intent prevents re-execution after any crash. The effect
        # can live in the separate public SQLite, so absence of a result never
        # means the effect did not happen.
        try:
            result = operation()
        except LanError as exc:
            result = {'error': str(exc)}
        with store.transaction():
            conn.execute('UPDATE lan_write_requests SET result=? WHERE user=? AND tool=? AND request_id=?',
                              (json.dumps(result, ensure_ascii=False, allow_nan=False), *key))
        return result

    def _content(self, title, summary, project=''):
        limits = (min(200, self.server.config.context_l0_max_chars),
                  min(4000, self.server.config.context_l1_max_chars,
                      self.server.config.context_l2_max_chars), 128)
        for value, maximum in zip((title, summary, project), limits):
            if not isinstance(value, str) or len(value) > maximum:
                raise LanError('invalid_public_content')
            if (contains_sensitive_text(value)
                    or re.search(r'(?:[a-z][a-z0-9+.-]*://|(?:^|[\s(])[~/]|[A-Za-z]:[\\/]|\\\\)', value, re.I)):
                raise LanError('unsafe_public_content')
        if not title.strip() or not summary.strip():
            raise LanError('invalid_public_content')
        return title.strip(), summary.strip(), project.strip()

    def publish(self, user, args):
        title, summary, project = self._content(args['title'], args['summary'], args.get('project', ''))
        source = self.runtime.server_for(user).handle_tool_call('context_read', {'id': args['source_context_id'], 'layer': 'l1'})
        if 'error' in source:
            raise LanError('source_not_readable')
        with self.store.transaction():
            item = self.store.create_item(ContextItemDraft(
                identity_key='public:' + uuid.uuid4().hex,
                content_type=ContextContentType.FACT,
                layers=ContextLayers(l0=title, l1=summary, l2=summary, generator='lan_curated'),
                project=project, scope=ContextScope.GLOBAL, status=ContextStatus.ACTIVE, confidence=0.6,
            ))
            self.conn.execute('INSERT INTO lan_publications VALUES(?,?,?,?,?,?)',
                              (item.id, user, 1, title, summary, project))
        pending = self._sync(item.id, title)
        return {**self.read(item.id), 'active': True, 'vector_pending': pending}

    def _authorized(self, user, item_id):
        row = self.conn.execute('SELECT * FROM lan_publications WHERE context_id=?', (item_id,)).fetchone()
        if not row:
            raise LanError('not_found')
        if row['author'] != user and user != 'jiangli':
            raise LanError('publication_forbidden')
        return row

    def update(self, user, args):
        row = self._authorized(user, args['id'])
        if not row['active']:
            raise LanError('not_found')
        title, summary, _ = self._content(args['title'], args['summary'], row['project'])
        with self.store.transaction():
            self.conn.execute('DELETE FROM context_layers WHERE item_id=?', (args['id'],))
            self.store._insert_layers(args['id'], ContextLayers(l0=title, l1=summary, l2=summary, generator='lan_curated'), _now_iso())
            self.conn.execute('UPDATE lan_publications SET title=?,summary=? WHERE context_id=?', (title, summary, args['id']))
        pending = self._sync(args['id'], title)
        return {**self.read(args['id']), 'active': True, 'vector_pending': pending}

    def unpublish(self, user, args):
        self._authorized(user, args['id'])
        with self.store.transaction():
            self.store.set_item_status(args['id'], ContextStatus.DELETED)
            self.conn.execute('UPDATE lan_publications SET active=0 WHERE context_id=?', (args['id'],))
        pending = self._sync(args['id'], None)
        return {'id': args['id'], 'space': 'public', 'ref': f"public:context:{args['id']}", 'active': False, 'vector_pending': pending}

    def _sync(self, item_id, title):
        try:
            self.service._apply_vector_aftermath(_VectorAftermath(
                context_upserts=((item_id, title),) if title is not None else (),
                context_removals=(item_id,) if title is None else (),
            ))
            return not self.service.status().context_vector_ready
        except Exception:
            # A failed cache update must not roll back durable publication facts.
            self.service.vector_index.mark_dirty()
            return True

    @staticmethod
    def _view(row):
        return {'id': row['context_id'], 'context_id': row['context_id'],
                'space': 'public', 'ref': f"public:context:{row['context_id']}",
                'title': row['title'], 'summary': row['summary'], 'project': row['project'],
                'verification': 'unverified_shared_summary'}

    def read(self, item_id, layer='l1'):
        row = self.conn.execute('SELECT p.* FROM lan_publications p JOIN context_items i ON i.id=p.context_id WHERE p.context_id=? AND p.active=1 AND i.status=?',
                                (item_id, 'active')).fetchone()
        if row is None:
            raise LanError('not_found')
        if layer not in ('l0', 'l1', 'l2'):
            raise LanError('invalid_layer')
        return {**self._view(row), 'layer': layer,
                'content': row['title'] if layer == 'l0' else row['summary']}

    def search(self, args):
        rows = self.conn.execute('SELECT p.* FROM lan_publications p JOIN context_items i ON i.id=p.context_id WHERE p.active=1 AND i.status=?', ('active',)).fetchall()
        eligible = {r['context_id']: r for r in rows}
        if not eligible:
            return []
        # Eligibility precedes candidate budgets, so orphan rows cannot crowd
        # real publications out of lexical or semantic retrieval.
        self.service._require_serving()
        hits = self.service.retriever.search(ContextSearchRequest(
            query=args['query'], top_k=min(max(args.get('top_k', 10), 1), 20),
            project=args.get('project', ''), cross_project=True,
            content_types=tuple(ContextContentType(t) for t in args.get('content_types', [])),
        ), eligible_ids=set(eligible))
        return [{**self._view(eligible[h.id]), 'l0': eligible[h.id]['title'],
                 'value': eligible[h.id]['summary'], 'score': h.score,
                 'available_layers': ['l0', 'l1', 'l2']} for h in hits]

    def status(self):
        count = self.conn.execute("SELECT COUNT(*) FROM lan_publications p JOIN context_items i ON i.id=p.context_id WHERE p.active=1 AND i.status='active'").fetchone()[0]
        return {'space': 'public', 'active_memories': count, 'total_records': count}

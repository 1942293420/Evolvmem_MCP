"""Real application lifecycle, tiny SQLite effects, explicit PG vector/metadata port.

Python cosine here simulates the external vector driver only. It is not evidence
of PostgreSQL compilation, RLS, HNSW planning or native floating point parity.
"""
from contextlib import contextmanager
import hashlib
import json
import math
from types import SimpleNamespace
from unittest.mock import patch

from tests.pg_context_read_slice.tests.fixture import Fixture, NOW
from tests.pg_context_read_slice.tests.driver_port import Cursor, L, TOKEN
from evolvmem.runtime_contract import EMBEDDING_INPUT_TOKENS
from evolvmem.pg_vector_port.model import ModelContract, vector
from evolvmem.pg_vector_port.runtime import VectorPort, BorrowedEmbeddingProvider


class VectorFixture(Fixture):
    def __init__(self):
        super().__init__()
        self.contract = ModelContract.from_config(self.config, input_tokens=EMBEDDING_INPUT_TOKENS)
        self.query_vector = vector([1.0] + [0.0] * 767, self.contract)
        self.projections = {}
        self.registered_model = self.contract
        self.after_candidates = None
        self.after_initial = None
        self.engine = SimpleNamespace(config=self.config, is_loaded=True,
            encode_query=lambda text: self.query_vector.values,
            encode_document=lambda text: self.query_vector.values)
        original = Cursor.execute
        fixture = self
        def execute(cursor, sql, params=()):
            if cursor.raw.db is None:
                return original(cursor, sql, params)
            raw = cursor.raw
            if 'em_content.vector_' in sql or 'vector_hnsw' in sql:
                fixture.port.calls.append((raw, sql, params))
                if fixture.port.on_sql:
                    fixture.port.on_sql(raw, sql, params)
            if 'AS names(name)' in sql or 'count(*)=8' in sql or "extname='vector'" in sql:
                return cursor.values([(True,)])
            if 'vector_job_contract_unique' in sql:
                return cursor.values([(['library_id','index_kind','entity_id','entity_revision','action','model_contract'],)])
            if 'SELECT version FROM em_content.vector_schema_migrations' in sql:
                return cursor.values([(1,)])
            if 'FROM em_content.vector_models' in sql:
                c = fixture.registered_model
                return cursor.values([(c.dimension, c.document_fields, c.query_prefix)])
            if 'FROM em_content.vector_projections' in sql:
                query = json.loads(params[0])
                if 'ORDER BY embedding' in sql:
                    rows = [(p['id'], p['revision'], p['hash'], fixture.distance(p['embedding'], query))
                        for p in fixture.projections.values() if p['state'] == 'ready']
                    rows.sort(key=lambda r: r[3])
                    rows = rows[:params[-1]]
                    if fixture.after_candidates:
                        fixture.after_candidates()
                    return cursor.values(rows)
                rows = []
                for ident in params[-1]:
                    p = fixture.projections.get(ident)
                    if p is not None:
                        rows.append((p['id'], p['revision'], p['hash'], p['dimension'],
                            p['state'], p['vector_hash'], p['embedding'].pg_digest,
                            fixture.distance(p['embedding'], query), None))
                return cursor.values(rows)
            if 'coalesce(s.unsupported,0)' in sql:
                rows = []
                for ident in params[-1]:
                    row = fixture.item(ident)
                    if row is None:
                        continue
                    layer = raw.db.execute("SELECT content,content_hash FROM context_layers WHERE item_id=? AND layer='l0'",(ident,)).fetchone()
                    unsupported = raw.db.execute("SELECT count(*) FROM context_sources WHERE item_id=? AND (archive_id IS NOT NULL OR source_kind<>'migration')",(ident,)).fetchone()[0]
                    rows.append((row,*layer,unsupported))
                return cursor.values(rows)
            return original(cursor, sql, params)
        self.vector_patch = patch.object(Cursor, 'execute', execute)
        self.vector_patch.start()

    @staticmethod
    def distance(embedding, query):
        return 1 - sum(a*b for a,b in zip(embedding.values,query)) / math.sqrt(
            sum(a*a for a in embedding.values)*sum(b*b for b in query))

    def item(self, ident):
        cursor = self.port.databases['content_0'][1].execute('SELECT * FROM context_items WHERE id=?',(ident,))
        values = cursor.fetchone()
        if values is None:
            return None
        row = dict(zip((c[0] for c in cursor.description),values))
        row['tags'] = json.loads(row['tags']); row['projects'] = json.loads(row['projects'])
        row['personal_general'] = bool(row['personal_general'])
        return row

    def seed(self, key, value, *, component=.9):
        result = self.add(key,value); ident = result['context_id']
        row = self.item(ident)
        text = self.port.databases['content_0'][1].execute("SELECT content FROM context_layers WHERE item_id=? AND layer='l0'",(ident,)).fetchone()[0]
        embedding = vector([component, math.sqrt(1-component*component)] + [0.] * 766,self.contract)
        self.projections[ident] = dict(id=ident,revision=row['revision'],hash=hashlib.sha256(text.encode()).hexdigest(),
            embedding=embedding,vector_hash=embedding.pg_digest,dimension=768,state='ready')
        return ident

    def close(self):
        self.vector_patch.stop()
        super().close()

    def search(self, *, publish=None, request=None, limit=20):
        from evolvmem.pg_business_core.vector_recall import PgVectorContextRecall
        with self.host.request_scope():
            identity = self.host.authenticate(TOKEN); state = self.host._state()
            route = state.router.plan(identity,'context_read',library_id=L)
            port = VectorPort(state.manager,(route,),self.contract,
                embedding_provider=BorrowedEmbeddingProvider(self.engine))
            original = state.manager._transaction
            calls = 0
            @contextmanager
            def transaction(route):
                nonlocal calls
                with original(route) as session:
                    yield session
                calls += 1
                if calls == 1 and self.after_initial:
                    self.after_initial()
            state.manager._transaction = transaction
            return PgVectorContextRecall(port,self.config,clock=lambda:self.now).search(
                L,request or self.request(),candidate_limit=limit,publish=publish)

"""Project-scoped vectors; ANN ranking never substitutes for final eligibility."""
from dataclasses import dataclass
from evolvmem.library_foundation.sessions import LibrarySession
from evolvmem.library_foundation._projection_conflict import PreEffectRowConflict, is_serialization_failure
from evolvmem.library_foundation.models import FoundationError
from evolvmem.library_project_acl.scope import ProjectScope
from .model import ModelContract, require, text_hash, vector
from .projection import Job, Document, Prepared, PreparedPair, PairApplied, decide

@dataclass(frozen=True, slots=True)
class BackfillPage:
    index_kind: str
    after_id: int
    scanned: int
    enqueued: int
    complete: bool

class VectorRepository:
    def __init__(self, session, contract, project_scope):
        require(isinstance(session, LibrarySession) and isinstance(contract, ModelContract)
                and isinstance(project_scope, ProjectScope), 'actual_vector_session_required')
        project_scope.require(session)
        require(project_scope.project_id is not None, 'vector_project_required')
        self.session, self.contract, self.scope = session, contract, project_scope
        self.lib, self.project = project_scope.library_id, project_scope.project_id

    def check_schema(self):
        self.scope.require(self.session)
        require(bool(self.contract.model_revision), 'versioned_vector_contract_required')
        self.check_model()  # Trusted SQL checks actual schema/RLS/HNSW partitions.
        return True

    def check_model(self):
        self.scope.require(self.session)
        require(bool(self.contract.model_revision), 'versioned_vector_contract_required')
        rows = self.session.execute('SELECT runtime_version,dimension,document_contract_text::jsonb,query_prefix '
            'FROM em_security.vector_context(%s,%s,%s)', (self.lib, self.project, self.contract.key))
        require(rows == (('vector-runtime.v2', self.contract.dimension, self.contract.document_fields,
                         self.contract.query_prefix),), 'registered_project_vector_model_required')

    @staticmethod
    def _job(row):
        return Job(int(row['id']), str(row['library_id']), row['index_kind'], int(row['entity_id']),
                   row['action'], int(row['entity_revision']), row['content_hash'], row['model_contract'],
                   None if row['project_id'] is None else str(row['project_id']))

    def next_job(self):
        self.check_model()
        rows = self.session.execute("SELECT to_jsonb(j) FROM em_content.vector_outbox j WHERE library_id=%s "
            "AND project_id=%s AND model_contract=%s AND status='pending' ORDER BY id LIMIT 1",
            (self.lib, self.project, self.contract.key))
        return self._job(rows[0][0]) if rows else None

    def next_jobs(self, limit):
        """Private bounded candidate page for one native projection batch.

        Deliberately the same predicates as next_job with ``ORDER BY id`` and a
        validated ``LIMIT %s``. It bounds candidate rows, not distinct entities;
        callers deduplicate by entity in Python and never refill.
        """
        require(type(limit) is int and 1 <= limit <= 4, 'invalid_vector_candidate_batch')
        self.check_model()
        rows = self.session.execute("SELECT to_jsonb(j) FROM em_content.vector_outbox j WHERE library_id=%s "
            "AND project_id=%s AND model_contract=%s AND status='pending' ORDER BY id LIMIT %s",
            (self.lib, self.project, self.contract.key, limit))
        require(len(rows) <= limit, 'invalid_vector_candidate_batch')
        return tuple(self._job(row[0]) for row in rows)

    def _document(self, row, kind, text):
        require(str(row['library_id']) == self.lib and str(row['project_id']) == self.project, 'projection_project_mismatch')
        if kind == 'legacy':
            return Document(self.lib, kind, int(row['id']), int(row['revision']), text,
                            row['status'], row['expires_at'], project_id=self.project)
        return Document(self.lib, kind, int(row['id']), int(row['revision']), text,
            row['status'], row['expires_at'], row['content_type'], row['source_state'], tuple(row['projects']),
            row['personal_general'], row['experience_payload'] is not None, 0, self.project)

    def _entity_read(self, sql, params, lock):
        """Run one native entity read; only its locked form may mark a conflict.

        Callers pass the exact statement whose text they built, and the same
        ``lock`` flag that decided its ``FOR SHARE`` clause. Everything else -
        unlocked reads, layers, outbox, acknowledgement and commit - keeps the
        pre-existing redacted failure. The actual driver exception is inspected
        on the immediate context link; this is not a text match.
        """
        try:
            return self.session.execute(sql, params)
        except FoundationError as exc:
            if lock and is_serialization_failure(exc):
                raise PreEffectRowConflict() from None
            raise

    def document(self, job, *, lock=False):
        require((job.library_id, job.project_id) == (self.lib, self.project), 'projection_scope_mismatch')
        require(job.index_kind in ('legacy', 'context'), 'invalid_vector_job')
        if job.index_kind == 'legacy':
            sql = 'SELECT to_jsonb(m) FROM em_content.memories m WHERE library_id=%s AND project_id=%s AND id=%s'
            if lock: sql += ' FOR SHARE OF m'
            rows = self._entity_read(sql, (self.lib, self.project, job.entity_id), lock)
            return self._document(rows[0][0], 'legacy', rows[0][0]['value']) if rows else None
        sql = 'SELECT to_jsonb(i) FROM em_content.context_items i WHERE library_id=%s AND project_id=%s AND id=%s'
        if lock: sql += ' FOR SHARE OF i'
        rows = self._entity_read(sql, (self.lib, self.project, job.entity_id), lock)
        if not rows: return None
        sql = "SELECT content,content_hash FROM em_content.context_layers WHERE library_id=%s AND project_id=%s AND item_id=%s AND layer='l0'"
        if lock: sql += ' FOR SHARE'
        layers = self.session.execute(sql, (self.lib, self.project, job.entity_id))
        require(len(layers) == 1 and layers[0][1] == text_hash(layers[0][0]), 'context_layer_proof_invalid')
        return self._document(rows[0][0], 'context', layers[0][0])

    def documents(self, index_kind, ids):
        require(index_kind in ('legacy', 'context') and isinstance(ids, tuple) and len(ids) <= 240
                and all(type(i) is int and i > 0 for i in ids), 'invalid_vector_candidate_batch')
        if not ids: return {}
        selected = list(sorted(set(ids)))
        if index_kind == 'legacy':
            rows = self.session.execute('SELECT to_jsonb(m) FROM em_content.memories m '
                'WHERE library_id=%s AND project_id=%s AND id=ANY(%s)', (self.lib, self.project, selected))
            return {row['id']: self._document(row, 'legacy', row['value']) for (row,) in rows}
        rows = self.session.execute('''SELECT to_jsonb(i),l.content,l.content_hash
            FROM em_content.context_items i LEFT JOIN em_content.context_layers l
             ON l.library_id=i.library_id AND l.project_id=i.project_id AND l.item_id=i.id AND l.layer='l0'
            WHERE i.library_id=%s AND i.project_id=%s AND i.id=ANY(%s)''', (self.lib, self.project, selected))
        result = {}
        for row, text, digest in rows:
            require(isinstance(text, str) and digest == text_hash(text), 'context_layer_proof_invalid')
            result[row['id']] = self._document(row, 'context', text)
        return result

    def projection_proof(self, hits, embedding):
        require(isinstance(hits, tuple) and len(hits) <= 240 and len({h.entity_id for h in hits}) == len(hits)
                and len({h.index_kind for h in hits}) <= 1
                and all((h.library_id, h.project_id) == (self.lib, self.project) for h in hits), 'invalid_vector_candidate_batch')
        require(vector(embedding.values, self.contract) == embedding, 'prepared_embedding_invalid')
        if not hits: return (self.lib, self.project, self.contract.key, ())
        rows = self.session.execute('''SELECT entity_id,entity_revision,content_hash,dimension,
            projection_state,vector_hash,encode(sha256(public.vector_send(embedding)),'hex'),
            embedding <=> %s::public.vector FROM em_content.vector_projections
            WHERE library_id=%s AND project_id=%s AND model_contract=%s AND index_kind=%s
             AND entity_id=ANY(%s) ORDER BY entity_id''',
            (embedding.literal, self.lib, self.project, self.contract.key, hits[0].index_kind, sorted(h.entity_id for h in hits)))
        expected = {h.entity_id: h for h in hits}
        require(len(rows) == len(expected), 'recall_vector_changed')
        proof = []
        for ident, revision, digest, dimension, state, stored, actual, distance in rows:
            hit = expected.pop(ident, None)
            require(hit is not None and revision == hit.revision and digest == hit.content_hash
                    and dimension == self.contract.dimension and state == 'ready' and isinstance(stored, str)
                    and len(stored) == 64 and stored == actual and distance == hit.distance, 'recall_vector_changed')
            proof.append((ident, revision, digest, dimension, stored))
        require(not expected, 'recall_vector_changed')
        return (self.lib, self.project, self.contract.key, self.contract.query_prefix, embedding.digest, tuple(proof))

    def context_projection_proof(self, hits, embedding):
        require(all(h.index_kind == 'context' for h in hits), 'invalid_vector_candidate_batch')
        return self.projection_proof(hits, embedding)

    def qualification(self, hits):
        from evolvmem.pg_business_core.eligibility_read import PgEligibilityRead
        require(isinstance(hits, tuple) and len(hits) <= 240 and len({h.index_kind for h in hits}) <= 1
                and all((h.library_id, h.project_id) == (self.lib, self.project) for h in hits), 'invalid_vector_candidate_batch')
        reader = PgEligibilityRead(self.session, self.scope)
        mapping = {h.entity_id: h.entity_id for h in hits}
        if hits and hits[0].index_kind == 'legacy':
            rows = self.session.execute('SELECT legacy_memory_id,context_item_id FROM em_content.legacy_memory_migrations '
                'WHERE library_id=%s AND project_id=%s AND legacy_memory_id=ANY(%s)', (self.lib, self.project, sorted(mapping)))
            mapping = dict(rows)
            require(len(mapping) == len(rows), 'ambiguous_legacy_context_mapping')
        eligible = reader.eligible_ids(tuple(sorted(set(mapping.values()))))
        return tuple(h for h in hits if mapping.get(h.entity_id) in eligible), (reader.proof(), tuple(sorted(mapping.items())))

    def current_job(self, anchor, document):
        """Read one exact same-entity pending successor; never traverses links."""
        require(isinstance(anchor, Job) and isinstance(document, Document), 'typed_projection_input_required')
        require((anchor.library_id, anchor.project_id) == (self.lib, self.project)
                and (document.library_id, document.index_kind, document.entity_id, document.project_id)
                == (anchor.library_id, anchor.index_kind, anchor.entity_id, anchor.project_id),
                'projection_scope_mismatch')
        require(document.revision > anchor.revision, 'projection_pair_not_forward')
        rows = self.session.execute("SELECT to_jsonb(j) FROM em_content.vector_outbox j WHERE library_id=%s "
            "AND project_id=%s AND model_contract=%s AND index_kind=%s AND entity_id=%s "
            "AND entity_revision=%s AND content_hash=%s AND status='pending' AND id>%s "
            "ORDER BY id LIMIT 1",
            (self.lib, self.project, self.contract.key, anchor.index_kind, anchor.entity_id,
             document.revision, text_hash(document.text), anchor.id))
        return self._job(rows[0][0]) if rows else None

    def _claim_pair(self, anchor, current):
        """Claim only both exact ids, in deterministic id order, before effects."""
        require((anchor.library_id, anchor.project_id, anchor.index_kind, anchor.entity_id,
                 anchor.model_contract) == (current.library_id, current.project_id,
                 current.index_kind, current.entity_id, current.model_contract)
                and anchor.id != current.id, 'invalid_projection_pair')
        ids = tuple(sorted((anchor.id, current.id)))
        try:
            rows = self.session.execute("SELECT to_jsonb(j) FROM em_content.vector_outbox j "
                "WHERE library_id=%s AND project_id=%s AND model_contract=%s AND id=ANY(%s) "
                "AND status='pending' ORDER BY id FOR UPDATE SKIP LOCKED",
                (self.lib, self.project, self.contract.key, list(ids)))
        except FoundationError as exc:
            # This exact fixed claim SELECT precedes every vector write and ACK.
            if is_serialization_failure(exc):
                raise PreEffectRowConflict() from None
            raise
        claimed = {}
        for row in rows:
            job = self._job(row[0])
            require(job.id in ids and job.id not in claimed, 'projection_pair_claim_invalid')
            claimed[job.id] = job
        if anchor.id not in claimed:
            return None, claimed
        require(claimed[anchor.id] == anchor, 'prepared_job_changed')
        if claimed.get(current.id) != current:
            return None, claimed
        return claimed[current.id], claimed

    def apply_pair(self, prepared, now):
        """Publish the current job once, then ack current+anchor in one transaction."""
        require(self.session.route.action in ('write', 'admin'), 'vector_write_scope_required')
        require(isinstance(prepared, PreparedPair) and isinstance(prepared.anchor, Job)
                and isinstance(prepared.current, Job), 'prepared_pair_required')
        anchor, current = prepared.anchor, prepared.current
        require((anchor.library_id, anchor.project_id) == (self.lib, self.project)
                and (current.library_id, current.project_id) == (self.lib, self.project),
                'projection_scope_mismatch')
        require(current.id > anchor.id and current.revision > anchor.revision
                and prepared.decision in ('upsert', 'remove'),
                'invalid_projection_pair')
        require(prepared.decision != 'upsert' or prepared.embedding is not None,
                'prepared_embedding_missing')
        self.check_model()
        claimed_current, claimed = self._claim_pair(anchor, current)
        if anchor.id not in claimed:
            return PairApplied('already_claimed_or_processed', ())
        document = self.document(anchor, lock=True)
        anchor_decision = decide(anchor, document, self.contract, now)
        # The original anchor must remain genuinely stale; never forge a vector
        # or reuse this fallback to touch the current job or its projection.
        require(anchor_decision.startswith('stale_'), 'projection_pair_anchor_proof_changed')
        current_decision = decide(current, document, self.contract, now) if document is not None else 'stale_missing'
        exact = (document is not None and claimed_current is not None
                 and document.revision == current.revision
                 and text_hash(document.text) == current.content_hash
                 and anchor_decision == 'stale_revision'
                 and current_decision == prepared.decision
                 and current_decision in ('upsert', 'remove'))
        if not exact:
            self._ack(anchor, 'stale', anchor_decision)
            return PairApplied(anchor_decision, (anchor.id,))
        embedding = None
        if prepared.decision == 'upsert':
            embedding = vector(prepared.embedding.values, self.contract)
            require(embedding == prepared.embedding, 'prepared_embedding_invalid')
        self._write_projection(current, embedding)
        self._ack(current, 'completed', prepared.decision)
        self._ack(anchor, 'stale', anchor_decision)
        return PairApplied(prepared.decision, (anchor.id, current.id))

    def _write_projection(self, job, embedding):
        rows = self.session.execute('''INSERT INTO em_content.vector_projections
            (library_id,project_id,index_kind,entity_id,model_contract,dimension,entity_revision,content_hash,projection_state,embedding,vector_hash)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::public.vector,%s)
            ON CONFLICT(library_id,index_kind,entity_id,model_contract) DO UPDATE SET
             entity_revision=excluded.entity_revision,content_hash=excluded.content_hash,
             projection_state=excluded.projection_state,embedding=excluded.embedding,
             vector_hash=excluded.vector_hash,updated_at=clock_timestamp()
            WHERE vector_projections.project_id=excluded.project_id
             AND vector_projections.entity_revision<=excluded.entity_revision
             AND (vector_projections.entity_revision<excluded.entity_revision OR vector_projections.content_hash=excluded.content_hash)
            RETURNING entity_id''', (self.lib, self.project, job.index_kind, job.entity_id, self.contract.key,
                self.contract.dimension, job.revision, job.content_hash, 'ready' if embedding else 'removed',
                None if embedding is None else embedding.literal, None if embedding is None else embedding.pg_digest))
        require(rows == ((job.entity_id,),), 'projection_revision_conflict')

    def apply(self, prepared, now):
        require(self.session.route.action in ('write', 'admin'), 'vector_write_scope_required')
        require(isinstance(prepared, Prepared), 'prepared_vector_required')
        job = prepared.job
        require((job.library_id, job.project_id) == (self.lib, self.project), 'projection_scope_mismatch')
        self.check_model()
        rows = self.session.execute("SELECT to_jsonb(j) FROM em_content.vector_outbox j WHERE library_id=%s "
            "AND project_id=%s AND id=%s AND status='pending' FOR UPDATE SKIP LOCKED", (self.lib, self.project, job.id))
        if not rows: return 'already_claimed_or_processed'
        require(len(rows) == 1 and self._job(rows[0][0]) == job, 'prepared_job_changed')
        decision = decide(job, self.document(job, lock=True), self.contract, now)
        if decision.startswith('stale_'):
            self._ack(job, 'stale', decision)
            return decision
        embedding = None
        if decision == 'upsert':
            require(prepared.embedding is not None, 'prepared_embedding_missing')
            embedding = vector(prepared.embedding.values, self.contract)
            require(embedding == prepared.embedding, 'prepared_embedding_invalid')
        self._write_projection(job, embedding)
        self._ack(job, 'completed', decision)
        return decision

    def _ack(self, job, status, result):
        rows = self.session.execute("UPDATE em_content.vector_outbox SET status=%s,result_code=%s,processed_at=clock_timestamp() "
            "WHERE library_id=%s AND project_id=%s AND id=%s AND status='pending' RETURNING id",
            (status, result, self.lib, self.project, job.id))
        require(rows == ((job.id,),), 'vector_job_ack_failed')

    def counts(self):
        rows = self.session.execute("SELECT count(*) FILTER (WHERE model_contract=%s),count(*) FILTER (WHERE model_contract<>%s) "
            "FROM em_content.vector_outbox WHERE library_id=%s AND project_id=%s AND status='pending'",
            (self.contract.key, self.contract.key, self.lib, self.project))
        return dict(pending_for_model=int(rows[0][0]), pending_other_contracts=int(rows[0][1]))

    def enqueue_current_page(self, index_kind, after_id, limit, now):
        """Explicit current-entity backfill. Switch writers to this contract first.

        Concurrent mutations enqueue their own latest revision; this snapshot
        may add a stale job, which apply() cannot publish over a newer version.
        """
        require(self.session.route.action in ('write', 'admin'), 'vector_write_scope_required')
        require(index_kind in ('legacy', 'context') and type(after_id) is int and after_id >= 0
                and type(limit) is int and 1 <= limit <= 64, 'invalid_backfill_page')
        self.check_model()
        table = 'em_content.memories' if index_kind == 'legacy' else 'em_content.context_items'
        ids = self.session.execute('SELECT id FROM ' + table + ' WHERE library_id=%s AND project_id=%s AND id>%s ORDER BY id LIMIT %s',
                                  (self.lib, self.project, after_id, limit + 1))
        selected = tuple(row[0] for row in ids[:limit])
        documents = self.documents(index_kind, selected)
        queued = 0
        for ident in selected:
            doc = documents[ident]
            present = doc.status != 'deleted' if index_kind == 'legacy' else doc.status in ('active', 'superseded') and (doc.expires_at is None or doc.expires_at > now)
            action = 'upsert' if present else 'remove'
            rows = self.session.execute('''INSERT INTO em_content.vector_outbox
                (library_id,project_id,index_kind,entity_id,action,entity_revision,content_hash,model_contract,created_at)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(library_id,index_kind,entity_id,entity_revision,action,model_contract) DO NOTHING RETURNING id''',
                (self.lib, self.project, index_kind, ident, action, doc.revision, text_hash(doc.text), self.contract.key, now))
            queued += len(rows)
        return BackfillPage(index_kind, selected[-1] if selected else after_id, len(selected), queued, len(ids) <= limit)

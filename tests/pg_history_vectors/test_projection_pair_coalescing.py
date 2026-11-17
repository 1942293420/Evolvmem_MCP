"""Native stale-anchor/current job coalescing: one request, one model encode.

No DB, model or service: the repository's exact pair-claim/lock/ack SQL
boundary is scripted, and the runtime prepare path uses only typed doubles.
"""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from evolvmem.library_foundation._projection_conflict import PreEffectRowConflict
from evolvmem.pg_vector_port import projection as projection_module
from evolvmem.pg_vector_port import repository as repository_module
from evolvmem.pg_vector_port import runtime as runtime_module
from evolvmem.pg_vector_port.model import ModelContract, text_hash, vector
from evolvmem.pg_vector_port.projection import Document, Job, PreparedPair
from evolvmem.pg_vector_port.repository import VectorRepository
from evolvmem.pg_vector_port.runtime import (ProjectionResult, ProjectionSerializationDeferred,
                                            VectorPort)

CONTRACT = ModelContract('model-a', 768, 'doc:', 'query:', 512, 'a' * 64)
TEXT = 'current body'
HASH = text_hash(TEXT)
EMBEDDING = vector((.5,) * 768, CONTRACT)


def anchor_job(**kw):
    values = dict(id=1, library_id='lib', index_kind='context', entity_id=9, action='remove',
                  revision=1, content_hash='0' * 64, model_contract=CONTRACT.key, project_id='proj')
    values.update(kw)
    return Job(**values)


def current_job(**kw):
    values = dict(id=2, library_id='lib', index_kind='context', entity_id=9, action='upsert',
                  revision=2, content_hash=HASH, model_contract=CONTRACT.key, project_id='proj')
    values.update(kw)
    return Job(**values)


def document(revision=2, text=TEXT, status='active'):
    return Document('lib', 'context', 9, revision, text, status, None, 'decision', 'ready',
                    ('alpha',), False, False, project_id='proj')


def job_row(job):
    return dict(id=job.id, library_id=job.library_id, index_kind=job.index_kind,
                entity_id=job.entity_id, action=job.action, entity_revision=job.revision,
                content_hash=job.content_hash, model_contract=job.model_contract,
                project_id=job.project_id)


def item_row(doc):
    return dict(id=doc.entity_id, library_id=doc.library_id, project_id=doc.project_id,
                revision=doc.revision, status=doc.status, expires_at=doc.expires_at,
                content_type=doc.content_type, source_state=doc.source_state,
                projects=list(doc.projects), personal_general=doc.personal_general,
                experience_payload=None)


class ScriptedSession:
    """Exact SQL boundary: returns prepared rows and records every statement."""
    def __init__(self, claim_rows, item, layer=(TEXT, HASH), claim_error=None):
        self.route = SimpleNamespace(action='write')
        self.claim_rows, self.item, self.layer = claim_rows, item, layer
        self.claim_error, self.calls, self.acks = claim_error, [], []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if sql.startswith('SELECT to_jsonb(j) FROM em_content.vector_outbox j WHERE library_id=%s '
                          'AND project_id=%s AND model_contract=%s AND id=ANY(%s)'):
            if self.claim_error is not None:
                raise self.claim_error
            return tuple((job_row(j),) for j in self.claim_rows
                         if j.id in params[3] and params[0:3] == (j.library_id, j.project_id,
                                                                  j.model_contract))
        if sql.startswith('SELECT to_jsonb(i) FROM em_content.context_items i'):
            return (((self.item,),) if self.item is not None else ())
        if sql.startswith('SELECT content,content_hash FROM em_content.context_layers'):
            return (self.layer,)
        if sql.startswith('INSERT INTO em_content.vector_projections'):
            return ((params[3],),)
        if sql.startswith('UPDATE em_content.vector_outbox SET status='):
            self.acks.append((params[0], params[1], params[4]))
            return ((params[4],),)
        raise AssertionError('unexpected SQL: ' + sql)


def repository(session, contract=CONTRACT):
    repo = object.__new__(VectorRepository)
    repo.session, repo.contract = session, contract
    repo.scope = SimpleNamespace(require=lambda session: True)
    repo.lib, repo.project = 'lib', 'proj'
    repo.check_model = lambda: None
    return repo


class PairApplyTests(unittest.TestCase):
    def test_both_jobs_valid_use_one_current_write_and_ack_both_in_order(self):
        anchor, current, doc = anchor_job(), current_job(), document()
        session = ScriptedSession((anchor, current), item_row(doc))
        pair = PreparedPair(anchor, current, 'upsert', EMBEDDING)
        applied = repository(session).apply_pair(pair, '2026-01-01 00:00:00')

        self.assertEqual(applied.outcome, 'upsert')
        self.assertEqual(applied.acknowledged_job_ids, (anchor.id, current.id))
        self.assertEqual([a[2] for a in session.acks], [current.id, anchor.id])
        self.assertEqual([(a[0], a[1]) for a in session.acks],
                         [('completed', 'upsert'), ('stale', 'stale_revision')])
        writes = [c for c in session.calls if c[0].startswith('INSERT INTO em_content.vector_projections')]
        self.assertEqual(len(writes), 1)
        # The single write is the current job's revision, never the stale anchor's.
        self.assertEqual((writes[0][1][3], writes[0][1][6]), (current.entity_id, current.revision))

    def test_remove_current_needs_no_embedding_and_removes_the_projection(self):
        anchor, current = anchor_job(), current_job(action='remove')
        doc = document(status='deleted')
        session = ScriptedSession((anchor, current), item_row(doc))
        pair = PreparedPair(anchor, current, 'remove', None)
        applied = repository(session).apply_pair(pair, '2026-01-01 00:00:00')
        self.assertEqual(applied.outcome, 'remove')
        self.assertEqual(applied.acknowledged_job_ids, (anchor.id, current.id))
        writes = [c for c in session.calls if c[0].startswith('INSERT INTO em_content.vector_projections')]
        self.assertEqual(len(writes), 1)
        self.assertIsNone(writes[0][1][9])          # embedding
        self.assertEqual(writes[0][1][8], 'removed')  # projection_state

    def test_anchor_not_claimed_never_touches_current_or_projection(self):
        anchor, current, doc = anchor_job(), current_job(), document()
        session = ScriptedSession((current,), item_row(doc))
        applied = repository(session).apply_pair(
            PreparedPair(anchor, current, 'upsert', EMBEDDING), '2026-01-01 00:00:00')
        self.assertEqual(applied.outcome, 'already_claimed_or_processed')
        self.assertEqual(applied.acknowledged_job_ids, ())
        self.assertEqual(session.acks, [])
        self.assertFalse(any(c[0].startswith('INSERT INTO em_content.vector_projections')
                             for c in session.calls))

    def test_current_missing_or_locked_acks_only_the_original_anchor(self):
        anchor, current, doc = anchor_job(), current_job(), document()
        session = ScriptedSession((anchor,), item_row(doc))
        applied = repository(session).apply_pair(
            PreparedPair(anchor, current, 'upsert', EMBEDDING), '2026-01-01 00:00:00')
        self.assertEqual(applied.outcome, 'stale_revision')
        self.assertEqual(applied.acknowledged_job_ids, (anchor.id,))
        self.assertEqual(session.acks, [('stale', 'stale_revision', anchor.id)])
        self.assertFalse(any(c[0].startswith('INSERT INTO em_content.vector_projections')
                             for c in session.calls))

    def test_current_document_drift_falls_back_to_anchor_stale_only(self):
        anchor, current = anchor_job(), current_job()
        session = ScriptedSession((anchor, current), item_row(document(revision=3, text='newer')))
        applied = repository(session).apply_pair(
            PreparedPair(anchor, current, 'upsert', EMBEDDING), '2026-01-01 00:00:00')
        self.assertEqual(applied.acknowledged_job_ids, (anchor.id,))
        self.assertEqual(session.acks, [('stale', 'stale_revision', anchor.id)])

    def test_anchor_no_longer_stale_fails_closed_without_ack_or_write(self):
        anchor, current = anchor_job(action='upsert', revision=2, content_hash=HASH), current_job(revision=3, content_hash='1' * 64)
        session = ScriptedSession((anchor, current), item_row(document()))
        with self.assertRaises(Exception) as raised:
            repository(session).apply_pair(
                PreparedPair(anchor, current, 'upsert', EMBEDDING), '2026-01-01 00:00:00')
        self.assertEqual(getattr(raised.exception, 'code', None),
                         'projection_pair_anchor_proof_changed')
        self.assertEqual(session.acks, [])

    def test_pair_claim_40001_is_the_only_pre_effect_conflict_sentinel(self):
        anchor, current = anchor_job(), current_job()
        failure = runtime_module.FoundationError('database_operation_failed')
        failure.__context__ = RuntimeError('synthetic 40001')
        session = ScriptedSession((), None, claim_error=failure)
        with patch.object(repository_module, 'is_serialization_failure', lambda exc: True):
            with self.assertRaises(PreEffectRowConflict):
                repository(session).apply_pair(
                    PreparedPair(anchor, current, 'upsert', EMBEDDING), '2026-01-01 00:00:00')
        self.assertEqual(session.acks, [])

    def test_forged_rewound_current_pair_rejects_before_claim_or_effect(self):
        anchor, current = anchor_job(revision=3), current_job(revision=2)
        session = ScriptedSession((anchor, current), item_row(document()))
        with self.assertRaises(Exception) as raised:
            repository(session).apply_pair(
                PreparedPair(anchor, current, 'upsert', EMBEDDING), '2026-01-01 00:00:00')
        self.assertEqual(getattr(raised.exception, 'code', None), 'invalid_projection_pair')
        self.assertEqual(session.calls, [])

    def test_pair_claim_other_database_error_is_not_swallowed(self):
        session = ScriptedSession((), None,
                                  claim_error=runtime_module.FoundationError('database_operation_failed'))
        with patch.object(repository_module, 'is_serialization_failure', lambda exc: False):
            with self.assertRaises(runtime_module.FoundationError):
                repository(session).apply_pair(
                    PreparedPair(anchor_job(), current_job(), 'upsert', EMBEDDING),
                    '2026-01-01 00:00:00')


class PairLookupTests(unittest.TestCase):
    def test_lookup_is_exactly_typed_and_scoped_and_ordered(self):
        session = ScriptedSession((), None)
        repo = repository(session)
        session.execute = lambda sql, params=(): (session.calls.append((sql, params)) or ())
        anchor, doc = anchor_job(), document()
        self.assertIsNone(repo.current_job(anchor, doc))
        sql, params = session.calls[-1]
        self.assertIn("status='pending'", sql)
        self.assertIn('entity_revision=%s', sql)
        self.assertIn('content_hash=%s', sql)
        self.assertIn('id>%s', sql)
        self.assertIn('ORDER BY id LIMIT 1', sql)
        self.assertEqual(params, ('lib', 'proj', CONTRACT.key, 'context', 9, 2, HASH, anchor.id))


class Guard:
    def begin_authority(self): pass
    def check(self): pass
    @contextmanager
    def enforce(self): yield


def runtime_instance(repository_double, *, cache_lookup=None):
    port = object.__new__(VectorPort)
    port.project_key = 'proj'
    port.contract = CONTRACT
    port.guard = Guard()
    port.cache_lookup = cache_lookup
    port._historical_libraries = set()
    port._scope = lambda route: route
    port.repository = lambda session: repository_double
    def run(route, operation):
        try:
            return operation(None)
        except PreEffectRowConflict:
            raise runtime_module.ProjectionConflictDeferred() from None
    port.manager = SimpleNamespace(run=run)
    port.encoded = []
    port._embedding = lambda text, *, query: (port.encoded.append(text), EMBEDDING)[1]
    return port


class RuntimePrepareTests(unittest.TestCase):
    def repository_double(self, decision='stale_revision', current=None):
        anchor, doc = anchor_job(), document()
        calls = []
        class Repo:
            def next_job(inner): return anchor
            def document(inner, job, *, lock=False): return doc
            def current_job(inner, job, document):
                calls.append(('current_job', job, document))
                return current
            def cache_entry(inner, job): return None
            def apply(inner, prepared, now):
                calls.append(('apply', prepared))
                return decision
            def apply_pair(inner, pair, now):
                calls.append(('apply_pair', pair))
                return projection_module.PairApplied(pair.decision, (anchor.id, pair.current.id))
        return Repo(), anchor, calls

    def test_budget_two_pairs_stale_anchor_with_exact_current_using_one_encode(self):
        repo, anchor, calls = self.repository_double(current=current_job())
        port = runtime_instance(repo)
        result = port.project_one(SimpleNamespace(action='write'), completion_budget=2)
        self.assertEqual(result.acknowledged_job_ids, (anchor.id, 2))
        self.assertEqual(port.encoded, [TEXT])
        self.assertEqual([c[0] for c in calls], ['current_job', 'apply_pair'])
        self.assertEqual(calls[-1][1].decision, 'upsert')

    def test_committed_stale_fallback_returns_one_anchor_ack_not_an_error(self):
        repo, anchor, _ = self.repository_double(current=current_job())
        repo.apply_pair = lambda pair, now: projection_module.PairApplied(
            'stale_revision', (anchor.id,))
        port = runtime_instance(repo)
        result = port.project_one(SimpleNamespace(action='write'), completion_budget=2)
        self.assertEqual(result.outcome, 'stale_revision')
        self.assertEqual(result.acknowledged_job_ids, (anchor.id,))
        self.assertEqual(result.acknowledged_job_count, 1)

    def test_committed_no_claim_returns_zero_ack_not_an_error(self):
        repo, anchor, _ = self.repository_double(current=current_job())
        repo.apply_pair = lambda pair, now: projection_module.PairApplied(
            'already_claimed_or_processed', ())
        port = runtime_instance(repo)
        result = port.project_one(SimpleNamespace(action='write'), completion_budget=2)
        self.assertEqual(result.outcome, 'already_claimed_or_processed')
        self.assertEqual(result.acknowledged_job_ids, ())
        self.assertEqual(result.acknowledged_job_count, 0)

    def test_prepare_forged_rewound_current_rejects_before_encoding(self):
        repo, anchor, calls = self.repository_double(current=current_job(revision=1))
        port = runtime_instance(repo)
        with self.assertRaises(Exception) as raised:
            port.project_one(SimpleNamespace(action='write'), completion_budget=2)
        self.assertEqual(getattr(raised.exception, 'code', None), 'invalid_projection_pair')
        self.assertEqual(port.encoded, [])
        self.assertEqual([c[0] for c in calls], ['current_job'])

    def test_default_budget_one_keeps_existing_single_stale_semantics(self):
        repo, anchor, calls = self.repository_double(current=current_job())
        port = runtime_instance(repo)
        result = port.project_one(SimpleNamespace(action='write'))
        self.assertEqual(result.acknowledged_job_ids, (anchor.id,))
        self.assertEqual(port.encoded, [])
        self.assertEqual([c[0] for c in calls], ['apply'])

    def test_superseded_context_remove_action_is_still_encoded_as_upsert(self):
        repo, anchor, calls = self.repository_double(current=current_job(action='remove'))
        doc = document(status='superseded')
        repo.document = lambda job, *, lock=False: doc
        port = runtime_instance(repo)
        result = port.project_one(SimpleNamespace(action='write'), completion_budget=2)
        self.assertEqual(result.acknowledged_job_ids, (anchor.id, 2))
        self.assertEqual(port.encoded, [TEXT])
        self.assertEqual(calls[-1][1].decision, 'upsert')

    def test_no_forward_revision_or_non_stale_anchor_never_looks_up_current(self):
        cases = ((document(revision=1), HASH),            # stale_action, no forward revision
                 (document(revision=1), '0' * 64))       # stale_content, not stale_revision
        for doc, anchor_hash in cases:
            repo, anchor, calls = self.repository_double(current=current_job())
            repo.next_job = lambda: anchor_job(content_hash=anchor_hash)
            repo.document = lambda job, *, lock=False: doc
            port = runtime_instance(repo)
            port.project_one(SimpleNamespace(action='write'), completion_budget=2)
            self.assertNotIn('current_job', [c[0] for c in calls])

    def test_budget_is_exact_and_bounded(self):
        port = runtime_instance(self.repository_double()[0])
        for bad in (0, 3, True, 1.5, '2', None):
            with self.subTest(bad=bad):
                with self.assertRaises(Exception) as raised:
                    port.project_one(SimpleNamespace(action='write'), completion_budget=bad)
                self.assertEqual(getattr(raised.exception, 'code', None),
                                 'invalid_vector_completion_budget')


class ConflictMappingTests(unittest.TestCase):
    def test_pre_effect_pair_conflict_maps_to_serialization_deferred(self):
        repo, _, _ = RuntimePrepareTests().repository_double(current=current_job())
        def conflict(pair, now):
            raise PreEffectRowConflict()
        repo.apply_pair = conflict
        port = runtime_instance(repo)
        with self.assertRaises(ProjectionSerializationDeferred):
            port.project_one(SimpleNamespace(action='write'), completion_budget=2)


if __name__ == '__main__':
    unittest.main()

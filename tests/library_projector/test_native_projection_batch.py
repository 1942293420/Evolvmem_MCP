"""Native bounded projection batch: planning, aggregation and worker dispatch.

No database, model or network is opened. The repository's exact SQL boundary is
scripted and the runtime uses typed doubles, mirroring the existing native pair
and session tests.
"""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from evolvmem.library_foundation._projection_conflict import (PreEffectRowConflict,
                                                              ProjectionBatchConflict)
from evolvmem.pg_vector_port.model import ModelContract, text_hash, vector
from evolvmem.pg_vector_port.projection import Document, Job, PairApplied, PreparedPair
from evolvmem.pg_vector_port.repository import VectorRepository
from evolvmem.library_foundation._projection_conflict import ProjectionBatchDeferred
from evolvmem.pg_vector_port.runtime import (ProjectionBatchResult, ProjectionResult,
                                            ProjectionBatchSerializationDeferred, VectorPort,
                                            _NativeProjectionPort)
from evolvmem.pg_vector_port.worker import ProjectionWorker

CONTRACT = ModelContract('model-a', 768, 'doc:', 'query:', 512, 'a' * 64)
TEXT = 'current body'
HASH = text_hash(TEXT)
EMBEDDING = vector((.5,) * 768, CONTRACT)


def job(ident=1, *, entity=9, kind='context', revision=2, action='upsert',
        digest=HASH, project='proj'):
    return Job(ident, 'lib', kind, entity, action, revision, digest, CONTRACT.key, project)


def document(*, entity=9, revision=2, status='active', text=TEXT):
    return Document('lib', 'context', entity, revision, text, status, None, 'decision',
                    'ready', ('proj',), False, False, project_id='proj')


def job_row(value):
    return dict(id=value.id, library_id=value.library_id, index_kind=value.index_kind,
                entity_id=value.entity_id, action=value.action,
                entity_revision=value.revision, content_hash=value.content_hash,
                model_contract=value.model_contract, project_id=value.project_id)


class CandidateSession:
    """Only the new bounded candidate SELECT plus the existing document read."""
    def __init__(self, rows=()):
        self.rows, self.calls = tuple(rows), []

    def execute(self, sql, params=()):
        self.calls.append((sql, params))
        if 'status=\'pending\' ORDER BY id LIMIT %s' in sql:
            return tuple((job_row(row),) for row in self.rows[:params[3]])
        raise AssertionError('unexpected SQL: ' + sql)


def repository(session):
    repo = object.__new__(VectorRepository)
    repo.session, repo.contract = session, CONTRACT
    repo.scope = SimpleNamespace(require=lambda session: True)
    repo.lib, repo.project = 'lib', 'proj'
    repo.check_model = lambda: None
    return repo


class RepositoryCandidateTests(unittest.TestCase):
    def test_bounded_candidate_select_orders_and_limits_with_exact_scope(self):
        session = CandidateSession((job(3), job(1), job(2)))
        rows = repository(session).next_jobs(4)
        self.assertEqual((3, 1, 2), tuple(row.id for row in rows))
        sql, params = session.calls[0]
        self.assertIn("status='pending' ORDER BY id LIMIT %s", sql)
        self.assertEqual((sql.count('%s')), 4)
        self.assertEqual(('lib', 'proj', CONTRACT.key, 4), params)

    def test_candidate_limit_must_be_a_plain_int_in_range(self):
        repo = repository(CandidateSession())
        for bad in (0, 5, True, 2.0, '2'):
            with self.subTest(bad=bad):
                with self.assertRaises(Exception):
                    repo.next_jobs(bad)


class Op:
    def __init__(self, callback, kind):
        self._callback, self.kind = callback, kind

    def __call__(self, session):
        return self._callback(session)


class Manager:
    """Counts exactly one RO prepare and one fresh RW apply transaction."""
    def __init__(self, repo):
        self.repo, self.prepare_calls, self.apply_calls = repo, 0, 0

    def _projection_prepare(self, callback):
        return Op(callback, 'prepare')

    def _projection_batch_apply(self, callback):
        return Op(callback, 'batch_apply')

    def run(self, route, operation, publish=None):
        if operation.kind == 'prepare':
            self.prepare_calls += 1
        else:
            self.apply_calls += 1
        return operation(object())


class Repo:
    """Scripted native repository boundary used only by the runtime."""
    def __init__(self, candidates, documents, *, successors=(), pair_result=None,
                 outcomes=None, fail_at=None):
        self.candidates = tuple(candidates)
        self.documents = dict(documents)
        self.successors = dict(successors)
        self.pair_result = pair_result
        self.outcomes = dict(outcomes or {})
        self.fail_at = fail_at
        self.in_transaction = False
        self.applied = []
        self.encoded_during_transaction = []

    def check_schema(self):
        self.schema_calls = getattr(self, 'schema_calls', 0) + 1
        return True

    def next_jobs(self, limit):
        return self.candidates[:limit]

    def document(self, value):
        return self.documents.get(value.id)

    def current_job(self, anchor, doc):
        return self.successors.get(anchor.id)

    def apply(self, prepared, now):
        self.applied.append(prepared.job.id)
        if self.fail_at == prepared.job.id:
            raise PreEffectRowConflict()
        return self.outcomes.get(prepared.job.id, 'upsert')

    def apply_pair(self, prepared, now):
        self.applied.append((prepared.anchor.id, prepared.current.id))
        if self.fail_at == prepared.current.id:
            raise PreEffectRowConflict()
        return self.pair_result


class Guard:
    timeout_seconds = 10
    def enforce(self):
        return contextmanager(lambda: (yield))()
    def begin_authority(self):
        pass
    def check(self):
        pass


def port(manager, repo, encode=None):
    value = object.__new__(_NativeProjectionPort)
    value.guard = Guard()
    value.manager = manager
    value.contract = CONTRACT
    value.routes = (object(),)
    value.project_key = 'proj'
    value.provider = None
    value.cache_lookup = None
    value._projection_feedback = None
    value._scope = lambda route: route
    value.repository = lambda session: repo
    def default_encode(text, query):
        repo.encoded_during_transaction.append(repo.in_transaction)
        return EMBEDDING
    value._embedding = encode or default_encode
    return value


class RuntimeBatchTests(unittest.TestCase):
    def test_three_distinct_entities_use_one_ro_prepare_and_one_fresh_rw_apply(self):
        values = (job(1, entity=1), job(2, entity=2), job(3, entity=3))
        repo = Repo(values, {v.id: document(entity=v.entity_id) for v in values})
        manager = Manager(repo)
        batch = port(manager, repo)
        result = batch.project_batch(SimpleNamespace(action='write'), completion_budget=3)
        self.assertEqual((1, 2, 3), result.acknowledged_job_ids)
        self.assertEqual((1, 1), (manager.prepare_calls, manager.apply_calls))
        self.assertEqual([False, False, False], repo.encoded_during_transaction)
        self.assertEqual([1, 2, 3], repo.applied)

    def test_budget_bounds_planned_and_acknowledged_rows(self):
        values = tuple(job(i, entity=i) for i in range(1, 5))
        for budget in (1, 2, 3, 4):
            with self.subTest(budget=budget):
                repo = Repo(values, {v.id: document(entity=v.entity_id) for v in values})
                manager = Manager(repo)
                result = port(manager, repo).project_batch(
                    SimpleNamespace(action='write'), completion_budget=budget)
                self.assertLessEqual(len(result.acknowledged_job_ids), budget)
                self.assertLessEqual(len(repo.applied), budget)

    def test_invalid_budget_bool_zero_or_five_is_rejected_before_any_read(self):
        repo = Repo((), {})
        manager = Manager(repo)
        for bad in (True, 0, 5, 2.0):
            with self.subTest(bad=bad):
                with self.assertRaises(Exception):
                    port(manager, repo).project_batch(
                        SimpleNamespace(action='write'), completion_budget=bad)
        self.assertEqual(0, manager.prepare_calls)

    def test_four_candidates_of_one_entity_make_a_smaller_batch_without_refill(self):
        values = (job(1, entity=9, revision=1), job(2, entity=9, revision=2),
                  job(3, entity=9, revision=3), job(4, entity=9, revision=4))
        repo = Repo(values, {1: document(entity=9, revision=1)})
        manager = Manager(repo)
        result = port(manager, repo).project_batch(
            SimpleNamespace(action='write'), completion_budget=4)
        self.assertEqual((1,), result.acknowledged_job_ids)
        self.assertEqual([1], repo.applied)
        self.assertEqual(1, manager.apply_calls)

    def test_same_numeric_id_in_legacy_and_context_are_distinct_entities(self):
        values = (job(1, entity=9, kind='legacy'), job(2, entity=9, kind='context'))
        repo = Repo(values, {
            1: Document('lib', 'legacy', 9, 2, TEXT, 'active', None, project_id='proj'),
            2: document(entity=9, revision=2)})
        manager = Manager(repo)
        result = port(manager, repo).project_batch(
            SimpleNamespace(action='write'), completion_budget=2)
        self.assertEqual((1, 2), result.acknowledged_job_ids)

    def test_pair_reserves_successor_outside_candidates_and_acks_once_each(self):
        anchor = job(1, entity=9, revision=1, digest='0' * 64)
        current = job(2, entity=9, revision=2)
        repo = Repo((anchor,), {1: document(entity=9, revision=2)},
                    successors={1: current},
                    pair_result=PairApplied('upsert', (1, 2)))
        manager = Manager(repo)
        result = port(manager, repo).project_batch(
            SimpleNamespace(action='write'), completion_budget=2)
        self.assertEqual((1, 2), result.acknowledged_job_ids)
        self.assertEqual([(1, 2)], repo.applied)
        self.assertEqual(1, manager.apply_calls)

    def test_pair_is_never_formed_when_only_one_slot_remains(self):
        anchor = job(1, entity=9, revision=1, digest='0' * 64)
        repo = Repo((anchor,), {1: document(entity=9, revision=2)},
                    successors={1: job(2, entity=9)})
        manager = Manager(repo)
        result = port(manager, repo).project_batch(
            SimpleNamespace(action='write'), completion_budget=1)
        self.assertEqual((1,), result.acknowledged_job_ids)
        self.assertEqual([1], repo.applied)

    def test_foreign_or_duplicate_aggregate_acknowledgement_is_rejected(self):
        anchor = job(1, entity=9, revision=1, digest='0' * 64)
        current = job(2, entity=9)
        repo = Repo((anchor,), {1: document(entity=9, revision=2)},
                    successors={1: current},
                    pair_result=PairApplied('upsert', (1, 99)))
        manager = Manager(repo)
        with self.assertRaises(Exception):
            port(manager, repo).project_batch(
                SimpleNamespace(action='write'), completion_budget=2)

    def test_batch_conflict_is_not_deferred_by_a_plain_apply(self):
        values = (job(1, entity=1), job(2, entity=2))
        repo = Repo(values, {1: document(entity=1), 2: document(entity=2)}, fail_at=2)
        manager = Manager(repo)
        with self.assertRaises(ProjectionBatchConflict):
            port(manager, repo).project_batch(
                SimpleNamespace(action='write'), completion_budget=2)
        self.assertEqual([1, 2], repo.applied)
        batch = port(manager, repo)
        self.assertIsNone(batch._projection_result)


class ForgedAggregateManager(Manager):
    def __init__(self, repo, aggregate):
        super().__init__(repo)
        self.aggregate = aggregate

    def run(self, route, operation, publish=None):
        if operation.kind == 'prepare':
            self.prepare_calls += 1
            return operation(object())
        self.apply_calls += 1
        return self.aggregate


class DeferredManager(Manager):
    def run(self, route, operation, publish=None):
        if operation.kind == 'prepare':
            self.prepare_calls += 1
            return operation(object())
        self.apply_calls += 1
        raise ProjectionBatchDeferred()


class RuntimeDeferralTests(unittest.TestCase):
    def test_post_cleanup_batch_deferred_maps_to_separate_sentinel_and_clears(self):
        values = (job(1, entity=1), job(2, entity=2))
        repo = Repo(values, {1: document(entity=1), 2: document(entity=2)})
        manager = DeferredManager(repo)
        batch = port(manager, repo)
        with self.assertRaises(ProjectionBatchSerializationDeferred) as raised:
            batch.project_batch(SimpleNamespace(action='write'), completion_budget=2)
        self.assertEqual('projection_batch_serialization_deferred', raised.exception.code)
        self.assertIsNone(batch._projection_result)
        self.assertEqual((1, 1), (manager.prepare_calls, manager.apply_calls))
        self.assertEqual([], repo.applied)


class EmptyAndHistoryTests(unittest.TestCase):
    def test_empty_queue_still_checks_schema_and_opens_no_apply(self):
        repo = Repo((), {})
        manager = Manager(repo)
        result = port(manager, repo).project_batch(
            SimpleNamespace(action='write'), completion_budget=3)
        self.assertEqual((), result.acknowledged_job_ids)
        self.assertEqual(1, repo.schema_calls)
        self.assertEqual((1, 0), (manager.prepare_calls, manager.apply_calls))

    def test_read_role_is_rejected_before_any_candidate_or_encode(self):
        repo = Repo((), {})
        manager = Manager(repo)
        with self.assertRaises(Exception):
            port(manager, repo).project_batch(
                SimpleNamespace(action='read'), completion_budget=1)
        self.assertEqual((0, 0), (manager.prepare_calls, manager.apply_calls))
        self.assertFalse(getattr(repo, 'schema_calls', 0))

    def test_history_repository_during_prepare_is_fatal(self):
        from evolvmem.pg_vector_port.history_repository import HistoryVectorRepository
        history = object.__new__(HistoryVectorRepository)
        repo = Repo((job(1, entity=1),), {1: document(entity=1)})
        manager = Manager(repo)
        batch = port(manager, repo)
        batch.repository = lambda session: history
        with self.assertRaises(Exception):
            batch.project_batch(SimpleNamespace(action='write'), completion_budget=1)
        self.assertEqual(0, manager.apply_calls)

    def test_history_repository_after_encode_is_fatal_without_apply(self):
        from evolvmem.pg_vector_port.history_repository import HistoryVectorRepository
        history = object.__new__(HistoryVectorRepository)
        values = (job(1, entity=1),)
        repo = Repo(values, {1: document(entity=1)})
        manager = Manager(repo)
        batch = port(manager, repo)
        class SwitchingManager(Manager):
            def run(self, route, operation, publish=None):
                if operation.kind == 'batch_apply':
                    batch.repository = lambda session: history
                return super().run(route, operation)
        manager.__class__ = SwitchingManager
        with self.assertRaises(Exception):
            batch.project_batch(SimpleNamespace(action='write'), completion_budget=1)
        self.assertEqual([], repo.applied)
        self.assertIsNone(batch._projection_result)


class AggregateValidationTests(unittest.TestCase):
    def _batch(self, aggregate, budget=2):
        values = (job(1, entity=1), job(2, entity=2))
        repo = Repo(values, {1: document(entity=1), 2: document(entity=2)})
        manager = ForgedAggregateManager(repo, aggregate)
        return port(manager, repo), repo

    def test_aggregate_must_be_positive_unique_ints_within_budget_and_subset(self):
        for aggregate in ((5,), (0,), (-1,), (True,), ('1',), (1, 1), (1, 2, 3)):
            with self.subTest(aggregate=aggregate):
                batch, repo = self._batch(aggregate)
                with self.assertRaises(Exception):
                    batch.project_batch(SimpleNamespace(action='write'), completion_budget=2)
                self.assertIsNone(batch._projection_result)

    def test_forged_foreign_ack_is_rejected(self):
        batch, _ = self._batch((1, 99))
        with self.assertRaises(Exception):
            batch.project_batch(SimpleNamespace(action='write'), completion_budget=2)

    def test_success_then_failure_clears_feedback(self):
        values = (job(1, entity=1),)
        repo = Repo(values, {1: document(entity=1)})
        manager = Manager(repo)
        batch = port(manager, repo)
        self.assertEqual((1,), batch.project_batch(
            SimpleNamespace(action='write'), completion_budget=1).acknowledged_job_ids)
        self.assertIsNotNone(batch._projection_result)
        repo.fail_at = 1
        with self.assertRaises(Exception):
            batch.project_batch(SimpleNamespace(action='write'), completion_budget=1)
        self.assertIsNone(batch._projection_result)


class ScriptedWorkerContext:
    def __init__(self, port, budget_log):
        self.port, self.budget_log = port, budget_log

    @contextmanager
    def __call__(self):
        yield self.port, SimpleNamespace(action='write')


class WorkerDispatchTests(unittest.TestCase):
    def _native(self, calls):
        value = object.__new__(_NativeProjectionPort)
        value.guard = SimpleNamespace(timeout_seconds=10)
        value.project_one = lambda route, *, completion_budget=1: calls.append(
            ('single', completion_budget))
        value.project_batch = lambda route, *, completion_budget=1: (
            calls.append(('batch', completion_budget))
            or ProjectionBatchResult(1, 'batch_completed', (1, 2, 3)))
        return value

    def _ordinary(self, calls):
        value = object.__new__(VectorPort)
        value.guard = SimpleNamespace(timeout_seconds=10)
        value.project_one = lambda route, *, completion_budget=1: (
            calls.append(('single', completion_budget)) or object())
        value.project_batch = lambda route, *, completion_budget=1: calls.append(
            ('batch', completion_budget))
        return value

    def test_actual_private_native_port_uses_one_bounded_batch_call(self):
        calls = []
        worker = ProjectionWorker(ScriptedWorkerContext(self._native(calls), calls), enabled=True)
        result = worker._project(4)
        self.assertEqual([('batch', 4)], calls)
        self.assertEqual((1, 2, 3), result.acknowledged_job_ids)

    def test_native_batch_four_ack_is_counted_only_after_factory_cleanup(self):
        calls = []
        class Factory:
            def __init__(self): self.closed = False
            @contextmanager
            def __call__(self):
                try:
                    yield self.port, SimpleNamespace(action='write')
                finally:
                    self.closed = True
        factory = Factory()
        factory.port = self._native(calls)
        worker = ProjectionWorker(factory, enabled=True)
        result = worker._project(4)
        self.assertEqual((1, 2, 3), result.acknowledged_job_ids)
        self.assertTrue(factory.closed)
        self.assertEqual(3, len(worker._confirmed(result, 4)))

    def test_plain_projection_result_three_or_four_is_rejected_by_worker(self):
        for ids in ((1, 2, 3), (1, 2, 3, 4)):
            with self.subTest(ids=ids):
                with self.assertRaises(Exception):
                    ProjectionWorker._confirmed(ProjectionResult(1, 'upsert', ids), 2)
        with self.assertRaises(Exception):
            ProjectionWorker._confirmed(ProjectionResult(1, 'upsert', (1, 2, 3, 4, 5)), 4)
        with self.assertRaises(Exception):
            ProjectionWorker._confirmed(ProjectionBatchResult(1, 'batch_completed', (1, 1, 2)), 4)

    def test_native_serialization_deferred_counts_zero_and_paces_full_interval(self):
        calls = []
        class Clock:
            elapsed = 0.0
        clock = Clock()
        class Stop:
            def __init__(self): self.stopped = False; self.waits = []
            def is_set(self): return self.stopped
            def set(self): self.stopped = True
            def wait(self, seconds):
                self.waits.append(seconds)
                if len(self.waits) >= 2: self.set()
        identities = []
        def make_port():
            value = object.__new__(_NativeProjectionPort)
            value.guard = SimpleNamespace(timeout_seconds=10)
            value.project_batch = lambda route, *, completion_budget=1: (
                (_ for _ in ()).throw(ProjectionBatchSerializationDeferred()))
            return value
        @contextmanager
        def factory():
            identities.append(object())
            yield make_port(), SimpleNamespace(action='write')
        worker = ProjectionWorker(factory, enabled=True, interval_seconds=.5, max_jobs_per_pass=1,
                                  max_runtime_seconds=5)
        worker._stop = Stop()
        with patch('evolvmem.pg_vector_port.worker.time',
                   SimpleNamespace(monotonic=lambda: clock.elapsed)):
            worker._epoch()
        self.assertEqual(0, worker.status()['completed_jobs'])
        self.assertEqual(2, len(identities))
        self.assertIsNot(identities[0], identities[1])
        self.assertEqual([.5, .5], worker._stop.waits)

    def test_ordinary_port_keeps_the_existing_single_job_path_and_min_two(self):
        calls = []
        worker = ProjectionWorker(ScriptedWorkerContext(self._ordinary(calls), calls), enabled=True)
        worker._project(4)
        self.assertEqual([('single', 2)], calls)


class WorkerBaseExceptionTests(unittest.TestCase):
    def test_native_interrupted_batch_stops_worker_without_retry_or_count(self):
        calls = []
        def make_port():
            value = object.__new__(_NativeProjectionPort)
            value.guard = SimpleNamespace(timeout_seconds=10)
            def project_batch(route, *, completion_budget=1):
                calls.append(completion_budget)
                raise KeyboardInterrupt('synthetic interrupt')
            value.project_batch = project_batch
            return value
        @contextmanager
        def factory():
            yield make_port(), SimpleNamespace(action='write')
        worker = ProjectionWorker(factory, enabled=True, interval_seconds=.5)
        worker._run()
        state = worker.status()
        self.assertEqual('failed', state['state'])
        self.assertEqual(0, state['completed_jobs'])
        self.assertEqual(1, len(calls))
        self.assertTrue(worker._stop.is_set())


if __name__ == '__main__':
    unittest.main()

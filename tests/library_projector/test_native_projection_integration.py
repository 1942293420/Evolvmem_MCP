"""Integrated native batch proof through the actual runtime/session state machines.

The content driver, repository SQL boundary and model encoder are doubles, but
``_NativeProjectionPort.project_batch``, ``SessionManager.run``,
``SessionManager._transaction``, the authority scope and the real typed
SerializationFailure classification all execute unmodified.
"""
from contextlib import contextmanager
from types import SimpleNamespace
import unittest

import psycopg.errors
import psycopg.pq

from tests.library_foundation.test_projection_batch_sessions import (
    Authority, BINDING, make_route)
from evolvmem.library_foundation import models
from evolvmem.library_foundation.sessions import (ContentSessionState, DatabaseCredential,
                                                  DatabaseRole, SessionManager)
from evolvmem.pg_vector_port.model import ModelContract, text_hash, vector
from evolvmem.pg_vector_port.projection import Document, Job
from evolvmem.pg_vector_port.runtime import (_NativeProjectionPort,
                                            ProjectionBatchSerializationDeferred)

CONTRACT = ModelContract('model-a', 768, 'doc:', 'query:', 512, 'a' * 64)
TEXT = 'current body'
HASH = text_hash(TEXT)
EMBEDDING = vector((.5,) * 768, CONTRACT)
ROLE = 'runtime_a'


def typed_40001():
    """Exactly the driver shape: redacted FoundationError, immediate context."""
    try:
        raise psycopg.errors.SerializationFailure('private synthetic diagnostic')
    except BaseException:
        try:
            raise models.FoundationError('database_operation_failed') from None
        except models.FoundationError as exc:
            return exc


def job(ident, entity):
    return Job(ident, 'lib', 'context', entity, 'upsert', 2, HASH, CONTRACT.key, 'proj')


def document(entity):
    return Document('lib', 'context', entity, 2, TEXT, 'active', None, 'decision',
                    'ready', ('proj',), False, False, project_id='proj')


class State:
    def __init__(self, candidates, *, fail_ids=(), failure=None, commit_error=None):
        self.candidates = tuple(candidates)
        self.documents = {j.id: document(j.entity_id) for j in candidates}
        self.fail_ids = frozenset(fail_ids)
        self.failure = failure
        self.commit_error = commit_error
        self.committed = []
        self.connections = []
        self.schema_checks = []
        self.encodes = []
        self.applied = []
        self.commit_attempts = 0


class Content:
    """Staged content connection whose commit is the only visibility boundary."""
    def __init__(self, authority, state, *, read_only=False):
        self.authority, self.state = authority, state
        self.role = DatabaseRole(ROLE, ROLE, False, False, False, True)
        self.session_state = ContentSessionState(
            '00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000100',
            7, 11, 'admin', 'pg_active', 'ready')
        self.credential = DatabaseCredential(
            '00000000-0000-0000-0000-000000000020', '00000000-0000-0000-0000-000000000010', 3)
        self.transaction_status = psycopg.pq.TransactionStatus.IDLE
        self.staged = []
        self.events = []
        self.closed = False
        self.close_count = 0
        self.commit_count = 0
        self.rollback_count = 0
        self.in_transaction = False

    def begin(self, *, read_only):
        assert self.authority.held and read_only is False
        self.in_transaction = True
        self.transaction_status = psycopg.pq.TransactionStatus.INTRANS
        self.events.append('begin')

    def inspect_role(self):
        return self.role

    def session_context(self):
        return self.session_state

    def pin_session_context(self, policy_epoch, route_epoch):
        return self.session_state

    def session_credential(self):
        return self.credential

    def execute(self, sql, params=()):
        assert self.in_transaction
        self.events.append((sql, params))
        if sql in ('VECTOR', 'ACK'):
            self.staged.append((sql, params))

    def commit(self):
        assert self.authority.held and self.in_transaction
        self.state.commit_attempts += 1
        self.events.append('commit')
        self.commit_count += 1
        self.state.committed.extend(self.staged)
        self.staged = []
        self.in_transaction = False
        self.transaction_status = psycopg.pq.TransactionStatus.IDLE
        if self.state.commit_error is not None:
            raise self.state.commit_error

    def rollback(self):
        self.rollback_count += 1
        self.events.append('rollback')
        self.staged = []
        self.in_transaction = False
        self.transaction_status = psycopg.pq.TransactionStatus.IDLE

    def _rollback_projection_batch(self):
        self.rollback_count += 1
        self.events.append('batch_rollback')
        self.staged = []
        self.in_transaction = False
        self.transaction_status = psycopg.pq.TransactionStatus.IDLE

    def close(self):
        self.close_count += 1
        self.closed = True
        self.in_transaction = False
        self.events.append('close')


class PrepareConnection(Content):
    def begin(self, *, read_only):
        assert read_only is True
        self.in_transaction = True
        self.transaction_status = psycopg.pq.TransactionStatus.INTRANS
        self.events.append('begin')

    def commit(self):
        # The prepare snapshot has no effect and never raises the RW commit error.
        self.events.append('commit')
        self.commit_count += 1
        self.in_transaction = False
        self.transaction_status = psycopg.pq.TransactionStatus.IDLE


class Repository:
    """Scripted repository boundary using the actual LibrarySession object."""
    def __init__(self, session, state, contract):
        self.session, self.state, self.contract = session, state, contract

    def check_schema(self):
        self.state.schema_checks.append(self.session._connection.in_transaction)
        return True

    def next_jobs(self, limit):
        return self.state.candidates[:limit]

    def document(self, value):
        return self.state.documents.get(value.id)

    def current_job(self, anchor, doc):
        return None

    def apply(self, prepared, now):
        self.state.applied.append(prepared.job.id)
        if self.state.failure is not None:
            raise self.state.failure
        if prepared.job.id in self.state.fail_ids:
            raise typed_40001()
        self.session.execute('VECTOR', (prepared.job.id, prepared.job.revision))
        self.session.execute('ACK', (prepared.job.id,))
        return 'upsert'


class Guard:
    timeout_seconds = 10
    def enforce(self):
        return contextmanager(lambda: (yield))()
    def begin_authority(self):
        pass
    def check(self):
        pass


class Harness:
    def __init__(self, state, *, authority=None):
        self.state = state
        self.authority = authority or Authority()
        self.connections = []
        def open_connection(binding):
            assert binding == BINDING
            connection = (PrepareConnection if not self.connections else Content)(
                self.authority, state)
            self.connections.append(connection)
            return connection
        self.manager = SessionManager(
            SimpleNamespace(revalidate=lambda route: None),
            SimpleNamespace(resolve=lambda route: BINDING),
            SimpleNamespace(open=open_connection), self.authority)
        self.route = make_route()
        self.port = object.__new__(_NativeProjectionPort)
        self.port.guard = Guard()
        self.port.manager = self.manager
        self.port.contract = CONTRACT
        self.port.routes = (self.route,)
        self.port.project_key = 'proj'
        self.port.provider = None
        self.port.cache_lookup = None
        self.port._projection_feedback = None
        self.port._scope = lambda route: route
        self.port.repository = lambda session: Repository(session, state, CONTRACT)
        def encode(text, query):
            state.encodes.append(any(c.in_transaction for c in self.connections))
            return EMBEDDING
        self.port._embedding = encode

    @property
    def prepare(self):
        return self.connections[0]

    @property
    def apply(self):
        return self.connections[1]


class IntegratedRollbackTests(unittest.TestCase):
    def _assert_full_rollback(self, harness, *, applied):
        state = harness.state
        self.assertEqual([], state.committed)
        self.assertEqual([], harness.apply.staged)
        self.assertIs(harness.apply.transaction_status, psycopg.pq.TransactionStatus.IDLE)
        self.assertTrue(harness.apply.closed)
        self.assertEqual(1, harness.apply.close_count)
        self.assertEqual(0, harness.apply.commit_count)
        self.assertEqual(1, harness.apply.rollback_count)
        self.assertEqual([], state.committed)
        self.assertEqual(applied, state.applied)
        self.assertEqual(['enter', 'post_check', 'cleanup'],
                         harness.authority.events[-3:])
        self.assertEqual(2, harness.authority.events.count('enter'))
        self.assertIsNone(harness.port._projection_result)
        self.assertTrue(state.encodes and not any(state.encodes))
        self.assertFalse(any(c.in_transaction for c in harness.connections))
        self.assertTrue(harness.prepare.closed)

    def test_second_entity_typed40001_rolls_back_first_entity_staged_effects(self):
        state = State((job(1, 1), job(2, 2), job(3, 3)), fail_ids=(2,))
        harness = Harness(state)
        with self.assertRaises(ProjectionBatchSerializationDeferred):
            harness.port.project_batch(harness.route, completion_budget=3)
        self._assert_full_rollback(harness, applied=[1, 2])
        self.assertEqual(0, state.commit_attempts)

    def test_third_entity_typed40001_rolls_back_two_prior_entity_effects(self):
        state = State((job(1, 1), job(2, 2), job(3, 3)), fail_ids=(3,))
        harness = Harness(state)
        with self.assertRaises(ProjectionBatchSerializationDeferred):
            harness.port.project_batch(harness.route, completion_budget=3)
        self._assert_full_rollback(harness, applied=[1, 2, 3])
        # The two earlier entities staged real vector/ACK mutations.
        self.assertEqual([], harness.apply.staged)

    def test_commit_40001_is_unknown_fatal_without_retry_or_feedback(self):
        state = State((job(1, 1), job(2, 2)))
        state.commit_error = typed_40001()
        harness = Harness(state)
        with self.assertRaises(models.FoundationError) as raised:
            harness.port.project_batch(harness.route, completion_budget=2)
        self.assertEqual('commit_result_unknown', raised.exception.code)
        self.assertEqual(1, state.commit_attempts)
        self.assertEqual([1, 2], state.applied)
        self.assertIsNone(harness.port._projection_result)
        # Simulated driver persisted before raising; no second effect/retry.
        self.assertTrue(state.committed)
        self.assertEqual(4, len(state.committed))

    def test_claim_skipped_entity_contributes_zero_and_commits_others(self):
        state = State((job(1, 1), job(2, 2)))
        harness = Harness(state)
        original = Repository.apply
        def apply(repository, prepared, now):
            if prepared.job.id == 2:
                return 'already_claimed_or_processed'
            return original(repository, prepared, now)
        Repository.apply = apply
        try:
            result = harness.port.project_batch(harness.route, completion_budget=2)
        finally:
            Repository.apply = original
        self.assertEqual((1,), result.acknowledged_job_ids)
        self.assertEqual(1, harness.apply.commit_count)
        self.assertEqual([('VECTOR', (1, 2)), ('ACK', (1,))], state.committed)


class NonConversionTests(unittest.TestCase):
    """Only the immediate typed SerializationFailure may become a deferral."""
    def _assert_fatal(self, failure):
        state = State((job(1, 1),), failure=failure)
        harness = Harness(state)
        with self.assertRaises(models.FoundationError):
            harness.port.project_batch(harness.route, completion_budget=1)
        self.assertEqual(0, harness.apply.commit_count)
        self.assertEqual([], state.committed)
        self.assertTrue(harness.apply.closed)
        self.assertIsNone(harness.port._projection_result)

    def test_non_40001_driver_error_is_not_converted(self):
        try:
            raise psycopg.errors.OperationalError('private synthetic diagnostic')
        except BaseException:
            try:
                raise models.FoundationError('database_operation_failed') from None
            except models.FoundationError as exc:
                self._assert_fatal(exc)

    def test_sqlstate_only_forgery_is_not_converted(self):
        class FakeSerialization(Exception):
            sqlstate = '40001'
        try:
            raise FakeSerialization('private synthetic diagnostic')
        except BaseException:
            try:
                raise models.FoundationError('database_operation_failed') from None
            except models.FoundationError as exc:
                self._assert_fatal(exc)

    def test_nested_cause_40001_is_not_converted(self):
        try:
            try:
                raise psycopg.errors.SerializationFailure('private synthetic diagnostic')
            except BaseException:
                raise models.FoundationError('database_operation_failed') from None
        except models.FoundationError as inner:
            try:
                raise models.FoundationError('database_operation_failed') from inner
            except models.FoundationError as outer:
                self._assert_fatal(outer)


if __name__ == '__main__':
    unittest.main()

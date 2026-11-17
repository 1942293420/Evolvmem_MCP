"""Native projection activation+preparation merges one fresh read-only session.

Only the transport (authority lease, connection/provider, router catalog) is
synthetic. The production SessionManager transaction, private preparation
marker, private native port and project_one preparation/apply ordering run here.
No database, model, service or network is used.
"""
from contextlib import contextmanager
import dataclasses
from types import SimpleNamespace
import unittest

from tests.library_foundation import test_foundation as base
from tests.library_foundation.test_foundation import A, B, LIBRARY, CREDENTIAL, DEVICE, PROJECT
from evolvmem.library_catalog.adapter import ControlAuthorityLease
from evolvmem.library_foundation import models
from evolvmem.library_foundation._projection_conflict import (ConfirmedPreEffectRollback,
    ProjectionConflictDeferred, _ProjectionApplyOperation)
from evolvmem.library_foundation.sessions import SessionManager, _ProjectionPrepareOperation
from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from evolvmem.pg_vector_port.history_repository import HistoryVectorRepository
from evolvmem.pg_vector_port.model import ModelContract, VectorError, text_hash
from evolvmem.pg_vector_port.projection import Document, Job
from evolvmem.pg_vector_port.runtime import _NativeProjectionPort, VectorPort

TEXT = 'current body'
REVISION = 'a' * 64
CONTRACT = ModelContract('model-a', 768, 'doc:', 'query:', 512, REVISION)


class Authority(ControlAuthorityLease):
    """ControlAuthorityLease double: counts fresh holds and post-yield cleanup."""
    def __init__(self):
        self.deadline_guard = SyncRequestDeadlineGuard(30)
        self.holds = 0
        self.held = False
        self.hold_many_calls = 0

    @contextmanager
    def hold(self, route):
        if self.held:
            raise AssertionError('shared/reentrant request lease')
        self.held = True
        self.holds += 1
        try:
            yield
        finally:
            self.held = False

    @contextmanager
    def hold_many(self, routes):
        if self.held:
            raise AssertionError('shared/reentrant request lease')
        self.held = True
        self.hold_many_calls += 1
        try:
            yield
        finally:
            self.held = False


class Connection:
    def __init__(self, api, authority):
        self.authority = authority
        self.role = api.DatabaseRole('runtime_a', 'runtime_a', False, False, False, True)
        self.state = api.ContentSessionState(A, LIBRARY, 7, 11, 'admin', 'pg_active', 'ready')
        self.credential = api.DatabaseCredential(CREDENTIAL, DEVICE, 3)
        self.begin_modes = []
        self.session_contexts = 0
        self.pins = 0
        self.events = []
        self.commits = 0
        self.rollbacks = 0
        self.closes = 0

    def begin(self, *, read_only):
        assert self.authority.held
        self.begin_modes.append(read_only)
        self.events.append(('begin', read_only))

    def inspect_role(self):
        return self.role

    def session_context(self):
        self.session_contexts += 1
        self.events.append('session_context')
        return self.state

    def pin_session_context(self, policy_epoch, route_epoch):
        assert self.authority.held and self.begin_modes[-1] is False
        self.pins += 1
        self.events.append('pin_session_context')
        return self.state

    def session_credential(self):
        return self.credential

    def execute(self, sql, params=()):
        self.events.append(('sql', sql))
        return ()

    def commit(self):
        assert self.authority.held
        self.commits += 1
        self.events.append('commit')

    def rollback(self):
        self.rollbacks += 1
        self.events.append('rollback')

    def close(self):
        self.closes += 1
        self.events.append('close')


class Provider:
    def __init__(self, api, authority):
        self.api = api
        self.authority = authority
        self.deadline_guard = authority.deadline_guard
        self.open_count = 0
        self.connections = []
        self.configure = None

    def open(self, binding):
        self.open_count += 1
        connection = Connection(self.api, self.authority)
        if self.configure is not None:
            self.configure(connection)
        self.connections.append(connection)
        return connection


class TrackingHistoryRepository(HistoryVectorRepository):
    """Historical type witness that must never receive an apply call."""
    def __init__(self):
        self.apply_calls = 0

    def apply(self, prepared, now):
        self.apply_calls += 1
        return 'upsert'


class ScriptedRepository:
    """Records the exact prepare ordering; transport SQL is not involved."""
    def __init__(self, events, *, job=None, document=None, schema_error=None):
        self.events = events
        self.job = job
        self.document_value = document
        self.schema_error = schema_error
        self.applied = []
        self.schema_calls = 0
        self.next_calls = 0

    def check_schema(self):
        self.schema_calls += 1
        self.events.append('check_schema')
        if self.schema_error is not None:
            raise models.FoundationError(self.schema_error)
        return True

    def next_job(self):
        self.next_calls += 1
        self.events.append('next_job')
        return self.job

    def document(self, job, *, lock=False):
        self.events.append('document')
        return self.document_value

    def apply(self, prepared, now):
        self.events.append('apply')
        self.applied.append(prepared)
        return 'upsert'


def native_job():
    return Job(1, LIBRARY, 'context', 9, 'upsert', 1, text_hash(TEXT), CONTRACT.key, PROJECT)


def native_document():
    return Document(LIBRARY, 'context', 9, 1, TEXT, 'active', None, 'decision', 'none',
                    (), False, False, 0, PROJECT)


class NativePrepareTests(base.FoundationTests):
    def setUp(self):
        super().setUp()
        self.catalog.permission = 'admin'
        binding = self.api.ConnectionBinding('pg.synthetic', LIBRARY, A, CREDENTIAL, DEVICE,
                                             '127.0.0.1', 55439, 'synthetic_a', 'runtime_a',
                                             'secrets.synthetic.a')
        self.registry = self.api.ConnectionRegistry([binding])
        self.authority = Authority()
        self.provider = Provider(self.api, self.authority)
        self.manager = self.api.SessionManager(self.router, self.registry, self.provider,
                                               self.authority)
        self.read_route = self.plan()
        self.write_route = self.plan('memory_add')
        self.embedding_provider = SimpleNamespace(
            is_loaded=True, model_revision=REVISION,
            config=SimpleNamespace(embedding_model_filename='model-a', embedding_dim=768,
                                   embedding_doc_prefix='doc:', embedding_query_prefix='query:'),
            encode_document=lambda text, deadline=None: (.5,) * 768)
        self.events = []
        self.encoded = []

    def port(self, repository, *, project_key='alpha'):
        port = _NativeProjectionPort(self.manager, (self.read_route,), CONTRACT,
                                     project_key=project_key,
                                     embedding_provider=self.embedding_provider)
        select = repository if callable(repository) else (lambda session: repository)
        port.repository = select
        port._embedding = lambda text, *, query: (self.encoded.append(text), 'embedding')[1]
        return port

    def productive_repository(self):
        repo = ScriptedRepository(self.events, job=native_job())
        repo.document_value = native_document()
        return repo

    def test_private_construction_does_not_activate_and_selection_plus_prepare_plus_apply_opens_three(self):
        # Selection is the host's existing authorized read, then the private port
        # is constructed for the selected canonical project, then one job runs.
        selection = self.manager.run(self.read_route, lambda session: (('row',),))
        self.assertEqual((('row',),), selection)
        repo = self.productive_repository()
        port = self.port(repo)
        self.assertEqual(1, self.provider.open_count)
        self.assertEqual(1, self.authority.holds)
        self.assertTrue(self.provider.connections[-1].begin_modes == [True])
        result = port.project_one(self.write_route, completion_budget=1)
        self.assertEqual(result.acknowledged_job_ids, (1,))
        # Exactly three content sessions: selection, merged read-only prepare,
        # unchanged write apply. No constructor activation session was added.
        self.assertEqual(3, self.provider.open_count)
        self.assertEqual(3, self.authority.holds)
        self.assertEqual([c.begin_modes for c in self.provider.connections],
                         [[True], [True], [False]])
        prepare, apply = self.provider.connections[1], self.provider.connections[2]
        self.assertEqual(1, prepare.session_contexts)
        self.assertEqual(0, prepare.pins)
        self.assertEqual(0, apply.session_contexts)
        self.assertEqual(1, apply.pins)
        self.assertEqual(1, prepare.commits)
        self.assertEqual(1, apply.commits)
        self.assertEqual([1, 1, 1], [c.closes for c in self.provider.connections])
        self.assertEqual(self.events, ['check_schema', 'next_job', 'document', 'apply'])
        self.assertEqual(self.encoded, [TEXT])

    def test_prepare_is_write_authorized_even_though_transaction_is_read_only(self):
        self.provider.configure = lambda connection: setattr(
            connection, 'state', dataclasses.replace(connection.state, permission='read'))
        repo = self.productive_repository()
        port = self.port(repo)
        with self.assertRaises(models.FoundationError) as raised:
            port.project_one(self.write_route)
        self.assertEqual('database_authorization_mismatch', raised.exception.code)
        self.assertEqual([], self.events)
        self.assertEqual([], self.encoded)
        self.assertEqual(1, self.provider.open_count)
        self.assertEqual([True], self.provider.connections[0].begin_modes)
        self.assertEqual(0, self.provider.connections[0].commits)
        self.assertTrue(self.authority.held is False)

    def test_role_credential_and_project_permission_mismatch_fail_before_repository(self):
        changes = (
            (lambda c: setattr(c, 'role', dataclasses.replace(
                c.role, superuser=True)), 'unsafe_database_role'),
            (lambda c: setattr(c, 'credential', dataclasses.replace(
                c.credential, credential_epoch=4)), 'database_credential_mismatch'),
            (lambda c: setattr(c, 'state', dataclasses.replace(
                c.state, route_epoch=10)), 'database_authorization_mismatch'),
        )
        for configure, code in changes:
            with self.subTest(code=code):
                self.setUp()
                self.provider.configure = configure
                repo = self.productive_repository()
                port = self.port(repo)
                with self.assertRaises(models.FoundationError) as raised:
                    port.project_one(self.write_route)
                self.assertEqual(code, raised.exception.code)
                self.assertEqual([], self.events)
                self.assertEqual([], self.encoded)
                self.assertEqual(1, self.provider.open_count)

    def test_check_schema_precedes_next_job_and_empty_queue_still_checks_model(self):
        repo = ScriptedRepository(self.events)
        port = self.port(repo)
        result = port.project_one(self.write_route)
        self.assertEqual(result.outcome, 'no_pending_for_model')
        self.assertEqual(result.acknowledged_job_ids, ())
        self.assertEqual(self.events, ['check_schema', 'next_job'])
        self.assertEqual(1, repo.schema_calls)
        self.assertEqual(1, repo.next_calls)
        self.assertEqual([], self.encoded)
        # Only the merged prepare opened; there is no apply session for no work.
        self.assertEqual(1, self.provider.open_count)

    def test_invalid_model_rejects_before_job_document_or_encoder(self):
        repo = ScriptedRepository(self.events,
                                  schema_error='registered_project_vector_model_required')
        port = self.port(repo)
        with self.assertRaises(models.FoundationError) as raised:
            port.project_one(self.write_route)
        self.assertEqual('registered_project_vector_model_required', raised.exception.code)
        self.assertEqual(['check_schema'], self.events)
        self.assertEqual(0, repo.next_calls)
        self.assertEqual([], self.encoded)
        self.assertEqual(1, self.provider.open_count)

    def test_projection_only_port_rejects_nonprojection_apis_before_transport_or_model(self):
        repo = self.productive_repository()
        port = self.port(repo)
        calls = (
            lambda: port.search('q', index_kind='context', top_k=1, candidate_limit=1),
            lambda: port.search_embedding('embedding', index_kind='context', top_k=1,
                                          candidate_limit=1),
            lambda: port.enqueue_current_page(self.write_route, index_kind='context'),
            lambda: port.business_executor(SimpleNamespace(), semantic_merge_enabled=False),
            lambda: port.drain(self.write_route, max_jobs=2),
        )
        for call in calls:
            with self.subTest(call=call):
                with self.assertRaises(models.FoundationError) as raised:
                    call()
                self.assertEqual('native_projection_only_forbidden', raised.exception.code)
        self.assertEqual(0, self.provider.open_count)
        self.assertEqual(0, self.authority.holds)
        self.assertEqual([], self.events)
        self.assertEqual([], self.encoded)

    def test_historical_repository_at_prepare_is_fatal_before_history_or_native_work(self):
        history = TrackingHistoryRepository()
        port = self.port(lambda session: history)
        with self.assertRaises(models.FoundationError) as raised:
            port.project_one(self.write_route)
        self.assertEqual('native_projection_history_drift', raised.exception.code)
        self.assertEqual([], self.events)
        self.assertEqual([], self.encoded)
        self.assertEqual(0, history.apply_calls)
        self.assertEqual(1, self.provider.open_count)
        self.assertEqual(0, self.provider.connections[0].commits)
        self.assertTrue(self.authority.held is False)

    def test_historical_repository_drift_between_prepare_and_apply_is_fatal_with_no_effect(self):
        repo = self.productive_repository()
        history = TrackingHistoryRepository()
        seen = []
        def select(session):
            seen.append(len(seen) + 1)
            return repo if len(seen) == 1 else history
        port = self.port(select)
        with self.assertRaises(models.FoundationError) as raised:
            port.project_one(self.write_route)
        self.assertEqual('native_projection_history_drift', raised.exception.code)
        self.assertEqual([1, 2], seen)
        self.assertEqual(repo.applied, [])
        self.assertEqual(0, history.apply_calls)
        self.assertEqual(self.events, ['check_schema', 'next_job', 'document'])
        self.assertEqual(self.encoded, [TEXT])
        # Apply opened its fresh write session but failed before any claim/ACK.
        self.assertEqual(2, self.provider.open_count)
        apply = self.provider.connections[1]
        self.assertEqual([('begin', False), 'pin_session_context', 'rollback', 'close'],
                         apply.events)
        self.assertEqual(0, apply.commits)

    def test_each_project_one_owns_fresh_sessions_and_no_readiness_state(self):
        repo = self.productive_repository()
        port = self.port(repo)
        first = port.project_one(self.write_route)
        second = port.project_one(self.write_route)
        self.assertEqual((1,), first.acknowledged_job_ids)
        self.assertEqual((1,), second.acknowledged_job_ids)
        self.assertEqual(4, self.provider.open_count)
        self.assertEqual(4, self.authority.holds)
        self.assertEqual(len({id(c) for c in self.provider.connections}), 4)
        self.assertEqual([c.begin_modes for c in self.provider.connections],
                         [[True], [False], [True], [False]])

    def test_private_construction_rejects_foreign_or_incomplete_bindings(self):
        cases = (
            (lambda: _NativeProjectionPort(object(), (self.read_route,), CONTRACT,
                                           project_key='alpha'), 'actual_vector_manager_required'),
            (lambda: _NativeProjectionPort(self.manager, (self.write_route,), CONTRACT,
                                           project_key='alpha'), 'invalid_read_routes'),
            (lambda: _NativeProjectionPort(self.manager, (self.read_route,), CONTRACT,
                                           project_key=None), 'native_projection_project_required'),
            (lambda: _NativeProjectionPort(self.manager, (self.read_route,), CONTRACT,
                                           project_key=' alpha '), 'invalid_project_key'),
        )
        for make, code in cases:
            with self.subTest(code=code):
                with self.assertRaises((models.FoundationError, VectorError)) as raised:
                    make()
                self.assertEqual(code, raised.exception.code)
        self.assertEqual(0, self.provider.open_count)
        other = dataclasses.replace(
            self.read_route, library=dataclasses.replace(self.read_route.library, library_id=B))
        with self.assertRaises(VectorError) as raised:
            _NativeProjectionPort(self.manager, (self.read_route, other), CONTRACT,
                                  project_key='alpha')
        self.assertEqual('native_projection_single_library_required', raised.exception.code)
        foreign_guard = SyncRequestDeadlineGuard(30)
        self.provider.deadline_guard = foreign_guard
        with self.assertRaises(VectorError) as raised:
            _NativeProjectionPort(self.manager, (self.read_route,), CONTRACT,
                                  project_key='alpha')
        self.assertEqual('actual_vector_guard_required', raised.exception.code)
        self.provider.deadline_guard = self.authority.deadline_guard

    def test_private_host_factory_returns_the_projection_only_object_without_activation(self):
        port = VectorPort._native_projection_factory(
            self.manager, (self.read_route,), CONTRACT, project_key='alpha',
            embedding_provider=self.embedding_provider)
        self.assertIsInstance(port, _NativeProjectionPort)
        self.assertTrue(port._projection_only)
        self.assertEqual(0, self.provider.open_count)
        self.assertEqual(0, self.authority.holds)

    def test_ordinary_constructor_keeps_eager_activation(self):
        activation = []
        class ProbePort(VectorPort):
            def repository(self, session):
                activation.append(session.route.library.library_id)
                return SimpleNamespace(check_schema=lambda: True)
        port = ProbePort(self.manager, (self.read_route,), CONTRACT, project_key='alpha')
        self.assertIsInstance(port, VectorPort)
        self.assertNotIsInstance(port, _NativeProjectionPort)
        self.assertEqual([LIBRARY], activation)
        self.assertEqual(1, self.provider.open_count)
        self.assertEqual(1, self.authority.hold_many_calls)
        self.assertEqual([True], self.provider.connections[0].begin_modes)


class PrivatePreparationMarkerTests(base.FoundationTests):
    def setUp(self):
        super().setUp()
        self.catalog.permission = 'admin'
        binding = self.api.ConnectionBinding('pg.synthetic', LIBRARY, A, CREDENTIAL, DEVICE,
                                             '127.0.0.1', 55439, 'synthetic_a', 'runtime_a',
                                             'secrets.synthetic.a')
        self.registry = self.api.ConnectionRegistry([binding])
        self.authority = Authority()
        self.provider = Provider(self.api, self.authority)
        self.manager = self.api.SessionManager(self.router, self.registry, self.provider,
                                               self.authority)
        self.read_route = self.plan()
        self.write_route = self.plan('memory_add')

    def test_prepare_marker_is_read_only_session_context_on_the_original_write_route(self):
        marker = self.manager._projection_prepare(lambda session: ('prepared',))
        self.assertIs(type(marker), _ProjectionPrepareOperation)
        result = self.manager.run(self.write_route, marker)
        self.assertEqual(('prepared',), result)
        connection = self.provider.connections[0]
        self.assertEqual([True], connection.begin_modes)
        self.assertEqual(1, connection.session_contexts)
        self.assertEqual(0, connection.pins)
        self.assertEqual(1, connection.commits)
        self.assertEqual(1, connection.closes)
        self.assertEqual(1, self.provider.open_count)
        self.assertEqual(1, self.authority.holds)
        self.assertFalse(self.authority.held)

    def test_prepare_marker_requires_write_or_admin_and_no_publisher(self):
        with self.assertRaises(ValueError):
            self.manager.run(self.read_route, self.manager._projection_prepare(lambda s: None))
        with self.assertRaises(ValueError):
            self.manager.run(self.write_route, self.manager._projection_prepare(lambda s: None),
                             publish=lambda result: None)
        self.assertEqual(0, self.provider.open_count)

    def test_prepare_marker_failure_never_enables_apply_conflict_deferral(self):
        def fail(session):
            raise ConfirmedPreEffectRollback()
        with self.assertRaises(models.FoundationError) as raised:
            self.manager.run(self.write_route, self.manager._projection_prepare(fail))
        self.assertEqual('database_operation_failed', raised.exception.code)
        self.assertNotIsInstance(raised.exception, ProjectionConflictDeferred)
        connection = self.provider.connections[0]
        self.assertEqual([True], connection.begin_modes)
        self.assertEqual(1, connection.rollbacks)
        self.assertEqual(1, connection.closes)
        self.assertEqual(0, connection.commits)

    def test_prepare_marker_checks_role_credential_and_write_projection_state_before_callback(self):
        cases = (
            (lambda c: setattr(c, 'role', dataclasses.replace(c.role, bypass_rls=True)),
             'unsafe_database_role'),
            (lambda c: setattr(c, 'credential', dataclasses.replace(c.credential, device_id=B)),
             'database_credential_mismatch'),
            (lambda c: setattr(c, 'state', dataclasses.replace(c.state, permission='read')),
             'database_authorization_mismatch'),
        )
        for configure, code in cases:
            with self.subTest(code=code):
                self.setUp()
                self.provider.configure = configure
                called = []
                with self.assertRaises(models.FoundationError) as raised:
                    self.manager.run(self.write_route,
                                     self.manager._projection_prepare(lambda s: called.append(True)))
                self.assertEqual(code, raised.exception.code)
                self.assertEqual([], called)
                connection = self.provider.connections[0]
                self.assertEqual([True], connection.begin_modes)
                self.assertEqual(0, connection.commits)
                self.assertEqual(1, connection.closes)

    def test_apply_marker_still_defers_only_after_the_existing_rollback_and_close(self):
        def fail(session):
            raise ConfirmedPreEffectRollback()
        with self.assertRaises(ProjectionConflictDeferred):
            self.manager.run(self.write_route, _ProjectionApplyOperation(fail))
        connection = self.provider.connections[0]
        self.assertEqual([False], connection.begin_modes)
        self.assertEqual(1, connection.rollbacks)
        self.assertEqual(1, connection.closes)


if __name__ == '__main__':
    unittest.main()

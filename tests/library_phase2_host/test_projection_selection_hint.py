"""Behavioral scheduling-hint checks through the real host native projection port.

Only the PostgreSQL session/manager/catalog and repository I/O boundary is
faked; LibraryHost.start_projection, the actual private _NativeProjectionPort,
request scopes, guards and the worker factory are exercised.  No DB, model or
service is started.
"""
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from tests.library_phase2_host import test_host_startup as support

from evolvmem.library_foundation import FoundationError, RequestIdentity
from evolvmem.library_foundation.models import AuthorizedRoute, LibraryDescriptor
from evolvmem.library_project_acl.coordinator import project_uuid
from evolvmem.pg_vector_port import repository as repository_module
from evolvmem.pg_vector_port import runtime as runtime_module
from evolvmem.pg_vector_port import worker as worker_module
from evolvmem.pg_vector_port.model import ModelContract
from evolvmem.pg_vector_port.runtime import ProjectionDeferred, ProjectionResult
import evolvmem.library_host as module

LIB, LIB2 = support.uid(31), support.uid(35)
MODE = "SELECT to_regclass('em_security.history_readiness') IS NOT NULL"
LISTING = ('SELECT p.project_id::text,p.project_key,COALESCE(w.has_pending,false) '
           'FROM em_security.list_recall_projects(')


def row(key, pending=True):
    return (project_uuid(key), key, pending)


class _AuthorityLease:
    def __init__(self, catalog, *, deadline_guard):
        self.deadline_guard = deadline_guard


class _Job:
    def __init__(self, ident, library_id, key):
        self.id = ident
        self.library_id = library_id
        self.index_kind = 'knowledge'
        self.entity_id = project_uuid(key)
        self.revision = 1
        self.action = 'upsert'


class Harness:
    def __init__(self, libraries, keys, remaining, *, history=False, ordinary_port=False):
        self.libraries = tuple(libraries)
        self.keys = {library_id: tuple(keys.get(library_id, ())) for library_id in libraries}
        self.remaining = dict(remaining)
        self.history = history
        self.ordinary_port = ordinary_port
        self.discoveries = 0
        self.selections = []
        self.plans = []
        self.auths = []
        self.tokens = []
        self.states = []
        self.guards = []
        self.active_at_port = []
        self.prepares = 0
        self.applies = 0
        self.applied_keys = []
        self.results = []
        self.repository_modes = []
        self.prepare_errors = {}
        self.apply_error = None
        self.apply_outcome = 'remove'
        self.poison_guard = False
        self.token_error = None
        self.plan_errors = {}
        self.ports = []
        self.job_ids = 0
        contract = ModelContract('trusted-model', 768, '', '', 512)
        self.contract = contract
        provider = SimpleNamespace(contract=contract,
            encode_query=lambda *a, **k: None,
            encode_document=lambda *a, **k: (0.0,) * 768,
            close=lambda: None)
        self.host = support.host(recall_enabled=True, embedding_provider=provider,
                                 model_contract=contract.key)
        identity = RequestIdentity(support.uid(32), support.uid(33), support.uid(34), 1)
        self.identity = identity
        self.descriptors = {}
        for number, library_id in enumerate(libraries, start=1):
            self.descriptors[library_id] = LibraryDescriptor(
                library_id=library_id, kind='project', backend='postgres',
                connection_ref='one', policy_epoch=1, route_epoch=1,
                authority_state='pg_active', acl_state='ready',
                writer_backend='postgres', project_id=support.uid(100 + number),
                active=True)

        harness = self

        class Router:
            def __init__(self, *a, **k):
                pass
            def plan(self, identity, tool, library_id, **kwargs):
                harness.plans.append((tool, library_id))
                error = harness.plan_errors.get((library_id, tool))
                if error is not None:
                    raise FoundationError(error)
                action = 'read' if tool == 'context_read' else 'write'
                return AuthorizedRoute(identity, harness.descriptors[library_id], tool, action)

        class Session:
            def __init__(self, mode):
                self.mode = mode
            def execute(self, sql, params=()):
                if sql == MODE:
                    return ((harness.history,),)
                assert sql.startswith(LISTING), sql
                assert params[1] == 'write' and params[0] == params[3]
                assert params[4] == harness.contract.key and len(params) == 5
                library_id, after = params[0], params[2]
                harness.selections.append((library_id, after))
                rows = tuple(row(key) for key in harness.keys[library_id]
                             if harness.remaining.get((library_id, key), 0) > 0
                             and (after is None or key > after))
                return rows

        class Manager:
            def __init__(self, router, registry, provider, authority):
                self.router = router
                self.authority = authority
                self.provider = provider
            def _projection_prepare(self, operation):
                return ('prepare', operation)
            def _projection_apply(self, operation):
                return ('apply', operation)
            def run(self, route, operation, *, publish=None):
                mode = 'discovery'
                if isinstance(operation, tuple):
                    mode, operation = operation
                if mode == 'discovery':
                    harness.discoveries += 1
                elif mode == 'prepare':
                    harness.prepares += 1
                if mode in ('prepare', 'apply'):
                    harness.active_at_port.append(harness.host.active_requests)
                return operation(Session(mode))
            def run_many(self, routes, operation, *, finalize=None, combine=None, publish=None):
                values = tuple(operation(Session('activation')) for _ in routes)
                return combine(values) if combine is not None else values

        class Repository:
            def __init__(self, session, contract, scope):
                self.mode = session.mode
                self.key = scope.project_key
                harness.repository_modes.append((session.mode, self.key))
            def check_schema(self):
                error = harness.prepare_errors.get(self.key)
                if error is not None:
                    raise FoundationError(error)
            def next_job(self):
                if harness.remaining.get((self.library_id, self.key), 0) <= 0:
                    return None
                harness.job_ids += 1
                return _Job(harness.job_ids, self.library_id, self.key)
            def document(self, job):
                return SimpleNamespace(text='document-%s' % job.id, revision=job.revision)
            def cache_entry(self, job):
                return None
            def apply(self, prepared, now):
                if harness.apply_error is not None:
                    raise harness.apply_error
                if harness.poison_guard:
                    harness.host._state().guard._unhealthy.set()
                harness.applies += 1
                harness.applied_keys.append(self.key)
                left = harness.remaining.get((self.library_id, self.key), 0)
                if left:
                    harness.remaining[(self.library_id, self.key)] = left - 1
                return harness.apply_outcome

        def resolve(session, key):
            return SimpleNamespace(project_key=key)

        def decide(job, document, contract, now):
            return 'remove'

        def auth(token):
            state = harness.host._state()
            harness.auths.append(state)
            harness.states.append(state)
            harness.guards.append(state.guard)
            state.identity = identity
            return identity

        # Install the per-library descriptor on the fake repository so apply
        # knows which (library, key) it served.
        class BoundRepository(Repository):
            def __init__(self, session, contract, scope):
                super().__init__(session, contract, scope)
                self.library_id = session.library_id

        class BoundSession(Session):
            def __init__(self, mode, library_id):
                super().__init__(mode)
                self.library_id = library_id

        # Manager.run knows the route; bind it into the session for repository use.
        original_run = Manager.run
        def run(self, route, operation, *, publish=None):
            mode = 'discovery'
            if isinstance(operation, tuple):
                mode, operation = operation
            if mode == 'discovery':
                harness.discoveries += 1
            elif mode == 'prepare':
                harness.prepares += 1
            if mode in ('prepare', 'apply'):
                harness.active_at_port.append(harness.host.active_requests)
            session = BoundSession(mode, route.library.library_id)
            return operation(session)
        Manager.run = run

        self.host.authenticate = auth

        class Service(worker_module.ProjectionService):
            def start(self):
                return self.status()

        class OrdinaryPort:
            def __init__(self, manager, routes, contract, *, project_key=None,
                         embedding_provider=None, cache_lookup=None):
                state = harness.host._state()
                self.guard = state.guard
                self.key = project_key
                harness.ports.append(self)
            def project_one(self, write_route, *, completion_budget=1):
                return ProjectionResult(1, 'remove', (1,))

        self._patches = [
            patch.object(worker_module, 'ProjectionService', Service),
        ]
        if ordinary_port:
            self._patches.extend([
                patch.object(worker_module, 'VectorPort', OrdinaryPort),
                patch.object(runtime_module, 'VectorPort', OrdinaryPort),
            ])
        else:
            original_native_factory = runtime_module.VectorPort._native_projection_factory
            def capture_native_factory(manager, routes, contract, **kwargs):
                port = original_native_factory(manager, routes, contract, **kwargs)
                harness.ports.append(port)
                return port
            self._patches.append(
                patch.object(runtime_module.VectorPort, '_native_projection_factory',
                             staticmethod(capture_native_factory)))
        self._patches.extend([
            patch.object(module, 'AuthorityRouter', Router),
            patch.object(module, 'SessionManager', Manager),
            patch.object(module, 'ControlAuthorityLease', _AuthorityLease),
            patch.object(runtime_module, 'SessionManager', Manager),
            patch.object(runtime_module, 'ControlAuthorityLease', _AuthorityLease),
            patch.object(module, 'ParallelReadCoordinator', lambda *a, **k: None),
            patch.object(runtime_module, 'VectorRepository', BoundRepository),
            patch.object(runtime_module, 'resolve_project_scope', resolve),
            patch.object(runtime_module, 'decide', decide),
            patch.object(repository_module, 'VectorRepository', BoundRepository),
        ])
        for item in self._patches:
            item.start()
        self.host.start_projection(token_loader=self.token,
                                   library_ids=self.libraries)

    def token(self):
        self.tokens.append('synthetic')
        if self.token_error is not None:
            raise self.token_error
        return 'synthetic'

    def visit(self):
        try:
            result = self.host._projection_worker._project()
        except BaseException as exc:
            self.results.append(exc)
            raise
        self.results.append(result)
        return result

    def close(self):
        try:
            self.host.close()
        finally:
            for item in reversed(self._patches):
                item.stop()

    def pair_read_plans(self):
        return len([event for event in self.plans if event[0] == 'context_read']) - self.discoveries

    def write_plans(self):
        return len([event for event in self.plans if event[0] == 'memory_add'])

    def keys_applied(self):
        return list(self.applied_keys)


class SelectionHintChecks(unittest.TestCase):
    def test_four_productive_native_visits_discover_once(self):
        env = Harness((LIB,), {LIB: ('alpha',)}, {(LIB, 'alpha'): 4})
        try:
            for _ in range(4):
                env.visit()
        finally:
            env.close()
        self.assertEqual(env.discoveries, 1)
        self.assertEqual(len(env.selections), 1)
        self.assertEqual(env.keys_applied(), ['alpha'] * 4)
        self.assertEqual(len(env.auths), 4)
        self.assertEqual(len(env.tokens), 4)
        self.assertEqual(len({id(state) for state in env.states}), 4)
        self.assertEqual(len({id(guard) for guard in env.guards}), 4)
        self.assertEqual(env.active_at_port, [1] * 8)
        self.assertEqual(env.prepares, 4)
        self.assertEqual(env.applies, 4)
        self.assertEqual(env.pair_read_plans(), 4)
        self.assertEqual(env.write_plans(), 4)
        self.assertTrue(all(result.acknowledged_job_count == 1 for result in env.results))
        self.assertEqual(env.host.active_requests, 0)

    def test_fifth_visit_rediscovers_after_served_key(self):
        env = Harness((LIB,), {LIB: ('alpha', 'beta')},
                      {(LIB, 'alpha'): 4, (LIB, 'beta'): 1})
        try:
            for _ in range(5):
                env.visit()
        finally:
            env.close()
        self.assertEqual(env.keys_applied(), ['alpha'] * 4 + ['beta'])
        self.assertEqual(env.discoveries, 2)
        self.assertEqual(env.selections[0], (LIB, None))
        self.assertEqual(env.selections[1], (LIB, 'alpha'))

    def test_two_libraries_hot_projects_progress_within_burst(self):
        env = Harness((LIB, LIB2), {LIB: ('alpha',), LIB2: ('beta',)},
                      {(LIB, 'alpha'): 4, (LIB2, 'beta'): 4})
        try:
            for _ in range(8):
                env.visit()
        finally:
            env.close()
        self.assertEqual(env.keys_applied(), ['alpha'] * 4 + ['beta'] * 4)
        self.assertEqual(env.discoveries, 2)
        self.assertEqual(len(env.auths), 8)
        self.assertEqual(env.pair_read_plans(), 8)
        self.assertEqual(env.write_plans(), 8)

    def test_new_pending_key_does_not_extend_the_burst(self):
        env = Harness((LIB,), {LIB: ('alpha', 'beta')},
                      {(LIB, 'alpha'): 4, (LIB, 'beta'): 1})
        try:
            for _ in range(5):
                env.visit()
        finally:
            env.close()
        self.assertEqual(env.keys_applied(), ['alpha'] * 4 + ['beta'])
        self.assertEqual(env.discoveries, 2)
        self.assertEqual(env.selections, [(LIB, None), (LIB, 'alpha')])

    def test_zero_ack_no_pending_clears_hint_and_rotates(self):
        env = Harness((LIB, LIB2), {LIB: ('alpha',), LIB2: ('beta',)},
                      {(LIB, 'alpha'): 1, (LIB2, 'beta'): 1})
        try:
            self.assertEqual(env.visit().acknowledged_job_count, 1)
            idle = env.visit()
            self.assertIsNone(idle.job_id)
            self.assertEqual(idle.acknowledged_job_count, 0)
            env.visit()
        finally:
            env.close()
        self.assertEqual(env.keys_applied(), ['alpha', 'beta'])
        self.assertEqual(env.discoveries, 2)
        self.assertEqual(env.prepares, 3)

    def test_zero_ack_already_claimed_clears_hint_and_rotates(self):
        env = Harness((LIB, LIB2), {LIB: ('alpha',), LIB2: ('beta',)},
                      {(LIB, 'alpha'): 4, (LIB2, 'beta'): 1})
        try:
            env.visit()
            env.apply_outcome = 'already_claimed_or_processed'
            env.visit()
            env.visit()
        finally:
            env.close()
        self.assertEqual(env.keys_applied(), ['alpha', 'alpha', 'beta'])
        self.assertEqual(env.discoveries, 2)

    def test_unused_port_advances_to_next_library(self):
        env = Harness((LIB, LIB2), {LIB: ('alpha',), LIB2: ('beta',)},
                      {(LIB, 'alpha'): 0, (LIB2, 'beta'): 1})
        try:
            self.assertIsNone(env.visit())
            env.visit()
        finally:
            env.close()
        self.assertEqual(env.discoveries, 2)
        self.assertEqual(len(env.ports), 1)
        self.assertEqual(env.keys_applied(), ['beta'])

    def test_missing_private_feedback_retains_no_hint(self):
        original = runtime_module._NativeProjectionPort.project_one
        def without_feedback(port, write_route, *, completion_budget=1):
            result = original(port, write_route, completion_budget=completion_budget)
            port._projection_feedback = None
            return result
        env = Harness((LIB, LIB2), {LIB: ('alpha',), LIB2: ('beta',)},
                      {(LIB, 'alpha'): 4, (LIB2, 'beta'): 1})
        try:
            with patch.object(runtime_module._NativeProjectionPort, 'project_one',
                              without_feedback):
                env.visit()
                env.visit()
        finally:
            env.close()
        self.assertEqual(env.discoveries, 2)
        self.assertEqual(env.keys_applied(), ['alpha', 'beta'])

    def test_malformed_private_feedback_clears_hint(self):
        original = runtime_module._NativeProjectionPort.project_one
        def malformed(port, write_route, *, completion_budget=1):
            result = original(port, write_route, completion_budget=completion_budget)
            port._projection_feedback = ProjectionResult(
                1, 'foreign', ([1],))
            return result
        env = Harness((LIB, LIB2), {LIB: ('alpha',), LIB2: ('beta',)},
                      {(LIB, 'alpha'): 4, (LIB2, 'beta'): 1})
        try:
            with patch.object(runtime_module._NativeProjectionPort, 'project_one',
                              malformed):
                env.visit()
            env.visit()
        finally:
            env.close()
        self.assertEqual(env.keys_applied(), ['alpha', 'beta'])
        self.assertEqual(env.discoveries, 2)

    def test_second_hint_write_acl_rejection_defers_before_effect(self):
        env = Harness((LIB, LIB2), {LIB: ('alpha',), LIB2: ('beta',)},
                      {(LIB, 'alpha'): 4, (LIB2, 'beta'): 1})
        try:
            env.visit()
            env.prepare_errors['alpha'] = 'project_scope_unavailable'
            with self.assertRaises(ProjectionDeferred):
                env.visit()
            self.assertEqual(env.applies, 1)
            self.assertEqual(env.keys_applied(), ['alpha'])
            env.visit()
        finally:
            env.close()
        self.assertEqual(env.keys_applied(), ['alpha', 'beta'])
        self.assertEqual(env.discoveries, 2)

    def test_token_and_route_denial_keep_existing_deferral_path(self):
        for kind, code in (('token', 'library_access_denied'),
                           ('route', 'library_unavailable'),
                           ('route', 'authority_changed'),
                           ('route', 'policy_epoch_changed')):
            with self.subTest(kind=kind, code=code):
                env = Harness((LIB, LIB2), {LIB: ('alpha',), LIB2: ('beta',)},
                              {(LIB, 'alpha'): 4, (LIB2, 'beta'): 1})
                try:
                    env.visit()
                    if kind == 'token':
                        env.token_error = FoundationError(code)
                    else:
                        env.plan_errors[(LIB, 'context_read')] = code
                    with self.assertRaises(ProjectionDeferred):
                        env.visit()
                    self.assertEqual(env.applies, 1)
                    self.assertEqual(env.keys_applied(), ['alpha'])
                    env.token_error = None
                    env.plan_errors.clear()
                    env.visit()
                finally:
                    env.close()
                self.assertEqual(env.keys_applied(), ['alpha', 'beta'])
                self.assertEqual(env.discoveries, 2)

    def test_unknown_commit_is_fatal_without_retry_and_clears_hint(self):
        env = Harness((LIB, LIB2), {LIB: ('alpha',), LIB2: ('beta',)},
                      {(LIB, 'alpha'): 4, (LIB2, 'beta'): 1})
        try:
            env.visit()
            env.apply_error = FoundationError('commit_result_unknown')
            with self.assertRaises(FoundationError) as raised:
                env.visit()
            self.assertEqual(raised.exception.code, 'commit_result_unknown')
            self.assertEqual(env.applies, 1)
            self.assertEqual(env.keys_applied(), ['alpha'])
            env.apply_error = None
            env.visit()
        finally:
            env.close()
        self.assertEqual(env.keys_applied(), ['alpha', 'beta'])
        self.assertEqual(env.discoveries, 2)

    def test_unhandled_base_exception_is_not_masked_and_clears_hint(self):
        env = Harness((LIB, LIB2), {LIB: ('alpha',), LIB2: ('beta',)},
                      {(LIB, 'alpha'): 4, (LIB2, 'beta'): 1})
        error = RuntimeError('projection exploded')
        try:
            env.visit()
            env.apply_error = error
            with self.assertRaises(RuntimeError) as raised:
                env.visit()
            self.assertIs(raised.exception, error)
            self.assertEqual(env.keys_applied(), ['alpha'])
            env.apply_error = None
            env.visit()
        finally:
            env.close()
        self.assertEqual(env.keys_applied(), ['alpha', 'beta'])
        self.assertEqual(env.discoveries, 2)

    def test_cleanup_failure_is_fatal_after_normal_scope_exit(self):
        env = Harness((LIB, LIB2), {LIB: ('alpha',), LIB2: ('beta',)},
                      {(LIB, 'alpha'): 4, (LIB2, 'beta'): 1})
        try:
            env.visit()
            env.poison_guard = True
            with self.assertRaises(FoundationError) as raised:
                env.visit()
            self.assertEqual(raised.exception.code, 'library_host_cleanup_incomplete')
            self.assertTrue(env.host._unhealthy)
            self.assertTrue(env.guards[-1]._unhealthy.is_set())
            self.assertEqual(len([item for item in env.results
                                  if isinstance(item, ProjectionResult)]), 1)
            self.assertEqual(env.applies, 2)
            with self.assertRaises(FoundationError) as raised:
                env.visit()
            self.assertEqual(raised.exception.code, 'library_host_cleanup_incomplete')
            self.assertEqual(env.applies, 2)
        finally:
            env.close()

    def test_native_history_drift_is_fatal_and_clears_hint(self):
        env = Harness((LIB, LIB2), {LIB: ('alpha',), LIB2: ('beta',)},
                      {(LIB, 'alpha'): 4, (LIB2, 'beta'): 1})
        try:
            env.visit()
            env.history = True
            with self.assertRaises(FoundationError) as raised:
                env.visit()
            self.assertEqual(raised.exception.code, 'native_projection_history_drift')
            self.assertEqual(env.applies, 1)
            env.history = False
            env.visit()
        finally:
            env.close()
        self.assertEqual(env.keys_applied(), ['alpha', 'beta'])
        self.assertEqual(env.discoveries, 2)

    def test_historical_path_uses_eager_port_every_visit(self):
        env = Harness((LIB,), {LIB: ('alpha',)}, {(LIB, 'alpha'): 4},
                      history=True, ordinary_port=True)
        try:
            for _ in range(3):
                env.visit()
        finally:
            env.close()
        self.assertEqual(env.discoveries, 3)
        self.assertEqual(env.selections, [])
        self.assertEqual(len(env.ports), 3)
        self.assertTrue(all(not isinstance(port, runtime_module._NativeProjectionPort)
                            for port in env.ports))
        self.assertTrue(all(not hasattr(port, '_projection_result')
                            for port in env.ports))

    def test_private_feedback_is_readonly_and_reset_on_failure(self):
        env = Harness((LIB,), {LIB: ('alpha',)}, {(LIB, 'alpha'): 4})
        try:
            result = env.visit()
            port = env.ports[-1]
            feedback = port._projection_result
            self.assertIsInstance(feedback, ProjectionResult)
            self.assertIs(feedback, result)
            self.assertIs(port._projection_result, feedback)
            with self.assertRaises(AttributeError):
                port._projection_result = ProjectionResult(1, 'forged', (1,))
            with self.assertRaises(TypeError):
                port.project_one(None, forged=True)
            self.assertIs(port._projection_result, feedback)
            env.apply_error = RuntimeError('failed attempt')
            with self.assertRaises(RuntimeError):
                env.visit()
            self.assertIsNone(env.ports[-1]._projection_result)
        finally:
            env.close()

    def test_ordinary_fallback_port_retains_no_hint(self):
        env = Harness((LIB, LIB2), {LIB: ('alpha',), LIB2: ('beta',)},
                      {(LIB, 'alpha'): 4, (LIB2, 'beta'): 4}, ordinary_port=True)
        try:
            env.visit()
            env.visit()
        finally:
            env.close()
        self.assertEqual(env.discoveries, 2)
        self.assertEqual([port.key for port in env.ports], ['alpha', 'beta'])
        self.assertTrue(all(not hasattr(port, '_projection_result')
                            for port in env.ports))

    def test_cursor_skips_empty_prefix_and_holds_selected_key(self):
        env = Harness((LIB,), {LIB: ('alpha', 'beta', 'gamma', 'zeta')},
                      {(LIB, 'alpha'): 0, (LIB, 'beta'): 0,
                       (LIB, 'gamma'): 4, (LIB, 'zeta'): 1})
        try:
            for _ in range(5):
                env.visit()
        finally:
            env.close()
        self.assertEqual(env.keys_applied(), ['gamma'] * 4 + ['zeta'])
        self.assertEqual(env.selections, [(LIB, None), (LIB, 'gamma')])
        self.assertEqual(env.discoveries, 2)


if __name__ == '__main__':
    unittest.main()

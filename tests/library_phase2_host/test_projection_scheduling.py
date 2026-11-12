"""Bounded productive projection runs and wrap-once discovery.

The real ProjectionService/factory/_project code path is exercised: only
VectorPort construction, routing, session-management and repository lookups are
replaced, so each recorded job is one actual factory context with its own
request scope, identity, guard and deadline. No DB, thread, model or service is
started.
"""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import importlib.util
import sys
import unittest

path = Path(__file__).with_name('test_host_startup.py')
spec = importlib.util.spec_from_file_location('projection_scheduling_support', path)
support = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = support
spec.loader.exec_module(support)

from evolvmem.library_foundation import FoundationError, RequestIdentity
from evolvmem.library_project_acl.coordinator import project_uuid
from evolvmem.library_project_acl import scope as scope_module
from evolvmem.pg_vector_port import repository as repository_module
import evolvmem.library_host as module
import evolvmem.pg_vector_port.runtime as runtime_module
from evolvmem.pg_vector_port.runtime import ProjectionResult
import evolvmem.pg_vector_port.worker as worker_module

LIB, LIB2 = support.uid(31), support.uid(35)
PROBE = "SELECT to_regclass('em_security.history_readiness') IS NOT NULL"
LISTING = ('SELECT p.project_id::text,p.project_key,COALESCE(w.has_pending,false) '
           'FROM em_security.list_recall_projects(')


def row(key, pending):
    return (project_uuid(key), key, pending)


def run(page, calls, *, history=False, schema_error=None, on_port=None,
        library_ids=(LIB,), pages=None):
    provider = SimpleNamespace(contract=SimpleNamespace(key='trusted-model'),
        encode_query=lambda *a, **k: None, close=lambda: None)
    host = support.host(recall_enabled=True, embedding_provider=provider,
                        model_contract='trusted-model')
    events, queries, results = [], [], []
    states, guards, deadlines = [], [], []
    active_at_port, active_after, invocations = [], [], [0]

    class Port:
        def __init__(self, manager, routes, contract, project_key=None,
                     embedding_provider=None):
            state = host._state()
            states.append(state)
            guards.append(state.guard)
            deadlines.append(state.guard.absolute_deadline())
            active_at_port.append(host.active_requests)
            events.append(('port', project_key, routes[0].library_id))
            self.key, self.guard = project_key, state.guard
            self.timeout = state.guard.timeout_seconds
        def project_one(self, route, *, completion_budget=1):
            assert 1 <= completion_budget <= 2
            events.append(('job', self.key))
            if on_port is not None:
                on_port(self.key)
            return SimpleNamespace(outcome='projected', job_id='job-%s' % self.key)

    class Router:
        def __init__(self, *a, **k):
            pass
        def plan(self, identity, tool, library_id):
            events.append(('plan', tool, library_id))
            return SimpleNamespace(library=SimpleNamespace(writer_backend='postgres'),
                library_id=library_id, identity=identity, tool=tool,
                action='read' if tool == 'context_read' else 'write')

    class Manager:
        def __init__(self, *a, **k):
            pass
        def run(self, route, operation):
            invocations[0] += 1
            seen = []
            def execute(sql, params=()):
                if sql == PROBE:
                    return ((history,),)
                assert sql.startswith(LISTING), sql
                assert params[1] == 'write' and params[0] == params[3]
                assert params[4] == 'trusted-model' and len(params) == 5
                events.append(('sql', params[0], params[2]))
                seen.append(params[2])
                chosen = (pages or {}).get(params[0], page)
                return chosen(params[2], len(seen), invocations[0])
            try:
                return operation(SimpleNamespace(execute=execute))
            finally:
                queries.append(seen)

    class Repository:
        def __init__(self, session, contract, scope):
            events.append(('repository', scope.project_key))
        def check_schema(self):
            events.append(('schema',))
            if schema_error is not None:
                raise FoundationError(schema_error)

    class Service(worker_module.ProjectionService):
        def start(self):
            events.append(('start', self.per_pass, self.maximum_jobs))
            return self.status()

    identity = RequestIdentity(support.uid(32), support.uid(33), support.uid(34), 1)
    def auth(token):
        events.append(('auth', token, host.active_requests))
        host._state().identity = identity
        return identity
    host.authenticate = auth

    def resolve(session, key):
        events.append(('scope', key))
        return SimpleNamespace(project_key=key)

    with patch.object(worker_module, 'ProjectionService', Service), \
            patch.object(worker_module, 'VectorPort', Port), \
            patch.object(runtime_module, 'VectorPort', Port), \
            patch.object(module, 'AuthorityRouter', Router), \
            patch.object(module, 'SessionManager', Manager), \
            patch.object(module, 'ParallelReadCoordinator', lambda *a, **k: None), \
            patch.object(scope_module, 'resolve_project_scope', resolve), \
            patch.object(repository_module, 'VectorRepository', Repository):
        host.start_projection(token_loader=lambda: 'synthetic',
                              library_ids=tuple(library_ids))
        try:
            for _ in range(calls):
                try:
                    results.append(host._projection_worker._project())
                except FoundationError as exc:
                    results.append(exc)
                active_after.append(host.active_requests)
        finally:
            host.close()
    return SimpleNamespace(events=events, results=results, queries=queries,
        states=states, guards=guards, deadlines=deadlines,
        active_at_port=active_at_port, active_after=active_after, host=host)


class ProjectionSchedulingChecks(unittest.TestCase):
    def test_ordinary_fallback_rediscovers_each_visit_without_hint(self):
        remaining = {'alpha': 4, 'beta': 4}
        def page(cursor, n, inv):
            return tuple(row(key, remaining[key] > 0) for key in ('alpha', 'beta')
                         if cursor is None or key > cursor)
        def on_port(key):
            remaining[key] -= 1
        out = run(page, 8, on_port=on_port)
        self.assertEqual([e[1] for e in out.events if e[0] == 'job'],
                         ['alpha', 'beta'] * 4)
        # An ordinary test port has no private native feedback contract.
        # Every visit rediscovers and advances; it never receives a hint.
        self.assertEqual([result is None for result in out.results], [False] * 8)
        self.assertEqual([e for e in out.events if e[0] == 'start'][0][1:], (1, 256))
        # Each ordinary fallback visit performs discovery; exhausted tails
        # wrap at most once in that same request.
        self.assertEqual([len(query) for query in out.queries], [1, 1, 2, 1, 2, 1, 2, 1])
        # Every job owns an actual, distinct request scope/identity/guard.
        self.assertEqual(len(out.states), 8)
        self.assertEqual(len({id(state) for state in out.states}), 8)
        self.assertEqual(len({id(guard) for guard in out.guards}), 8)
        self.assertEqual(out.active_at_port, [1] * 8)
        self.assertEqual(out.active_after, [0] * 8)
        self.assertEqual([e[1] for e in out.events if e[0] == 'auth'], ['synthetic'] * 8)
        self.assertEqual([e[2] for e in out.events if e[0] == 'auth'], [1] * 8)
        self.assertTrue(all(guard.timeout_seconds == 30 for guard in out.guards))
        self.assertEqual(out.deadlines, sorted(out.deadlines))

    def test_empty_tail_wraps_once_in_the_same_transaction(self):
        def page(cursor, n, inv):
            if inv == 1:
                return (row('alpha', False), row('beta', False))
            if inv == 2:
                return () if cursor is not None else (row('alpha', False), row('beta', False))
            return () if cursor is not None else (row('gamma', True),)
        out = run(page, 3)
        self.assertEqual([result is None for result in out.results], [True, True, False])
        self.assertEqual([len(query) for query in out.queries], [1, 2, 2])
        self.assertEqual(out.queries[1], ['beta', None])
        self.assertEqual(out.queries[2], ['beta', None])
        self.assertEqual([e[1] for e in out.events if e[0] == 'job'], ['gamma'])
        # Model/schema witness still runs for each non-empty idle page,
        # including the page reached through the single wrap.
        self.assertEqual(len([e for e in out.events if e[0] == 'schema']), 2)
        self.assertEqual([e[1] for e in out.events if e[0] == 'scope'], ['alpha', 'alpha'])
        self.assertEqual(len([e for e in out.events if e[0] == 'auth']), 3)

    def test_null_start_empty_queries_once_and_rotates_libraries(self):
        out = run(lambda cursor, n, inv: (), 4, library_ids=(LIB, LIB2))
        self.assertEqual(out.results, [None] * 4)
        self.assertEqual([len(query) for query in out.queries], [1, 1, 1, 1])
        self.assertEqual([query[0] for query in out.queries], [None] * 4)
        self.assertEqual([e[2] for e in out.events if e[0] == 'plan'],
                         [LIB, LIB2, LIB, LIB2])
        self.assertFalse([e for e in out.events if e[0] in ('port', 'job')])

    def test_flapping_hot_key_cannot_starve_another_library(self):
        # Ordinary fallback rediscovers every visit; two libraries alternate
        # even when the selected project changes. Native burst proof uses the
        # real private port in test_projection_selection_hint.py.
        a_state = {'next': 'alpha'}
        def page_a(cursor, n, inv):
            target = a_state['next']
            return tuple(row(key, key == target) for key in ('alpha', 'beta')
                         if cursor is None or key > cursor)
        def page_b(cursor, n, inv):
            return (row('gamma', True),) if cursor is None else ()
        def on_port(key):
            if key in ('alpha', 'beta'):
                a_state['next'] = 'beta' if key == 'alpha' else 'alpha'
        out = run(page_a, 8, on_port=on_port, library_ids=(LIB, LIB2),
                  pages={LIB: page_a, LIB2: page_b})
        libraries = [event[2] for event in out.events if event[0] == 'port']
        self.assertEqual(libraries, [LIB, LIB2] * 4)
        keys = [event[1] for event in out.events if event[0] == 'job']
        self.assertEqual(keys, ['alpha', 'gamma', 'beta', 'gamma'] * 2)
        self.assertEqual(len([key for key in libraries if key == LIB2]), 4)
        self.assertEqual([result is None for result in out.results], [False] * 8)
        self.assertLessEqual(max(len(query) for query in out.queries), 2)

    def test_ordinary_fallback_observes_revoked_page_next_visit(self):
        remaining = {'alpha': 4, 'beta': 4}
        revoked = [False]
        def page(cursor, n, inv):
            return tuple(row(key, remaining[key] > 0) for key in ('alpha', 'beta')
                         if not (key == 'alpha' and revoked[0])
                         and (cursor is None or key > cursor))
        def on_port(key):
            remaining[key] -= 1
            if key == 'alpha' and remaining['alpha'] == 2:
                revoked[0] = True
        out = run(page, 6, on_port=on_port)
        keys = [e[1] for e in out.events if e[0] == 'job']
        # This ordinary fake covers refreshed pagination without hint reuse.
        # After alpha disappears from the listing, no later visit selects it.
        # Actual hinted ACL refusal is covered by the real native Harness.
        self.assertEqual(keys, ['alpha', 'beta', 'alpha', 'beta', 'beta', 'beta'])
        self.assertEqual([result is None for result in out.results], [False] * 6)
        self.assertEqual(len([e for e in out.events if e[0] == 'port']), 6)

    def test_visit_failure_is_fatal_and_next_request_advances_fairly(self):
        remaining = {'alpha': 4, 'beta': 4}
        def page(cursor, n, inv):
            return tuple(row(key, remaining[key] > 0) for key in ('alpha', 'beta')
                         if cursor is None or key > cursor)
        def on_port(key):
            remaining[key] -= 1
            if key == 'alpha' and remaining['alpha'] == 2:
                raise FoundationError('commit_result_unknown')
        out = run(page, 4, on_port=on_port)
        self.assertIsInstance(out.results[2], FoundationError)
        self.assertEqual(out.results[2].code, 'commit_result_unknown')
        self.assertEqual(out.active_after, [0, 0, 0, 0])
        # The failed project is not retried by the next (fresh) request.
        self.assertEqual([e[1] for e in out.events if e[0] == 'job'],
                         ['alpha', 'beta', 'alpha', 'beta'])

    def test_listing_contract_and_idle_model_witness_preserved(self):
        out = run(lambda cursor, n, inv: (row('alpha', False),), 2)
        self.assertEqual(out.results, [None, None])
        self.assertEqual([e[1:] for e in out.events if e[0] == 'sql'],
                         [(LIB, None), (LIB, 'alpha')])
        self.assertEqual(len([e for e in out.events if e[0] == 'schema']), 2)
        failed = run(lambda cursor, n, inv: (row('alpha', False),), 1,
                     schema_error='registered_project_vector_model_required')
        self.assertEqual([getattr(r, 'code', None) for r in failed.results],
                         ['registered_project_vector_model_required'])
        self.assertEqual(failed.active_after, [0])
        invalid = run(lambda cursor, n, inv: ((project_uuid('alpha'), 'alpha'),), 1)
        self.assertEqual([getattr(r, 'code', None) for r in invalid.results],
                         ['projection_project_listing_invalid'])
        self.assertEqual(invalid.active_after, [0])

    def test_historical_path_keeps_single_visit_rotation(self):
        out = run(lambda cursor, n, inv: (row('alpha', True),), 2, history=True)
        self.assertFalse([e for e in out.events if e[0] == 'sql'])
        self.assertEqual([e[1] for e in out.events if e[0] == 'port'], [None, None])
        self.assertEqual([e[1] for e in out.events if e[0] == 'job'], [None, None])
        self.assertEqual([result is None for result in out.results], [False, False])


if __name__ == '__main__':
    unittest.main()

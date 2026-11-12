"""Native listing selection: pending annotation, cursor and no-port paths."""
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
import importlib.util
import sys
import unittest

path = Path(__file__).with_name('test_host_startup.py')
spec = importlib.util.spec_from_file_location('projection_pending_support', path)
support = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = support
spec.loader.exec_module(support)
from evolvmem.library_foundation import FoundationError, RequestIdentity
from evolvmem.library_project_acl.coordinator import project_uuid
from evolvmem.library_project_acl import scope as scope_module
from evolvmem.pg_vector_port import repository as repository_module
import evolvmem.library_host as module
from evolvmem.pg_vector_port.runtime import ProjectionResult

LIB = support.uid(31)


def row(key, pending):
    return (project_uuid(key), key, pending)


def exercise(page, calls, *, history=False, model='trusted-model', schema_error=None):
    provider = SimpleNamespace(contract=SimpleNamespace(key=model),
        encode_query=lambda *a, **k: None, close=lambda: None)
    host = support.host(recall_enabled=True, embedding_provider=provider, model_contract=model)
    wire = ModuleType('evolvmem.pg_vector_port.worker')
    runtime = ModuleType('evolvmem.pg_vector_port.runtime')
    events = []
    pages = []

    class ProjectionDeferred(RuntimeError):
        pass

    class Service:
        def __init__(self, factory, **kwargs):
            self.factory = factory
        def start(self):
            events.append('start')
        def close(self, **kwargs):
            events.append('stop')

    class Router:
        def __init__(self, *a, **k):
            pass
        def plan(self, identity, tool, library_id):
            events.append(('plan', tool, library_id, identity))
            return SimpleNamespace(library=SimpleNamespace(writer_backend='postgres'),
                library_id=library_id, identity=identity, tool=tool,
                action='read' if tool == 'context_read' else 'write')

    class Manager:
        def __init__(self, *a, **k):
            pass
        def run(self, route, operation):
            def execute(sql, params=()):
                if sql == "SELECT to_regclass('em_security.history_readiness') IS NOT NULL":
                    assert params == ()
                    return ((history,),)
                events.append(('sql', sql, params))
                pages.append(params[2])
                batch = page(params[2])
                events.append(('page', batch))
                return batch
            return operation(SimpleNamespace(execute=execute))

    class Port:
        def __init__(self, manager, routes, contract, **kwargs):
            events.append(('port', kwargs['project_key'], routes[0].library_id))

    def resolve(session, key):
        events.append(('scope', key))
        return SimpleNamespace(project_key=key)

    class Repository:
        def __init__(self, session, contract, scope):
            events.append(('repository', contract.key, scope.project_key))
        def check_schema(self):
            events.append(('schema',))
            if schema_error is not None:
                raise FoundationError(schema_error)

    wire.ProjectionDeferred = ProjectionDeferred
    runtime.ProjectionResult = ProjectionResult
    wire.ProjectionService = Service
    runtime.VectorPort = Port
    identity = RequestIdentity(support.uid(32), support.uid(33), support.uid(34), 1)

    def auth(token):
        state = host._state()
        events.append(('auth', token, host.active_requests))
        state.identity = identity
        return identity

    host.authenticate = auth
    results = []
    with patch.dict(sys.modules, {wire.__name__: wire, runtime.__name__: runtime}), \
            patch.object(module, 'AuthorityRouter', Router), \
            patch.object(module, 'SessionManager', Manager), \
            patch.object(module, 'ParallelReadCoordinator', lambda *a, **k: None), \
            patch.object(scope_module, 'resolve_project_scope', resolve), \
            patch.object(repository_module, 'VectorRepository', Repository):
        host.start_projection(token_loader=lambda: 'synthetic', library_ids=(LIB,))
        try:
            for _ in range(calls):
                with host._projection_worker.factory() as context:
                    results.append(context)
        finally:
            host.close()
    return events, results, pages


class ProjectionPendingSelectionChecks(unittest.TestCase):
    def test_page_without_pending_activates_no_port_and_advances_then_resets(self):
        def page(after):
            if after is None:
                return (row('alpha', False), row('beta', False))
            return ()
        events, results, pages = exercise(page, 2)
        self.assertEqual(results, [None, None])
        self.assertEqual([e[0] for e in events].count('port'), 0)
        # One wrap is allowed for the exhausted tail, inside the same read
        # transaction; the following null-start probe queries only once.
        self.assertEqual(pages, [None, 'beta', None])
        sqls = [e for e in events if e[0] == 'sql']
        self.assertEqual(len(sqls), 3)
        query = sqls[0][1]
        self.assertIn('vector_outbox', query)
        self.assertIn('list_recall_projects', query)
        self.assertIn('LEFT JOIN LATERAL (SELECT true AS has_pending', query)
        self.assertIn("o.status='pending' LIMIT 1) w ON true", query)
        self.assertIn('COALESCE(w.has_pending,false)', query)
        self.assertNotIn('WHERE w.', query)
        self.assertNotIn('AND w.', query)
        self.assertTrue(query.endswith('ORDER BY p.project_key COLLATE "C"'))
        self.assertEqual(sqls[0][2], (LIB, 'write', None, LIB, 'trusted-model'))
        self.assertEqual(sqls[1][2], (LIB, 'write', 'beta', LIB, 'trusted-model'))
        self.assertEqual(sqls[2][2], (LIB, 'write', None, LIB, 'trusted-model'))
        self.assertEqual([e[1] for e in events if e[0] == 'scope'], ['alpha', 'alpha'])
        self.assertEqual([e[0] for e in events].count('schema'), 2)
        # Both authorized rows survive the annotation join; the cursor advances
        # to the last row rather than dropping or filtering page rows, and the
        # wrapped idle page still validates the model witness.
        self.assertEqual([e[1] for e in events if e[0] == 'page'],
                         [(row('alpha', False), row('beta', False)), (),
                          (row('alpha', False), row('beta', False))])
        # The empty page is the only reset, and the following probe starts over.
        self.assertEqual([e for e in events if e[0] == 'auth'][0][2], 1)

    def test_cursor_skips_empty_prefix_picks_first_pending_then_wraps(self):
        starts = [0]
        def page(after):
            if after is None:
                starts[0] += 1
                return ((row('alpha', False), row('beta', False)) if starts[0] == 1
                        else (row('omega', True),))
            return {'beta': (row('gamma', True), row('zeta', True)),
                    'gamma': ()}[after]
        # This fake port has no private native feedback contract, so the host
        # retains no scheduling hint: every visit performs its own discovery.
        events, results, pages = exercise(page, 3)
        self.assertEqual([result is None for result in results],
                         [True, False, False])
        # The empty prefix advances to beta, gamma is picked first, then the
        # exhausted tail wraps once (two pages in one transaction) to omega.
        self.assertEqual(pages, [None, 'beta', 'gamma', None])
        ports = [e[1] for e in events if e[0] == 'port']
        self.assertEqual(ports, ['gamma', 'omega'])
        self.assertTrue(all(e[2] == LIB for e in events if e[0] == 'port'))
        sqls = [e[2] for e in events if e[0] == 'sql']
        self.assertEqual([p[2] for p in sqls], pages)
        self.assertTrue(all(len(p) == 5 and p[0] == LIB and p[1] == 'write'
                            and p[3] == LIB and p[4] == 'trusted-model' for p in sqls))
        self.assertTrue(all(event[1].endswith('ORDER BY p.project_key COLLATE "C"')
                            for event in events if event[0] == 'sql'))
        self.assertEqual([e[1] for e in events if e[0] == 'scope'], ['alpha'])
        self.assertEqual([e[0] for e in events].count('schema'), 1)
        plans = [e for e in events if e[0] == 'plan']
        self.assertTrue(plans and all(e[1] in ('context_read', 'memory_add') for e in plans))
        self.assertEqual(len([e for e in events if e[0] == 'auth']), 3)

    def test_model_key_is_exact_and_listing_shape_is_checked(self):
        events, results, pages = exercise(lambda after: (row('gamma', True),), 1, model='other-model')
        self.assertIsNotNone(results[0])
        sqls = [e[2] for e in events if e[0] == 'sql']
        self.assertEqual(sqls[0][4], 'other-model')
        with self.assertRaises(FoundationError):
            exercise(lambda after: ((project_uuid('gamma'), 'gamma'),), 1)
        with self.assertRaises(FoundationError):
            exercise(lambda after: (row('beta', False), row('alpha', False)), 1)

    def test_no_pending_page_validates_schema_and_propagates_failure(self):
        with self.assertRaises(FoundationError) as raised:
            exercise(lambda after: (row('alpha', False),), 1,
                     schema_error='registered_project_vector_model_required')
        self.assertEqual(raised.exception.code, 'registered_project_vector_model_required')

    def test_historical_path_never_lists_or_annotates_projects(self):
        events, results, pages = exercise(lambda after: (), 1, history=True)
        self.assertEqual(pages, [])
        self.assertFalse([e for e in events if e[0] == 'sql'])
        self.assertEqual([e[1] for e in events if e[0] == 'port'], [None])


if __name__ == '__main__':
    unittest.main()

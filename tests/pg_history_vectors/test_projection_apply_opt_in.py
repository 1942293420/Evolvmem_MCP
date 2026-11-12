"""Only the native vector apply path opts into pre-effect conflict deferral."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from evolvmem.library_foundation import _projection_conflict as conflict
from evolvmem.pg_vector_port import runtime as runtime_module
from evolvmem.pg_vector_port.runtime import ProjectionResult, ProjectionSerializationDeferred, VectorPort
from evolvmem.pg_vector_port.projection import Document, Job


class Guard:
    def begin_authority(self): pass
    def check(self): pass
    @contextmanager
    def enforce(self): yield


class Manager:
    def __init__(self):
        self.calls = []
    def _projection_apply(self, operation):
        return conflict._ProjectionApplyOperation(operation)
    def run(self, route, operation):
        self.calls.append(type(operation))
        return operation(None)


def instance(repository):
    port = object.__new__(VectorPort)
    port.project_key = 'alpha'
    port.contract = SimpleNamespace(key='trusted-model')
    port.guard = Guard()
    port.cache_lookup = None
    port._historical_libraries = set()
    port._scope = lambda route: route
    port.repository = lambda session: repository
    port.manager = Manager()
    port._embedding = lambda text, *, query: 'embedding'
    return port


class Repository:
    def __init__(self):
        self.job = Job(1, 'lib', 'context', 9, 'upsert', 1, 'digest', 'trusted-model', 'project')
        self.applied = []
    def next_job(self): return self.job
    def document(self, job): return Document('lib', 'context', 9, 1, 'text', 'active', None,
                                             'note', 'ready', ('alpha',), False, False)
    def apply(self, prepared, now):
        self.applied.append(prepared)
        return 'upsert'


class NativeOptInTests(unittest.TestCase):
    def test_native_apply_uses_private_opt_in_and_maps_confirmed_sentinel(self):
        repository = Repository()
        port = instance(repository)
        def apply(prepared, now):
            self.assertIs(port.manager.calls[-1], conflict._ProjectionApplyOperation)
            repository.applied.append(prepared)
            raise conflict.ProjectionConflictDeferred()
        repository.apply = apply
        with patch.object(runtime_module, 'decide', lambda *args: 'upsert'):
            with self.assertRaises(ProjectionSerializationDeferred) as cm:
                port.project_one(SimpleNamespace(action='write'))
        self.assertIs(type(cm.exception), ProjectionSerializationDeferred)
        self.assertTrue(issubclass(ProjectionSerializationDeferred, runtime_module.ProjectionDeferred))
        # prepare ran plain, only the single apply attempt opted in; no re-entry.
        self.assertEqual(2, len(port.manager.calls))
        self.assertEqual(1, sum(c is conflict._ProjectionApplyOperation for c in port.manager.calls))

    def test_failed_apply_does_not_reenter_or_retry(self):
        repository = Repository()
        port = instance(repository)
        def apply(prepared, now):
            repository.applied.append('attempt')
            raise runtime_module.FoundationError('database_operation_failed')
        repository.apply = apply
        with patch.object(runtime_module, 'decide', lambda *args: 'upsert'):
            with self.assertRaises(runtime_module.FoundationError):
                port.project_one(SimpleNamespace(action='write'))
        self.assertEqual(['attempt'], repository.applied)
        self.assertEqual(2, len(port.manager.calls))


if __name__ == '__main__':
    unittest.main()

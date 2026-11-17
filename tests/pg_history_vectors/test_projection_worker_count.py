"""Worker/drain acknowledge actual committed rows, never logical calls."""
from contextlib import contextmanager
from types import SimpleNamespace
import unittest

from evolvmem.pg_vector_port import worker as worker_module
from evolvmem.pg_vector_port.model import VectorError
from evolvmem.pg_vector_port.runtime import ProjectionResult, VectorPort


class Stop:
    def __init__(self, after):
        self.after, self.stopped, self.waits = after, False, []
    def is_set(self): return self.stopped
    def set(self): self.stopped = True
    def wait(self, seconds):
        self.waits.append(seconds)
        if len(self.waits) >= self.after: self.set()
        return self.stopped


class Port(VectorPort):
    def __init__(self, results):
        self.results, self.budgets = list(results), []
        self.guard = SimpleNamespace(timeout_seconds=30)
    def project_one(self, route, *, completion_budget=1):
        self.budgets.append(completion_budget)
        result = self.results.pop(0)
        if isinstance(result, Exception): raise result
        return result


class Script:
    def __init__(self, port):
        self.port, self.opened, self.active = port, [], []
    @contextmanager
    def context(self):
        request = object()
        self.active.append(request); self.opened.append(request)
        try:
            self.port.guard = SimpleNamespace(timeout_seconds=30)
            yield self.port, request
        finally:
            assert self.active.pop() is request


class WorkerCountTests(unittest.TestCase):
    def worker(self, results, maximum_jobs, *, stop_after=1):
        port = Port(results)
        script = Script(port)
        worker = worker_module.ProjectionWorker(script.context, enabled=True,
            interval_seconds=.5, max_jobs_per_pass=1, max_jobs=maximum_jobs, idle_probe_budget=8)
        worker._stop = Stop(after=stop_after)
        return worker, port

    def test_pair_result_advances_epoch_by_actual_rows_and_reduces_next_budget(self):
        worker, port = self.worker([
            ProjectionResult(1, 'upsert', (1, 2)),
            ProjectionResult(3, 'upsert', (3,)),
        ], maximum_jobs=3, stop_after=2)
        worker._epoch()
        self.assertEqual(port.budgets, [2, 1])
        self.assertEqual(worker.status()['completed_jobs'], 3)
        self.assertEqual(worker.status()['epoch_jobs'], 3)

    def test_one_request_acknowledges_what_singles_need_two_requests_for(self):
        worker, port = self.worker([ProjectionResult(1, 'upsert', (1, 2))],
                                   maximum_jobs=2, stop_after=1)
        worker._epoch()
        self.assertEqual(port.budgets, [2])
        self.assertEqual(worker.status()['completed_jobs'], 2)
        self.assertEqual(len(worker.factory.__self__.opened), 1)
        singles, single_port = self.worker([ProjectionResult(1, 'upsert', (1,)),
                                            ProjectionResult(2, 'upsert', (2,))],
                                           maximum_jobs=2, stop_after=2)
        singles._epoch()
        self.assertEqual(single_port.budgets, [2, 1])
        self.assertEqual(singles.status()['completed_jobs'], 2)
        self.assertEqual(len(singles.factory.__self__.opened), 2)

    def test_omitted_ids_never_infer_a_completion(self):
        result = ProjectionResult(1, 'upsert')
        self.assertEqual(result.acknowledged_job_ids, ())
        self.assertEqual(result.acknowledged_job_count, 0)
        worker, port = self.worker([result], maximum_jobs=2)
        worker._epoch()
        self.assertEqual(port.budgets, [2])
        self.assertEqual(worker.status()['completed_jobs'], 0)

    def test_zero_ack_outcomes_are_nonproductive(self):
        for result in (ProjectionResult(None, 'no_pending_for_model'),
                       ProjectionResult(1, 'already_claimed_or_processed', ()),
                       ProjectionResult(1, 'stale_revision', ())):
            with self.subTest(outcome=result.outcome):
                worker, port = self.worker([result], maximum_jobs=2)
                worker._epoch()
                self.assertEqual(worker.status()['completed_jobs'], 0)
                self.assertEqual(worker.status()['epoch_jobs'], 0)

    def test_over_budget_duplicate_or_fake_ids_are_rejected(self):
        cases = [(1, 2, 3), (1, 1), (0,), (-1,), (True,), ('1',)]
        for ids in cases:
            with self.subTest(ids=ids):
                worker, _ = self.worker([ProjectionResult(1, 'upsert', ids)], maximum_jobs=4)
                with self.assertRaises(VectorError) as raised:
                    worker._confirmed(ProjectionResult(1, 'upsert', ids), 2)
                self.assertEqual(raised.exception.code, 'invalid_projection_acknowledged_ids')


class DrainBudgetTests(unittest.TestCase):
    def test_drain_never_acknowledges_more_than_max_jobs(self):
        port = object.__new__(VectorPort)
        results = [ProjectionResult(1, 'upsert', (1, 2)),
                   ProjectionResult(3, 'upsert', (3,))]
        port.results, port.budgets = results, []
        def project_one(route, *, completion_budget=1):
            port.budgets.append(completion_budget)
            result = port.results.pop(0)
            assert len(result.acknowledged_job_ids) <= completion_budget
            return result
        port.project_one = project_one
        outcomes = VectorPort.drain(port, SimpleNamespace(action='write'), max_jobs=3)
        self.assertEqual(port.budgets, [2, 1])
        self.assertEqual(sum(len(o.acknowledged_job_ids or ()) for o in outcomes), 3)

    def test_drain_one_can_never_pair(self):
        port = object.__new__(VectorPort)
        port.budgets = []
        port.project_one = lambda route, *, completion_budget=1: (
            port.budgets.append(completion_budget) or ProjectionResult(1, 'upsert', (1,)))
        outcomes = VectorPort.drain(port, SimpleNamespace(action='write'), max_jobs=1)
        self.assertEqual(port.budgets, [1])
        self.assertEqual(outcomes[0].acknowledged_job_ids, (1,))


if __name__ == '__main__':
    unittest.main()

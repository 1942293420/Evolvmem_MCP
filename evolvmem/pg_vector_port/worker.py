"""Explicit projection epochs and supervised service; import starts no thread."""
import threading
import time
import re
from .model import VectorError, require
from .runtime import (VectorPort,ProjectionBatchResult,ProjectionDeferred,
                      ProjectionSerializationDeferred,_NativeProjectionPort)


class ProjectionWorker:
    """context_factory yields (actual VectorPort, actual authorized write route).

    The host creates a fresh request guard/identity/manager on this worker's
    thread for each job. Never capture an HTTP request's guard or connection.
    The owned encoder must have its own bounded native-process lifecycle.
    An error, including unknown commit, stops this run without a retry.
    """
    def __init__(self, context_factory, *, enabled=False, interval_seconds=1,
                 max_runtime_seconds=300, max_jobs_per_pass=4, max_jobs=256,
                 idle_probe_budget=1):
        require(callable(context_factory) and type(enabled) is bool, 'trusted_projection_factory_required')
        require(type(interval_seconds) in (int, float) and .1 <= interval_seconds <= 60
                and type(max_runtime_seconds) in (int, float) and 1 <= max_runtime_seconds <= 3600,
                'invalid_projection_schedule')
        require(type(max_jobs_per_pass) is int and 1 <= max_jobs_per_pass <= 16
                and type(max_jobs) is int and 1 <= max_jobs <= 4096, 'invalid_projection_budget')
        require(type(idle_probe_budget) is int and 1 <= idle_probe_budget <= 16,
                'invalid_projection_idle_budget')
        self.factory, self.enabled = context_factory, enabled
        self.interval, self.maximum_seconds = interval_seconds, max_runtime_seconds
        self.per_pass, self.maximum_jobs = max_jobs_per_pass, max_jobs
        self.idle_probe_budget = idle_probe_budget
        self._stop, self._lock = threading.Event(), threading.Lock()
        self._thread = None
        self._continuous = False
        self._state = dict(state='not_started', completed_jobs=0, completed_epochs=0,
                           epoch_jobs=0, last_outcome=None, error_code=None)

    def start(self):
        require(self.enabled, 'projection_worker_disabled')
        with self._lock:
            require(self._thread is None and not self._stop.is_set(), 'projection_worker_already_started')
            self._state['state'] = 'running'
            self._thread = threading.Thread(target=self._run, name='evolvmem-bounded-projection', daemon=True)
            self._thread.start()
        return self.status()

    def _epoch(self):
        deadline = time.monotonic() + self.maximum_seconds
        completed, idle_probes = 0, 0
        with self._lock:
            self._state.update(state='running', epoch_jobs=0)
        while not self._stop.is_set() and time.monotonic() < deadline and completed < self.maximum_jobs:
            # Productive passes yield cooperatively; idle/deferred passes keep
            # the configured polling interval after their request has exited.
            delay = .01
            for _ in range(self.per_pass):
                if self._stop.is_set() or time.monotonic() >= deadline or completed >= self.maximum_jobs:
                    break
                budget = min(4, self.maximum_jobs - completed)
                # Factory cleanup finishes before any next effect or epoch.
                try:
                    result=self._project(budget)
                except ProjectionSerializationDeferred:
                    # Confirmed rollback/close ended the request without any
                    # vector effect. The factory has fully exited. Pace exactly
                    # like a deferred authority pass: full interval, fresh next
                    # identity/route/deadline, never a completed job.
                    with self._lock:self._state['last_outcome']='serialization_deferred'
                    idle_probes = 0
                    delay = self.interval
                    break
                except ProjectionDeferred:
                    # The factory has fully exited. Its host validates cleanup;
                    # the next pass gets a new identity, route and deadline.
                    with self._lock:self._state['last_outcome']='authority_deferred'
                    idle_probes = 0
                    delay = self.interval
                    break
                if result is None:
                    idle_probes += 1
                    if idle_probes >= self.idle_probe_budget:
                        idle_probes = 0
                        delay = self.interval
                        break
                    continue
                with self._lock:
                    self._state['last_outcome'] = result.outcome
                confirmed = self._confirmed(
                    result, min(4, budget) if isinstance(result, ProjectionBatchResult)
                    else min(2, budget))
                if not confirmed:
                    idle_probes += 1
                    if idle_probes >= self.idle_probe_budget:
                        idle_probes = 0
                        delay = self.interval
                        break
                    continue
                idle_probes = 0
                completed += len(confirmed)
                with self._lock:
                    self._state['completed_jobs'] += len(confirmed)
                    self._state['epoch_jobs'] = completed
            if completed < self.maximum_jobs:
                self._stop.wait(min(delay, max(0, deadline - time.monotonic())))

    @staticmethod
    def _confirmed(result, budget):
        """Exact committed ids only; older single-job results adapt to one row."""
        ids = result.acknowledged_job_ids
        require(type(ids) is tuple and len(ids) <= budget and len(set(ids)) == len(ids)
                and all(type(i) is int and i > 0 for i in ids)
                and (not ids or result.job_id is not None), 'invalid_projection_acknowledged_ids')
        return ids

    def _project(self, budget=1):
        with self.factory() as context:
            if context is None:
                with self._lock:
                    self._state['last_outcome'] = 'idle'
                return None
            require(isinstance(context, tuple) and len(context) == 2,
                    'bounded_actual_projection_context_required')
            port, write_route = context
            require(isinstance(port, VectorPort) and port.guard.timeout_seconds <= 30,
                    'bounded_actual_projection_context_required')
            if self._stop.is_set():
                return None
            # Only the actual private native projection object may batch; every
            # ordinary/hostorical port keeps its existing single/pair path with
            # the unchanged min(2, remaining) cap.
            # A native epoch budget of one is the pre-existing single-job case;
            # larger epoch budgets use the bounded batch. Every ordinary or
            # historical port keeps the unchanged project_one path.
            if isinstance(port, _NativeProjectionPort) and budget >= 2:
                result = port.project_batch(write_route, completion_budget=min(4, budget))
            else:
                result = port.project_one(write_route, completion_budget=min(2, budget))
        return result

    def _run(self):
        try:
            while not self._stop.is_set():
                self._epoch()
                if self._stop.is_set():
                    break
                with self._lock:
                    self._state['state'] = 'budget_complete'
                    self._state['completed_epochs'] += 1
                if not self._continuous:
                    return
                # Only fully successful, cleaned-up epochs can continue. The
                # exception handler below never restarts a failed/unknown job.
                self._stop.wait(self.interval)
            with self._lock:
                self._state['state'] = 'stopped'
        except BaseException as exc:
            code = getattr(exc, 'code', '')
            if not isinstance(code, str) or re.fullmatch('[a-z][a-z0-9_]{0,95}', code) is None:
                code = 'projection_worker_failed'
            with self._lock:
                self._state.update(state='failed', error_code=code)
            self._stop.set()  # Preserve durable pending/unknown state for explicit review.

    def status(self):
        with self._lock:
            return dict(self._state, active=self._thread is not None and self._thread.is_alive())

    def close(self, *, timeout=35):
        require(type(timeout) in (int, float) and 0 <= timeout <= 60, 'invalid_projection_shutdown_timeout')
        self._stop.set()
        thread = self._thread
        if thread is not None:
            require(thread is not threading.current_thread(), 'projection_self_join_refused')
            thread.join(timeout=timeout)
            require(not thread.is_alive(), 'projection_shutdown_incomplete')
        return self.status()


class ProjectionService(ProjectionWorker):
    """One owned thread; only successful finite epochs allow another epoch."""
    def __init__(self, context_factory, **options):
        require(type(options.get('max_jobs_per_pass', 4)) is int
                and 1 <= options.get('max_jobs_per_pass', 4) <= 4, 'invalid_projection_service_budget')
        super().__init__(context_factory, **options)
        self._continuous = True

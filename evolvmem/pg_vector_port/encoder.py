"""Bounded owned process around the existing real EmbeddingEngine; no downloads."""
from copy import deepcopy
from collections import deque
import hashlib
import math
import multiprocessing
import os
from pathlib import Path
import threading
import time

from evolvmem.config import Config
from evolvmem.runtime_contract import EMBEDDING_INPUT_TOKENS
from .model import ModelContract, VectorError, require, vector


def _revision(path, deadline):
    path = Path(path)
    before = path.stat()
    require(path.is_file() and 0 < before.st_size <= 2 * 1024**3, 'existing_model_file_required')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024**2):
            require(time.monotonic() < deadline, 'embedding_model_hash_timeout')
            digest.update(chunk)
    after = path.stat()
    identity = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    require(identity(before) == identity(after), 'embedding_model_file_changed')
    return digest.hexdigest(), identity(after)


def _worker_cpus(available, offset):
    cpus = sorted(available)
    require(bool(cpus), 'embedding_cpu_affinity_unavailable')
    start = offset % len(cpus)
    return {cpus[(start + index) % len(cpus)] for index in range(min(2, len(cpus)))}


def _child(connection, config, expected_revision, startup_seconds, cpu_offset=0):
    engine = None
    try:
        if hasattr(os, 'nice'):
            os.nice(10)
        if hasattr(os, 'sched_getaffinity'):
            os.sched_setaffinity(0, _worker_cpus(os.sched_getaffinity(0), cpu_offset))
        from evolvmem.embedding import EmbeddingEngine
        revision, before = _revision(config.model_path, time.monotonic() + startup_seconds)
        require(revision == expected_revision, 'embedding_model_file_changed')
        engine = EmbeddingEngine(config)
        engine.initialize()  # Actual GGUF path; does not download missing files.
        stat = config.model_path.stat()
        require(before == (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns), 'embedding_model_file_changed')
        contract = ModelContract.from_config(config, input_tokens=EMBEDDING_INPUT_TOKENS, model_revision=revision)
        require(engine.is_loaded and engine.dim == contract.dimension, 'embedding_model_dimension_changed')
        connection.send(('ready', contract.key))
        while True:
            request = connection.recv()
            if request == ('close',):
                break
            require(isinstance(request, tuple) and len(request) == 3 and request[0] in ('query', 'document'), 'invalid_embedding_request')
            kind, ident, text = request
            require(type(ident) is int and isinstance(text, str) and 0 < len(text) <= 32768, 'invalid_embedding_request')
            values = engine.encode_query(text) if kind == 'query' else engine.encode_document(text)
            result = vector(values, contract)
            connection.send(('embedding', ident, result.values))
    except (EOFError, BrokenPipeError):
        pass
    except BaseException as exc:
        try:
            code = exc.code if isinstance(exc, VectorError) else 'embedding_engine_failed'
            connection.send(('error', code, type(exc).__name__))
        except (OSError, EOFError):
            pass
    finally:
        if engine is not None:
            engine.close()
        connection.close()


class ManagedEmbeddingProvider:
    """One real encoder, serialized requests, bounded native-call termination.

    The caller explicitly enables/initializes this owner and must close it.
    No remote status is treated as proof of a local filename/model revision.
    A timeout retires the process; it never silently restarts or changes models.
    """
    def __init__(self, config, *, startup_timeout=30, encode_timeout=10, cpu_offset=0):
        require(isinstance(config, Config), 'trusted_embedding_config_required')
        require(not config.embedding_http_url, 'unattested_remote_embedding_model')
        require(type(startup_timeout) in (int, float) and 0 < startup_timeout <= 120
                and type(encode_timeout) in (int, float) and 0 < encode_timeout <= 30, 'invalid_embedding_timeout')
        require(type(cpu_offset) is int and 0 <= cpu_offset <= 6, 'invalid_embedding_cpu_offset')
        self._config = deepcopy(config)
        self._config.apply_environment = False
        self._config.context_reranker_model_dir = ''
        self.startup_timeout, self.encode_timeout = float(startup_timeout), float(encode_timeout)
        self._cpu_offset = cpu_offset
        self._lock, self._state_lock = threading.Lock(), threading.Lock()
        self._close_lock = threading.Lock()
        self._process = self._pipe = None
        self._loaded = self._closed = False
        self._contract = None
        self._serial = 0

    @property
    def config(self):
        return deepcopy(self._config)

    @property
    def contract(self):
        require(self.is_loaded and self._contract is not None, 'embedding_provider_unavailable')
        return self._contract

    @property
    def model_revision(self):
        return None if self._contract is None else self._contract.model_revision

    @property
    def is_loaded(self):
        with self._state_lock:
            return self._loaded and not self._closed and self._process is not None and self._process.is_alive()

    def initialize(self):
        require(self._lock.acquire(blocking=False), 'embedding_provider_busy')
        try:
            require(not self._closed, 'embedding_provider_closed')
            if self.is_loaded:
                return self.contract
            deadline = time.monotonic() + self.startup_timeout
            revision, _ = _revision(self._config.model_path, deadline)
            self._contract = ModelContract.from_config(self._config, input_tokens=EMBEDDING_INPUT_TOKENS, model_revision=revision)
            context = multiprocessing.get_context('spawn')
            parent, child = context.Pipe()
            process = context.Process(target=_child, args=(child, deepcopy(self._config), revision,
                                      max(0, deadline - time.monotonic()), self._cpu_offset),
                                      name='evolvmem-owned-embedding', daemon=True)
            with self._state_lock:
                require(not self._closed, 'embedding_provider_closed')
                self._pipe, self._process = parent, process
                try:
                    process.start()
                finally:
                    child.close()
            require(parent.poll(max(0, deadline - time.monotonic())), 'embedding_initialize_timeout')
            response = parent.recv()
            require(response == ('ready', self._contract.key), 'embedding_initialize_failed')
            with self._state_lock:
                require(not self._closed, 'embedding_provider_closed')
                self._loaded = True
            return self.contract
        except BaseException:
            self.close()
            raise
        finally:
            self._lock.release()

    def _exchange(self, request, timeout):
        # poll() alone cannot bound a full pipe's send or a partial recv frame.
        # The owned peer is terminated on timeout, releasing both IPC ends.
        pipe, complete, output = self._pipe, threading.Event(), []
        def transfer():
            try:
                pipe.send(request)
                output.append((True, pipe.recv()))
            except BaseException as exc:
                output.append((False, type(exc).__name__))
            finally:
                complete.set()
        thread = threading.Thread(target=transfer, name='evolvmem-embedding-ipc', daemon=True)
        thread.start()
        if not complete.wait(timeout):
            self.close()
            thread.join(timeout=1)
            require(not thread.is_alive(), 'embedding_ipc_shutdown_incomplete')
            raise VectorError('embedding_encode_timeout')
        thread.join(timeout=1)
        require(not thread.is_alive() and len(output) == 1 and output[0][0], 'embedding_encode_failed')
        return output[0][1]

    def _encode(self, kind, text, deadline=None):
        require(isinstance(text, str) and 0 < len(text) <= 32768, 'invalid_embedding_input')
        require(deadline is None or (type(deadline) in (int, float) and math.isfinite(deadline)),
                'invalid_embedding_deadline')
        until = time.monotonic() + self.encode_timeout
        if deadline is not None:
            until = min(until, deadline)
        require(until > time.monotonic(), 'embedding_request_deadline_exceeded')
        require(self._lock.acquire(blocking=False), 'embedding_provider_busy')
        started = False
        try:
            require(self.is_loaded, 'embedding_provider_unavailable')
            self._serial += 1
            ident = self._serial
            remaining = until - time.monotonic()
            require(remaining > 0, 'embedding_request_deadline_exceeded')
            started = True
            reply = self._exchange((kind, ident, text), remaining)
            require(isinstance(reply, tuple) and len(reply) == 3 and reply[:2] == ('embedding', ident), 'embedding_encode_failed')
            require(self.is_loaded, 'embedding_provider_closed')
            return vector(reply[2], self._contract).values
        except BaseException:
            # Expiration before IPC has no native work to retire. In particular
            # an expired pool waiter must never terminate another active call.
            if started:
                self.close()
            raise
        finally:
            self._lock.release()

    def encode_query(self, text, *, deadline=None):
        return self._encode('query', text, deadline)

    def encode_document(self, text, *, deadline=None):
        return self._encode('document', text, deadline)

    def close(self):
        # A second closer must observe completed child termination, rather than
        # treating detached handles as evidence that the first closer finished.
        with self._close_lock:
            with self._state_lock:
                self._closed = True
                self._loaded = False
                process, pipe = self._process, self._pipe
            if process is not None:
                if process.is_alive():
                    # Do not wait indefinitely for a native model call to return.
                    process.terminate()
                    process.join(timeout=2)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=1)
                require(not process.is_alive(), 'embedding_shutdown_incomplete')
                with self._state_lock:
                    self._process = None
                process.close()
            if pipe is not None:
                pipe.close()
                with self._state_lock:
                    self._pipe = None
        return {'stopped': True}


class ManagedEmbeddingPool:
    """Four attested owned encoders, with bounded FIFO request admission.

    Only trusted startup composition selects this capacity. There is no caller
    model/routing/CPU selection and no request executor or background retry.
    Four existing-model mappings budget about 1.2 GiB nominal model RSS plus
    Python/native overhead; the 3 GiB Host fixture still needs measured load QA.
    At most four native calls and 32 waiting callers are admitted. Waiting uses
    the caller's thread and the original absolute deadline, with no extra task.
    A started encoder failure retires the whole pool; waiting expiry only exits
    that caller. Every worker reads the same existing artifact, never a copy.
    """
    def __init__(self, config, *, startup_timeout=30, encode_timeout=10):
        self._workers = tuple(ManagedEmbeddingProvider(config, startup_timeout=startup_timeout,
            encode_timeout=encode_timeout, cpu_offset=index * 2) for index in range(4))
        self._config = self._workers[0].config
        self.startup_timeout, self.encode_timeout = float(startup_timeout), float(encode_timeout)
        self._condition = threading.Condition()
        self._initialize_lock, self._close_lock = threading.Lock(), threading.Lock()
        self._available, self._waiters = deque(), deque()
        self._loaded = self._closed = self._started = False
        self._contract = None

    @property
    def config(self):
        return deepcopy(self._config)

    @property
    def contract(self):
        try:
            with self._condition:
                require(self.is_loaded and self._contract is not None, 'embedding_provider_unavailable')
                return self._contract
        except VectorError:
            self._retire_unhealthy()
            raise

    @property
    def model_revision(self):
        with self._condition:
            return None if self._contract is None else self._contract.model_revision

    @property
    def is_loaded(self):
        with self._condition:
            return self._loaded and not self._closed and all(worker.is_loaded for worker in self._workers)

    def _retire_unhealthy(self):
        # Health getters remain read-only. Serving paths retire a formerly
        # loaded pool whose peer died, after releasing the scheduler condition;
        # shutdown must not hold the lock needed by returning/queued callers.
        with self._condition:
            unhealthy = self._loaded and not self._closed and not all(
                worker.is_loaded for worker in self._workers)
        if unhealthy:
            self.close()

    def _check_worker(self, worker):
        require(worker.contract == self._contract, 'embedding_contract_changed')
        self._contract.check_provider(worker, input_tokens=EMBEDDING_INPUT_TOKENS)
        require(worker.config.model_path == self._config.model_path, 'embedding_model_file_changed')

    def initialize(self):
        require(self._initialize_lock.acquire(blocking=False), 'embedding_provider_busy')
        try:
            with self._condition:
                require(not self._closed, 'embedding_provider_closed')
                if self.is_loaded:
                    return self._contract
                require(not self._started, 'embedding_provider_unavailable')
                self._started = True
            deadline = time.monotonic() + self.startup_timeout
            for worker in self._workers:
                with self._condition:
                    require(not self._closed, 'embedding_provider_closed')
                remaining = deadline - time.monotonic()
                require(remaining > 0, 'embedding_initialize_timeout')
                worker.startup_timeout = remaining
                worker.initialize()
                require(time.monotonic() < deadline, 'embedding_initialize_timeout')
                if self._contract is None:
                    self._contract = worker.contract
                self._check_worker(worker)
            with self._condition:
                require(not self._closed, 'embedding_provider_closed')
                self._available.extend(self._workers)
                self._loaded = True
                self._condition.notify_all()
            return self.contract
        except BaseException:
            self.close()
            raise
        finally:
            self._initialize_lock.release()

    def _take(self, until):
        try:
            with self._condition:
                require(self.is_loaded, 'embedding_provider_unavailable')
                require(until > time.monotonic(), 'embedding_request_deadline_exceeded')
                if self._available and not self._waiters:
                    return self._available.popleft()
                require(len(self._waiters) < 32, 'embedding_provider_busy')
                ticket = object()
                self._waiters.append(ticket)
                self._condition.notify_all()
                try:
                    while True:
                        require(self.is_loaded, 'embedding_provider_unavailable')
                        remaining = until - time.monotonic()
                        require(remaining > 0, 'embedding_request_deadline_exceeded')
                        if self._available and self._waiters[0] is ticket:
                            self._waiters.popleft()
                            return self._available.popleft()
                        # A child exiting cannot notify this in-process
                        # condition. Bound health detection without a monitor
                        # thread or resetting the absolute caller deadline.
                        self._condition.wait(min(.1, remaining))
                finally:
                    if ticket in self._waiters:
                        self._waiters.remove(ticket)
                    self._condition.notify_all()
        except VectorError:
            self._retire_unhealthy()
            raise

    def _encode(self, kind, text, deadline):
        require(isinstance(text, str) and 0 < len(text) <= 32768, 'invalid_embedding_input')
        require(deadline is None or (type(deadline) in (int, float) and math.isfinite(deadline)),
                'invalid_embedding_deadline')
        until = time.monotonic() + self.encode_timeout
        if deadline is not None:
            until = min(until, deadline)
        # Admission/queue errors happen before owning a worker and must not
        # retire models that are serving other callers.
        worker = self._take(until)
        try:
            require(time.monotonic() < until, 'embedding_request_deadline_exceeded')
            self._check_worker(worker)
            method = worker.encode_query if kind == 'query' else worker.encode_document
            result = method(text, deadline=until)
            self._check_worker(worker)
            with self._condition:
                require(self.is_loaded, 'embedding_provider_unavailable')
            return vector(result, self._contract).values
        except BaseException as exc:
            # Native providers leave themselves alive only for expiry before
            # exchange. Every started/unknown error fails the entire pool shut.
            if not (isinstance(exc, VectorError)
                    and exc.code == 'embedding_request_deadline_exceeded' and worker.is_loaded):
                self.close()
            raise
        finally:
            with self._condition:
                if self._loaded and not self._closed:
                    self._available.append(worker)
                self._condition.notify_all()

    def encode_query(self, text, *, deadline=None):
        return self._encode('query', text, deadline)

    def encode_document(self, text, *, deadline=None):
        return self._encode('document', text, deadline)

    def close(self):
        with self._condition:
            self._closed = True
            self._loaded = False
            self._available.clear()
            self._condition.notify_all()
        with self._close_lock:
            failed = False
            for worker in self._workers:
                try:
                    worker.close()
                except BaseException:
                    failed = True
            require(not failed, 'embedding_shutdown_incomplete')
        return {'stopped': True}

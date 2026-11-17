"""One resident GGUF worker per vector space, with killable deadline-bound IPC."""
from __future__ import annotations
import hashlib
import multiprocessing
import os
from pathlib import Path
import threading
import time
from .embedding_client import EmbeddingFailure, ModelSpec, _require, validate_vector


def _attest(spec):
    path = Path(spec.model_path)
    before = path.stat()
    _require(path.is_file() and 0 < before.st_size <= 2 * 1024**3, "embedding_model_invalid")
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024**2):
            sha.update(chunk)
    after = path.stat()
    identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
    _require(identity(before) == identity(after) and sha.hexdigest() == spec.model_sha256,
             "embedding_model_mismatch")
    return identity(after)


def _child(pipe, spec: ModelSpec):
    model = None
    try:
        for name in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS",
                     "NUMEXPR_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
            os.environ[name] = "1"
        identity = _attest(spec)
        from llama_cpp import Llama, LLAMA_POOLING_TYPE_CLS, LLAMA_POOLING_TYPE_MEAN
        pooling = LLAMA_POOLING_TYPE_CLS if spec.pooling == "cls" else LLAMA_POOLING_TYPE_MEAN
        model = Llama(model_path=spec.model_path, embedding=True, n_ctx=spec.input_tokens,
            n_batch=spec.input_tokens, n_ubatch=spec.input_tokens, n_threads=spec.n_threads,
            n_threads_batch=spec.n_threads, pooling_type=pooling, verbose=False)
        _require(model.n_embd() == spec.dimension and model.pooling_type() == pooling,
                 "embedding_model_contract_mismatch")
        def encode(kind, text):
            prefix = spec.query_prefix if kind == "query" else spec.document_prefix
            original = model.n_batch
            try:
                # Llama.embed tokenizes prefix and input together and slices this
                # token budget before decoding; batch storage remains large enough.
                model.n_batch = spec.query_tokens if kind == "query" else spec.input_tokens
                values = model.embed(prefix + text, normalize=True, truncate=True)
                return validate_vector(values, spec.dimension)
            finally:
                model.n_batch = original
        for _ in range(5):
            encode("query", "synthetic query warmup")
        encode("document", "synthetic document dimension probe")
        current = Path(spec.model_path).stat()
        _require(identity == (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns),
                 "embedding_model_mismatch")
        pipe.send(("ready", spec.contract_id))
        while True:
            request = pipe.recv()
            _require(isinstance(request, tuple) and len(request) == 4, "invalid_embedding_request")
            kind, serial, text, deadline = request
            _require(kind in ("query", "document") and type(serial) is int
                and isinstance(text, str) and 0 < len(text) <= 32768, "invalid_embedding_request")
            _require(time.monotonic() < deadline, "embedding_deadline_exceeded")
            values = encode(kind, text)
            _require(time.monotonic() < deadline, "embedding_deadline_exceeded")
            pipe.send(("vector", serial, values))
    except (EOFError, BrokenPipeError):
        pass
    except BaseException as exc:
        code = exc.code if isinstance(exc, EmbeddingFailure) else "embedding_worker_failed"
        try: pipe.send(("error", code))
        except (OSError, EOFError): pass
    finally:
        if model is not None:
            model.close()
        pipe.close()


class ModelWorker:
    def __init__(self, spec, *, startup_timeout=60):
        self.spec, self.startup_timeout = spec, startup_timeout
        self.lock, self._lifecycle = threading.Lock(), threading.RLock()
        self.process = self.pipe = None
        self._loaded, self._serial = False, 0

    @property
    def is_loaded(self):
        with self._lifecycle:
            return bool(self._loaded and self.process is not None and self.process.is_alive())

    def initialize(self):
        _require(self.lock.acquire(blocking=False), "embedding_busy")
        try:
            if self.is_loaded:
                return
            context = multiprocessing.get_context("spawn")
            parent, child = context.Pipe()
            process = context.Process(target=_child, args=(child, self.spec),
                name=f"evolvmem-embedding-{self.spec.dimension}", daemon=True)
            with self._lifecycle:
                self.pipe, self.process = parent, process
                try: process.start()
                finally: child.close()
            _require(parent.poll(self.startup_timeout), "embedding_startup_timeout")
            response = parent.recv()
            _require(response == ("ready", self.spec.contract_id), "embedding_startup_failed")
            with self._lifecycle:
                self._loaded = True
        except BaseException:
            self.close()
            raise
        finally:
            self.lock.release()

    def encode(self, kind, text, *, deadline):
        _require(type(deadline) in (float, int) and time.monotonic() < deadline,
                 "embedding_deadline_exceeded")
        _require(self.lock.acquire(blocking=False), "embedding_busy")
        started = False
        try:
            _require(self.is_loaded, "embedding_not_ready")
            self._serial += 1
            serial, pipe = self._serial, self.pipe
            done, result = threading.Event(), []
            def transfer():
                try:
                    pipe.send((kind, serial, text, deadline))
                    result.append(pipe.recv())
                except (OSError, EOFError, ValueError):
                    result.append(("error", "embedding_worker_failed"))
                finally:
                    done.set()
            started = True
            thread = threading.Thread(target=transfer, name="embedding-ipc", daemon=True)
            thread.start()
            if not done.wait(max(0, deadline - time.monotonic())):
                self.close(); thread.join(.1)
                raise EmbeddingFailure("embedding_deadline_exceeded")
            _require(time.monotonic() < deadline, "embedding_deadline_exceeded")
            reply = result[0]
            _require(isinstance(reply, tuple) and len(reply) == 3 and reply[:2] == ("vector", serial),
                     "embedding_worker_failed")
            return validate_vector(reply[2], self.spec.dimension)
        except EmbeddingFailure:
            if started:
                self.close()
            raise
        finally:
            self.lock.release()

    def close(self):
        with self._lifecycle:
            self._loaded = False
            process, pipe = self.process, self.pipe
            if process is not None:
                if process.is_alive():
                    process.terminate(); process.join(.15)
                if process.is_alive():
                    process.kill(); process.join(.15)
                _require(not process.is_alive(), "embedding_shutdown_failed")
                self.process = None
                process.close()
            if pipe is not None:
                pipe.close(); self.pipe = None

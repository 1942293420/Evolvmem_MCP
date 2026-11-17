"""Loopback-only, resident dual-model embedding service. No request-time loading."""
from __future__ import annotations
import argparse
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import signal
import threading
import time
from .embedding_client import EmbeddingFailure, RuntimeConfig, _require, validate_vector
from ._embedding_worker import ModelWorker


class EmbeddingRuntime:
    def __init__(self, config, *, worker_factory=ModelWorker):
        self.config, self._factory = config, worker_factory
        self.legacy = self._worker(config.legacy)
        self.retrieval = self._worker(config.retrieval)
        self._state, self._initialize = threading.RLock(), threading.Lock()
        self._ready = self._closed = False
        self._stop, self._recovery = threading.Event(), None

    def _worker(self, spec):
        return self._factory(spec, startup_timeout=self.config.startup_timeout_seconds)

    @property
    def ready(self):
        with self._state:
            return bool(self._ready and not self._closed
                        and self.legacy.is_loaded and self.retrieval.is_loaded)

    def _prepare(self):
        if not self._initialize.acquire(blocking=False):
            return
        try:
            for name, spec in (("legacy", self.config.legacy), ("retrieval", self.config.retrieval)):
                with self._state:
                    if self._closed:
                        return
                    worker = getattr(self, name)
                    if not worker.is_loaded:
                        worker.close()
                        worker = self._worker(spec)
                        setattr(self, name, worker)
                worker.initialize()
                with self._state:
                    if self._closed:
                        worker.close()
                        return
            with self._state:
                self._ready = not self._closed and self.legacy.is_loaded and self.retrieval.is_loaded
        except BaseException:
            with self._state:
                self._ready = False
            raise
        finally:
            self._initialize.release()

    def start(self, *, recover=True):
        try:
            self._prepare()
        finally:
            if recover and not self._closed and self._recovery is None:
                self._recovery = threading.Thread(target=self._recover, name="embedding-recovery", daemon=True)
                self._recovery.start()

    def _recover(self):
        while not self._stop.wait(self.config.recovery_interval_seconds):
            if not self.ready:
                try: self._prepare()
                except Exception: pass  # status stays not_ready; no texts or exception contents logged

    def encode(self, request):
        _require(isinstance(request, dict)
            and request.get("retrieval_contract_id") == self.config.retrieval.contract_id
            and request.get("legacy_contract_id") == self.config.legacy.contract_id,
            "embedding_contract_mismatch")
        kind, text, deadline = request.get("kind"), request.get("text"), request.get("deadline")
        _require(kind in ("query", "note") and isinstance(text, str) and 0 < len(text) <= 32768,
                 "invalid_embedding_input")
        _require(type(deadline) in (int, float) and time.monotonic() < deadline
            <= time.monotonic() + self.config.request_timeout_seconds + .1,
            "embedding_deadline_exceeded")
        _require(self.ready, "embedding_not_ready")
        try:
            if kind == "query":
                vector = self.retrieval.encode("query", text, deadline=deadline)
                return dict(vector=validate_vector(vector, 1024), contract_id=self.config.retrieval.contract_id)
            legacy = self.legacy.encode("document", text, deadline=deadline)
            retrieval = self.retrieval.encode("document", text, deadline=deadline)
            return dict(legacy_embedding=validate_vector(legacy, 768),
                retrieval_embedding=validate_vector(retrieval, 1024),
                contract_id=self.config.retrieval.contract_id,
                legacy_contract_id=self.config.legacy.contract_id,
                content_sha256=hashlib.sha256(text.encode()).hexdigest())
        except EmbeddingFailure as exc:
            if exc.code != "embedding_busy":
                with self._state:
                    self._ready = False
            raise

    def close(self):
        with self._state:
            self._closed, self._ready = True, False
            self._stop.set()
            workers = (self.legacy, self.retrieval)
        for worker in workers:
            worker.close()
        if self._recovery is not None and self._recovery is not threading.current_thread():
            self._recovery.join(.5)


class _LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def make_server(runtime, *, port=9380):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, status, value):
            data = json.dumps(value, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            try: self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError): pass
            self.close_connection = True

        def do_GET(self):
            if self.path != "/status":
                self._reply(404, {"error": "not_found"}); return
            self._reply(200, dict(ready=runtime.ready,
                legacy_contract_id=runtime.config.legacy.contract_id,
                retrieval_contract_id=runtime.config.retrieval.contract_id))

        def do_POST(self):
            if self.path != "/encode":
                self._reply(404, {"error": "not_found"}); return
            try:
                self.connection.settimeout(2)
                lengths = self.headers.get_all("Content-Length", [])
                _require(len(lengths) == 1 and not self.headers.get("Transfer-Encoding"),
                         "invalid_embedding_input")
                size = int(lengths[0])
                _require(0 < size <= 65536, "invalid_embedding_input")
                raw = self.rfile.read(size)
                _require(len(raw) == size, "invalid_embedding_input")
                request = json.loads(raw)
                result = runtime.encode(request)
                self._reply(200, result)
            except EmbeddingFailure as exc:
                self._reply(503 if exc.code in ("embedding_busy", "embedding_not_ready",
                    "embedding_deadline_exceeded") else 400, {"error": exc.code})
            except (ValueError, OSError):
                self._reply(400, {"error": "invalid_embedding_input"})
            except Exception:
                self._reply(503, {"error": "embedding_not_ready"})
    return _LoopbackServer(("127.0.0.1", port), Handler)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args(argv)
    config = RuntimeConfig.from_file(args.config)
    _require(config.port == 9380, "invalid_embedding_config")
    runtime = EmbeddingRuntime(config)
    server = None
    try:
        runtime.start()
        _require(runtime.ready, "embedding_not_ready")
        server = make_server(runtime, port=9380)
        def stop(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, stop)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if server is not None:
            server.server_close()
        runtime.close()


if __name__ == "__main__":
    main()

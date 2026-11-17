"""Contract, HTTP boundary and owned process deadline regression tests."""
import hashlib
import importlib
import json
import math
from pathlib import Path
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import pytest


def modules():
    return (importlib.import_module("evolvmem.experience_v2.embedding_client"),
            importlib.import_module("evolvmem.experience_v2.embedding_service"),
            importlib.import_module("evolvmem.experience_v2._embedding_worker"))


def raw_config(port=9380):
    def model(dim):
        return dict(model_path=f"/tmp/model-{dim}.gguf", model_sha256="a" * 64,
                    dimension=dim, query_prefix="query: ", document_prefix="",
                    pooling="cls", input_tokens=512, n_threads=2)
    return dict(url=f"http://127.0.0.1:{port}", legacy=model(768),
                retrieval=model(1024), request_timeout_seconds=2)


def test_note_hash_preserves_exact_values():
    c, _, _ = modules()
    note = dict(category="c ", problem=" p", solution="s\n", reusable_experience="r ")
    expected = "c \n p\ns\n\nr "
    assert c.note_text(note) == expected
    assert c.note_content_sha256(note) == hashlib.sha256(expected.encode()).hexdigest()


def test_unconfigured_client_has_no_side_effects(monkeypatch):
    c, _, _ = modules()
    monkeypatch.delenv("EVOLVMEM_EXPERIENCE_EMBEDDING_CONFIG", raising=False)
    assert c.EmbeddingClient.from_environment() is None


def test_configured_client_parses_without_model_or_network_access(tmp_path, monkeypatch):
    c, _, _ = modules()
    cfg = tmp_path / "embedding.json"
    cfg.write_text(json.dumps(raw_config()), encoding="utf-8")
    monkeypatch.setenv("EVOLVMEM_EXPERIENCE_EMBEDDING_CONFIG", str(cfg))
    client = c.EmbeddingClient.from_environment()
    assert client.retrieval_contract_id != client.legacy_contract_id


@pytest.mark.parametrize("url", ["http://example.com:9380", "https://127.0.0.1:9380",
    "http://localhost:9380", "http://u:p@127.0.0.1:9380", "http://127.0.0.1:9380/x",
    "http://127.0.0.1:9380?secret=x"])
def test_only_literal_loopback_http_endpoint_allowed(url):
    c, _, _ = modules()
    cfg = raw_config(); cfg["url"] = url
    with pytest.raises(c.EmbeddingFailure, match="invalid_embedding_config"):
        c.EmbeddingClient(c.RuntimeConfig.from_dict(cfg))


@pytest.mark.parametrize("field,value", [("model_sha256", "bad"), ("pooling", "unknown"),
                                       ("input_tokens", 0), ("dimension", 768)])
def test_retrieval_contract_rejects_invalid_model_specs(field, value):
    c, _, _ = modules()
    cfg = raw_config(); cfg["retrieval"][field] = value
    with pytest.raises(c.EmbeddingFailure, match="invalid_embedding_config"):
        c.RuntimeConfig.from_dict(cfg)


def test_contract_binds_model_prefix_pooling_and_token_budget():
    c, _, _ = modules()
    base = c.RuntimeConfig.from_dict(raw_config()).retrieval.contract_id
    for field, value in [("model_sha256", "b" * 64), ("query_prefix", "new "),
                         ("document_prefix", "new "), ("pooling", "mean"), ("input_tokens", 256)]:
        cfg = raw_config(); cfg["retrieval"][field] = value
        assert c.RuntimeConfig.from_dict(cfg).retrieval.contract_id != base


class FakeWorker:
    def __init__(self, spec, **kwargs):
        self.spec, self.is_loaded, self.closed = spec, False, False
        self.calls = []
    def initialize(self):
        self.is_loaded = True
    def encode(self, kind, text, *, deadline):
        self.calls.append((kind, text, deadline))
        return [1.0] + [0.0] * (self.spec.dimension - 1)
    def close(self):
        self.closed, self.is_loaded = True, False


@pytest.fixture
def live_server():
    c, s, _ = modules()
    cfg = c.RuntimeConfig.from_dict(raw_config())
    runtime = s.EmbeddingRuntime(cfg, worker_factory=FakeWorker)
    runtime.start(recover=False)
    server = s.make_server(runtime, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    client_cfg = raw_config(server.server_port)
    try:
        yield c.EmbeddingClient(c.RuntimeConfig.from_dict(client_cfg)), runtime
    finally:
        server.shutdown(); server.server_close(); runtime.close(); thread.join(2)


def test_http_roundtrip_projects_both_spaces(live_server):
    client, runtime = live_server
    note = dict(category="c", problem="p", solution="s", reusable_experience="r")
    out = client.encode_note(note, deadline=time.monotonic() + 1)
    assert len(out.legacy_embedding) == 768
    assert len(out.retrieval_embedding) == 1024
    assert out.contract_id == client.retrieval_contract_id
    c, _, _ = modules()
    assert out.content_sha256 == c.note_content_sha256(note)
    assert len(client.encode_query("q", deadline=time.monotonic() + 1)) == 1024


def test_expired_deadline_never_encodes(live_server):
    client, runtime = live_server
    c, _, _ = modules()
    with pytest.raises(c.EmbeddingFailure, match="embedding_deadline_exceeded"):
        client.encode_query("q", deadline=time.monotonic() - 1)
    assert not runtime.retrieval.calls


@pytest.mark.parametrize("bad_vector", [[0.0] * 1024, [math.nan] * 1024, [1.0] * 768])
def test_service_rejects_bad_worker_vectors(live_server, bad_vector):
    client, runtime = live_server
    c, _, _ = modules()
    runtime.retrieval.encode = lambda *a, **k: bad_vector
    with pytest.raises(c.EmbeddingFailure):
        client.encode_query("q", deadline=time.monotonic() + 1)


def test_bad_contract_is_rejected_before_worker_call(live_server):
    client, runtime = live_server
    c, _, _ = modules()
    cfg = raw_config(client.config.port)
    cfg["retrieval"]["model_sha256"] = "b" * 64
    wrong = c.EmbeddingClient(c.RuntimeConfig.from_dict(cfg))
    with pytest.raises(c.EmbeddingFailure, match="embedding_contract_mismatch"):
        wrong.encode_query("q", deadline=time.monotonic() + 1)
    assert not runtime.retrieval.calls


def test_not_ready_fails_without_lazy_loading(live_server):
    client, runtime = live_server
    c, _, _ = modules()
    runtime.retrieval.close()
    with pytest.raises(c.EmbeddingFailure, match="embedding_not_ready"):
        client.encode_query("q", deadline=time.monotonic() + 1)
    assert not runtime.retrieval.is_loaded


def test_busy_does_not_retire_healthy_runtime(live_server):
    client, runtime = live_server
    c, _, _ = modules()
    def busy(*args, **kwargs):
        raise c.EmbeddingFailure("embedding_busy")
    runtime.retrieval.encode = busy
    with pytest.raises(c.EmbeddingFailure, match="embedding_busy"):
        client.encode_query("q", deadline=time.monotonic() + 1)
    assert runtime.ready


def synthetic_child(pipe, spec):
    pipe.send(("ready", spec.contract_id))
    try:
        while True:
            kind, serial, text, deadline = pipe.recv()
            if text == "slow": time.sleep(2)
            pipe.send(("vector", serial, [1.0] + [0.0] * (spec.dimension - 1)))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        pipe.close()


def test_owned_worker_timeout_retires_native_process(monkeypatch):
    c, _, w = modules()
    monkeypatch.setattr(w, "_child", synthetic_child)
    spec = c.RuntimeConfig.from_dict(raw_config()).retrieval
    worker = w.ModelWorker(spec, startup_timeout=5)
    try:
        worker.initialize()
        start = time.monotonic()
        with pytest.raises(c.EmbeddingFailure, match="embedding_deadline_exceeded"):
            worker.encode("query", "slow", deadline=start + .1)
        assert time.monotonic() - start < .8
        assert not worker.is_loaded
        assert worker.process is None or not worker.process.is_alive()
    finally:
        worker.close()


def test_owned_worker_busy_fails_immediately(monkeypatch):
    c, _, w = modules()
    monkeypatch.setattr(w, "_child", synthetic_child)
    worker = w.ModelWorker(c.RuntimeConfig.from_dict(raw_config()).retrieval, startup_timeout=5)
    worker.initialize(); worker.lock.acquire()
    try:
        started = time.monotonic()
        with pytest.raises(c.EmbeddingFailure, match="embedding_busy"):
            worker.encode("query", "q", deadline=started + 1)
        assert time.monotonic() - started < .05
        assert worker.is_loaded
    finally:
        worker.lock.release(); worker.close()



@pytest.fixture
def response_server():
    servers = []
    def launch(payload=None, drip=False):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                raw = json.dumps(payload).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                try:
                    if drip:
                        for b in raw:
                            self.wfile.write(bytes([b])); self.wfile.flush(); time.sleep(.03)
                    else: self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler); server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        servers.append((server, thread))
        c, _, _ = modules()
        return c.EmbeddingClient(c.RuntimeConfig.from_dict(raw_config(server.server_port)))
    yield launch
    for server, thread in servers:
        server.shutdown(); server.server_close(); thread.join(2)


def test_client_rejects_server_contract_substitution(response_server):
    c, _, _ = modules()
    client = response_server(dict(contract_id="different", vector=[1.0] * 1024))
    with pytest.raises(c.EmbeddingFailure, match="embedding_contract_mismatch"):
        client.encode_query("q", deadline=time.monotonic() + 1)


def test_client_rejects_zero_vector_even_from_matching_contract(response_server):
    c, _, _ = modules()
    contract = c.RuntimeConfig.from_dict(raw_config()).retrieval.contract_id
    client = response_server(dict(contract_id=contract, vector=[0.0] * 1024))
    with pytest.raises(c.EmbeddingFailure, match="invalid_embedding_vector"):
        client.encode_query("q", deadline=time.monotonic() + 1)


def test_http_drip_cannot_reset_absolute_deadline(response_server):
    c, _, _ = modules()
    client = response_server(dict(contract_id="padding" * 200), drip=True)
    start = time.monotonic()
    with pytest.raises(c.EmbeddingFailure):
        client.encode_query("q", deadline=start + .12)
    assert time.monotonic() - start < .5


def test_startup_failure_does_not_report_ready():
    c, s, _ = modules()
    class FailingWorker(FakeWorker):
        def initialize(self): raise c.EmbeddingFailure("embedding_model_invalid")
    runtime = s.EmbeddingRuntime(c.RuntimeConfig.from_dict(raw_config()), worker_factory=FailingWorker)
    try:
        with pytest.raises(c.EmbeddingFailure): runtime.start(recover=False)
        assert not runtime.ready
    finally: runtime.close()



@pytest.mark.parametrize("bad", [[True] * 1024, [10**400] * 1024, [math.inf] * 1024])
def test_vector_validation_has_stable_failure_for_nonfinite_or_non_numeric(bad):
    c, _, _ = modules()
    with pytest.raises(c.EmbeddingFailure, match="invalid_embedding_vector"):
        c.validate_vector(bad, 1024)


def test_background_recovery_uses_original_contract_without_request_loading():
    c, s, _ = modules()
    raw = raw_config(); raw["recovery_interval_seconds"] = .03
    runtime = s.EmbeddingRuntime(c.RuntimeConfig.from_dict(raw), worker_factory=FakeWorker)
    runtime.start()
    old = runtime.retrieval
    try:
        old.close()
        assert not runtime.ready
        end = time.monotonic() + 1
        while not runtime.ready and time.monotonic() < end:
            time.sleep(.01)
        assert runtime.ready
        assert runtime.retrieval is not old
        assert runtime.retrieval.spec.contract_id == old.spec.contract_id
    finally:
        runtime.close()


def test_query_token_budget_is_part_of_contract():
    c, _, _ = modules()
    first = c.RuntimeConfig.from_dict(raw_config()).retrieval.contract_id
    raw = raw_config(); raw["retrieval"]["query_tokens"] = 64
    assert c.RuntimeConfig.from_dict(raw).retrieval.contract_id != first



def test_real_dual_model_runtime_when_explicitly_configured():
    import os
    from dataclasses import replace
    path = os.environ.get("EVOLVMEM_EMBEDDING_MODEL_TEST_CONFIG")
    if not path:
        pytest.skip("explicit local real-model test configuration required")
    c, s, _ = modules()
    cfg = c.RuntimeConfig.from_file(path)
    runtime = s.EmbeddingRuntime(cfg)
    server = None
    try:
        started = time.monotonic(); runtime.start(recover=False)
        startup_ms = (time.monotonic() - started) * 1000
        assert runtime.ready
        assert runtime.legacy.process.pid != runtime.retrieval.process.pid
        server = s.make_server(runtime, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        client = c.EmbeddingClient(replace(cfg, url=f"http://127.0.0.1:{server.server_port}"))
        samples = []
        for _ in range(20):
            started = time.monotonic()
            vector = client.encode_query("synthetic retrieval query", deadline=started + 2)
            samples.append((time.monotonic() - started) * 1000)
            assert len(vector) == 1024
        note = dict(category="synthetic", problem="test problem", solution="test solution",
                    reusable_experience="test reusable rule")
        started = time.monotonic()
        projection = client.encode_note(note, deadline=started + 2)
        note_ms = (time.monotonic() - started) * 1000
        assert projection.content_sha256 == c.note_content_sha256(note)
        assert len(projection.legacy_embedding) == 768
        assert len(projection.retrieval_embedding) == 1024
        assert sorted(samples)[18] < 900
        print(json.dumps(dict(startup_ms=round(startup_ms, 2),
            query_samples=20, query_p95_ms=round(sorted(samples)[18], 2),
            query_max_ms=round(max(samples), 2), dual_note_ms=round(note_ms, 2))))
    finally:
        if server is not None:
            server.shutdown(); server.server_close(); thread.join(2)
        runtime.close()

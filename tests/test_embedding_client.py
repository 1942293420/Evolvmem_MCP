"""Shared engine through real loopback HTTP; deterministic encoding boundary."""
import json
import threading
from dataclasses import replace
import pytest
from tests.test_lan_sharing import settings_for
from evolvmem.config import Config
from evolvmem.embedding import EmbeddingEngine
from evolvmem.lan_runtime import LanRuntime
from evolvmem.lan_server import make_http_server


class Model:
    def __init__(self):
        self.is_loaded = False
        self.calls = []
        self.initializations = 0
        self.dim = 768
    def initialize(self):
        self.initializations += 1
        self.is_loaded = True
    def close(self):
        self.is_loaded = False
    def encode_query(self, text):
        self.calls.append(('query', text))
        return [0.25] * self.dim
    def encode_document(self, text):
        self.calls.append(('document', text))
        return [0.5] * self.dim


@pytest.fixture
def embedding_http(tmp_path):
    model = Model()
    runtime = LanRuntime(replace(settings_for(tmp_path), embedding_enabled=True), embedding_engine=model)
    runtime.initialize()
    server = make_http_server(runtime, host='127.0.0.1', port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token = tmp_path / 'embedding-token'
    token.write_text('jiangli-token')
    token.chmod(0o600)
    config = Config(data_dir=tmp_path / 'client', apply_environment=False)
    config.embedding_http_url = f'http://127.0.0.1:{server.server_port}'
    config.embedding_http_token_file = str(token)
    yield config, model, runtime
    server.shutdown()
    thread.join(3)
    server.server_close()
    runtime.close()


def test_embedding_clients_share_one_model_without_llama_and_prefix_once(embedding_http, monkeypatch):
    config, model, runtime = embedding_http
    import sys
    monkeypatch.setitem(sys.modules, 'llama_cpp', None)
    clients = [EmbeddingEngine(config), EmbeddingEngine(config)]
    for client in clients:
        client.initialize()
        assert client.is_loaded and client.dim == 768
        assert client.encode_query('question') == [0.25] * 768
        assert client.encode_document('document') == [0.5] * 768
    assert model.calls == [('query', 'question'), ('document', 'document')] * 2
    assert model.initializations == 1
    for client in clients:
        client.close()
        assert not client.is_loaded
    assert model.is_loaded


def test_embedding_contract_shape_and_unavailable_fail_closed(embedding_http):
    config, model, runtime = embedding_http
    for changes in (dict(embedding_dim=3), dict(embedding_query_prefix='wrong: '), dict(embedding_doc_prefix='wrong: ')):
        client = EmbeddingEngine(replace(config, **changes))
        with pytest.raises(RuntimeError, match='embedding_contract_mismatch'):
            client.initialize()
        assert not client.is_loaded
    client = EmbeddingEngine(config)
    client.initialize()
    model.dim = 2
    with pytest.raises(RuntimeError):
        client.encode_query('dimension mismatch')
    model.dim = 768
    model.is_loaded = False
    with pytest.raises(RuntimeError, match='embedding_unavailable'):
        EmbeddingEngine(config).initialize()
    from evolvmem.lan_tools import LanTools
    adapter = LanTools(runtime)
    assert 'error' not in adapter.call_tool('kane', 'memory_add', dict(key='project:demo:fact:fts', value='available lexical fallback example', request_id='fts'))
    assert adapter.call_tool('kane', 'memory_search', dict(query='lexical'))['count'] == 1


def test_embedding_client_credentials_and_unavailable_are_redacted(tmp_path):
    config = Config(data_dir=tmp_path / 'client', apply_environment=False)
    config.embedding_http_url = 'http://127.0.0.1:1'
    config.embedding_http_token_file = str(tmp_path / 'credential')
    (tmp_path / 'credential').write_text('SUPER-SECRET-TOKEN')
    (tmp_path / 'credential').chmod(0o600)
    client = EmbeddingEngine(config)
    with pytest.raises(RuntimeError) as exc:
        client.initialize()
    assert 'SUPER-SECRET' not in str(exc.value)
    assert str(tmp_path) not in str(exc.value)
    assert not client.is_loaded


def test_existing_vector_instances_can_overwrite_each_others_cache(tmp_path):
    """Concrete pre-existing deployment hazard: two resident index writers."""
    import numpy as np
    from evolvmem.vector_index import VectorIndex
    config = Config(data_dir=tmp_path, apply_environment=False)
    first, second = VectorIndex(config), VectorIndex(config)
    first.initialize(3)
    first.add(1, np.array([1., 0., 0.]))
    first.save()
    second.initialize(3)
    first.add(2, np.array([0., 1., 0.]))
    first.save()
    second.add(3, np.array([0., 0., 1.]))
    second.save()
    reopened = VectorIndex(config)
    reopened.initialize(3)
    ids = {row['id'] for row in reopened.search(np.array([0., 1., 0.]), k=10)}
    assert ids == {1, 3}  # Owner's second resident writer lost ID 2's cached vector.
    for index in (first, second, reopened):
        index.close()


def test_native_extraction_process_and_lan_share_model_and_both_indexes(embedding_http):
    config, model, runtime = embedding_http
    from evolvmem.lan_tools import LanTools
    from tests.test_lan_sharing import add
    import subprocess
    import sys
    from pathlib import Path
    adapter = LanTools(runtime)
    import hashlib
    def distinct_document(text):
        offset = int(hashlib.sha256(text.encode()).hexdigest(), 16) % 768
        return [1.0 if i == offset else 0.0 for i in range(768)]
    model.encode_document = distinct_document
    original = add(adapter)
    config.data_dir = runtime.settings.owner_data_dir
    config.context_mode = 'primary'
    config.adapter = 'kimi'
    config.context_vectors_required = False
    config.save()
    script = '''
import json, sys
from evolvmem.config import Config
from evolvmem.mcp_server import MemoryMCPServer
from evolvmem.legacy_models import LegacyExtractionRequest, LegacyExtractionItem
sys.modules['llama_cpp'] = None
server = MemoryMCPServer(Config.from_file(data_dir=__import__('pathlib').Path(sys.argv[1]), apply_environment=False))
server.initialize()
server._init_done.set()
print('ready', flush=True)
sys.stdin.readline()
result = server.context_service.persist_legacy_extraction(LegacyExtractionRequest(summary=LegacyExtractionItem(key='SESSION_SUMMARY:shared-http', value='Native hook completed the shared model extraction verification'), source_session='temporary-hook-test'))
print(json.dumps({'persisted':result.persisted, 'context_id':result.summary.context_id, 'legacy_id':result.summary.legacy_id}), flush=True)
server.shutdown()
'''
    proc = subprocess.Popen([sys.executable, '-c', script, str(config.data_dir)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        import select
        assert select.select([proc.stdout], [], [], 10)[0], 'native startup timeout'
        assert proc.stdout.readline().strip() == 'ready'
        # The resident native process has loaded this same owner index before
        # the HTTP runtime inserts a second item into it.
        later = add(adapter, key='project:lan:fact:later', value='LAN inserted a second owner marker after native startup', rid='later')
        proc.stdin.write('go\n')
        proc.stdin.flush()
        out, err = proc.communicate(timeout=15)
        assert proc.returncode == 0, err
        native = json.loads(out)
        assert native['persisted'] == 1
        server = runtime.server_for('jiangli')
        context_ids = server.context_service.vector_index.ids()
        expected_context = sorted([original['context_id'], later['context_id'], native['context_id']])
        expected_legacy = sorted([original['id'], later['id'], native['legacy_id']])
        assert len(set(expected_context)) == 3
        assert context_ids == expected_context
        assert server.context_service._legacy_vector_index().ids() == expected_legacy
        from evolvmem.vector_index import VectorIndex
        for path, expected in [(config.context_vector_path, expected_context), (config.vector_path, expected_legacy)]:
            reopened = VectorIndex(config, path=path)
            reopened.initialize(768)
            assert reopened.ids() == expected
            reopened.close()
        # Native extraction is immediately visible to the resident HTTP namespace.
        assert adapter.call_tool('jiangli', 'memory_search', {'query': 'Native hook', 'space': 'personal'})['count'] >= 1
        assert model.initializations == 1
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_embedding_prefixes_once_through_real_engine(embedding_http):
    config, model, runtime = embedding_http
    # Swap only the heavyweight llama embed boundary; execute actual prefix code.
    local = EmbeddingEngine(runtime.server_for('jiangli').config)
    class Encoder:
        def __init__(self): self.seen = []
        def embed(self, text):
            self.seen.append(text)
            return [0.1] * 768
        def close(self): pass
    encoder = Encoder()
    local._model, local._dim = encoder, 768
    runtime._shared_engine._engine = local
    client = EmbeddingEngine(config)
    client.initialize()
    client.encode_query('question')
    client.encode_document('article')
    assert encoder.seen == ['search_query: question', 'search_document: article']


def test_embedding_endpoints_validate_shape_size_auth_and_no_model(embedding_http):
    config, model, runtime = embedding_http
    from evolvmem.lan_http_client import JsonClient
    client = JsonClient(config.embedding_http_url + '/embedding', config.embedding_http_token_file)
    for payload in ({'kind': 'wrong', 'text': 'x'}, {'kind': 'query', 'text': 'x' * 32769}, {'kind': 'query', 'text': 12}, {'kind': 'query', 'text': 'x', 'user': 'kane'}):
        with pytest.raises(RuntimeError, match='lan_http_unavailable'):
            client.post(payload)
    model.encode_query = lambda text: [float('nan')] * 768
    with pytest.raises(RuntimeError, match='lan_http_unavailable'):
        client.post({'kind': 'query', 'text': 'nonfinite'})
    from pathlib import Path
    token_file = Path(config.embedding_http_token_file)
    token_file.chmod(0o644)
    with pytest.raises(RuntimeError, match='lan_http_unavailable'):
        client.post({'kind': 'query', 'text': 'credentials'})
    token_file.chmod(0o600)
    assert not runtime.server_for('jiangli').config.embedding_http_url
    assert runtime.server_for('jiangli').config.lan_shared_vector_cache


def test_embedding_http_does_not_wait_for_sqlite_dispatch_lock(embedding_http):
    """Native extraction holds SQLite while requesting embeddings: no lock cycle."""
    config, model, runtime = embedding_http
    import sqlite3, http.client, time
    from concurrent.futures import ThreadPoolExecutor
    from urllib.parse import urlsplit
    from evolvmem.lan_http_client import JsonClient
    external = sqlite3.connect(runtime.settings.owner_data_dir / 'memory.db')
    external.execute('BEGIN IMMEDIATE')
    with ThreadPoolExecutor(max_workers=1) as pool:
        rpc_client = JsonClient(config.embedding_http_url + '/owner/mcp', config.embedding_http_token_file)
        future = pool.submit(rpc_client.post, dict(jsonrpc='2.0', id=1, method='tools/call', params=dict(name='memory_add', arguments=dict(key='project:demo:fact:blocked', value='blocked SQLite marker'))))
        try:
            held = False
            for _ in range(200):
                if not runtime._lan_dispatch_lock.acquire(blocking=False):
                    held = True
                    break
                runtime._lan_dispatch_lock.release()
                time.sleep(.005)
            assert held, 'MCP request did not enter dispatch'
            endpoint = urlsplit(config.embedding_http_url)
            conn = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=2)
            conn.request('POST', '/embedding', json.dumps(dict(kind='query', text='hook query inside SQLite transaction')),
                         {'Content-Type': 'application/json', 'Authorization': 'Bearer jiangli-token'})
            response = conn.getresponse()
            assert response.status == 200
            assert len(json.loads(response.read())['vector']) == 768
            conn.close()
        finally:
            external.rollback()
            external.close()
        assert 'result' in future.result(timeout=10)


def test_initialized_http_engine_marks_unavailable_after_server_loss(embedding_http):
    config, model, runtime = embedding_http
    client = EmbeddingEngine(config)
    client.initialize()
    assert client.is_loaded
    model.is_loaded = False
    with pytest.raises(RuntimeError):
        client.encode_query('server lost its model')
    assert not client.is_loaded

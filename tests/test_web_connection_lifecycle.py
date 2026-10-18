"""Real browser preconnects must not monopolize the Web console listener."""
import json
import socket
import threading
import urllib.request
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from evolvmem.web_server import make_http_server
from tests.test_web_server import _make_service


@pytest.mark.parametrize('prefix', [b'', b'GET / HTTP/1.1\r\nHost: localhost\r\n'])
def test_idle_or_incomplete_browser_connection_does_not_block_login(test_config, prefix):
    holder, ready, accepted = {}, threading.Event(), threading.Event()

    def serve():
        service = _make_service(test_config)
        server = make_http_server(service, port=0)
        original = server.get_request

        def get_request():
            result = original()
            accepted.set()
            return result

        server.get_request = get_request
        holder['server'] = server
        ready.set()
        try:
            server.serve_forever()
        finally:
            service.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    assert ready.wait(5)
    server = holder['server']
    idle = socket.create_connection(server.server_address, timeout=2)
    try:
        if prefix:
            idle.sendall(prefix)
        assert accepted.wait(2)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        # Keep the first connection open throughout. A serial socket listener
        # blocks here even though this is only a local, read-only login query.
        with opener.open(f'http://127.0.0.1:{server.server_port}/api/auth/me', timeout=2) as response:
            assert response.status == 200
            assert json.load(response)['authenticated'] is True
    finally:
        idle.close()
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_parallel_requests_use_independent_read_connections(test_config, monkeypatch):
    from evolvmem import web_server
    service = _make_service(test_config)
    original = web_server.api_stats
    counts = {'active': 0, 'peak': 0}
    guard = threading.Lock()

    def observed_stats(*args, **kwargs):
        with guard:
            counts['active'] += 1
            counts['peak'] = max(counts['peak'], counts['active'])
        try:
            # Widen the overlap window while still executing real SQLite reads.
            time.sleep(.03)
            return original(*args, **kwargs)
        finally:
            with guard:
                counts['active'] -= 1

    monkeypatch.setattr(web_server, 'api_stats', observed_stats)
    server = make_http_server(service, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    start = threading.Barrier(4)

    def get_stats(_):
        start.wait(timeout=3)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{server.server_port}/api/stats', timeout=3) as r:
            assert r.status == 200
            return json.load(r)

    try:
        with ThreadPoolExecutor(max_workers=4) as clients:
            results = list(clients.map(get_stats, range(4)))
        assert len(results) == 4 and all(result == results[0] for result in results)
        assert counts['peak'] > 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
        service.close()

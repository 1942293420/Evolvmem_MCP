"""Real loopback accept-queue behavior for a bounded identity burst.

The listener is created by the production factory but does not call
serve_forever() until every client connect() has completed.  This isolates the
kernel pending-accept queue from application request work.
"""
import json
import socket
import threading

from evolvmem.lan_server import make_http_server
from evolvmem.library_foundation import RequestIdentity


CLIENTS = 20
SOCKET_DEADLINE = 0.6
TOKEN = 'synthetic-backlog-token'


class BacklogHost:
    def __init__(self):
        self.identity = RequestIdentity(
            '00000000-0000-0000-0000-000000000001',
            '00000000-0000-0000-0000-000000000002',
            '00000000-0000-0000-0000-000000000003',
            1,
        )
        self.calls = []

    def authenticate(self, token):
        return self.identity if token == TOKEN else None

    def serve_rpc(self, identity, request, *, publish):
        assert identity is self.identity
        self.calls.append(request['id'])
        publish({'jsonrpc': '2.0', 'id': request['id'], 'result': {}})


def _request_bytes(sequence):
    body = json.dumps({'jsonrpc': '2.0', 'id': sequence, 'method': 'ping'}).encode()
    return (
        b'POST /mcp HTTP/1.1\r\n'
        b'Host: 127.0.0.1\r\n'
        b'Authorization: Bearer ' + TOKEN.encode() + b'\r\n'
        b'Content-Type: application/json\r\n'
        b'Content-Length: ' + str(len(body)).encode() + b'\r\n'
        b'Connection: close\r\n\r\n' + body
    )


def _read_response(sock):
    chunks = []
    while True:
        block = sock.recv(65536)
        if not block:
            break
        chunks.append(block)
    raw = b''.join(chunks)
    head, separator, body = raw.partition(b'\r\n\r\n')
    assert separator == b'\r\n\r\n', 'incomplete HTTP response'
    assert head.startswith(b'HTTP/1.1 200 '), head[:80]
    return json.loads(body)


def test_twenty_connection_burst_is_queued_before_request_acceptance():
    host = BacklogHost()
    server = make_http_server(None, host='127.0.0.1', port=0, library_host=host)
    connected = []
    failures = []
    lock = threading.Lock()
    barrier = threading.Barrier(CLIENTS + 1)
    server_thread = None

    def connect(index):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(SOCKET_DEADLINE)
        try:
            barrier.wait(timeout=3)
            sock.connect(server.server_address)
        except BaseException as exc:
            sock.close()
            with lock:
                failures.append((index, type(exc).__name__, str(exc)))
        else:
            with lock:
                connected.append((index, sock))

    clients = [threading.Thread(target=connect, args=(index,)) for index in range(CLIENTS)]
    for client in clients:
        client.start()
    try:
        barrier.wait(timeout=3)
        for client in clients:
            client.join(3)
        assert not any(client.is_alive() for client in clients), 'connect burst did not settle'
        assert not failures, (
            f'pending accept queue of {server.request_queue_size} refused or timed out '
            f'{len(failures)} of {CLIENTS} connections: {failures}'
        )
        assert len(connected) == CLIENTS

        # No handler has run yet. Start acceptance only after the entire burst
        # has completed, then prove every queued connection receives a reply.
        server_thread = threading.Thread(
            target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True
        )
        server_thread.start()
        for index, sock in sorted(connected):
            sock.sendall(_request_bytes(index))
            response = _read_response(sock)
            assert response == {'jsonrpc': '2.0', 'id': index, 'result': {}}
        assert sorted(host.calls) == list(range(CLIENTS))
    finally:
        for _, sock in connected:
            sock.close()
        if server_thread is not None:
            server.shutdown()
            server_thread.join(3)
        server.server_close()
        assert server_thread is None or not server_thread.is_alive()

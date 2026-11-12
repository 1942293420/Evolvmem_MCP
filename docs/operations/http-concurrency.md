# HTTP connection queue and application concurrency

EvolvMem's LAN listener uses `ThreadingHTTPServer`. A short burst of client connections
can complete TCP handshakes before the application has accepted every socket.
Those sockets wait in the kernel's pending-accept queue; they are not yet
authenticated requests and do not consume an application request slot.

```text
20 client connections (one synthetic identity in the unit test)
        |
        | TCP handshakes (kernel completes these)
        v
kernel listen() pending-accept queue
request_queue_size = 64 (finite)
        |
        | serve_forever() accepts one ready socket per loop
        v
one handler thread per accepted connection
(daemon_threads=True, block_on_close=True)
        |
        | LibraryHost.request_scope() admission
        | max_requests active-request cap (nonblocking)
        v
admitted request slot and request deadline guard
        |
        | authenticate token + expected-user / Origin checks
        v
        | Content-Length and 1 MiB body checks
        v
        | serve_rpc / publication
```

The queue is an acceptance buffer, not an application work queue. The explicit
finite value `64` covers the bounded 20-connection burst while leaving the kernel
queue bounded. A larger burst can still wait or time out at connect; it must not
be interpreted as permission for unlimited active work.

The unit test uses one synthetic identity for all 20 client connections. It joins
the connect threads and the `serve_forever` thread. It does not directly join or
assert the exit of the daemon handler threads; receipt of the HTTP responses is
the observed request-completion boundary. These are loopback unit checks, not a
full service-level SLA.

## Constraints

- `request_queue_size = 64` changes only the `listen()` backlog. It is not
  unbounded and does not add a thread pool.
- Authentication, Origin/expected-user checks, the 1 MiB body limit,
  `Content-Length` validation, deadlines, and `LibraryHost.max_requests`
  admission remain unchanged.
- Overcapacity inside the application is still refused with
  `library_host_busy`; queue capacity does not bypass that guard.
- `daemon_threads=True` and `block_on_close=True` remain unchanged.
- The old implicit default was Python's `5`. Deployments must not rely on that
  interpreter default for a 20-connection burst.
- This document makes no PostgreSQL, production-service, or performance claim.
  The accompanying verification covers loopback sockets and synthetic hosts.

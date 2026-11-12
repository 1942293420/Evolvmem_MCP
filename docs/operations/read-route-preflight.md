# Bounded sequential read preflight

## Problem

`AuthorityRouter.plan_many` originally called `plan()` once per selected
library. Each `plan()` opened a separate short `ControlProvider.open()` session
to run `em_control.resolve_library`. That preserved sequential error precedence
but used one control connection per library.

An earlier candidate eagerly validated every UUID and fetched every snapshot in
one batch before validating any route. That changed behavior: a denial on the
first library could be overridden by a malformed later UUID or a later backend
failure, and some catalog exceptions were reclassified. This candidate keeps
one connection while restoring the original order.

## Candidate behavior

- `ControlCatalog.authorization_preflight(identity)` is a context manager that
  yields a session-bound `resolve(library_id)` callable.
- In the ordinary path it opens **one** read-only `ControlProvider.open()`
  session. Each resolver call runs the existing
  `em_control.resolve_library` SQL and decodes through the existing typed
  `_decode_snapshot`/`_snapshot` path.
- If an authority lease is already active (`_HELD`), the context yields a
  resolver that calls the existing `authorization_snapshot()` per ID, preserving
  that method's per-library lease scope and transaction-liveness checks.
- `AuthorityRouter.plan_many` validates identity and the first UUID before
  entering the context, then for each library: validates its UUID, resolves its
  snapshot, and validates the route before reading the next library. A denial or
  other validated failure is therefore final, exactly as in the original
  sequential `plan()` loop.
- Resolver exceptions are mapped to `catalog_unavailable` by the router, as the
  original `plan()` did. Provider open/cleanup exceptions are normalized by the
  context to `catalog_unavailable`, but an already-raised validated denial is
  not remapped.
- One-library selections still use `plan()` unchanged. Adapters without
  `authorization_preflight` still fall back to the original per-library
  `plan()` loop. Duplicate/out-of-range selections are rejected by the existing
  router guard before opening anything.
- Snapshot validation is shared through `_validated_route()`; identity, tool,
  UUID, permission, ACL/authority, epoch, writer, backend-support and error
  classification remain unchanged.

## Preflight is not a lease

The preflight is a short read-only control lookup. It is not an authority pin.
`ControlAuthorityLease` still opens its own read-write control connection, pins
every route, checks each scope, and cleans up as before. Permission and epoch
are checked again against the fresh pin row; preflight snapshots are never
reused as lease authority. Credential authentication, revalidation, content
snapshots, held-lease lifetime, and content connections are untouched. No
cross-request cache or adapter lifetime extension was added.

The test connection count is a unit-test count using a synthetic connector. It
is not a production performance or real PostgreSQL measurement.

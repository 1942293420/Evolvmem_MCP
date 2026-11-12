# Projection selection flow

## Before

```text
ProjectionService tick
  └─ host context() (fresh auth + request scope + deadline)
       ├─ router.plan(context_read, library)
       ├─ manager.run(route, SELECT list_recall_projects(lib,'write',after))
       ├─ rows empty?  ── yes ──> after = NULL; yield None (idle)
       │                 no
       ├─ choose rows[0].project_key (whether or not the model has work)
       ├─ after = chosen key
       ├─ VectorPort(project_key) opened
       └─ worker.project_one(): often returns no_pending_for_model
```

## After

```text
ProjectionService tick (same max_jobs, deferral, deadline, cleanup)
  └─ host context() (same authenticated/authorized read session)
       ├─ router.plan(context_read, library)
       ├─ manager.run(route,
       │     SELECT p.project_id, p.project_key, COALESCE(w.has_pending,false)
       │     FROM em_security.list_recall_projects(lib,'write',after) p
       │     LEFT JOIN LATERAL (
       │       SELECT true AS has_pending FROM em_content.vector_outbox o
       │       WHERE o.library_id=lib AND o.project_id=p.project_id
       │         AND o.model_contract=model AND o.status='pending' LIMIT 1
       │     ) w ON true
       │     ORDER BY p.project_key COLLATE "C")
       │       -- function still applies write ACL + RLS + LIMIT 128
       │       -- Python rejects malformed or non-increasing rows
       ├─ rows empty? ── yes ──> after = NULL; yield None   (wrap, as before)
       │                 no
       ├─ any has_pending?
       │     no  ──> resolve_project_scope(session, first witness key)
       │              VectorRepository(...).check_schema()  (same session/txn)
       │              after = page last key; yield None      (no port)
       │     yes ──> choose FIRST pending key
       │              after = that key
       │              VectorPort(project_key) opened
       └─ worker.project_one() handles the exact job; disappearance is a
          normal no-job result and the cursor has still moved forward
```

The existing historical branch is unchanged and never executes this listing.
The lateral subquery runs as the runtime role, so outbox RLS
(`allows(...,'read')`, which includes write/admin) and the existing table
`SELECT` grant still apply. `LEFT JOIN ... ON true` preserves every authorized
page row; only the pending witness is annotated, and the explicit key order is
unchanged.

## Plan risk

`em_content.vector_outbox` has only `vector_pending(library_id,id)`. The
correlated predicate on `project_id` + `model_contract` + `status` is not
indexed. A page of up to 128 projects may repeatedly scan library-wide pending
rows and evaluate RLS. Verify with `EXPLAIN (ANALYZE, BUFFERS)` before adoption;
no index is added here.

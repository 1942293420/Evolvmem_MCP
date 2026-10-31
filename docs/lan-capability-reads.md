# Independent LAN capability queries

Hosted LAN capability queries with background health enabled now use request-owned SQLite connections and capability indexes. The route decision is fixed before lock selection; it cannot switch back to a resident handler after bypassing the business lock. Unhosted adapters keep their existing locked health contract. Other search tools and mutations keep their existing routes.

Existing authentication, argument, personal namespace, project/module and source eligibility checks remain. Entry and exit health gates still reject unavailable boundaries. Search uses its own candidate read snapshots and final selected-result validation opens a new current snapshot. Invalid final structures preserve the `context_unavailable` error and exit health diagnostic. Namespace file identity and runtime shutdown are checked before a result returns.

Each request owns a `mode=ro`, query-only connection with a 100 ms SQLite busy timeout. There is no outer transaction: vector encoding runs outside the database snapshot, while candidate and final proof reads own separate snapshots. The capability index's eligibility callback references only the request store. A frozen native index and its proof manifest retain the existing file-lock order and current-source checks. The request does not save vectors or proofs, clear dirty state, initialize database schema, or update access counts. Search may create its existing lock files/directories; it is not free of all filesystem effects.

All adapters and namespaces of one runtime share two heavy-query permits. Saturation returns `capability_read_busy` after a bounded permit wait, independently of the eight continuation-read permits. This deliberately bounds native index memory. It is an intermediate capacity limit, not acceptance for 10–20 simultaneous successful recalls. Model serialization and short index-file coordination still apply; this change does not promise a hard request deadline.

Only the embedding engine is shared. Requests explicitly close their own SQLite and native handles in nested cleanup blocks, returning permits even after cleanup errors. They never close the shared engine. Every serialized encode/rerank entry checks the engine's closed flag while holding the same lifecycle lock, preventing queued calls from using an already closed model.

Regression coverage includes business-lock independence, writes during a paused recall, read-only owned connections, committed-only data, vector-only real-index results compared with the existing result signature, absence of index saves/shared-engine closes, final withdrawal, invalid structures, exit-health rejection, shared adapter limits and queued encodes after model shutdown. Full existing provenance, vector snapshot, ranking and LAN tests remain release gates.

The combined LAN, continuation, background health, capability, project-module and source-export regression run passed 735 cases. Desktop workflow checks passed at 1280 and 1440 widths; the heavy-read limit and remaining concurrency acceptance boundary are visible in the health result detail.

PostgreSQL, per-device database authorization, federated queries, client stdio concurrency and final mixed-workload acceptance remain later work. This change does not rewrite or reclassify historical data.

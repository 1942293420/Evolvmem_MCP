# Current, source-backed capability recall

Search now combines current source eligibility with per-source value assessment before ranking the final results. The vector cache and lexical index use the same eligibility function. A high similarity score cannot revive a withdrawn source, confirm ownership or certify a method's outcome.

An assessed source must pass both current provenance and value eligibility on the same source ID. A shared memory can remain available through an independent valid source. Existing active knowledge without the new assessment remains compatible while its source is valid. When that legacy knowledge later receives a weak additional source, the weak source does not inherit the old source's assessment exemption or contribute new project labels. Unknown ownership stays explicit. Project names shown in recall come from current eligible sources rather than the legacy source_project label.

Historical browsing remains separate: withdrawal does not delete the memory or its source record. Default recall excludes unsupported content. Any future review UI must preserve that distinction and show why a row is not being recalled.

## Identifier namespaces

Capability IDs and Context IDs can have the same integer value but identify different records. Primary session-start results now return capability_ids separately and leave selected_ids for actual Context records. LAN references use personal:capability:ID and personal:context:ID separately. The rendered block labels development knowledge with capability:ID and an unverified method-result notice. Capability IDs must never be sent to Context feedback, outcome, access-count or deletion APIs.

## Background recovery

The existing LAN recovery worker owns its own SQLite handles. A separate capability recovery loop performs at most 32 IDs per round, no more often than once per minute, with bounded failure backoff. It shares the existing embedding engine, not a request transaction. Source and assessment checks are batched. Failed namespaces do not stop other namespaces, and recovery closes its own vector handles. No model request, full rebuild or source migration runs in a foreground read handler.

The ContextService index factory also uses the combined eligibility function for individual synchronization and query reads. Complete coverage is certified by the index's source/text/vector proofs, never by one successful write. This does not change the separate Context/legacy vector caches.

## Evidence

Three retrieval tests first reproduced untraceable recall, stale project labels and withdrawn sources still being recalled. Namespace tests reproduced capability ID 1 appearing as Context selected_id 1. After implementation, capability recall, namespace, MCP handler, Context model and LAN sharing tests passed 96 cases. Recovery-loop, worker-isolation, LAN runtime and capability-index recovery tests passed 51 cases. These are isolated-test results, not production deployment evidence.

The dated database copy also verified 2,169 provenance insertions, an idempotent repeat, preserved eligibility (2,148 records) and confirmed ownership (264), complete rollback, idempotent rollback, unchanged capability rows and SQLite quick_check ok. The canonical backup was not modified.

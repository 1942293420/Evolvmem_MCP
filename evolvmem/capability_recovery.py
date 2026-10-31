"""Bounded background capability recovery on worker-owned database handles."""
from __future__ import annotations

import time

from evolvmem.capability_index import CapabilityIndex
from evolvmem.capability_recall_eligibility import eligible_ids as eligible_memory_ids


class CapabilityVectorRecoveryLoop:
    def __init__(self, *, interval_seconds=60, batch_size=32, clock=time.monotonic, index_factory=None):
        if not isinstance(interval_seconds, (int, float)) or interval_seconds < 60:
            raise ValueError("invalid_recovery_interval")
        if type(batch_size) is not int or not 1 <= batch_size <= 256:
            raise ValueError("invalid_recovery_batch")
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size
        self.clock = clock
        self.index_factory = index_factory
        self.next_attempt_at = 0
        self.failures = 0

    def due(self):
        return self.clock() >= self.next_attempt_at

    def tick(self, services):
        if not self.due():
            return ()
        reports = []
        failed = False
        for service in services:
            index = None
            try:
                index = (self.index_factory(service) if self.index_factory else CapabilityIndex(
                    service.store, service.config, service.embedding_engine,
                    eligible_ids=lambda ids, store=service.store: eligible_memory_ids(store, ids)))
                report = index.reconcile(limit=self.batch_size)
                failed |= report["status"] in {"failed", "unavailable", "incompatible"}
                reports.append(report)
            except Exception as exc:
                failed = True
                reports.append({"status": "failed", "processed": 0, "error": type(exc).__name__})
            finally:
                if index is not None:
                    try:
                        index.vector_index.close()
                    except Exception:
                        pass
        self.failures = self.failures + 1 if failed else 0
        self.next_attempt_at = self.clock() + min(900, self.interval_seconds * 2 ** min(max(0, self.failures - 1), 4))
        return tuple(reports)

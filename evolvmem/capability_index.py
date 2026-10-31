"""Independent capability vectors with bounded, resumable proof-based recovery."""
from __future__ import annotations

from contextlib import contextmanager, nullcontext
import hashlib
import json
import os
import tempfile

import numpy as np
from usearch.index import Index

from evolvmem.capability_store import CapabilityStore
from evolvmem.vector_index import VectorIndex
from evolvmem.request_trace import request_trace, stage
from evolvmem.vector_provenance import contract, entry, text_digest, vector_digest


class _Incompatible(ValueError):
    pass


class _StaleAudit(RuntimeError):
    """A committed database change invalidated the read-only eligibility audit."""



class CapabilityIndex:
    def __init__(self, context_store, config, embedding_engine=None, *, eligible=None, eligible_ids=None):
        self.context_store = context_store
        self.config = config
        self.embedding_engine = embedding_engine
        self.eligible = eligible
        self.eligible_ids = eligible_ids
        self.vector_index = VectorIndex(config, path=config.capability_vector_path)

    @staticmethod
    def _supports_shared_recovery():
        try:
            import fcntl  # Linux host backend; keep Windows import/legacy recall usable.
        except ImportError:
            return False
        return True

    @property
    def _proof_path(self):
        return self.vector_index.path.with_suffix(".provenance.json")

    @contextmanager
    def _locked(self):
        # This capability-only gate coordinates proofs with vector saves. The
        # underlying SharedVectorCache retains its own reload/merge/replace lock.
        if not self._supports_shared_recovery():
            # Preserve the pre-existing single-writer local path on Windows.
            # reconcile explicitly refuses to certify multi-writer recovery there.
            yield
            return
        import fcntl
        path = self.vector_index.path.with_suffix(".recovery.lock")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as lock:
            with stage('capability_lock_wait'):
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                with stage('capability_lock_held'):
                    yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _ensure_index(self, *, reload=False) -> None:
        if reload:
            self.vector_index.close()  # Discard any staging left by a failed save.
        try:
            self.vector_index.count()
        except RuntimeError:
            self.vector_index.initialize(dim=self.config.embedding_dim)
        if self.vector_index._shared_cache is None and self._supports_shared_recovery():
            from evolvmem.lan_vector_cache import SharedVectorCache
            self.vector_index._shared_cache = SharedVectorCache(self.vector_index)
        if self.vector_index.inspect_metadata().dimension != self.config.embedding_dim:
            raise _Incompatible("index_dimension_mismatch")
        engine_config = getattr(self.embedding_engine, "config", None)
        if engine_config is not None and contract(engine_config) != contract(self.config):
            raise _Incompatible("engine_contract_mismatch")

    def _load_proofs(self):
        try:
            proof = json.loads(self._proof_path.read_text())
        except FileNotFoundError:
            proof = None
        except (OSError, ValueError):
            raise _Incompatible("invalid_proof_manifest")
        if proof is None:
            return {"version": 1, "contract": contract(self.config), "generation": 0,
                    "cursor": 0, "items": {}}
        if not isinstance(proof, dict) or proof.get("version") != 1 or not isinstance(proof.get("items"), dict):
            raise _Incompatible("invalid_proof_manifest")
        if proof.get("contract") != contract(self.config):
            raise _Incompatible("embedding_contract_mismatch")
        return proof

    def _save_proofs(self, proof):
        proof["generation"] = int(proof.get("generation", 0)) + 1
        fd, temporary = tempfile.mkstemp(prefix=".capability-proof-", dir=self._proof_path.parent)
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(proof, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._proof_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _pending(self):
        self.vector_index.mark_dirty()
        # VectorIndex.save must not clear a global debt marker it happens to own.
        self.vector_index.preserve_dirty()

    @staticmethod
    def _text(memory):
        return memory["title"] + "\n" + memory["content"]

    def _eligible(self, memory, *, allowed=None):
        if memory is None or memory["status"] != "active":
            return False
        if self.eligible_ids is not None:
            if allowed is None:
                allowed = set(self.eligible_ids([int(memory["memory_id"])]))
            if int(memory["memory_id"]) not in allowed:
                return False
        return self.eligible is None or bool(self.eligible(memory))

    def _proof_matches(self, ident, memory, proofs, *, vectors=None):
        proof = proofs.get(str(ident))
        if not isinstance(proof, dict) or proof.get("text") != text_digest(self._text(memory)):
            return False
        vector = (vectors or self.vector_index).vector_copy(ident)
        return vector is not None and proof.get("vector") == vector_digest(vector)

    def _audit(self, proof, *, vectors=None):
        rows = self.context_store._connection().execute(
            "SELECT * FROM capability_memories WHERE status='active' ORDER BY memory_id"
        ).fetchall()
        truth = {int(row["memory_id"]): CapabilityStore._public(row) for row in rows}
        allowed = set(self.eligible_ids(list(truth))) if self.eligible_ids is not None else None
        truth = {ident: row for ident, row in truth.items() if self._eligible(row, allowed=allowed)}
        vectors = vectors or self.vector_index
        indexed = set(vectors.ids())
        expected = set(truth)
        missing = expected - indexed
        outdated = {ident for ident in expected & indexed
                    if not self._proof_matches(ident, truth[ident], proof["items"], vectors=vectors)}
        removed = (indexed | {int(ident) for ident in proof["items"]}) - expected
        return {"eligible": len(expected), "indexed": len(indexed),
                "missing": len(missing), "outdated": len(outdated),
                "obsolete": len(removed), "remaining": len(missing | outdated | removed),
                "pending_ids": sorted(missing | outdated | removed)}

    def _audit_fence(self):
        conn = self.context_store._connection()
        # data_version observes commits by other connections; total_changes
        # catches our own commits/rollbacks, which do not change data_version.
        version = conn.execute("PRAGMA data_version").fetchone()[0]
        from evolvmem.context_store import _now_iso
        from evolvmem.context_temporal import window_contains
        now = _now_iso()
        # Source eligibility also changes at time boundaries without a DB write.
        # Recheck only the small temporal metadata projection, not source bodies
        # or the full provenance/value gate, inside the publication transaction.
        windows = tuple((row[0], bool(row[1] and row[1] <= now),
                         window_contains(row[2], None, now),
                         window_contains(None, row[3], now)) for row in conn.execute(
            "SELECT id,expires_at,effective_from,effective_until FROM context_items "
            "WHERE status='active' AND (expires_at IS NOT NULL OR effective_from IS NOT NULL "
            "OR effective_until IS NOT NULL) ORDER BY id"))
        identity = self.config.db_path.stat()
        return (id(conn), identity.st_dev, identity.st_ino,
                conn.execute('PRAGMA schema_version').fetchone()[0],
                version, conn.total_changes, tuple(contract(self.config)), windows)

    @contextmanager
    def _read_snapshot(self, *, certify=False):
        """Own one read transaction, never a caller's write or file lock."""
        conn = self.context_store._connection()
        if conn.in_transaction or self.context_store._transaction_depth:
            raise RuntimeError('capability_audit_requires_idle_connection')
        fence = self._audit_fence() if certify else None
        query_only = conn.execute('PRAGMA query_only').fetchone()[0]
        try:
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            yield fence
        finally:
            if conn.in_transaction:
                conn.rollback()
            conn.execute('PRAGMA query_only=' + str(int(query_only)))
        if certify and self._audit_fence() != fence:
            raise _StaleAudit('eligibility_snapshot_changed')

    @staticmethod
    def _file_image(path):
        try:
            with path.open('rb') as handle:
                before = os.fstat(handle.fileno())
                raw = handle.read()
                after = os.fstat(handle.fileno())
            current = path.stat()
        except FileNotFoundError:
            if path.exists():
                raise _StaleAudit('index_image_changed')
            return None, None
        def identity(value):
            return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns
        if identity(before) != identity(after) or identity(after) != identity(current):
            raise _StaleAudit('index_image_changed')
        return raw, (*identity(after), hashlib.sha256(raw).digest())

    def _image_fence(self):
        return (self._file_image(self.vector_index.path)[1],
                self._file_image(self._proof_path)[1], tuple(contract(self.config)))

    @contextmanager
    def _snapshot(self, *, flush=False):
        """Copy durable bytes under file locks; restore an owned index outside.

        No native handle, pending mutation or auto-refresh cache escapes. The
        content digests also catch equal-size replacements and vector-only saves.
        """
        with self._locked():
            self._ensure_index(reload=True)
            if flush:
                self.vector_index.preserve_dirty()
                self.vector_index.save()
            cache = self.vector_index._shared_cache
            with cache._locked() if cache is not None else nullcontext():
                raw, vector_fence = self._file_image(self.vector_index.path)
                _, proof_fence = self._file_image(self._proof_path)
                proof = self._load_proofs()
                # The recovery gate coordinates proof writers; check bytes
                # again to reject a nonparticipating replacement as well.
                if self._file_image(self._proof_path)[1] != proof_fence:
                    raise _StaleAudit('index_image_changed')
                image_fence = (vector_fence, proof_fence, tuple(contract(self.config)))
        frozen = VectorIndex(self.config, path=self.vector_index.path)
        try:
            frozen._dim = self.config.embedding_dim
            frozen._index = (Index.restore(raw, view=False) if raw is not None
                             else Index(ndim=self.config.embedding_dim, metric='cos', dtype='f32'))
            if frozen.inspect_metadata().dimension != self.config.embedding_dim:
                raise _Incompatible('index_dimension_mismatch')
            yield frozen, proof, image_fence
        finally:
            frozen.close()

    def _read_audit(self, proof, *, certify=True, vectors=None):
        """Audit one WAL snapshot without reserving SQLite's only writer.

        Eligibility callbacks must be SQLite reads. Current provenance, source
        heads, manual decisions, value evidence and source bodies all live in
        this database; no model or external rule file determines this gate.
        For certification, any commit during or after the snapshot invalidates its publication,
        even when the commit changes an unrelated row. False dirty is safe.
        """
        with self._read_snapshot(certify=certify) as fence:
            audit = self._audit(proof, vectors=vectors)
        return audit, fence

    def _sync_one(self, memory_id):
        store = CapabilityStore(self.context_store)
        with self._locked():
            self._ensure_index(reload=True)
            self._load_proofs()
            self._pending()
        with stage('capability_sync_initial_proof'), self._read_snapshot():
            original = store.get(memory_id)
            was_eligible = self._eligible(original)
            encoded_contract = contract(self.config)
        vector = None
        if was_eligible:
            if self.embedding_engine is None or not getattr(self.embedding_engine, "is_loaded", False):
                return "unavailable"
            vector = np.asarray(self.embedding_engine.encode_document(self._text(original)), dtype=np.float32)
            if vector.ndim != 1 or len(vector) != self.config.embedding_dim or not np.isfinite(vector).all():
                raise ValueError("invalid_embedding")
        # Recheck the heavy source proof outside every file/writer lock, then
        # fence its DB snapshot in the short publication transaction.
        try:
            with stage('capability_sync_final_proof'), self._read_snapshot(certify=True) as fence:
                current = store.get(memory_id)
                now_eligible = self._eligible(current)
        except _StaleAudit:
            return 'stale'
        if was_eligible:
            if (not now_eligible or current['revision'] != original['revision']
                    or self._text(current) != self._text(original)):
                return 'stale'
        elif now_eligible:
            return 'stale'
        with self._locked(), self.context_store.transaction():
            self._ensure_index(reload=True)
            proof = self._load_proofs()
            if self._audit_fence() != fence:
                return 'stale'
            if encoded_contract != contract(self.config):
                raise _Incompatible("embedding_contract_changed")
            self._pending()
            self.vector_index.remove(memory_id)
            if was_eligible:
                self.vector_index.add(memory_id, vector)
                proof["items"][str(memory_id)] = entry(self._text(current), vector)
            else:
                proof["items"].pop(str(memory_id), None)
            self.vector_index.preserve_dirty()
            self.vector_index.save()
            proof["cursor"] = memory_id
            self._save_proofs(proof)
            return "updated" if was_eligible else "removed"

    @request_trace('capability_sync')
    def sync(self, memory_id: int, text: str) -> str:
        """Compatibility entry point; canonical SQLite text supersedes caller text.

        A single write never certifies the complete cache or clears dirty debt.
        """
        if self.context_store._connection().in_transaction or self.context_store._transaction_depth:
            # Never wait for a file gate while borrowing a caller's writer.
            # The writer prevents a concurrent certifier from clearing debt.
            self._pending()
            return 'failed'
        try:
            status = self._sync_one(int(memory_id))
            return "synchronized" if status in {"updated", "removed"} else status
        except Exception:
            with self._locked():
                self._pending()
            return "failed"

    @request_trace('capability_reconcile')
    def reconcile(self, *, limit=64) -> dict:
        """Repair at most limit IDs; only a complete final audit clears dirty.

        Active rows form the default truth; ``eligible(memory)`` can narrow it.
        Resume is automatic from durable proofs and a fair round-robin cursor.
        """
        if type(limit) is not int or not 1 <= limit <= 4096:
            raise ValueError("invalid_reconcile_limit")
        result = {"status": "failed", "processed": 0, "updated": 0, "removed": 0,
                  "failed": 0, "stale": 0, "unavailable": 0, "remaining": None, "errors": []}
        if not self._supports_shared_recovery():
            result["status"] = "unsupported_platform"
            result["errors"] = [{"reason": "shared_recovery_requires_posix_file_locks"}]
            result["dirty"] = self.vector_index.is_dirty()
            return result
        if self.context_store._connection().in_transaction or self.context_store._transaction_depth:
            self._pending()
            result['errors'] = [{'reason': 'capability_audit_requires_idle_connection'}]
            result['dirty'] = True
            return result
        try:
            with self._snapshot() as (vectors, proof, _):
                # Selection is advisory: individual _sync_one calls recheck
                # current sources. Concurrent uploads must not starve progress.
                with stage('capability_recovery_initial_audit'):
                    audit, _ = self._read_audit(proof, certify=False, vectors=vectors)
                ids = audit.pop("pending_ids")
                cursor = int(proof.get("cursor", 0))
                ids = ([ident for ident in ids if ident > cursor]
                       + [ident for ident in ids if ident <= cursor])[:limit]
            if ids:
                with self._locked():
                    self._pending()
            for ident in ids:
                try:
                    state = self._sync_one(ident)
                    result[state] += 1
                except Exception as exc:
                    result["failed"] += 1
                    result["errors"].append({"memory_id": ident, "reason": type(exc).__name__})
                result["processed"] += 1
                # Advance even on failure, so one poison item cannot starve later IDs.
                with self._locked():
                    proof = self._load_proofs()
                    proof["cursor"] = ident
                    self._save_proofs(proof)
            with self._snapshot(flush=True) as (vectors, proof, image_fence):
                with stage('capability_recovery_final_audit'):
                    audit, fence = self._read_audit(proof, vectors=vectors)
            with self._locked():
                self._ensure_index(reload=True)
                cache = self.vector_index._shared_cache
                with cache._locked():
                    with self.context_store.transaction():
                        # BEGIN IMMEDIATE fences later DB writers; no full-library
                        # source/value scan or model call occurs in this section.
                        if self._audit_fence() != fence:
                            raise _StaleAudit("eligibility_snapshot_changed")
                        if self._image_fence() != image_fence:
                            raise _StaleAudit('index_image_changed')
                        audit.pop("pending_ids")
                        result.update(audit)
                        if audit["remaining"] == 0 and not result["failed"]:
                            self._save_proofs(proof)
                            self.vector_index.clear_dirty()
                            result["status"] = "ready"
                        else:
                            self._pending()
                            result["status"] = ("failed" if result["failed"] else
                                                "unavailable" if result["unavailable"] else "partial")
        except _StaleAudit as exc:
            with self._locked():
                self._pending()
            result.update(status="partial", remaining=None, stale=result["stale"] + 1)
            result["errors"].append({"reason": str(exc)})
        except Exception as exc:
            with self._locked():
                self._pending()
            result["status"] = "incompatible" if isinstance(exc, _Incompatible) else "failed"
            result["errors"].append({"reason": str(exc) if isinstance(exc, _Incompatible) else type(exc).__name__})
        result["dirty"] = self.vector_index.is_dirty()
        return result

    @stage('capability_vector')
    def search(self, query: str, categories=(), top_k: int = 10) -> list[dict]:
        if self.embedding_engine is None or not getattr(self.embedding_engine, "is_loaded", False):
            return []
        if self.context_store._connection().in_transaction or self.context_store._transaction_depth:
            return []
        try:
            encoded_contract = contract(self.config)
            with stage('capability_embedding'):
                embedding = np.asarray(self.embedding_engine.encode_query(query), dtype=np.float32)
            if encoded_contract != contract(self.config):
                raise _Incompatible('embedding_contract_changed')
            with self._snapshot() as (vectors, proof, _):
                matches = vectors.search(embedding, k=max(1, int(top_k)) * 4)
                # A coherent read snapshot, not a promise that no later write
                # can occur. LAN selected-result revalidation remains in place.
                with self._read_snapshot():
                    store = CapabilityStore(self.context_store)
                    with stage('capability_vector_eligibility'):
                        allowed = (set(self.eligible_ids([int(match["id"]) for match in matches]))
                                   if self.eligible_ids is not None else None)
                    categories = set(categories)
                    results = []
                    for match in matches:
                        ident = int(match["id"])
                        memory = store.get(ident)
                        if not self._eligible(memory, allowed=allowed) or (categories and memory["category"] not in categories):
                            continue
                        # Legacy IDs without proofs remain searchable during the bounded
                        # backfill, but cannot count as complete recovery coverage.
                        if str(ident) in proof["items"] and not self._proof_matches(ident, memory, proof["items"], vectors=vectors):
                            continue
                        memory["vector_score"] = 1.0 - float(match["distance"]) / 2.0
                        results.append(memory)
                if encoded_contract != contract(self.config):
                    raise _Incompatible('embedding_contract_changed')
                return results[:max(1, int(top_k))]
        except Exception:
            return []

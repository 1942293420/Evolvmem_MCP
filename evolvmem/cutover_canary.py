"""The cutover canary: one authorized, exactly verifiable dual-write probe.

The canary is a single high-entropy, global pinned preference created
through ContextService (never the semantic-merge path) so the shadow step
and the post-switch acceptance can prove exact recall against a real
library without touching user content. The owner-only canary journal
privately holds the body and both probe queries plus the legacy/context
IDs and content hashes; the public handle projection carries hashes and
IDs only.

Cleanup re-reads and verifies both IDs, the legacy→context mapping, the
``cutover_canary`` source-kind provenance row, and the L2 content hash
before calling the dual ``legacy_hard_delete``. Any mismatch refuses the
deletion and retains the journal for recovery. On success both derived
vector entries are removed and absence is verified by exact-ID reads —
never by a new similarity search — and the journal is marked cleaned.

When the caller already holds the exclusive cutover lock (the formal
cutover's shadow step), ``externally_locked=True`` neutralizes the
service's per-mutation shared lock so the cutover's own write path cannot
deadlock against itself.
"""

from __future__ import annotations

from collections.abc import Mapping
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from typing import ClassVar

from evolvmem.config import Config
from evolvmem.context_models import (
    ContextContentType,
    ContextLayer,
    ContextMode,
    ContextReadRequest,
    ContextScope,
    ContextSearchRequest,
    ContextStatus,
    ContextTier,
    ContextValidationError,
)
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.cutover_models import validate_public_summary
from evolvmem.legacy_models import LegacyAddRequest, LegacyHardDeleteRequest
from evolvmem.vector_index import VectorIndex

__all__ = [
    "CANARY_JOURNAL_FILENAME",
    "CANARY_SOURCE_KIND",
    "CanaryHandle",
    "CanaryVerificationError",
    "CutoverCanary",
    "CutoverCanaryError",
]

CANARY_SOURCE_KIND = "cutover_canary"
CANARY_JOURNAL_FILENAME = "cutover-canary.json"

_SCHEMA = "evolvmem.cutover_canary"
_VERSION = 1
_EXTRACTION_VERSION = "cutover-v1"
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_CJK_HEX_DIGITS = "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳"
_SAFE_CODE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")


class CutoverCanaryError(Exception):
    """A canary lifecycle failure; messages never contain canary content."""

    def __init__(self, code: str, message: str = "") -> None:
        if not isinstance(code, str) or not _SAFE_CODE_PATTERN.match(code):
            raise ContextValidationError("code must be a lower-snake reason code")
        self.code = code
        super().__init__(message or code)


class CanaryVerificationError(CutoverCanaryError):
    """Cleanup verification failed; deletion is refused and the journal stays."""


class _ExternallyHeldLock:
    """Null lock: the caller already holds the real exclusive cutover lock."""

    def shared(self, *, timeout_seconds: float = 0.0):
        return contextlib.nullcontext()

    def exclusive(self, *, timeout_seconds: float = 0.0):
        return contextlib.nullcontext()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value)
    )


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _utc_now(clock) -> str:
    moment = clock() if clock is not None else datetime.now(timezone.utc)
    if not isinstance(moment, datetime):
        raise ContextValidationError("clock must return a datetime")
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        raise ContextValidationError("clock must return a timezone-aware datetime")
    return moment.astimezone(timezone.utc).strftime(_TIMESTAMP_FORMAT)


def _write_private_file(path: Path, data: bytes) -> None:
    """0600 same-directory temp, fsync, atomic replace, directory fsync."""
    tmp_name: str | None = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        if tmp_name is not None:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
        raise


@dataclass(frozen=True, slots=True)
class CanaryHandle:
    """In-memory canary handle: private fields, public projection by hashes."""

    SCHEMA: ClassVar[str] = "evolvmem.cutover_canary"
    VERSION: ClassVar[int] = 1

    legacy_id: int
    context_id: int
    key: str
    body: str
    exact_query: str
    cjk_query: str
    nonce: str
    key_sha256: str
    body_sha256: str
    l2_sha256: str

    def __post_init__(self) -> None:
        for name in ("legacy_id", "context_id"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ContextValidationError(f"{name} must be a positive integer")
        for name in ("key", "body", "exact_query", "cjk_query", "nonce"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name):
                raise ContextValidationError(f"{name} must be a non-empty string")
        for name in ("key_sha256", "body_sha256", "l2_sha256"):
            if not _is_sha256(getattr(self, name)):
                raise ContextValidationError(f"{name} must be a lowercase SHA-256 hex")

    def public_dict(self) -> dict:
        """Hashes and IDs only — never the body, key, queries, or paths."""
        public = {
            "schema": self.SCHEMA,
            "version": self.VERSION,
            "state": "prepared",
            "legacy_id": self.legacy_id,
            "context_id": self.context_id,
            "key_sha256": self.key_sha256,
            "body_sha256": self.body_sha256,
            "l2_sha256": self.l2_sha256,
        }
        validate_public_summary(public)
        return public


class CutoverCanary:
    """Creates, probes, and exactly cleans one authorized cutover canary."""

    def __init__(
        self,
        config: Config,
        *,
        store: ContextStore | None = None,
        embedding_engine=None,
        journal_directory,
        journal_name: str = CANARY_JOURNAL_FILENAME,
        externally_locked: bool = False,
        clock=None,
    ) -> None:
        if not isinstance(config, Config):
            raise ContextValidationError("config must be a Config instance")
        if store is not None and not isinstance(store, ContextStore):
            raise ContextValidationError("store must be a ContextStore instance")
        if type(externally_locked) is not bool:
            raise ContextValidationError("externally_locked must be a boolean")
        if (
            not isinstance(journal_name, str)
            or not journal_name
            or Path(journal_name).name != journal_name
            or "\\" in journal_name
        ):
            raise ContextValidationError("journal_name must be a bare filename")
        self.config = config
        self._store = store
        self._owns_store = store is None
        self._engine = embedding_engine
        self.journal_directory = Path(journal_directory)
        self._journal_name = journal_name
        self._externally_locked = externally_locked
        self._clock = clock
        self._service: ContextService | None = None

    # -- lifecycle --

    def _open_service(self) -> ContextService:
        if self._service is None:
            service = ContextService(
                self.config, store=self._store, embedding_engine=self._engine
            )
            if self._externally_locked:
                # The caller holds the real exclusive lock; the per-mutation
                # shared flock would deadlock against it in this process.
                service._cutover_lock = _ExternallyHeldLock()
            service.initialize(mode=ContextMode.SHADOW, adapter="cutover-canary")
            self._service = service
        return self._service

    def close(self) -> None:
        """Close the service only when this canary owns its store."""
        if self._service is not None and self._owns_store:
            self._service.close()
        self._service = None

    @property
    def journal_path(self) -> Path:
        return self.journal_directory / self._journal_name

    # -- preparation --

    def prepare(self, *, authorized: bool = False) -> CanaryHandle:
        """Create the one authorized canary as a dual-write pinned preference.

        Without the explicit ``authorized=True`` the library is never
        touched; an existing canary journal (live or cleaned) is refused so
        canaries never stack.
        """
        if type(authorized) is not bool or not authorized:
            raise CutoverCanaryError("canary_not_authorized")
        if self.journal_path.exists():
            raise CutoverCanaryError("canary_journal_exists")
        if not self.journal_directory.is_dir():
            raise CutoverCanaryError("canary_journal_directory_missing")

        nonce = secrets.token_hex(8)
        cjk_query = "探针" + "".join(
            _CJK_HEX_DIGITS[int(char, 16)] for char in secrets.token_hex(4)
        )
        key = f"cutover:canary:{nonce}"
        # Both probe queries lead the first line so the derived L0 carries
        # them before any sentence terminator.
        body = (
            f"cutover-canary {nonce} {cjk_query}：上下文核心切换验收探针，仅供授权门禁使用\n"
            "本记录由可逆切换门禁创建，用于验证召回链路；门禁通过后被精确删除。"
        )
        service = self._open_service()
        store = service.store
        result = service.legacy_add(
            LegacyAddRequest(
                key=key,
                value=body,
                attribute="preference",
                tags=("cutover-canary", nonce),
                source_session="cutover-canary",
                importance=9.0,
                tier="pinned",
            )
        )
        if result.context_id is None:
            raise CutoverCanaryError("canary_dual_write_missing_context")
        legacy_id = result.legacy_id
        context_id = result.context_id
        try:
            self._record_provenance(store, context_id, nonce)
        except Exception as exc:
            try:
                service.legacy_hard_delete(LegacyHardDeleteRequest(legacy_id=legacy_id))
            except Exception:
                pass  # the prepare error is the one that matters
            raise CutoverCanaryError("canary_provenance_failed") from exc

        l2 = store.get_layer(context_id, ContextLayer.L2)
        if not l2:
            raise CutoverCanaryError("canary_layer_missing")
        handle = CanaryHandle(
            legacy_id=legacy_id,
            context_id=context_id,
            key=key,
            body=body,
            exact_query=nonce,
            cjk_query=cjk_query,
            nonce=nonce,
            key_sha256=_sha256_text(key),
            body_sha256=_sha256_text(body),
            l2_sha256=_sha256_text(l2),
        )
        self._write_journal(handle, state="prepared", cleaned_utc="")
        return handle

    def _record_provenance(
        self, store: ContextStore, context_id: int, nonce: str
    ) -> None:
        """Mark the item with the canary source kind inside one transaction.

        Mirrors ContextStore.record_migration_source so cleanup can prove
        the mapped item is the canary this service created.
        """
        service = self._open_service()
        with service._cutover_lock.shared():
            with store.transaction():
                store._connection().execute(
                    "INSERT INTO context_sources ("
                    "item_id, source_kind, source_ref, extraction_version, created_at"
                    ") VALUES (?, ?, ?, ?, ?)",
                    (
                        context_id,
                        CANARY_SOURCE_KIND,
                        nonce,
                        _EXTRACTION_VERSION,
                        _utc_now(self._clock),
                    ),
                )
                store._connection().execute(
                    "UPDATE context_items SET source_count=("
                    "SELECT COUNT(*) FROM context_sources WHERE item_id=?"
                    ") WHERE id=?",
                    (context_id, context_id),
                )

    def _write_journal(self, handle: CanaryHandle, *, state: str, cleaned_utc: str) -> None:
        payload = {
            "schema": _SCHEMA,
            "version": _VERSION,
            "state": state,
            "nonce": handle.nonce,
            "key": handle.key,
            "body": handle.body,
            "exact_query": handle.exact_query,
            "cjk_query": handle.cjk_query,
            "legacy_id": handle.legacy_id,
            "context_id": handle.context_id,
            "key_sha256": handle.key_sha256,
            "body_sha256": handle.body_sha256,
            "l2_sha256": handle.l2_sha256,
            "created_utc": _utc_now(self._clock),
            "cleaned_utc": cleaned_utc,
        }
        _write_private_file(
            self.journal_path,
            (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )

    # -- probes --

    def probe(self, query: str, *, top_k: int = 5) -> tuple[int, ...]:
        """Core-side ranked IDs for one probe query through the service."""
        results = self._open_service().search(
            ContextSearchRequest(query=query, top_k=top_k)
        )
        return tuple(result.id for result in results)

    # -- cleanup --

    def cleanup(self, handle: CanaryHandle | None = None) -> dict:
        """Verify, then exactly delete the canary on both sides.

        Verification re-reads both IDs, the mapping, the source-kind
        provenance, and the content hash; any mismatch refuses deletion and
        retains the journal. On success absence is proven by exact-ID reads
        and the journal is marked cleaned. Failures keep the journal for
        recovery.
        """
        payload = self._load_journal()
        if payload["state"] != "prepared":
            raise CutoverCanaryError("canary_not_prepared")
        legacy_id = payload["legacy_id"]
        context_id = payload["context_id"]
        if handle is not None and (
            handle.legacy_id != legacy_id or handle.context_id != context_id
        ):
            raise CanaryVerificationError("canary_handle_mismatch")

        service = self._open_service()
        store = service.store
        self._verify_present(store, payload)
        removal = service.legacy_hard_delete(
            LegacyHardDeleteRequest(legacy_id=legacy_id)
        )
        if not removal.changed:
            raise CutoverCanaryError("canary_delete_failed")
        self._verify_absent(service, store, legacy_id, context_id)

        handle_for_journal = handle or CanaryHandle(
            legacy_id=legacy_id,
            context_id=context_id,
            key=payload["key"],
            body=payload["body"],
            exact_query=payload["exact_query"],
            cjk_query=payload["cjk_query"],
            nonce=payload["nonce"],
            key_sha256=payload["key_sha256"],
            body_sha256=payload["body_sha256"],
            l2_sha256=payload["l2_sha256"],
        )
        self._write_journal(
            handle_for_journal, state="cleaned", cleaned_utc=_utc_now(self._clock)
        )
        public = {
            "schema": _SCHEMA,
            "version": _VERSION,
            "cleaned": True,
            "legacy_id": legacy_id,
            "context_id": context_id,
            "key_sha256": payload["key_sha256"],
            "body_sha256": payload["body_sha256"],
            "l2_sha256": payload["l2_sha256"],
        }
        validate_public_summary(public)
        return public

    def _load_journal(self) -> dict:
        try:
            raw = self.journal_path.read_bytes()
        except OSError as exc:
            raise CutoverCanaryError("canary_journal_unreadable") from exc
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CutoverCanaryError("canary_journal_malformed") from exc
        if not isinstance(payload, dict) or payload.get("schema") != _SCHEMA:
            raise CutoverCanaryError("canary_journal_malformed")
        if payload.get("version") != _VERSION:
            raise CutoverCanaryError("canary_journal_malformed")
        for name in ("legacy_id", "context_id"):
            value = payload.get(name)
            if type(value) is not int or value <= 0:
                raise CutoverCanaryError("canary_journal_malformed")
        for name in ("nonce", "key", "body", "exact_query", "cjk_query", "state"):
            if not isinstance(payload.get(name), str):
                raise CutoverCanaryError("canary_journal_malformed")
        for name in ("key_sha256", "body_sha256", "l2_sha256"):
            if not _is_sha256(payload.get(name)):
                raise CutoverCanaryError("canary_journal_malformed")
        return payload

    def _verify_present(self, store: ContextStore, payload: dict) -> None:
        """Re-read and verify every identity fact before any delete."""
        legacy_id = payload["legacy_id"]
        context_id = payload["context_id"]
        repository = store.legacy_projection()
        row = repository.get_by_id(legacy_id)
        if row is None:
            raise CanaryVerificationError("canary_legacy_row_missing")
        if row["key"] != payload["key"]:
            raise CanaryVerificationError("canary_key_mismatch")
        if _sha256_text(row["value"]) != payload["body_sha256"]:
            raise CanaryVerificationError("canary_body_mismatch")
        if store.resolve_legacy_mapping(legacy_id) != context_id:
            raise CanaryVerificationError("canary_mapping_mismatch")
        item = store.get_item(context_id)
        if item is None:
            raise CanaryVerificationError("canary_context_item_missing")
        if (
            item.identity_key != payload["key"]
            or item.status is not ContextStatus.ACTIVE
            or item.tier is not ContextTier.PINNED
            or item.scope is not ContextScope.GLOBAL
            or item.content_type is not ContextContentType.PREFERENCE
        ):
            raise CanaryVerificationError("canary_item_mismatch")
        source = store._connection().execute(
            "SELECT 1 FROM context_sources "
            "WHERE item_id=? AND source_kind=? AND source_ref=?",
            (context_id, CANARY_SOURCE_KIND, payload["nonce"]),
        ).fetchone()
        if source is None:
            raise CanaryVerificationError("canary_source_missing")
        l2 = store.get_layer(context_id, ContextLayer.L2)
        if l2 is None or _sha256_text(l2) != payload["l2_sha256"]:
            raise CanaryVerificationError("canary_content_mismatch")

    def _verify_absent(
        self, service: ContextService, store: ContextStore, legacy_id: int, context_id: int
    ) -> None:
        """Exact-ID absence checks only; never a new similarity search."""
        if store.legacy_projection().get_by_id(legacy_id) is not None:
            raise CutoverCanaryError("canary_absence_verification_failed")
        if store.resolve_legacy_mapping(legacy_id) is not None:
            raise CutoverCanaryError("canary_absence_verification_failed")
        if store.get_item(context_id) is not None:
            raise CutoverCanaryError("canary_absence_verification_failed")
        read = service.read(ContextReadRequest(id=context_id, layer=ContextLayer.L2))
        if read.error_code != "not_found":
            raise CutoverCanaryError("canary_absence_verification_failed")
        for path, removed_id in (
            (self.config.vector_path, legacy_id),
            (self.config.context_vector_path, context_id),
        ):
            if not path.is_file():
                continue  # no derived cache exists; the entry cannot be present
            index = VectorIndex(self.config, path=path)
            try:
                index.initialize(dim=self.config.embedding_dim)
                present = removed_id in index.ids()
            finally:
                index.close()
            if present:
                raise CutoverCanaryError("canary_absence_verification_failed")

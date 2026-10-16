"""Encrypted shared-block archives with lossless legacy reads and safe purge.

Encrypted payloads live as files under ``${data_dir}/session_archives/``
(never as SQLite BLOBs); the symmetric key lives in ``${data_dir}/archive.key``
with owner-only permissions. When the ``cryptography`` backend is unavailable
the archive write fails with a content-free warning and callers continue
without an archive — payloads are never written as plaintext.

Purge is irreversible and never faked: a row only transitions to ``purged``
after its payload file is actually gone from disk; failures keep the row
``available`` so the next sweep retries. Log lines and reports carry archive
ids only — never payload content or absolute paths.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import os
from pathlib import Path

from evolvmem.config import Config
from evolvmem.context_store import ContextStore

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover - exercised in tests via monkeypatch
    AESGCM = None

logger = logging.getLogger(__name__)

_ARCHIVE_DIR_NAME = "session_archives"
_KEY_FILE_NAME = "archive.key"
_KEY_BYTES = 32
_NONCE_BYTES = 12
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


@dataclass(frozen=True)
class SessionArchiveRecord:
    """Public view of one session_archives row; payload_path stays relative."""

    id: int
    project: str
    adapter: str
    external_session_id: str
    payload_path: str
    payload_sha256: str
    state: str
    expires_at: str
    purged_at: str | None
    created_at: str


@dataclass(frozen=True)
class SessionPurgeReport:
    """Outcome of one purge run; carries ids only, never paths or content."""

    purged_archive_ids: tuple[int, ...] = ()
    failed_archive_ids: tuple[int, ...] = ()
    held_archive_ids: tuple[int, ...] = ()


def _coerce_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc)


def _format_ts(moment: datetime) -> str:
    """Match the store's lexical-order UTC timestamp format."""
    return moment.strftime(_TIMESTAMP_FORMAT)


def _row_to_record(row: dict) -> SessionArchiveRecord:
    return SessionArchiveRecord(
        id=int(row["id"]),
        project=str(row["project"]),
        adapter=str(row["adapter"]),
        external_session_id=str(row["external_session_id"]),
        payload_path=str(row["payload_path"]),
        payload_sha256=str(row["payload_sha256"]),
        state=str(row["state"]),
        expires_at=str(row["expires_at"]),
        purged_at=row["purged_at"],
        created_at=str(row["created_at"]),
    )


class SessionArchiver:
    """Owns encrypted payload files and their session_archives metadata rows."""

    def __init__(self, config: Config, store: ContextStore):
        self.config = config
        self.store = store

    @property
    def _archive_dir(self) -> Path:
        return self.config.data_dir / _ARCHIVE_DIR_NAME

    @property
    def _key_path(self) -> Path:
        return self.config.data_dir / _KEY_FILE_NAME

    def _storage(self):
        from evolvmem.archive_storage import ArchiveStorage
        return ArchiveStorage(self.config, self._load_or_create_key())

    def _register_storage(self, archive_id, blob, manifest):
        self.store._connection().execute(
            'INSERT INTO session_archive_storage VALUES(?,?,?,?) '
            'ON CONFLICT(archive_id) DO UPDATE SET manifest_sha256=excluded.manifest_sha256,'
            'content_sha256=excluded.content_sha256,logical_bytes=excluded.logical_bytes',
            (archive_id, hashlib.sha256(blob).hexdigest(), manifest['sha256'], manifest['size']))

    def _remove_superseded(self, previous, target):
        if previous is not None and previous['payload_path'] != target:
            try:
                (self.config.data_dir / previous['payload_path']).unlink(missing_ok=True)
            except OSError:
                logger.warning('session archive %d superseded payload cleanup failed', previous['id'])

    # ---- archiving ----

    def archive_session(
        self,
        project: str,
        adapter: str,
        external_session_id: str,
        payload: str,
        *,
        now: datetime | None = None,
    ) -> SessionArchiveRecord | None:
        """Encrypt and archive one raw session payload; None when unavailable.

        Shared blocks and the encrypted manifest are written under the same
        SQLite writer transaction as their pointer, serializing collection.
        A DB failure removes the manifest; unreferenced blocks are collected.
        Re-archiving the same (adapter, external_session_id) replaces the
        payload and expiry instead of creating a second row.
        """
        if AESGCM is None:
            logger.warning(
                "session archive skipped: encryption backend unavailable"
            )
            return None
        moment = _coerce_now(now)
        try:
            storage = self._storage()
            self._archive_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self._archive_dir, 0o700)
        except Exception:
            logger.warning("session archive skipped: encryption failed")
            return None
        expires_at = _format_ts(
            moment + timedelta(days=self.config.context_archive_ttl_days)
        )
        written = None
        try:
            with self.store.transaction():
                from evolvmem.archive_storage import StorageSpaceLow
                try:
                    blob, manifest = storage.pack(payload)
                    digest = hashlib.sha256(blob).hexdigest()
                    relative_path = f"{_ARCHIVE_DIR_NAME}/{digest}.bin"
                    target = self.config.data_dir / relative_path
                    self._write_payload_file(target, blob)
                    written = target
                except StorageSpaceLow:
                    logger.warning('session archive deferred: archive_storage_space_low')
                    return None
                except Exception:
                    logger.warning('session archive skipped: encrypted payload write failed')
                    return None
                previous = self.store.get_session_archive_by_external(adapter, external_session_id)
                archive_id = self.store.upsert_session_archive(
                    project,
                    adapter,
                    external_session_id,
                    payload_path=relative_path,
                    payload_sha256=digest,
                    expires_at=expires_at,
                    recorded_at=_format_ts(moment),
                )
                self._register_storage(archive_id, blob, manifest)
                from evolvmem.history_memory import save_clean
                from evolvmem.knowledge_rules import KnowledgeRules
                save_clean(self.store, archive_id, payload, policy=KnowledgeRules(self.config.data_dir).read())
                self.store.defer_until_commit(lambda: self._remove_superseded(previous, relative_path))
        except Exception:
            if written is not None:
                written.unlink(missing_ok=True)
            raise

        row = self.store.get_session_archive(archive_id)
        if row is None:  # pragma: no cover - the upsert just wrote this row
            raise RuntimeError("archived session row could not be loaded")
        return _row_to_record(row)

    def read_payload(self, archive_id: int) -> str | None:
        """Decrypt one available archive's payload; None when not readable."""
        if AESGCM is None:
            logger.warning(
                "session archive read skipped: encryption backend unavailable"
            )
            return None
        row = self.store.get_session_archive(archive_id)
        if row is None or row["state"] != "available":
            return None
        try:
            blob = (self.config.data_dir / row["payload_path"]).read_bytes()
            return self._storage().unpack(blob)
        except Exception:
            logger.warning(
                "session archive %d payload could not be decrypted", archive_id
            )
            return None

    # ---- purge ----

    def sweep_expired(self, *, now: datetime | None = None) -> SessionPurgeReport:
        """Purge every available archive whose TTL has been reached.

        Knowledge, review, holds, current uploads and pending work are
        protected. This explicit sweep is not used by automatic upkeep.
        """
        moment = _format_ts(_coerce_now(now))
        rows = self.store.list_expired_session_archives(moment)
        from evolvmem.archive_maintenance import protected_archive_ids
        with self.store.transaction():
            protected = protected_archive_ids(self.store)
            held = tuple(row['id'] for row in rows if row['id'] in protected)
            report = self._purge_rows([row for row in rows if row['id'] not in protected], purged_at=moment)
        return SessionPurgeReport(report.purged_archive_ids, report.failed_archive_ids,
                                  held + report.held_archive_ids)

    def purge_project(self, project: str) -> SessionPurgeReport:
        """Immediately purge all available archives of one project.

        ContextItems are never deleted; only their source_state is recomputed.
        Held archives (``session_archive_holds``) are skipped exactly like in
        the TTL sweep and reported as held.
        """
        rows = self.store.list_available_project_archives(project)
        return self._purge_rows(rows, purged_at=_format_ts(_coerce_now(None)))

    def _purge_rows(self, rows: list[dict], *, purged_at: str) -> SessionPurgeReport:
        purged: list[int] = []
        failed: list[int] = []
        held: list[int] = []
        held_ids = {
            int(row["archive_id"])
            for row in self.store._connection()
            .execute("SELECT archive_id FROM session_archive_holds")
            .fetchall()
        }
        for row in rows:
            archive_id = int(row["id"])
            if archive_id in held_ids:
                # 覆盖门控：摘要尚未进入滚动摘要闭包的归档不得 purge
                held.append(archive_id)
                continue
            try:
                with self.store.transaction():
                    current = self.store.get_session_archive(archive_id)
                    if (not current or current['state'] != 'available'
                            or current['payload_path'] != row['payload_path']):
                        continue
                    if self.store._connection().execute(
                            'SELECT 1 FROM session_archive_holds WHERE archive_id=?', (archive_id,)).fetchone():
                        held.append(archive_id)
                        continue
                    (self.config.data_dir / current['payload_path']).unlink(missing_ok=True)
                    self.store.mark_session_archive_purged(
                        archive_id, purged_at=purged_at
                    )
                    item_ids = [
                        int(source["item_id"])
                        for source in self.store.list_archive_sources(archive_id)
                    ]
                    self.store.recompute_source_states(item_ids)
            except Exception:
                logger.warning(
                    "session archive %d purge deferred: removal or state update failed",
                    archive_id,
                )
                failed.append(archive_id)
                continue
            purged.append(archive_id)
        return SessionPurgeReport(
            purged_archive_ids=tuple(purged),
            failed_archive_ids=tuple(failed),
            held_archive_ids=tuple(held),
        )

    # ---- key and payload files ----

    def _load_or_create_key(self) -> bytes:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        path = self._key_path
        try:
            fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            key = path.read_bytes()
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(os.urandom(_KEY_BYTES))
                handle.flush()
                os.fsync(handle.fileno())
            from evolvmem.archive_storage import sync_directory
            sync_directory(path.parent)
            key = path.read_bytes()
        if len(key) != _KEY_BYTES:
            raise RuntimeError("session archive key file is corrupt")
        return key

    @staticmethod
    def _write_payload_file(target: Path, blob: bytes) -> None:
        fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            from evolvmem.archive_storage import sync_directory
            sync_directory(target.parent)
        except Exception:
            target.unlink(missing_ok=True)
            raise

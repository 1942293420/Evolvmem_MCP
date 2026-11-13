"""Unified HistoryReader protocol & strictly read-only SQLite adapter (Stage 3 -> Stage 5 swap)."""
from __future__ import annotations

import ast
from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Protocol, Sequence, runtime_checkable

from evolvmem.archive_storage import ArchiveStorage
from evolvmem.config import Config

_ARCHIVE_REF_RE = re.compile(r'^archive:([1-9][0-9]*)\Z')
_BLOCK_REF_RE = re.compile(r'^block:([A-Za-z0-9_.:-]+)\Z')


@dataclass(frozen=True)
class HistoryRawRecord:
    """Single 1-based record in a historical session (SQLite or PG)."""

    event_index: int
    role: str
    content: str
    raw_event: dict[str, Any] | None = None


@dataclass(frozen=True)
class HistorySessionSummary:
    """Metadata header describing one readable session source."""

    session_id: str
    adapter: str
    source_format: str
    project: str
    source_ref: str
    library: str
    external_session_id: str
    created_at: str
    has_raw_bytes: bool = False


@dataclass(frozen=True)
class HistorySession:
    """Full read-only session payload handed to the deterministic event parser."""

    summary: HistorySessionSummary
    records: tuple[HistoryRawRecord, ...]
    raw_bytes: bytes | None = None


@runtime_checkable
class HistoryReader(Protocol):
    """Source-agnostic history interface (SQLite in Stage 3, PG in Stage 5)."""

    def list_sessions(
        self,
        *,
        project: str | None = None,
        adapter: str | None = None,
        since: str | None = None,
        limit: int = 100,
        offset: int = 0,
        dedupe_logical_sessions: bool = True,
    ) -> list[HistorySessionSummary]: ...

    def get_session(self, source_ref: str) -> HistorySession | None: ...

    def verify_evidence_pointer(self, pointer: dict[str, Any]) -> bool: ...


class InMemoryHistoryReader:
    """Deterministic in-memory implementation mirroring Stage 5 PgHistoryReader contract."""

    def __init__(self, sessions: Sequence[HistorySession] = ()):
        self._sessions = {s.summary.source_ref: s for s in sessions}
        self._by_id = {s.summary.session_id: s for s in sessions}

    def list_sessions(
        self,
        *,
        project: str | None = None,
        adapter: str | None = None,
        since: str | None = None,
        limit: int = 100,
        offset: int = 0,
        dedupe_logical_sessions: bool = True,
    ) -> list[HistorySessionSummary]:
        items = [s.summary for s in self._sessions.values()]
        if project is not None:
            items = [s for s in items if s.project == project]
        if adapter is not None:
            items = [s for s in items if s.adapter == adapter]
        if since is not None:
            items = [s for s in items if s.created_at >= since]
        items.sort(key=lambda s: (s.created_at, s.source_ref), reverse=True)
        return items[max(0, offset):max(0, offset) + max(0, limit)]

    def get_session(self, source_ref: str) -> HistorySession | None:
        return self._sessions.get(source_ref) or self._by_id.get(source_ref)

    def verify_evidence_pointer(self, pointer: dict[str, Any]) -> bool:
        if not isinstance(pointer, dict):
            return False
        src = str(pointer.get('source') or '')
        ev = pointer.get('event')
        if type(ev) is not int or ev < 1:
            return False
        sess = self.get_session(src)
        return sess is not None and 1 <= ev <= len(sess.records)


class SqliteHistoryReader:
    """Strictly read-only SQLite adapter (?mode=ro + PRAGMA query_only=ON)."""

    def __init__(
        self,
        *,
        memory_db: Path | str | None = None,
        candidate_db: Path | str | None = None,
        data_dir: Path | str | None = None,
        library: str = 'personal',
    ):
        self.memory_db = Path(memory_db).expanduser() if memory_db else None
        self.candidate_db = Path(candidate_db).expanduser() if candidate_db else None
        if data_dir is not None:
            self.data_dir: Path | None = Path(data_dir).expanduser()
        elif self.memory_db is not None:
            self.data_dir = self.memory_db.parent
        else:
            self.data_dir = None
        self.library = str(library or 'personal')

    @contextmanager
    def _connect(self, db_path: Path):
        uri = f'file:{db_path.resolve()}?mode=ro'
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute('PRAGMA query_only = ON')
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _infer_format(adapter: str, upload_fmt: str | None) -> str:
        if upload_fmt:
            return str(upload_fmt)
        return {'codex': 'codex-jsonl', 'dsh': 'dsh-session-jsonl-v1', 'kimi': 'kimi-wire-jsonl', 'claude': 'claude-jsonl'}.get(adapter, 'messages')

    def _decrypt_archive_bytes(self, rel_path: str | None) -> bytes | None:
        if not rel_path or self.data_dir is None:
            return None
        key_path = self.data_dir / 'archive.key'
        blob_path = self.data_dir / rel_path
        if not key_path.is_file() or not blob_path.is_file():
            return None
        try:
            key = key_path.read_bytes()
            if len(key) != 32:
                return None
            storage = ArchiveStorage(Config(data_dir=self.data_dir, apply_environment=False), key)
            unpacked = storage.unpack(blob_path.read_bytes())
            return unpacked.encode('utf-8')
        except Exception:
            return None

    def _read_archive_session(self, conn, archive_id: int) -> HistorySession | None:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if 'session_archives' not in tables:
            return None
        row = conn.execute(
            'SELECT id, project, adapter, external_session_id, payload_path, state, created_at '
            'FROM session_archives WHERE id = ?',
            (archive_id,),
        ).fetchone()
        if row is None:
            return None

        upload_fmt = None
        if 'lan_session_uploads' in tables:
            uf = conn.execute(
                'SELECT source_format FROM lan_session_uploads WHERE archive_id = ? LIMIT 1',
                (archive_id,),
            ).fetchone()
            if uf and uf[0]:
                upload_fmt = str(uf[0])

        raw_bytes = self._decrypt_archive_bytes(row['payload_path']) if row['state'] == 'available' else None
        envelope_conv = None
        if raw_bytes is not None:
            stripped = raw_bytes.lstrip()
            if stripped.startswith(b'{') and b'\n' not in raw_bytes.strip():
                try:
                    env = json.loads(raw_bytes.decode('utf-8', errors='replace'))
                    if isinstance(env, dict) and isinstance(env.get('transcript'), str):
                        if env.get('source_format'):
                            upload_fmt = str(env['source_format'])
                        envelope_conv = env.get('conversation') or env.get('messages')
                        raw_bytes = env['transcript'].encode('utf-8')
                except ValueError:
                    pass
        records: list[HistoryRawRecord] = []
        if 'conversation_history' in tables:
            ch = conn.execute(
                'SELECT messages FROM conversation_history WHERE archive_id = ?',
                (archive_id,),
            ).fetchone()
            if ch and ch['messages']:
                try:
                    msgs = json.loads(ch['messages'])
                    if isinstance(msgs, list):
                        for idx, msg in enumerate(msgs, 1):
                            if isinstance(msg, dict):
                                records.append(
                                    HistoryRawRecord(
                                        event_index=idx,
                                        role=str(msg.get('role') or 'unknown'),
                                        content=str(msg.get('content') or ''),
                                        raw_event=msg,
                                    )
                                )
                except ValueError:
                    pass

        if not records and isinstance(envelope_conv, list):
            for idx, msg in enumerate(envelope_conv, 1):
                if isinstance(msg, dict):
                    records.append(HistoryRawRecord(idx, str(msg.get('role') or 'unknown'), str(msg.get('content') or ''), msg))

        if not records and raw_bytes is not None:
            try:
                text = raw_bytes.decode('utf-8', errors='replace')
                if text.lstrip().startswith('{'):
                    maybe = json.loads(text)
                    if isinstance(maybe, dict) and isinstance(maybe.get('messages'), list):
                        for idx, msg in enumerate(maybe['messages'], 1):
                            if isinstance(msg, dict):
                                records.append(HistoryRawRecord(idx, str(msg.get('role') or 'unknown'), str(msg.get('content') or ''), msg))
            except ValueError:
                pass
            if not records:
                for idx, line in enumerate(raw_bytes.splitlines(), 1):
                    try:
                        obj = json.loads(line.decode('utf-8', errors='replace'))
                        if isinstance(obj, dict):
                            records.append(HistoryRawRecord(idx, str(obj.get('type') or 'event'), json.dumps(obj, ensure_ascii=False), obj))
                    except ValueError:
                        continue

        adapter = str(row['adapter'])
        summary = HistorySessionSummary(
            session_id=f'archive:{archive_id}',
            adapter=adapter,
            source_format=self._infer_format(adapter, upload_fmt),
            project=str(row['project'] or ''),
            source_ref=f'archive:{archive_id}',
            library=self.library,
            external_session_id=str(row['external_session_id'] or ''),
            created_at=str(row['created_at'] or ''),
            has_raw_bytes=raw_bytes is not None,
        )
        return HistorySession(summary=summary, records=tuple(records), raw_bytes=raw_bytes)

    def _read_candidate_block(self, conn, block_id: str) -> HistorySession | None:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if 'blocks' not in tables or 'messages' not in tables:
            return None
        row = conn.execute(
            'SELECT block_id, adapter, native_session_id, observed_first_at, projects FROM blocks WHERE block_id = ?',
            (block_id,),
        ).fetchone()
        if row is None:
            return None
        projects_raw = row['projects'] or '[]'
        try:
            projects = json.loads(projects_raw)
        except ValueError:
            projects = []
        project = str(projects[0]) if isinstance(projects, list) and projects else ''
        msg_rows = conn.execute(
            'SELECT ordinal, role, content, recorded_at, archive_id, message_index FROM messages WHERE block_id = ? ORDER BY ordinal ASC',
            (block_id,),
        ).fetchall()
        records = tuple(
            HistoryRawRecord(
                event_index=int(r['ordinal'] or idx),
                role=str(r['role'] or 'unknown'),
                content=str(r['content'] or ''),
                raw_event={'role': r['role'], 'content': r['content'], 'recorded_at': r['recorded_at'], 'archive_id': r['archive_id'], 'message_index': r['message_index']},
            )
            for idx, r in enumerate(msg_rows, 1)
        )
        adapter = str(row['adapter'] or 'unknown')
        summary = HistorySessionSummary(
            session_id=f'block:{block_id}',
            adapter=adapter,
            source_format=self._infer_format(adapter, 'messages'),
            project=project,
            source_ref=f'block:{block_id}',
            library=self.library,
            external_session_id=str(row['native_session_id'] or block_id),
            created_at=str(row['observed_first_at'] or ''),
            has_raw_bytes=False,
        )
        return HistorySession(summary=summary, records=records, raw_bytes=None)

    def list_sessions(
        self,
        *,
        project: str | None = None,
        adapter: str | None = None,
        since: str | None = None,
        limit: int = 100,
        offset: int = 0,
        dedupe_logical_sessions: bool = True,
    ) -> list[HistorySessionSummary]:
        results: list[HistorySessionSummary] = []
        if self.memory_db and self.memory_db.is_file():
            with self._connect(self.memory_db) as conn:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                has_key = bool(self.data_dir and (self.data_dir / 'archive.key').is_file())
                if 'session_archives' in tables and (has_key or 'conversation_history' in tables):
                    has_uploads = 'lan_session_uploads' in tables
                    clauses = ["sa.state = 'available'"]
                    params: list[Any] = []
                    if not has_key and 'conversation_history' in tables:
                        clauses.append('EXISTS (SELECT 1 FROM conversation_history ch WHERE ch.archive_id = sa.id)')
                    if dedupe_logical_sessions:
                        if has_uploads:
                            clauses.append(
                                "sa.id IN (SELECT MAX(sa2.id) FROM session_archives sa2 "
                                "LEFT JOIN lan_session_uploads lsu2 ON lsu2.archive_id = sa2.id "
                                "WHERE sa2.state = 'available' "
                                "GROUP BY COALESCE(NULLIF(lsu2.session_id, ''), sa2.external_session_id))"
                            )
                        else:
                            clauses.append(
                                "sa.id IN (SELECT MAX(sa2.id) FROM session_archives sa2 "
                                "WHERE sa2.state = 'available' GROUP BY sa2.external_session_id)"
                            )
                    if project is not None:
                        clauses.append('sa.project = ?')
                        params.append(project)
                    if adapter is not None:
                        clauses.append('sa.adapter = ?')
                        params.append(adapter)
                    if since is not None:
                        clauses.append('sa.created_at >= ?')
                        params.append(since)
                    where = ' AND '.join(clauses)
                    join_sql = 'LEFT JOIN lan_session_uploads lsu ON lsu.archive_id = sa.id ' if has_uploads else ''
                    fmt_col = 'lsu.source_format AS upload_fmt' if has_uploads else 'NULL AS upload_fmt'
                    rows = conn.execute(
                        f'SELECT sa.id, sa.project, sa.adapter, sa.external_session_id, sa.payload_path, sa.created_at, {fmt_col} '
                        f'FROM session_archives sa {join_sql}'
                        f'WHERE {where} ORDER BY sa.created_at DESC, sa.id DESC LIMIT ? OFFSET ?',
                        (*params, max(0, int(limit)), max(0, int(offset))),
                    ).fetchall()
                    for r in rows:
                        adp = str(r['adapter'])
                        results.append(
                            HistorySessionSummary(
                                session_id=f"archive:{r['id']}",
                                adapter=adp,
                                source_format=self._infer_format(adp, r['upload_fmt']),
                                project=str(r['project'] or ''),
                                source_ref=f"archive:{r['id']}",
                                library=self.library,
                                external_session_id=str(r['external_session_id'] or ''),
                                created_at=str(r['created_at'] or ''),
                                has_raw_bytes=bool(has_key and r['payload_path']),
                            )
                        )
        if not results and self.candidate_db and self.candidate_db.is_file():
            with self._connect(self.candidate_db) as conn:
                clauses = ['message_count > 0']
                params = []
                if adapter is not None:
                    clauses.append('adapter = ?')
                    params.append(adapter)
                if since is not None:
                    clauses.append('observed_first_at >= ?')
                    params.append(since)
                rows = conn.execute(
                    f"SELECT block_id, adapter, native_session_id, observed_first_at, projects FROM blocks "
                    f"WHERE {' AND '.join(clauses)} ORDER BY observed_first_at DESC, block_id DESC LIMIT ? OFFSET ?",
                    (*params, max(0, int(limit) * 3 if project else int(limit)), max(0, int(offset))),
                ).fetchall()
                for r in rows:
                    try:
                        projs = json.loads(r['projects'] or '[]')
                    except ValueError:
                        projs = []
                    proj = str(projs[0]) if isinstance(projs, list) and projs else ''
                    if project is not None and project not in projs:
                        continue
                    adp = str(r['adapter'] or 'unknown')
                    results.append(
                        HistorySessionSummary(
                            session_id=f"block:{r['block_id']}",
                            adapter=adp,
                            source_format=self._infer_format(adp, 'messages'),
                            project=proj,
                            source_ref=f"block:{r['block_id']}",
                            library=self.library,
                            external_session_id=str(r['native_session_id'] or r['block_id']),
                            created_at=str(r['observed_first_at'] or ''),
                            has_raw_bytes=False,
                        )
                    )
                    if len(results) >= limit:
                        break
        return results

    def get_session(self, source_ref: str) -> HistorySession | None:
        m_arch = _ARCHIVE_REF_RE.fullmatch(str(source_ref or ''))
        if m_arch:
            arch_id = int(m_arch.group(1))
            if self.memory_db and self.memory_db.is_file():
                with self._connect(self.memory_db) as conn:
                    res = self._read_archive_session(conn, arch_id)
                    if res is not None:
                        return res
            if self.candidate_db and self.candidate_db.is_file():
                with self._connect(self.candidate_db) as conn:
                    row = conn.execute('SELECT block_id FROM messages WHERE archive_id = ? LIMIT 1', (arch_id,)).fetchone()
                    if row:
                        return self._read_candidate_block(conn, str(row[0]))
            return None

        m_blk = _BLOCK_REF_RE.fullmatch(str(source_ref or ''))
        if m_blk and self.candidate_db and self.candidate_db.is_file():
            with self._connect(self.candidate_db) as conn:
                return self._read_candidate_block(conn, m_blk.group(1))
        return None

    def verify_evidence_pointer(self, pointer: dict[str, Any]) -> bool:
        if not isinstance(pointer, dict):
            return False
        src = str(pointer.get('source') or '')
        ev = pointer.get('event')
        if type(ev) is not int or ev < 1:
            return False
        m_arch = _ARCHIVE_REF_RE.fullmatch(src)
        if m_arch and self.candidate_db and self.candidate_db.is_file():
            arch_id = int(m_arch.group(1))
            with self._connect(self.candidate_db) as conn:
                row = conn.execute(
                    'SELECT 1 FROM messages WHERE archive_id = ? AND message_index = ? LIMIT 1',
                    (arch_id, ev),
                ).fetchone()
                if row is not None:
                    return True
        sess = self.get_session(src)
        return sess is not None and any(r.event_index == ev for r in sess.records)


def scan_direct_sqlite_in_pipeline(pipeline_dir: Path) -> list[dict[str, Any]]:
    """Ensure pipeline modules only read history via HistoryReader, never raw sqlite3."""
    violations: list[dict[str, Any]] = []
    for path in sorted(Path(pipeline_dir).rglob('*.py')):
        if path.name == 'history_reader.py' or '__pycache__' in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(a.name == 'sqlite3' for a in node.names):
                violations.append({'file': str(path), 'line': node.lineno, 'kind': 'sqlite3_import'})
            elif isinstance(node, ast.ImportFrom) and node.module == 'sqlite3':
                violations.append({'file': str(path), 'line': node.lineno, 'kind': 'sqlite3_import'})
    return violations

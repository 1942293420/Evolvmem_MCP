"""PostgreSQL adapter for personal experience_notes replacing SQLite manual tables.

All writes to experience_notes route through ExperienceWriter.apply() with a
user_fast AdmissionTicket. Reads never fall back to SQLite when experience_v2 is active.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import uuid

import psycopg

from evolvmem.experience_v2.control.migrations import loopback_psycopg_connector
from evolvmem.experience_v2.control.pg_console_store import (
    PgControlConfig,
    PgControlUnavailableError,
)
from evolvmem.experience_v2.writer import (
    CATEGORIES,
    ExperienceWriteError,
    ExperienceWriter,
    Op,
    TicketIssuer,
)
from evolvmem.library_startup import FileSecretResolver
from evolvmem.manual_experience_search import MAX_DOCUMENTS

FIELDS = ('category', 'problem', 'solution', 'reusable_experience')
PG_NOTE_ID = re.compile(r'(?:manual-[a-f0-9]{32}|exp-[0-9a-z]{26})\Z')
_SCHEMA_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')
_PROCESS_TICKET_SECRET = secrets.token_bytes(32)
_SELECT_MANUAL = (
    'id, category, problem, solution, reusable_experience, '
    'revision, deleted_at, created_at, updated_at'
)


class PgExperienceUnavailableError(RuntimeError):
    """Raised when PG experience store is required but unconfigured or unreachable."""

    code = 'pg_experience_unavailable'
    status = 503


@dataclass(frozen=True, slots=True)
class PgExperienceSettings:
    dsn: str | None = None
    schema: str | None = None
    control_config: PgControlConfig | None = None
    ticket_secret: bytes = _PROCESS_TICKET_SECRET


def _json_canon(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _iso_sec(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec='seconds')
    return str(value)


def _format_manual_note(note: dict) -> dict:
    return {
        'id': note['id'],
        'category': note['category'],
        'problem': note['problem'],
        'solution': note['solution'],
        'reusable_experience': note['reusable_experience'],
        'revision': int(note['revision']),
        'deleted_at': _iso_sec(note.get('deleted_at')),
        'created_at': _iso_sec(note.get('created_at')),
        'updated_at': _iso_sec(note.get('updated_at')),
        'origin': 'manual',
    }


def _row_to_manual_dict(row: tuple, *, include_keyword: bool = False) -> dict:
    keys = ('id', 'category', 'problem', 'solution', 'reusable_experience',
            'revision', 'deleted_at', 'created_at', 'updated_at')
    data = dict(zip(keys, row[:9]))
    for ts in ('deleted_at', 'created_at', 'updated_at'):
        data[ts] = _iso_sec(data[ts])
    data['origin'] = 'manual'
    if include_keyword and len(row) > 9:
        data['keyword_match'] = bool(row[9])
    return data


def _summary() -> dict:
    return {'categories': dict.fromkeys(CATEGORIES, 0), 'total': 0, 'deleted_total': 0}


def _read_secret_file(path: str | Path) -> bytes:
    try:
        raw = Path(path).expanduser().read_text(encoding='utf-8').strip().encode('utf-8')
        if not raw:
            raise ValueError('empty_secret')
        return raw
    except Exception:
        raise PgExperienceUnavailableError('pg_experience_ticket_secret_unavailable') from None


def load_pg_experience_settings(environ=None) -> PgExperienceSettings:
    env = os.environ if environ is None else environ
    secret = _PROCESS_TICKET_SECRET
    secret_file = (env.get('EVOLVMEM_EXPERIENCE_TICKET_SECRET_FILE') or '').strip()
    if secret_file:
        secret = _read_secret_file(secret_file)

    dsn = (env.get('EVOLVMEM_EXPERIENCE_PG_DSN') or '').strip()
    schema = (env.get('EVOLVMEM_EXPERIENCE_PG_SCHEMA') or '').strip() or None
    if schema is not None and not _SCHEMA_RE.fullmatch(schema):
        raise PgExperienceUnavailableError('invalid_pg_schema')
    if dsn:
        return PgExperienceSettings(dsn=dsn, schema=schema, ticket_secret=secret)

    cfg_path = (env.get('EVOLVMEM_EXPERIENCE_PG_CONFIG') or env.get('EVOLVMEM_CONSOLE_PG_CONFIG') or '').strip()
    if cfg_path:
        try:
            cfg = PgControlConfig.from_file(cfg_path)
        except PgControlUnavailableError:
            raise PgExperienceUnavailableError('pg_experience_config_unavailable') from None
        if not secret_file and 'experience_ticket' in cfg.secret_files:
            resolver = FileSecretResolver(cfg.secret_files)
            try:
                resolved = resolver('experience_ticket').strip().encode('utf-8')
                if not resolved:
                    raise ValueError('empty_secret')
                secret = resolved
            except Exception:
                raise PgExperienceUnavailableError('pg_experience_ticket_secret_unavailable') from None
        return PgExperienceSettings(control_config=cfg, ticket_secret=secret)

    raise PgExperienceUnavailableError('pg_experience_unconfigured')


@contextmanager
def open_experience_conn(settings: PgExperienceSettings | None = None, *, role: str = 'coordinator'):
    cfg = settings or load_pg_experience_settings()
    try:
        if cfg.dsn:
            kwargs = {'connect_timeout': 2}
            if cfg.schema:
                kwargs['options'] = f'-c search_path={cfg.schema},public'
            conn = psycopg.connect(cfg.dsn, **kwargs)
        elif cfg.control_config is not None:
            cc = cfg.control_config
            connector = loopback_psycopg_connector(allow_loopback_disable_ssl=cc.allow_loopback_disable_ssl)
            resolver = FileSecretResolver(cc.secret_files)
            user = cc.reader_role if role == 'reader' else cc.coordinator_role
            ref = cc.reader_password_ref if role == 'reader' else cc.coordinator_password_ref
            conn = connector(
                host=cc.host,
                port=cc.port,
                dbname=cc.database,
                user=user,
                password=resolver(ref),
                connect_timeout=2,
            )
        else:
            raise PgExperienceUnavailableError('pg_experience_unconfigured')
    except PgExperienceUnavailableError:
        raise
    except Exception:
        raise PgExperienceUnavailableError('pg_experience_unavailable') from None

    try:
        lock_ms = int(cfg.control_config.lock_timeout_ms) if cfg.control_config is not None else 500
        stmt_ms = int(cfg.control_config.statement_timeout_ms) if cfg.control_config is not None else 2000
        with conn:
            conn.execute(f'SET lock_timeout = {lock_ms}')
            conn.execute(f'SET statement_timeout = {stmt_ms}')
            yield conn
    except PgExperienceUnavailableError:
        raise
    except (ExperienceWriteError, ValueError):
        raise
    except psycopg.Error:
        raise PgExperienceUnavailableError('pg_experience_unavailable') from None
    finally:
        try:
            conn.close()
        except Exception:
            pass


def check_available(settings: PgExperienceSettings | None = None) -> None:
    """Fail closed with PgExperienceUnavailableError unless PG schema is ready."""
    with open_experience_conn(settings, role='reader') as conn:
        row = conn.execute("SELECT to_regclass('experience_notes')").fetchone()
        if not row or row[0] is None:
            raise PgExperienceUnavailableError('pg_experience_schema_missing')


class PgExperienceStore:
    """Drop-in PG backend for manual_experiences read/write contracts."""

    def __init__(self, settings: PgExperienceSettings | None = None):
        self.settings = settings

    def _settings(self) -> PgExperienceSettings:
        return self.settings or load_pg_experience_settings()

    def read(self, source, params, *, category: str, query: str, page: int, page_size: int,
             deleted: int, item_id=None, search=None) -> dict:
        if item_id is not None and (not isinstance(item_id, str) or not PG_NOTE_ID.fullmatch(item_id)):
            raise ExperienceWriteError('experience_not_found', 404)
        if not source or not source.get('user_id'):
            if item_id is not None:
                raise ExperienceWriteError('experience_not_found', 404)
            return {
                'ok': True, 'connected': False, 'experience_connected': False,
                'origin': 'manual', 'rows': [], 'total': 0, 'page': page,
                'page_size': page_size, 'summary': _summary(), 'reason': 'experience_not_connected',
            }

        owner = source['user_id']
        result = {
            'ok': True, 'connected': True, 'experience_connected': True,
            'origin': 'manual', 'rows': [], 'total': 0, 'page': page,
            'page_size': page_size, 'summary': _summary(),
        }
        del_cond = 'deleted_at IS NOT NULL' if deleted else 'deleted_at IS NULL'
        base_where = f"owner_id = %s AND origin = 'manual' AND status = 'active' AND {del_cond}"

        with open_experience_conn(self._settings(), role='reader') as conn:
            if item_id is not None:
                row = conn.execute(
                    f'SELECT {_SELECT_MANUAL} FROM experience_notes WHERE {base_where} AND id = %s',
                    (owner, item_id),
                ).fetchone()
                if row is None:
                    raise ExperienceWriteError('experience_not_found', 404)
                return {
                    'ok': True, 'connected': True, 'experience_connected': True,
                    'origin': 'manual', 'experience': _row_to_manual_dict(row),
                }

            totals = conn.execute(
                """
                SELECT
                    COALESCE(SUM(CASE WHEN deleted_at IS NULL THEN 1 ELSE 0 END), 0),
                    COALESCE(SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END), 0)
                FROM experience_notes
                WHERE owner_id = %s AND origin = 'manual' AND status = 'active'
                """,
                (owner,),
            ).fetchone()
            result['summary']['total'] = int(totals[0] or 0)
            result['summary']['deleted_total'] = int(totals[1] or 0)

            where = base_where
            values: list = [owner]
            if query:
                result['search_mode'] = 'keyword'
                literal = '%' + query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%'
                match = "(problem ILIKE %s ESCAPE '\\' OR solution ILIKE %s ESCAPE '\\' OR reusable_experience ILIKE %s ESCAPE '\\')"
                if search is not None and not deleted and any(char.isalnum() for char in query):
                    candidates = conn.execute(
                        f'SELECT {_SELECT_MANUAL}, {match} AS keyword_match FROM experience_notes '
                        f'WHERE {where} ORDER BY updated_at DESC, id DESC LIMIT %s',
                        [literal, literal, literal, *values, MAX_DOCUMENTS + 1],
                    ).fetchall()
                    if candidates:
                        wrapped = [_row_to_manual_dict(r, include_keyword=True) for r in candidates]
                        ranked = search.rank(('pg', owner), query, wrapped)
                        if ranked is not None:
                            result['search_mode'] = 'hybrid'
                            visible = []
                            for row in ranked:
                                if row['category'] in CATEGORIES:
                                    result['summary']['categories'][row['category']] += 1
                                if not category or row['category'] == category:
                                    visible.append({k: v for k, v in row.items() if k != 'keyword_match'})
                            offset = (page - 1) * page_size
                            result['total'] = len(visible)
                            result['rows'] = visible[offset:offset + page_size]
                            return result
                where += f' AND {match}'
                values.extend([literal, literal, literal])

            for cat, cnt in conn.execute(
                f'SELECT category, count(*) FROM experience_notes WHERE {where} GROUP BY category',
                values,
            ).fetchall():
                if cat in CATEGORIES:
                    result['summary']['categories'][cat] = int(cnt)

            if category:
                where += ' AND category = %s'
                values.append(category)

            total_row = conn.execute(f'SELECT count(*) FROM experience_notes WHERE {where}', values).fetchone()
            result['total'] = int(total_row[0] if total_row else 0)
            rows = conn.execute(
                f'SELECT {_SELECT_MANUAL} FROM experience_notes WHERE {where} '
                'ORDER BY updated_at DESC, id DESC LIMIT %s OFFSET %s',
                [*values, page_size, (page - 1) * page_size],
            ).fetchall()
            result['rows'] = [_row_to_manual_dict(r) for r in rows]
            return result

    def write(self, source, actor_id, body: dict, submitted: dict, *, validate_new=None) -> tuple[int, dict]:
        if not source or actor_id != source.get('user_id'):
            raise ExperienceWriteError('experience_management_forbidden', 403)
        if body['action'] != 'create' and not PG_NOTE_ID.fullmatch(body['id']):
            raise ExperienceWriteError('invalid_experience_id', 400)

        owner = source['user_id']
        settings = self._settings()
        issuer = TicketIssuer(settings.ticket_secret, gate_id='manual_experiences')
        writer = ExperienceWriter(settings.ticket_secret)
        with open_experience_conn(settings, role='coordinator') as conn:
            prior = conn.execute(
                'SELECT 1 FROM experience_requests WHERE owner_id = %s AND request_id = %s',
                (owner, body['request_id']),
            ).fetchone()
            if prior is None and validate_new is not None:
                validate_new(submitted)

            op = Op(
                action=body['action'],
                owner_id=owner,
                actor_id=actor_id,
                request_id=body['request_id'],
                note_id=body.get('id'),
                category=body.get('category'),
                problem=body.get('problem'),
                solution=body.get('solution'),
                reusable_experience=body.get('reusable_experience'),
                expected_revision=body.get('revision'),
                origin='manual',
            )
            ticket = issuer.issue(
                proposal_uid=f"manual:{body['request_id']}",
                decision_id=f'user_fast:{uuid.uuid4().hex}',
                lane='user_fast',
            )
            applied = writer.apply(conn, ticket, op)
            status = int(applied.get('http_status') or (201 if body['action'] == 'create' else 200))
            response = {
                'ok': True,
                'experience': _format_manual_note(applied['experience']),
                'replayed': bool(applied.get('replayed', False)),
            }
            return status, response

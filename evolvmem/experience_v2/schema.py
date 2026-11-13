"""Append-only schema migration runner for PostgreSQL personal experience v2 databases."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re

MIGRATIONS_DIR = Path(__file__).resolve().parent / 'migrations'
_MIGRATION_NAME_RE = re.compile(r'^[0-9]{3}_[a-z0-9_]+\.sql\Z')


class ExperienceMigrationError(RuntimeError):
    """Raised when an experience v2 migration fails checksum or ordering validation."""


def list_migration_files(migrations_dir: Path | None = None) -> list[Path]:
    """Return sorted numbered *.sql migration files."""
    root = Path(migrations_dir) if migrations_dir is not None else MIGRATIONS_DIR
    if not root.is_dir():
        return []
    files = [p for p in sorted(root.iterdir()) if p.is_file() and _MIGRATION_NAME_RE.fullmatch(p.name)]
    return files


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def apply_experience_migrations(conn, *, migrations_dir: Path | None = None) -> list[str]:
    """Apply pending append-only migrations and verify checksums of applied ones."""
    files = list_migration_files(migrations_dir)
    applied_now: list[str] = []
    with conn.transaction():
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS experience_schema_migrations (
              version TEXT PRIMARY KEY,
              sha256 TEXT NOT NULL,
              applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )
        existing = {
            row[0]: row[1]
            for row in conn.execute('SELECT version, sha256 FROM experience_schema_migrations').fetchall()
        }
        for path in files:
            digest = _file_sha256(path)
            if path.name in existing:
                if existing[path.name] != digest:
                    raise ExperienceMigrationError(f'checksum_mismatch:{path.name}')
                continue
            sql_text = path.read_text(encoding='utf-8')
            conn.execute(sql_text)
            conn.execute(
                'INSERT INTO experience_schema_migrations(version, sha256) VALUES (%s, %s)',
                (path.name, digest),
            )
            applied_now.append(path.name)
    return applied_now

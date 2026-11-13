#!/usr/bin/env python3
"""CLI tool to migrate manual_experience_* SQLite backup/source into PG experience_v2."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg

from evolvmem.experience_v2.manual_migrator import migrate_manual_experiences
from evolvmem.experience_v2.schema import apply_experience_migrations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Migrate SQLite manual experiences to PostgreSQL.')
    parser.add_argument('--sqlite', required=True, help='Path to SQLite memory.db (opened read-only).')
    parser.add_argument('--pg-dsn', required=True, help='PostgreSQL target DSN (never prod unless approved).')
    parser.add_argument('--receipt', required=True, help='Output JSON receipt path.')
    parser.add_argument('--owner', default=None, help='Optional owner_id filter.')
    parser.add_argument('--secret-file', default=None, help='Optional HMAC secret file.')
    args = parser.parse_args(argv)

    secret: bytes
    if args.secret_file:
        secret = Path(args.secret_file).read_bytes().strip()
    elif os.environ.get('EVOLVMEM_TICKET_SECRET'):
        secret = os.environ['EVOLVMEM_TICKET_SECRET'].encode('utf-8')
    else:
        secret = secrets.token_bytes(32)

    with psycopg.connect(args.pg_dsn) as conn:
        apply_experience_migrations(conn)
        receipt = migrate_manual_experiences(Path(args.sqlite), conn, secret=secret, owner_filter=args.owner)

    out_path = Path(args.receipt)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.chmod(out_path, 0o600)
    print(json.dumps({'status': 'ok', 'notes_verified': receipt['notes_verified_count'], 'unexplained_diff_count': receipt['unexplained_diff_count']}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

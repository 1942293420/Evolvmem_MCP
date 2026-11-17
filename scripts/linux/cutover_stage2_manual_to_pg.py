#!/usr/bin/env python3
"""Stage 2 Task 2.11: Maintenance-window cutover & read-only pre-cutover dry-run tool."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import uuid

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import psycopg

from evolvmem.experience_v2.manual_migrator import migrate_manual_experiences
from evolvmem.experience_v2.schema import apply_experience_migrations

_SCHEMA_RE = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')


def _build_conn_params(args) -> tuple[str, str]:
    dsn = (args.pg_dsn or '').strip()
    if dsn:
        return dsn, f'host=127.0.0.1 port={args.pg_port} dbname={args.pg_db}'
    if args.pg_password_file:
        pw = Path(args.pg_password_file).expanduser().read_text(encoding='utf-8').strip()
    elif os.environ.get('EVOLVMEM_PG_PASSWORD'):
        pw = os.environ['EVOLVMEM_PG_PASSWORD']
    else:
        raise ValueError('pg_credentials_required')
    conninfo = (
        f'host={args.pg_host} port={args.pg_port} dbname={args.pg_db} '
        f'user={args.pg_user} password={pw}'
    )
    redacted = f'host={args.pg_host} port={args.pg_port} dbname={args.pg_db} user={args.pg_user}'
    return conninfo, redacted


def _run_migration_into_schema(
    *,
    conninfo: str,
    schema: str,
    sqlite_db: Path,
    owner_id: str | None,
    secret: bytes,
    cleanup_schema: bool,
    embeddings_by_id: dict[str, list[float]] | None = None,
) -> dict:
    if not _SCHEMA_RE.fullmatch(schema):
        raise ValueError('invalid_schema')
    with psycopg.connect(conninfo, autocommit=True) as admin:
        has_vec = admin.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone()
        if not has_vec:
            admin.execute('CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public')
        if schema != 'public':
            admin.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
    try:
        with psycopg.connect(conninfo, options=f'-c search_path={schema},public') as conn:
            apply_experience_migrations(conn)
            receipt = migrate_manual_experiences(
                sqlite_db,
                conn,
                secret=secret,
                owner_filter=owner_id,
                embeddings_by_id=embeddings_by_id,
            )
            conn.commit()
        return {
            'schema': schema,
            'notes_migrated_this_run': int(receipt.get('notes_migrated_this_run', 0)),
            'notes_verified_count': int(receipt.get('notes_verified_count', 0)),
            'events_verified_count': int(receipt.get('events_verified_count', 0)),
            'requests_verified_count': int(receipt.get('requests_verified_count', 0)),
            'unexplained_diff_count': int(receipt.get('unexplained_diff_count', -1)),
        }
    finally:
        if cleanup_schema:
            with psycopg.connect(conninfo, autocommit=True) as admin:
                admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _update_policy_flag(policy_file: Path, *, enabled: bool) -> dict:
    data = json.loads(policy_file.read_text(encoding='utf-8')) if policy_file.is_file() else {}
    data['experience_v2_enabled'] = bool(enabled)
    tmp = policy_file.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(tmp, policy_file)
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='Stage 2 manual experience -> PG cutover helper.')
    parser.add_argument('--sqlite-db', required=True, help='Source SQLite memory.db (opened read-only).')
    parser.add_argument('--owner-id', default=None, help='Target owner_id.')
    parser.add_argument('--pg-dsn', default=None)
    parser.add_argument('--pg-host', default='127.0.0.1')
    parser.add_argument('--pg-port', type=int, default=5432)
    parser.add_argument('--pg-db', default='postgres')
    parser.add_argument('--pg-user', default='postgres')
    parser.add_argument('--pg-password-file', default=None)
    parser.add_argument('--schema', default=None)
    parser.add_argument('--policy-file', default=None)
    parser.add_argument('--secret-file', default=None)
    parser.add_argument('--embeddings-json', default=None)
    parser.add_argument('--receipt-out', default=None)
    parser.add_argument('--dry-run', action='store_true', default=False)
    parser.add_argument('--cleanup-dry-run-schema', action='store_true', default=False)
    parser.add_argument('--execute', action='store_true', default=False)
    parser.add_argument('--i-have-maintenance-approval', action='store_true', default=False)
    args = parser.parse_args(argv)

    if args.execute and not args.i_have_maintenance_approval:
        print(json.dumps({'ok': False, 'error': 'maintenance_approval_required'}))
        return 2

    is_execute = bool(args.execute and args.i_have_maintenance_approval)
    mode = 'execute' if is_execute else 'dry_run'
    schema = args.schema or (f's2_dryrun_{uuid.uuid4().hex[:10]}' if mode == 'dry_run' else 'public')
    sqlite_db = Path(args.sqlite_db).expanduser().resolve()
    if not sqlite_db.is_file():
        print(json.dumps({'ok': False, 'error': 'sqlite_source_missing'}))
        return 2

    secret = (
        Path(args.secret_file).expanduser().read_bytes().strip()
        if args.secret_file
        else secrets.token_bytes(32)
    )
    conninfo, redacted_target = _build_conn_params(args)
    embeddings_by_id = None
    if args.embeddings_json:
        emb_raw = json.loads(Path(args.embeddings_json).expanduser().read_text(encoding='utf-8'))
        if isinstance(emb_raw, dict):
            embeddings_by_id = emb_raw
    verification = _run_migration_into_schema(
        conninfo=conninfo,
        schema=schema,
        sqlite_db=sqlite_db,
        owner_id=args.owner_id,
        secret=secret,
        cleanup_schema=(mode == 'dry_run' and args.cleanup_dry_run_schema),
        embeddings_by_id=embeddings_by_id,
    )

    policy_updated = False
    if is_execute and args.policy_file:
        _update_policy_flag(Path(args.policy_file).expanduser(), enabled=True)
        policy_updated = True

    owner_digest = hashlib.sha256(args.owner_id.encode('utf-8')).hexdigest()[:12] if args.owner_id else None
    payload = {
        'task': '2.11',
        'mode': mode,
        'timestamp': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'target': redacted_target,
        'owner_sha256_prefix': owner_digest,
        'policy_updated': policy_updated,
        'production_touched': bool(is_execute),
        'migration_verification': verification,
        'cutover_runbook': [
            '1. Obtain user approval for maintenance window',
            '2. Quiesce manual writes on evolvmem-web & evolvmem-lan-mcp (brief maintenance stop)',
            '3. Re-run cutover_stage2_manual_to_pg.py --execute --i-have-maintenance-approval against prod PG 55432',
            '4. Verify notes_verified_count == SQLite count and unexplained_diff_count == 0',
            '5. Set experience_v2_enabled=true in product-policy.json and restart services',
            '6. Smoke check Web + MCP read/write parity',
        ],
        'instant_rollback': {
            'experience_v2_enabled': False,
            'step': 'Set experience_v2_enabled=false in product-policy.json and reload services; SQLite remains intact.',
        },
        'llm_usage': {'calls': 0, 'tokens': 0},
    }
    if args.receipt_out:
        out_p = Path(args.receipt_out).expanduser()
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.chmod(out_p, 0o600)
    print(json.dumps({'ok': verification['unexplained_diff_count'] == 0, 'mode': mode, 'verification': verification}))
    return 0 if verification['unexplained_diff_count'] == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
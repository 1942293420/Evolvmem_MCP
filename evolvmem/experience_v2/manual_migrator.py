"""Fidelity SQLite manual_experience_* -> PostgreSQL experience_v2 migrator (Task 2.3)."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from evolvmem.experience_v2.writer import ExperienceWriter, Op, TicketIssuer


class ManualMigrationError(RuntimeError):
    """Raised when SQLite manual_experience_* migration or read-back verification fails."""


def _iso_utc(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, str):
        dt = datetime.fromisoformat(val)
    elif isinstance(val, datetime):
        dt = val
    else:
        return str(val)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _sha256_obj(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _open_sqlite_ro(sqlite_path: Path) -> sqlite3.Connection:
    path = Path(sqlite_path).resolve()
    if not path.is_file():
        raise ManualMigrationError(f'sqlite_source_not_found:{path}')
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _read_sqlite_source(sqlite_path: Path, owner_filter: str | None) -> tuple[list[dict], list[dict], list[dict]]:
    sconn = _open_sqlite_ro(sqlite_path)
    try:
        tables = {
            r[0]
            for r in sconn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'manual_experience_%'"
            ).fetchall()
        }
        if 'manual_experience_notes' not in tables:
            return [], [], []
        where = 'WHERE owner_id = ?' if owner_filter else ''
        params = (owner_filter,) if owner_filter else ()
        notes = [
            dict(r)
            for r in sconn.execute(
                f"""
                SELECT id, owner_id, category, problem, solution, reusable_experience,
                       revision, deleted_at, created_at, updated_at
                FROM manual_experience_notes {where} ORDER BY created_at ASC, id ASC
                """,
                params,
            ).fetchall()
        ]
        events = (
            [
                dict(r)
                for r in sconn.execute(
                    f"""
                    SELECT event_id, owner_id, actor_id, request_id, action, note_id,
                           before_json, after_json, created_at
                    FROM manual_experience_events {where} ORDER BY rowid ASC
                    """,
                    params,
                ).fetchall()
            ]
            if 'manual_experience_events' in tables
            else []
        )
        requests = (
            [
                dict(r)
                for r in sconn.execute(
                    f"""
                    SELECT owner_id, request_id, actor_id, payload_sha256,
                           response_json, http_status, created_at
                    FROM manual_experience_requests {where} ORDER BY created_at ASC, request_id ASC
                    """,
                    params,
                ).fetchall()
            ]
            if 'manual_experience_requests' in tables
            else []
        )
        return notes, events, requests
    finally:
        sconn.close()


def _note_digest(row: dict[str, Any]) -> str:
    return _sha256_obj(
        {
            'id': row['id'],
            'owner_id': row['owner_id'],
            'category': row['category'],
            'problem': row['problem'].strip(),
            'solution': row['solution'].strip(),
            'reusable_experience': row['reusable_experience'].strip(),
            'revision': int(row['revision']),
            'deleted_at': _iso_utc(row['deleted_at']),
            'created_at': _iso_utc(row['created_at']),
            'updated_at': _iso_utc(row['updated_at']),
        }
    )


def verify_manual_migration(
    sqlite_path: Path,
    pg_conn,
    *,
    owner_filter: str | None = None,
) -> dict[str, Any]:
    """Read back PG rows and compare content digests & metadata against SQLite."""
    notes, events, requests = _read_sqlite_source(sqlite_path, owner_filter)
    where = 'WHERE owner_id = %s' if owner_filter else ''
    params = (owner_filter,) if owner_filter else ()

    pg_notes_rows = pg_conn.execute(
        f"""
        SELECT id, owner_id, category, problem, solution, reusable_experience,
               revision, deleted_at, created_at, updated_at, origin
        FROM experience_notes
        {'WHERE owner_id = %s AND origin = %s' if owner_filter else 'WHERE origin = %s'}
        """,
        (owner_filter, 'manual') if owner_filter else ('manual',),
    ).fetchall()
    pg_notes = {
        r[0]: {
            'id': r[0],
            'owner_id': r[1],
            'category': r[2],
            'problem': r[3],
            'solution': r[4],
            'reusable_experience': r[5],
            'revision': r[6],
            'deleted_at': r[7],
            'created_at': r[8],
            'updated_at': r[9],
            'origin': r[10],
        }
        for r in pg_notes_rows
    }

    pg_events = {
        r[0]: {
            'owner_id': r[1],
            'actor_id': r[2],
            'request_id': r[3],
            'action': r[4],
            'note_id': r[5],
            'before_json': r[6],
            'after_json': r[7],
        }
        for r in pg_conn.execute(
            f"""
            SELECT event_id, owner_id, actor_id, request_id, action, note_id, before_json, after_json
            FROM experience_events {where}
            """,
            params,
        ).fetchall()
    }
    pg_reqs = {
        (r[0], r[1]): {'actor_id': r[2], 'payload_sha256': r[3], 'http_status': r[4]}
        for r in pg_conn.execute(
            f"""
            SELECT owner_id, request_id, actor_id, payload_sha256, http_status
            FROM experience_requests {where}
            """,
            params,
        ).fetchall()
    }

    diffs: list[dict[str, Any]] = []
    verified_notes: list[dict[str, Any]] = []
    for n in notes:
        nid = n['id']
        p_row = pg_notes.get(nid)
        if p_row is None:
            diffs.append({'kind': 'note_missing_in_pg', 'id': nid})
            continue
        src_hash = _note_digest(n)
        pg_hash = _note_digest(p_row)
        if src_hash != pg_hash:
            diffs.append({'kind': 'note_digest_mismatch', 'id': nid, 'src_hash': src_hash, 'pg_hash': pg_hash})
            continue
        verified_notes.append(
            {
                'id': nid,
                'owner_sha256_prefix': hashlib.sha256(str(n['owner_id']).encode('utf-8')).hexdigest()[:12],
                'category': n['category'],
                'revision': int(n['revision']),
                'deleted': n['deleted_at'] is not None,
                'sha256': src_hash,
            }
        )

    ev_verified = 0
    for ev in events:
        pe = pg_events.get(ev['event_id'])
        if pe is None:
            diffs.append({'kind': 'event_missing_in_pg', 'event_id': ev['event_id']})
            continue
        src_after = json.loads(ev['after_json'])
        if pe['action'] != ev['action'] or pe['note_id'] != ev['note_id'] or pe['after_json'] != src_after:
            diffs.append({'kind': 'event_mismatch', 'event_id': ev['event_id']})
            continue
        ev_verified += 1

    req_verified = 0
    for req in requests:
        pr = pg_reqs.get((req['owner_id'], req['request_id']))
        if pr is None:
            diffs.append({'kind': 'request_missing_in_pg', 'request_id': req['request_id']})
            continue
        if pr['payload_sha256'] != req['payload_sha256'] or int(pr['http_status']) != int(req['http_status']):
            diffs.append({'kind': 'request_mismatch', 'request_id': req['request_id']})
            continue
        req_verified += 1

    if diffs:
        raise ManualMigrationError(f'verification_failed:{len(diffs)}:{diffs[:3]}')

    return {
        'notes_source_count': len(notes),
        'notes_verified_count': len(verified_notes),
        'events_source_count': len(events),
        'events_verified_count': ev_verified,
        'requests_source_count': len(requests),
        'requests_verified_count': req_verified,
        'unexplained_diff_count': len(diffs),
        'note_digests': verified_notes,
    }


def migrate_manual_experiences(
    sqlite_path: Path,
    pg_conn,
    *,
    secret: bytes | str,
    owner_filter: str | None = None,
    embeddings_by_id: dict[str, list[float]] | None = None,
) -> dict[str, Any]:
    """Migrate SQLite manual_experience_* rows into PG through ExperienceWriter."""
    notes, events, requests = _read_sqlite_source(sqlite_path, owner_filter)
    issuer = TicketIssuer(secret, gate_id='manual-migrator')
    writer = ExperienceWriter(secret)

    events_by_note: dict[str, list[dict]] = {}
    for ev in events:
        events_by_note.setdefault(ev['note_id'], []).append(ev)

    reqs_by_owner: dict[str, list[dict]] = {}
    for req in requests:
        reqs_by_owner.setdefault(req['owner_id'], []).append(req)
    attached_owner_reqs: set[str] = set()

    migrated_now = 0
    for idx, note in enumerate(notes):
        owner = note['owner_id']
        note_id = note['id']
        hist_reqs = []
        if owner not in attached_owner_reqs:
            hist_reqs = reqs_by_owner.get(owner, [])
            attached_owner_reqs.add(owner)

        ticket = issuer.issue(proposal_uid=f'import-{note_id}', decision_id=f'import-{note_id}', lane='user_fast')
        op = Op(
            action='import_manual',
            owner_id=owner,
            actor_id=owner,
            request_id=f'migrate-manual:{note_id}',
            note_id=note_id,
            category=note['category'],
            problem=note['problem'],
            solution=note['solution'],
            reusable_experience=note['reusable_experience'],
            origin='manual',
            embedding=(embeddings_by_id.get(note_id) if embeddings_by_id else None),
            revision=int(note['revision']),
            deleted_at=_iso_utc(note['deleted_at']),
            created_at=_iso_utc(note['created_at']),
            updated_at=_iso_utc(note['updated_at']),
            restore_snapshot={
                'historical_events': events_by_note.get(note_id, []),
                'historical_requests': hist_reqs if idx == 0 or hist_reqs else [],
            },
        )
        res = writer.apply(pg_conn, ticket, op)
        if not res.get('replayed'):
            migrated_now += 1

    verified = verify_manual_migration(sqlite_path, pg_conn, owner_filter=owner_filter)
    return {
        'schema': 'evolvmem.manual_migration_receipt.v1',
        'migrated_at': datetime.now(timezone.utc).isoformat(),
        'source_db_name': Path(sqlite_path).name,
        'notes_migrated_this_run': migrated_now,
        **verified,
    }

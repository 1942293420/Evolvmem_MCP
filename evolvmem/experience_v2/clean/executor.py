"""Stage 4 Task 4.3: manifest rescue overrides, 30-day .jsonl.gz backup, FK/FTS/USearch deletion."""
from __future__ import annotations

from collections import defaultdict
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from typing import Any, Sequence

from usearch.index import Index

from evolvmem.experience_v2.clean.legacy_reader import open_sqlite_readonly

DEFAULT_RESCUE_IDS = ('capability_memories:3076', 'learning_memories:16405')
_CHUNK = 400
_VALID_TABLES = frozenset({'context_items', 'capability_memories', 'learning_memories'})


def parse_legacy_id(legacy_id: str) -> tuple[str, int]:
    """Parse '<table_name>:<int_pk>' into validated (table, pk)."""
    tbl, sep, raw = str(legacy_id or '').partition(':')
    if not sep or tbl not in _VALID_TABLES:
        raise ValueError(f'invalid_legacy_id:{legacy_id}')
    return tbl, int(raw)


def _split_ids(legacy_ids: Sequence[str]) -> tuple[list[int], list[int], list[int]]:
    ci: list[int] = []
    cm: list[int] = []
    lm: list[int] = []
    for lid in legacy_ids:
        tbl, pk = parse_legacy_id(lid)
        if tbl == 'context_items':
            ci.append(pk)
        elif tbl == 'capability_memories':
            cm.append(pk)
        else:
            lm.append(pk)
    return sorted(set(ci)), sorted(set(cm)), sorted(set(lm))


def _chunks(vals: Sequence[int]) -> Sequence[Sequence[int]]:
    for i in range(0, len(vals), _CHUNK):
        yield vals[i : i + _CHUNK]


def apply_rescue_overrides(
    keep_items: Sequence[dict[str, Any]],
    delete_items: Sequence[dict[str, Any]],
    *,
    rescue_ids: Sequence[str] = DEFAULT_RESCUE_IDS,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Move approved borderline legacy_ids from delete manifest into keep manifest."""
    rescue_set = {str(x) for x in rescue_ids}
    keep_out = [dict(x) for x in keep_items]
    keep_seen = {str(x['legacy_id']) for x in keep_out}
    del_out: list[dict[str, Any]] = []
    for item in delete_items:
        lid = str(item['legacy_id'])
        if lid in rescue_set:
            if lid not in keep_seen:
                promoted = dict(item)
                promoted['gate_layer'] = 'L4'
                promoted['reason_code'] = 'user_sample_rescue'
                promoted['detail'] = 'Rescued by user during Stage 4 delete-sample-50 approval'
                keep_out.append(promoted)
                keep_seen.add(lid)
        else:
            del_out.append(dict(item))
    overlap = {str(x['legacy_id']) for x in keep_out} & {str(x['legacy_id']) for x in del_out}
    if overlap:
        raise ValueError(f'manifest_overlap:{sorted(overlap)}')
    return keep_out, del_out


def _fetch_grouped_rows(
    conn: sqlite3.Connection,
    existing_tables: set[str],
    table: str,
    fk_col: str,
    ids: Sequence[int],
) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    if table not in existing_tables or not ids:
        return grouped
    for batch in _chunks(list(ids)):
        ph = ','.join('?' for _ in batch)
        rows = conn.execute(f'SELECT * FROM "{table}" WHERE "{fk_col}" IN ({ph})', tuple(batch)).fetchall()
        for r in rows:
            d = dict(r)
            grouped[int(d[fk_col])].append(d)
    return grouped


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as fh:
        while chunk := fh.read(65536):
            h.update(chunk)
    return h.hexdigest()


def export_deleted_legacy_backup(
    sqlite_path: Path | str,
    delete_ids: Sequence[str],
    out_gz_path: Path | str,
    *,
    retain_days: int = 30,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Export full raw rows & children of approved delete IDs into a chmod-600 .jsonl.gz backup."""
    ci_ids, cm_ids, lm_ids = _split_ids(delete_ids)
    expected_total = len(ci_ids) + len(cm_ids) + len(lm_ids)
    ts = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    retain_until = (ts + timedelta(days=int(retain_days))).isoformat()

    conn = open_sqlite_readonly(sqlite_path)
    try:
        existing_tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        ci_rows = _fetch_grouped_rows(conn, existing_tables, 'context_items', 'id', ci_ids)
        cm_rows = _fetch_grouped_rows(conn, existing_tables, 'capability_memories', 'memory_id', cm_ids)
        lm_rows = _fetch_grouped_rows(conn, existing_tables, 'learning_memories', 'item_id', lm_ids)

        found_ids = (
            {f'context_items:{pk}' for pk in ci_rows}
            | {f'capability_memories:{pk}' for pk in cm_rows}
            | {f'learning_memories:{pk}' for pk in lm_rows}
        )
        missing = set(delete_ids) - found_ids
        if missing:
            raise ValueError(f'backup_missing_sqlite_rows:{sorted(missing)[:10]}')

        ci_children = {
            tbl: _fetch_grouped_rows(conn, existing_tables, tbl, 'item_id', ci_ids)
            for tbl in (
                'context_layers', 'context_sources', 'context_evidence', 'context_project_resolutions',
                'knowledge_metadata', 'knowledge_qa', 'unit_derivations', 'history_experience_ingest_materials',
            )
        }
        cm_children = {
            tbl: _fetch_grouped_rows(conn, existing_tables, tbl, 'memory_id', cm_ids)
            for tbl in ('capability_assessments', 'capability_sources')
        }
    finally:
        conn.close()

    out_path = Path(out_gz_path).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix='.stage4-del-backup-', suffix='.jsonl.gz', dir=out_path.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        with gzip.open(tmp_path, 'wt', encoding='utf-8') as gz:
            header = {
                'record_type': 'backup_header',
                'schema_version': 1,
                'created_at': ts.isoformat(),
                'retain_until': retain_until,
                'expected_count': expected_total,
            }
            gz.write(json.dumps(header, ensure_ascii=False, separators=(',', ':')) + '\n')
            for pk in ci_ids:
                rec = {
                    'record_type': 'deleted_legacy_row',
                    'legacy_id': f'context_items:{pk}',
                    'source_table': 'context_items',
                    'raw_pk': pk,
                    'row': ci_rows[pk][0],
                    'children': {k: v[pk] for k, v in ci_children.items() if pk in v},
                }
                gz.write(json.dumps(rec, ensure_ascii=False, separators=(',', ':')) + '\n')
            for pk in cm_ids:
                rec = {
                    'record_type': 'deleted_legacy_row',
                    'legacy_id': f'capability_memories:{pk}',
                    'source_table': 'capability_memories',
                    'raw_pk': pk,
                    'row': cm_rows[pk][0],
                    'children': {k: v[pk] for k, v in cm_children.items() if pk in v},
                }
                gz.write(json.dumps(rec, ensure_ascii=False, separators=(',', ':')) + '\n')
            for pk in lm_ids:
                rec = {
                    'record_type': 'deleted_legacy_row',
                    'legacy_id': f'learning_memories:{pk}',
                    'source_table': 'learning_memories',
                    'raw_pk': pk,
                    'row': lm_rows[pk][0],
                    'children': {},
                }
                gz.write(json.dumps(rec, ensure_ascii=False, separators=(',', ':')) + '\n')
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, out_path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()

    verified_ids: set[str] = set()
    with gzip.open(out_path, 'rt', encoding='utf-8') as gz:
        first = json.loads(gz.readline())
        if first.get('record_type') != 'backup_header' or int(first.get('expected_count', -1)) != expected_total:
            raise ValueError('backup_header_verification_failed')
        for line in gz:
            if line.strip():
                verified_ids.add(str(json.loads(line)['legacy_id']))
    if len(verified_ids) != expected_total:
        raise ValueError(f'backup_count_mismatch:{len(verified_ids)}!={expected_total}')

    return {
        'path': str(out_path),
        'sha256': _sha256_file(out_path),
        'bytes': out_path.stat().st_size,
        'exported_count': len(verified_ids),
        'retain_until': retain_until,
    }


def _verify_backup_receipt(backup_receipt: dict[str, Any], delete_ids: Sequence[str]) -> None:
    bpath = Path(str((backup_receipt or {}).get('path') or ''))
    if not bpath.is_file():
        raise ValueError(f'backup_file_missing:{bpath}')
    if _sha256_file(bpath) != str(backup_receipt.get('sha256') or ''):
        raise ValueError('backup_sha256_mismatch')
    backed_ids: set[str] = set()
    with gzip.open(bpath, 'rt', encoding='utf-8') as gz:
        _header = json.loads(gz.readline())
        for line in gz:
            if line.strip():
                backed_ids.add(str(json.loads(line)['legacy_id']))
    missing = set(delete_ids) - backed_ids
    if missing:
        raise ValueError(f'backup_missing_delete_ids:{len(missing)}')


def _prune_usearch_and_provenance(
    index_path: Path,
    prov_path: Path,
    del_int_ids: Sequence[int],
    *,
    lock_paths: Sequence[Path],
) -> dict[str, Any]:
    if not index_path.exists() and not prov_path.exists():
        return {'present': False, 'removed_vectors': 0, 'removed_proofs': 0}
    target_ids = sorted(set(int(x) for x in del_int_ids))
    removed_vecs = 0
    remaining_vecs = 0
    removed_proofs = 0
    with ExitStack() as stack:
        for lp in sorted({Path(p) for p in lock_paths}, key=lambda x: str(x)):
            lp.parent.mkdir(parents=True, exist_ok=True)
            fh = stack.enter_context(lp.open('a', encoding='utf-8'))
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            stack.callback(fcntl.flock, fh.fileno(), fcntl.LOCK_UN)

        if index_path.exists():
            idx = Index.restore(str(index_path), view=False)
            before_n = len(idx)
            for key in target_ids:
                if key in idx:
                    idx.remove(key)
                    removed_vecs += 1
            remaining_vecs = len(idx)
            if removed_vecs > 0:
                fd, tmp_idx = tempfile.mkstemp(prefix='.stage4-vec-', dir=index_path.parent)
                os.close(fd)
                try:
                    idx.save(tmp_idx)
                    os.replace(tmp_idx, index_path)
                finally:
                    if os.path.exists(tmp_idx):
                        os.unlink(tmp_idx)
            else:
                remaining_vecs = before_n

        if prov_path.exists():
            raw = json.loads(prov_path.read_text(encoding='utf-8'))
            items = raw.get('items')
            if isinstance(items, dict):
                for key in target_ids:
                    if items.pop(str(key), None) is not None:
                        removed_proofs += 1
                if 'generation' in raw and removed_proofs > 0:
                    raw['generation'] = int(raw['generation']) + 1
                if removed_proofs > 0:
                    fd, tmp_p = tempfile.mkstemp(prefix='.stage4-prov-', dir=prov_path.parent)
                    try:
                        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                            json.dump(raw, handle, ensure_ascii=False)
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(tmp_p, prov_path)
                    finally:
                        if os.path.exists(tmp_p):
                            os.unlink(tmp_p)
    return {'present': True, 'removed_vectors': removed_vecs, 'remaining_vectors': remaining_vecs, 'removed_proofs': removed_proofs}


def _assert_no_protected_ci_refs(conn: sqlite3.Connection, existing: set[str], ci_ids: Sequence[int]) -> None:
    if not ci_ids:
        return
    checks = (
        ('continuity_workstreams', ('current_context_id',)),
        ('context_project_rollups', ('current_context_id',)),
        ('legacy_memory_migrations', ('context_item_id',)),
        ('session_archive_holds', ('source_context_id',)),
    )
    for tbl, cols in checks:
        if tbl not in existing:
            continue
        tbl_cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{tbl}")').fetchall()}
        for col in cols:
            if col not in tbl_cols:
                continue
            for batch in _chunks(ci_ids):
                ph = ','.join('?' for _ in batch)
                cnt = conn.execute(f'SELECT COUNT(*) FROM "{tbl}" WHERE "{col}" IN ({ph})', tuple(batch)).fetchone()[0]
                if int(cnt) > 0:
                    raise RuntimeError(f'protected_fk_reference:{tbl}.{col}:{cnt}')


def delete_approved_legacy_sqlite_and_vectors(
    sqlite_path: Path | str,
    data_dir: Path | str,
    delete_ids: Sequence[str],
    *,
    backup_receipt: dict[str, Any],
) -> dict[str, Any]:
    """Verify backup first, then execute FK-safe SQLite deletion, FTS rebuild, and .usearch pruning."""
    _verify_backup_receipt(backup_receipt, delete_ids)
    ci_ids, cm_ids, lm_ids = _split_ids(delete_ids)

    db_path = Path(sqlite_path).resolve()
    dd = Path(data_dir).resolve()
    vec_mem_path = dd / 'vectors.usearch'
    vec_mem_sha_before = _sha256_file(vec_mem_path) if vec_mem_path.is_file() else None

    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA foreign_keys = ON')
    conn.execute('PRAGMA busy_timeout = 10000')
    try:
        existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        pre_non_exp = int(conn.execute("SELECT COUNT(*) FROM context_items WHERE content_type != 'experience'").fetchone()[0])
        pre_arch = int(conn.execute('SELECT COUNT(*) FROM session_archives').fetchone()[0]) if 'session_archives' in existing else 0
        pre_mem = int(conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0]) if 'memories' in existing else 0

        _assert_no_protected_ci_refs(conn, existing, ci_ids)
        for batch in _chunks(ci_ids):
            ph = ','.join('?' for _ in batch)
            non_exp_cnt = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM context_items WHERE id IN ({ph}) AND content_type != 'experience'",
                    tuple(batch),
                ).fetchone()[0]
            )
            if non_exp_cnt > 0:
                raise ValueError(f'non_experience_context_item_in_delete_ids:{non_exp_cnt}')
        deleted_counts = {'context_items': 0, 'capability_memories': 0, 'learning_memories': 0}

        with conn:
            for batch in _chunks(cm_ids):
                ph = ','.join('?' for _ in batch)
                if 'capability_assessments' in existing:
                    conn.execute(f'DELETE FROM capability_assessments WHERE memory_id IN ({ph})', tuple(batch))
                if 'capability_sources' in existing:
                    conn.execute(f'DELETE FROM capability_sources WHERE memory_id IN ({ph})', tuple(batch))
                cur = conn.execute(f'DELETE FROM capability_memories WHERE memory_id IN ({ph})', tuple(batch))
                deleted_counts['capability_memories'] += max(0, cur.rowcount)

            ci_cols = {r[1] for r in conn.execute('PRAGMA table_info(context_items)').fetchall()}
            for batch in _chunks(ci_ids):
                ph = ','.join('?' for _ in batch)
                if 'capability_sources' in existing:
                    conn.execute(f'UPDATE capability_sources SET context_item_id = NULL WHERE context_item_id IN ({ph})', tuple(batch))
                if 'history_experience_ingest_materials' in existing:
                    conn.execute(f'UPDATE history_experience_ingest_materials SET item_id = NULL WHERE item_id IN ({ph})', tuple(batch))
                if 'context_project_resolutions' in existing:
                    conn.execute(f'DELETE FROM context_project_resolutions WHERE item_id IN ({ph})', tuple(batch))
                if 'supersedes' in ci_cols:
                    conn.execute(f'UPDATE context_items SET supersedes = NULL WHERE supersedes IN ({ph})', tuple(batch))
                if 'superseded_by' in ci_cols:
                    conn.execute(f'UPDATE context_items SET superseded_by = NULL WHERE superseded_by IN ({ph})', tuple(batch))

            for batch in _chunks(lm_ids):
                ph = ','.join('?' for _ in batch)
                cur = conn.execute(f'DELETE FROM learning_memories WHERE item_id IN ({ph})', tuple(batch))
                deleted_counts['learning_memories'] += max(0, cur.rowcount)

            for batch in _chunks(ci_ids):
                ph = ','.join('?' for _ in batch)
                cur = conn.execute(
                    f"DELETE FROM context_items WHERE id IN ({ph}) AND content_type = 'experience'",
                    tuple(batch),
                )
                deleted_counts['context_items'] += max(0, cur.rowcount)

            if 'capability_memories_fts' in existing:
                conn.execute("INSERT INTO capability_memories_fts(capability_memories_fts) VALUES('rebuild')")
            for fts_tbl in ('capability_memories_fts', 'context_layers_fts', 'context_layers_fts_trigram'):
                if fts_tbl in existing:
                    conn.execute(f"INSERT INTO {fts_tbl}({fts_tbl}) VALUES('integrity-check')")

        fk_errs = conn.execute('PRAGMA foreign_key_check').fetchall()
        if fk_errs:
            raise RuntimeError(f'sqlite_foreign_key_check_failed:{fk_errs[:5]}')
        qc = str(conn.execute('PRAGMA quick_check').fetchone()[0])
        if qc != 'ok':
            raise RuntimeError(f'sqlite_quick_check_failed:{qc}')
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')

        post_non_exp = int(conn.execute("SELECT COUNT(*) FROM context_items WHERE content_type != 'experience'").fetchone()[0])
        post_arch = int(conn.execute('SELECT COUNT(*) FROM session_archives').fetchone()[0]) if 'session_archives' in existing else 0
        post_mem = int(conn.execute('SELECT COUNT(*) FROM memories').fetchone()[0]) if 'memories' in existing else 0
        if (post_non_exp, post_arch, post_mem) != (pre_non_exp, pre_arch, pre_mem):
            raise RuntimeError(f'protected_table_count_changed:before={(pre_non_exp, pre_arch, pre_mem)}:after={(post_non_exp, post_arch, post_mem)}')

        post_remaining = {
            'context_items_experience': int(conn.execute("SELECT COUNT(*) FROM context_items WHERE content_type = 'experience'").fetchone()[0]),
            'context_items_non_experience': post_non_exp,
            'capability_memories': int(conn.execute('SELECT COUNT(*) FROM capability_memories').fetchone()[0]),
            'learning_memories': int(conn.execute('SELECT COUNT(*) FROM learning_memories').fetchone()[0]),
            'session_archives': post_arch,
            'memories': post_mem,
        }
    finally:
        conn.close()

    cap_vec = _prune_usearch_and_provenance(
        dd / 'capability_vectors.usearch',
        dd / 'capability_vectors.provenance.json',
        cm_ids,
        lock_paths=(dd / 'capability_vectors.recovery.lock', dd / 'capability_vectors.usearch.lock'),
    )
    ctx_vec = _prune_usearch_and_provenance(
        dd / 'context_vectors.usearch',
        dd / 'context_vectors.provenance.json',
        ci_ids,
        lock_paths=(dd / 'context_vectors.usearch.lock',),
    )
    if vec_mem_sha_before is not None and _sha256_file(vec_mem_path) != vec_mem_sha_before:
        raise RuntimeError('vectors_usearch_unexpectedly_modified')

    return {
        'deleted': deleted_counts,
        'deleted_total': sum(deleted_counts.values()),
        'post_remaining': post_remaining,
        'vectors': {'capability': cap_vec, 'context': ctx_vec, 'vectors_usearch_untouched': True},
    }

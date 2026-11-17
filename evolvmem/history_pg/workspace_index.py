"""Verified workspace metadata index; never changes business project attribution.

Run out of the HTTP request path. Only native metadata and recovery source cwd
are considered; conversation text, assistant claims and project aliases are not.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import time
from typing import Any

from evolvmem.archive_storage import ArchiveStorage, MAGIC
from evolvmem.config import Config
from evolvmem.history_pg.config import load_pg_history_config
from evolvmem.history_pg.workspace_paths import normalize_workspace_path, workspace_key

BLOCK_PREFIX = 'workspace_projection:v1:block:'
FOLDER_PREFIX = 'workspace_projection:v1:folder:'
MAX_METADATA_BYTES = 256 * 1024 * 1024
INDEXER_VERSION = 2


def extract_workspace_metadata(raw: bytes, adapter: str, expected_session_id: str = '') -> dict[str, Any]:
    """Read explicit Codex session/turn metadata, never paths quoted in messages."""
    if adapter != 'codex':
        return {'workspace_path': '', 'reason': 'unsupported_adapter'}
    paths: dict[str, str] = {}
    ids: set[str] = set()
    subagent = False
    # Old encrypted archives contain a JSON envelope around the native JSONL.
    stripped = raw.strip()
    if stripped.startswith(b'{') and b'\n' not in stripped:
        try:
            envelope = json.loads(stripped)
        except (ValueError, UnicodeError):
            return {'workspace_path': '', 'reason': 'invalid_metadata'}
        if isinstance(envelope, dict) and isinstance(envelope.get('transcript'), str):
            eid = envelope.get('session_id')
            source = envelope.get('source')
            for value in (eid, source.get('session_id') if isinstance(source, dict) else None):
                if isinstance(value, str) and value:
                    ids.add(value)
            raw = envelope['transcript'].encode('utf-8')
    for line in io.BytesIO(raw):
        if b'"session_meta"' not in line and b'"turn_context"' not in line:
            continue
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(row, dict) or row.get('type') not in ('session_meta', 'turn_context'):
            continue
        payload = row.get('payload')
        if not isinstance(payload, dict):
            continue
        if row['type'] == 'session_meta':
            if isinstance(payload.get('id'), str):
                ids.add(payload['id'])
            source = payload.get('source')
            subagent = subagent or bool(payload.get('parent_thread_id')) or (isinstance(source, dict) and 'subagent' in source)
        path = normalize_workspace_path(payload.get('cwd'))
        if path:
            paths[workspace_key(path)] = path
    if subagent:
        return {'workspace_path': '', 'reason': 'subagent_session', 'native_ids': sorted(ids)}
    if len(ids) > 1 or (expected_session_id and ids and ids != {expected_session_id}):
        return {'workspace_path': '', 'reason': 'session_mismatch', 'native_ids': sorted(ids)}
    if len(paths) > 1:
        return {'workspace_path': '', 'reason': 'mixed_workspace', 'native_ids': sorted(ids)}
    if paths:
        return {'workspace_path': next(iter(paths.values())), 'reason': 'verified', 'native_ids': sorted(ids)}
    return {'workspace_path': '', 'reason': 'workspace_unknown', 'native_ids': sorted(ids)}


def _json_ids(value: str) -> list[int]:
    try:
        arr = json.loads(value or '[]')
        return sorted({int(i) for i in arr if str(i).isdigit()}) if isinstance(arr, list) else []
    except (ValueError, TypeError):
        return []


def _load_inputs(conn, block_ids=None):
    sql = 'SELECT block_id,adapter,native_session_id,native_device_id,source_archive_ids,block_sha256 FROM em_history.blocks'
    rows = conn.execute(sql + (' WHERE block_id = ANY(%s)' if block_ids is not None else ''), (list(block_ids),) if block_ids is not None else ()).fetchall()
    blocks = {r[0]: {'block_id': r[0], 'adapter': r[1], 'session_id': r[2], 'device_id': r[3], 'source_ids_raw': r[4], 'block_sha256': r[5], 'archive_ids': _json_ids(r[4]), 'recovery': []} for r in rows}
    if not blocks:
        return blocks, {}
    for bid, aid in conn.execute('SELECT block_id,archive_id FROM em_history.sources WHERE block_id = ANY(%s)', (list(blocks),)).fetchall():
        blocks[bid]['archive_ids'].append(int(aid))
    for bid, adapter, cwd, sha, source_path in conn.execute('SELECT block_id,adapter,cwd,source_sha256,source_path FROM em_history.recovery_sources WHERE block_id = ANY(%s)', (list(blocks),)).fetchall():
        blocks[bid]['recovery'].append([adapter, cwd, sha, source_path])
    ids = sorted({i for b in blocks.values() for i in b['archive_ids']})
    archives = {}
    if ids:
        for r in conn.execute('SELECT a.id,a.adapter,a.payload_path,a.payload_sha256,a.state,a.device_id,a.external_session_id,s.manifest_sha256,s.content_sha256,s.logical_bytes FROM em_runtime.session_archives a LEFT JOIN em_runtime.session_archive_storage s ON s.archive_id=a.id WHERE a.id = ANY(%s)', (ids,)).fetchall():
            archives[int(r[0])] = dict(zip(('id','adapter','path','sha256','state','device_id','external_id','manifest_sha256','content_sha256','logical_bytes'), r))
    for b in blocks.values():
        b['archive_ids'] = sorted(set(b['archive_ids']))
        b['recovery'].sort(key=str)
    return blocks, archives


def _path_for(archive, data_dir):
    p = Path(archive['path'])
    if not p.is_absolute():
        p = (data_dir / p).resolve()
        if not p.is_relative_to(data_dir):
            raise ValueError('invalid_archive_path')
    return p


def _fingerprint(block, archives, data_dir):
    evidence = []
    for aid in block['archive_ids']:
        a = archives.get(aid)
        stat = None
        if a:
            try:
                st = _path_for(a, data_dir).stat()
                stat = [st.st_size, st.st_mtime_ns]
            except (OSError, ValueError):
                pass
        evidence.append([a, stat])
    return hashlib.sha256(json.dumps([INDEXER_VERSION, block, evidence], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _stream_archive_metadata(a, p, key_dir):
    """Authenticate large manifests to private disk; retain only metadata rows."""
    from evolvmem.archive_streaming_clean import authenticated_spool, MAX_LOGICAL_BYTES
    from evolvmem.stream_json import JsonReader, StringSink
    size = a.get('logical_bytes')
    if not isinstance(size, int) or not 0 <= size <= MAX_LOGICAL_BYTES:
        return {'workspace_path': '', 'reason': 'metadata_byte_budget'}
    root = Path.home() / '.cache' / 'evolvmem' / 'workspace-index'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_mode & 0o077:
        raise ValueError('workspace_temp_not_private')
    if shutil.disk_usage(root).free < 2 * size + 64 * 1024**2:
        raise ValueError('workspace_temp_space_low')
    with tempfile.TemporaryDirectory(prefix='private-', dir=root) as temporary:
        payload, transcript = Path(temporary) / 'payload.json', Path(temporary) / 'native.jsonl'
        payload.touch(mode=0o600); transcript.touch(mode=0o600)
        authenticated_spool(Config(data_dir=key_dir, apply_environment=False),
            {'payload_path': str(p.relative_to(key_dir))}, a, payload)
        with payload.open('r', encoding='utf-8', newline='') as source, transcript.open('w', encoding='utf-8', newline='') as destination:
            reader = JsonReader(source)
            outer = reader.value({'session_id': True, 'source': {'session_id': True}, 'transcript': StringSink(destination)})
            if isinstance(outer, dict) and isinstance(outer.get('transcript'), StringSink):
                reader.finish()
                native = transcript
            else:
                native = payload
        rows = []
        selector = {'type': True, 'payload': {'id': True, 'cwd': True, 'parent_thread_id': True, 'source': True}}
        with native.open('r', encoding='utf-8', newline='') as source:
            reader = JsonReader(source)
            while True:
                reader.white()
                if not reader.peek():
                    break
                reader.retained = 0
                row = reader.value(selector)
                if isinstance(row, dict) and row.get('type') in ('session_meta', 'turn_context'):
                    rows.append(row)
        # The same parser validates envelope identities and native rows.
        tiny = '\n'.join(json.dumps(row) for row in rows)
        if native == transcript:
            tiny = json.dumps({'session_id': outer.get('session_id'), 'source': outer.get('source'), 'transcript': tiny})
        return extract_workspace_metadata(tiny.encode(), a['adapter'])


def _archive_metadata(a, data_dir):
    if a['adapter'] != 'codex':
        return {'workspace_path': '', 'reason': 'unsupported_adapter'}
    if a['state'] != 'available':
        return {'workspace_path': '', 'reason': 'archive_unavailable'}
    try:
        p = _path_for(a, data_dir)
        if (a.get('logical_bytes') or 0) > MAX_METADATA_BYTES and a.get('manifest_sha256'):
            key_dir = data_dir if (data_dir / 'archive.key').is_file() else next((d for d in p.parents if (d / 'archive.key').is_file()), None)
            if key_dir is None:
                return {'workspace_path': '', 'reason': 'missing_archive_key'}
            return _stream_archive_metadata(a, p, key_dir)
        if p.stat().st_size > MAX_METADATA_BYTES:
            return {'workspace_path': '', 'reason': 'metadata_byte_budget'}
        disk = p.read_bytes()
        physical_ok = hashlib.sha256(disk).hexdigest() == a['sha256']
        storage_verified = False
        if a.get('manifest_sha256'):
            if hashlib.sha256(disk).hexdigest() != a['manifest_sha256']:
                return {'workspace_path': '', 'reason': 'archive_manifest_mismatch'}
        if disk.startswith(MAGIC) or p.suffix == '.bin':
            key_dir = data_dir if (data_dir / 'archive.key').is_file() else next((d for d in p.parents if (d / 'archive.key').is_file()), None)
            if key_dir is None:
                return {'workspace_path': '', 'reason': 'missing_archive_key'}
            key = (key_dir / 'archive.key').read_bytes()
            storage = ArchiveStorage(Config(data_dir=key_dir, apply_environment=False), key)
            manifest = storage.manifest(disk)
            if manifest and manifest.get('size', MAX_METADATA_BYTES + 1) > MAX_METADATA_BYTES:
                return {'workspace_path': '', 'reason': 'metadata_byte_budget'}
            if a.get('manifest_sha256'):
                if not manifest or manifest.get('sha256') != a['content_sha256'] or manifest.get('size') != a['logical_bytes']:
                    return {'workspace_path': '', 'reason': 'archive_storage_mismatch'}
                storage_verified = True
            raw = storage.unpack(disk).encode('utf-8')
        else:
            raw = disk
        if not storage_verified and not physical_ok and hashlib.sha256(raw).hexdigest() != a['sha256']:
            return {'workspace_path': '', 'reason': 'archive_hash_mismatch'}
        # Native ID is checked against each block later, since legacy archive
        # external_session_id is often a composite hash, not the native ID.
        return extract_workspace_metadata(raw, a['adapter'])
    except (OSError, ValueError, TypeError):
        return {'workspace_path': '', 'reason': 'archive_unreadable'}
    except Exception:
        # Cryptographic failures reveal no key material or archive contents.
        return {'workspace_path': '', 'reason': 'archive_verification_failed'}


def index_history_workspaces(cfg, dbname, *, apply=False, data_dir=None, block_ids=None, force=False):
    data_dir = Path(data_dir or os.environ.get('EVOLVMEM_DATA_DIR') or '~/.claude/evolvmem').expanduser().resolve()
    with cfg.connect(dbname, role_kind='reader') as conn:
        blocks, archives = _load_inputs(conn, block_ids)
        old = dict(conn.execute("SELECT key,value FROM em_history.candidate_meta WHERE key LIKE 'workspace_projection:v1:block:%%'").fetchall())
    stats = Counter(total=len(blocks), written=0, unchanged=0, verified=0, unknown=0, raced=0)
    reasons = Counter()
    cache = {}
    for bid, b in blocks.items():
        fp = _fingerprint(b, archives, data_dir)
        try:
            previous = json.loads(old.get(BLOCK_PREFIX + bid, '{}'))
        except ValueError:
            previous = {}
        if not isinstance(previous, dict):
            previous = {}
        transient = {'archive_unavailable','missing_archive','missing_archive_key','archive_unreadable',
            'archive_verification_failed','archive_hash_mismatch','archive_manifest_mismatch','archive_storage_mismatch'}
        indexed_at = previous.get('indexed_at', 0)
        if not isinstance(indexed_at, (int, float)):
            indexed_at = 0
        retry_due = previous.get('reason') in transient and time.time() - indexed_at >= 300
        if not force and not retry_due and previous.get('fingerprint') == fp:
            stats['unchanged'] += 1
            stats['verified' if previous.get('reason') == 'verified' else 'unknown'] += 1
            reasons[previous.get('reason') or 'workspace_unknown'] += 1
            continue
        paths = {}
        errors = []
        evidence = []
        for adapter, cwd, sha, source_path in b['recovery']:
            p = normalize_workspace_path(cwd)
            if p and adapter == b['adapter']:
                paths[workspace_key(p)] = p
                evidence.append({'kind': 'recovery_cwd', 'source_sha256': sha})
        for aid in b['archive_ids']:
            a = archives.get(aid)
            if not a:
                errors.append('missing_archive')
                continue
            if a['adapter'] != b['adapter']:
                errors.append('adapter_mismatch')
                continue
            if aid not in cache:
                cache[aid] = _archive_metadata(a, data_dir)
            meta = cache[aid]
            nids = meta.get('native_ids') or []
            if b['session_id'] and nids and nids != [b['session_id']]:
                errors.append('session_mismatch')
                continue
            if b['device_id'] and a['device_id'] and b['device_id'] != a['device_id']:
                errors.append('device_mismatch')
                continue
            if meta['reason'] not in ('verified','unsupported_adapter','workspace_unknown'):
                errors.append(meta['reason'])
            p = meta.get('workspace_path')
            if p:
                paths[workspace_key(p)] = p
            evidence.append({'kind': 'native_metadata', 'archive_id': aid, 'payload_sha256': a['sha256'], 'reason': meta['reason']})
        reason = errors[0] if errors else 'mixed_workspace' if len(paths) > 1 else 'verified' if paths else 'workspace_unknown'
        path = next(iter(paths.values())) if reason == 'verified' else ''
        result = {'version': 1, 'workspace_path': path, 'reason': reason, 'fingerprint': fp, 'source_evidence': evidence,
            'block_sha256': b['block_sha256'], 'source_archive_ids': b['source_ids_raw'], 'indexed_at': time.time()}
        stats['verified' if path else 'unknown'] += 1
        reasons[reason] += 1
        if apply:
            with cfg.connect(dbname, role_kind='writer') as conn:
                latest, latest_archives = _load_inputs(conn, [bid])
                if bid not in latest or _fingerprint(latest[bid], latest_archives, data_dir) != fp:
                    stats['raced'] += 1
                    continue
                with conn.cursor() as cur:
                    cur.executemany('INSERT INTO em_history.candidate_meta(key,value) VALUES (%s,%s) ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value',
                        [(BLOCK_PREFIX + bid, json.dumps(result, ensure_ascii=False, sort_keys=True)),
                         (FOLDER_PREFIX + bid, workspace_key(path) if path else '')])
                conn.commit()
            stats['written'] += 1
    return {**stats, 'reasons': dict(reasons)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--owner', required=True, help='Configured history owner; never infer from a hostname')
    parser.add_argument('--apply', action='store_true', help='Write only workspace projection metadata')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--data-dir', type=Path)
    args = parser.parse_args()
    cfg = load_pg_history_config()
    out = index_history_workspaces(cfg, cfg.resolve_user_history_db(args.owner), apply=args.apply, force=args.force, data_dir=args.data_dir)
    print(json.dumps({'owner': args.owner, 'applied': args.apply, **out}, sort_keys=True))


if __name__ == '__main__':
    main()

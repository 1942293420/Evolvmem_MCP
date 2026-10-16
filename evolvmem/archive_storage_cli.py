"""Local storage inspection and bounded, resumable maintenance (no model calls)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil

from evolvmem.archive_maintenance import maintain, save_report
from evolvmem.config import Config
from evolvmem.context_store import ContextStore
from evolvmem.session_archive import SessionArchiver


def status(archiver):
    conn, root = archiver.store._connection(), archiver.config.data_dir
    result = dict(conn.execute('SELECT state,count(*) FROM session_archives GROUP BY state'))
    result['shared_archives'], result['logical_bytes'] = conn.execute(
        'SELECT count(*),coalesce(sum(s.logical_bytes),0) FROM session_archive_storage s '
        "JOIN session_archives a ON a.id=s.archive_id WHERE a.state='available'").fetchone()
    result['legacy_remaining'] = result.get('available', 0) - result['shared_archives']
    result['physical_bytes'] = sum(p.stat().st_size for name in ('session_archives', 'session_chunks')
        for p in (root / name).glob('*.bin') if p.is_file())
    result['disk_free_bytes'] = shutil.disk_usage(root).free
    result['reserve_mb'] = archiver.config.context_archive_min_free_mb
    try:
        result['last_maintenance'] = json.loads((root / 'archive_maintenance.json').read_text())
    except (OSError, ValueError):
        result['last_maintenance'] = None
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'maintain', 'verify'))
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--max-mb', type=int, default=32)
    args = parser.parse_args()
    if args.limit < 1 or args.max_mb < 1:
        parser.error('limits must be positive')
    config = Config.from_file(data_dir=args.data_dir, apply_environment=False, ensure_dirs=False)
    if not config.db_path.is_file():
        parser.error('existing memory.db required')
    with ContextStore(config) as store:
        archiver = SessionArchiver(config, store)
        if args.action == 'maintain':
            result = maintain(archiver, limit=args.limit, max_bytes=args.max_mb * 1024 * 1024)
            save_report(config, result)
        elif args.action == 'verify':
            ids = [r[0] for r in store._connection().execute("SELECT id FROM session_archives WHERE state='available'")]
            failed = [aid for aid in ids if archiver.read_payload(aid) is None]
            result = {'checked': len(ids), 'errors': len(failed), 'failed_archive_ids': failed}
        else:
            result = status(archiver)
        print(json.dumps(result, ensure_ascii=False))
        return 1 if result.get('errors', 0) else 0


if __name__ == '__main__':
    raise SystemExit(main())

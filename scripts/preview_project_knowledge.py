#!/usr/bin/env python3
"""Bounded, offline, read-only project preview. No model, service or migration.

Reads the existing directory projection in one SQLite read transaction. The
report distinguishes a readable legacy material from an extracted fact, and a
source reference from verified readable original bytes. It never infers merges.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import hashlib
import html
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evolvmem.project_knowledge import directory
from evolvmem.project_ownership import load_ownership


@contextmanager
def readonly_store(db):
    # No ContextStore initialization: opening a preview must not run migrations.
    conn = sqlite3.connect(Path(db).resolve().as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    conn.execute('BEGIN')
    try:
        yield SimpleNamespace(_connection=lambda: conn)
    finally:
        conn.rollback()
        conn.close()


def snapshot(conn, ids):
    """Fingerprint the selected original display, provenance and human ledger."""
    marks = ','.join('?' for _ in ids)
    rows = {}
    for table in ('context_items', 'context_layers', 'context_sources', 'context_project_resolutions'):
        column = 'id' if table == 'context_items' else 'item_id'
        rows[table] = [dict(r) for r in conn.execute(f'SELECT * FROM {table} WHERE {column} IN ({marks})', ids)]
    kids = [r[0] for r in conn.execute(f'SELECT knowledge_id FROM knowledge_objects WHERE item_id IN ({marks})', ids)]
    kid_marks = ','.join('?' for _ in kids)
    for table in ('knowledge_objects', 'knowledge_decisions', 'knowledge_source_bindings'):
        rows[table] = [dict(r) for r in conn.execute(f'SELECT * FROM {table} WHERE knowledge_id IN ({kid_marks})', kids)]
    text = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(text.encode()).hexdigest()


def preview(db, project, *, limit=20, item_ids=None, query=''):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('limit_must_be_1_to_50')
    with readonly_store(db) as store:
        # Select only existing eligible project entries, deterministically. The
        # shared read projection may scan metadata; no historical source is sent
        # elsewhere or submitted for reorganization.
        page = directory(store, project, q=query)
        all_items = sorted(page['items'], key=lambda i: (min(i['item_ids']), i['id']))
        allowed = set(item_ids) if item_ids is not None else None
        entries = [i for i in all_items if allowed is None or allowed.intersection(i['item_ids'])][:limit]
        ids = sorted({x for i in entries for x in i['item_ids']})
        conn = store._connection()
        before = snapshot(conn, ids)
        ownership = load_ownership(store, ids)
        result = []
        for item in entries:
            saved = [dict(r) for r in conn.execute(
                'SELECT knowledge_id,revision,decision_json FROM knowledge_objects WHERE item_id IN ('+
                ','.join('?' for _ in item['item_ids'])+')', item['item_ids'])]
            decisions = [{'knowledge_id': r['knowledge_id'], 'revision': r['revision'],
                          'decision': json.loads(r['decision_json'])} for r in saved]
            missing = []
            for source in item['sources']:
                archive_id = source.get('archive_id')
                key = source.get('source_key', '')
                if archive_id is None and key.startswith('archive:'):
                    archive_id = int(key.split(':')[1])
                if archive_id is not None and not conn.execute('SELECT 1 FROM session_archives WHERE id=?', (archive_id,)).fetchone():
                    missing.append(str(archive_id))
            result.append({**item, 'human_decisions': decisions,
                           'ownership': {str(i): ownership[i].public() for i in item['item_ids'] if i in ownership},
                           'missing_archive_references': missing,
                           'original_bytes_checked': False})
        after = snapshot(conn, ids)
        return {'project': project, 'query': query, 'mode': 'read_only', 'matching_entries': page['total'],
                'sample_entries': len(entries), 'sample_item_ids': ids,
                'sections': dict(Counter(i['section'] for i in entries)),
                'extracted_facts': sum(i['kind'] == 'fact' for i in entries),
                'legacy_materials': sum(i['kind'] == 'material' for i in entries),
                'known_disagreements': sum(len(i['warnings']) for i in entries),
                'without_source_links': sum(not i['sources'] for i in entries),
                'missing_archive_references': sum(len(i['missing_archive_references']) for i in result),
                'ownership_records_by_source': dict(Counter(o.decision_source for o in ownership.values())),
                'original_ids_display_sources_decisions_unchanged': before == after,
                'snapshot_sha256': before, 'model_calls': 0, 'writes': conn.total_changes,
                'limitations': ['未提炼的旧资料尚不能判断语义重复、事实变化或分歧。',
                               '来源链接存在不等于已解密核对全部原文。',
                               '此预览不合并、不重分类，也不计算未经实测的节省次数。'],
                'items': result}


def render_html(report):
    esc = lambda value: html.escape(str(value))
    cards = []
    for item in report['items']:
        body = item['body'] or '\n'.join(v['value'] for v in item['versions'])
        cards.append('<article><h2>'+esc(item['title'])+'</h2><p>'+esc(item['section'])+' · '+
                     ('已提炼事实' if item['kind']=='fact' else '旧资料，尚未提炼事实')+'</p><p>'+esc(body)+
                     '</p><details><summary>查看现有归属、来源与记录编号</summary><pre>'+esc(json.dumps({
                         k:item[k] for k in ('item_ids','ownership','human_decisions','sources','warnings')},
                         ensure_ascii=False,indent=2))+'</pre></details></article>')
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>项目知识只读预览</title><style>body{margin:0;padding:24px;font:18px/1.7 system-ui;color:#182b3d;background:#f4f7fa}main{max-width:1400px;margin:auto;overflow-wrap:anywhere}article{background:white;padding:24px;margin:20px 0;border:1px solid #ccd6df;border-radius:10px}h1{font-size:28px}h2{font-size:22px}pre,p{white-space:pre-wrap;overflow-wrap:anywhere}pre{font:16px/1.6 system-ui}summary{cursor:pointer}</style><main><h1>'+esc(report['project'])+'：知识只读预览</h1><p>抽查 '+str(report['sample_entries'])+' 条；已提炼事实 '+str(report['extracted_facts'])+' 条，尚未提炼的旧资料 '+str(report['legacy_materials'])+' 条。没有改动正式资料。</p><p>'+esc(' '.join(report['limitations']))+'</p>'+''.join(cards)+'</main></html>'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--project', required=True)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--query', default='')
    parser.add_argument('--item-id', type=int, action='append')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--html', type=Path)
    args = parser.parse_args()
    # Never overwrite the input database, a symlink, or an earlier report.
    outputs = [p for p in (args.output, args.html) if p]
    if any(p.exists() or p.is_symlink() or p.resolve() == args.db.resolve() for p in outputs) or len({p.resolve() for p in outputs}) != len(outputs):
        parser.error('output must be a new file distinct from the database')
    result = preview(args.db, args.project, limit=args.limit, item_ids=args.item_id, query=args.query)
    for path, content in [(args.output,json.dumps(result,ensure_ascii=False,indent=2)),
                          *([(args.html,render_html(result))] if args.html else [])]:
        with path.open('x', encoding='utf-8') as handle:
            path.chmod(0o600)
            handle.write(content)
    print(json.dumps({k:v for k,v in result.items() if k not in ('items','sample_item_ids')},ensure_ascii=False))


if __name__ == '__main__':
    main()

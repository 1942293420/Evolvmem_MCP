"""Owner-only, reversible history management; original candidates stay read-only."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import sqlite3

from evolvmem.history_candidate_read import HistoryCandidateError, HistoryCandidateReader


def management_path(environ):
    value = (environ if environ is not None else os.environ).get('EVOLVMEM_HISTORY_MANAGEMENT_DB')
    if not value:
        return None
    path = pathlib.Path(value).expanduser()
    if not path.is_absolute():
        raise HistoryCandidateError('invalid_management_configuration', 503)
    return path


class State:
    def __init__(self, path, scope=''):
        self.path = path
        self.scope = scope
        self.items = {}
        if path and path.is_file():
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as c:
                if c.execute("select count(*) from sqlite_master where name='history_management_meta'").fetchone()[0] != 1:
                    raise HistoryCandidateError('invalid_management_configuration', 503)
                meta = c.execute('select version,scope from history_management_meta').fetchone()
                if meta is None or meta[0] != 1 or scope and meta[1] != scope:
                    raise HistoryCandidateError('invalid_management_configuration', 503)
                c.row_factory = sqlite3.Row
                self.items = {(r['kind'], r['project'], r['key']): dict(r)
                              for r in c.execute('select * from trash')}

    def deleted(self, kind, project, key):
        return (kind, project, key) in self.items


class ManagedReader(HistoryCandidateReader):
    def __init__(self, db_path, state):
        super().__init__(db_path)
        self.management = state

    def project_visible(self, project):
        return not self.management.deleted('project', project, project)

    def relation_visible(self, project, block):
        return self.project_visible(project) and not self.management.deleted('project_block', project, block)

    def block_visible(self, block):
        if not self.management.items:
            return True
        if self.management.deleted('block', '', block):
            return False
        projects = [r[0] for r in self.conn.execute('select project from block_projects where block_id=?', (block,))]
        return not projects or any(self.relation_visible(p, block) for p in projects)

    def resolve_project(self, identifier):
        project = super().resolve_project(identifier)
        if not self.project_visible(project['project']):
            raise HistoryCandidateError('history_item_deleted', 410)
        return project

    def _block(self, block_id):
        block = super()._block(block_id)
        if not self.block_visible(block['block_id']):
            raise HistoryCandidateError('history_item_deleted', 410)
        return block

    def projects(self):
        rows = super().projects()
        if not self.management.items:
            return rows
        for row in rows:
            if not self.project_visible(row['project']):
                continue
            blocks = [b for b in self.conn.execute(
                'select b.* from blocks b join block_projects p on p.block_id=b.block_id where p.project=?',
                (row['project'],)) if self.relation_visible(row['project'], b['block_id'])]
            row.update(dialogue_blocks=sum(b['message_count'] > 0 for b in blocks),
                       empty_sources=sum(b['message_count'] == 0 for b in blocks),
                       messages=sum(b['message_count'] for b in blocks), turns=sum(b['turn_count'] for b in blocks))
        return [r for r in rows if self.project_visible(r['project'])]

    def project_overview(self, identifier):
        data = super().project_overview(identifier)
        if not self.management.items:
            return data
        data['history'] = [r for r in data['history'] if self.relation_visible(data['project'], r['block_id'])]
        data['history_count'] = len(data['history'])
        data['empty_source_count'] = sum(
            self.relation_visible(data['project'], r[0]) for r in self.conn.execute(
                'select b.block_id from blocks b join block_projects p on p.block_id=b.block_id '
                'where p.project=? and b.message_count=0', (data['project'],)))
        return data

    def block_projects(self, block_id):
        data = super().block_projects(block_id)
        data['projects'] = [p for p in data['projects'] if self.relation_visible(p['project'], data['block_id'])]
        return data

    def unassigned(self):
        data = super().unassigned()
        for key in ('dialogue', 'empty'):
            data[key] = [r for r in data[key] if self.block_visible(r['block_id'])]
            data[key + '_count'] = len(data[key])
        return data

    def attachment(self, sha256):
        if not isinstance(sha256, str) or not re.fullmatch('[a-f0-9]{64}', sha256):
            raise HistoryCandidateError('attachment_not_found', 404)
        if self.management.items:
            candidates = self.conn.execute('select block_id,attachments from messages where attachments like ?',
                                           ('%' + str(sha256) + '%',))
            if not any(self.block_visible(r[0]) and any(a.get('sha256') == sha256 for a in json.loads(r[1] or '[]'))
                       for r in candidates):
                raise HistoryCandidateError('attachment_not_found', 404)
        return super().attachment(sha256)


def manage(reader, path, action, body):
    if not path:
        raise HistoryCandidateError('history_management_unavailable', 503)
    if not isinstance(body, dict) or action not in ('delete', 'restore'):
        raise HistoryCandidateError('invalid_management_request', 400)
    targets = body.get('targets')
    rid = body.get('request_id')
    if not isinstance(targets, list) or not 1 <= len(targets) <= 500 or not isinstance(rid, str) or not 1 <= len(rid) <= 100:
        raise HistoryCandidateError('invalid_management_request', 400)
    fingerprint = hashlib.sha256(json.dumps([action, targets], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.resolve() == reader.db_path.resolve():
        raise HistoryCandidateError('invalid_management_configuration', 503)
    with sqlite3.connect(path, timeout=10) as c:
        names = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
        if names and 'history_management_meta' not in names:
            raise HistoryCandidateError('invalid_management_configuration', 503)
        os.chmod(path, 0o600)
        c.execute('create table if not exists history_management_meta(version integer,scope text)')
        if not c.execute('select 1 from history_management_meta').fetchone():
            c.execute('insert into history_management_meta values(1,?)', (reader.management.scope,))
        elif c.execute('select scope from history_management_meta').fetchone()[0] != reader.management.scope:
            raise HistoryCandidateError('invalid_management_configuration', 503)
        c.execute('create table if not exists trash(kind text,project text,key text,title text,deleted_at text,'
                  'primary key(kind,project,key))')
        c.execute('create table if not exists operations(request_id text primary key,fingerprint text,result text,created_at text)')
        c.commit()
        c.execute('begin immediate')
        old = c.execute('select fingerprint,result from operations where request_id=?', (rid,)).fetchone()
        if old:
            if old[0] != fingerprint:
                raise HistoryCandidateError('request_id_conflict', 409)
            return json.loads(old[1])
        normalized = {}
        for target in targets:
            if not isinstance(target, dict):
                raise HistoryCandidateError('invalid_management_target', 400)
            kind, project, key = (target.get(k, '') for k in ('kind', 'project', 'key'))
            if not all(isinstance(x, str) and len(x) <= 1000 for x in (kind, project, key)) or not key:
                raise HistoryCandidateError('invalid_management_target', 400)
            if kind == 'project':
                row = reader.conn.execute('select display_name from projects where project=?', (key,)).fetchone()
                if project != key or row is None:
                    raise HistoryCandidateError('invalid_management_target', 400)
                title = row[0] or key
            elif kind in ('block', 'project_block'):
                row = reader.conn.execute('select title from blocks where block_id=?', (key,)).fetchone()
                if row is None:
                    raise HistoryCandidateError('invalid_management_target', 400)
                relations = [r[0] for r in reader.conn.execute('select project from block_projects where block_id=?', (key,))]
                if kind == 'block' and (relations or project) or kind == 'project_block' and project not in relations:
                    raise HistoryCandidateError('invalid_management_target', 400)
                title = row[0] or key
            else:
                raise HistoryCandidateError('invalid_management_target', 400)
            normalized[(kind, project, key)] = str(title)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        for (kind, project, key), title in normalized.items():
            if action == 'delete':
                c.execute('insert or ignore into trash values(?,?,?,?,?)', (kind, project, key, title, now))
            else:
                c.execute('delete from trash where kind=? and project=? and key=?', (kind, project, key))
        result = {'ok': True, 'action': action, 'count': len(normalized), 'recoverable': True}
        c.execute('insert into operations values(?,?,?,?)', (rid, fingerprint, json.dumps(result), now))
        return result


def snippet(text, query):
    text = ' '.join(str(text or '').split())
    pos = text.casefold().find(query.casefold())
    start = max(0, pos - 40)
    return ('…' if start else '') + text[start:start + 180] + ('…' if len(text) > start + 180 else '')


def search(reader, params):
    query = str(params.get('q') or '').strip()
    scope = params.get('scope', 'projects')
    status = params.get('status', 'all')
    try:
        page = max(1, int(params.get('page', 1)))
        size = max(1, min(100, int(params.get('page_size', 50))))
    except (ValueError, TypeError):
        raise HistoryCandidateError('invalid_pagination', 400)
    if scope not in ('projects', 'unassigned') or not 1 <= len(query) <= 200:
        raise HistoryCandidateError('invalid_search_request', 400)
    hits = {}
    scope_sql = ('not exists' if scope == 'unassigned' else 'exists')
    for r in reader.conn.execute('select block_id,ordinal,role,content from messages m '
                                 'where ' + scope_sql + ' (select 1 from block_projects p where p.block_id=m.block_id) '
                                 'and instr(lower(content),lower(?))>0 order by ordinal', (query,)):
        if r['block_id'] not in hits and reader.block_visible(r['block_id']):
            hits[r['block_id']] = {'snippet': snippet(r['content'], query), 'role': r['role'], 'ordinal': r['ordinal']}
    page_cache = {}
    def match(block, hit):
        if block not in page_cache:
            found, count = 1, 0
            for turn in reader.turns_for_block(block):
                messages = ([turn['question']] if turn['question'] else []) + turn['answers']
                size = 1 + len(turn['answers'])
                if count and count + size > 50:
                    found += 1; count = 0
                if any(m['ordinal'] == hit.get('ordinal') for m in messages):
                    break
                count += size
            page_cache[block] = found
        return {**hit, 'block_id': block, 'page': page_cache[block]}
    items = []
    if scope == 'projects':
        for row in reader.projects():
            if status == 'with_history' and not row['dialogue_blocks'] or status == 'without_history' and row['dialogue_blocks']:
                continue
            overview = reader.project_overview(row['project'])
            hit = next(((b['block_id'], hits[b['block_id']]) for b in overview['history'] if b['block_id'] in hits), None)
            summary = overview['summary']
            def words(value):
                if isinstance(value, str): return value
                if isinstance(value, list): return ' '.join(words(x) for x in value)
                return ''
            meta = ' '.join([row['project'], row['display_name']] + [words(summary.get(k)) for k in
                            ('purpose','environment','main_features','necessary_info','historical_notes','unknowns')])
            title_hit = next((b for b in overview['history'] if query.casefold() in b['title'].casefold()), None)
            if hit:
                items.append({**row, 'match': {**hit[1], 'block_id': hit[0]}})
            elif title_hit:
                items.append({**row, 'match': {'block_id': title_hit['block_id'], 'page': 1, 'snippet': snippet(title_hit['title'], query)}})
            elif query.casefold() in meta.casefold():
                items.append({**row, 'match': {'snippet': snippet(meta, query)}})
    else:
        data = reader.unassigned()
        for kind in ('dialogue', 'empty'):
            if status == 'empty' and kind != 'empty' or status == 'dialogue' and kind != 'dialogue':
                continue
            for row in data[kind]:
                hit = hits.get(row['block_id'])
                if hit or query.casefold() in (row['title'] + ' ' + row['block_id']).casefold():
                    items.append({**row, '__kind': kind, 'match': {**hit, 'block_id': row['block_id']} if hit else
                                  {'snippet': snippet(row['title'], query)}})
    total = len(items)
    pages = max(1, (total + size - 1) // size)
    page = min(page, pages)
    selected = items[(page - 1) * size:page * size]
    for row in selected:
        hit = row.get('match') or {}
        if hit.get('block_id') and 'ordinal' in hit:
            row['match'] = match(hit['block_id'], hit)
    return {'ok': True, 'items': selected, 'total': total,
            'page': page, 'pages': pages, 'q': query, 'scope': scope}

"""User-authored experience notes, isolated from extracted and verified cases.

Reads open only a registered existing database in read-only mode. The first
authorized write creates this module's tables, without initializing Core,
changing source content, invoking a model, or modifying vector indexes.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

CATEGORIES = ('前端经验', '业务经验', '系统环境经验')
FIELDS = ('category', 'problem', 'solution', 'reusable_experience')
NOTE_ID = re.compile(r'manual-[a-f0-9]{32}\Z')
REQUEST_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:-]{7,127}\Z', re.ASCII)
SELECT = 'id,category,problem,solution,reusable_experience,revision,deleted_at,created_at,updated_at'
DDL = (
    '''CREATE TABLE IF NOT EXISTS manual_experience_notes(
        id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, category TEXT NOT NULL,
        problem TEXT NOT NULL, solution TEXT NOT NULL, reusable_experience TEXT NOT NULL,
        revision INTEGER NOT NULL, deleted_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''',
    '''CREATE INDEX IF NOT EXISTS manual_experience_owner ON manual_experience_notes(owner_id,deleted_at,updated_at)''',
    '''CREATE TABLE IF NOT EXISTS manual_experience_requests(
        owner_id TEXT NOT NULL, request_id TEXT NOT NULL, actor_id TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL, response_json TEXT NOT NULL, http_status INTEGER NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(owner_id,request_id))''',
    '''CREATE TABLE IF NOT EXISTS manual_experience_events(
        event_id TEXT PRIMARY KEY,owner_id TEXT NOT NULL,actor_id TEXT NOT NULL,
        request_id TEXT NOT NULL,action TEXT NOT NULL,note_id TEXT NOT NULL,
        before_json TEXT,after_json TEXT NOT NULL,created_at TEXT NOT NULL)''',
)


class ManualExperienceError(ValueError):
    def __init__(self, code, status=400):
        super().__init__(code)
        self.code, self.status = code, status


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _database(source):
    if not source or not source['data_dir']:
        return None
    path = Path(source['data_dir']).resolve()/'memory.db'
    return path if path.is_file() else None


def _row(row):
    record = dict(row)
    record.pop('keyword_match', None)
    return {**record, 'origin': 'manual'}


def _summary():
    return {'categories': dict.fromkeys(CATEGORIES, 0), 'total': 0, 'deleted_total': 0}


def _integer(value, minimum, maximum, code):
    if type(value) not in (int, str) or not re.fullmatch(r'[0-9]+', str(value)):
        raise ManualExperienceError(code)
    number = int(value)
    if not minimum <= number <= maximum:
        raise ManualExperienceError(code)
    return number


def _read_params(params):
    if set(params)-{'origin','category','q','page','page_size','deleted'}:
        raise ManualExperienceError('invalid_experience_query')
    if params.get('origin', 'manual') != 'manual':
        raise ManualExperienceError('invalid_origin')
    category = params.get('category', '')
    if category not in ('', *CATEGORIES):
        raise ManualExperienceError('invalid_experience_category')
    query = params.get('q', '')
    if not isinstance(query, str) or len(query)>200 or any(ord(c)<32 for c in query):
        raise ManualExperienceError('invalid_experience_query')
    return category, query.strip(), _integer(params.get('page',1),1,1000000000,'invalid_page'), \
        _integer(params.get('page_size',20),1,200,'invalid_page_size'), \
        _integer(params.get('deleted',0),0,1,'invalid_deleted')


def read(source, params, item_id=None, *, search=None):
    category, query, page, page_size, deleted = _read_params(params)
    if item_id is not None and (not isinstance(item_id,str) or not NOTE_ID.fullmatch(item_id)):
        raise ManualExperienceError('experience_not_found',404)
    path = _database(source)
    result = {'ok': True, 'connected': bool(path), 'experience_connected': bool(path),
              'origin': 'manual', 'rows': [], 'total': 0, 'page': page, 'page_size': page_size,
              'summary': _summary()}
    if not path:
        if item_id is not None:
            raise ManualExperienceError('experience_not_found',404)
        return {**result, 'reason':'experience_not_connected'}
    conn = sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=15)
    conn.row_factory=sqlite3.Row
    try:
        conn.execute('PRAGMA query_only=ON')
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manual_experience_notes'").fetchone():
            if item_id is not None:
                raise ManualExperienceError('experience_not_found',404)
            return result
        owner = source['user_id']
        if item_id is not None:
            row = conn.execute(f'SELECT {SELECT} FROM manual_experience_notes WHERE owner_id=? AND id=? AND deleted_at IS '+('NOT NULL' if deleted else 'NULL'),(owner,item_id)).fetchone()
            if not row:
                raise ManualExperienceError('experience_not_found',404)
            return {'ok':True,'connected':True,'experience_connected':True,'origin':'manual','experience':_row(row)}
        totals = conn.execute('''SELECT SUM(CASE WHEN deleted_at IS NULL THEN 1 ELSE 0 END),
            SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END) FROM manual_experience_notes WHERE owner_id=?''',(owner,)).fetchone()
        result['summary'].update(total=totals[0] or 0,deleted_total=totals[1] or 0)
        where = 'owner_id=? AND deleted_at IS '+('NOT NULL' if deleted else 'NULL')
        values = [owner]
        if query:
            result['search_mode'] = 'keyword'
            literal = '%'+query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
            match = "(problem LIKE ? ESCAPE '\\' OR solution LIKE ? ESCAPE '\\' OR reusable_experience LIKE ? ESCAPE '\\')"
            if search is not None and not deleted and any(char.isalnum() for char in query):
                from evolvmem.manual_experience_search import MAX_DOCUMENTS
                candidates = conn.execute(
                    f'SELECT {SELECT}, {match} AS keyword_match FROM manual_experience_notes '
                    f'WHERE {where} ORDER BY updated_at DESC,id DESC LIMIT ?',
                    [literal]*3 + values + [MAX_DOCUMENTS + 1],
                ).fetchall()
                if candidates:
                    ranked = search.rank((path, owner), query, candidates)
                    if ranked is not None:
                        result['search_mode'] = 'hybrid'
                        visible = []
                        for row in ranked:
                            if row['category'] in CATEGORIES:
                                result['summary']['categories'][row['category']] += 1
                            if not category or row['category'] == category:
                                visible.append(row)
                        offset = (page - 1) * page_size
                        result['total'] = len(visible)
                        result['rows'] = [_row(row) for row in visible[offset:offset + page_size]]
                        return result
            where += f' AND {match}'
            values += [literal]*3
        for row in conn.execute('SELECT category,count(*) FROM manual_experience_notes WHERE '+where+' GROUP BY category',values):
            if row[0] in CATEGORIES:
                result['summary']['categories'][row[0]]=row[1]
        if category:
            where += ' AND category=?'; values.append(category)
        result['total']=conn.execute('SELECT count(*) FROM manual_experience_notes WHERE '+where,values).fetchone()[0]
        rows=conn.execute(f'SELECT {SELECT} FROM manual_experience_notes WHERE '+where+' ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?',values+[page_size,(page-1)*page_size]).fetchall()
        result['rows']=[_row(row) for row in rows]
        return result
    finally:
        conn.close()


def _text(value, maximum):
    if (not isinstance(value,str) or not value.strip() or len(value)>maximum
            or any(ord(c)<32 and c not in '\n\r\t' or ord(c)==127 for c in value)):
        raise ManualExperienceError('invalid_experience_text')
    return value.strip()


def _write_params(body):
    body=dict(body)
    if body.pop('origin','manual')!='manual':
        raise ManualExperienceError('invalid_origin')
    action=body.get('action')
    if action not in ('create','update','delete','restore'):
        raise ManualExperienceError('invalid_experience_action')
    allowed={'action','request_id'} | (set(FIELDS) if action in ('create','update') else set()) | ({'id','revision'} if action!='create' else set())
    if set(body)!=allowed:
        raise ManualExperienceError('invalid_experience_fields')
    if not isinstance(body['request_id'],str) or not REQUEST_ID.fullmatch(body['request_id']):
        raise ManualExperienceError('invalid_request_id')
    if action in ('create','update'):
        if body['category'] not in CATEGORIES:
            raise ManualExperienceError('invalid_experience_category')
        for name,limit in [('problem',1000),('solution',8000),('reusable_experience',8000)]:
            body[name]=_text(body[name],limit)
    if action!='create':
        if not isinstance(body['id'],str) or not NOTE_ID.fullmatch(body['id']):
            raise ManualExperienceError('invalid_experience_id')
        body['revision']=_integer(body['revision'],1,2147483647,'invalid_experience_revision')
    return body


def write(source, actor_id, body, *, validate_new=None):
    if not source or actor_id!=source['user_id']:
        raise ManualExperienceError('experience_management_forbidden',403)
    submitted=body
    body=_write_params(body)
    path=_database(source)
    if not path:
        raise ManualExperienceError('experience_not_connected',404)
    owner=source['user_id']
    payload_sha=hashlib.sha256(_json({'owner_id':owner,'actor_id':actor_id,'body':body}).encode()).hexdigest()
    conn=sqlite3.connect(path.as_uri()+'?mode=rw',uri=True,timeout=15,isolation_level=None)
    conn.row_factory=sqlite3.Row
    try:
        conn.execute('PRAGMA busy_timeout=15000')
        conn.execute('BEGIN IMMEDIATE')
        for sql in DDL:
            conn.execute(sql)
        prior=conn.execute('SELECT * FROM manual_experience_requests WHERE owner_id=? AND request_id=?',(owner,body['request_id'])).fetchone()
        if prior:
            if prior['actor_id']!=actor_id or prior['payload_sha256']!=payload_sha:
                raise ManualExperienceError('experience_request_id_conflict',409)
            response=json.loads(prior['response_json']);response['replayed']=True
            conn.execute('COMMIT')
            return prior['http_status'],response
        # A transport can tighten the format for new notes without rejecting
        # retries of notes that satisfied the contract when they were saved.
        if validate_new is not None:
            validate_new(submitted)
        timestamp=datetime.now(timezone.utc).isoformat(timespec='seconds')
        action=body['action']; before=None
        if action=='create':
            note_id='manual-'+uuid.uuid4().hex
            conn.execute('''INSERT INTO manual_experience_notes(id,owner_id,category,problem,solution,reusable_experience,revision,deleted_at,created_at,updated_at)
                VALUES(?,?,?,?,?,?,1,NULL,?,?)''',(note_id,owner,*(body[k] for k in FIELDS),timestamp,timestamp))
        else:
            note_id=body['id']
            row=conn.execute(f'SELECT {SELECT} FROM manual_experience_notes WHERE owner_id=? AND id=?',(owner,note_id)).fetchone()
            if not row:
                raise ManualExperienceError('experience_not_found',404)
            before=_row(row)
            if row['revision']!=body['revision']:
                raise ManualExperienceError('experience_revision_conflict',409)
            if (action in ('update','delete') and row['deleted_at'] is not None
                    or action=='restore' and row['deleted_at'] is None):
                raise ManualExperienceError('experience_state_conflict',409)
            if action=='update':
                conn.execute('''UPDATE manual_experience_notes SET category=?,problem=?,solution=?,reusable_experience=?,
                    revision=revision+1,updated_at=? WHERE owner_id=? AND id=?''',(*(body[k] for k in FIELDS),timestamp,owner,note_id))
            else:
                conn.execute('''UPDATE manual_experience_notes SET deleted_at=?,revision=revision+1,updated_at=?
                    WHERE owner_id=? AND id=?''',(timestamp if action=='delete' else None,timestamp,owner,note_id))
        note=_row(conn.execute(f'SELECT {SELECT} FROM manual_experience_notes WHERE owner_id=? AND id=?',(owner,note_id)).fetchone())
        status=201 if action=='create' else 200
        response={'ok':True,'experience':note,'replayed':False}
        conn.execute('INSERT INTO manual_experience_events VALUES(?,?,?,?,?,?,?,?,?)',
                     (uuid.uuid4().hex,owner,actor_id,body['request_id'],action,note_id,_json(before) if before else None,_json(note),timestamp))
        conn.execute('INSERT INTO manual_experience_requests VALUES(?,?,?,?,?,?,?)',
                     (owner,body['request_id'],actor_id,payload_sha,_json(response),status,timestamp))
        conn.execute('COMMIT')
        return status,response
    except Exception:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()

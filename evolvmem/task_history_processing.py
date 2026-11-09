"""One explicit owner MCP grant for one immutable archive's project history.

No queue runner, ambient override, backlog scan or experience extraction. A
persisted unfinished intent is indeterminate and never automatically retried.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import re
import time

from evolvmem.lan_sharing import LanError

TOOL = 'session_archive_process_history'
PIPELINE = 'project-history:v2'
TABLE = 'task_history_grants'
SCHEMA = f"""CREATE TABLE IF NOT EXISTS {TABLE}(
    request_id TEXT PRIMARY KEY, identity_hash TEXT UNIQUE NOT NULL,
    archive_id INTEGER NOT NULL, source_sha256 TEXT NOT NULL,
    source_revision TEXT NOT NULL, project TEXT NOT NULL, source_format TEXT NOT NULL,
    task_id INTEGER, state TEXT NOT NULL, result TEXT, created_at REAL NOT NULL)"""


def prepare_schema(capture):
    """Owner LAN startup only, before health generations and physical baselines."""
    with capture.store.transaction():
        capture.conn.execute(SCHEMA)


def archive_revision(conn, archive_id):
    """Read the existing organization revision without enrolling or decrypting."""
    archive = conn.execute('SELECT payload_sha256,state FROM session_archives WHERE id=?', (archive_id,)).fetchone()
    body = conn.execute('SELECT body FROM conversation_history WHERE archive_id=?', (archive_id,)).fetchone()
    review = conn.execute('SELECT state,revision FROM knowledge_cleaning_reviews WHERE source_key=?',
                          (f'archive:{archive_id}',)).fetchone()
    if not archive or archive['state'] != 'available' or not body or not str(body[0] or '').strip() or (review and review['state'] == 'deleted'):
        return ''
    raw = hashlib.sha256(json.dumps([archive['payload_sha256'], archive['state'], body[0]], ensure_ascii=False).encode()).hexdigest()
    return hashlib.sha256((raw + ':' + str(review['revision'] if review else 0)).encode()).hexdigest()


def validate_source(capture, args):
    from evolvmem.product_policy import load_policy
    if not load_policy().valid:
        raise LanError('policy_unavailable')
    capture._identity(args)
    if (type(args.get('archive_id')) is not int or args['archive_id'] < 1
            or any(not isinstance(args.get(k), str) or not re.fullmatch('[0-9a-f]{64}', args[k])
                   for k in ('sha256', 'source_revision'))):
        raise LanError('invalid_arguments')
    receipt = capture.status({k: args[k] for k in ('device_id', 'session_id')})
    expected = {'archive_id': args['archive_id'], 'source_sha256': args['sha256'],
                'source_revision': args['source_revision'], 'project': args['project'],
                'source_format': args['source_format'], 'current': True, 'status': 'archived',
                'payload_state': 'available'}
    if any(receipt.get(k) != v for k, v in expected.items()) or receipt.get('next_offset') != receipt.get('total_bytes'):
        raise LanError('archive_revision_conflict')
    if capture.source_excluded(receipt):
        raise LanError('archive_excluded')
    if not capture.conn.execute("SELECT 1 FROM context_project_registry WHERE project=? AND status='active'", (args['project'],)).fetchone():
        raise LanError('project_not_registered')
    return receipt


@dataclass(frozen=True)
class TaskHistoryGrant:
    """Lexical capability issued only by this authenticated operation."""
    capture: object
    args: dict
    request_id: str
    task_id: int
    expires_at: float

    def assert_current(self):
        if time.monotonic() >= self.expires_at:
            raise LanError('task_history_grant_expired')
        validate_source(self.capture, self.args)
        row = self.capture.conn.execute(f'SELECT * FROM {TABLE} WHERE request_id=?', (self.request_id,)).fetchone()
        if (not row or row['state'] != 'running' or row['task_id'] != self.task_id
                or any(row[k] != self.args[k] for k in ('archive_id', 'source_revision', 'project', 'source_format'))
                or row['source_sha256'] != self.args['sha256']):
            raise LanError('task_history_grant_invalid')
        task = self.capture.conn.execute('SELECT * FROM organization_tasks WHERE id=?', (self.task_id,)).fetchone()
        if (not task or task['status'] != 'scoped' or task['stage'] != 'task_history_scoped'
                or task['source_key'] != f"archive:{self.args['archive_id']}"
                or task['source_revision'] != self.args['source_revision']):
            raise LanError('task_history_grant_invalid')

    def assert_scope(self, service, task, unit, pipeline=PIPELINE):
        self.assert_current()
        if (service is not self.capture.server.context_service or task['id'] != self.task_id
                or pipeline != PIPELINE or unit['task_id'] != self.task_id
                or unit['project'] != self.args['project'] or json.loads(unit['projects']) != [self.args['project']]
                or unit['knowledge_scope'] != 'project' or unit['disposition'] != 'history_only'
                or unit['decision'] != 'manual'):
            raise LanError('task_history_scope_forbidden')


def grant_status(conn, receipt):
    """Persisted exact intent state; running does not assert worker liveness."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return None
    row = conn.execute(f'SELECT state,task_id,source_revision FROM {TABLE} WHERE archive_id=? AND source_sha256=? '
        'AND source_revision=? AND project=? AND source_format=?',
        (receipt['archive_id'], receipt['source_sha256'], receipt['source_revision'], receipt['project'], receipt['source_format'])).fetchone()
    return dict(row) if row else None


def completed_grant(conn, receipt, task, project):
    """Query-only proof exception, never creates a grant or changes policy."""
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
        return False
    return bool(conn.execute(f"SELECT 1 FROM {TABLE} WHERE archive_id=? AND source_sha256=? "
        "AND source_revision=? AND project=? AND source_format=? AND task_id=? AND state='completed'",
        (receipt['archive_id'], receipt['source_sha256'], task['source_revision'], project,
         receipt.get('source_format', 'codex-jsonl'), task['id'])).fetchone())


def process_history(capture, args, request_id, *, authenticated_user, owner):
    if authenticated_user != 'jiangli' or owner is not True:
        raise LanError('owner_forbidden')
    from evolvmem.lan_sharing import validate_request_id
    validate_request_id(request_id)
    validate_source(capture, args)
    service, conn = capture.server.context_service, capture.conn
    identity_hash = hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    with capture.store.transaction():
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone():
            raise LanError('task_history_schema_unprepared')
        row = conn.execute(f'SELECT * FROM {TABLE} WHERE request_id=?', (request_id,)).fetchone()
        if row and row['identity_hash'] != identity_hash:
            raise LanError('request_id_conflict')
        row = row or conn.execute(f'SELECT * FROM {TABLE} WHERE identity_hash=?', (identity_hash,)).fetchone()
        if row:
            if row['state'] == 'completed':
                return json.loads(row['result'])
            raise LanError('request_indeterminate')
        key = f"archive:{args['archive_id']}"
        if conn.execute("SELECT 1 FROM organization_tasks t WHERE t.status!='superseded' AND "
            "(t.source_key=? OR t.source_key IN (SELECT c.source_key FROM organization_conversations c "
            "WHERE EXISTS(SELECT 1 FROM json_each(c.members) m WHERE m.value=?)))", (key, key)).fetchone():
            raise LanError('source_already_organized')
        conn.execute(f'INSERT INTO {TABLE}(request_id,identity_hash,archive_id,source_sha256,source_revision,'
            'project,source_format,state,created_at) VALUES(?,?,?,?,?,?,?, ?,?)',
            (request_id, identity_hash, args['archive_id'], args['sha256'], args['source_revision'],
             args['project'], args['source_format'], 'running', time.time()))
    # Intent is durable before any content read/provider call. No catch/retry:
    # failures preserve that intent for explicit inspection and reconciliation.
    from evolvmem import governance_sources, auto_organization as org
    from evolvmem.context_store import _now_iso
    from evolvmem.project_archive_pipeline import ProjectArchivePipeline
    governance_sources.apply_plan(service, governance_sources.build_plan(conn, keys=[key]), limit=1)
    source, text = org.source_snapshot(service, key)
    if source['expected_revision'] != args['source_revision']:
        raise LanError('archive_revision_conflict')
    rules = service.knowledge().rules.read()
    now = _now_iso()
    with capture.store.transaction():
        validate_source(capture, args)
        cur = conn.execute('INSERT INTO organization_tasks(source_key,source_revision,rule_revision,source_snapshot,'
            'source_title,stage,status,unit_count,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
            (key, args['source_revision'], rules['revision'], text, 'Explicit task history',
             'task_history_scoped', 'scoped', 1, now, now))
        task_id = cur.lastrowid
        digest = org.unit_digest(key, 0, len(text), text)
        conn.execute('INSERT INTO organization_units(task_id,ordinal,digest,title,text,source_start,source_end,'
            'project,projects,knowledge_scope,decision,disposition,created_at,updated_at) VALUES(?,0,?,?,?,0,?,?,?,?,?,?,?,?)',
            (task_id, digest, 'Explicit task history', text, len(text), args['project'], json.dumps([args['project']]),
             'project', 'manual', 'history_only', now, now))
        conn.execute(f'UPDATE {TABLE} SET task_id=? WHERE request_id=?', (task_id, request_id))
    grant = TaskHistoryGrant(capture, dict(args), request_id, task_id, time.monotonic() + 300)
    task = org._task_row(service, task_id)
    result = ProjectArchivePipeline(service).process_task(task, task_history_grant=grant)
    with capture.store.transaction():
        grant.assert_current()
        org._assert_current(service, task)
        result = {'state': 'completed', 'archive_id': args['archive_id'], 'source_sha256': args['sha256'],
                  'source_revision': args['source_revision'], 'source_format': args['source_format'],
                  'project': args['project'], 'task_id': task_id, **result}
        conn.execute("UPDATE organization_tasks SET status='completed',finished_at=?,updated_at=? WHERE id=?", (now, now, task_id))
        conn.execute(f"UPDATE {TABLE} SET state='completed',result=? WHERE request_id=?", (json.dumps(result), request_id))
    return result

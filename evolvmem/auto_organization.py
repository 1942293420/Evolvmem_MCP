"""Persistent background organization: whole-source units, scoped decisions, ingestion.

One bounded worker owns its own SQLite connection and processes one task at a
time. A task carries the frozen source snapshot plus the source and rule
revisions it was built from, so a repeated request never duplicates work, spans
always index the text that was actually processed, and a stale revision is
never reported as current. Model calls always happen outside database
transactions.

Ingestion reuses the existing extraction paths: one source-linked
SESSION_SUMMARY per assigned unit for project history, and one structured
knowledge candidate per unit through ``persist_legacy_extraction`` (related
lookup, duplicate sharing, add/supplement/replace/skip validation, vector
aftermath). Generated knowledge is never a verified-experience claim.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time

from evolvmem.context_store import _now_iso
from evolvmem.project_store import ProjectStoreError
from evolvmem import organization_guidance as guidance_store
from evolvmem import topic_segmentation

MAX_ATTEMPTS = 3
POLL_SECONDS = 2
PAGE_SIZE = 20
SUBJECT_LABELS = {'habit': '长期习惯', 'project_convention': '项目约定', 'task_requirement': '任务要求',
                  'environment': '环境事实', 'decision': '决策依据', 'experience': '技术经验',
                  'reference': '参考资料'}
# Generated knowledge is never a success claim: experience units always wait for
# the existing ExperienceService evidence rules before they can become active.

def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _digest(*parts) -> str:
    return hashlib.sha256('\x1f'.join(str(p) for p in parts).encode()).hexdigest()[:16]


def unit_digest(source_key, source_start, source_end, text) -> str:
    """Durable identity anchored on the logical source, range and exact text.

    The task id is deliberately absent: the same source range must keep one
    identity across retries, rule edits and background restarts, so a re-run
    updates or links the existing knowledge instead of duplicating it.
    """
    return _digest('unit', source_key, source_start, source_end, text)


def _unit_revision(row) -> str:
    if isinstance(row, sqlite3.Row):
        row = dict(row)
    return hashlib.sha256(_canonical({k: row.get(k) for k in
        ('revision', 'project', 'decision', 'reason', 'disposition', 'evidence_quote', 'cleaned_text')}).encode()).hexdigest()[:16]


def _load_llm():
    """Reuse the existing provider without reading or caching real config here."""
    from evolvmem.kimi_hooks import _call_llm_with_retry, _load_llm_config
    config = _load_llm_config()
    if config is None:
        raise ValueError('extraction_provider_unavailable')
    return config, _call_llm_with_retry


def project_names(service):
    return [p['project'] for p in service.knowledge().registry() if p['status'] == 'active']


# ------------------------------------------------- deterministic source snapshot

def source_snapshot(service, source_key):
    """Deterministic clean input for the automatic pipeline, without a manual gate.

    Reuses the shared cleaning core (transport/injected noise removed, saved
    drop-lines and duplicate collapsing applied, credentials redacted) so the
    raw source never needs a human confirmation first. Noise is set aside, never
    deleted: the archive and its payload stay available for verification.
    """
    from evolvmem.knowledge_cleaning import _clean_input, record
    row = record(service, source_key)
    policy = service.knowledge().rules.read()
    text = _clean_input(service, row, policy)
    if not isinstance(text, str) or not text.strip():
        raise ValueError('cleaning_source_unavailable')
    return row, text


def _source_title(service, source_key, text):
    from evolvmem.history_organization import _source_record
    label = ''
    try:
        label = str(_source_record(service, source_key).get('source') or '')
    except (ValueError, KeyError):
        label = ''
    first = text.strip().split('\n', 1)[0][:80]
    return (label + ' · ' + first)[:200] if label else first[:200]


def source_messages(service, source_key):
    """Cleaned messages with structural roles for one source.

    Roles come from the original structured messages (or are ``unknown`` for an
    unstructured item), never from parsing role-like prefixes inside content.
    """
    from evolvmem.knowledge_cleaning import cleaned_messages, record
    row = record(service, source_key)
    return cleaned_messages(service, row, service.knowledge().rules.read())


def _source_spans(service, task, archive_id):
    return topic_segmentation.message_spans(source_messages(service, task['source_key']))


def _source_records(service, task, row=None):
    """Structural message spans; ``segment`` turns them into numbered windows."""
    return _source_spans(service, task, None)


def _assert_current(service, task, *, conn=None):
    """Re-read source and rule revisions; a stale task must not write."""
    from evolvmem.knowledge_cleaning import record
    row = record(service, task['source_key'])
    if row['expected_revision'] != task['source_revision']:
        raise ValueError('revision_conflict')
    if service.knowledge().rules.read()['revision'] != task['rule_revision']:
        raise ValueError('revision_conflict')
    current = _task_row(service, task['id'])
    if current['status'] == 'superseded':
        raise ValueError('revision_conflict')


# ---------------------------------------------------------------- task records

def enqueue(service, source_keys, *, snapshot=True):
    """Create one task per source and rule revision; repeated requests reuse it."""
    from evolvmem.knowledge_cleaning import record
    policy = service.knowledge().rules.read()
    created, duplicates, items = 0, 0, []
    for source_key in source_keys:
        row = record(service, source_key)
        revision = row['expected_revision']
        existing = service.store._connection().execute(
            'SELECT * FROM organization_tasks WHERE source_key=? AND source_revision=? AND rule_revision=?',
            (source_key, revision, policy['revision'])).fetchone()
        if existing:
            duplicates += 1
            items.append(task_view(service, existing['id']))
            continue
        text = ''
        if snapshot:
            try:
                text = source_snapshot(service, source_key)[1]
            except (ValueError, KeyError, OSError):
                text = ''
        title = _source_title(service, source_key, text) if text else source_key
        with service.store.transaction():
            conn = service.store._connection()
            cursor = conn.execute(
                'INSERT INTO organization_tasks(source_key,source_revision,rule_revision,source_snapshot,'
                "source_title,stage,status,created_at,updated_at) VALUES(?,?,?,?,?,'queued','pending',?,?)",
                (source_key, revision, policy['revision'], text, title, _now_iso(), _now_iso()))
            self_task = cursor.lastrowid
            # Any older work for this source is superseded, including completed
            # and review rows, so the "current" list never shows a stale result.
            conn.execute("UPDATE organization_tasks SET status='superseded',superseded_by=?,finished_at=?,updated_at=? "
                         "WHERE source_key=? AND status!='superseded' AND id!=?",
                         (self_task, _now_iso(), _now_iso(), source_key, self_task))
        _mark_outputs_stale(service, source_key, current_task=self_task)
        created += 1
        items.append(task_view(service, self_task))
    return {'items': items, 'created': created, 'duplicates': duplicates,
            'rule_revision': policy['revision']}


def _mark_outputs_stale(service, source_key, *, current_task):
    """A superseded revision's outputs stop being current but stay as history.

    Only purely automatic items are downgraded: a human-confirmed project
    decision keeps its status. Nothing is deleted.
    """
    conn = service.store._connection()
    with service.store.transaction():
        rows = conn.execute(
            "SELECT u.item_id FROM organization_units u JOIN organization_tasks t ON t.id=u.task_id "
            "WHERE t.source_key=? AND t.status='superseded' AND t.id<? AND u.item_id IS NOT NULL",
            (source_key, current_task)).fetchall()
        for row in rows:
            item_id = row['item_id']
            resolution = conn.execute(
                'SELECT decision_source FROM context_project_resolutions WHERE item_id=?', (item_id,)).fetchone()
            if resolution and resolution['decision_source'] == 'human':
                continue
            conn.execute("UPDATE context_items SET status='candidate',updated_at=? "
                         "WHERE id=? AND status='active'", (_now_iso(), item_id))


def _task_row(service, task_id):
    """The full stored row, including the frozen source snapshot."""
    row = service.store._connection().execute(
        'SELECT * FROM organization_tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        raise ValueError('task_not_found')
    return dict(row)


def task_view(service, task_id):
    view = _task_row(service, task_id)
    view.pop('source_snapshot', None)
    view['source_current'] = _source_current(service, view)
    return view


def _source_current(service, task):
    """False when the stored source revision is no longer the latest one.

    A task whose source changed but has not been re-enqueued yet must not be
    presented as the current result.
    """
    if task['status'] == 'superseded':
        return False
    try:
        from evolvmem.knowledge_cleaning import record
        return record(service, task['source_key'])['expected_revision'] == task['source_revision']
    except (ValueError, KeyError, OSError):
        return False


def list_tasks(service, options=None):
    options = options or {}
    conn = service.store._connection()
    where, args = ['1=1'], []
    status = options.get('status')
    if status == 'current':
        where.append("status!='superseded'")
    elif status and status != 'all':
        where.append('status=?')
        args.append(status)
    if options.get('current'):
        where.append("status!='superseded'")
    page = max(1, int(options.get('page', 1) or 1))
    rows = conn.execute('SELECT id FROM organization_tasks WHERE ' + ' AND '.join(where) +
                        ' ORDER BY id DESC', args).fetchall()
    ids = [r['id'] for r in rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]]
    counts = {name: conn.execute(
        'SELECT count(*) c FROM organization_tasks WHERE status=?', (name,)).fetchone()['c']
        for name in ('pending', 'running', 'completed', 'review', 'failed', 'superseded')}
    views = [task_view(service, i) for i in ids]
    return {'items': views, 'total': len(rows), 'page': page,
            'page_size': PAGE_SIZE, 'counts': counts,
            'current_task_ids': [r['id'] for r in views if r['source_current']]}


def task_detail(service, task_id):
    row = service.store._connection().execute(
        'SELECT * FROM organization_tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        raise ValueError('task_not_found')
    task = {k: v for k, v in dict(row).items() if k != 'source_snapshot'}
    return {**task, 'source_text': row['source_snapshot'], 'units': unit_views(service, task_id)}


def unit_views(service, task_id):
    rows = service.store._connection().execute(
        'SELECT * FROM organization_units WHERE task_id=? ORDER BY ordinal', (task_id,)).fetchall()
    return [{**dict(row), 'revision': _unit_revision(row)} for row in rows]


def find_unit(service, task_id, digest):
    row = service.store._connection().execute(
        'SELECT * FROM organization_units WHERE task_id=? AND digest=?', (task_id, digest)).fetchone()
    if row is None:
        raise ValueError('unit_not_found')
    return dict(row)


def retry(service, task_id):
    """Explicit retry. Resume the furthest completed stage; never re-segment by default."""
    with service.store.transaction():
        conn = service.store._connection()
        row = conn.execute('SELECT * FROM organization_tasks WHERE id=?', (task_id,)).fetchone()
        if row is None:
            raise ValueError('task_not_found')
        if row['status'] == 'superseded':
            raise ValueError('task_superseded')
        has_units = conn.execute(
            'SELECT 1 FROM organization_units WHERE task_id=? LIMIT 1', (task_id,)).fetchone() is not None
        stage = 'assignment' if has_units else 'queued'
        conn.execute("UPDATE organization_tasks SET status='pending',stage=?,error_code='',error_detail='',"
                     'finished_at=NULL,updated_at=? WHERE id=?', (stage, _now_iso(), task_id))
    return {'ok': True, 'status': 'pending', 'stage': stage,
            'task': task_view(service, task_id)}


def resegment(service, task_id):
    """Explicitly discard stored units and segment the current source again.

    The snapshot is re-derived from the current valid source and the source and
    rule revisions are checked first. Keeping a frozen snapshot while records
    come from the current cleaned messages would shift every offset after a
    runtime cleaning change, so a stale task is sent back to enqueue instead of
    being silently resegmented. A task that already carries a human decision is
    never discarded by resegmentation: the reviewer must resolve it first.
    """
    task = _task_row(service, task_id)
    if task['status'] == 'superseded':
        raise ValueError('task_superseded')
    manual = service.store._connection().execute(
        "SELECT 1 FROM organization_units WHERE task_id=? AND decision='manual' LIMIT 1",
        (task_id,)).fetchone()
    if manual:
        raise ValueError('resegment_manual_review_required')
    _assert_current(service, task)
    _, text = source_snapshot(service, task['source_key'])
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE organization_tasks SET source_snapshot=?,source_title=?,status='pending',stage='queued',"
            "unit_count=0,review_count=0,error_code='',error_detail='',finished_at=NULL,updated_at=? WHERE id=?",
            (text, _source_title(service, task['source_key'], text), _now_iso(), task_id))
    return {'ok': True, 'status': 'pending', 'stage': 'queued', 'task': task_view(service, task_id)}


# ---------------------------------------------------------------- pipeline

class OrganizationWorker:
    """One bounded background worker with its own connection and thread."""

    def __init__(self, config, *, mode=None):
        self.config = config
        # The worker must run the same Context mode as the foreground service;
        # the config default alone (legacy) cannot produce context knowledge.
        self.mode = mode
        self._service = None
        self._thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._owns_thread = False

    # -- lifecycle --

    def _context_schema_present(self):
        """True when the database already carries the Context tables."""
        import sqlite3 as _sqlite3
        try:
            conn = _sqlite3.connect(f'file:{self.config.db_path}?mode=ro', uri=True)
            try:
                return conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_items'").fetchone() is not None
            finally:
                conn.close()
        except _sqlite3.Error:
            return False

    def _resolved_mode(self):
        from evolvmem.context_models import ContextMode
        from evolvmem.web_server import _context_mode
        mode = self.mode or _context_mode(self.config)
        # The organizer only writes Context knowledge. A database that already
        # has the Context schema but a legacy/compat serving mode runs the
        # worker in shadow mode, which never changes how requests are served.
        if mode in (ContextMode.LEGACY, ContextMode.COMPAT) and self._context_schema_present():
            return ContextMode.SHADOW
        return mode

    def _connect(self):
        if self._service is None:
            from evolvmem.context_models import ContextMode
            from evolvmem.context_service import ContextService
            mode = self._resolved_mode()
            if mode in (ContextMode.LEGACY, ContextMode.COMPAT):
                raise ValueError('organization_needs_context_mode')
            # No embedding engine is passed: the worker must not load a second
            # model just to organize text.
            service = ContextService(self.config)
            service.initialize(mode=mode, adapter='auto-organization')
            self._service = service
        return self._service

    def start(self):
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._owns_thread = True
        self._thread = threading.Thread(target=self._run, name='evolvmem-organization', daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout=10):
        """Cooperative stop: the thread closes its own connection in its finally."""
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread:
            thread.join(timeout=timeout)
        if thread is None or not thread.is_alive():
            self._close_service()
        return not (thread and thread.is_alive())

    def _close_service(self):
        service, self._service = self._service, None
        if service is not None:
            service.close()

    def wake(self):
        self._wake.set()

    def tasks(self, options=None):
        return list_tasks(self._connect(), options)['items']

    def units(self, task_id):
        return unit_views(self._connect(), task_id)

    def _run(self):
        try:
            try:
                self._restore()
            except Exception:
                pass
            while not self._stop.is_set():
                worked = False
                try:
                    if self._stop.is_set():
                        break
                    worked = self.tick()
                    if not worked and not self._stop.is_set():
                        try:
                            from evolvmem.organization_arrival import discover_new
                            worked = bool(discover_new(self._connect())['created'])
                        except Exception:
                            worked = False
                except Exception:
                    worked = False
                if worked:
                    continue
                self._wake.wait(POLL_SECONDS)
                self._wake.clear()
        finally:
            # Closing here is safe: this is the only thread using the connection.
            self._close_service()
            self._owns_thread = False

    def _restore(self):
        """Interrupted work returns to the queue instead of hanging forever."""
        service = self._connect()
        with service.store.transaction():
            service.store._connection().execute(
                "UPDATE organization_tasks SET status='pending',updated_at=? WHERE status='running'",
                (_now_iso(),))

    # -- one unit of work --

    def tick(self):
        service = self._connect()
        task = self._claim(service)
        if task is None:
            return False
        self._process(service, task)
        return True

    def _claim(self, service):
        conn = service.store._connection()
        with service.store.transaction():
            row = conn.execute(
                "SELECT id FROM organization_tasks WHERE status='pending' ORDER BY id LIMIT 1").fetchone()
            if row is None:
                return None
            cursor = conn.execute(
                "UPDATE organization_tasks SET status='running',attempts=attempts+1,started_at=?,updated_at=? "
                "WHERE id=? AND status='pending'",
                (_now_iso(), _now_iso(), row['id']))
            if cursor.rowcount != 1:
                return None
        return task_view(service, row['id'])

    def _is_current(self, service, task):
        """A newer task for the same source owns the result; stale work stops."""
        newer = service.store._connection().execute(
            'SELECT id FROM organization_tasks WHERE source_key=? AND id>? LIMIT 1',
            (task['source_key'], task['id'])).fetchone()
        return newer is None

    def _process(self, service, task):
        task_id = task['id']
        try:
            if not self._is_current(service, task):
                self._finish(service, task_id, 'superseded', 'stale', 'stale_task')
            elif task['stage'] in ('queued', 'cleaning', 'segmentation'):
                self._segment(service, task)
            elif task['stage'] == 'assignment':
                self._assign(service, task)
            elif task['stage'] == 'extraction':
                self._extract_units(service, task)
            else:
                self._finish(service, task_id, 'completed', 'done')
        except topic_segmentation.SegmentationError as error:
            self._fail(service, task_id, 'review', 'coverage_' + error.code if error.code in
                       ('coverage_gap', 'coverage_overlap', 'quote_not_found') else error.code,
                       error.detail)
        except ValueError as error:
            code = str(error)
            if code in ('revision_conflict', 'cleaning_source_deleted', 'classification_no_longer_unassigned',
                        'classification_source_changed', 'invalid_items', 'extraction_provider_unavailable',
                        'organization_needs_context_mode'):
                self._fail(service, task_id, 'review' if code == 'revision_conflict' else 'failed', code, '')
            else:
                self._fail(service, task_id, 'failed', 'organization_failed', code)
        except Exception as error:  # provider or storage failure; task stays visible
            self._fail(service, task_id, 'failed', 'organization_failed', type(error).__name__)

    def _fail(self, service, task_id, status, code, detail):
        task = task_view(service, task_id)
        # Bounded retries: a transient provider/storage failure returns to the
        # queue until MaxAttempts, then becomes an explicit, visible failure.
        final = 'pending' if status == 'failed' and task['attempts'] < MAX_ATTEMPTS else status
        with service.store.transaction():
            service.store._connection().execute(
                "UPDATE organization_tasks SET status=?,error_code=?,error_detail=?,finished_at=?,updated_at=? WHERE id=?",
                (final, code, str(detail or '')[:500], None if final == 'pending' else _now_iso(),
                 _now_iso(), task_id))

    def _finish(self, service, task_id, status, stage, error_code='', detail=''):
        with service.store.transaction():
            service.store._connection().execute(
                'UPDATE organization_tasks SET status=?,stage=?,error_code=?,error_detail=?,finished_at=?,updated_at=? WHERE id=?',
                (status, stage, error_code, str(detail or '')[:500], _now_iso(), _now_iso(), task_id))

    def _touch(self, service, task_id, stage, status='pending'):
        with service.store.transaction():
            service.store._connection().execute(
                'UPDATE organization_tasks SET stage=?,status=?,updated_at=? WHERE id=?',
                (stage, status, _now_iso(), task_id))

    # -- stage 1: whole-source segmentation --

    def _segment(self, service, task):
        row = _task_row(service, task['id'])
        text = row['source_snapshot'] or ''
        if not text.strip():
            _, text = source_snapshot(service, task['source_key'])
            with service.store.transaction():
                service.store._connection().execute(
                    'UPDATE organization_tasks SET source_snapshot=?,source_title=?,updated_at=? WHERE id=?',
                    (text, _source_title(service, task['source_key'], text), _now_iso(), task['id']))
            row = _task_row(service, task['id'])
        _assert_current(service, task)
        if self._stop.is_set():
            raise ValueError('worker_stopped')
        policy = service.knowledge().rules.read()
        records = _source_records(service, task, row)
        projects = [p['project'] + '（' + '、'.join([p['display_name'], *p.get('aliases', [])]) + '）'
                    for p in service.knowledge().registry() if p['status'] == 'active']
        config, call = _load_llm()

        def provider(request):
            # The same cancellation check guards every provider call, including
            # the one coverage correction: a stop between them is not ignored.
            if self._stop.is_set():
                raise ValueError('worker_stopped')
            return call(request, config, deadline=time.monotonic() + 90)

        units = topic_segmentation.segment(
            text, provider,
            records=records, cleaning_instructions=policy['settings']['cleaning_instructions'],
            projects=projects)
        if self._stop.is_set():
            raise ValueError('worker_stopped')
        # After the model calls, the source and rules must still be the ones the
        # snapshot was taken from, otherwise the result would be stale.
        _assert_current(service, task)
        if not self._is_current(service, task):
            self._finish(service, task['id'], 'superseded', 'stale', 'stale_task')
            return
        self._store_units(service, row, units)
        self._touch(service, task['id'], 'assignment')

    def _store_units(self, service, task, units):
        conn = service.store._connection()
        # Structural spans from the original messages; the snapshot text is only
        # a fallback for a source whose messages cannot be re-read.
        messages = source_messages(service, task['source_key'])
        spans = topic_segmentation.message_spans(messages) if messages else \
            topic_segmentation.message_spans([{'role': 'unknown', 'content': task['source_snapshot']}])
        with service.store.transaction():
            _assert_current(service, task, conn=conn)
            prior = {r['digest']: dict(r) for r in conn.execute(
                "SELECT u.* FROM organization_units u JOIN organization_tasks t ON t.id=u.task_id "
                "WHERE t.source_key=? AND u.decision='manual' ORDER BY t.id", (task['source_key'],))}
            conn.execute('DELETE FROM organization_units WHERE task_id=?', (task['id'],))
            for ordinal, unit in enumerate(units):
                digest = unit_digest(task['source_key'], unit['source_start'], unit['source_end'], unit['text'])
                role = next((span['role'] for span in spans
                             if span['start'] <= unit['source_start'] < span['end']), '')
                disposition = unit.get('disposition') or 'keep'
                if disposition not in topic_segmentation.DISPOSITIONS:
                    disposition = 'review'
                conn.execute(
                    'INSERT INTO organization_units(task_id,ordinal,digest,title,text,cleaned_text,source_start,'
                    "source_end,category,role,disposition,disposition_reason,project_hint,project,decision,reason,"
                    "evidence_quote,extraction_stage,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'','review','',?,'pending',?,?)",
                    (task['id'], ordinal, digest, unit['title'], unit['text'],
                     unit.get('cleaned_text') or unit['text'], unit['source_start'], unit['source_end'],
                     unit['category'], role, disposition, unit.get('disposition_reason', ''),
                     unit['project_hint'], unit.get('evidence_quote', ''), _now_iso(), _now_iso()))
                old = prior.get(digest)
                if old:
                    conn.execute("UPDATE organization_units SET project=?,decision='manual',reason=?,"
                                 "revision=?,disposition=? WHERE task_id=? AND digest=?",
                                 (old['project'], old['reason'], old['revision'] + 1,
                                  old['disposition'], task['id'], digest))
            conn.execute('UPDATE organization_tasks SET unit_count=?,updated_at=? WHERE id=?',
                         (len(units), _now_iso(), task['id']))

    # -- stage 2: project assignment --

    def _assign(self, service, task):
        _assert_current(service, task)
        policy = service.knowledge().rules.read()
        units = [dict(r) for r in service.store._connection().execute(
            'SELECT * FROM organization_units WHERE task_id=? ORDER BY ordinal', (task['id'],))]
        if not units:
            raise topic_segmentation.SegmentationError('coverage_gap', '没有可归属的单元')
        names = set(project_names(service))
        auto_projects, review = [], 0
        for unit in units:
            if unit['decision'] == 'manual':
                # A human decision is never re-decided by a later automatic pass.
                continue
            if unit['disposition'] == 'set_aside':
                continue
            if unit['disposition'] == 'review':
                self._update_unit(service, task['id'], unit['digest'], project=unit['project'] or '',
                                  decision='review', expected_revision=unit,
                                  reason=unit['disposition_reason'])
                review += 1
                continue
            guidance = guidance_store.classify(service, unit)
            if guidance['status'] == 'review':
                self._update_unit(service, task['id'], unit['digest'], project='', decision='review',
                                  expected_revision=unit, reason=guidance['reason'])
                review += 1
                continue
            if guidance['status'] == 'apply':
                conflict = self._guidance_conflict(service, unit, guidance['project'], policy)
                if conflict:
                    self._update_unit(service, task['id'], unit['digest'], project='', decision='review',
                                      expected_revision=unit, reason=conflict)
                    review += 1
                    continue
                self._update_unit(service, task['id'], unit['digest'], project=guidance['project'],
                                  decision='auto', expected_revision=unit, reason=guidance['reason'])
                auto_projects.append(guidance['project'])
                continue
            if guidance['status'] == 'pending':
                self._update_unit(service, task['id'], unit['digest'], project='', decision='review',
                                  expected_revision=unit, reason=guidance['reason'])
                review += 1
                continue
            hint = unit['project_hint'] if unit['project_hint'] in names else ''
            project, reason, decision = self._decide(service, unit, hint, policy, names)
            self._update_unit(service, task['id'], unit['digest'], project=project, decision=decision,
                              expected_revision=unit, reason=reason)
            if decision == 'auto':
                auto_projects.append(project)
            else:
                review += 1
        if self._stop.is_set():
            raise ValueError('worker_stopped')
        _assert_current(service, task)
        # A mixed or partly unresolved source is never folded into one project;
        # units are attributed independently and the archive stays unbound.
        # Unit ownership is authoritative; leave the immutable whole archive
        # unbound so later corrections cannot leak the original mixed source.
        self._ingest(service, task)
        _refresh_task_state(service, task['id'])

    def _reload_units(self, service, task_id):
        return [dict(r) for r in service.store._connection().execute(
            'SELECT * FROM organization_units WHERE task_id=? ORDER BY ordinal', (task_id,))]

    def _guidance_conflict(self, service, unit, project, policy):
        """Guidance never overrides conflicting project evidence in the unit."""
        kb = service.knowledge()
        evaluated = kb.rules.evaluate({'body': unit['text'], 'source': True}, kb.registry(), policy=policy)
        others = [candidate for candidate in evaluated['candidates'] if candidate.casefold() != project.casefold()]
        if others:
            return '你的指导与这段正文里的其他项目线索冲突（' + '、'.join(others) + '），需要人工确认'
        return ''

    def _decide(self, service, unit, hint, policy, names):
        """Program validation decides; the model hint alone is never the gate."""
        if not names:
            return '', '没有已登记项目，先登记项目再整理', 'review'
        kb = service.knowledge()
        evidence_quote = unit.get('evidence_quote', '')
        if evidence_quote and evidence_quote not in unit['text']:
            return '', '模型引用的证据不在原文中，需要人工核对', 'review'
        if not hint:
            evaluated = kb.rules.evaluate({'body': unit['text'], 'source': True}, kb.registry(), policy=policy)
            candidates = evaluated['candidates']
            if len(candidates) == 1:
                return candidates[0], '正文只点名一个已登记项目，按规则自动归属', 'auto'
            return '', ('正文点名多个项目，需要人工拆分' if candidates
                        else '正文没有明确项目线索，等待人工选择'), 'review'
        evaluated = kb.rules.evaluate({'body': unit['text'], 'source': True},
                                      kb.registry(), policy=policy)
        if hint.casefold() not in {p.casefold() for p in evaluated['candidates']}:
            return '', '模型建议缺少正文项目证据，等待人工确认', 'review'
        candidates = [name for name in evaluated['candidates'] if name.casefold() != hint.casefold()]
        if len(candidates) > 1:
            return '', '项目线索冲突，不能覆盖其他项目归属', 'review'
        if not candidates:
            return hint, '正文与已登记项目一致，按规则自动归属', 'auto'
        if evidence_quote and evidence_quote in unit['text'] and candidates[0] in evidence_quote:
            return candidates[0], '引用原话指向另一个已登记项目，按证据归属', 'auto'
        return '', '这段同时提到' + '、'.join(candidates) + '与' + hint + '，需要人工拆分', 'review'

    def _update_unit(self, service, task_id, digest, *, project, decision, expected_revision=None,
                     reason='', evidence_quote=None):
        """CAS inside the transaction; a manual decision is never overwritten."""
        with service.store.transaction():
            conn = service.store._connection()
            row = conn.execute('SELECT * FROM organization_units WHERE task_id=? AND digest=?',
                               (task_id, digest)).fetchone()
            if row is None:
                raise ValueError('unit_not_found')
            if row['decision'] == 'manual':
                return False
            if expected_revision is not None and _unit_revision(expected_revision) != _unit_revision(row):
                raise ValueError('revision_conflict')
            conn.execute(
                'UPDATE organization_units SET project=?,decision=?,reason=?,evidence_quote=?,'
                'revision=revision+1,updated_at=? WHERE task_id=? AND digest=?',
                (project, decision, str(reason or '')[:600],
                 str(row['evidence_quote'] if evidence_quote is None else evidence_quote)[:300],
                 _now_iso(), task_id, digest))
            return True

    # -- stage 3: ingestion through the existing extraction paths --

    def _ingest(self, service, task, *, extract=True):
        """History + extraction through the shared per-unit pipeline.

        Every source kind uses the same real cleaned messages and structural
        spans, so an item's offsets and author are never re-derived from a
        prefixed transport form. The frozen snapshot is only a fallback for a
        source whose messages cannot be re-read, and it stays author-unknown.
        """
        messages = source_messages(service, task['source_key'])
        spans = topic_segmentation.message_spans(messages) if messages else \
            topic_segmentation.message_spans([{'role': 'unknown',
                                               'content': _task_row(service, task['id'])['source_snapshot']}])
        _ingest_units(service, task, spans, extract=extract)

    def _extract_units(self, service, task):
        """Second pass: only the units whose extraction stage is incomplete."""
        self._ingest(service, task, extract=True)
        if not self._is_current(service, task):
            self._finish(service, task['id'], 'superseded', 'stale', 'stale_task')
            return
        _refresh_task_state(service, task['id'])

def _cas_unit_review(service, task_id, unit, reason):
    """Mark one unit for review with a revision check, preserving manual decisions."""
    with service.store.transaction():
        conn = service.store._connection()
        row = conn.execute('SELECT * FROM organization_units WHERE task_id=? AND digest=?',
                           (task_id, unit['digest'])).fetchone()
        if row is None or row['decision'] == 'manual':
            return False
        conn.execute("UPDATE organization_units SET decision='review',reason=?,revision=revision+1,"
                     'updated_at=? WHERE task_id=? AND digest=?',
                     (str(reason)[:600], _now_iso(), task_id, unit['digest']))
        return True


def _ingest_units(service, task, spans, *, extract=True):
    """Deterministic history first, then real extraction for pending units.

    History is written for every confirmed unit without a model call, so the
    project record is available even while extraction runs. Extraction only ever
    sees this unit's own messages, and an unchanged completed result is skipped.
    """
    from evolvmem import unit_extraction
    if spans and isinstance(spans[0], dict) and 'part' not in spans[0]:
        spans = topic_segmentation.message_spans(
            [{'role': span.get('role', 'unknown'), 'content': span.get('content', '')} for span in spans])
    units = [dict(r) for r in service.store._connection().execute(
        'SELECT * FROM organization_units WHERE task_id=? ORDER BY ordinal', (task['id'],))]
    for unit in units:
        if not unit['project'] or unit['decision'] == 'review' or unit['disposition'] == 'set_aside':
            continue
        messages = topic_segmentation.unit_messages(unit, spans)
        if not messages:
            _cas_unit_review(service, task['id'], unit, '没有可核对的原始消息，保留待确认，未写入知识')
            continue
        _assert_current(service, task)
        if not unit['item_id']:
            item_id = unit_extraction.write_history(service, task, unit, messages)
            if item_id is None:
                _cas_unit_review(service, task['id'], unit, '历史记录写入未完成，保留待确认')
                continue
            with service.store.transaction():
                service.store._connection().execute(
                    'UPDATE organization_units SET item_id=?,updated_at=? WHERE task_id=? AND digest=?',
                    (item_id, _now_iso(), task['id'], unit['digest']))
        else:
            unit_extraction._link_provenance(
                service, unit['item_id'], unit_extraction._archive_id(task), task, unit)
    if not extract:
        return
    for unit in unit_extraction.pending_units(service, task['id']):
        if unit['disposition'] == 'set_aside':
            continue
        unit_extraction.extract_unit(service, task, unit, spans)


# ---------------------------------------------------------------- corrections

def correct(service, body):
    """Apply human corrections; a batch request keeps per-item outcomes.

    A batch carries an ``items`` list and every entry is validated and written
    on its own, so one stale or conflicting entry never changes another one and
    partial success is reported exactly.
    """
    if isinstance(body.get('items'), list):
        return correct_batch(service, body)
    return correct_one(service, body)


def correct_batch(service, body):
    entries = body['items']
    if not 1 <= len(entries) <= 100 or any(not isinstance(e, dict) for e in entries):
        raise ValueError('invalid_items')
    results, guidance = [], []
    for entry in entries:
        try:
            result = correct_one(service, {**body, **entry}, record_guidance=True)
            results.append({'digest': entry.get('digest'), 'ok': True, 'unit': result['unit'],
                            'guidance': result.get('guidance')})
            if result.get('guidance'):
                guidance.append(result['guidance'])
        except (ValueError, ProjectStoreError, sqlite3.IntegrityError) as error:
            code = str(error) if not isinstance(error, sqlite3.IntegrityError) else 'identity_conflict'
            results.append({'digest': entry.get('digest'), 'ok': False, 'error': code})
    return {'items': results, 'succeeded': sum(r['ok'] for r in results),
            'failed': sum(not r['ok'] for r in results), 'guidance': guidance}


def correct_one(service, body, *, record_guidance=True):
    """One atomic human correction: unit CAS, guidance and derived knowledge.

    The unit revision is checked inside the same transaction that writes the
    unit and the guidance, so a rejected correction can never leave a guidance
    row behind. Derived knowledge follows the new project (shared knowledge is
    never moved), and a project change queues a fresh extraction.
    """
    task_id = body.get('task_id')
    digest = body.get('digest')
    if type(task_id) is not int or not isinstance(digest, str):
        raise ValueError('invalid_items')
    task = task_view(service, task_id)
    if task['status'] == 'superseded':
        raise ValueError('task_superseded')
    _assert_current(service, task)
    project = str(body.get('project') or '')
    reason = str(body.get('reason') or ('人工修正：' + (body.get('guidance') or '用户核对')))[:600]
    if project:
        service.knowledge()._project(project)
    expected = body.get('expected_revision')
    text = str(body.get('guidance') or '').strip()
    scope = 'future' if body.get('scope') == 'future' else 'batch'
    stored = None
    with service.store.transaction():
        conn = service.store._connection()
        row = conn.execute('SELECT * FROM organization_units WHERE task_id=? AND digest=?',
                           (task_id, digest)).fetchone()
        if row is None:
            raise ValueError('unit_not_found')
        if _unit_revision(row) != expected:
            raise ValueError('revision_conflict')
        if text and record_guidance:
            stored = guidance_store.record(
                service, guidance=text, scope=scope, project=project or row['project'],
                condition=str(body.get('condition') or ''), exceptions=body.get('exceptions', ''),
                negative=body.get('negative') is True, source_text=text, source_task_id=task_id)
        decision = 'manual'
        if body.get('negative') is True:
            decision = 'review'
            reason = '你标记的反例：' + (text or '不要再套用此指导')
        changed_project = project != row['project']
        conn.execute(
            'UPDATE organization_units SET project=?,decision=?,reason=?,revision=revision+1,'
            "extraction_stage=CASE WHEN ? THEN 'pending' ELSE extraction_stage END,updated_at=? "
            'WHERE task_id=? AND digest=?',
            (project, decision, reason, 1 if changed_project else 0, _now_iso(), task_id, digest))
    refreshed = find_unit(service, task_id, digest)
    if project:
        item_id = _ensure_unit_item(service, task, refreshed)
        if item_id:
            _apply_manual_project(service, item_id, project, reason)
        else:
            # The decision is kept, but nothing can be written for a unit whose
            # original message cannot be re-read; say so instead of failing.
            with service.store.transaction():
                service.store._connection().execute(
                    'UPDATE organization_units SET reason=?,updated_at=? WHERE task_id=? AND digest=?',
                    ((reason + '（未写入知识：没有可核对的原始消息）')[:600], _now_iso(), task_id, digest))
    elif refreshed['item_id']:
        _unresolve_item(service, refreshed['item_id'], reason)
    if changed_project:
        _follow_derived(service, task_id, digest, project, reason)
    _refresh_task_state(service, task_id)
    refreshed = find_unit(service, task_id, digest)
    return {'ok': True, 'unit': {**refreshed, 'revision': _unit_revision(refreshed)},
            'guidance': stored, 'scope': (stored or {}).get('scope', 'batch'),
            'task': task_view(service, task_id)}


def _follow_derived(service, task_id, digest, project, reason):
    """Move or supersede only this unit's own derived knowledge.

    Knowledge shared with another source keeps its links and is re-extracted
    instead of being silently moved.
    """
    from evolvmem import unit_derivations
    task = task_view(service, task_id)
    rows = unit_derivations.derived_ids(service, task_id, digest)
    session_ref = f'{task["source_key"]}#{digest}'
    archive_id = int(task['source_key'].split(':')[1]) if task['source_key'].startswith('archive:') else None
    for row in rows:
        if row['kind'] != 'knowledge':
            continue
        item_id = row['item_id']
        try:
            detail = service.knowledge().detail(item_id)
            if project and detail['project'] != project and detail['content_type'] == 'experience':
                # A changed project is a new applicability scope. Preserve the
                # parent case and its evidence; the derived case starts unverified.
                case = service.experiences()._payload(item_id)
                child = service.experiences().record(
                    {**case, 'project': project, 'parent_experience_id': item_id})
                unit_derivations.record_derivation(service, task_id, digest, child['id'],
                                                   kind='knowledge', project=project)
                if not unit_derivations.is_shared(service, item_id, archive_id=archive_id,
                                                  unit_ref=session_ref, session_ref=session_ref):
                    unit_derivations.supersede(service, item_id, '归属范围已修正，保留原始验证记录')
                continue
            # Capture a currently-valid Q&A before the project changes; only its
            # project field moves, so it can be re-recorded through the normal
            # Q&A path (which re-checks conflicts) instead of being left stale.
            snapshot = (unit_derivations.qa_move_snapshot(service, item_id)
                        if project and detail['project'] != project else None)
            if not project and not unit_derivations.is_shared(service, item_id, archive_id=archive_id,
                                                              unit_ref=session_ref, session_ref=session_ref):
                unit_derivations.supersede(service, item_id, reason or '归属已撤回，先停止展示')
            elif project and unit_derivations.retarget(service, item_id, project, archive_id=archive_id,
                                           unit_ref=session_ref, session_ref=session_ref):
                unit_derivations.update_project(service, task_id, digest, item_id, project)
                unit_derivations.rerecord_moved_qa(service, item_id, snapshot)
            else:
                _candidate_reason(service, item_id,
                                  '该知识同时来自其他来源，未随本次修正迁移；本单元会重新提炼')
        except (ValueError, ProjectStoreError):
            continue
    if task['status'] in ('completed', 'review', 'failed'):
        with service.store.transaction():
            service.store._connection().execute(
                "UPDATE organization_tasks SET status='pending',stage='extraction',error_code='',"
                'error_detail=?,finished_at=NULL,updated_at=? WHERE id=?',
                ('归属修正后重新提炼', _now_iso(), task_id))


def _candidate_reason(service, item_id, reason):
    with service.store.transaction():
        service.store._connection().execute(
            'UPDATE knowledge_metadata SET ingestion_reason=? WHERE item_id=?',
            (str(reason)[:500], int(item_id)))


def _ensure_unit_item(service, task, unit):
    """Deterministic history for a just-assigned unit; extraction stays queued."""
    if unit['item_id']:
        return unit['item_id']
    from evolvmem import unit_extraction
    messages = source_messages(service, task['source_key'])
    spans = topic_segmentation.message_spans(messages) if messages else \
        topic_segmentation.message_spans([{'role': 'unknown',
                                           'content': _task_row(service, task['id'])['source_snapshot']}])
    unit = dict(unit)
    if not topic_segmentation.unit_messages(unit, spans):
        return None
    item_id = unit_extraction.write_history(service, task, unit,
                                            topic_segmentation.unit_messages(unit, spans))
    if item_id is None:
        return None
    with service.store.transaction():
        service.store._connection().execute(
            'UPDATE organization_units SET item_id=?,extraction_stage=?,updated_at=? '
            'WHERE task_id=? AND digest=?',
            (item_id, 'pending', _now_iso(), task['id'], unit['digest']))
    return item_id


def _apply_manual_project(service, item_id, project, reason):
    """Use the existing assign/transition primitives so ownership and vectors follow."""
    kb = service.knowledge()
    row = kb.detail(item_id)
    if row['project'] != project:
        kb.assign(item_id, {'project': project, 'expected_revision': row['revision']})
        row = kb.detail(item_id)
    # A human-confirmed unit is real history; other categories stay candidates
    # until their own evidence/rules admit them.
    if row['content_type'] == 'session_summary' and row['status'] != 'active':
        kb.transition(item_id, {'expected_revision': row['revision'], 'action': 'publish'})


def _unresolve_item(service, item_id, reason):
    """Clear a project without turning the item into a global preference."""
    from evolvmem.project_models import ProjectResolutionDecision
    from evolvmem.project_store import ProjectStore
    kb = service.knowledge()
    row = kb.detail(item_id)
    with service.store.transaction():
        conn = service.store._connection()
        conn.execute("UPDATE context_items SET project='',scope='project',status='candidate',updated_at=? "
                     'WHERE id=?', (_now_iso(), item_id))
        conn.execute('UPDATE knowledge_metadata SET ingestion_reason=? WHERE item_id=?', (reason, item_id))
        ProjectStore(conn, service.store._require_transaction, generic_names=()).record_resolution(
            item_id, ProjectResolutionDecision.unresolved('auto-organization.v1', ()))
    # The item stays a candidate; empty project means unresolved, never global.
    kb._sync([item_id])


def _refresh_task_state(service, task_id):
    units = [dict(r) for r in service.store._connection().execute(
        'SELECT * FROM organization_units WHERE task_id=? ORDER BY ordinal', (task_id,))]
    pending = [unit for unit in units if unit['disposition'] != 'set_aside']
    review = sum(1 for unit in pending if unit['decision'] == 'review' or not unit['project'])
    assigned = [u for u in pending if u['project'] and u['decision'] != 'review']
    failed = any(u['extraction_stage'] == 'failed' for u in assigned)
    unfinished = any(u['extraction_stage'] in ('pending', 'running') for u in assigned)
    status, stage, error = ('failed', 'extraction', 'extraction_failed') if failed else (
        ('pending', 'extraction', '') if unfinished else
        ('review', 'done', 'units_need_review') if review else ('completed', 'done', ''))
    with service.store.transaction():
        conn = service.store._connection()
        if units:
            conn.execute('UPDATE organization_tasks SET review_count=?,status=?,stage=?,error_code=?,'
                         'finished_at=?,updated_at=? WHERE id=? AND status!=?',
                         (review, status, stage, error, None if unfinished else _now_iso(),
                          _now_iso(), task_id, 'superseded'))
    return task_view(service, task_id)


def set_disposition(service, body):
    """Set a unit aside or restore it; nothing is ever deleted."""
    task_id, digest = body.get('task_id'), body.get('digest')
    if type(task_id) is not int or not isinstance(digest, str):
        raise ValueError('invalid_items')
    disposition = str(body.get('disposition') or '')
    if disposition not in topic_segmentation.DISPOSITIONS:
        raise ValueError('invalid_disposition')
    reason = str(body.get('reason') or '')[:300]
    if disposition != 'keep' and not reason:
        raise ValueError('disposition_reason_required')
    with service.store.transaction():
        conn = service.store._connection()
        row = conn.execute('SELECT * FROM organization_units WHERE task_id=? AND digest=?',
                           (task_id, digest)).fetchone()
        if row is None:
            raise ValueError('unit_not_found')
        if _unit_revision(row) != body.get('expected_revision'):
            raise ValueError('revision_conflict')
        stage = 'pending' if disposition == 'keep' else row['extraction_stage']
        conn.execute(
            'UPDATE organization_units SET disposition=?,disposition_reason=?,extraction_stage=?,'
            'revision=revision+1,updated_at=? WHERE task_id=? AND digest=?',
            (disposition, reason, stage, _now_iso(), task_id, digest))
    _refresh_task_state(service, task_id)
    refreshed = find_unit(service, task_id, digest)
    return {'ok': True, 'unit': {**refreshed, 'revision': _unit_revision(refreshed)}}


def guidance_list(service):
    return {'items': guidance_store.rows(service, enabled_only=False)}


def guidance_update(service, body):
    guidance_id = body.get('id')
    if 'enabled' not in body:
        raise ValueError('invalid_request')
    updated = guidance_store.set_enabled(service, guidance_id, bool(body['enabled']),
                                        expected_revision=body.get('expected_revision'))
    return {'items': [updated]}


def dispatch(service, method, route, body):
    if route == '':
        return list_tasks(service, body)
    if route == '/detail':
        return task_detail(service, body.get('task_id'))
    if route == '/units':
        return {'items': unit_views(service, body.get('task_id'))}
    if route == '/guidance':
        if method == 'GET':
            return guidance_list(service)
        return guidance_update(service, body)
    if route == '/settings':
        from evolvmem import organization_arrival
        if method == 'GET':
            return organization_arrival.settings(service)
        return organization_arrival.update_settings(service, body)
    if method == 'POST' and route == '/backlog':
        from evolvmem import organization_arrival
        return organization_arrival.backlog(service, body)
    if method == 'POST' and route == '/tasks':
        return enqueue(service, _keys(body))
    if method == 'POST' and route == '/retry':
        return retry(service, body.get('task_id'))
    if method == 'POST' and route == '/resegment':
        return resegment(service, body.get('task_id'))
    if method == 'POST' and route == '/correct':
        return correct(service, body)
    if method == 'POST' and route == '/disposition':
        return set_disposition(service, body)
    raise LookupError('route_not_found')


def _keys(body):
    items = body.get('items')
    if (not isinstance(items, list) or not 1 <= len(items) <= 100
            or any(not isinstance(e, dict) or not isinstance(e.get('key'), str) for e in items)
            or len({e['key'] for e in items}) != len(items)):
        raise ValueError('invalid_items')
    return [e['key'] for e in items]

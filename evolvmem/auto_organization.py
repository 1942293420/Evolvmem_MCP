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
from evolvmem.knowledge_cleaning import SourceNoDialogue
from evolvmem.project_store import ProjectStoreError
from evolvmem import organization_context
from evolvmem import organization_guidance as guidance_store
from evolvmem import topic_segmentation

MAX_ATTEMPTS = 3
POLL_SECONDS = 2
PAGE_SIZE = 20
# Bounded per-index work for one background discovery/reconcile tick.
RECONCILE_LIMIT = 20
# A single claim never spins on more than this many retired candidate rows.
CLAIM_PREFLIGHT_LIMIT = 5
# A single claim pass examines at most this many pending rows, so deferring a
# batch that waits for its own same-session predecessor stays bounded.
CLAIM_SCAN_LIMIT = 20
# A valid archive with no dialogue is a truthful skip, not a provider failure.
NO_DIALOGUE_CODE = 'source_no_dialogue'
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
        ('revision', 'project', 'decision', 'reason', 'disposition', 'evidence_quote', 'cleaned_text',
         'continues_context', 'context_basis', 'applied_guidance_id')}).encode()).hexdigest()[:16]


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

    A readable archive whose cleaned dialogue is empty raises
    ``SourceNoDialogue``: it is a truthful skip instead of three provider
    retries. The same holds when a saved cleaning policy legitimately filters a
    readable source down to nothing. A missing or undecryptable archive keeps
    the existing ``cleaning_source_unavailable`` error.
    """
    from evolvmem.knowledge_cleaning import _clean_input, record, source_readable
    row = record(service, source_key)
    if not row.get('dialogue_available', True):
        raise SourceNoDialogue(NO_DIALOGUE_CODE)
    policy = service.knowledge().rules.read()
    text = _clean_input(service, row, policy)
    if not isinstance(text, str) or not text.strip():
        if source_readable(service, row):
            raise SourceNoDialogue(NO_DIALOGUE_CODE)
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


# ------------------------------------------------- conservative unit coalescing

def _manual_ranges(service, source_key):
    """Source ranges a human already decided on; a merge must never touch them."""
    return [(row['source_start'], row['source_end']) for row in service.store._connection().execute(
        "SELECT u.source_start,u.source_end FROM organization_units u "
        "JOIN organization_tasks t ON t.id=u.task_id "
        "WHERE t.source_key=? AND u.decision='manual'", (source_key,))]


def _continuation(kb, policy, projects, manual, previous, current, text):
    """The two locatable quotes when the pair is one continuing same-project task.

    Every condition is program-checked against the real unit text and the real
    project rules; a model hint never presses a conflicting rule candidate
    down, and anything uncertain returns ``None`` so the pair stays split.
    """
    if previous.get('disposition') != 'keep' or current.get('disposition') != 'keep':
        return None
    if current.get('continues_previous') is not True:
        return None
    if current['source_start'] < previous['source_end']:
        # Overlapping or backwards slices are never swallowed into one unit.
        return None
    if 'chunk_index' not in previous or previous.get('chunk_index') != current.get('chunk_index'):
        return None
    hint = str(previous.get('project_hint') or '')
    if not hint or hint != str(current.get('project_hint') or ''):
        return None
    project = projects.get(hint.casefold())
    if not project:
        return None
    if text[previous['source_end']:current['source_start']].strip():
        return None
    if any(start < current['source_end'] and end > previous['source_start'] for start, end in manual):
        return None
    previous_quote = topic_segmentation.resolve_evidence_quote(
        text[previous['source_start']:previous['source_end']], previous.get('evidence_quote'))
    current_quote = topic_segmentation.resolve_evidence_quote(
        text[current['source_start']:current['source_end']], current.get('evidence_quote'))
    if not previous_quote or not current_quote:
        return None
    judged = kb.rules.evaluate({'body': previous['text'], 'source': True}, kb.registry(), policy=policy)
    if [name.casefold() for name in judged['candidates']] != [project.casefold()]:
        return None
    judged = kb.rules.evaluate({'body': current['text'], 'source': True}, kb.registry(), policy=policy)
    if any(name.casefold() != project.casefold() for name in judged['candidates']):
        return None
    return previous_quote, current_quote


def _join_units(previous, current, text, quote):
    """One merged unit: the whole original slice, bounded summaries, no upgrade."""
    start, end = previous['source_start'], current['source_end']
    cleaned = (str(previous.get('cleaned_text') or '') + '\n'
               + str(current.get('cleaned_text') or '')).strip()[:2000]
    category = previous.get('category') if previous.get('category') == current.get('category') else 'reference'
    return {**previous, 'text': text[start:end], 'source_start': start, 'source_end': end,
            'title': previous.get('title') or '', 'cleaned_text': cleaned, 'category': category,
            'evidence_quote': quote, 'start_id': previous.get('start_id'),
            'end_id': current.get('end_id'), 'disposition': 'keep',
            'disposition_reason': previous.get('disposition_reason') or ''}


def coalesce_units(service, task, units, text, policy):
    """Join adjacent units that are one continuing, same-project task.

    The merge is deliberately narrow: both units must be ``keep``, the later one
    must declare ``continues_previous`` as a real boolean true, both must carry
    the same non-empty registered project hint, the previous group must be the
    only project its own text names while the current text names no other
    project, and both must carry a quote that locates in the real text
    (horizontal whitespace only may differ). A rule-candidate conflict is never
    pressed down by the hint, a manual decision on the same range blocks the
    merge, units from different chunks are never joined, and nothing is merged
    across real gap content. The input list and the merged result are both
    validated against the whole source, so an existing overlap or gap can never
    be hidden by a merge. The merged unit keeps the complete original ``text``
    slice, its start/end offsets and the first verifiable project quote.
    """
    # A merge must never hide an existing overlap, gap or invented span, and the
    # result must still cover the very same source range.
    topic_segmentation.coverage(text, units)
    if len(units) < 2:
        return [dict(unit) for unit in units]
    kb = service.knowledge()
    projects = {row['project'].casefold(): row['project'] for row in kb.registry()
                if row['status'] == 'active'}
    manual = _manual_ranges(service, task['source_key'])
    merged: list[dict] = []
    for unit in units:
        previous = merged[-1] if merged else None
        pair = _continuation(kb, policy, projects, manual, previous, unit, text) if previous else None
        if pair:
            merged[-1] = _join_units(previous, unit, text, pair[0])
        else:
            merged.append(dict(unit))
    topic_segmentation.coverage(text, merged)
    return merged


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
    """Create one task per source and rule revision; repeated requests reuse it.

    A verified system source is never queued at all, and a readable archive with
    no dialogue is recorded once as a completed skip instead of a pending task
    that would spend three provider attempts. Both keep their raw archive.
    """
    from evolvmem.history_organization import source_excluded
    from evolvmem.knowledge_cleaning import record
    policy = service.knowledge().rules.read()
    created, duplicates, items = 0, 0, []
    for source_key in source_keys:
        if source_excluded(service, source_key):
            continue
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
            except SourceNoDialogue:
                item = _record_skip(service, source_key, revision, policy['revision'], NO_DIALOGUE_CODE)
                created += 1
                items.append(item)
                continue
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


def _record_skip(service, source_key, revision, rule_revision, code):
    """One completed, attempt-free task recording why no provider work is due."""
    with service.store.transaction():
        conn = service.store._connection()
        cursor = conn.execute(
            'INSERT INTO organization_tasks(source_key,source_revision,rule_revision,source_snapshot,'
            "source_title,stage,status,error_code,attempts,created_at,updated_at,finished_at) "
            "VALUES(?,?,?,'',?,'done','completed',?,0,?,?,?)",
            (source_key, revision, rule_revision, source_key, code, _now_iso(), _now_iso(), _now_iso()))
        task_id = cursor.lastrowid
        conn.execute("UPDATE organization_tasks SET status='superseded',superseded_by=?,finished_at=?,updated_at=? "
                     "WHERE source_key=? AND status!='superseded' AND id!=?",
                     (task_id, _now_iso(), _now_iso(), source_key, task_id))
    _mark_outputs_stale(service, source_key, current_task=task_id)
    return task_view(service, task_id)


def _mirror_legacy_candidate(service, item_ids):
    """Mirror this exact downgrade into the mapped legacy projection.

    The legacy ``memories.status`` is part of the formal recall gate, so a
    downgrade that only edits ``context_items`` leaves ``projection_lag`` behind
    forever. Only the ids this call actually changed are written, inside the
    caller's transaction: a legacy row that is not ``active`` is never touched,
    so an archived or superseded row is never resurrected and a candidate is
    never re-activated.
    """
    ids = sorted({int(item_id) for item_id in item_ids})
    if not ids or not service.store.legacy_memory_table_exists():
        return 0
    conn = service.store._connection()
    placeholders = ','.join('?' for _ in ids)
    rows = conn.execute(
        'SELECT m.id AS legacy_id FROM memories m JOIN legacy_memory_migrations migration '
        f'ON migration.legacy_memory_id=m.id WHERE migration.context_item_id IN ({placeholders}) '
        "AND m.status='active'", ids).fetchall()
    moved = 0
    for row in rows:
        conn.execute('UPDATE memories SET status=?,updated_at=? WHERE id=?',
                     ('candidate', _now_iso(), row['legacy_id']))
        moved += 1
    return moved


def _own_archive_ids(source_key):
    try:
        return {int(str(source_key).split(':')[1])} if str(source_key).startswith('archive:') else set()
    except (ValueError, IndexError):
        return set()


def _has_live_output_owner(conn, item_id):
    """True while a non-superseded task still owns this item.

    Both output kinds count: the unit history item link and the knowledge
    derivation row.
    """
    if conn.execute(
            "SELECT 1 FROM organization_units u JOIN organization_tasks t ON t.id=u.task_id "
            "WHERE u.item_id=? AND t.status!='superseded' LIMIT 1", (int(item_id),)).fetchone():
        return True
    return conn.execute(
        "SELECT 1 FROM unit_derivations d JOIN organization_tasks t ON t.id=d.unit_task_id "
        "WHERE d.item_id=? AND t.status!='superseded' LIMIT 1", (int(item_id),)).fetchone() is not None


def _independent_source_link(conn, item_id, *, own_keys, own_archives):
    """True when a source link outside this source's own provenance backs the item.

    Own provenance is an ``organization``/``session`` link whose ref or archive
    is this source (``archive:N#digest@start-end``, ``archive:N`` or the archive
    id itself). A link to another archive, a manual/experience/migration entry or
    an organization link from another source is independent backing, so shared
    history stays active. An ``extraction`` companion ref only counts as
    independent when the item has no own-archive link at all, so the same
    pipeline is never mistaken for corroboration.
    """
    rows = conn.execute('SELECT source_kind,archive_id,source_ref FROM context_sources WHERE item_id=?',
                        (int(item_id),)).fetchall()
    linked_to_own_archive = any(
        row['archive_id'] is not None and int(row['archive_id']) in own_archives for row in rows)
    for row in rows:
        kind = str(row['source_kind'] or '')
        ref = str(row['source_ref'] or '')
        archive = int(row['archive_id']) if row['archive_id'] is not None else None
        if archive is not None and archive in own_archives:
            continue
        if any(ref == key or ref.startswith(key + '#') for key in own_keys):
            continue
        if kind == 'extraction' and linked_to_own_archive:
            continue
        return True
    return False


def _mark_outputs_stale(service, source_key, *, current_task):
    """A superseded revision's outputs stop being current but stay as history.

    Candidates are every output the retired tasks of this source produced: their
    unit history item (``organization_units.item_id``) and their knowledge
    derivations (``unit_derivations``). Only purely automatic items are
    downgraded: a human-confirmed project decision keeps its status, an item a
    live (non-superseded) task still owns is a current output, and an item an
    independent source link backs is shared history. A source-only output with no
    other backing never stays active. The same transaction mirrors the new
    candidate status onto the exact legacy rows it changed. Nothing is deleted.
    """
    conn = service.store._connection()
    retired = [dict(row) for row in conn.execute(
        "SELECT id FROM organization_tasks WHERE source_key=? AND status='superseded' AND id<?",
        (source_key, current_task))]
    if not retired:
        return
    own_keys, own_archives = {str(source_key)}, _own_archive_ids(source_key)
    with service.store.transaction():
        candidates = set()
        for task in retired:
            for row in conn.execute(
                    'SELECT item_id FROM organization_units WHERE task_id=? AND item_id IS NOT NULL',
                    (task['id'],)):
                candidates.add(int(row['item_id']))
            for row in conn.execute(
                    'SELECT item_id FROM unit_derivations WHERE unit_task_id=?', (task['id'],)):
                candidates.add(int(row['item_id']))
        downgraded = []
        for item_id in sorted(candidates):
            if _has_live_output_owner(conn, item_id):
                continue
            resolution = conn.execute(
                'SELECT decision_source FROM context_project_resolutions WHERE item_id=?',
                (item_id,)).fetchone()
            if resolution and resolution['decision_source'] == 'human':
                continue
            if _independent_source_link(conn, item_id, own_keys=own_keys, own_archives=own_archives):
                continue
            cursor = conn.execute("UPDATE context_items SET status='candidate',updated_at=? "
                                  "WHERE id=? AND status='active'", (_now_iso(), item_id))
            if cursor.rowcount:
                downgraded.append(item_id)
        _mirror_legacy_candidate(service, downgraded)
    if downgraded:
        # After the commit, drop these exact ids from the active-only Context
        # vector cache and the legacy index. Otherwise the database says
        # candidate while the indexes still hold the old ids, and the vector
        # counts keep disagreeing with the rows.
        service.knowledge()._sync(downgraded)


def _task_row(service, task_id):
    """The full stored row, including the frozen source snapshot."""
    row = service.store._connection().execute(
        'SELECT * FROM organization_tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        raise ValueError('task_not_found')
    return dict(row)


def task_view(service, task_id, *, visible=None):
    view = _task_row(service, task_id)
    view.pop('source_snapshot', None)
    view['context'] = organization_context.decode(view.get('context_basis'))
    view['source_current'] = _source_current(service, view, visible=visible)
    return view


def _source_current(service, task, *, visible=None):
    """False when the stored source revision is no longer the latest one.

    A task whose source changed but has not been re-enqueued yet must not be
    presented as the current result. A recorded no-dialogue skip is likewise not
    a current organization result.
    """
    if task['status'] == 'superseded' or task.get('error_code') == NO_DIALOGUE_CODE:
        return False
    try:
        from evolvmem.knowledge_cleaning import record
        return record(service, task['source_key'], visible=visible)['expected_revision'] == task['source_revision']
    except (ValueError, KeyError, OSError):
        return False


def list_tasks(service, options=None):
    options = options or {}
    conn = service.store._connection()
    where, args = ['1=1'], []
    status = options.get('status')
    # A recorded no-dialogue skip is not a current organization result.
    current_only = "status!='superseded' AND (error_code IS NULL OR error_code<>?)"
    if status == 'current':
        where.append(current_only)
        args.append(NO_DIALOGUE_CODE)
    elif status and status != 'all':
        where.append('status=?')
        args.append(status)
    if options.get('current'):
        where.append(current_only)
        args.append(NO_DIALOGUE_CODE)
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


# ------------------------------------------------- source eligibility reconcile

def _table_exists(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _archive_row(service, archive_id):
    conn = service.store._connection()
    return conn.execute('SELECT * FROM session_archives WHERE id=?', (int(archive_id),)).fetchone()


def _dialogue_state(service, archive_id):
    """'readable' / 'empty' / 'unavailable' for one archive.

    Delegates to the shared cleaning helper so the queue verdict and the
    organization verdict can never disagree. A non-empty cleaned body never
    triggers a decryption; only a genuinely empty candidate pays for one, and an
    available row whose ciphertext is missing or corrupt stays 'unavailable'.
    """
    from evolvmem.knowledge_cleaning import archive_dialogue_state
    return archive_dialogue_state(service, archive_id)


def _head_archive_map(service):
    """(device_id, session_id) -> the archive the head pointer selects, or None."""
    conn = service.store._connection()
    if not _table_exists(conn, 'lan_session_heads') or not _table_exists(conn, 'lan_session_uploads'):
        return {}
    rows = conn.execute(
        'SELECT h.device_id,h.session_id,u.archive_id FROM lan_session_heads h '
        'LEFT JOIN lan_session_uploads u ON u.device_id=h.device_id AND u.session_id=h.session_id '
        'AND u.sha256=h.sha256 WHERE h.sha256 IS NOT NULL').fetchall()
    return {(row['device_id'], row['session_id']):
            (int(row['archive_id']) if row['archive_id'] is not None else None) for row in rows}


def _upload_families(service, archive_id):
    """The (device_id, session_id) upload families one archive belongs to."""
    conn = service.store._connection()
    if not _table_exists(conn, 'lan_session_uploads'):
        return []
    return [(row['device_id'], row['session_id']) for row in conn.execute(
        'SELECT DISTINCT device_id,session_id FROM lan_session_uploads WHERE archive_id=?',
        (int(archive_id),))]


def _newest_family_sibling(service, archive):
    """Newest archive id of the same logical family, if any.

    The logical family is filtered in SQL before the bound, so unrelated archive
    rows between two versions of one family never hide the newer one. Only
    bounded metadata is read; no body is decrypted.
    """
    from evolvmem.session_identity import SQL_LOGICAL_IDENTITY, logical_identity
    rows = service.store._connection().execute(
        'SELECT sa.id FROM session_archives sa WHERE sa.adapter=? AND sa.id>? AND '
        + SQL_LOGICAL_IDENTITY.format(alias='sa') + '=? ORDER BY sa.id DESC LIMIT 1',
        (archive['adapter'], int(archive['id']),
         logical_identity(archive['adapter'], archive['external_session_id']))).fetchone()
    return int(rows['id']) if rows is not None else None


def _newest_readable_sibling(service, archive):
    """Newest same-family archive whose dialogue is actually readable, if any.

    Family rows are selected in SQL first (newest first) and only those are
    checked for readability, so an unrelated row can never crowd the window and
    only a genuinely empty candidate pays for a decryption.
    """
    from evolvmem.session_identity import SQL_LOGICAL_IDENTITY, logical_identity
    rows = service.store._connection().execute(
        'SELECT sa.id FROM session_archives sa WHERE sa.adapter=? AND sa.id>? AND '
        + SQL_LOGICAL_IDENTITY.format(alias='sa') + '=? ORDER BY sa.id DESC LIMIT ?',
        (archive['adapter'], int(archive['id']),
         logical_identity(archive['adapter'], archive['external_session_id']),
         RECONCILE_LIMIT)).fetchall()
    for row in rows:
        if _dialogue_state(service, row['id']) == 'readable':
            return int(row['id'])
    return None


def _version_state(service, archive_id):
    """(is_authoritative_current, successor_archive_id) for one archived version.

    Uploaded snapshots follow ``lan_session_heads``: a history-only upload that
    arrives later never displaces the head, and re-capturing an earlier archive
    with a newer ``source_order`` restores it as current. A family without a head
    row keeps the legacy rule used by the visibility oracle (the newest archive
    of the same logical family is current). Independent local incremental batches
    are always their own current version. The successor is only returned when it
    is genuinely known and readable.
    """
    from evolvmem.session_identity import is_incremental_batch
    archive = _archive_row(service, archive_id)
    if archive is None:
        return True, None
    if is_incremental_batch(archive['external_session_id']):
        return True, None
    heads = _head_archive_map(service)
    for family in _upload_families(service, archive_id):
        if family not in heads:
            continue
        head_archive = heads[family]
        if head_archive is None or int(head_archive) == int(archive_id):
            return True, None
        successor = int(head_archive) if _dialogue_state(service, head_archive) == 'readable' else None
        return False, successor
    sibling = _newest_family_sibling(service, archive)
    if sibling is None or int(sibling) <= int(archive_id):
        return True, None
    return False, _newest_readable_sibling(service, archive)


def _successor_task_id(service, successor_archive_id):
    if successor_archive_id is None:
        return None
    row = service.store._connection().execute(
        'SELECT id FROM organization_tasks WHERE source_key=? ORDER BY id DESC',
        (f'archive:{successor_archive_id}',)).fetchone()
    return row['id'] if row else None


def _archive_id_sql(alias='t'):
    return f"CAST(substr({alias}.source_key,9) AS INTEGER)"


def _head_mismatch_sql(archive_id_sql):
    """SQL: this archive's family has a head that selects another version."""
    return (f"EXISTS (SELECT 1 FROM lan_session_uploads u JOIN lan_session_heads h "
            f"ON h.device_id=u.device_id AND h.session_id=u.session_id "
            f"JOIN lan_session_uploads c ON c.device_id=h.device_id AND c.session_id=h.session_id "
            f"AND c.sha256=h.sha256 WHERE u.archive_id={archive_id_sql} AND h.sha256 IS NOT NULL "
            f"AND c.archive_id IS NOT NULL AND c.archive_id<>u.archive_id)")


def _head_absent_sql(archive_id_sql):
    return (f"NOT EXISTS (SELECT 1 FROM lan_session_uploads lu2 JOIN lan_session_heads h2 "
            f"ON h2.device_id=lu2.device_id AND h2.session_id=lu2.session_id "
            f"WHERE lu2.archive_id={archive_id_sql})")


def _legacy_sibling_sql(archive_id_sql, joins='', projection='1'):
    """SQL: a newer archive of the same logical family exists.

    Identity equality happens in SQL before any bound, so unrelated archives
    between two versions cannot hide the newer one, and independent incremental
    batches never match each other.
    """
    from evolvmem.session_identity import SQL_LOGICAL_IDENTITY
    left = SQL_LOGICAL_IDENTITY.format(alias='sa')
    right = SQL_LOGICAL_IDENTITY.format(alias='sb')
    return (f"EXISTS (SELECT {projection} FROM session_archives sa JOIN session_archives sb "
            f"ON sb.adapter=sa.adapter AND sb.id>sa.id AND {right}={left} {joins} "
            f"WHERE sa.id={archive_id_sql})")


def _successor_task_exists_sql(service, archive_id_sql):
    """SQL: a task already exists for the version that supersedes this archive."""
    conn = service.store._connection()
    if not _table_exists(conn, 'lan_session_uploads'):
        return _legacy_sibling_sql(
            archive_id_sql, joins="JOIN organization_tasks st ON st.source_key='archive:'||sb.id")
    head_link = (f"EXISTS (SELECT 1 FROM lan_session_uploads u JOIN lan_session_heads h "
                 f"ON h.device_id=u.device_id AND h.session_id=u.session_id "
                 f"JOIN lan_session_uploads c ON c.device_id=h.device_id AND c.session_id=h.session_id "
                 f"AND c.sha256=h.sha256 JOIN organization_tasks st ON st.source_key='archive:'||c.archive_id "
                 f"WHERE u.archive_id={archive_id_sql} AND h.sha256 IS NOT NULL "
                 f"AND c.archive_id IS NOT NULL AND c.archive_id<>u.archive_id)")
    if not _table_exists(conn, 'lan_session_heads'):
        return _legacy_sibling_sql(
            archive_id_sql, joins="JOIN organization_tasks st ON st.source_key='archive:'||sb.id")
    legacy_link = _legacy_sibling_sql(
        archive_id_sql, joins="JOIN organization_tasks st ON st.source_key='archive:'||sb.id")
    return f"({head_link} OR ({_head_absent_sql(archive_id_sql)} AND {legacy_link}))"


def _link_superseded_successors(service):
    """Point already-superseded automatic tasks at their version's task, if any.

    A reconciliation tick can retire a version before its successor has a task
    (the successor arrives in the same discovery pass). Only rows whose known
    successor **already has a task** are selected, in SQL, so a bound of
    unlinkable rows can never starve a later link; each linked row then leaves
    the set. A row whose version is current again is left untouched.
    """
    conn = service.store._connection()
    if not _table_exists(conn, 'organization_tasks'):
        return 0
    eligible = _successor_task_exists_sql(service, _archive_id_sql('t'))
    rows = conn.execute(
        "SELECT t.id FROM organization_tasks t WHERE t.status='superseded' "
        "AND t.superseded_by IS NULL AND t.source_key LIKE 'archive:%' AND "
        + eligible + " ORDER BY t.id LIMIT ?", (RECONCILE_LIMIT,)).fetchall()
    linked = 0
    for row in rows:
        task = conn.execute('SELECT source_key FROM organization_tasks WHERE id=?',
                            (int(row['id']),)).fetchone()
        if task is None:
            continue
        try:
            current, successor = _version_state(service, int(task['source_key'].split(':')[1]))
        except (ValueError, TypeError, IndexError):
            continue
        if current:
            continue
        task_id = _successor_task_id(service, successor)
        if task_id is None:
            continue
        with service.store.transaction():
            conn.execute('UPDATE organization_tasks SET superseded_by=?,updated_at=? WHERE id=?',
                         (task_id, _now_iso(), row['id']))
        linked += 1
    return linked


def _has_manual_units(conn, task_id):
    """A human decision on any unit protects its whole task, at any status."""
    return conn.execute(
        "SELECT 1 FROM organization_units WHERE task_id=? AND decision='manual' LIMIT 1",
        (int(task_id),)).fetchone() is not None


def _downgrade_superseded_outputs(service, source_key, *, before_id):
    """Existing stale-output policy: downgrade automatic outputs below a boundary.

    Thin wrapper over ``_mark_outputs_stale`` that keeps its current-task
    semantics for the enqueue path while letting a retirement settle the retired
    task itself (``before_id`` is exclusive).
    """
    _mark_outputs_stale(service, source_key, current_task=before_id)


def _retire_task(service, task_id, *, status, reason, successor=None):
    """Retire one automatic task; a human decision is never touched.

    The task keeps its attempt count, gets a truthful terminal status, and only
    a ``superseded`` verdict settles its own outputs through the existing
    stale-output policy (candidate + legacy mirror + vector sync). Nothing is
    published or deleted.
    """
    if task_id is None:
        return False
    with service.store.transaction():
        conn = service.store._connection()
        row = conn.execute('SELECT * FROM organization_tasks WHERE id=?', (int(task_id),)).fetchone()
        if row is None or row['status'] == 'superseded':
            return False
        if _has_manual_units(conn, task_id):
            # A human decided units of this source: keep the visible result and
            # its units exactly as they are, whatever the task status is.
            return False
        conn.execute('UPDATE organization_tasks SET status=?,error_code=?,error_detail=?,superseded_by=?,'
                     'finished_at=?,updated_at=? WHERE id=?',
                     (status, reason, '', successor, _now_iso(), _now_iso(), int(task_id)))
        source_key = row['source_key']
    if status == 'superseded' and isinstance(source_key, str):
        _downgrade_superseded_outputs(service, source_key, before_id=int(task_id) + 1)
    # Only finish retirement after output settlement succeeds. Shared outputs
    # intentionally stay active; they must not keep this retired task scanning.
    with service.store.transaction():
        conn.execute('UPDATE organization_tasks SET stage=?,updated_at=? WHERE id=?',
                     ('retired' if status == 'superseded' else 'done', _now_iso(), int(task_id)))
    return True


def _reconcile_candidates(service, *, limit):
    """Bounded, fair candidate list for source reconciliation.

    Candidates are chosen by an actionable predicate, never by raw id order
    alone, so unrelated pending/review rows can never starve a later stale
    source. Every candidate is one ``_settle_ineligible`` can genuinely settle
    (or leave as a real error), and a settled row leaves the set on the next
    tick, so repeated bounded scans keep advancing through the table. Completed
    tasks are included while they still own an active automatic output of a
    retired source; a task with any manual unit is never a candidate.
    """
    conn = service.store._connection()
    archive_id_sql = _archive_id_sql('t')
    not_manual = ("NOT EXISTS (SELECT 1 FROM organization_units mu WHERE mu.task_id=t.id "
                  "AND mu.decision='manual')")
    human_guard = ""
    if _table_exists(conn, 'context_project_resolutions'):
        human_guard = (" AND NOT EXISTS (SELECT 1 FROM context_project_resolutions r "
                       "WHERE r.item_id=i.id AND r.decision_source='human')")
    # The task's own live output. ``lt.id<>t.id`` is essential: a completed task
    # owns its own item, so counting itself as a live owner would exclude exactly
    # the obsolete completed automatic rows this pass must settle.
    own_active_parts = [
        "EXISTS (SELECT 1 FROM organization_units u JOIN context_items i ON i.id=u.item_id "
        "WHERE u.task_id=t.id AND i.status='active' "
        "AND NOT EXISTS (SELECT 1 FROM organization_units lu JOIN organization_tasks lt "
        "ON lt.id=lu.task_id WHERE lu.item_id=u.item_id AND lt.status!='superseded' "
        "AND lt.id<>t.id)" + human_guard + ")"]
    if _table_exists(conn, 'unit_derivations'):
        # Knowledge derivations are outputs too, not only the unit history item.
        own_active_parts.append(
            "EXISTS (SELECT 1 FROM unit_derivations d JOIN context_items i ON i.id=d.item_id "
            "WHERE d.unit_task_id=t.id AND i.status='active' "
            "AND NOT EXISTS (SELECT 1 FROM unit_derivations ld JOIN organization_tasks lt "
            "ON lt.id=ld.unit_task_id WHERE ld.item_id=d.item_id AND lt.status!='superseded' "
            "AND lt.id<>t.id)" + human_guard + ")")
    own_active = "(" + " OR ".join(own_active_parts) + ")"
    unsettled = "t.status IN ('pending','running','failed','review')"
    params, branches = [], []
    # A readable archive with no dialogue: one cheap one-shot skip, never a
    # provider retry loop. Readability (ciphertext present) is re-verified by the
    # settle step before this becomes a success.
    branches.append(
        f"(t.status='pending' AND t.attempts=0 AND EXISTS (SELECT 1 FROM conversation_history ch "
        f"JOIN session_archives sav ON sav.id=ch.archive_id WHERE ch.archive_id={archive_id_sql} "
        f"AND sav.state='available' AND trim(ch.body)=''))")
    if _table_exists(conn, 'lan_session_uploads'):
        from evolvmem.lan_capture import EXCLUDED_REASONS
        excluded = ','.join('?' for _ in sorted(EXCLUDED_REASONS))
        branches.append(
            f"(EXISTS (SELECT 1 FROM lan_session_uploads a WHERE a.archive_id={archive_id_sql} "
            f"AND a.attribution_reason IN ({excluded})) AND ({unsettled} OR {own_active}))")
        params.extend(sorted(EXCLUDED_REASONS))
        if _table_exists(conn, 'lan_session_heads'):
            # The head pointer, not the largest archive id, decides the version.
            branches.append(f"({_head_mismatch_sql(archive_id_sql)} AND ({unsettled} OR {own_active}))")
        head_absent = _head_absent_sql(archive_id_sql)
    else:
        head_absent = "1=1"
    # Legacy (head-less) families: the same newest-family rule the visibility
    # oracle uses. Identity equality in SQL keeps independent incremental batches
    # out of this branch entirely.
    branches.append(
        f"({head_absent} AND {_legacy_sibling_sql(archive_id_sql)} "
        f"AND ({unsettled} OR {own_active}))")
    sql = ("SELECT t.id,t.source_key FROM organization_tasks t WHERE t.source_key LIKE 'archive:%' "
           "AND NOT (t.status='superseded' AND t.stage='retired') "
           "AND " + not_manual + " AND (" + " OR ".join(branches) + ") ORDER BY t.id LIMIT ?")
    rows = conn.execute(sql, (*params, max(1, int(limit)))).fetchall()
    return [(row['id'], row['source_key']) for row in rows]


def _settle_ineligible(service, task_id, source_key):
    """Settle one task whose source is excluded, empty or superseded.

    Returns ``'excluded'``, ``'no_dialogue'``, ``'superseded'`` or ``None`` when
    the task must stay exactly as it is (manual decision, source still current,
    or a genuinely unavailable source that remains an error). A task that is
    already ``superseded`` only has its own automatic outputs settled, so an old
    verdict never keeps an output eligible forever.
    """
    from evolvmem.history_organization import source_excluded
    conn = service.store._connection()
    if task_id is None or not isinstance(source_key, str) or not source_key.startswith('archive:'):
        return None
    row = conn.execute('SELECT id,status,stage,source_key FROM organization_tasks WHERE id=?',
                       (int(task_id),)).fetchone()
    if row is None:
        return None
    if _has_manual_units(conn, task_id):
        # A human decided units of this source: the task, its units and its
        # outputs all stay exactly as they are, whatever the task status is.
        return None
    if row['status'] == 'superseded' and row['stage'] == 'retired':
        return None
    try:
        archive_id = int(source_key.split(':')[1])
    except (ValueError, IndexError):
        return None
    retired = reason_for_retirement(service, source_key)
    if row['status'] == 'superseded':
        if retired is not None:
            _downgrade_superseded_outputs(service, source_key, before_id=int(task_id) + 1)
            with service.store.transaction():
                conn.execute("UPDATE organization_tasks SET stage='retired',updated_at=? WHERE id=?",
                             (_now_iso(), int(task_id)))
        return None
    if retired == 'excluded':
        return 'excluded' if _retire_task(
            service, task_id, status='superseded', reason='source_excluded') else None
    if retired == 'superseded':
        _, successor = _version_state(service, archive_id)
        return 'superseded' if _retire_task(
            service, task_id, status='superseded', reason='source_superseded',
            successor=_successor_task_id(service, successor)) else None
    if _dialogue_state(service, archive_id) == 'empty':
        return 'no_dialogue' if _retire_task(
            service, task_id, status='completed', reason=NO_DIALOGUE_CODE) else None
    return None


def reason_for_retirement(service, source_key):
    """``'excluded'``/``'superseded'`` when this source must leave the queue."""
    from evolvmem.history_organization import source_excluded
    if source_excluded(service, source_key):
        return 'excluded'
    try:
        archive_id = int(str(source_key).split(':')[1])
    except (ValueError, IndexError):
        return None
    current, _ = _version_state(service, archive_id)
    return None if current else 'superseded'


def settle_ineligible_source(service, task_id, source_key):
    """Public, model-free preflight: settle a task whose source is not eligible."""
    try:
        return _settle_ineligible(service, task_id, source_key) is not None
    except Exception:
        # A preflight failure never blocks normal organization work.
        return False


def reconcile_sources(service, *, limit=RECONCILE_LIMIT):
    """Retire automatic tasks whose source is excluded, empty or superseded.

    Safe and model-free: it only re-reads indexed state (and decrypts only a
    verified-empty candidate). It is bounded per tick and its candidate
    predicate is actionable, so repeated ticks keep covering later rows. Raw
    archives and payloads are never deleted, a manual unit decision is never
    overwritten, and a source that merely changed for another reason is left
    alone.
    """
    conn = service.store._connection()
    summary = {'superseded': 0, 'no_dialogue': 0, 'excluded': 0, 'linked': 0,
               'examined': 0, 'model_calls': 0, 'context_invalidated': 0}
    if not _table_exists(conn, 'organization_tasks'):
        return summary
    summary['linked'] += _link_superseded_successors(service)
    try:
        candidates = _reconcile_candidates(service, limit=limit)
    except sqlite3.Error:
        candidates = []
    summary['examined'] = len(candidates)
    for task_id, source_key in candidates:
        try:
            verdict = _settle_ineligible(service, task_id, source_key)
        except Exception:
            # Reconciliation is best effort: one bad source never stops the tick.
            continue
        if verdict in summary:
            summary[verdict] += 1
    # A successor task created in the same discovery pass gets its link now.
    summary['linked'] += _link_superseded_successors(service)
    # Bounded, model-free dependency re-check: an automatic unit whose inherited
    # predecessor changed, was withdrawn or retired goes back to review.
    summary['context_invalidated'] += organization_context.invalidate_dependents(
        service, limit=limit)['invalidated']
    return summary


def link_superseded_successors(service):
    """Public wrapper: fill in successors of already-retired source versions."""
    return _link_superseded_successors(service)


def repair_sources(service, *, limit=RECONCILE_LIMIT):
    """Bounded root-controlled repair of existing automatic state; no model call."""
    if service is None:
        return {'skipped': 'context_mode_required', 'model_calls': 0}
    return reconcile_sources(service, limit=limit)


def task_detail(service, task_id):
    row = service.store._connection().execute(
        'SELECT * FROM organization_tasks WHERE id=?', (task_id,)).fetchone()
    if row is None:
        raise ValueError('task_not_found')
    task = {k: v for k, v in dict(row).items() if k != 'source_snapshot'}
    task['context'] = organization_context.decode(row['context_basis'])
    return {**task, 'source_current': _source_current(service, task),
            'source_text': row['source_snapshot'], 'units': unit_views(service, task_id)}


def unit_views(service, task_id):
    from evolvmem.organization_diagnostics import describe
    feedback = {r['digest']: dict(r) for r in service.store._connection().execute(
        'SELECT * FROM organization_review_feedback WHERE task_id=?', (task_id,))}
    rows = service.store._connection().execute(
        'SELECT * FROM organization_units WHERE task_id=? ORDER BY ordinal', (task_id,)).fetchall()
    return [{**dict(row), 'revision': _unit_revision(row),
             'diagnostic_text': describe(row['extraction_diagnostic']),
             'feedback': feedback.get(row['digest'], {}).get('verdict', '')
                         if feedback.get(row['digest'], {}).get('unit_revision') == _unit_revision(row) else '',
             'context': organization_context.decode(row['context_basis'])} for row in rows]


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
        # Cross-tick rotation of the bounded claim scan. A run of tasks that all
        # wait for a predecessor outside the first window must not stall the
        # queue forever, and the window must never be widened to the whole table.
        self._claim_offset = 0
        # Local Codex capture state (poll throttle, cached instance, config stamp).
        # The capture module itself decides whether it is configured and enabled.
        self._capture_state = {}

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

    def _http_embedding_engine(self):
        """The already-running shared HTTP model, only when one is configured.

        Returns ``None`` when no ``embedding_http_url`` is set, so an install
        without the LAN model never loads a second local model. A configured but
        unreachable service also degrades to ``None``: writes stay archivable and
        the vector cache keeps its dirty retry marker instead of failing.
        """
        if not self.config.embedding_http_url:
            return None
        from evolvmem.embedding import EmbeddingEngine
        engine = EmbeddingEngine(self.config)
        try:
            engine.initialize()
        except Exception:
            try:
                engine.close()
            except Exception:
                pass
            return None
        return engine

    def _connect(self):
        service = self._service
        if service is None:
            from evolvmem.context_models import ContextMode
            from evolvmem.context_service import ContextService
            mode = self._resolved_mode()
            if mode in (ContextMode.LEGACY, ContextMode.COMPAT):
                raise ValueError('organization_needs_context_mode')
            # Reusing the configured shared model lets an accepted write update
            # the search index immediately; without one nothing is passed.
            built = ContextService(self.config, embedding_engine=self._http_embedding_engine())
            built.initialize(mode=mode, adapter='auto-organization')
            # ``tasks``/``units`` may reach this from another thread. Whoever
            # claims the slot first wins and the other build is released at once,
            # so a second HTTP client can never leak.
            if self._service is None:
                self._service = built
            else:
                built.close()
            service = self._service
        return service

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
                    if self.capture_once():
                        worked = True
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

    def capture_once(self):
        """One bounded local Codex capture round; True when it archived a batch.

        Collection is opt-in twice over: the local ``local_codex_capture.json``
        must be present, valid and enabled, and the existing ``auto_new`` arrival
        switch must be on. Both checks live in ``local_codex_capture`` so a paused
        arrival mode stops collection without touching queued work.
        """
        from evolvmem import local_codex_capture
        try:
            return bool(local_codex_capture.capture_once(self._connect(), self._capture_state))
        except Exception:
            # Capture never breaks organization: a failure is visible in the
            # read-only status view instead.
            return False

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
        """Claim the oldest claimable pending task.

        The eligibility preflight runs before the claim, so a verified system
        source, an empty session or a retired version is settled without spending
        an attempt or a model call. A batch whose own same-session predecessor is
        still being organized is *deferred* inside this bounded scan instead of
        being failed, reviewed or retried: it keeps ``pending``, spends no
        attempt, and the predecessor is claimed in the same pass.

        The scan is one bounded page, and a page that only waits rotates to the
        next page on the next tick (see ``_claim_page``). A run of waiting tasks
        longer than the page therefore still reaches the real predecessor that
        sorts after it, without widening the page to the whole table and without
        ever marking a waiting task finished.
        """
        conn = service.store._connection()
        for _ in range(CLAIM_PREFLIGHT_LIMIT):
            rows = self._claim_page(conn)
            if not rows:
                return None
            settled = False
            for row in rows:
                if settle_ineligible_source(service, row['id'], row['source_key']):
                    settled = True
                    break
                if organization_context.awaiting_predecessor(service, row['source_key']):
                    continue
                with service.store.transaction():
                    cursor = conn.execute(
                        "UPDATE organization_tasks SET status='running',attempts=attempts+1,"
                        "started_at=?,updated_at=? WHERE id=? AND status='pending'",
                        (_now_iso(), _now_iso(), row['id']))
                    if cursor.rowcount != 1:
                        continue
                # A claim always restarts from the oldest pending task, so the
                # normal fifo order is never disturbed by the rotation.
                self._claim_offset = 0
                return task_view(service, row['id'])
            if not settled:
                # Every pending task in this window is waiting for its own
                # predecessor (or was taken by another worker). Rotate the window
                # for the next tick instead of reporting an empty queue, so a
                # predecessor that sorts after the window is still reachable.
                self._claim_offset += len(rows)
                if len(rows) < CLAIM_SCAN_LIMIT:
                    self._claim_offset = 0
                return None
        return None

    def _claim_page(self, conn):
        """One bounded page of pending tasks, rotating across ticks."""
        sql = ("SELECT id,source_key FROM organization_tasks WHERE status='pending' "
               'ORDER BY id LIMIT ? OFFSET ?')
        rows = conn.execute(sql, (CLAIM_SCAN_LIMIT, self._claim_offset)).fetchall()
        if not rows and self._claim_offset:
            self._claim_offset = 0
            rows = conn.execute(sql, (CLAIM_SCAN_LIMIT, 0)).fetchall()
        return rows

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
            elif settle_ineligible_source(service, task_id, task['source_key']):
                # A race that changed eligibility after the claim: settle it
                # truthfully instead of calling the model.
                return
            elif task['stage'] in ('queued', 'cleaning', 'segmentation'):
                self._segment(service, task)
            elif task['stage'] == 'assignment':
                self._assign(service, task)
            elif task['stage'] == 'extraction':
                self._extract_units(service, task)
            else:
                self._finish(service, task_id, 'completed', 'done')
        except SourceNoDialogue:
            # No dialogue at all is a truthful skip, never a provider failure.
            self._finish(service, task_id, 'completed', 'done', NO_DIALOGUE_CODE)
        except topic_segmentation.SegmentationError as error:
            self._fail(service, task_id, 'review', 'coverage_' + error.code if error.code in
                       ('coverage_gap', 'coverage_overlap', 'quote_not_found') else error.code,
                       error.detail)
        except ValueError as error:
            code = str(error)
            if code in ('revision_conflict', 'cleaning_source_deleted', 'cleaning_source_unavailable',
                        'classification_no_longer_unassigned',
                        'classification_source_changed', 'invalid_items', 'extraction_provider_unavailable',
                        'organization_needs_context_mode'):
                self._fail(service, task_id, 'review' if code == 'revision_conflict' else 'failed', code, '')
            else:
                from evolvmem.organization_diagnostics import capture, describe
                self._fail(service, task_id, 'failed', 'organization_failed', describe(capture(error, task['stage'])))
        except Exception as error:  # provider or storage failure; task stays visible
            from evolvmem.organization_diagnostics import capture, describe
            self._fail(service, task_id, 'failed', 'organization_failed', describe(capture(error, task['stage'])))

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
        # The same-session predecessor is frozen before the model call and
        # re-validated afterwards, so a basis that breaks mid-call can never
        # produce a stored inherited assignment.
        basis = organization_context.build_basis(service, task)
        stored_basis = organization_context.encode(basis)
        with service.store.transaction():
            service.store._connection().execute(
                'UPDATE organization_tasks SET context_basis=?,updated_at=? WHERE id=?',
                (stored_basis, _now_iso(), task['id']))
        context_prompt = organization_context.basis_prompt(
            basis, organization_context.tail_text(service, basis['source_key']) if basis else '')

        def provider(request):
            # The same cancellation check guards every provider call, including
            # the one coverage correction: a stop between them is not ignored.
            if self._stop.is_set():
                raise ValueError('worker_stopped')
            return call(request, config, deadline=time.monotonic() + 90)

        units = topic_segmentation.segment(
            text, provider,
            records=records, cleaning_instructions=policy['settings']['cleaning_instructions'],
            projects=projects, context=context_prompt)
        if self._stop.is_set():
            raise ValueError('worker_stopped')
        # After the model calls, the source and rules must still be the ones the
        # snapshot was taken from, otherwise the result would be stale.
        _assert_current(service, task)
        if not self._is_current(service, task):
            self._finish(service, task['id'], 'superseded', 'stale', 'stale_task')
            return
        basis, basis_changed = organization_context.refresh(service, basis)
        if basis_changed:
            # The predecessor changed while the model was answering: nothing may
            # be written from the old context. The units are kept for review.
            for unit in units:
                unit['continues_context'] = False
            with service.store.transaction():
                service.store._connection().execute(
                    "UPDATE organization_tasks SET context_basis='',updated_at=? WHERE id=?",
                    (_now_iso(), task['id']))
        # Only a program-verified same-project continuation is joined; the whole
        # source is re-checked for exact coverage before anything is stored.
        units = coalesce_units(service, task, units, text, policy)
        topic_segmentation.coverage(text, units)
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
            # Re-segmentation replaces every unit of this task, so a dependency
            # recorded against an old digest must not survive as a live link.
            organization_context.clear_task_dependencies(service, task['id'], conn=conn)
            for ordinal, unit in enumerate(units):
                digest = unit_digest(task['source_key'], unit['source_start'], unit['source_end'], unit['text'])
                role = next((span['role'] for span in spans
                             if span['start'] <= unit['source_start'] < span['end']), '')
                disposition = unit.get('disposition') or 'keep'
                if disposition not in topic_segmentation.DISPOSITIONS:
                    disposition = 'review'
                if disposition == 'history_only':
                    # The provider suggests purpose; original structural roles
                    # and project conflicts are checked by the program.
                    real = topic_segmentation.unit_messages(unit, spans)
                    signals = service.knowledge().rules.evaluate(
                        {'body': unit['text'], 'source': True}, service.knowledge().registry())
                    if not real or any(m['role'] != 'assistant' for m in real) or \
                            unit['category'] != 'reference' or len(signals['candidates']) > 1:
                        disposition = 'review'
                        unit['disposition_reason'] = '不能按纯助手进度处理：原文含其他角色、重要类别或项目冲突，保留待核对'
                conn.execute(
                    'INSERT INTO organization_units(task_id,ordinal,digest,title,text,cleaned_text,source_start,'
                    "source_end,category,role,disposition,disposition_reason,project_hint,project,decision,reason,"
                    "evidence_quote,continues_context,context_basis,extraction_stage,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'','review','',?,?,'','pending',?,?)",
                    (task['id'], ordinal, digest, unit['title'], unit['text'],
                     unit.get('cleaned_text') or unit['text'], unit['source_start'], unit['source_end'],
                     unit['category'], role, disposition, unit.get('disposition_reason', ''),
                     unit['project_hint'], unit.get('evidence_quote', ''),
                     1 if unit.get('continues_context') is True else 0, _now_iso(), _now_iso()))
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
        basis = self._valid_basis(service, task)
        auto_projects, review = [], 0
        for unit in units:
            if unit['decision'] == 'manual':
                # A human decision is never re-decided by a later automatic pass.
                continue
            if unit['disposition'] == 'set_aside':
                continue
            # Only a successfully located quote is replaced by the real source
            # substring. An unlocatable quote stays exactly as the model wrote
            # it (``None`` tells ``_update_unit`` to keep the stored value), so
            # a later automatic pass can never pass for want of evidence.
            resolved_quote = topic_segmentation.resolve_evidence_quote(
                unit['text'], unit.get('evidence_quote'))
            if unit['disposition'] == 'review':
                self._update_unit(service, task['id'], unit['digest'], project=unit['project'] or '',
                                  decision='review', expected_revision=unit, context_basis='',
                                  reason=unit['disposition_reason'], evidence_quote=resolved_quote)
                review += 1
                continue
            guidance = guidance_store.classify(service, unit)
            if unit['disposition'] == 'history_only' and guidance['status'] == 'review':
                with service.store.transaction():
                    service.store._connection().execute(
                        "UPDATE organization_units SET disposition='review',disposition_reason=?,revision=revision+1 "
                        "WHERE task_id=? AND digest=?", (guidance['reason'], task['id'], unit['digest']))
                unit = find_unit(service, task['id'], unit['digest'])
            if guidance['status'] == 'review':
                self._update_unit(service, task['id'], unit['digest'], project='', decision='review',
                                  expected_revision=unit, context_basis='',
                                  reason=guidance['reason'], evidence_quote=resolved_quote)
                review += 1
                continue
            if guidance['status'] == 'apply':
                conflict = self._guidance_conflict(service, unit, guidance['project'], policy)
                if conflict:
                    self._update_unit(service, task['id'], unit['digest'], project='', decision='review',
                                      expected_revision=unit, context_basis='',
                                      reason=conflict, evidence_quote=resolved_quote)
                    review += 1
                    continue
                self._update_unit(service, task['id'], unit['digest'], project=guidance['project'],
                                  decision='auto', expected_revision=unit, context_basis='',
                                  guidance_id=guidance['id'],
                                  reason=guidance['reason'], evidence_quote=resolved_quote)
                auto_projects.append(guidance['project'])
                continue
            if guidance['status'] == 'pending':
                self._update_unit(service, task['id'], unit['digest'], project='', decision='review',
                                  expected_revision=unit, context_basis='',
                                  reason=guidance['reason'], evidence_quote=resolved_quote)
                review += 1
                continue
            hint = unit['project_hint'] if unit['project_hint'] in names else ''
            used = {}
            project, reason, decision = self._decide(service, unit, hint, policy, names, basis, used)
            inherited = decision == 'auto' and used.get('context') is True
            self._update_unit(service, task['id'], unit['digest'], project=project, decision=decision,
                              expected_revision=unit, reason=reason, evidence_quote=resolved_quote,
                              context_basis=organization_context.unit_context(basis) if inherited else '')
            if inherited:
                self._record_dependency(service, task['id'], unit['digest'], basis)
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

    def _valid_basis(self, service, task):
        """The task's stored same-session basis, re-validated before use.

        A basis that a human change, a withdrawal or a retired source has since
        broken is dropped here, so this pass can never inherit a dead context.
        """
        stored = organization_context.decode(_task_row(service, task['id']).get('context_basis'))
        basis, _changed = organization_context.refresh(service, stored)
        if stored and not basis:
            with service.store.transaction():
                service.store._connection().execute(
                    "UPDATE organization_tasks SET context_basis='',updated_at=? WHERE id=?",
                    (_now_iso(), task['id']))
        return basis

    def _record_dependency(self, service, task_id, digest, basis):
        with service.store.transaction():
            organization_context.record_dependency(service, task_id, digest, basis)

    def _guidance_conflict(self, service, unit, project, policy):
        """Guidance never overrides conflicting project evidence in the unit."""
        kb = service.knowledge()
        evaluated = kb.rules.evaluate({'body': unit['text'], 'source': True}, kb.registry(), policy=policy)
        others = [candidate for candidate in evaluated['candidates'] if candidate.casefold() != project.casefold()]
        if others:
            return '你的指导与这段正文里的其他项目线索冲突（' + '、'.join(others) + '），需要人工确认'
        return ''

    def _decide(self, service, unit, hint, policy, names, basis=None, used=None):
        """Program validation decides; the model hint alone is never the gate.

        A validated same-session basis can carry a unit that names no project at
        all, but only when the model explicitly judged this batch a continuation
        and the current batch offers no decisive competing project evidence. A
        name that only appears as a delegation to a tool -- by its registered id,
        display name or alias ("交给 DSH 执行", "使用 Kimi 运行") -- is not a
        project switch; a real switch ("修改 DSH 项目") always wins.

        When the model names exactly the project the validated predecessor
        established, the program accepts it even though this batch's own text
        does not repeat the name: that is precisely the case a model reaches by
        reading the supplied background.

        ``used``, when a dict is passed, records whether the returned project
        actually rests on that basis, so a dependency is never recorded for a
        unit whose own text already names the project.
        """
        if used is not None:
            used['context'] = False
        if not names:
            return '', '没有已登记项目，先登记项目再整理', 'review'
        raw_quote = str(unit.get('evidence_quote') or '')
        # Only the kind/count of horizontal whitespace may differ from the real
        # text; the located value is always the actual source substring.
        evidence_quote = topic_segmentation.resolve_evidence_quote(unit.get('text', ''), raw_quote) or ''
        if raw_quote and not evidence_quote:
            return '', '模型引用的证据在原文中定位不到（只允许空白差异），需要人工核对', 'review'
        _evaluated, decisive, tool_only = organization_context.project_candidates(
            service, unit['text'], policy)
        inherited = (unit.get('continues_context') in (1, True) and isinstance(basis, dict)
                     and basis.get('state') == organization_context.STATE_READY
                     and basis.get('project') in names)
        tool_names = {name.casefold() for name in tool_only}
        decisive_names = {name.casefold() for name in decisive}

        def carried(project, reason):
            if used is not None:
                used['context'] = True
            return project, reason, 'auto'

        def by_text(reason):
            """The batch's own decisive project evidence decides."""
            if len(decisive) == 1:
                return decisive[0], reason, 'auto'
            if len(decisive) > 1:
                return '', '正文点名多个项目，需要人工拆分', 'review'
            return None

        if not hint:
            decided = by_text('正文只点名一个已登记项目，按规则自动归属')
            if decided:
                return decided
            if inherited:
                return carried(basis['project'], organization_context.reason_for(basis))
            if tool_only:
                return '', ('正文只把' + '、'.join(tool_only) +
                            '当作工具提及，缺少业务项目依据，等待人工确认'), 'review'
            return '', '正文没有明确项目线索，等待人工选择', 'review'
        hinted = hint.casefold()
        if hinted in tool_names:
            # The hint only rests on a tool gesture ("交给 DSH 执行"). It is
            # never a project switch, so any real project this batch names wins;
            # otherwise the validated same-session project continues.
            decided = by_text('正文点名一个已登记项目，工具提及不构成项目切换')
            if decided:
                return decided
            if inherited:
                return carried(basis['project'], organization_context.reason_for(basis))
            return '', '模型建议只来自工具提及，缺少项目依据，等待人工确认', 'review'
        if hinted in decisive_names:
            competitors = [name for name in decisive if name.casefold() != hinted]
            if len(competitors) > 1:
                return '', '项目线索冲突，不能覆盖其他项目归属', 'review'
            if not competitors:
                return hint, '正文与已登记项目一致，按规则自动归属', 'auto'
            if evidence_quote and evidence_quote in unit['text'] and competitors[0] in evidence_quote:
                return competitors[0], '引用原话指向另一个已登记项目，按证据归属', 'auto'
            return '', '这段同时提到' + '、'.join(competitors) + '与' + hint + '，需要人工拆分', 'review'
        if inherited and hinted == str(basis['project']).casefold() and not decisive:
            # The model named exactly the validated predecessor's project and the
            # batch itself offers no competing decisive evidence: this is the
            # continuation the background was supplied for.
            return carried(basis['project'], organization_context.reason_for(basis))
        decided = by_text('正文点名一个已登记项目，按规则自动归属')
        if decided:
            return decided
        return '', '模型建议缺少正文项目证据，等待人工确认', 'review'

    def _update_unit(self, service, task_id, digest, *, project, decision, expected_revision=None,
                     reason='', evidence_quote=None, context_basis=None, guidance_id=0):
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
                'context_basis=?,applied_guidance_id=?,revision=revision+1,updated_at=? WHERE task_id=? AND digest=?',
                (project, decision, str(reason or '')[:600],
                 str(row['evidence_quote'] if evidence_quote is None else evidence_quote)[:300],
                 str(row['context_basis'] if context_basis is None else context_basis),
                 guidance_id, _now_iso(), task_id, digest))
            if context_basis == '':
                # The unit no longer rests on a predecessor: drop the dependency
                # so the reconciliation pass cannot report it as live.
                organization_context.clear_dependency(service, task_id, digest, conn=conn)
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
            "extraction_stage=CASE WHEN ? THEN 'pending' ELSE extraction_stage END,"
            "context_basis=CASE WHEN ? THEN '' ELSE context_basis END,updated_at=? "
            'WHERE task_id=? AND digest=?',
            (project, decision, reason, 1 if changed_project else 0,
             1 if changed_project else 0, _now_iso(), task_id, digest))
        if changed_project:
            # The human decision now owns this unit's project; the inherited
            # basis no longer explains it and must not stay a live dependency.
            organization_context.clear_dependency(service, task_id, digest, conn=conn)
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
    # A correction to this unit is exactly the event that must send units
    # inheriting from it back to review; the scan is bounded and model-free.
    organization_context.invalidate_dependents(service, limit=RECONCILE_LIMIT)
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
        # The unresolved output is a candidate for real, so the legacy projection
        # moves in the same transaction; the project stays empty and the item is
        # never re-activated here.
        _mirror_legacy_candidate(service, [item_id])
    # The item stays a candidate; empty project means unresolved, never global.
    kb._sync([item_id])


def _refresh_task_state(service, task_id):
    units = [dict(r) for r in service.store._connection().execute(
        'SELECT * FROM organization_units WHERE task_id=? ORDER BY ordinal', (task_id,))]
    pending = [unit for unit in units if unit['disposition'] not in ('set_aside', 'history_only')]
    review = sum(1 for unit in pending if unit['decision'] == 'review' or not unit['project'])
    assigned = [u for u in pending if u['project'] and u['decision'] != 'review']
    failed = any(u['extraction_stage'] == 'failed' for u in assigned)
    from evolvmem.organization_diagnostics import describe
    failure_detail = next((describe(u.get('extraction_diagnostic', '')) for u in assigned
                           if u['extraction_stage'] == 'failed' and u.get('extraction_diagnostic')), '')
    unfinished = any(u['extraction_stage'] in ('pending', 'running') for u in assigned)
    status, stage, error = ('failed', 'extraction', 'extraction_failed') if failed else (
        ('pending', 'extraction', '') if unfinished else
        ('review', 'done', 'units_need_review') if review else ('completed', 'done', ''))
    with service.store.transaction():
        conn = service.store._connection()
        if units:
            conn.execute('UPDATE organization_tasks SET review_count=?,status=?,stage=?,error_code=?,error_detail=?,'
                         'finished_at=?,updated_at=? WHERE id=? AND status!=?',
                         (review, status, stage, error, failure_detail, None if unfinished else _now_iso(),
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
    # Setting a unit aside withdraws it as a predecessor: anything that inherited
    # from it returns to review in the same bounded, model-free pass.
    organization_context.invalidate_dependents(service, limit=RECONCILE_LIMIT)
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
    if route == '/metrics':
        from evolvmem.organization_metrics import metrics
        return metrics(service)
    if method == 'POST' and route == '/feedback':
        from evolvmem.organization_metrics import feedback
        return feedback(service, body)
    if route == '/progress':
        from evolvmem.organization_progress import list_progress
        return list_progress(service, body)
    if route.startswith('/groups'):
        from evolvmem import organization_review
        if route == '/groups':
            return organization_review.groups(service, body)
        if method == 'POST' and route == '/groups/preview':
            return organization_review.preview(service, body)
        if method == 'POST' and route == '/groups/apply':
            return organization_review.apply(service, body)
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
    if method == 'POST' and route == '/repair':
        # Root-controlled bounded repair of existing automatic state; no model call.
        return repair_sources(service, limit=min(RECONCILE_LIMIT, max(1, int(body.get('limit', RECONCILE_LIMIT) or RECONCILE_LIMIT))))
    raise LookupError('route_not_found')


def _keys(body):
    items = body.get('items')
    if (not isinstance(items, list) or not 1 <= len(items) <= 100
            or any(not isinstance(e, dict) or not isinstance(e.get('key'), str) for e in items)
            or len({e['key'] for e in items}) != len(items)):
        raise ValueError('invalid_items')
    return [e['key'] for e in items]

"""Bounded same-session prior context for independent incremental batches.

One Linux Codex rollout is archived as a series of *independent* incremental
batches, so a later batch that keeps discussing the same project often no longer
repeats the project name. This module carries exactly one kind of evidence for
that case: the **nearest preceding verified batch of the same session** and the
project a human or the program already established for its tail.

The evidence is deliberately narrow and program-checked:

* only a strict ``local_codex_jsonl`` incremental batch (the exact identity
  shape ``local_codex_capture.batch_external_id`` writes) can ever be a source;
  the payload must be readable, must declare the same session id the identity
  hashes, must carry the same line range and must not be a system sub-session;
* candidates are filtered *by session first* in SQL on the identity alone, then
  the nearest preceding one is chosen, so a whole library of unrelated archives
  can never push the right predecessor out and no other body is ever decrypted;
* the *actual* tail unit of the predecessor decides: an unresolved or
  unattributed tail is a barrier, so an older resolved unit can never be
  smuggled past it;
* only a project already established on that tail (a human attribution, or a
  program-verified explicit project) is inherited; a model hint alone is never
  the gate;
* an inherited dependency is recorded per unit with a semantic fingerprint of
  the predecessor (text, cleaning, evidence, decision, unit revision and the
  predecessor's own source revision) and re-validated across model calls, so a
  manual change, withdrawal, re-assignment, content change or retired source
  sends the dependent automatic units back to review without touching human
  decisions.

Nothing here merges archives, rewrites bytes or reuses the prior text as this
batch's own evidence: the prior material only ever reaches the segmentation
prompt as background, never the extraction prompt.
"""
from __future__ import annotations

import hashlib
import json
import re

from evolvmem.context_store import _now_iso
from evolvmem.session_identity import is_incremental_batch

KIND = 'incremental_previous'
STATE_READY = 'ready'
STATE_PENDING_PREVIOUS = 'pending_previous'

# Hard bounds: at most this many same-session archives are ever looked at, and
# the prompt only ever receives a bounded tail of the one predecessor.
SESSION_WINDOW = 12
TAIL_CHARS = 600
QUOTE_CHARS = 160
# Units examined per bounded, model-free reconciliation tick, and how many
# cascade rounds one bounded call may take (a two-level chain settles in two).
RECONCILE_LIMIT = 10
CASCADE_ROUNDS = 3

# Phrasing that only delegates work to a name; it mentions a tool, it does not
# choose a project. ``交给 DSH 执行`` must never be read as ``DSH 项目``.
_DELEGATION_BEFORE = ('交给', '交由', '让', '使用', '用', '由', '委托', '叫', '请', '找')
_DELEGATION_AFTER = ('执行', '来执行', '来做', '处理', '运行', '跑', '协助', '完成', '接手', '操作', '干活')
# Phrasing that really names a project: a genuine switch or a project reference.
_SWITCH_BEFORE = ('修改', '切换到', '转成', '转到', '回到', '更新', '开发', '修复', '调整',
                  '重构', '继续', '针对', '关于', '打开', '维护')
_PROJECT_AFTER = ('项目', '工程', '仓库', '代码库', '代码', '文档', '需求', '任务', '计划',
                  '版本', '发布', '部署', '目录', '模块', '那边', '这边', '的')
_WS = r'[ \t\u3000\u00a0]*'


# ------------------------------------------------------------ mention analysis

def _occurrences(text, name):
    """Every case-insensitive occurrence of one registered name in the text."""
    folded, needle = text.casefold(), str(name).casefold()
    if not needle:
        return []
    pattern = re.escape(needle)
    if needle.isascii():
        pattern = r'(?<![0-9a-z_-])' + pattern + r'(?![0-9a-z_-])'
    return list(re.finditer(pattern, folded))


def _preceded_by(text, start, phrase):
    """Whether ``phrase`` sits immediately before ``start`` (whitespace allowed)."""
    window = text[max(0, start - len(phrase) - 4):start]
    return re.search(re.escape(phrase.casefold()) + _WS + r'$', window) is not None


def _followed_by(text, end, phrase):
    window = text[end:end + len(phrase) + 4]
    return re.match(_WS + re.escape(phrase.casefold()), window) is not None


def mention_kind(text, name):
    """``'tool_only'`` when every occurrence only delegates work to this name.

    ``交给 DSH 执行`` / ``使用 Codex 跑一下`` mention a tool. ``修改 DSH 项目`` /
    ``DSH 的仓库`` really name the project. One non-delegating occurrence is
    enough to make the mention decisive, so a real switch always wins.
    """
    folded = str(text or '').casefold()
    occurrences = _occurrences(folded, name)
    if not occurrences:
        return 'absent'
    for match in occurrences:
        explicit = (any(_followed_by(folded, match.end(), suffix) for suffix in _PROJECT_AFTER)
                    or any(_preceded_by(folded, match.start(), prefix) for prefix in _SWITCH_BEFORE))
        if explicit:
            return 'decisive'
        delegated = (any(_preceded_by(folded, match.start(), prefix) for prefix in _DELEGATION_BEFORE)
                     or any(_followed_by(folded, match.end(), suffix) for suffix in _DELEGATION_AFTER))
        if not delegated:
            # A bare mention is ordinary business text, never a tool gesture.
            return 'decisive'
    return 'tool_only'


def surface_names(registry, project):
    """Every registered way one project can be written: id, display name, aliases.

    A rule candidate is a *project id*, but the text usually writes the display
    name or an alias ("Kimi执行器" / "Kimi"). Judging the tool gesture on the
    canonical id alone would call an alias-only mention ``absent`` and promote a
    tool to a project, so every registered surface form is checked.
    """
    names = [str(project)]
    for row in registry or ():
        if not isinstance(row, dict) or str(row.get('project')) != str(project):
            continue
        for value in (row.get('display_name'), *(row.get('aliases') or ())):
            if isinstance(value, str) and value.strip():
                names.append(value.strip())
    seen, unique = set(), []
    for name in names:
        if name.casefold() not in seen:
            seen.add(name.casefold())
            unique.append(name)
    return unique


def mention_kind_project(text, registry, project):
    """``'tool_only'`` only when *every* surface form is a pure delegation.

    One surface form used as real project evidence makes the mention decisive.
    """
    forms = surface_names(registry, project)
    kinds = [mention_kind(text, name) for name in forms]
    if 'decisive' in kinds:
        return 'decisive'
    if 'tool_only' in kinds:
        return 'tool_only'
    return 'absent'


def project_candidates(service, text, policy=None):
    """One canonical split of the rule candidates of one text.

    Every entry point that has to know "is this a business project or a tool
    gesture" calls this, so ``_decide`` and the predecessor check can never
    drift apart. Returns ``(evaluated, decisive, tool_only)``.
    """
    kb = service.knowledge()
    registry = kb.registry()
    policy = policy if policy is not None else kb.rules.read()
    evaluated = kb.rules.evaluate({'body': text, 'source': True}, registry, policy=policy)
    decisive, tool_only = decisive_candidates(text, evaluated['candidates'], registry)
    return evaluated, decisive, tool_only


def decisive_candidates(text, candidates, registry=None):
    """Split rule candidates into real project evidence and tool-only mentions."""
    decisive, tool_only = [], []
    for name in candidates or ():
        kind = (mention_kind_project(text, registry, name) if registry is not None
                else mention_kind(text, name))
        (tool_only if kind == 'tool_only' else decisive).append(name)
    return decisive, tool_only


# --------------------------------------------------------------- basis records

def _source_key(archive_id):
    return 'archive:%d' % int(archive_id)


def _archive_id_of(source_key):
    match = re.fullmatch(r'archive:([1-9][0-9]*)', str(source_key or ''))
    return int(match[1]) if match else None


def _int_or_none(value):
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _structural_info(service, source_key):
    """Identity-only facts for one incremental batch; never reads a payload.

    Used on hot paths (claim preflight, neighbour selection) where only the
    session head and the line range matter. Anything that is not exactly the
    recognised shape returns ``None``.
    """
    archive_id = _archive_id_of(source_key)
    if archive_id is None:
        return None
    row = service.store.get_session_archive(archive_id)
    if row is None or row.get('state') != 'available' or row.get('adapter') != 'codex':
        return None
    external = str(row.get('external_session_id') or '')
    if not is_incremental_batch(external):
        return None
    head, span, digest = external.split(':')
    try:
        start_line, end_line = (int(part) for part in span.split('-'))
    except ValueError:
        return None
    return {'archive_id': archive_id, 'source_key': source_key, 'head': head,
            'digest': digest, 'start_line': start_line, 'end_line': end_line,
            'payload_sha256': str(row.get('payload_sha256') or '')}


# The identity span in SQL: ``<head>:<start>-<end>:<digest>`` has a fixed,
# 32-character head, so the span starts at position 34 and ends at the colon
# before the digest. These expressions let the neighbour queries filter by line
# range *inside SQLite*, before any LIMIT, so a window of later batches can never
# crowd the real predecessor out.
def _span_sql(column):
    return "substr(%s,34,instr(substr(%s,34),':')-1)" % (column, column)


def _start_line_sql(column):
    span = _span_sql(column)
    return "CAST(substr(%s,1,instr(%s,'-')-1) AS INTEGER)" % (span, span)


def _end_line_sql(column):
    span = _span_sql(column)
    return "CAST(substr(%s,instr(%s,'-')+1) AS INTEGER)" % (span, span)


def _shape_sql(column):
    """The exact incremental-batch shape, mirrored in SQL for filtering only."""
    return ("(instr(%s,':')=33 AND substr(%s,-17,1)=':' "
            "AND (length(%s)-length(replace(%s,':','')))=2 "
            "AND instr(substr(%s,34),'-')>0)" % (column, column, column, column, column))


# Verified batch facts, bounded: repeated checks inside one operation (one
# assignment, one unit's extraction) must not decrypt the same payload again.
# The key carries the row's own payload digest, so a rewritten archive is never
# served from a stale entry, and the payload itself is never retained.
_INFO_CACHE = {}
_INFO_CACHE_LIMIT = 16


def _cache_get(key):
    return _INFO_CACHE.get(key, _MISS)


_MISS = object()


def _cache_put(key, value):
    if len(_INFO_CACHE) >= _INFO_CACHE_LIMIT:
        _INFO_CACHE.clear()
    _INFO_CACHE[key] = value


def _declares_subagent(payload):
    """Whether the batch's own leading metadata marks a system sub-session."""
    transcript = payload.get('transcript') if isinstance(payload, dict) else None
    if not isinstance(transcript, str) or not transcript:
        return False
    from evolvmem.codex_transcript import workspace_details
    for raw in transcript.split('\n', 6)[:6]:
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get('type') == 'session_meta' \
                and workspace_details([row])['subagent']:
            return True
    return False


def archive_info(service, source_key):
    """Verified incremental-batch facts for one source key, or ``None``.

    Every check is structural: the identity must have the exact incremental
    shape, the decrypted payload must be a ``local_codex_jsonl`` batch whose
    session id hashes to the identity head and whose line range matches the
    identity span, and the batch must not be a system sub-session. The stored
    raw transcript must still hash to the digest the batch itself declares *and*
    to the digest embedded in the archive identity, so observably damaged bytes
    can never be used as background. The archive identity, its bytes and the
    stored range are only checked here, never rewritten.
    """
    info = _structural_info(service, source_key)
    if info is None:
        return None
    from evolvmem.history_organization import source_excluded
    if source_excluded(service, source_key):
        return None
    archive = service.store.get_session_archive(info['archive_id'])
    cache_key = (archive.get('payload_sha256'), archive.get('state'), archive.get('external_session_id'))
    cached = _cache_get(cache_key)
    if cached is not _MISS:
        return dict(cached) if cached else None
    from evolvmem.session_archive import SessionArchiver
    raw = SessionArchiver(service.config, service.store).read_payload(info['archive_id'])
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    source = payload.get('source')
    if not isinstance(source, dict) or source.get('kind') != 'local_codex_jsonl':
        return None
    if source.get('adapter') != 'codex':
        return None
    session_id = source.get('session_id')
    if not isinstance(session_id, str) or not session_id:
        return None
    if hashlib.sha256(session_id.encode()).hexdigest()[:32] != info['head']:
        return None
    if _int_or_none(source.get('start_line')) != info['start_line'] \
            or _int_or_none(source.get('end_line')) != info['end_line']:
        return None
    transcript = payload.get('transcript')
    declared = payload.get('source_sha256')
    if not isinstance(transcript, str) or not isinstance(declared, str) or not declared:
        return None
    measured = hashlib.sha256(transcript.encode('utf-8')).hexdigest()
    if measured != declared or measured[:16] != info['digest']:
        # The bytes no longer match the digest the archive declares about
        # itself, or the digest its own identity is built from.
        return None
    if _declares_subagent(payload):
        return None
    verified = {**info, 'session_id': session_id}
    _cache_put(cache_key, verified)
    return dict(verified)


def tail_text(service, source_key):
    """Bounded tail of one source's own snapshot; empty when unavailable."""
    from evolvmem.auto_organization import source_snapshot
    try:
        _, text = source_snapshot(service, source_key)
    except (ValueError, KeyError):
        return ''
    return str(text or '')[-TAIL_CHARS:]


def _sibling_sql(column='external_session_id'):
    """Same-session, same-adapter, still-readable siblings, shape-checked."""
    return ("SELECT id,external_session_id FROM session_archives "
            "WHERE adapter='codex' AND state='available' AND id<>? "
            "AND external_session_id>=? AND external_session_id<? "
            "AND " + _shape_sql(column))


def _sibling_params(info):
    # A half-open range on the identity prefix is the exact "same session head"
    # filter and can use the (adapter, external_session_id) index, so a library
    # of unrelated archives never has to be read or decrypted.
    return (info['archive_id'], info['head'] + ':', info['head'] + ';')


def preceding_batch(service, info):
    """The nearest verified same-session batch strictly before ``info``.

    Selection happens *in SQL*: same adapter, readable, same session head, the
    exact incremental shape, and an end line strictly before this batch — and
    only then ``ORDER BY end line DESC LIMIT``. A window of later batches (or a
    long run of them) can therefore never hide the real predecessor, and no
    other session's payload is ever opened.

    A sibling that shares lines with this batch makes the ranges ambiguous, so a
    single bounded existence query refuses the whole predecessor lookup.
    """
    if not info:
        return None
    conn = service.store._connection()
    end_sql, start_sql = _end_line_sql('external_session_id'), _start_line_sql('external_session_id')
    try:
        overlap = conn.execute(
            _sibling_sql() + ' AND ' + end_sql + '>=? AND ' + start_sql + '<=? LIMIT 1',
            (*_sibling_params(info), info['start_line'], info['end_line'])).fetchone()
    except Exception:
        return None
    if overlap is not None:
        return None
    try:
        rows = conn.execute(
            _sibling_sql() + ' AND ' + end_sql + '<? ORDER BY ' + end_sql + ' DESC LIMIT ?',
            (*_sibling_params(info), info['start_line'], SESSION_WINDOW)).fetchall()
    except Exception:
        return None
    for row in rows:
        candidate = _structural_info(service, _source_key(row['id']))
        if candidate is None or candidate['end_line'] >= info['start_line']:
            continue
        verified = archive_info(service, candidate['source_key'])
        if verified is not None:
            return verified
    return None


def current_source(service, source_key):
    """Live revision facts of a source: ``(expected_revision, review_revision)``.

    The cleaning page can edit a source's stored text without touching the
    revision cached on its task row, so a cached value is never treated as
    proof. ``(None, 0)`` means the source cannot be read at all. Reading the
    revision never decrypts a captured batch: the cleaned dialogue is already
    stored, and only a genuinely empty candidate is ever re-readable.
    """
    from evolvmem.knowledge_cleaning import record, review
    try:
        revision = str(record(service, source_key)['expected_revision'])
    except Exception:
        return None, 0
    saved = review(service, source_key)
    return revision, int(saved['revision'] or 0) if saved else 0


def _prior_task(service, source_key):
    row = service.store._connection().execute(
        'SELECT * FROM organization_tasks WHERE source_key=? ORDER BY id DESC LIMIT 1',
        (source_key,)).fetchone()
    return dict(row) if row else None


def _prior_tail_unit(service, task_id):
    """The predecessor's *actual* last unit, whatever it decided.

    The tail decides, so a resolved unit earlier in the batch can never be
    borrowed past an unresolved or unattributed tail: a batch that ends on an
    ambiguous new topic is a barrier, not a free pass for the project it
    discussed before that.
    """
    row = service.store._connection().execute(
        'SELECT * FROM organization_units WHERE task_id=? ORDER BY ordinal DESC LIMIT 1',
        (task_id,)).fetchone()
    return dict(row) if row else None


def _locatable(text, unit):
    from evolvmem.topic_segmentation import resolve_evidence_quote
    quote = resolve_evidence_quote(unit.get('text', ''), unit.get('evidence_quote'))
    if quote:
        return quote[:QUOTE_CHARS]
    return str(unit.get('text') or '').strip()[:QUOTE_CHARS]


def _prior_project_verified(service, unit):
    """Program re-check that this unit's project is explicit in its own text.

    A manual decision is authoritative on its own. An automatic one must either
    name exactly that project in the unit text, or itself rest on a validated
    same-session basis; a bare hint is never accepted.
    """
    if unit.get('decision') == 'manual':
        return True
    inherited = decode(unit.get('context_basis'))
    if inherited:
        # A predecessor that itself inherited must still be backed by a live
        # basis; one nested level is enough to keep the chain honest.
        return validate(service, inherited, depth=0)
    _evaluated, decisive, _tool_only = project_candidates(service, unit.get('text', ''))
    return [name.casefold() for name in decisive] == [str(unit['project']).casefold()]


def build_basis(service, task):
    """The predecessor evidence for one task, or ``{}`` when there is none.

    The returned dict is already program-validated for *source* eligibility. The
    project is only established for a predecessor whose **actual tail** already
    carries a human or program-verified attribution on a live task; anything
    else (unresolved tail, withdrawn tail, a still-running predecessor task)
    yields no usable basis.
    """
    info = archive_info(service, task['source_key'])
    if info is None:
        return {}
    prior = preceding_batch(service, info)
    if prior is None:
        return {}
    base = {'kind': KIND, 'session': info['head'], 'source_key': prior['source_key'],
            'archive_id': prior['archive_id'], 'start_line': prior['start_line'],
            'end_line': prior['end_line'], 'session_id': prior['session_id']}
    prior_task = _prior_task(service, prior['source_key'])
    if prior_task is None or prior_task['status'] in ('pending', 'running'):
        return {**base, 'state': STATE_PENDING_PREVIOUS}
    if prior_task['status'] == 'superseded':
        return {}
    # A new basis may only be built from a predecessor whose *own* stored version
    # is still the live one. Its units were produced from the text of that
    # version, so borrowing the current revision for them would silently attach
    # today's cleaned source to an older project conclusion. Once the source has
    # moved on (a cleaning edit, or a new archive version) there is no usable
    # basis until that new version is itself organized.
    live, cleaning_revision = current_source(service, prior['source_key'])
    if live is None or str(prior_task['source_revision'] or '') != live:
        return {}
    unit = _prior_tail_unit(service, prior_task['id'])
    if unit is None or unit['disposition'] not in ('keep', 'history_only') \
            or unit['decision'] not in ('auto', 'manual') or not unit['project']:
        return {}
    if not _prior_project_verified(service, unit):
        return {}
    return {**base, 'state': STATE_READY, 'task_id': prior_task['id'],
            'unit_digest': unit['digest'], 'project': unit['project'],
            'basis_revision': fingerprint(unit, live),
            'prior_revision': int(unit['revision'] or 0),
            'prior_source_revision': live,
            'prior_cleaning_revision': cleaning_revision,
            'prior_payload_sha256': str(prior.get('payload_sha256') or ''),
            'quote': _locatable(prior_task.get('source_snapshot') or '', unit),
            'basis_source': 'manual' if unit['decision'] == 'manual' else 'auto'}


def fingerprint(unit, source_revision):
    """Semantic fingerprint of the predecessor a basis rests on.

    Covers the unit's own revision counter, its text, cleaned text, evidence
    quote, decision, disposition and reason, plus the predecessor's *live*
    source revision (what the cleaning page page and the archived bytes actually
    say right now, not a cached task column). Any of those changing means the
    recorded basis no longer describes what it was validated against, so it must
    be re-checked rather than trusted. The stored value is always re-computed and
    compared; it is never presented as evidence on its own.
    """
    values = {key: unit.get(key) for key in
              ('revision', 'text', 'cleaned_text', 'evidence_quote', 'project',
               'decision', 'disposition', 'reason')}
    values['source_revision'] = source_revision
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True,
                                     default=str).encode()).hexdigest()[:32]


# --------------------------------------------------------- validation / encode

def encode(basis):
    """Canonical storage form; empty string for "no basis"."""
    if not basis:
        return ''
    return json.dumps(basis, ensure_ascii=False, sort_keys=True)


def decode(raw):
    if not raw:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(str(raw))
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def validate(service, basis, *, depth=1):
    """Re-check one stored basis against the live store; ``True`` when current.

    The predecessor's unit is re-read, its fingerprint re-computed and compared,
    and one nested level of inheritance is checked so a chain is not left
    pointing at a basis that has itself just been invalidated. The archive
    identity and bytes are verified last, because that is the only step that
    decrypts anything.
    """
    if not basis or basis.get('state') != STATE_READY:
        return False
    digest, project = basis.get('unit_digest'), basis.get('project')
    task_id = basis.get('task_id')
    if not digest or not project or type(task_id) is not int:
        return False
    conn = service.store._connection()
    row = conn.execute('SELECT * FROM organization_units WHERE task_id=? AND digest=?',
                       (task_id, digest)).fetchone()
    if row is None:
        return False
    unit = dict(row)
    if unit['project'] != project or unit['disposition'] not in ('keep', 'history_only') \
            or unit['decision'] not in ('auto', 'manual'):
        return False
    task_row = conn.execute('SELECT * FROM organization_tasks WHERE id=?', (task_id,)).fetchone()
    if task_row is None or task_row['status'] == 'superseded' \
            or task_row['source_key'] != basis.get('source_key'):
        return False
    recorded = str(basis.get('basis_revision') or '')
    live, _cleaning_revision = current_source(service, basis.get('source_key'))
    if not recorded or live is None or fingerprint(unit, live) != recorded:
        return False
    info = archive_info(service, basis.get('source_key'))
    if info is None:
        return False
    if info['head'] != basis.get('session') or info['start_line'] != basis.get('start_line') \
            or info['end_line'] != basis.get('end_line'):
        return False
    nested = decode(unit.get('context_basis'))
    if nested and depth > 0 and not validate(service, nested, depth=depth - 1):
        return False
    return True


def refresh(service, basis):
    """Re-validate a stored basis; ``(basis_or_empty, changed)``."""
    if not basis:
        return {}, False
    if basis.get('state') == STATE_READY and validate(service, basis):
        return basis, False
    # A basis that was never ready carried no context into the model, so it is
    # not a "change" from the caller's point of view.
    return {}, basis.get('state') == STATE_READY


# ------------------------------------------------------------------- prompting

def basis_prompt(basis, tail=''):
    """The bounded background block for the segmentation prompt.

    Only ever carries the predecessor's identity, its established project and a
    bounded tail. It is labelled as background so the model cannot take its
    numbering, quotes or content as this batch's own.
    """
    if not basis or basis.get('state') != STATE_READY:
        return ''
    lines = ['同会话前文（仅供判断本批是否延续同一项目同一任务，不是本批内容；',
             '不得把它的编号、原话或正文当作本批的正文或证据）：',
             '- 来源：%s（archive %d，第 %d-%d 行，会话 %s）'
             % (basis['source_key'], basis['archive_id'], basis['start_line'],
                basis['end_line'], str(basis.get('session_id') or '')[:8] + '…'),
             '- 已确认归属：%s（依据：%s）'
             % (basis['project'], '人工归属' if basis.get('basis_source') == 'manual' else '正文明确点名'),
             '- 前文原话：「%s」' % str(basis.get('quote') or '')]
    if tail:
        lines.append('- 前文末尾：\n' + str(tail)[-TAIL_CHARS:])
    return '\n'.join(lines)


def unit_context(basis):
    """The compact, timestamp-free basis a unit inherited from, for display.

    It keeps everything ``validate`` needs, so a re-check reads the same facts
    that were recorded; only the bounded prompt tail is dropped.
    """
    if not basis or basis.get('state') != STATE_READY:
        return ''
    return encode({key: basis[key] for key in
                   ('kind', 'state', 'source_key', 'archive_id', 'start_line', 'end_line',
                    'session', 'session_id', 'task_id', 'unit_digest', 'project',
                    'basis_revision', 'prior_revision', 'prior_source_revision',
                    'prior_payload_sha256', 'prior_cleaning_revision', 'quote')
                   if key in basis})


def reason_for(basis):
    """Human-readable attribution reason naming exactly which predecessor."""
    return ('沿用同会话前文：%s（第 %d-%d 行）已确认的 %s 归属，原话「%s」'
            % (basis['source_key'], basis['start_line'], basis['end_line'],
               basis['project'], str(basis.get('quote') or '')[:60]))


# ------------------------------------------------------- bounded reconciliation

_CANDIDATE_SQL = """
SELECT b.unit_task_id,b.unit_digest,b.prior_task_id,b.prior_digest,b.prior_source_key,
       b.prior_start_line,b.prior_end_line,b.project,
       p.project AS prior_project,p.decision AS prior_decision,p.disposition AS prior_disposition,
       pt.status AS prior_status
FROM organization_context_basis b
LEFT JOIN organization_units p ON p.task_id=b.prior_task_id AND p.digest=b.prior_digest
LEFT JOIN organization_tasks pt ON pt.id=b.prior_task_id
LEFT JOIN session_archives sa ON sa.id=b.prior_archive_id
LEFT JOIN knowledge_cleaning_reviews cr ON cr.source_key=b.prior_source_key
JOIN organization_units u ON u.task_id=b.unit_task_id AND u.digest=b.unit_digest
WHERE b.state='active' AND u.decision='auto'
  AND (p.task_id IS NULL OR pt.id IS NULL OR pt.status='superseded'
       OR sa.id IS NULL OR sa.state<>'available'
       OR p.project<>b.project OR p.decision NOT IN ('auto','manual') OR p.disposition NOT IN ('keep','history_only')
       OR p.revision<>b.prior_revision OR pt.source_revision<>b.prior_source_revision
       OR sa.payload_sha256<>b.prior_payload_sha256
       OR COALESCE(cr.revision,0)<>b.prior_cleaning_revision)
ORDER BY b.unit_task_id,b.unit_digest LIMIT ?
"""


def record_dependency(service, task_id, digest, basis, *, conn=None):
    """Remember which predecessor one unit's automatic project rests on."""
    if not basis or basis.get('state') != STATE_READY:
        return
    now = _now_iso()
    connection = conn or service.store._connection()
    connection.execute(
        'INSERT INTO organization_context_basis(unit_task_id,unit_digest,prior_task_id,prior_digest,'
        'prior_source_key,prior_archive_id,prior_start_line,prior_end_line,prior_revision,'
        'prior_source_revision,prior_payload_sha256,prior_cleaning_revision,'
        'project,state,created_at,updated_at) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) '
        "ON CONFLICT(unit_task_id,unit_digest) DO UPDATE SET prior_task_id=excluded.prior_task_id,"
        'prior_digest=excluded.prior_digest,prior_source_key=excluded.prior_source_key,'
        'prior_archive_id=excluded.prior_archive_id,'
        'prior_start_line=excluded.prior_start_line,prior_end_line=excluded.prior_end_line,'
        'prior_revision=excluded.prior_revision,'
        'prior_source_revision=excluded.prior_source_revision,'
        'prior_payload_sha256=excluded.prior_payload_sha256,'
        'prior_cleaning_revision=excluded.prior_cleaning_revision,'
        "project=excluded.project,state='active',updated_at=excluded.updated_at",
        (int(task_id), str(digest), int(basis['task_id']), str(basis['unit_digest']),
         str(basis['source_key']), int(basis.get('archive_id') or 0),
         int(basis.get('start_line') or 0), int(basis.get('end_line') or 0),
         int(basis.get('prior_revision') or 0), str(basis.get('prior_source_revision') or ''),
         str(basis.get('prior_payload_sha256') or ''),
         int(basis.get('prior_cleaning_revision') or 0),
         str(basis['project']), 'active', now, now))


def clear_dependency(service, task_id, digest, *, conn=None):
    connection = conn or service.store._connection()
    connection.execute('DELETE FROM organization_context_basis WHERE unit_task_id=? AND unit_digest=?',
                       (int(task_id), str(digest)))


def clear_task_dependencies(service, task_id, *, conn=None):
    connection = conn or service.store._connection()
    connection.execute('DELETE FROM organization_context_basis WHERE unit_task_id=?', (int(task_id),))


def dependency_rows(service, task_id):
    return [dict(row) for row in service.store._connection().execute(
        'SELECT * FROM organization_context_basis WHERE unit_task_id=? ORDER BY unit_digest',
        (int(task_id),))]


def invalidate_dependents(service, *, limit=RECONCILE_LIMIT):
    """Bounded, model-free: send auto units with a dead predecessor back to review.

    The candidate predicate is actionable in SQL, so a valid row never starves a
    later broken one; a settled unit leaves the candidate set on the next tick,
    which makes repeated bounded scans idempotent. A manual decision is never
    touched, and the derived knowledge is left in place: the existing
    ``unit_derivations.currently_backed`` rule already stops an unconfirmed unit
    from supplying current recall.
    """
    summary = {'examined': 0, 'invalidated': 0, 'model_calls': 0}
    conn = service.store._connection()
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='organization_context_basis'"
                    ).fetchone() is None:
        return summary
    # A bounded cascade: one round settles every direct dependent; a dependent of
    # a dependent becomes actionable in the next round because its predecessor's
    # decision just changed. The loop stops as soon as a round settles nothing or
    # finds nothing, so the total work stays within CASCADE_ROUNDS * limit.
    for _round in range(CASCADE_ROUNDS):
        try:
            rows = conn.execute(_CANDIDATE_SQL, (max(1, int(limit)),)).fetchall()
        except Exception:
            break
        if not rows:
            break
        summary['examined'] += len(rows)
        settled = 0
        touched_tasks = set()
        for row in rows:
            if _invalidate_one(service, conn, row):
                settled += 1
                touched_tasks.add(row['unit_task_id'])
        summary['invalidated'] += settled
        _refresh_tasks(service, touched_tasks)
        if not settled:
            break
    return summary


def _invalidate_one(service, conn, row):
    """Send one automatically inherited unit back to review; never a manual one."""
    reason = '前文依据已变化或失效（%s 第 %d-%d 行），本批退回核对' % (
        str(row['prior_source_key'] or row['prior_task_id']),
        int(row['prior_start_line'] or 0), int(row['prior_end_line'] or 0))
    with service.store.transaction():
        unit = conn.execute('SELECT * FROM organization_units WHERE task_id=? AND digest=?',
                            (row['unit_task_id'], row['unit_digest'])).fetchone()
        if unit is None or unit['decision'] == 'manual':
            conn.execute("UPDATE organization_context_basis SET state='detached',updated_at=? "
                         'WHERE unit_task_id=? AND unit_digest=?',
                         (_now_iso(), row['unit_task_id'], row['unit_digest']))
            return False
        conn.execute("UPDATE organization_units SET decision='review',project='',reason=?,"
                     'revision=revision+1,updated_at=? WHERE task_id=? AND digest=?',
                     (reason[:600], _now_iso(), row['unit_task_id'], row['unit_digest']))
        conn.execute("UPDATE organization_context_basis SET state='invalid',updated_at=? "
                     'WHERE unit_task_id=? AND unit_digest=?',
                     (_now_iso(), row['unit_task_id'], row['unit_digest']))
    return True


def _refresh_tasks(service, task_ids):
    if not task_ids:
        return
    from evolvmem.auto_organization import _refresh_task_state
    for task_id in sorted(task_ids):
        try:
            _refresh_task_state(service, task_id)
        except Exception:
            continue


# ------------------------------------------------------------ claim preflight

def awaiting_predecessor(service, source_key):
    """Whether this batch must wait for its own predecessor's task to settle.

    Called on the claim hot path, so it is metadata-only: no payload is ever
    decrypted. It only reports ``True`` while a predecessor *task* is pending or
    running, which is a finite state (the worker retries such a task at most
    ``MAX_ATTEMPTS`` times). A predecessor with no task at all is never waited
    for, so a batch can never be parked forever by a source nobody queued.
    """
    info = _structural_info(service, source_key)
    if info is None:
        return False
    conn = service.store._connection()
    try:
        row = conn.execute(
            _sibling_sql() + ' AND ' + _end_line_sql('external_session_id') + '<? '
            'ORDER BY ' + _end_line_sql('external_session_id') + ' DESC LIMIT 1',
            (*_sibling_params(info), info['start_line'])).fetchone()
    except Exception:
        return False
    if row is None:
        return False
    task = conn.execute('SELECT status FROM organization_tasks WHERE source_key=? '
                        'ORDER BY id DESC LIMIT 1', (_source_key(row['id']),)).fetchone()
    return bool(task is not None and task['status'] in ('pending', 'running'))

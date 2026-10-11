"""Business criteria and explicitly confirmed one-off guidance.

Only semantic rules participate in automatic organization. Legacy phrase
helpers remain for reading old records and compatibility, never for assigning
new units. Semantic rules require AI reasons and verbatim source evidence.
"""
from __future__ import annotations

from evolvmem import knowledge_scope as ks
import json

import re

from evolvmem.context_store import _now_iso

_TERM = re.compile(r'[a-zA-Z_]{3,}|[\u4e00-\u9fff]{2}')
# Words that cannot by themselves identify a topic. A condition made only of
# these terms stays a "pending" suggestion and is never applied automatically.
GENERIC = {
    '资料', '项目', '内容', '文档', '知识', '记忆', '整理', '处理', '以后', '后续',
    '相关', '通用', '默认', '需要', '这个', '那个', '东西', '信息', '工作', '事情',
    '文件', '数据', '聊天', '对话', '会话', '记录', '情况', '问题', '时候',
}
MIN_PHRASE = 4  # Kept for importers; matching below applies at every length.
# Contiguous-phrase matching. Only the stored phrase is cleaned: its allowed
# horizontal whitespace (space / tab / full-width space / nbsp) is dropped and
# the remaining literal characters are joined by an optional horizontal gap, so
# stored "不是AB" matches a unit "不是 ab" and stored "创作者 联络" matches
# "创作者联络" -- whitespace may differ in both directions. The pattern is
# searched on the casefolded unit with its ORIGINAL whitespace and line breaks,
# so the phrase's outer ASCII word edges still see the unit's real neighbours
# ("AlphaShop" matches inside "an alphashop document", never "XAlphaShop" or
# "alphashopping"), and no line break or other vertical whitespace is ever
# consumed: a phrase is never assembled across two paragraphs. A stored phrase
# that itself contains a line break (or any other non-allowed whitespace) never
# matches.
_PHRASE_GAP = r'[ \t\u3000\u00a0]*'
_PHRASE_HORIZONTAL = re.compile(r'[ \t\u3000\u00a0]')
_ASCII_WORD_CHARS = frozenset(
    '0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_')
_ASCII_EDGE_LEFT = '(?<![0-9A-Za-z_])'
_ASCII_EDGE_RIGHT = '(?![0-9A-Za-z_])'


def phrase_pattern(phrase):
    """Compile one stored condition or exception into a contiguous-phrase regex.

    The phrase's own allowed horizontal whitespace is removed and its literal
    characters are joined by ``_PHRASE_GAP``, so "不是AB" matches "不是 ab" and
    "创作者 联络" matches "创作者联络" in either direction. The regex is meant
    to be searched on a casefolded unit that keeps its original whitespace, so
    the outer ASCII word edges see the unit's real neighbours. Empty input, or a
    phrase that still contains a line break or other non-allowed whitespace,
    yields ``None``.
    """
    cleaned = _PHRASE_HORIZONTAL.sub('', str(phrase or '').casefold())
    if not cleaned or any(char.isspace() for char in cleaned):
        return None
    pattern = _PHRASE_GAP.join(re.escape(char) for char in cleaned)
    if cleaned[0] in _ASCII_WORD_CHARS:
        pattern = _ASCII_EDGE_LEFT + pattern
    if cleaned[-1] in _ASCII_WORD_CHARS:
        pattern = pattern + _ASCII_EDGE_RIGHT
    return re.compile(pattern)


def phrase_match(phrase, unit) -> bool:
    """True when one stored phrase appears whole and contiguous in the unit.

    The unit keeps its original whitespace and line breaks; only the phrase's
    own horizontal whitespace is optional, and the phrase's parts are never
    searched independently (no cross-paragraph reassembly).
    """
    pattern = phrase_pattern(phrase)
    return bool(pattern and pattern.search(str(unit or '').casefold()))


def terms(text: str) -> set[str]:
    plain = str(text or '').replace('"', ' ')
    return {token.casefold() for token in _TERM.findall(plain)}


def distinctive(text: str) -> set[str]:
    return terms(text) - GENERIC


def narrow(condition: str) -> bool:
    """A reusable condition needs a distinctive term most units will not share."""
    return bool(distinctive(condition))


def state_for(condition: str, scope: str) -> str:
    if scope != 'future':
        return 'batch'
    return 'reusable' if narrow(condition) else 'pending'


def rows(service, *, enabled_only: bool = True):
    sql = 'SELECT * FROM organization_guidance'
    if enabled_only:
        sql += ' WHERE enabled=1'
    sql += ' ORDER BY id'
    return [dict(r) for r in service.store._connection().execute(sql)]


def record(service, *, guidance, scope, project='', condition='', exceptions='',
           negative=False, enabled=True, source_text='', source_task_id=None, projects=None, knowledge_scope='project', matching_mode='phrase'):
    """Store one correction atomically; a second connection sees it immediately."""
    text = str(guidance or '').strip()
    if not 1 <= len(text) <= 1000:
        raise ValueError('invalid_guidance')
    if scope not in ('batch', 'future'):
        raise ValueError('invalid_guidance_scope')
    condition = str(condition or '')[:1000 if matching_mode == 'semantic' else 500]
    now = _now_iso()
    with service.store.transaction():
        cursor = service.store._connection().execute(
            'INSERT INTO organization_guidance(scope,project,condition,exceptions,guidance,state,negative,'
            'enabled,revision,source_text,source_task_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?)',
            (scope, str(project or ''), condition, str(exceptions or '')[:500], text,
             ('reusable' if matching_mode == 'semantic' and scope == 'future' else state_for(condition, scope)), 1 if negative else 0, 1 if enabled else 0,
             str(source_text or text)[:1000], source_task_id, now, now))
        target = ks.normalize(service, {'project': project, 'projects': projects if projects is not None else ([project] if project else []), 'knowledge_scope': knowledge_scope})
        service.store._connection().execute('UPDATE organization_guidance SET projects=?,knowledge_scope=?,matching_mode=? WHERE id=?',
            (json.dumps(target['projects']), target['knowledge_scope'], matching_mode, cursor.lastrowid))
        return dict(service.store._connection().execute(
            'SELECT * FROM organization_guidance WHERE id=?', (cursor.lastrowid,)).fetchone())


def set_enabled(service, guidance_id, enabled, *, expected_revision=None):
    """Disable/enable with an optional revision check, committed before return."""
    if type(guidance_id) is not int:
        raise ValueError('invalid_guidance')
    with service.store.transaction():
        conn = service.store._connection()
        existing = conn.execute('SELECT * FROM organization_guidance WHERE id=?', (guidance_id,)).fetchone()
        if not existing:
            raise ValueError('guidance_not_found')
        if expected_revision is not None and int(expected_revision) != existing['revision']:
            raise ValueError('revision_conflict')
        conn.execute(
            'UPDATE organization_guidance SET enabled=?,revision=revision+1,updated_at=? WHERE id=?',
            (1 if enabled else 0, _now_iso(), guidance_id))
        return dict(conn.execute('SELECT * FROM organization_guidance WHERE id=?', (guidance_id,)).fetchone())


def _condition_match(row, folded: str):
    """The condition matches only as one complete contiguous phrase."""
    return phrase_match(row['condition'], folded)


def exception_items(exceptions) -> list[str]:
    """Split a stored exceptions field into non-empty items.

    Several exceptions are separated by ``|`` or a newline. A single-value field
    (the original form) yields exactly one item, and empty segments are ignored.
    """
    return [item.strip() for item in re.split(r'[|\n]', str(exceptions or '')) if item.strip()]


def _exception_hit(item: str, folded: str) -> bool:
    """One exception item uses the same complete contiguous phrase rule."""
    return phrase_match(item, folded)


def _excluded(row, folded: str) -> str:
    """The exception item that removes this rule, or '' when none matches."""
    for item in exception_items(row['exceptions']):
        if _exception_hit(item, folded):
            return item
    return ''


def classify(service, unit):
    """Resolve reusable guidance for one unit into an explicit outcome.

    status is one of: none (no rule touched this unit), apply (one project),
    review (negative rule, exception or conflicting rules), pending (a stored
    suggestion with no narrow condition; surfaced but never applied).
    """
    body = str(unit.get('body') or unit.get('text') or '')
    folded = body.casefold()
    negatives, positives, pendings = [], [], []
    for row in rows(service):
        if row['scope'] != 'future' or not ks.assigned(row):
            continue
        condition_hit = _condition_match(row, folded)
        if row['state'] != 'reusable' or not narrow(row['condition']):
            # A stored suggestion never applies; surface it so the user can
            # confirm a narrower condition instead of guessing.
            if row['condition'] and condition_hit:
                pendings.append(row)
            continue
        if not condition_hit:
            continue
        hit = _excluded(row, folded)
        if hit:
            negatives.append({**row, 'negative': True, 'conflict': True, 'exception_hit': hit,
                              'reason': '命中你设置的例外：' + hit})
            continue
        if row['negative']:
            negatives.append({**row, 'reason': '你标记的反例与这条单元相关，需人工核对后再归属'})
            continue
        positives.append({**row, 'reason': '沿用了你确认过的指导：' + row['guidance']})
    if negatives:
        return {'status': 'review', **negatives[0]}
    projects = {(ks.scope(row), tuple(ks.projects(row))) for row in positives}
    if len(projects) > 1:
        return {'status': 'review', 'conflict': True, 'project': '',
                'reason': '你保存的多条指导指向不同项目，需要人工确认：' + '、'.join(ks.label(row) for row in positives)}
    if positives:
        return {'status': 'apply', **positives[0]}
    if pendings:
        return {'status': 'pending', 'suggestion': pendings[0]['project'],
                'reason': '这条指导的适用条件太宽泛，暂不自动套用：' + pendings[0]['guidance']}
    return {'status': 'none'}


def match(service, unit):
    """Backward-compatible narrow view: a dict when a rule applies or blocks."""
    result = classify(service, unit)
    if result['status'] in ('apply', 'review'):
        return result
    return None


def edit_criteria(service, body):
    """A user rewrites a saved rule as business criteria with explicit applicability."""
    condition, exceptions = str(body.get('condition') or '').strip(), str(body.get('exceptions') or '').strip()
    if not 1 <= len(condition) <= 1000 or len(exceptions)>500:
        raise ValueError('review_condition_required')
    target=ks.normalize(service,body,required=True)
    with service.store.transaction():
        conn=service.store._connection()
        row=conn.execute('SELECT * FROM organization_guidance WHERE id=?',(body.get('id'),)).fetchone()
        if row is None:raise ValueError('guidance_not_found')
        if row['revision']!=body.get('expected_revision'):raise ValueError('revision_conflict')
        conn.execute("UPDATE organization_guidance SET condition=?,exceptions=?,project=?,projects=?,knowledge_scope=?,"
                     "matching_mode='semantic',state='reusable',guidance=?,source_text=?,enabled=1,scope='future',negative=0,"
                     'revision=revision+1,updated_at=? WHERE id=?',
                     (condition,exceptions,target['project'],json.dumps(target['projects']),target['knowledge_scope'],
                      condition,condition,_now_iso(),row['id']))
    return {'items':[dict(conn.execute('SELECT * FROM organization_guidance WHERE id=?',(row['id'],)).fetchone())]}

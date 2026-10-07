"""User guidance with explicit scope, conditions, exceptions and enable state.

One correction is a one-off by default. Reuse of a correction for later sources
is an explicit request. A reusable rule needs a *narrow* match condition: the
user's own condition must contain at least one distinctive term beyond generic
words such as 资料 / 项目, and every condition term (or the whole condition
phrase) must be present in the unit. Exceptions exclude a match instead of
silently shrinking the keyword set, and two matching rules that point at
different projects are a conflict for review, never a first-rule-wins guess.
"""
from __future__ import annotations

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
MIN_PHRASE = 4


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
           negative=False, enabled=True, source_text='', source_task_id=None):
    """Store one correction atomically; a second connection sees it immediately."""
    text = str(guidance or '').strip()
    if not 1 <= len(text) <= 1000:
        raise ValueError('invalid_guidance')
    if scope not in ('batch', 'future'):
        raise ValueError('invalid_guidance_scope')
    condition = str(condition or '')[:500]
    now = _now_iso()
    with service.store.transaction():
        cursor = service.store._connection().execute(
            'INSERT INTO organization_guidance(scope,project,condition,exceptions,guidance,state,negative,'
            'enabled,revision,source_text,source_task_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?)',
            (scope, str(project or ''), condition, str(exceptions or '')[:500], text,
             state_for(condition, scope), 1 if negative else 0, 1 if enabled else 0,
             str(source_text or text)[:1000], source_task_id, now, now))
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
    condition = str(row['condition'] or '').strip()
    if not condition:
        return False
    if len(condition) >= MIN_PHRASE and condition.casefold() in folded:
        return True
    needed = terms(condition)
    return bool(needed) and all(token in folded for token in needed)


def exception_items(exceptions) -> list[str]:
    """Split a stored exceptions field into non-empty items.

    Several exceptions are separated by ``|`` or a newline. A single-value field
    (the original form) yields exactly one item, and empty segments are ignored.
    """
    return [item.strip() for item in re.split(r'[|\n]', str(exceptions or '')) if item.strip()]


def _exception_hit(item: str, folded: str) -> bool:
    """One exception item uses the original single-value match rule."""
    if item.casefold() in folded:
        return True
    needed = terms(item)
    return bool(needed) and all(token in folded for token in needed)


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
        if row['scope'] != 'future' or not row['project']:
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
    projects = {row['project'] for row in positives}
    if len(projects) > 1:
        return {'status': 'review', 'conflict': True, 'project': '',
                'reason': '你保存的多条指导指向不同项目，需要人工确认：' + '、'.join(sorted(projects))}
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

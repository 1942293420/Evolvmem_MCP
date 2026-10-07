"""Concise Q&A backed by authoritative knowledge, conditions and source revisions."""
from __future__ import annotations

import hashlib
import json
import re

from evolvmem.context_store import _now_iso
from evolvmem.context_temporal import window_contains
from evolvmem.extraction_policy import contains_sensitive_text
from evolvmem.memory_learning import CATEGORIES

HISTORY_TYPES = ('session_summary', 'project_summary', 'workstream_checkpoint')

# Coarse buckets for review. They never replace the actual reason text, only
# group same-kind review items so a reviewer can look at one cause at a time.
REVIEW_REASON_LABELS = {
    'conflict': '冲突待确认',
    'evidence': '依据不足',
    'relation': '关联待核对',
    'source': '来源待确认',
    'verification': '有效性未验证',
    'format': '问答待整理',
    'other': '其他待确认',
}

# The stored Q&A placeholder names no concrete cause; the real ingestion reason
# behind it must be shown instead.
GENERIC_QA_REASONS = frozenset({'推断、来源或入库状态需要确认'})


def _experience_item(row):
    return row.get('content_type') == 'experience' or bool(row.get('experience_payload'))


def _unverified_experience(row):
    """A real experience item without a verified outcome, judged by its fields.

    Withdrawal or deactivation is a source problem, not missing verification:
    only zero successes or a missing payload mean the experience is unverified.
    """
    if not _experience_item(row):
        return False
    return int(row.get('success_count') or 0) <= 0 or not row.get('experience_payload')


def _review_code(reason, row):
    text = str(reason or '')
    if any(keyword in text for keyword in ('原话', '推断', '缺少用户明确依据', '缺少依据')):
        return 'evidence'
    if any(keyword in text for keyword in ('同一标识', '相同标识', '重复标识', '已有不同答案', '已有不同内容')):
        return 'conflict'
    if any(keyword in text for keyword in ('关联资料', '未找到可证明重复')):
        return 'relation'
    # Verification is only ever claimed for a real experience item, never from
    # the learning category alone.
    if _experience_item(row) and '验证' in text:
        return 'verification'
    if any(keyword in text for keyword in ('缺少完整问答', '缺少答案', '问答缺失',
                                           '问答答案与记忆内容不一致', '尚未整理为简洁问答')):
        return 'format'
    if any(keyword in text for keyword in ('来源', '归属', '项目线索', '失效', '未生效',
                                           '暂存', '迁移', '撤回', '重新提炼')):
        return 'source'
    return 'other'


def _review_reason(row, reason, *, effective, priority):
    """One coarse {code,label,detail}; ``detail`` keeps the actual reason text."""
    if effective:
        return None
    text = str(reason or '')
    if priority == 'source':
        # The current source/staleness judgment wins: never re-label it as
        # unverified just because the merged text mentions verification or the
        # item happens to be an experience.
        code = 'source'
    else:
        if text in GENERIC_QA_REASONS:
            text = str(row.get('ingestion_reason') or '').strip() or text
        code = _review_code(text, row)
    return {'code': code, 'label': REVIEW_REASON_LABELS[code], 'detail': text}


def _merge_review_reasons(*collections):
    """Deduplicate (code, detail) pairs across the sources of one display group."""
    merged, seen = [], set()
    for reasons in collections:
        for reason in reasons or ():
            key = (reason.get('code'), reason.get('detail'))
            if key not in seen:
                seen.add(key)
                merged.append(reason)
    return merged


def fingerprint(row):
    return hashlib.sha256(json.dumps([row['project'], row['scope'], row['body'],
        row['learning']['category'], row['learning'].get('trigger', '')], ensure_ascii=False).encode()).hexdigest()


def normalize(question):
    return re.sub(r'[\s，。？！、,?.!：:]+', '', question).casefold()


def validate(question, answer):
    if not isinstance(question, str) or not 4 <= len(question.strip()) <= 160 or contains_sensitive_text(question):
        raise ValueError('invalid_qa_question')
    if not isinstance(answer, str) or not 5 <= len(answer.strip()) <= 400 or contains_sensitive_text(answer):
        raise ValueError('invalid_qa_answer')
    return question.strip(), answer.strip()


def detail(service, item_id):
    row = service.knowledge().detail(item_id)
    if row['content_type'] in HISTORY_TYPES:
        raise ValueError('history_is_not_qa')
    saved = service.store._connection().execute('SELECT * FROM knowledge_qa WHERE item_id=?', (item_id,)).fetchone()
    current = fingerprint(row)
    from evolvmem.memory_eligibility import eligible
    proof_ready = eligible(service.store, item_id)
    effective = bool(proof_ready and saved and saved['status'] == 'active' and saved['source_fingerprint'] == current
                     and service.learning().usable(row)
                     and window_contains(row['effective_from'], row['effective_until'], _now_iso()))
    status = saved['status'] if saved else 'unformatted'
    reason = saved['reason'] if saved else '旧资料尚未整理为简洁问答'
    priority = ''
    if not proof_ready:
        status, reason = 'candidate', '来源暂存、归属已变更，或方法尚无验证依据，暂不用于召回'
        # An unverified experience is its own cause; other ineligible sources
        # keep the current source judgment instead of an old ingestion reason.
        priority = '' if _unverified_experience(row) else 'source'
    elif saved and saved['source_fingerprint'] != current:
        status, reason, priority = 'stale', '来源正文、分类、条件或归属已改变，需要重新核对问答', 'source'
    elif saved and saved['status'] == 'active' and not effective:
        status, reason, priority = 'candidate', '来源尚未生效、已失效或归属待确认', 'source'
    revision = hashlib.sha256(json.dumps([row['revision'], dict(saved) if saved else None], sort_keys=True).encode()).hexdigest()
    return {'id': item_id, 'question': saved['question'] if saved else '', 'answer': saved['answer'] if saved else '',
            'category': row['learning']['category'], 'trigger': row['learning'].get('trigger', ''), 'learning':row['learning'],
            'project': row['project'], 'scope': row['scope'], 'status': status, 'effective': effective,
            'reason': reason, 'review_reason': _review_reason(row, reason, effective=effective, priority=priority),
            'origin': saved['origin'] if saved else 'legacy',
            'managed_content': row['managed_content'], 'source_body': row['body'], 'source_title': row['title'], 'sources': row['sources'],
            'revision': revision, 'updated_at': row['updated_at'], 'source_status': row['status']}


def conflicts(service, project, scope, question, answer, category, trigger, *, exclude=()):
    conn = service.store._connection()
    found = []
    for r in conn.execute("SELECT q.item_id,q.question FROM knowledge_qa q JOIN context_items i ON i.id=q.item_id "
                          "WHERE i.project=? AND i.scope=? AND q.status='active'", (project, scope)):
        if r['item_id'] in exclude or normalize(r['question']) != normalize(question):
            continue
        other = detail(service, r['item_id'])
        if other['effective'] and other['category'] == category and other['trigger'] == trigger and other['answer'] != answer:
            found.append(other)
    return found


def record(service, item_id, metadata, *, origin='extraction', approved=False):
    """Source row and Q&A commit together; this never promotes its source."""
    if 'question' not in metadata and 'answer' not in metadata:
        return
    question, answer = metadata.get('question', ''), metadata.get('answer', '')
    row = service.knowledge().detail(item_id)
    reason, valid = '', True
    try:
        question, answer = validate(question, answer)
        if answer != row['body']:
            raise ValueError('qa_answer_source_mismatch')
    except ValueError:
        question, answer = '', ''
        reason, valid = '问答缺失、过长或与来源内容不一致，需要整理', False
    eligible = valid and service.learning().usable(row) and (approved or row['learning'].get('basis') == 'explicit')
    if valid and conflicts(service, row['project'], row['scope'], question, answer,
                           row['learning']['category'], row['learning'].get('trigger', ''), exclude=(item_id,)):
        eligible, reason = False, '同一问题和适用条件已有不同答案，待确认'
    if not eligible and not reason:
        reason = '推断、来源或入库状态需要确认'
    service.store._connection().execute('INSERT INTO knowledge_qa(item_id,question,answer,source_fingerprint,status,reason,origin,updated_at) '
        'VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(item_id) DO UPDATE SET question=excluded.question,answer=excluded.answer,'
        'source_fingerprint=excluded.source_fingerprint,status=excluded.status,reason=excluded.reason,origin=excluded.origin,'
        'revision=revision+1,updated_at=excluded.updated_at',
        (item_id, question, answer, fingerprint(row), 'active' if eligible else 'candidate', reason, origin, _now_iso()))


def list_items(service, params=None):
    p = params or {}
    where, args = ["content_type NOT IN ('session_summary','project_summary','workstream_checkpoint')",
                   "status IN ('active','candidate')"], []
    project = p.get('project', '__all__')
    if project not in ('__all__', None):
        where.append('project=?'); args.append('' if project == '__global__' else project)
        if project == '__global__':
            where.append("scope='global'")
    rows = service.store._connection().execute('SELECT id FROM context_items WHERE ' + ' AND '.join(where) + ' ORDER BY updated_at DESC,id DESC', args).fetchall()
    items, groups = [], {}
    for r in rows:
        item = detail(service, r['id'])
        if p.get('category') and item['category'] != p['category']:
            continue
        state = p.get('state', '')
        if state == 'active' and not item['effective']:
            continue
        if state == 'pending' and item['effective']:
            continue
        if state == 'unformatted' and item['status'] != 'unformatted':
            continue
        if p.get('q') and str(p['q']).casefold() not in '\n'.join([item['question'], item['answer'], item['source_body']]).casefold():
            continue
        signature = (item['project'], item['scope'], item['category'], item['trigger'], item['status'],
                     normalize(item['question']), item['answer'] or item['source_body'])
        item['review_reasons'] = [item['review_reason']] if item['review_reason'] else []
        item['reason_codes'] = [item['review_reason']['code']] if item['review_reason'] else []
        if signature in groups:
            group = groups[signature]
            group['source_ids'].append(item['id'])
            group['sources'].extend(s for s in item['sources'] if s not in group['sources'])
            group['review_reasons'] = _merge_review_reasons(group['review_reasons'], item['review_reasons'])
            group['reason_codes'] = list(dict.fromkeys(r['code'] for r in group['review_reasons']))
        else:
            item['source_ids'] = [item['id']]
            groups[signature] = item
            items.append(item)
    # Count one deduplicated display group at most once per code, before the
    # reason filter and pagination.
    counted = {}
    for item in items:
        for code in dict.fromkeys(item['reason_codes']):
            counted.setdefault(code, {'label': REVIEW_REASON_LABELS[code], 'count': 0})['count'] += 1
    review_reason_counts = dict(sorted(counted.items(), key=lambda pair: (-pair[1]['count'], pair[0])))
    reason_code = str(p.get('reason_code') or '').strip()
    if reason_code:
        items = [item for item in items if reason_code in item['reason_codes']]
    # Source count follows the displayed groups after the reason filter and
    # before pagination; review_reason_counts stays a pre-filter group count.
    record_count = sum(len(item['source_ids']) for item in items)
    page = max(1, int(p.get('page', 1)))
    size = min(100, max(1, int(p.get('page_size', 30))))
    page_items = [{**item, 'source_body':item['source_body'][:350]} for item in items[(page-1)*size:page*size]]
    return {'items': page_items, 'total': len(items), 'record_count': record_count,
            'review_reason_counts': review_reason_counts, 'page': page, 'page_size': size}


def save(service, body, *, item_id=None):
    question, answer = validate(body.get('question'), body.get('answer'))
    category, trigger = body.get('category', 'reference'), str(body.get('trigger') or '').strip()
    if category not in CATEGORIES or len(trigger) > 500:
        raise ValueError('invalid_learning_category')
    kb = service.knowledge()
    with service.store.transaction():
        old = detail(service, item_id) if item_id else None
        if old and body.get('expected_revision') != old['revision']:
            raise ValueError('revision_conflict')
        project = str(body.get('project', old['project'] if old else ''))
        scope = 'project' if project else 'global'
        kb._project(project)
        peers = conflicts(service, project, scope, question, answer, category, trigger, exclude=(item_id,))
        publishing = body.get('action') == 'publish'
        if publishing and peers and not body.get('replace_conflicts'):
            raise ValueError('qa_conflict_requires_confirmation')
        if publishing and peers:
            for peer in peers:
                source = kb.detail(peer['id'])
                kb.transition(peer['id'], {'expected_revision':source['revision'], 'action':'archive'})
        derived = kb.detail(item_id) if old and kb.detail(item_id)['managed_content'] else None
        if old and not derived:
            source = kb.detail(item_id)
            source = kb.update(item_id, {'expected_revision':source['revision'], 'title':question, 'body':answer})
            if project != old['project'] or scope != old['scope']:
                source = kb.assign(item_id, {'expected_revision':source['revision'], 'project':project})
        else:
            source = kb.create({'title':question, 'body':answer, 'project':project, 'scope':scope,
                                'action':'draft', 'source':('知识来源 #'+str(derived['id'])) if derived else body.get('source') or '用户整理问答'})
            item_id = source['id']
            if derived:
                for link in derived['sources']:
                    if link.get('archive_id'):
                        service.store.record_session_source(item_id, link['archive_id'], extraction_version='qa.manual')
        source = service.learning().classify(item_id, {'expected_revision':source['revision'], 'category':category, 'trigger':trigger})
        if publishing:
            source = kb.transition(item_id, {'expected_revision':source['revision'], 'action':'publish'}, refresh_qa=False)
        else:
            kb.conn.execute("UPDATE context_items SET status='candidate' WHERE id=?", (item_id,))
            for legacy_id in source['legacy_ids']:
                kb.conn.execute("UPDATE memories SET status='candidate' WHERE id=?", (legacy_id,))
            kb._stamp(item_id, reason='问答修改待确认')
        record(service, item_id, {'question':question, 'answer':answer}, origin='manual', approved=publishing)
    kb._sync([item_id])
    return detail(service, item_id)

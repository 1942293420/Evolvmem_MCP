"""Review matching units together without inventing a shared project.

A proposal binds both the user's exact instruction and every current member.
Re-reading it before applying prevents a stale browser tab from broadening a
decision. Individual corrections retain the existing source and revision CAS.
"""
from evolvmem import knowledge_scope as ks
import hashlib
import json

from evolvmem import organization_guidance as guidance
from evolvmem.session_identity import logical_identity, is_incremental_batch


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def _family(unit):
    if unit['disposition'] == 'review':
        return '价值待确认'
    if '冲突' in unit['reason'] or '反例' in unit['reason'] or '例外' in unit['reason']:
        return '归属有冲突'
    return '项目待确认'


def _groups(service):
    from evolvmem import auto_organization as org
    from evolvmem.history_organization import _unassigned_archives
    conn = service.store._connection()
    visible = {r['id'] for r in _unassigned_archives(service)}
    rows = conn.execute("SELECT u.* FROM organization_units u JOIN organization_tasks t ON t.id=u.task_id "
                        "WHERE (t.status IN ('review','completed','failed') OR "
                        "(t.status IN ('pending','running') AND t.stage='extraction')) AND u.decision='review' "
                        "AND u.disposition NOT IN ('set_aside','history_only') ORDER BY u.task_id,u.ordinal").fetchall()
    tasks, groups = {}, {}
    for row in rows:
        unit = dict(row)
        tid = unit['task_id']
        if tid not in tasks:
            task = org.task_view(service, tid, visible=visible)
            source = None
            if task['source_key'].startswith('archive:'):
                source = conn.execute('SELECT adapter,external_session_id FROM session_archives WHERE id=?',
                                      (int(task['source_key'].split(':')[1]),)).fetchone()
            identity = task['source_key']
            if source and source['external_session_id']:
                adapter, external = source['adapter'], source['external_session_id']
                identity = (adapter, external.split(':')[0] if adapter == 'codex' and
                            is_incremental_batch(external) else logical_identity(adapter, external))
            tasks[tid] = (task, identity)
        task, identity = tasks[tid]
        if not task['source_current']:
            continue
        family = _family(unit)
        gid = _hash([identity, family, unit['category']])[:24]
        group = groups.setdefault(gid, {'id': gid, 'reason': family, 'category': unit['category'],
                                       'title': task['source_title'] or task['source_key'], 'members': []})
        group['members'].append({**unit, 'revision': org._unit_revision(unit),
                                 'source_key': task['source_key'], 'rule_revision': task['rule_revision'],
                                 'source_revision': task['source_revision']})
    return list(groups.values())


def _brief(unit):
    return {key: unit[key] for key in ('task_id', 'digest', 'revision', 'title', 'source_key')} | {
        'quote': (unit['evidence_quote'] or unit['text'])[:400]}


def groups(service, options=None):
    rows = _groups(service)
    page = max(1, int((options or {}).get('page', 1)))
    return {'items': [{k: v for k, v in group.items() if k != 'members'} |
                      {'count': len(group['members']), 'examples': [_brief(u) for u in group['members'][:3]]}
                     for group in rows[(page-1)*20:page*20]],
            'total': len(rows), 'unit_count': sum(len(g['members']) for g in rows), 'page': page, 'page_size': 20}


def detail(service, body):
    group = next((g for g in _groups(service) if g['id'] == body.get('group_id') or
                  (not body.get('group_id') and any(str(u['task_id']) == str(body.get('task_id')) and
                   u['digest'] == body.get('digest') for u in g['members']))), None)
    if not group:
        raise ValueError('review_preview_changed')
    return {k: v for k, v in group.items() if k != 'members'} | {
        'items': [_brief(u) | {k: u[k] for k in ('text', 'reason', 'disposition', 'disposition_reason')}
                  for u in group['members']]}


def preview(service, body, *, _judgments=None):
    from evolvmem import auto_organization as org
    semantic = body.get('matching_mode') == 'semantic' and body.get('selection') != 'item'
    if semantic and _judgments is None:
        from evolvmem.organization_semantic_review import proposal
        return proposal(service, body)
    target = ks.normalize(service, body, required=True)
    project = target['project']
    condition = str(body.get('condition') or '').strip()
    exceptions = str(body.get('exceptions') or '').strip()
    selection = body.get('selection', 'group')
    if selection not in ('item', 'group', 'all'):
        raise ValueError('invalid_items')
    if selection != 'item' and (not condition or len(condition) > 1000 or (not semantic and (not guidance.narrow(condition) or not guidance.phrase_pattern(condition)))):
        raise ValueError('review_condition_required')
    if len(exceptions) > 500:
        raise ValueError('invalid_guidance')
    all_groups = _groups(service)
    group = next((g for g in all_groups if g['id'] == body.get('group_id')), None)
    if not group:
        raise ValueError('review_preview_changed')
    policy = service.knowledge().rules.read()
    if selection == 'item':
        condition, exceptions = '', ''
    text = str(body.get('guidance') or (condition if semantic else f'这条资料归入 {ks.label(target)}' if selection == 'item'
                                     else f'包含“{condition}”的资料归入 {ks.label(target)}')).strip()
    if not 1 <= len(text) <= 1000:
        raise ValueError('invalid_guidance')
    result = {'group_id': group['id'], **target, 'condition': condition,
              'exceptions': exceptions, 'scope': 'future' if selection != 'item' and body.get('scope') == 'future' else 'batch',
              'selection': selection, 'guidance': text, 'eligible': [], 'excluded': [], 'remaining': 0,
              'matched_count': 0, 'matching_mode': 'semantic' if semantic else 'phrase'}
    if semantic:
        result['job_id'] = body.get('job_id')
    members = [u for g in all_groups for u in g['members']] if selection == 'all' else group['members']
    if selection == 'item':
        members = [u for u in members if u['task_id'] == body.get('task_id') and u['digest'] == body.get('digest')]
        if len(members) != 1:
            raise ValueError('review_preview_changed')
        result.update(task_id=members[0]['task_id'], digest=members[0]['digest'])
    result['searched_count'] = len(members)
    for unit in members:
        reason, code = '', ''
        matched = semantic or selection == 'item' or guidance.phrase_match(condition, unit['text'])
        result['matched_count'] += int(matched)
        hit = '' if semantic else next((x for x in guidance.exception_items(exceptions) if guidance.phrase_match(x, unit['text'])), '')
        if not matched:
            reason, code = '原文不含这段连续词句', 'phrase_mismatch'
        elif hit:
            reason, code = '命中例外：' + hit, 'exception'
        elif unit['rule_revision'] != policy['revision']:
            reason, code = '这份资料按旧规则整理，需按最新规则重新整理后再确认归属', 'rule_changed'
        elif unit['disposition'] == 'review':
            reason, code = '请先确认这条资料是否值得保留', 'value_review'
        elif selection != 'item' and not semantic:
            reason = org.OrganizationWorker._guidance_conflict(None, service, unit, target, policy)
            code = 'conflict' if reason else ''
        if not reason and len(result['eligible']) >= 100:
            reason, code = '本次最多处理 100 条，下次继续', 'batch_limit'
            result['remaining'] += 1
        entry = _brief(unit)
        if semantic and not reason:
            if len(result['eligible']) + sum(u.get('reason_code','').startswith('ai_') for u in result['excluded']) >= 100:
                reason, code = '本批最多判断 100 条，其余资料留在待确认列表。', 'batch_limit'
                result['remaining'] += 1
            else:
                verdict = (_judgments or {}).get(f"{unit['task_id']}:{unit['digest']}")
                if verdict:
                    entry.update(reason=verdict['reason'], evidence_quote=verdict['evidence_quote'])
                    if verdict['decision'] != 'include':
                        reason, code = verdict['reason'], 'ai_excluded' if verdict['decision']=='exclude' else 'ai_uncertain'
                else:
                    reason, code = '等待 AI 根据业务依据判断', 'ai_pending'
        (result['excluded'] if reason else result['eligible']).append(entry | ({'reason': reason, 'reason_code': code} if reason else {}))
    # Include all members (also excluded ones), source and rule versions. A new
    # member, changed rule or manual correction requires another preview.
    result['revision'] = _hash([result, policy['revision'], members])
    return result


def reprocess(service, body):
    """Explicit recovery for the stale sources in an unchanged review preview."""
    from evolvmem import auto_organization as org
    saved = body.get('proposal')
    if not isinstance(saved, dict) or not saved.get('revision'):
        raise ValueError('review_preview_required')
    current = preview(service, saved)
    if current != saved:
        raise ValueError('review_preview_changed')
    keys = list(dict.fromkeys(u['source_key'] for u in current['excluded']
                             if u['reason_code'] == 'rule_changed'))
    if not keys:
        raise ValueError('review_reprocess_not_needed')
    # The existing queue preserves source history and prior human decisions.
    # A click processes only this visible bounded batch, never the whole backlog.
    return org.enqueue(service, keys[:20]) | {'remaining_sources': max(0, len(keys)-20)}


def apply(service, body):
    from evolvmem import auto_organization as org
    saved = body.get('proposal')
    if not isinstance(saved, dict) or not saved.get('revision'):
        raise ValueError('review_preview_required')
    current = preview(service, saved)
    if current != saved:
        raise ValueError('review_preview_changed')
    # Validate every source first: a known stale preview writes nothing.
    for tid in {u['task_id'] for u in current['eligible']}:
        org._assert_current(service, org.task_view(service, tid))
    results = []
    for unit in current['eligible']:
        try:
            org.correct_one(service, {**current, 'task_id': unit['task_id'], 'digest': unit['digest'],
                                      'expected_revision': unit['revision']}, record_guidance=False)
            results.append({'task_id': unit['task_id'], 'digest': unit['digest'], 'ok': True})
        except ValueError as error:
            results.append({'task_id': unit['task_id'], 'digest': unit['digest'], 'ok': False, 'error': str(error)})
    succeeded = sum(r['ok'] for r in results)
    stored = None
    if succeeded and current['scope'] == 'future':
        stored = guidance.record(service, guidance=current['guidance'], project=current['project'], projects=current['projects'], knowledge_scope=current['knowledge_scope'],
                                 condition=current['condition'], exceptions=current['exceptions'], scope='future',
                                 source_task_id=current['eligible'][0]['task_id'], matching_mode=current['matching_mode'])
    return {'items': results, 'succeeded': succeeded, 'failed': len(results)-succeeded, 'guidance': stored}

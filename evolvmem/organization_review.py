"""Review matching units together without inventing a shared project.

A proposal binds both the user's exact instruction and every current member.
Re-reading it before applying prevents a stale browser tab from broadening a
decision. Individual corrections retain the existing source and revision CAS.
"""
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
                        "WHERE t.status IN ('review','completed','failed') AND u.decision='review' "
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


def preview(service, body):
    from evolvmem import auto_organization as org
    project = str(body.get('project') or '')
    service.knowledge()._project(project)
    condition = str(body.get('condition') or '').strip()
    exceptions = str(body.get('exceptions') or '').strip()
    if not condition or len(condition) > 500 or not guidance.narrow(condition) or not guidance.phrase_pattern(condition):
        raise ValueError('review_condition_required')
    if len(exceptions) > 500:
        raise ValueError('invalid_guidance')
    group = next((g for g in _groups(service) if g['id'] == body.get('group_id')), None)
    if not group:
        raise ValueError('review_preview_changed')
    policy = service.knowledge().rules.read()
    text = str(body.get('guidance') or f'包含“{condition}”的资料归入 {project}').strip()
    if not 1 <= len(text) <= 1000:
        raise ValueError('invalid_guidance')
    result = {'group_id': group['id'], 'project': project, 'condition': condition,
              'exceptions': exceptions, 'scope': 'future' if body.get('scope') == 'future' else 'batch',
              'guidance': text, 'eligible': [], 'excluded': [], 'remaining': 0}
    for unit in group['members']:
        reason = ''
        if unit['rule_revision'] != policy['revision']:
            reason = '整理规则已变化，请先重新处理这份资料'
        elif unit['disposition'] == 'review':
            reason = '资料价值仍待核对，请在单元详情中确认保留或暂存'
        elif not guidance.phrase_match(condition, unit['text']):
            reason = '不符合适用短语'
        else:
            hit = next((x for x in guidance.exception_items(exceptions) if guidance.phrase_match(x, unit['text'])), '')
            if hit:
                reason = '命中例外：' + hit
            else:
                reason = org.OrganizationWorker._guidance_conflict(None, service, unit, project, policy)
        if not reason and len(result['eligible']) >= 100:
            reason = '本次最多处理 100 条，下次继续'
            result['remaining'] += 1
        entry = _brief(unit)
        (result['excluded'] if reason else result['eligible']).append(entry | ({'reason': reason} if reason else {}))
    # Include all members (also excluded ones), source and rule versions. A new
    # member, changed rule or manual correction requires another preview.
    result['revision'] = _hash([result, policy['revision'], group['members']])
    return result


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
        stored = guidance.record(service, guidance=current['guidance'], project=current['project'],
                                 condition=current['condition'], exceptions=current['exceptions'], scope='future',
                                 source_task_id=current['eligible'][0]['task_id'])
    return {'items': results, 'succeeded': succeeded, 'failed': len(results)-succeeded, 'guidance': stored}

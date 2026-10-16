"""Only explicit human choices archive material or defer it."""
from evolvmem import organization_review as review, organization_exclusions as exclusions


def archive_one(service, body):
    """Save the selected project directly, without an AI preview or keep step."""
    from evolvmem import auto_organization as org, knowledge_scope as ks
    task_id, digest, action = body.get('task_id'), body.get('digest'), body.get('action')
    if type(task_id) is not int or not isinstance(digest, str) or action not in ('include', 'defer'):
        raise ValueError('invalid_items')
    with service.store.transaction():
        task = org.task_view(service, task_id)
        org._assert_current(service, task)
        unit = org.find_unit(service, task_id, digest)
        if org._unit_revision(unit) != body.get('expected_revision'):
            raise ValueError('revision_conflict')
        if action == 'defer':
            result = org.set_disposition(service, {**body, 'disposition': 'set_aside', 'reason': '用户选择暂不归档'})
        else:
            target = ks.normalize(service, body, required=True)
            if task['rule_revision'] != service.knowledge().rules.read()['revision']:
                raise ValueError('review_preview_changed')
            exclusions.write_decision(service, {**unit, **{k: task[k] for k in ('source_key', 'source_revision', 'rule_revision')}}, target, 'undo')
            result = org.correct_one(service, {**target, 'task_id': task_id, 'digest': digest,
                'expected_revision': body.get('expected_revision'), 'reason': '用户选择项目并保存归档'}, record_guidance=False)
    return {**result, 'action': action}


def save(service, body):
    from evolvmem import auto_organization as org
    saved, choices = body.get('proposal'), body.get('decisions')
    if not isinstance(saved, dict) or not saved.get('revision'):
        raise ValueError('review_preview_required')
    if not isinstance(choices, list) or not 1 <= len(choices) <= 100:
        raise ValueError('invalid_items')
    selected = {}
    for choice in choices:
        if (not isinstance(choice, dict) or type(choice.get('task_id')) is not int
                or not isinstance(choice.get('digest'), str)
                or choice.get('action') not in ('include', 'exclude', 'defer')):
            raise ValueError('invalid_items')
        key = (choice['task_id'], choice['digest'])
        if key in selected:
            raise ValueError('invalid_items')
        selected[key] = choice['action']
    with service.store.transaction():
        current = review.preview(service, saved)
        if current != saved:
            raise ValueError('review_preview_changed')
        entries = {(u['task_id'], u['digest']): u for u in current['eligible'] + current['excluded']}
        units = {(u['task_id'], u['digest']): u for g in review._groups(service) for u in g['members']}
        # Validate all choices before touching any row. A human can override a
        # model judgment or decide an unjudged item, but cannot admit a stale
        # or unreadable source.
        for key in selected:
            entry, unit = entries.get(key), units.get(key)
            if (entry is None or unit is None or entry['revision'] != unit['revision']
                    or entry.get('reason_code', '') not in ('', 'ai_uncertain', 'ai_excluded', 'manual_excluded', 'value_review', 'batch_limit')):
                raise ValueError('review_preview_changed')
            org._assert_current(service, org.task_view(service, unit['task_id']))
            if unit['rule_revision'] != service.knowledge().rules.read()['revision']:
                raise ValueError('review_preview_changed')
        for key, action in selected.items():
            unit = units[key]
            if action == 'defer':
                org.set_disposition(service, {'task_id': key[0], 'digest': key[1],
                    'expected_revision': unit['revision'], 'disposition': 'set_aside', 'reason': '用户选择暂不归档'})
                continue
            exclusions.write_decision(service, unit, current, 'undo' if action == 'include' else 'exclude')
            if action == 'include':
                org.correct_one(service, {**current, 'task_id': key[0], 'digest': key[1],
                                          'expected_revision': unit['revision']}, record_guidance=False)
        included = sum(action == 'include' for action in selected.values())
        stored = None
        if included and current['scope'] == 'future':
            from evolvmem import organization_guidance
            stored = organization_guidance.record(
                service, guidance=current['guidance'], project=current['project'], projects=current['projects'],
                knowledge_scope=current['knowledge_scope'], condition=current['condition'], exceptions=current['exceptions'],
                scope='future', source_task_id=next(k[0] for k, v in selected.items() if v == 'include'),
                matching_mode=current['matching_mode'])
        if current.get('job_id'):
            from evolvmem import organization_semantic_review as semantic
            remaining=semantic.proposal(service,{'job_id':current['job_id']})
            if not remaining['eligible'] and not any(u.get('reason_code') in ('ai_uncertain','ai_excluded','ai_pending','review_changed','value_review','batch_limit')
                                                    for u in remaining['excluded']):
                semantic.mark_applied(service,current['job_id'])
    return {'ok': True, 'included': included, 'excluded': sum(a == 'exclude' for a in selected.values()),
            'deferred': sum(a == 'defer' for a in selected.values()), 'guidance': stored}

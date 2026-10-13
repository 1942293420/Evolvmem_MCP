"""Save explicitly selected inclusion/exclusion choices as one review action."""
from evolvmem import organization_review as review, organization_exclusions as exclusions


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
                or choice.get('action') not in ('include', 'exclude')):
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
        # model judgment, but cannot admit a stale or unreadable source.
        for key in selected:
            entry, unit = entries.get(key), units.get(key)
            if (entry is None or unit is None or entry['revision'] != unit['revision']
                    or entry.get('reason_code', '') not in ('', 'ai_uncertain', 'ai_excluded', 'manual_excluded')):
                raise ValueError('review_preview_changed')
            org._assert_current(service, org.task_view(service, unit['task_id']))
            if unit['rule_revision'] != service.knowledge().rules.read()['revision'] or unit['disposition'] == 'review':
                raise ValueError('review_preview_changed')
        for key, action in selected.items():
            unit = units[key]
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
    return {'ok': True, 'included': included, 'excluded': len(selected) - included, 'guidance': stored}

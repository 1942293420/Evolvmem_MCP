"""Queue-state metrics, current-source review groups and actual human feedback."""
import json
from evolvmem import auto_organization as org
from evolvmem.context_store import _now_iso
from evolvmem.organization_review import _groups
from evolvmem.vector_index import VectorIndex


def feedback(service, body):
    verdict = body.get('verdict')
    if verdict not in ('correct', 'incorrect'):
        raise ValueError('invalid_feedback')
    task = org.task_view(service, body.get('task_id'))
    org._assert_current(service, task)
    with service.store.transaction():
        unit = org.find_unit(service, task['id'], str(body.get('digest') or ''))
        if org._unit_revision(unit) != body.get('expected_revision'):
            raise ValueError('revision_conflict')
        service.store._connection().execute(
            'INSERT INTO organization_review_feedback VALUES(?,?,?,?,?) '
            'ON CONFLICT(task_id,digest) DO UPDATE SET unit_revision=excluded.unit_revision,'
            'verdict=excluded.verdict,updated_at=excluded.updated_at',
            (task['id'], unit['digest'], org._unit_revision(unit), verdict, _now_iso()))
    return {'ok': True, 'verdict': verdict}


def metrics(service):
    conn = service.store._connection()
    # Aggregate the persistent queue, never decrypt full historical payloads to
    # count completed work. Source/rule CAS belongs to preview and apply; review
    # groups below still validate the sources they actually offer for action.
    tasks = [dict(row) for row in conn.execute(
        "SELECT id,status FROM organization_tasks WHERE status!='superseded' AND error_code!=?",
        (org.NO_DIALOGUE_CODE,))]
    ids = {t['id'] for t in tasks}
    columns = ('task_id,digest,revision,project,decision,reason,disposition,evidence_quote,cleaned_text,'
               'continues_context,context_basis,applied_guidance_id')
    units = [dict(u) for u in conn.execute('SELECT '+columns+' FROM organization_units') if u['task_id'] in ids]
    manual = {u['task_id'] for u in units if u['decision'] == 'manual'}
    completed = [t for t in tasks if t['status'] == 'completed']
    automatic = sum(t['id'] not in manual for t in completed)
    settled = sum(t['status'] in ('completed', 'review', 'failed') for t in tasks)
    groups = _groups(service)
    current_units = {(u['task_id'], u['digest']): u for u in units}
    checked = []
    for row in conn.execute('SELECT * FROM organization_review_feedback'):
        unit = current_units.get((row['task_id'], row['digest']))
        if unit and org._unit_revision(unit) == row['unit_revision']:
            checked.append(row['verdict'])
    correct, incorrect = checked.count('correct'), checked.count('incorrect')
    recovery = None
    try:
        recovery = json.loads(service.config.context_vector_path.with_suffix('.recovery.json').read_text())
    except (OSError, ValueError):
        pass
    return {'scope': '按任务记录统计，排除已替代和空会话；自动完成率=未经人工修正的已完成任务/已结束任务（含待确认与失败）。来源更新后的任务状态由后台复核，审核分组另行核对当前来源',
            'settled_tasks': settled, 'automatic_completed': automatic,
            'manual_completed': len(completed)-automatic,
            'automatic_rate': automatic/settled if settled else None,
            'review_groups': len(groups), 'review_units': sum(len(g['members']) for g in groups),
            'history_only_units': sum(u['disposition'] == 'history_only' for u in units),
            'guidance_reused_units': sum(u['decision'] == 'auto' and bool(u['applied_guidance_id']) for u in units),
            'feedback': {'total': len(checked), 'correct': correct, 'incorrect': incorrect,
                         'error_rate': incorrect/len(checked) if checked else None},
            'index': {'pending': VectorIndex(service.config, path=service.config.context_vector_path).is_dirty(),
                      'exists': service.config.context_vector_path.exists(),
                      'last_recovery': recovery}}

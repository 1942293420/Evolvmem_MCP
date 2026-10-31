"""Explicit read grants for assigned primary sources; real isolated SQLite."""
import copy
import json

import pytest

from evolvmem import auto_organization as org, conversation_tasks, governance_migration
from evolvmem import history_organization as history, organization_arrival
from evolvmem.capability_provenance import ProvenanceRead
from evolvmem.session_archive import SessionArchiver
from tests.test_auto_organization import base_service, service


def api():
    import importlib.util
    assert importlib.util.find_spec('evolvmem.governance_sources'), 'owner source enrollment is missing'
    from evolvmem import governance_sources
    return governance_sources


def archive(service, name='assigned', project='evo', text='dsh 增加写入校验。'):
    return SessionArchiver(service.config, service.store).archive_session(
        project, 'kimi', name, json.dumps({'messages': [{'role': 'user', 'content': text}]}, ensure_ascii=False))


def grant(service, value):
    prepared = api().build_plan(service.store._connection(), keys=[f'archive:{value.id}'])
    assert api().apply_plan(service, prepared)['applied'] == 1
    return prepared


def test_prepare_read_only_and_enrollment_keeps_public_guard_and_archive_bytes(service):
    value = archive(service)
    conn = service.store._connection()
    before = dict(conn.execute('SELECT * FROM session_archives WHERE id=?', (value.id,)).fetchone())
    raw = (service.config.data_dir / value.payload_path).read_bytes()
    changes = conn.total_changes
    conn.execute('PRAGMA query_only=ON')
    try:
        prepared = api().build_plan(conn)
    finally:
        conn.execute('PRAGMA query_only=OFF')
    assert conn.total_changes == changes and prepared['summary']['selected'] == 1
    assert '增加写入校验' not in json.dumps(prepared, ensure_ascii=False)
    assert api().apply_plan(service, prepared)['applied'] == 1
    assert api().apply_plan(service, prepared)['already_applied'] == 1
    with pytest.raises(ValueError, match='classification_no_longer_unassigned'):
        history._source_record(service, f'archive:{value.id}')
    assert f'archive:{value.id}' not in {r['key'] for r in history.source_refs(service)}
    task = conversation_tasks.enqueue(service, [f'archive:{value.id}'])['items'][0]
    assert task['source_current'] and task['source_key'].startswith('conversation:')
    assert 'dsh' in org._task_row(service, task['id'])['source_snapshot']
    assert dict(conn.execute('SELECT * FROM session_archives WHERE id=?', (value.id,)).fetchone()) == before
    assert (service.config.data_dir / value.payload_path).read_bytes() == raw
    assert not conn.execute('SELECT 1 FROM organization_units').fetchone()
    assert ProvenanceRead(service.store).source_revision(task['source_key'])[1] == ''


@pytest.mark.parametrize('change', ['payload', 'project', 'body', 'head', 'excluded'])
def test_enrollment_cas_drift_rolls_back_every_selected_source(service, change):
    first = archive(service, 'one'); second = archive(service, 'two')
    conn = service.store._connection(); prepared = api().build_plan(conn)
    with service.store.transaction():
        if change == 'payload':
            conn.execute("UPDATE session_archives SET payload_sha256='changed' WHERE id=?", (second.id,))
        elif change == 'project':
            conn.execute("UPDATE session_archives SET project='dsh' WHERE id=?", (second.id,))
        elif change == 'body':
            conn.execute("UPDATE conversation_history SET body=body||' changed' WHERE archive_id=?", (second.id,))
        elif change == 'excluded':
            conn.execute('INSERT INTO archive_source_exclusions(archive_id,payload_sha256,reason,verified_at) VALUES(?,?,?,?)',
                         (second.id, second.payload_sha256, 'system', 'now'))
    if change == 'head':
        archive(service, 'two', text='更新的原话')
    with pytest.raises(ValueError, match='revision_conflict'):
        api().apply_plan(service, prepared)
    assert api().valid_ids(service) == set()


@pytest.mark.parametrize('change', ['project', 'project_empty', 'payload', 'head', 'deleted'])
def test_grant_is_rechecked_on_every_task_and_provenance_read(service, change):
    value = archive(service)
    grant(service, value)
    task = conversation_tasks.enqueue(service, [f'archive:{value.id}'])['items'][0]
    reader = ProvenanceRead(service.store)
    assert reader.source_revision(task['source_key'])[1] == ''
    conn = service.store._connection()
    if change == 'head':
        archive(service, text='新版本')
    else:
        with service.store.transaction():
            if change == 'deleted':
                conn.execute("UPDATE session_archives SET state='purged' WHERE id=?", (value.id,))
            else:
                field = 'project' if change in ('project', 'project_empty') else 'payload_sha256'
                conn.execute(f'UPDATE session_archives SET {field}=? WHERE id=?', ('' if change == 'project_empty' else 'changed', value.id))
    assert not org.task_view(service, task['id'])['source_current']
    assert ProvenanceRead(service.store).source_revision(task['source_key'])[1]


def test_missing_body_repair_keeps_grant_but_source_revision_is_fresh(service):
    value = archive(service)
    conn = service.store._connection()
    with service.store.transaction():
        conn.execute('DELETE FROM conversation_history WHERE archive_id=?', (value.id,))
    grant(service, value)
    prepared = governance_migration.build_plan(conn, rule_revision=service.knowledge().rules.read()['revision'])
    assert [x['source_key'] for x in prepared['cleaning_repairs']] == [f'archive:{value.id}']
    assert governance_migration.repair_cleaning(service, prepared)['repaired'] == 1
    assert value.id in api().valid_ids(service)
    task = conversation_tasks.enqueue(service, [f'archive:{value.id}'])['items'][0]
    assert task['source_current']


def test_new_arrival_enrolls_after_activation_only_and_never_trusts_project_hint(service):
    before = archive(service, 'before')
    organization_arrival.update_settings(service, {'auto_new': True})
    value = archive(service, 'after')
    result = organization_arrival.discover_new(service)
    assert result['created'] == 1
    assert value.id in api().valid_ids(service) and before.id not in api().valid_ids(service)
    assert conversation_tasks.members(service, result['items'][0]['source_key']) == [f'archive:{value.id}']
    assert not service.store._connection().execute('SELECT 1 FROM organization_units').fetchone()
    assert organization_arrival.discover_new(service)['created'] == 0


def test_enrolled_no_dialogue_is_terminal_without_provider_and_not_reenqueued(service):
    value = SessionArchiver(service.config, service.store).archive_session(
        'evo', 'kimi', 'empty', json.dumps({'messages': [{'role': 'tool', 'content': 'tool log'}]}))
    grant(service, value)
    task = conversation_tasks.enqueue(service, [f'archive:{value.id}'])['items'][0]
    assert task['status'] == 'completed' and task['error_code'] == org.NO_DIALOGUE_CODE
    assert task['attempts'] == 0


def test_invalid_hash_and_grant_rollback_cas(service):
    value = archive(service); prepared = api().build_plan(service.store._connection())
    changed = copy.deepcopy(prepared); changed['entries'][0]['source_key'] = 'archive:999'
    with pytest.raises(ValueError, match='invalid_enrollment_plan'):
        api().apply_plan(service, changed)
    api().apply_plan(service, prepared)
    assert api().rollback_plan(service, prepared)['rolled_back'] == 1
    assert value.id not in api().valid_ids(service)
    assert api().rollback_plan(service, prepared)['already_rolled_back'] == 1
    with pytest.raises(ValueError, match='rolled_back'):
        api().apply_plan(service, prepared)


def test_primary_uses_original_content_not_client_project_and_preserves_human_context(service, monkeypatch):
    from tests.test_primary_ownership import Provider, forbid_legacy
    from evolvmem.context_models import ContextMode
    service.config.knowledge_structure_mode = 'primary'
    monkeypatch.setattr(service, 'capability_index', None)
    value = archive(service, project='evo', text='dsh 增加写入校验。')
    item = service.knowledge().create({'title': '人工确认的业务规则', 'body': 'dsh 增加写入校验。',
                                       'project': 'evo', 'action': 'publish'})
    conn = service.store._connection()
    with service.store.transaction():
        conn.execute('INSERT INTO context_sources(item_id,archive_id,source_kind,source_ref,extraction_version,created_at) VALUES(?,?,?,?,?,?)',
                     (item['id'], value.id, 'archive', f'archive:{value.id}', 'legacy', 'now'))
    def protected():
        return [[dict(r) for r in conn.execute(sql, (item['id'],))] for sql in (
            'SELECT * FROM context_items WHERE id=?',
            'SELECT * FROM context_layers WHERE item_id=? ORDER BY layer',
            'SELECT * FROM context_project_resolutions WHERE item_id=?',
            'SELECT * FROM context_sources WHERE item_id=? ORDER BY id')]
    before = protected()
    assert before[2][0]['decision_source'] == 'human'
    grant(service, value)
    task = conversation_tasks.enqueue(service, [f'archive:{value.id}'])['items'][0]
    worker = org.OrganizationWorker(service.config, mode=ContextMode.SHADOW)
    row = org._task_row(service, task['id']); text = row['source_snapshot']
    worker._store_units(service, row, [{'source_start': 0, 'source_end': len(text), 'text': text,
        'title': 'dsh 增加写入校验。', 'category': 'task_requirement', 'project_hint': '',
        'evidence_quote': 'dsh 增加写入校验。'}])
    worker._touch(service, task['id'], 'assignment', 'running')
    provider = Provider(service)
    monkeypatch.setattr(org, '_load_llm', lambda: ({}, provider))
    forbid_legacy(monkeypatch, worker)
    worker._process(service, org.task_view(service, task['id']))
    current = org.unit_views(service, task['id'])[0]
    assert (current['decision'], current['project']) == ('auto', 'dsh')
    assert protected() == before
    assert conn.execute('SELECT project FROM session_archives WHERE id=?', (value.id,)).fetchone()[0] == 'evo'
    assert org.task_view(service, task['id'])['status'] == 'completed'


def test_enrollment_plan_rejects_linked_manual_context_changes(service):
    value = archive(service)
    item = service.knowledge().create({'title': '人工规则', 'body': '人工归属保持', 'project': 'evo'})
    conn = service.store._connection()
    with service.store.transaction():
        conn.execute('INSERT INTO context_sources(item_id,archive_id,source_kind,source_ref,extraction_version,created_at) VALUES(?,?,?,?,?,?)',
                     (item['id'], value.id, 'archive', f'archive:{value.id}', 'legacy', 'now'))
    prepared = api().build_plan(conn)
    with service.store.transaction():
        conn.execute('UPDATE context_project_resolutions SET revision=revision+1 WHERE item_id=?', (item['id'],))
    with pytest.raises(ValueError, match='revision_conflict'):
        api().apply_plan(service, prepared)
    assert api().valid_ids(service) == set()


def test_replay_never_claims_complete_after_grant_or_origin_changes(service):
    value = archive(service); prepared = grant(service, value)
    with service.store.transaction():
        service.store._connection().execute("UPDATE session_archives SET project='dsh' WHERE id=?", (value.id,))
    with pytest.raises(ValueError, match='revision_conflict'):
        api().apply_plan(service, prepared)


def test_pause_during_arrival_selection_prevents_enrollment_and_enqueue(service, monkeypatch):
    organization_arrival.update_settings(service, {'auto_new': True})
    value = archive(service)
    original = organization_arrival._pending_sources
    def pause(current):
        result = original(current)
        organization_arrival.update_settings(current, {'auto_new': False})
        return result
    monkeypatch.setattr(organization_arrival, '_pending_sources', pause)
    with pytest.raises(ValueError, match='settings_revision_conflict'):
        organization_arrival.discover_new(service)
    assert api().valid_ids(service) == set()
    assert not service.store._connection().execute('SELECT 1 FROM organization_tasks').fetchone()


def test_prepare_hashes_large_derived_text_in_bounded_chunks(service):
    value = archive(service)
    conn = service.store._connection()
    with service.store.transaction():
        conn.execute('UPDATE conversation_history SET body=? WHERE archive_id=?', ('正文' * 600000, value.id))
    statements = []
    conn.set_trace_callback(statements.append)
    try:
        api().build_plan(conn)
    finally:
        conn.set_trace_callback(None)
    assert not any('SELECT * FROM conversation_history' in sql for sql in statements)
    assert any('substr(body,' in sql and '65536' in sql for sql in statements)


def native_assigned(service, start, end, text):
    from tests.test_cross_batch_context import craft, SESSION
    value = craft(service, session=SESSION, start=start, end=end, text=text)
    with service.store.transaction():
        service.store._connection().execute("UPDATE session_archives SET project='evo' WHERE id=?", (value.id,))
    return value


def test_native_assigned_batches_group_in_source_order_and_skip_unenrolled_sibling(service):
    first = native_assigned(service, 1, 3, '原始需求：必须保留失败原因。')
    second = native_assigned(service, 4, 6, '继续实现：失败时保存具体原因。')
    third = native_assigned(service, 7, 9, '未授权的旧来源不应被借入。')
    grant(service, first); grant(service, second)
    task = conversation_tasks.enqueue(service, [f'archive:{second.id}'], expand=True)['items'][0]
    assert conversation_tasks.members(service, task['source_key']) == [f'archive:{first.id}', f'archive:{second.id}']
    assert third.id not in api().valid_ids(service)
    assert conversation_tasks.enqueue(service, [f'archive:{first.id}'], expand=True)['created'] == 0
    text = org._task_row(service, task['id'])['source_snapshot']
    assert text.index('原始需求') < text.index('继续实现')


def test_enrolled_review_unit_remains_visible_in_review_ui(service):
    from evolvmem import organization_review
    value = archive(service, text='归属尚待核实的原文。'); grant(service, value)
    task = conversation_tasks.enqueue(service, [f'archive:{value.id}'])['items'][0]
    row = org._task_row(service, task['id']); text = row['source_snapshot']
    org.OrganizationWorker(service.config)._store_units(service, row, [{
        'source_start': 0, 'source_end': len(text), 'text': text,
        'title': '待核实原文', 'category': 'reference', 'project_hint': '', 'evidence_quote': ''}])
    with service.store.transaction():
        service.store._connection().execute("UPDATE organization_tasks SET status='review',stage='done' WHERE id=?", (task['id'],))
    assert org.task_view(service, task['id'])['source_current']
    assert organization_review.groups(service, {'task_id': task['id']})['unit_count'] == 1


def test_revoking_grant_keeps_historical_task_but_removes_current_provenance(service):
    value = archive(service); prepared = grant(service, value)
    task = conversation_tasks.enqueue(service, [f'archive:{value.id}'])['items'][0]
    before = org._task_row(service, task['id'])
    api().rollback_plan(service, prepared)
    assert org._task_row(service, task['id']) == before
    assert not org.task_view(service, task['id'])['source_current']
    assert ProvenanceRead(service.store).source_revision(task['source_key'])[1] == 'archive_not_current'

"""Grouped review is a previewed decision over current sources, never a guessed project."""
import json
import pytest
from tests.test_auto_organization import service, base_service, org
from tests.unit_model_fixture import model_for, run_worker
from evolvmem.session_archive import SessionArchiver


def group_source(service, monkeypatch, *, session='group-demo', count=12):
    messages = [{'role':'user', 'content':f'条目{i:02d}：导出行必须保留原始编号。' +
                 ('其他平台需要单独判断。' if i >= count-2 else '')} for i in range(count)]
    source = SessionArchiver(service.config,service.store).archive_session(
        '', 'kimi', session, json.dumps({'messages':messages},ensure_ascii=False))
    tid = org(service,'/tasks',{'items':[{'key':f'archive:{source.id}'}]})['items'][0]['id']
    model = model_for(service,topics=[(f'条目{i:02d}','','task_requirement') for i in range(count)])
    run_worker(service,monkeypatch,model)
    assert len(org(service,'/units',{'task_id':tid})['items'])==count
    return tid, source


def proposal(service, group_id):
    return org(service,'/groups/preview',{'group_id':group_id,'project':'evo',
        'condition':'导出行','exceptions':'其他平台','scope':'future',
        'guidance':'导出行规范归 Evo，其他平台除外'})


def test_preview_ten_matches_two_exceptions_then_single_guidance(service,monkeypatch):
    tid,source=group_source(service,monkeypatch)
    before=service.store._connection().execute('select count(*) from organization_guidance').fetchone()[0]
    groups=org(service,'/groups')['items']
    assert len(groups)==12 and all(g['count']==1 and g['complete_task'] for g in groups)
    p=proposal(service,groups[0]['id'])
    assert len(p['eligible'])==10 and len(p['excluded'])==2
    assert all(x['reason']=='命中例外：其他平台' for x in p['excluded'])
    assert service.store._connection().execute('select count(*) from organization_guidance').fetchone()[0]==before
    r=org(service,'/groups/apply',{'proposal':p})
    assert r['succeeded']==10 and r['failed']==0
    assert service.store._connection().execute('select count(*) from organization_guidance').fetchone()[0]==before+1
    units=org(service,'/units',{'task_id':tid})['items']
    assert sum(u['decision']=='manual' and u['project']=='evo' for u in units)==10
    assert sum(u['decision']=='review' and not u['project'] for u in units)==2
    # Saving the ten starts extraction, but the two untouched review units
    # must stay visible while their sibling units are being extracted.
    remaining = org(service, '/groups')['items']
    assert len(remaining) == 2 and all(g['count']==1 for g in remaining)
    with pytest.raises(ValueError,match='review_preview_changed'):
        org(service,'/groups/apply',{'proposal':p})
    assert service.store._connection().execute('select count(*) from organization_guidance').fetchone()[0]==before+1


def test_preview_changes_refuse_all_writes(service,monkeypatch):
    tid,_=group_source(service,monkeypatch)
    p=proposal(service,org(service,'/groups')['items'][0]['id'])
    with service.store.transaction():
        service.store._connection().execute('update organization_units set revision=revision+1 where task_id=? and ordinal=0',(tid,))
    with pytest.raises(ValueError,match='review_preview_changed'):
        org(service,'/groups/apply',{'proposal':p})
    assert not any(u['decision']=='manual' for u in org(service,'/units',{'task_id':tid})['items'])


def test_preview_tampering_cannot_expand_members_or_change_scope(service,monkeypatch):
    group_source(service,monkeypatch)
    p=proposal(service,org(service,'/groups')['items'][0]['id'])
    p['exceptions']=''
    with pytest.raises(ValueError,match='review_preview_changed'):
        org(service,'/groups/apply',{'proposal':p})
    assert service.store._connection().execute('select count(*) from organization_guidance').fetchone()[0]==0


def test_groups_keep_sessions_separate_and_manual_rows_out(service,monkeypatch):
    tid,_=group_source(service,monkeypatch,session='first',count=4)
    group_source(service,monkeypatch,session='second',count=4)
    groups=org(service,'/groups')['items']
    assert len(groups)==8 and all(g['count']==1 for g in groups)
    assert len({g['id'] for g in groups})==8
    u=org(service,'/units',{'task_id':tid})['items'][0]
    org(service,'/correct',{'task_id':tid,'digest':u['digest'],'expected_revision':u['revision'],'project':'evo'})
    groups=org(service,'/groups')['items']
    assert all(not any(x['digest']==u['digest'] for x in g['examples']) for g in groups)


def test_narrow_condition_and_explicit_preview_required(service,monkeypatch):
    group_source(service,monkeypatch)
    gid=org(service,'/groups')['items'][0]['id']
    with pytest.raises(ValueError,match='review_condition_required'):
        org(service,'/groups/preview',{'group_id':gid,'project':'evo','condition':'资料'})
    with pytest.raises(ValueError,match='review_preview_required'):
        org(service,'/groups/apply',{'group_id':gid,'project':'evo'})


def test_rules_change_invalidates_preview(service,monkeypatch):
    group_source(service,monkeypatch)
    p=proposal(service,org(service,'/groups')['items'][0]['id'])
    rules=service.knowledge().rules.read()
    service.knowledge().rules.save({'expected_revision':rules['revision'],'instructions':rules['instructions']+'\n新的核对条件。'})
    with pytest.raises(ValueError,match='review_preview_changed'):
        org(service,'/groups/apply',{'proposal':p})


def test_single_item_review_needs_no_phrase_and_changes_only_selected_unit(service, monkeypatch):
    tid, _ = group_source(service, monkeypatch, count=4)
    unit = org(service, '/units', {'task_id': tid})['items'][2]
    group = org(service, '/groups/detail', {'task_id':tid,'digest':unit['digest']})
    p = org(service, '/groups/preview', {'group_id': group['id'], 'project': 'evo',
        'selection': 'item', 'task_id': tid, 'digest': unit['digest']})
    assert p['searched_count'] == 1
    assert [u['digest'] for u in p['eligible']] == [unit['digest']]
    assert org(service, '/groups/apply', {'proposal': p})['succeeded'] == 1
    assert sum(u['decision'] == 'manual' for u in org(service, '/units', {'task_id': tid})['items']) == 1


def test_all_pending_scope_is_explicit_and_previewed_across_sessions(service, monkeypatch):
    group_source(service, monkeypatch, session='scope-first', count=4)
    group_source(service, monkeypatch, session='scope-second', count=4)
    gid = org(service, '/groups')['items'][0]['id']
    p = org(service, '/groups/preview', {'group_id': gid, 'project': 'evo',
        'selection': 'all', 'condition': '导出行', 'exceptions': '其他平台'})
    assert p['searched_count'] == 8 and p['matched_count'] == 8
    assert len(p['eligible']) == len(p['excluded']) == 4
    assert org(service, '/groups/apply', {'proposal': p})['succeeded'] == 4


def test_group_detail_returns_every_untruncated_original_fragment(service, monkeypatch):
    tid, _ = group_source(service, monkeypatch, count=5)
    gid = org(service, '/groups')['items'][0]['id']
    detail = org(service, '/groups/detail', {'group_id': gid})
    originals = org(service, '/units', {'task_id': tid})['items']
    assert len(detail['items']) == 1
    assert detail['items'][0]['text'] == originals[0]['text']
    assert detail['items'][0]['task_record']['version']


def test_stale_preview_can_reprocess_only_matching_sources_then_be_saved(service, monkeypatch):
    tid, _ = group_source(service, monkeypatch, session='stale-first', count=4)
    other, _ = group_source(service, monkeypatch, session='stale-other', count=4)
    gid = org(service, '/groups')['items'][0]['id']
    rules = service.knowledge().rules.read()
    service.knowledge().rules.save({'expected_revision': rules['revision'],
        'instructions': rules['instructions'] + '\n按新规则核对。'})
    p = proposal(service, gid)
    assert not p['eligible']
    assert sum(u['reason_code'] == 'rule_changed' for u in p['excluded']) == 2
    r = org(service, '/groups/reprocess', {'proposal': p})
    assert r['created'] == 1 and len(r['items']) == 1
    assert org(service, '/detail', {'task_id': other})['status'] == 'review'
    run_worker(service, monkeypatch, model_for(service, topics=[
        (f'条目{i:02d}', '', 'task_requirement') for i in range(4)]))
    refreshed = proposal(service, gid)
    assert len(refreshed['eligible']) == 2
    assert org(service, '/groups/apply', {'proposal': refreshed})['succeeded'] == 2


def test_reprocess_rejects_modified_preview_without_queuing(service, monkeypatch):
    group_source(service, monkeypatch, count=4)
    gid = org(service, '/groups')['items'][0]['id']
    p = proposal(service, gid)
    p['condition'] = '别的条件'
    before = org(service)['total']
    with pytest.raises(ValueError, match='review_preview_changed'):
        org(service, '/groups/reprocess', {'proposal': p})
    assert org(service)['total'] == before


def test_rule_reprocess_preserves_other_human_project_decisions(service, monkeypatch):
    tid, _ = group_source(service, monkeypatch, count=4)
    first = org(service, '/units', {'task_id': tid})['items'][0]
    org(service, '/correct', {'task_id': tid, 'digest': first['digest'],
        'expected_revision': first['revision'], 'project': 'shop'})
    gid = org(service, '/groups')['items'][0]['id']
    rules = service.knowledge().rules.read()
    service.knowledge().rules.save({'expected_revision': rules['revision'],
        'instructions': rules['instructions'] + '\n核对新版规则。'})
    result = org(service, '/groups/reprocess', {'proposal': proposal(service, gid)})
    run_worker(service, monkeypatch, model_for(service, topics=[
        (f'条目{i:02d}', '', 'task_requirement') for i in range(4)]))
    units = org(service, '/units', {'task_id': result['items'][0]['id']})['items']
    retained = next(u for u in units if u['digest'] == first['digest'])
    assert retained['decision'] == 'manual' and retained['project'] == 'shop'


@pytest.mark.parametrize('by_member', [False, True])
def test_open_detail_checks_only_its_source_and_rechecks_changes(service, monkeypatch, by_member):
    from evolvmem import auto_organization, history_organization
    tid, source = group_source(service, monkeypatch, session='selected', count=2)
    group_source(service, monkeypatch, session='unrelated', count=2)
    group = org(service, '/groups')['items'][0]
    query = {'task_id': tid, 'digest': group['examples'][0]['digest']} if by_member else {'group_id': group['id']}
    checked, exclusion_reads = [], []
    original, exclusions = auto_organization._source_current, history_organization._excluded_archive_ids
    def measured(service, task, **kwargs):
        checked.append(task['id'])
        return original(service, task, **kwargs)
    def measured_exclusions(service):
        exclusion_reads.append(True)
        return exclusions(service)
    monkeypatch.setattr(auto_organization, '_source_current', measured)
    monkeypatch.setattr(history_organization, '_excluded_archive_ids', measured_exclusions)
    assert org(service, '/groups/detail', query)['id'] == group['id']
    assert checked == [tid], 'opening one task must not read unrelated conversation bodies'
    assert len(exclusion_reads) == 1, 'source exclusions are shared only within this read'
    with service.store.transaction():
        service.store._connection().execute("UPDATE conversation_history SET body=body || '新增需求' WHERE archive_id=?", (source.id,))
    with pytest.raises(ValueError, match='review_preview_changed'):
        org(service, '/groups/detail', query)


def test_group_list_reads_exclusions_once_per_request(service, monkeypatch):
    from evolvmem import history_organization
    group_source(service, monkeypatch, session='one', count=2)
    group_source(service, monkeypatch, session='two', count=2)
    calls=[]
    original=history_organization._excluded_archive_ids
    def measured(service):
        calls.append(True)
        return original(service)
    monkeypatch.setattr(history_organization, '_excluded_archive_ids', measured)
    assert org(service, '/groups')['total']==4
    assert len(calls)==1
    assert org(service, '/groups')['total']==4
    assert len(calls)==2, 'each request must read current exclusions again'


def test_detail_rechecks_new_source_exclusion(service, monkeypatch):
    _, source = group_source(service, monkeypatch, count=2)
    query = {'group_id': org(service, '/groups')['items'][0]['id']}
    assert org(service, '/groups/detail', query)['items']
    with service.store.transaction():
        service.store._connection().execute('INSERT INTO archive_source_exclusions '
            '(archive_id,payload_sha256,reason,verified_at) SELECT id,payload_sha256,?,? '
            'FROM session_archives WHERE id=?', ('system-source', '2026-10-14 00:00:00', source.id))
    with pytest.raises(ValueError, match='review_preview_changed'):
        org(service, '/groups/detail', query)


def test_member_lookup_never_substitutes_a_current_sibling_for_a_stale_member(service, monkeypatch):
    from evolvmem import auto_organization
    tid, _ = group_source(service, monkeypatch, session='older', count=2)
    other, _ = group_source(service, monkeypatch, session='newer', count=2)
    conn = service.store._connection()
    with service.store.transaction():
        conn.execute('UPDATE organization_tasks SET source_key=(SELECT source_key FROM organization_tasks WHERE id=?) WHERE id=?', (tid, other))
        conn.execute("UPDATE organization_units SET task_record='{}' WHERE task_id IN (?,?)", (tid, other))
    monkeypatch.setattr(auto_organization, '_source_current', lambda _service, task, **_: task['id'] == other)
    group = org(service, '/groups')['items'][0]
    stale = dict(conn.execute('SELECT task_id,digest FROM organization_units WHERE task_id=? LIMIT 1', (tid,)).fetchone())
    assert all(u['task_id'] == other for u in org(service, '/groups/detail', {'group_id': group['id']})['items'])
    with pytest.raises(ValueError, match='review_preview_changed'):
        org(service, '/groups/detail', stale)

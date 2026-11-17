"""Existing pending material must reach the default complete-task pipeline."""
from tests.test_cross_batch_context import config, service, craft, SESSION
from tests.test_complete_task_pipeline import legacy_unit
from evolvmem import auto_organization as org


def test_default_submit_uses_complete_conversation(service):
    a=craft(service,session=SESSION,start=1,end=4,text='增加写入开关。')
    b=craft(service,session=SESSION,start=5,end=8,text='修复同一开关的更新失败。')
    task=org.dispatch(service,'POST','/tasks',{'items':[{'key':f'archive:{b.id}'}]})['items'][0]
    assert task['source_key'].startswith('conversation:')
    assert '增加写入开关' in org.task_detail(service,task['id'])['source_text']


def test_existing_review_is_upgraded_without_click_or_arrival_toggle(service):
    from evolvmem import organization_task_upgrade as upgrade
    old,unit=legacy_unit(service)
    with service.store.transaction():
        service.store._connection().execute("UPDATE organization_tasks SET status='review',stage='done' WHERE id=?",(old['id'],))
    result=upgrade.advance(service)
    assert result['created']==1
    after=org.task_view(service,old['id'])
    assert after['status']=='superseded'
    new=org.task_view(service,after['superseded_by'])
    assert new['source_key'].startswith('conversation:')
    assert upgrade.advance(service)['created']==0
    assert upgrade.status(service)['processing']==1
    assert org.find_unit(service,old['id'],unit['digest'])['text']==unit['text']


def test_upgrade_skips_settled_manual_sources_and_keeps_failed_source_visible(service):
    from evolvmem import organization_task_upgrade as upgrade
    a,_=legacy_unit(service)
    with service.store.transaction():
        service.store._connection().execute("UPDATE organization_tasks SET status='completed',stage='done' WHERE id=?",(a['id'],))
        service.store._connection().execute("UPDATE organization_units SET decision='manual' WHERE task_id=?",(a['id'],))
    b=craft(service,session='broken',start=1,end=3,text='新的需求。')
    bad=org.enqueue(service,[f'archive:{b.id}'])['items'][0]
    with service.store.transaction():
        service.store._connection().execute("UPDATE session_archives SET state='expired' WHERE id=?",(b.id,))
    upgrade.advance(service)
    assert org.task_view(service,a['id'])['status']=='completed'
    assert upgrade.status(service)['failed']==1
    assert upgrade.advance(service)['created']==0


def test_retry_of_legacy_task_also_uses_complete_task(service):
    old,_=legacy_unit(service)
    task=org.dispatch(service,'POST','/resegment',{'task_id':old['id']})['task']
    assert task['source_key'].startswith('conversation:')


def test_obsolete_whole_snapshot_conversation_retires_without_model_retry(service):
    from evolvmem.session_archive import SessionArchiver
    from tests.test_source_eligibility_repair import windows_external,payload_for
    from evolvmem import conversation_tasks
    archiver=SessionArchiver(service.config,service.store)
    identity=['test-device','test-session']
    old=archiver.archive_session('','codex',windows_external(identity,'1'*64),payload_for('最初要求保留编号。'))
    task=conversation_tasks.enqueue(service,[f'archive:{old.id}'])['items'][0]
    with service.store.transaction():
        service.store._connection().execute("UPDATE organization_tasks SET status='failed',error_code='classification_source_changed' WHERE id=?",(task['id'],))
    new=archiver.archive_session('','codex',windows_external(identity,'2'*64),payload_for('最初要求保留编号。现在完成修改。'))
    current=conversation_tasks.enqueue(service,[f'archive:{new.id}'])['items'][0]
    org.reconcile_sources(service)
    retired=org.task_view(service,task['id'])
    assert retired['status']=='superseded' and retired['superseded_by']==current['id']
    assert retired['attempts']==0
    assert org.task_view(service,current['id'])['status']=='pending'

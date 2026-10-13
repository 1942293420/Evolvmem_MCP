"""Conversation batches are evidence sources, not business-task boundaries."""
import json
import pytest
from tests.test_cross_batch_context import config, service, craft, SESSION, OTHER_SESSION


def engine():
    try:
        from evolvmem import conversation_tasks
    except ImportError:
        pytest.fail('complete-conversation organization is not implemented')
    return conversation_tasks


def test_same_session_batches_share_one_source_without_project_decisions(service):
    first=craft(service,session=SESSION,start=1,end=3,text='需求：增加执行器写入设置。')
    second=craft(service,session=SESSION,start=4,end=6,text='更新时报错，继续修复配置读取。')
    other=craft(service,session=OTHER_SESSION,start=1,end=3,text='另一会话的独立需求。')
    api=engine()
    keys=api.register(service,[f'archive:{first.id}',f'archive:{second.id}',f'archive:{other.id}'])
    assert len(keys)==2
    row=api.snapshot(service,keys[0])
    assert '增加执行器写入设置' in row['body'] and '继续修复配置读取' in row['body']
    assert '另一会话' not in row['body']
    assert len(row['records'])==2 and row['messages']
    assert api.register(service,[f'archive:{second.id}'])==[keys[0]]


def test_appending_source_invalidates_snapshot_but_keeps_conversation_identity(service):
    api=engine();a=craft(service,session=SESSION,start=1,end=3,text='需求：增加开关。')
    key=api.register(service,[f'archive:{a.id}'])[0]
    before=api.snapshot(service,key)
    b=craft(service,session=SESSION,start=4,end=6,text='结果：开关可以保存。')
    assert api.register(service,[f'archive:{b.id}'])==[key]
    after=api.snapshot(service,key)
    assert after['expected_revision']!=before['expected_revision']
    assert all(t in after['body'] for t in ('增加开关','开关可以保存'))


def test_missing_member_never_silently_shortens_conversation(service):
    api=engine();a=craft(service,session=SESSION,start=1,end=3,text='需求：增加开关。')
    key=api.register(service,[f'archive:{a.id}'])[0]
    with service.store.transaction():
        service.store._connection().execute("UPDATE session_archives SET state='expired' WHERE id=?",(a.id,))
    with pytest.raises(ValueError):api.snapshot(service,key)


def test_overlapping_batches_are_not_concatenated_as_distinct_evidence(service):
    api=engine();a=craft(service,session=SESSION,start=1,end=8,text='需求与过程。')
    b=craft(service,session=SESSION,start=5,end=12,text='重叠过程与结果。')
    with pytest.raises(ValueError,match='conversation_overlap'):
        api.register(service,[f'archive:{a.id}',f'archive:{b.id}'])


def test_complete_task_enqueue_is_idempotent_and_preserves_archives(service):
    api=engine();a=craft(service,session=SESSION,start=1,end=3,text='需求：增加开关。')
    one=api.enqueue(service,[f'archive:{a.id}']);two=api.enqueue(service,[f'archive:{a.id}'])
    assert one['created']==1 and two['duplicates']==1
    assert one['items'][0]['id']==two['items'][0]['id']
    assert one['items'][0]['source_current']
    assert service.store.get_session_archive(a.id)['state']=='available'

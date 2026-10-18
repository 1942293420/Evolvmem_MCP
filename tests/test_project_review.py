"""A human confirms one project batch, without manufacturing verified knowledge."""
import pytest
from tests.test_knowledge_management import service
from evolvmem.knowledge_api import dispatch
from evolvmem.project_models import ProjectResolutionDecision


def preview(service):
    return dispatch(service,'GET','project-review',{'project':'eva'})


def draft(service, title='退款规则'):
    return service.knowledge().create({'title':title,'body':title+'，必须核对原始订单金额。','project':'eva','action':'draft'})


def test_project_batch_is_one_confirmation_and_preview_never_saves(service):
    kb=service.knowledge(); rows=[draft(service,str(n)+'退款规则') for n in range(24)]
    snapshot=preview(service)
    entries=[e for group in snapshot['groups'] for e in group['items']]
    assert len(entries)==24
    assert all(kb.detail(r['id'])['status']=='candidate' for r in rows)
    assert all(e['confirmed'] for e in entries),'previous human ownership is reused'
    for start in range(0,len(entries),20):
        result=dispatch(service,'POST','project-review/apply',{'source_project':'eva','project':'eva','action':'archive','items':entries[start:start+20]})
        assert result['failed']==0
    assert preview(service)['total']==0
    assert all(kb.detail(r['id'])['status']=='active' for r in rows)


def test_changed_and_conflicting_rows_are_not_silently_archived(service):
    kb=service.knowledge(); a,b=draft(service,'甲退款规则'),draft(service,'乙退款规则')
    entries=[e for group in preview(service)['groups'] for e in group['items']]
    kb.update(a['id'],{'expected_revision':a['revision'],'body':'用户新增要求：还需要核对实际到账金额。'})
    with service.store.transaction():
        kb.conn.execute("UPDATE context_project_resolutions SET review_state='rejected' WHERE item_id=?",(b['id'],))
    result=dispatch(service,'POST','project-review/apply',{'source_project':'eva','project':'eva','action':'archive','items':entries})
    assert result['succeeded']==0 and result['failed']==2
    assert all(kb.detail(r['id'])['status']=='candidate' for r in (a,b))
    assert any(e['id']==b['id'] for g in preview(service)['exceptions'] for e in g['items'])


def test_human_ownership_is_not_overwritten_by_batch_and_partial_failure_rolls_back(service, monkeypatch):
    kb=service.knowledge(); a=draft(service)
    entries=[e for group in preview(service)['groups'] for e in group['items']]
    result=dispatch(service,'POST','project-review/apply',{'source_project':'eva','project':'evolvmem','action':'archive','items':entries})
    assert result['failed']==1 and kb.detail(a['id'])['project']=='eva'
    # Missing provenance is a proposal, never auto-confirmed by preview.
    with service.store.transaction():kb.conn.execute('DELETE FROM context_project_resolutions WHERE item_id=?',(a['id'],))
    entries=[e for group in preview(service)['groups'] for e in group['items']]
    from evolvmem.knowledge import KnowledgeBase
    def rejected(*args,**kwargs):raise ValueError('qa_conflict_requires_confirmation')
    monkeypatch.setattr(KnowledgeBase,'transition',rejected)
    result=dispatch(service,'POST','project-review/apply',{'source_project':'eva','project':'eva','action':'archive','items':entries})
    assert result['failed']==1
    assert kb.detail(a['id'])['resolution'] is None,'failed publish must not leave half an ownership write'


def test_verified_experience_and_managed_tasks_are_separate_from_archive_choices(service):
    kb=service.knowledge(); row=draft(service)
    with service.store.transaction():kb.conn.execute("UPDATE context_items SET experience_payload='{}' WHERE id=?",(row['id'],))
    result=preview(service)
    assert result['groups']==[]
    assert result['exceptions'][0]['id']=='verification'
    assert result['exceptions'][0]['count']==1
    assert kb.detail(row['id'])['status']=='candidate'


def test_batch_defer_keeps_original_material_and_removes_it_from_pending(service):
    kb=service.knowledge(); row=draft(service)
    entries=[e for group in preview(service)['groups'] for e in group['items']]
    result=dispatch(service,'POST','project-review/apply',{
        'source_project':'eva','action':'defer','items':entries})
    assert result['succeeded']==1 and result['failed']==0
    saved=kb.detail(row['id'])
    assert saved['status']=='archived'
    assert saved['body']==row['body'] and saved['project']==row['project']
    assert preview(service)['total']==0

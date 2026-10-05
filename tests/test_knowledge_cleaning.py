import json
import pytest
from tests.test_history_qa_memory import service, archive
from evolvmem.knowledge_api import dispatch


def clean(service, route='', body=None):
    return dispatch(service, 'GET' if body is None else 'POST', 'cleaning'+route, body)


def ready(service):
    rows=clean(service)['items']
    if rows:
        return clean(service,'/save',{'items':[{**r,'cleaned_text':r['body'],'category':'reference'} for r in rows]})


def test_cleaning_is_required_before_project_organization(service):
    a=archive(service,project='')
    assert dispatch(service,'GET','history/organization')['total']==0
    row=clean(service)['items'][0]
    assert clean(service,'/save',{'items':[{**row,'cleaned_text':'用户确认的清洗稿：修改界面前明确验收条件。','category':'task_requirement'}]})['succeeded']==1
    assert clean(service)['total']==0
    prepared=dispatch(service,'GET','history/organization')['items'][0]
    assert '用户确认的清洗稿' in prepared['body']
    assert dispatch(service,'POST','history/organization/save',{'items':[{**prepared,'project':'evo'}]})['succeeded']==1
    text=dispatch(service,'GET',f'conversations/{a.id}',{'project':'evo'})
    assert '用户确认的清洗稿' in text['cleaning']['text']
    assert '修改界面前先明确验收条件' in text['text']


def test_preview_uses_saved_cleaning_rules_and_preserves_source(service,monkeypatch):
    from evolvmem import kimi_hooks
    from evolvmem.pipeline_skills import read,save
    archive(service,project='');row=clean(service)['items'][0]
    skill=read(service,'cleaning');save(service,'cleaning',{'expected_revision':skill['revision'],'instructions':'保留否定、条件和纠正，独立整理清洗稿。'})
    prompts=[]
    def model(prompt,*a,**kw):
        prompts.append(prompt)
        return json.dumps({'cleaned_text':'修改界面前明确验收条件。','category':'task_requirement','reason':'保留业务要求。'})
    monkeypatch.setattr(kimi_hooks,'_load_llm_config',lambda:object())
    monkeypatch.setattr(kimi_hooks,'_call_llm_with_retry',model)
    result=clean(service,'/preview',{'items':[row]})
    assert result['items'][0]['cleaned_text']=='修改界面前明确验收条件。'
    assert '保留否定、条件和纠正' in prompts[0]
    assert result['persisted']==0 and clean(service)['total']==1
    assert dispatch(service,'GET','history/organization')['total']==0


def test_batch_conflicts_keep_other_rows_and_manual_category(service):
    kb=service.knowledge()
    a=kb.create({'title':'清洗 A','body':'原始需求需要按产品范围分类。','action':'draft'})
    b=kb.create({'title':'清洗 B','body':'原始资料保留新的业务限制。','action':'draft'})
    rows=clean(service)['items']
    kb.update(a['id'],{'expected_revision':a['revision'],'body':'用户刚刚修改了这条需求，请重新核对。'})
    result=clean(service,'/save',{'items':[{**r,'cleaned_text':'人工修正后的内容，保留明确业务限制。','category':'task_requirement'} for r in rows]})
    assert (result['succeeded'],result['failed'])==(1,1)
    assert clean(service)['total']==1
    prepared=dispatch(service,'GET','history/organization')['items'][0]
    dispatch(service,'POST','history/organization/save',{'items':[{**prepared,'project':'evo'}]})
    saved=kb.detail(b['id'])
    assert saved['body']=='人工修正后的内容，保留明确业务限制。'
    assert saved['learning']['category']=='task_requirement'


def test_permanent_delete_requires_confirmation_and_removes_archive_content(service):
    a=archive(service,project='');row=clean(service)['items'][0]
    path=service.config.data_dir/a.payload_path
    with pytest.raises(ValueError,match='permanent_delete_confirmation_required'):
        clean(service,'/delete',{'items':[row]})
    assert path.exists()
    result=clean(service,'/delete',{'items':[row],'confirm_permanent':True})
    assert result['succeeded']==1 and not path.exists()
    assert not service.store._connection().execute('SELECT 1 FROM conversation_history WHERE archive_id=?',(a.id,)).fetchone()
    assert clean(service)['total']==0
    assert dispatch(service,'GET','history/organization')['total']==0


def test_delete_referenced_archive_is_blocked_and_other_item_is_deleted(service):
    a=archive(service,project='');kb=service.knowledge()
    item=kb.create({'title':'未归属资料','body':'这条独立资料可以从清洗列表永久删除。','action':'draft'})
    ref=kb.create({'title':'已入库依据','body':'已入库的内容需要引用这份原始会话。','project':'evo','action':'publish'})
    with service.store.transaction():
        service.store._connection().execute("INSERT INTO context_sources(item_id,archive_id,source_kind,source_ref,extraction_version,created_at) VALUES(?,?,'test','test','v1','2026-10-06')",(ref['id'],a.id))
    rows=clean(service)['items']
    result=clean(service,'/delete',{'items':rows,'confirm_permanent':True})
    assert (result['succeeded'],result['failed'])==(1,1)
    assert any(r.get('error')=='cleaning_source_referenced' for r in result['items'])
    assert (service.config.data_dir/a.payload_path).exists()
    with pytest.raises(ValueError,match='item_not_found'):kb.detail(item['id'])
    assert kb.detail(ref['id'])['status']=='active'


def test_long_preview_processes_every_segment_and_partial_failure_is_not_saved(service,monkeypatch):
    from evolvmem import kimi_hooks
    service.knowledge().create({'title':'长资料','body':'甲'*12000+'乙'*12000+'最后的否定要求不能丢失。','action':'draft'})
    row=clean(service)['items'][0];prompts=[]
    def model(prompt,*a,**kw):
        prompts.append(prompt)
        return json.dumps({'cleaned_text':f'清洗分段 {len(prompts)}','category':'reference'})
    monkeypatch.setattr(kimi_hooks,'_load_llm_config',lambda:object())
    monkeypatch.setattr(kimi_hooks,'_call_llm_with_retry',model)
    result=clean(service,'/preview',{'items':[row]})
    assert result['items'][0]['segments']==3
    assert '最后的否定要求不能丢失' in prompts[-1]
    assert all(f'清洗分段 {i}' in result['items'][0]['cleaned_text'] for i in (1,2,3))
    assert dispatch(service,'GET','history/organization')['total']==0
    monkeypatch.setattr(kimi_hooks,'_call_llm_with_retry',lambda *a,**k:'not JSON')
    assert clean(service,'/preview',{'items':[row]})['items'][0]['ok'] is False
    assert dispatch(service,'GET','history/organization')['total']==0


def test_cleaning_conditions_apply_to_original_message_roles(service,monkeypatch):
    from evolvmem import kimi_hooks
    from evolvmem.pipeline_skills import read,save
    from evolvmem.session_archive import SessionArchiver
    SessionArchiver(service.config,service.store).archive_session('','kimi','clean-noise',json.dumps({'messages':[
        {'role':'user','content':'固定提示\n保留重复的业务要求。'},
        {'role':'user','content':'保留重复的业务要求。'},
        {'role':'tool','content':'工具输出不能作为资料。'}]},ensure_ascii=False))
    skill=read(service,'cleaning');save(service,'cleaning',{'expected_revision':skill['revision'],'settings':{'cleaning_drop_lines':['固定提示'],'cleaning_collapse_duplicates':True}})
    prompts=[]
    def model(prompt,*a,**k):
        prompts.append(prompt);return json.dumps({'cleaned_text':'保留业务要求。','category':'reference'})
    monkeypatch.setattr(kimi_hooks,'_load_llm_config',lambda:object())
    monkeypatch.setattr(kimi_hooks,'_call_llm_with_retry',model)
    clean(service,'/preview',{'items':clean(service)['items']})
    assert '固定提示' not in prompts[0] and '工具输出不能' not in prompts[0]
    assert prompts[0].count('保留重复的业务要求。')==1


def test_delete_removes_all_unassigned_snapshots_and_cleaning_draft(service):
    from evolvmem.session_archive import SessionArchiver
    archiver=SessionArchiver(service.config,service.store)
    payload=json.dumps({'messages':[{'role':'user','content':'即将删除的测试会话及其快照。'}]})
    old=archiver.archive_session('','codex','delete-session:old',payload)
    latest=archiver.archive_session('','codex','delete-session:new',payload)
    ready(service)
    row=dispatch(service,'GET','history/organization')['items'][0]
    assert clean(service,'/delete',{'items':[row],'confirm_permanent':True})['succeeded']==1
    for a in (old,latest):assert not (service.config.data_dir/a.payload_path).exists()
    assert clean(service)['total']==dispatch(service,'GET','history/organization')['total']==0
    assert all(not r[0] and not r[1] for r in service.store._connection().execute('SELECT source_text,cleaned_text FROM knowledge_cleaning_reviews'))

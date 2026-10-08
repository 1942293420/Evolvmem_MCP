import pytest
from tests.test_cross_batch_context import (config,service,craft,SESSION,enqueue,units,org)
from tests.unit_model_fixture import model_for,run_worker
from evolvmem import organization_context as ctx

A='用户要求：demo 项目先明确验收条件。'
B='继续补充：批量删除还要支持撤销。'

def pair(service,monkeypatch,second=B):
    a=craft(service,session=SESSION,start=1,end=3,text=A)
    provider=model_for(service,projects={'demo','other','dsh'},topics=[(A,'','task_requirement'),(second,'','task_requirement',True)])
    ta=enqueue(service,a.id);run_worker(service,monkeypatch,provider)
    b=craft(service,session=SESSION,start=4,end=6,text=second)
    tb=enqueue(service,b.id);run_worker(service,monkeypatch,provider)
    assert units(service,tb)[0]['project']=='demo'
    return ta,tb,units(service,tb)[0]['context']

def test_future_archives_do_not_hide_nearest_predecessor(service):
    a=craft(service,session=SESSION,start=1,end=3,text=A)
    b=craft(service,session=SESSION,start=4,end=6,text=B)
    for i in range(25):craft(service,session=SESSION,start=7+i*3,end=9+i*3,text='后续批次 %d'%i)
    prior=ctx.preceding_batch(service,ctx.archive_info(service,f'archive:{b.id}'))
    assert prior and prior['archive_id']==a.id

def test_same_project_revision_change_invalidates_basis(service,monkeypatch):
    ta,tb,basis=pair(service,monkeypatch)
    assert ctx.validate(service,basis)
    with service.store.transaction():
        service.store._connection().execute('UPDATE organization_units SET revision=revision+1 WHERE task_id=?',(ta,))
    assert not ctx.validate(service,basis),'basis revision changed but stale basis accepted'

def test_withdrawn_archive_invalidates_dependent_without_model(service,monkeypatch):
    ta,tb,basis=pair(service,monkeypatch)
    with service.store.transaction():
        service.store._connection().execute("UPDATE session_archives SET state='missing' WHERE id=?",(basis['archive_id'],))
    ctx.invalidate_dependents(service)
    assert units(service,tb)[0]['decision']=='review'

def test_explicit_current_project_has_no_dependency(service,monkeypatch):
    ta,tb,basis=pair(service,monkeypatch,second='demo 项目新增要求：必须保留编号。')
    assert not basis,'an explicit current project must stand independently'

def test_unresolved_tail_is_a_barrier(service,monkeypatch):
    ta,tb,basis=pair(service,monkeypatch)
    conn=service.store._connection();a=dict(conn.execute('SELECT * FROM organization_units WHERE task_id=?',(ta,)).fetchone())
    a.update(digest='synthetic-unresolved-tail',ordinal=99,project='',decision='review',text='另一个未明确的项目')
    columns=list(a)
    with service.store.transaction():
        conn.execute('INSERT INTO organization_units('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',[a[k] for k in columns])
    assert not ctx.build_basis(service,org(service,'/detail',{'task_id':tb})), 'older resolved unit leaked past ambiguous tail'

def test_production_arrival_order_allows_continuations(service,monkeypatch):
    from evolvmem.organization_arrival import update_settings,discover_new
    update_settings(service,{'auto_new':True,'expected_revision':0})
    first=craft(service,session=SESSION,start=1,end=3,text=A)
    second=craft(service,session=SESSION,start=4,end=6,text=B)
    third=craft(service,session=SESSION,start=7,end=9,text='继续补充：导出后必须保留原始行顺序。')
    provider=model_for(service,projects={'demo','other','dsh'},topics=[(A,'','task_requirement'),(B,'','task_requirement',True),('继续补充：导出后必须保留原始行顺序。','','task_requirement',True)])
    discover_new(service)
    run_worker(service,monkeypatch,provider)
    got=list(service.store._connection().execute('SELECT project,decision FROM organization_units ORDER BY task_id'))
    assert len(got)==3 and all(r['project']=='demo' and r['decision']=='auto' for r in got),[dict(r) for r in got]

def test_correct_context_hint_is_accepted(service,monkeypatch):
    from evolvmem.auto_organization import OrganizationWorker
    ta,tb,basis=pair(service,monkeypatch)
    u=units(service,tb)[0]
    w=OrganizationWorker(service.config)
    result=w._decide(service,u,'demo',service.knowledge().rules.read(),{'demo','other','dsh'},basis)
    assert result[0]=='demo' and result[2]=='auto',result

def test_tool_alias_is_not_project(service,monkeypatch):
    from evolvmem.auto_organization import OrganizationWorker
    service.knowledge().save_project({'project':'execution-hub','display_name':'Kimi执行器','aliases':['Kimi']})
    ta,tb,basis=pair(service,monkeypatch)
    u=units(service,tb)[0];u.update(text='继续刚才的任务，我会使用 Kimi 执行修改。',evidence_quote='',continues_context=1)
    w=OrganizationWorker(service.config)
    result=w._decide(service,u,'',service.knowledge().rules.read(),{'demo','other','dsh','execution-hub'},basis)
    assert result[0]=='demo' and result[2]=='auto',result

def test_explicit_business_hint_ignores_execution_tool(service):
    from evolvmem.auto_organization import OrganizationWorker
    u={'text':'demo 项目要求保留编号，使用 DSH 执行修改。','evidence_quote':'demo 项目要求保留编号','continues_context':0}
    result=OrganizationWorker(service.config)._decide(service,u,'demo',service.knowledge().rules.read(),{'demo','other','dsh'},{})
    assert result[0]=='demo' and result[2]=='auto',result

def test_tool_hint_cannot_override_explicit_project_switch(service,monkeypatch):
    from evolvmem.auto_organization import OrganizationWorker
    ta,tb,basis=pair(service,monkeypatch)
    u=units(service,tb)[0];u.update(text='现在切换到 other 项目，使用 DSH 执行编号修复。',evidence_quote='',continues_context=1)
    result=OrganizationWorker(service.config)._decide(service,u,'dsh',service.knowledge().rules.read(),{'demo','other','dsh'},basis)
    assert result[0]!='demo' or result[2]=='review',result

def test_business_with_tool_can_supply_next_context(service,monkeypatch):
    text='demo 项目要求保留编号，使用 DSH 执行修改。'
    a=craft(service,session=SESSION,start=1,end=3,text=text)
    provider=model_for(service,projects={'demo','other','dsh'},topics=[(text,'','task_requirement'),(B,'','task_requirement',True)])
    ta=enqueue(service,a.id);run_worker(service,monkeypatch,provider)
    assert units(service,ta)[0]['project']=='demo'
    b=craft(service,session=SESSION,start=4,end=6,text=B);tb=enqueue(service,b.id);run_worker(service,monkeypatch,provider)
    assert units(service,tb)[0]['project']=='demo', units(service,tb)[0]['reason']

def test_reversed_queue_larger_than_claim_window_makes_progress(service):
    from evolvmem.auto_organization import OrganizationWorker,CLAIM_SCAN_LIMIT
    count=CLAIM_SCAN_LIMIT+2
    archives=[craft(service,session=SESSION,start=1+i*3,end=3+i*3,text=A if i==0 else B+'%d'%i) for i in range(count)]
    tids=[enqueue(service,a.id) for a in reversed(archives)]
    worker=OrganizationWorker(service.config)
    got=None
    for _ in range(3):
        got=worker._claim(service)
        if got is not None:break
    assert got is not None,'all first-window tasks wait for a predecessor outside the same window forever'
    assert got['source_key']=='archive:%d'%archives[0].id

def test_cleaned_predecessor_change_invalidates_saved_context(service,monkeypatch):
    from evolvmem.knowledge_api import dispatch
    ta,tb,basis=pair(service,monkeypatch)
    old=dispatch(service,'GET','cleaning/detail',{'key':basis['source_key']})
    dispatch(service,'POST','cleaning/save',{'items':[{'key':old['key'],'expected_revision':old['expected_revision'],'cleaned_text':'demo 项目：原要求撤回，原始行顺序限制已经取消。','category':'reference'}]})
    assert not ctx.validate(service,basis),'current cleaned source changed while cached task source_revision stayed the same'
    for _ in range(3):ctx.invalidate_dependents(service)
    assert units(service,tb)[0]['decision']=='review'

def test_new_basis_cannot_revalidate_old_units_against_new_cleaned_source(service,monkeypatch):
    from evolvmem.knowledge_api import dispatch
    ta,tb,basis=pair(service,monkeypatch)
    old=dispatch(service,'GET','cleaning/detail',{'key':basis['source_key']})
    dispatch(service,'POST','cleaning/save',{'items':[{'key':old['key'],'expected_revision':old['expected_revision'],'cleaned_text':'other 项目：现在讨论另一套导出规则。','category':'reference'}]})
    assert not ctx.build_basis(service,org(service,'/detail',{'task_id':tb})), 'new basis attached current source fingerprint to old project unit'

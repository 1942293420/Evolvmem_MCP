import sys, json, tempfile, os, shutil
from pathlib import Path
from http.server import HTTPServer
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from evolvmem.config import Config
from evolvmem.context_models import ContextMode
from tests.test_web_server import _make_service
from evolvmem.web_server import make_handler
import re
from evolvmem import kimi_hooks
HINTS={'Evo 演示项目':'evo','dsh-a':'dsh-a','DSH 备用项目':'dsh-b','其余内容':'','导出':'dsh-a','未归属事项':''}
PROJECTS={'evo','dsh-a','dsh-b'}
config=Config(data_dir=Path(tempfile.mkdtemp(prefix='evo-p1-browser-')))
s=_make_service(config,mode=ContextMode.SHADOW)
s.knowledge().save_project({'project':'evo','display_name':'Evo 演示项目'})
s.knowledge().save_project({'project':'old-evo','display_name':'旧版 Evo 演示项目'})
s.knowledge().save_project({'project':'dsh-a','display_name':'DSH'})
s.knowledge().save_project({'project':'dsh-b','display_name':'DSH 备用项目'})
with s.store.transaction():
    s.store._connection().execute("UPDATE context_project_registry SET status='archived' WHERE project='old-evo'")
from evolvmem.session_archive import SessionArchiver
from evolvmem.legacy_models import LegacyExtractionItem, LegacyExtractionRequest
archive=SessionArchiver(config,s.store).archive_session('evo','kimi','browser-conversation',json.dumps({'messages':[
 {'role':'user','content':'项目管理保留在原知识库，不另建页面。'},
 {'role':'tool','content':'HIDDEN_TOOL_LOG'},
 {'role':'assistant','content':'将按项目总记忆组织，并保留来源。'}]},ensure_ascii=False))
s.persist_legacy_extraction(LegacyExtractionRequest(summary=LegacyExtractionItem(key='project:evo:progress:log:browser',value='本次对话明确了知识库集中管理与项目记忆的组织方式。',attribute='fact'),candidates=(),source_session='browser-conversation'),source_archive_id=archive.id)
s.knowledge().create({'title':'管理约定','body':'项目管理保留在原知识库，不另建页面。','project':'evo','action':'publish','content_type':'reference'})
from tests.test_history_qa_memory import extracted_qa
extracted_qa(s)
from evolvmem.context_models import ContextItemDraft, ContextContentType, ContextLayers, ContextStatus
with s.store.transaction():
    rollup=s.store.create_item(ContextItemDraft(identity_key='project:evo:knowledge:current',
        content_type=ContextContentType.PROJECT_SUMMARY,
        layers=ContextLayers('知识库阶段汇总','项目摘要应归入项目历史。','保留会话来源和需求依据。','fixture'),
        project='evo',status=ContextStatus.ACTIVE))
    s.store._connection().execute("INSERT INTO context_project_rollups(project,current_context_id,status,covered_through,updated_at) VALUES ('evo',?,'ready','2026-10-04 00:00:00','2026-10-05 00:00:00')",(rollup.id,))
# Exercise real legacy migration from an encrypted archive.
with s.store.transaction():
    s.store._connection().execute('DELETE FROM conversation_history WHERE archive_id=?', (archive.id,))
from tests.test_memory_learning import memory
learning_source=memory(s, text='讨论方案时先列业务目标，再核对实现范围。')
s.learning().propose({'topic':'acceptance','instruction':'界面改动后核对实际操作入口。','trigger':'界面改动后','scope':'project','target':'evo','source_ids':[learning_source['id']]},origin='analysis')
# Synthetic unassigned history for the project-classification workflow.
for index in range(2):
    SessionArchiver(config,s.store).archive_session('', 'kimi', f'unassigned-demo-{index}', json.dumps({'messages':[
        {'role':'user','content':f'Evo 演示项目需要整理历史会话 {index}，先预览再确认归属。'}]},ensure_ascii=False))
s.knowledge().create({'title':'未归属演示资料','body':'这份资料需要归属 Evo 演示项目。','scope':'project','action':'draft'})
from tests.test_knowledge_cleaning import ready
ready(s)
# One synthetic candidate whose answer widened a real user quote: the stored
# review verdict must stay visible in the item detail panel.

_wide=s.knowledge().create({'title':'入口范围待确认','body':'退款、删除、导出均放在卡片。','project':'evo',
    'action':'draft','content_type':'reference'})
with s.store.transaction():
    s.learning().capture(_wide['id'],{'category':'project_convention','basis':'explicit',
        'quote':'设置入口在卡片本身，退款入口也在卡片上。','question':'这些入口放在哪里？',
        'answer':'退款、删除、导出均放在卡片。','trigger':'查看卡片入口时',
        'answer_support':{'verdict':'review','reason':'需独立核对答案是否超出用户原话范围：答案增加了用户未说的入口范围',
            'quote':'设置入口在卡片本身，退款入口也在卡片上。','asked':'退款、删除、导出均放在卡片。',
            'original_answer':'退款、删除、导出均放在卡片。','corrected':False,'failed':True,
            'question':'这些入口放在哪里？','digest':'fixture','source':'fixture'}},messages=[
            {'role':'user','content':'设置入口在卡片本身，退款入口也在卡片上。'}])

# One narrowed candidate: the corrected answer is one contiguous user fragment.
_narrow=s.knowledge().create({'title':'入口位置已收窄','body':'设置入口在卡片本身','project':'evo',
    'action':'draft','content_type':'reference'})
with s.store.transaction():
    s.learning().capture(_narrow['id'],{'category':'project_convention','basis':'explicit',
        'quote':'设置入口在卡片本身','question':'设置入口在哪里？','answer':'设置入口在卡片本身',
        'trigger':'查看入口时',
        'answer_support':{'verdict':'narrow','reason':'只有前半句能直接引用',
            'quote':'设置入口在卡片本身','asked':'设置入口和退款入口都在卡片上',
            'original_answer':'设置入口和退款入口都在卡片上','original_quote':'设置入口在卡片本身，退款入口也在卡片上。',
            'corrected':True,'question':'设置入口在哪里？','digest':'fixture','source':'fixture'}},messages=[
            {'role':'user','content':'设置入口在卡片本身，退款入口也在卡片上。'}])

# Only the external provider is substituted; HTTP, parsing and policy checks remain real.
def model(prompt, *a, **kw):
    if '你是资料清洗助手' in prompt:
        if '无用测试资料' in prompt.split('正文分段',1)[-1]:
            return json.dumps({'cleaned_text':'待核对的无用测试资料。','category':'reference','recommended_action':'delete','reason':'按清洗 Skill，本条没有完成事项或明确决定。'},ensure_ascii=False)
        return json.dumps({'cleaned_text':'用户要求先清洗资料，核对后再归入 Evo 演示项目。','category':'task_requirement','reason':'保留明确目标与处理顺序。'},ensure_ascii=False)
    if '整理分段助手' in prompt:
        # Whole-source segmentation over program-numbered records: the model
        # answers with record ids, never with a copy of the source text.
        records=[(int(pos),content) for pos,content in re.findall(r'^\[(\d+)\] [\w]+：(.*)$',prompt,re.M)]
        groups=[]
        for pos,content in records:
            hint=next((value for marker,value in HINTS.items() if marker in content),None)
            if hint is not None or not groups:
                groups.append({'start':pos,'end':pos,'hint':hint or '','title':content[:40],
                               'category':'task_requirement' if hint else 'reference'})
            else:
                groups[-1]['end']=pos
        units=[{'start_id':g['start'],'end_id':g['end'],'title':g['title'],
                'cleaned_summary':g['title'][:180],'category':g['category'],
                'project_hint':g['hint'] if g['hint'] in PROJECTS else '',
                'evidence_quote':g['title'][:20],'disposition':'keep','disposition_reason':'',
                # The model explicitly judges the cross-batch continuation from
                # the supplied same-session background.
                'continues_context':'继续刚才的报表整理' in g['title']}
               for g in groups]
        return json.dumps({'units':units},ensure_ascii=False)
    if '长期记忆提炼器' in prompt and '[user]:' in prompt:
        # Real extraction contract for one unit: concise QA grounded in the unit.
        lines=[content.strip() for role,content in re.findall(r'^\[(\w+)\]: (.*)$',prompt,re.M)
               if role in ('user','assistant')]
        source=(lines or ['这段资料需要整理。'])[0]
        value=source[:200]
        if len(value)<10:value=(value+'：这是一段需要整理的业务资料。')[:200]
        learning={'category':'task_requirement','basis':'explicit','quote':value,
                  'question':'这段对话明确了什么要求？','answer':value,'trigger':'',
                  'rationale':'提炼自本次对话','topic':'auto','instruction':'按对话内容执行'}
        return json.dumps({'memories':[{'key':'SESSION_SUMMARY','value':'本次整理了一个话题的对话内容，供项目历史核对。'},
            {'key':'project:x:request:auto','value':value,'confidence':.9,'attribute':'constraint','learning':learning}]},ensure_ascii=False)
    if '独立核对员' in prompt:
        # Independent answer-scope reviewer: one verdict per numbered candidate.
        candidates=re.findall(r'"编号": (\d+),\n\s+"引用": "([^"]*)"',prompt)
        out=[]
        for n,quote in candidates:
            if '卡片本身' in prompt and '设置入口在卡片本身' in quote and '退款入口也在卡片上' in quote:
                # Extractive narrowing example: only the first clause is quoted.
                out.append({'id':int(n),'verdict':'narrow','reason':'只有前半句能直接引用',
                            'quote':quote,'corrected_quote':'设置入口在卡片本身'})
                continue
            if '纠正：禁止上传' in prompt and '上传' in prompt:
                # A later user correction wins even when the answer matches the quote.
                out.append({'id':int(n),'verdict':'review','reason':'用户随后明确纠正：禁止上传任何资料',
                            'quote':quote})
            else:
                out.append({'id':int(n),'verdict':'supported','reason':'与本条用户原话一致','quote':quote})
        return json.dumps(out,ensure_ascii=False)
    if '你是项目历史分类助手' in prompt:
        samples=json.loads(prompt.split('待分类资料（仅正文片段，不代表完整会话）：\n',1)[1])
        return json.dumps({'items':[{'key':r['key'],'project':'evo','reason':'正文说明属于 Evo 演示项目。'} for r in samples]},ensure_ascii=False)
    if '"rules"' in prompt:
        return json.dumps({'rules':[]})
    if '[user]: 我想在经验里直接加项目，不要让我到处找。' in prompt:
        answer='在经验问答中提供项目创建入口，创建后自动选中。'
        return json.dumps({'memories':[{'key':'SESSION_SUMMARY','value':'用户要求在经验问答中新增项目。'},
            {'key':'project:evo:constraint:inline-project','value':answer,'confidence':.95,'attribute':'constraint',
             'learning':{'category':'task_requirement','basis':'explicit','quote':'我想在经验里直接加项目，不要让我到处找。',
                         'question':'如何在经验问答中新增项目？','answer':answer,
                         'normalization':{'requirement':answer,'acceptance':[],'questions':[]}}}]},ensure_ascii=False)
    text='以后，修改界面时先明确操作目标和验收条件。'
    return json.dumps({'memories':[{'key':'SESSION_SUMMARY','value':'Evo 正在完善界面调整时的协作流程。'},
        {'key':'project:evo:constraint:ui','value':text,'attribute':'constraint','confidence':.9,
         'learning':{'category':'project_convention','basis':'explicit','quote':text,'question':'修改界面时先明确什么？','answer':text,'instruction':text,'topic':'ui',
          'process':{'goal':{'quote':text},'understanding':{'quote':'我会新建一个独立管理页面。'},
          'correction':{'quote':'管理保留在原知识库，不另建页面。'}}}}]},ensure_ascii=False)
kimi_hooks._load_llm_config=lambda **kwargs: object()
kimi_hooks._call_llm_with_retry=model
kimi_hooks._llm_callable=lambda credentials:model
if os.environ.get('EVOLVMEM_CARD_FIXTURE') == '1':
    for slug, name, alias, total, pending, day in [
        ('cards-alpha','北辰 · 产品设计','设计稿',1,1,1),
        ('cards-beta','云帆 · 客户服务','售后',3,0,2),
        ('cards-gamma','星河 · 开发工具','工具箱',2,2,3),
    ]:
        s.knowledge().save_project({'project':slug,'display_name':name,'aliases':[alias]})
        for index in range(total):
            s.knowledge().create({'title':f'{name}资料 {index}',
                'body':f'{name}需要在项目历史中查看摘要、对话和资料。',
                'project':slug,'action':'draft' if index < pending else 'publish',
                'content_type':'reference'})
        stamp=f'2026-10-{day:02} 12:00:00'
        with s.store.transaction():
            s.store._connection().execute('UPDATE context_items SET updated_at=? WHERE project=?',(stamp,slug))
            s.store._connection().execute('UPDATE context_project_registry SET updated_at=? WHERE project=?',(stamp,slug))
server=HTTPServer(('127.0.0.1',int(os.environ.get('EVOLVMEM_TEST_PORT','39478'))),make_handler(s))
# A pending auto-organization task plus the bounded worker, both synthetic.
from evolvmem.auto_organization import OrganizationWorker
from evolvmem.knowledge_api import dispatch as knowledge_dispatch
from evolvmem.session_archive import SessionArchiver as _Archiver
_auto=[] if os.environ.get('EVOLVMEM_ORGANIZATION_FIXTURE')!='1' else [
    'Evo 演示项目：自动整理要先分段再归属。\n',
    '其余内容：这条没有明确项目线索，需要人工确认。\n',
    'dsh-a：导出必须保留来源版本。\n']
if _auto:
    _source=_Archiver(config,s.store).archive_session('','kimi','organization-demo',
        json.dumps({'messages':[{'role':'user','content':''.join(_auto)},
                                {'role':'assistant','content':'会按主题整理。'}]},ensure_ascii=False))
    _full=knowledge_dispatch(s,'GET','cleaning/detail',{'key':f'archive:{_source.id}'})
    knowledge_dispatch(s,'POST','cleaning/save',{'items':[{'key':_full['key'],
        'expected_revision':_full['expected_revision'],'cleaned_text':_full['body'],'category':'reference'}]})
    knowledge_dispatch(s,'POST','organization/tasks',{'items':[{'key':f'archive:{_source.id}'}]})
    # A second source with two units that both need a human decision.
    _review=_Archiver(config,s.store).archive_session('','kimi','organization-review-demo',
        json.dumps({'messages':[{'role':'user','content':'未归属事项一：没有项目线索。\n未归属事项二：同样需要人工确认。'},
                                {'role':'assistant','content':'会逐条等待确认。'}]},ensure_ascii=False))
    _review_full=knowledge_dispatch(s,'GET','cleaning/detail',{'key':f'archive:{_review.id}'})
    knowledge_dispatch(s,'POST','cleaning/save',{'items':[{'key':_review_full['key'],
        'expected_revision':_review_full['expected_revision'],'cleaned_text':_review_full['body'],'category':'reference'}]})
    knowledge_dispatch(s,'POST','organization/tasks',{'items':[{'key':f'archive:{_review.id}'}]})
    # Two real incremental batches of one Linux Codex session: the second keeps
    # working on the same project without repeating its name, so its unit must
    # show the same-session prior evidence it inherited from.
    import hashlib
    from evolvmem.local_codex_capture import batch_external_id
    _session='01a0fb28-893d-7250-9445-1a2c2fe6a0ab'
    def _incremental(start,end,text,answer):
        _stamp='2026-06-01T00:00:00.000Z'
        def _line(kind,payload):
            return json.dumps({'type':kind,'timestamp':_stamp,'payload':payload},
                              ensure_ascii=False,separators=(',',':'))+'\n'
        transcript=''.join([
            _line('session_meta',{'id':_session,'cwd':'/home/u/demo'}),
            _line('response_item',{'type':'message','role':'user','id':'evt-user',
                                   'content':[{'type':'input_text','text':text}]}),
            _line('response_item',{'type':'message','role':'assistant','id':'evt-ai',
                                   'content':[{'type':'output_text','text':answer}]})])
        digest=hashlib.sha256(transcript.encode()).hexdigest()
        payload=json.dumps({'conversation':[{'role':'user','content':text},
                                            {'role':'assistant','content':answer}],
            'transcript':transcript,'project':'','source_sha256':digest,
            'source':{'kind':'local_codex_jsonl','adapter':'codex','session_id':_session,
                      'file':f'/tmp/{_session}.jsonl','start_offset':0,
                      'end_offset':len(transcript.encode()),'start_line':start,
                      'end_line':end,'event_ids':[]},'line_locations':[]},ensure_ascii=False)
        return _Archiver(config,s.store).archive_session(
            '','codex',batch_external_id(_session,start,end,digest),payload)
    for _batch in (_incremental(1,3,'Evo 演示项目：报表导出必须保留原始编号。','已记录要求。'),
                   _incremental(4,6,'继续刚才的报表整理，已经完成日期格式的修改。','已按编号保留完成修改。')):
        _entry=knowledge_dispatch(s,'GET','cleaning/detail',{'key':f'archive:{_batch.id}'})
        knowledge_dispatch(s,'POST','cleaning/save',{'items':[{'key':_entry['key'],
            'expected_revision':_entry['expected_revision'],'cleaned_text':_entry['body'],'category':'reference'}]})
        knowledge_dispatch(s,'POST','organization/tasks',{'items':[{'key':f'archive:{_batch.id}'}]})
_worker=OrganizationWorker(config,mode=ContextMode.SHADOW).start() if _auto else None
print('Temporary browser fixture ready',flush=True)
try:server.serve_forever()
finally:
    server.server_close()
    if _worker:_worker.stop()
    s.close();shutil.rmtree(config.data_dir)

import sys, json, tempfile, os, shutil
from pathlib import Path
from http.server import HTTPServer
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from evolvmem.config import Config
from evolvmem.context_models import ContextMode
from tests.test_web_server import _make_service
from evolvmem.web_server import make_handler
from evolvmem import kimi_hooks
config=Config(data_dir=Path(tempfile.mkdtemp(prefix='evo-p1-browser-')))
s=_make_service(config,mode=ContextMode.SHADOW)
s.knowledge().save_project({'project':'evo','display_name':'Evo 演示项目'})
s.knowledge().save_project({'project':'old-evo','display_name':'Evo 演示项目'})
s.knowledge().save_project({'project':'dsh-a','display_name':'DSH'})
s.knowledge().save_project({'project':'dsh-b','display_name':'DSH'})
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
# Only the external provider is substituted; HTTP, parsing and policy checks remain real.
def model(prompt, *a, **kw):
    if '你是资料清洗助手' in prompt:
        return json.dumps({'cleaned_text':'用户要求先清洗资料，核对后再归入 Evo 演示项目。','category':'task_requirement','reason':'保留明确目标与处理顺序。'},ensure_ascii=False)
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
print('Temporary browser fixture ready',flush=True)
try:server.serve_forever()
finally:server.server_close();s.close();shutil.rmtree(config.data_dir)

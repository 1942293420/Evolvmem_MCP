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
# Exercise real legacy migration from an encrypted archive.
with s.store.transaction():
    s.store._connection().execute('DELETE FROM conversation_history WHERE archive_id=?', (archive.id,))
from tests.test_memory_learning import memory
learning_source=memory(s, text='讨论方案时先列业务目标，再核对实现范围。')
s.learning().propose({'topic':'acceptance','instruction':'界面改动后核对实际操作入口。','trigger':'界面改动后','scope':'project','target':'evo','source_ids':[learning_source['id']]},origin='analysis')
# Only the external provider is substituted; HTTP, parsing and policy checks remain real.
def model(prompt, *a, **kw):
    if '"rules"' in prompt:
        return json.dumps({'rules':[]})
    text='以后，修改界面时先明确操作目标和验收条件。'
    return json.dumps({'memories':[{'key':'SESSION_SUMMARY','value':'Evo 正在完善界面调整时的协作流程。'},
        {'key':'project:evo:constraint:ui','value':text,'attribute':'constraint','confidence':.9,
         'learning':{'category':'project_convention','basis':'explicit','quote':text,'question':'修改界面时先明确什么？','answer':text,'instruction':text,'topic':'ui',
          'process':{'goal':{'quote':text},'understanding':{'quote':'我会新建一个独立管理页面。'},
          'correction':{'quote':'管理保留在原知识库，不另建页面。'}}}}]},ensure_ascii=False)
kimi_hooks._load_llm_config=lambda **kwargs: object()
kimi_hooks._call_llm_with_retry=model
kimi_hooks._llm_callable=lambda credentials:model
server=HTTPServer(('127.0.0.1',int(os.environ.get('EVOLVMEM_TEST_PORT','39478'))),make_handler(s))
print('Temporary browser fixture ready',flush=True)
try:server.serve_forever()
finally:server.server_close();s.close();shutil.rmtree(config.data_dir)

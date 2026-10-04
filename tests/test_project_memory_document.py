"""Project memory is one current document with traceable clean conversations."""
import json
import pytest
from evolvmem.context_models import ContextMode
from tests.test_web_server import _make_service

@pytest.fixture
def service(test_config):
    s = _make_service(test_config, mode=ContextMode.SHADOW)
    for project in ('evo', 'shop'):
        s.knowledge().save_project({'project': project})
    yield s
    s.close()


def add(service, text, project='evo', action='publish'):
    return service.knowledge().create({'title': text[:16], 'body': text, 'project': project,
        'content_type': 'session_summary', 'action': action, 'source': '用户对话'})


def test_document_accumulates_and_removes_corrected_or_moved_knowledge(service):
    from evolvmem.project_memory import document
    first = add(service, '界面管理集中在当前知识库内完成。')
    second = add(service, '修改业务行为后，需要核对相关验收要求。')
    add(service, '另一项目的专属要求不可混入。', 'shop')
    add(service, '仍有歧义的内容不进入项目总记忆。', action='draft')
    book = document(service, 'evo')
    assert first['body'] in book['body'] and second['body'] in book['body']
    assert '另一项目' not in book['body'] and '仍有歧义' not in book['body']
    assert set(book['source_ids']) == {first['id'], second['id']}
    assert book['summary'] and book['revision']
    service.knowledge().assign(first['id'], {'project': 'shop', 'expected_revision': first['revision']})
    current = service.knowledge().detail(second['id'])
    service.knowledge().transition(second['id'], {'action': 'archive', 'expected_revision': current['revision']})
    assert document(service, 'evo')['source_ids'] == []


def test_document_snapshot_is_saved_after_intake_without_model(service):
    row = add(service, '明确需求后按项目验收条件推进开发。')
    stored = service.store._connection().execute('SELECT body,summary FROM project_memory_documents WHERE project=?', ('evo',)).fetchone()
    assert row['body'] in stored['body'] and stored['summary']


def test_cleaning_preserves_dialogue_but_excludes_tools_analysis_and_injected_context():
    from evolvmem.conversation import clean_messages
    messages = [
        {'role':'system','content':'SYSTEM_INSTRUCTIONS'},
        {'role':'user','content':'<environment_context>cwd=/tmp</environment_context>\n我需要统一的项目记忆。'},
        {'role':'assistant','channel':'analysis','content':'PRIVATE_REASONING'},
        {'role':'assistant','content':'会以项目总记忆组织，并保留来源。'},
        {'role':'tool','content':'TOOL_OUTPUT'},
        {'role':'assistant','tool_calls':[{'name':'exec'}],'content':''},
        {'role':'user','content':'# AGENTS.md instructions for /tmp\n<INSTRUCTIONS>AUTO_RULES</INSTRUCTIONS>'},
        {'role':'user','content':'先清洗，再入库。'},
    ]
    result = clean_messages(messages)
    assert [m['role'] for m in result] == ['user','assistant','user']
    assert result[0]['content'] == '我需要统一的项目记忆。'
    assert result[-1]['content'] == '先清洗，再入库。'


def test_archive_read_returns_only_dialogue_and_enforces_project(service):
    from evolvmem.session_archive import SessionArchiver
    from evolvmem.project_memory import conversation
    payload={'messages':[{'role':'user','content':'界面集中管理。'}, {'role':'tool','content':'RAW_TOOL_LOG'},
        {'role':'assistant','content':'理解了，管理仍在知识库。'}]}
    archive=SessionArchiver(service.config, service.store).archive_session('evo','kimi','synthetic',json.dumps(payload))
    result=conversation(service,'evo',archive.id)
    assert len(result['messages'])==2 and 'RAW_TOOL_LOG' not in result['text']
    with pytest.raises(ValueError, match='conversation_not_in_project'):
        conversation(service,'shop',archive.id)


def test_real_extraction_prompt_is_cleaned_before_provider(service, monkeypatch):
    from evolvmem import kimi_hooks
    from evolvmem.session_extraction import prepare_extraction
    prompts=[]
    def provider(prompt,*a,**kw):
        prompts.append(prompt)
        return json.dumps({'memories':[{'key':'SESSION_SUMMARY','value':'项目正在改进记忆展示和入库流程。'}]})
    monkeypatch.setattr(kimi_hooks,'_call_llm_with_retry',provider)
    prepare_extraction(service.config,'evo','session-clean',[
        {'role':'user','content':'<environment_context>INJECTED_DATA</environment_context>\n先清洗，再入库。'},
        {'role':'tool','content':'NOISY_TOOL_LOG'}],object())
    assert '先清洗，再入库' in prompts[0]
    assert 'NOISY_TOOL_LOG' not in prompts[0] and 'INJECTED_DATA' not in prompts[0]


def test_project_recall_includes_document_within_shared_budget_without_writes(service):
    from evolvmem.project_mention_recall import recall_mentioned_projects
    first=add(service,'管理集中在现有页面内，保留资料来源便于纠正。')
    conn=service.store._connection()
    before=conn.execute('SELECT revision FROM project_memory_documents WHERE project=?',('evo',)).fetchone()[0]
    result=recall_mentioned_projects(service,query='evo 的开发约定',max_chars=1600)
    assert '项目总记忆' in result.block and first['body'] in result.block
    assert result.used_chars == len(result.block) <=1600
    assert conn.execute('SELECT revision FROM project_memory_documents WHERE project=?',('evo',)).fetchone()[0]==before


def test_document_excludes_inactive_time_windows_and_preserves_duplicate_sources(service):
    from evolvmem.project_memory import document
    current = add(service, '当前项目约定需要保留来源。')
    duplicate = add(service, current['body'])
    future = add(service, '未来才会生效的约定。')
    past = add(service, '已经结束适用的约定。')
    with service.store.transaction():
        service.store._connection().execute('UPDATE context_items SET effective_from=? WHERE id=?', ('2099-01-01 00:00:00', future['id']))
        service.store._connection().execute('UPDATE context_items SET effective_until=? WHERE id=?', ('2000-01-01 00:00:00', past['id']))
    result = document(service, 'evo')
    assert result['body'].count(current['body']) == 1
    assert set(result['source_ids']) == {current['id'], duplicate['id']}
    assert '未来才会' not in result['body'] and '已经结束' not in result['body']


def test_session_start_receives_whole_project_overview_within_budget(service):
    from evolvmem.context_models import ContextSessionStartRequest
    add(service, '本项目的界面管理保留在知识库内。')
    add(service, '商店项目约定禁止跨项目混用。', 'shop')
    result = service.session_start(ContextSessionStartRequest(project='evo', query='本项目', max_chars=1400))
    assert '项目总记忆' in result.block and '商店项目' not in result.block
    assert len(result.block) == result.used_chars <= 1400

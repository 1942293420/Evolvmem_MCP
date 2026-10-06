"""Two memory lanes: clean historical dialogue and scoped, concise Q&A."""
import json
import pytest

from evolvmem.context_models import ContextMode
from evolvmem.session_archive import SessionArchiver
from tests.test_web_server import _make_service


@pytest.fixture
def service(test_config):
    service = _make_service(test_config, mode=ContextMode.SHADOW)
    for project in ('evo', 'shop'):
        service.knowledge().save_project({'project': project})
    yield service
    service.close()


def archive(service, session='session-a', project='evo'):
    return SessionArchiver(service.config, service.store).archive_session(project, 'kimi', session,
        json.dumps({'messages': [
            {'role': 'user', 'content': '<environment_context>INJECTED</environment_context>\n修改界面前先明确验收条件。'},
            {'role': 'assistant', 'channel': 'analysis', 'content': 'HIDDEN_REASONING'},
            {'role': 'tool', 'content': 'RAW_TOOL_LOG'},
            {'role': 'assistant', 'content': '我会先整理验收条件。'}]}, ensure_ascii=False))


def test_archive_persists_clean_history_in_database_and_survives_transport_file_loss(service):
    a = archive(service)
    conn = service.store._connection()
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_history'").fetchone(), 'clean dialogue must have database storage'
    row = conn.execute('SELECT * FROM conversation_history WHERE archive_id=?', (a.id,)).fetchone()
    assert len(json.loads(row['messages'])) == 2
    assert 'RAW_TOOL_LOG' not in row['body'] and 'INJECTED' not in row['body'] and 'HIDDEN_REASONING' not in row['body']
    (service.config.data_dir / a.payload_path).unlink()
    from evolvmem.history_memory import read
    assert '验收条件' in read(service, 'evo', a.id)['text']
    with pytest.raises(ValueError, match='conversation_not_in_project'):
        read(service, 'shop', a.id)


def test_history_migration_is_local_idempotent_and_keeps_missing_sources_explicit(service):
    a = archive(service)
    from evolvmem.history_memory import migrate
    with service.store.transaction():
        service.store._connection().execute('DELETE FROM conversation_history')
    first = migrate(service, {'project': 'evo', 'limit': 10})
    assert first['migrated'] == 1
    assert migrate(service, {'project': 'evo', 'limit': 10})['migrated'] == 0
    with service.store.transaction():
        service.store._connection().execute('DELETE FROM conversation_history')
    (service.config.data_dir / a.payload_path).unlink()
    assert migrate(service, {'project': 'evo', 'limit': 10})['unavailable'] == [a.id]


def extracted_qa(service, *, question='修改界面前应先明确什么？', answer='修改界面前先明确验收条件。',
                 key='ui-goal', basis='explicit', quote=None, session='qa-session'):
    from evolvmem.legacy_models import LegacyExtractionItem, LegacyExtractionRequest
    messages = [{'role': 'user', 'content': quote or answer}]
    a = SessionArchiver(service.config, service.store).archive_session('evo', 'kimi', session,
        json.dumps({'messages': messages}, ensure_ascii=False))
    result = service.persist_legacy_extraction(LegacyExtractionRequest(
        summary=LegacyExtractionItem(key='project:evo:progress:log:'+session, value='本次讨论了界面修改前的开发协作要求。', attribute='fact'),
        candidates=(LegacyExtractionItem(key='project:evo:constraint:'+key, value=answer, attribute='constraint', confidence=.95,
            learning={'category':'project_convention', 'basis':basis, 'quote':quote or answer,
                      'question':question, 'answer':answer, 'trigger':'修改界面时'}),),
        source_session=session), source_archive_id=a.id, source_messages=messages)
    return result, a


def test_explicit_extracted_qa_is_searchable_and_duplicate_keeps_all_sources(service):
    from evolvmem import qa_memory
    result, first = extracted_qa(service)
    rows = qa_memory.list_items(service, {'project':'evo'})['items']
    assert len(rows) == 1 and rows[0]['effective']
    assert rows[0]['question'] == '修改界面前应先明确什么？'
    assert rows[0]['answer'] == '修改界面前先明确验收条件。'
    _, second = extracted_qa(service, key='another-key', session='second-session')
    rows = qa_memory.list_items(service, {'project':'evo'})['items']
    assert len(rows) == 1
    links = service.knowledge().detail(result.candidates[0].context_id)['sources']
    assert {first.id, second.id} <= {s['archive_id'] for s in links}


def test_inferred_conflicting_and_stale_qa_are_not_served_as_active(service):
    from evolvmem import qa_memory
    extracted_qa(service)
    other, _ = extracted_qa(service, answer='修改界面前直接开始写代码。', key='different-key', session='conflict')
    row = qa_memory.detail(service, other.candidates[0].context_id)
    assert not row['effective'] and row['status'] == 'candidate'
    inferred, _ = extracted_qa(service, question='怎样开始调研？', answer='调研时先比较三种方案。', basis='inferred', key='research', session='inferred')
    assert not qa_memory.detail(service, inferred.candidates[0].context_id)['effective']
    first = qa_memory.list_items(service, {'project':'evo', 'state':'active'})['items'][0]
    item = service.knowledge().detail(first['id'])
    service.knowledge().update(item['id'], {'expected_revision':item['revision'], 'body':'用户修正了原先的界面流程要求。'})
    assert not qa_memory.detail(service, first['id'])['effective']
    assert not qa_memory.list_items(service, {'project':'evo', 'state':'active'})['items']


def test_manual_qa_validates_concision_conditions_and_project_boundary(service):
    from evolvmem import qa_memory
    row = qa_memory.save(service, {'project':'evo','question':'什么时候测试？','answer':'修改业务行为后测试相关验收项。',
        'category':'project_convention','trigger':'业务行为发生变化','action':'publish'})
    assert row['effective'] and row['category'] == 'project_convention'
    assert not qa_memory.list_items(service, {'project':'shop'})['items']
    with pytest.raises(ValueError, match='invalid_qa_answer'):
        qa_memory.save(service, {'project':'evo', 'question':'如何说明问题？', 'answer':'很长的内容'*200, 'category':'reference'})
    updated = qa_memory.save(service, {'expected_revision':row['revision'], 'question':'修改业务行为后如何验证？',
        'answer':row['answer'], 'category':row['category'], 'trigger':row['trigger'], 'action':'publish'}, item_id=row['id'])
    assert updated['question'] != row['question'] and updated['effective']
    with pytest.raises(ValueError, match='revision_conflict'):
        qa_memory.save(service, {'expected_revision':row['revision'], 'question':'旧页面修改？','answer':row['answer'], 'category':'reference'}, item_id=row['id'])


def test_two_lane_recall_returns_history_or_qa_without_cross_project_or_budget_leaks(service):
    from evolvmem.memory_recall import recall, read_conversation
    from evolvmem import qa_memory
    _, a = extracted_qa(service)
    qa_memory.save(service, {'project':'shop','question':'商店如何测试？','answer':'商店使用专属测试流程。','action':'publish','category':'experience'})
    history = recall(service, {'project':'evo','query':'以前讨论过什么？','kind':'auto','max_chars':1200})
    assert history['kind'] == 'history' and history['history'] and history['qa'] == []
    assert history['history'][0]['archive_id'] == a.id
    assert '修改界面前' in read_conversation(service, {'project':'evo','archive_id':a.id,'max_chars':1000})['text']
    experience = recall(service, {'project':'evo','query':'类似界面开发怎么做？','kind':'auto','max_chars':1000})
    assert experience['kind'] == 'experience' and experience['qa'] and experience['history'] == []
    assert '商店' not in experience['block'] and experience['used_chars'] == len(experience['block']) <= 1000
    assert recall(service, {'project':'evo','query':'以前怎么做','kind':'experience'})['kind'] == 'experience'
    assert recall(service, {'project':'evo','query':'以前讨论过什么？','kind':'both','max_chars':70})['used_chars'] <= 70


def test_history_document_and_qa_are_separate_and_current_source_edits_propagate(service):
    from evolvmem.project_memory import document
    result, _ = extracted_qa(service)
    book = document(service, 'evo')
    assert '本次讨论了' in book['body']
    assert '修改界面前先明确验收条件。' not in book['body']
    assert book['qa_count'] == 1
    row = service.knowledge().detail(result.summary.context_id)
    service.knowledge().transition(row['id'], {'expected_revision':row['revision'], 'action':'archive'})
    assert '本次讨论了' not in document(service, 'evo')['body']


def test_editing_an_active_qa_as_draft_stops_both_qa_and_legacy_serving(service):
    from evolvmem import qa_memory
    row = qa_memory.save(service, {'project':'evo','question':'什么时候运行测试？','answer':'修改业务行为后测试相关验收项。',
        'category':'project_convention','action':'publish'})
    changed = qa_memory.save(service, {'expected_revision':row['revision'],'question':row['question'],
        'answer':'仅发布前运行验收测试。','category':row['category'],'action':'draft'}, item_id=row['id'])
    assert not changed['effective']
    assert service.knowledge().detail(row['id'])['status'] == 'candidate'


def test_collaboration_skill_uses_effective_qa_but_does_not_promote_temporary_requirements(service):
    from evolvmem import qa_memory
    qa_memory.save(service, {'project':'evo','question':'界面讨论先明确什么？','answer':'先明确用户操作目标与验收条件。',
        'category':'project_convention','trigger':'界面讨论时','action':'publish'})
    qa_memory.save(service, {'project':'evo','question':'本次临时任务是什么？','answer':'本次只调整导航的显示顺序。',
        'category':'task_requirement','action':'publish'})
    skill = service.learning().skill('evo')['skill']
    assert skill == ''
    assert '本次只调整导航的显示顺序。' not in skill


def test_experience_intent_does_not_fall_back_to_history(service):
    from evolvmem.context_models import ContextSessionStartRequest
    archive(service)
    result = service.session_start(ContextSessionStartRequest(project='evo', query='开发约定', max_chars=1000))
    assert '历史对话' not in result.block and '修改界面前' not in result.block


def test_qa_project_move_and_stale_edit_keep_scope_consistent(service):
    from evolvmem import qa_memory, memory_recall
    draft = {'question':'前端修改应该先做什么？','answer':'先明确业务验收条件，再实现界面。','project':'evo','action':'publish'}
    original = qa_memory.save(service,draft)
    moved = qa_memory.save(service,{**draft,'project':'shop','expected_revision':original['revision']},item_id=original['id'])
    assert moved['effective'] and moved['project']=='shop'
    assert not memory_recall.recall(service,{'project':'evo','kind':'experience'})['qa']
    assert memory_recall.recall(service,{'project':'shop','kind':'experience'})['qa'][0]['id']==moved['id']
    with pytest.raises(ValueError,match='revision_conflict'):
        qa_memory.save(service,{**draft,'expected_revision':original['revision']},item_id=original['id'])


def test_global_qa_filter_excludes_unassigned_records(service):
    from evolvmem import qa_memory
    global_item=qa_memory.save(service,{'question':'开发时何时运行测试？','answer':'行为修改之后运行相关用例。','project':'','action':'publish'})
    unassigned=service.knowledge().create({'title':'未归属资料','body':'这个项目有自己的设置，需要核对归属。','action':'draft'})
    rows=qa_memory.list_items(service,{'project':'__global__'})['items']
    assert global_item['id'] in [r['id'] for r in rows]
    assert unassigned['id'] not in [r['id'] for r in rows]


def test_structured_case_can_produce_qa_without_overwriting_case(service):
    from evolvmem import qa_memory
    source=service.knowledge().create({'title':'结构化案例','body':'原来的案例内容和验证记录必须保留。','project':'evo','action':'publish'})
    with service.store.transaction():
        service.store._connection().execute('UPDATE context_items SET experience_payload=? WHERE id=?', ('{}',source['id']))
    source=service.knowledge().detail(source['id'])
    current=qa_memory.detail(service,source['id'])
    result=qa_memory.save(service,{'question':'旧案例如何保留验证依据？','answer':'提炼独立问答，并保留原案例的验证记录。','project':'evo','action':'publish','expected_revision':current['revision']},item_id=source['id'])
    assert result['id']!=source['id'] and result['effective']
    assert service.knowledge().detail(source['id'])['body']==source['body']
    assert any(str(source['id']) in s['source_ref'] for s in result['sources'])


def test_intake_confirmation_activates_valid_qa_but_conflicts_still_require_review(service):
    from evolvmem import qa_memory
    draft=qa_memory.save(service,{'question':'项目开始时先核对什么？','answer':'先核对需求和业务验收条件。','project':'evo','action':'draft'})
    source=service.knowledge().detail(draft['id'])
    service.knowledge().transition(source['id'],{'expected_revision':source['revision'],'action':'publish'})
    assert qa_memory.detail(service,draft['id'])['effective']
    conflict=qa_memory.save(service,{'question':'项目开始时先核对什么？','answer':'直接开发，最后再补需求。','project':'evo','action':'draft'})
    source=service.knowledge().detail(conflict['id'])
    with pytest.raises(ValueError,match='qa_conflict_requires_confirmation'):
        service.knowledge().transition(source['id'],{'expected_revision':source['revision'],'action':'publish'})
    assert service.knowledge().detail(source['id'])['status']=='candidate'


def test_unassigned_legacy_material_can_be_explicitly_confirmed_as_global_qa(service):
    from evolvmem import qa_memory
    source=service.knowledge().create({'title':'通用开发习惯','body':'讨论需求时先给出自己的理解。','action':'draft'})
    current=qa_memory.detail(service,source['id'])
    row=qa_memory.save(service,{'question':'讨论需求时应当怎么开始？','answer':'先给出自己的理解，再核对关键未知。','project':'','category':'habit','action':'publish','expected_revision':current['revision']},item_id=source['id'])
    assert row['effective'] and row['scope']=='global'


def test_migration_only_processes_visible_assigned_conversation_versions(service):
    from evolvmem import history_memory
    archiver=SessionArchiver(service.config,service.store)
    payload=json.dumps({'messages':[{'role':'user','content':'保留有效的项目对话。'}]})
    older=archiver.archive_session('evo','codex','same-session:old',payload)
    latest=archiver.archive_session('evo','codex','same-session:new',payload)
    unassigned=archiver.archive_session('','codex','unknown-session:version',payload)
    with service.store.transaction():service.store._connection().execute('DELETE FROM conversation_history')
    result=history_memory.migrate(service,{'limit':10})
    assert result['migrated']==1
    ids=[r[0] for r in service.store._connection().execute('SELECT archive_id FROM conversation_history')]
    assert ids==[latest.id] and older.id not in ids and unassigned.id not in ids
    assert history_memory.migrate(service,{'limit':10})['migrated']==0

"""Editable stage skills must change the real pipeline, with traceable requirements."""
import json
from pathlib import Path
import pytest
from tests.test_extraction_learning_contract import service, persist
from evolvmem.knowledge_api import dispatch
from evolvmem.pipeline_skills import verify


def save_stage(service, stage, **changes):
    old = dispatch(service, 'GET', 'skills/' + stage)
    return dispatch(service, 'POST', 'skills/' + stage, {'expected_revision': old['revision'], **changes})


def test_skill_catalog_and_attribution_settings_are_executable(service):
    assert [s['id'] for s in dispatch(service, 'GET', 'skills')['skills']] == ['ownership', 'cleaning', 'extraction', 'ingestion', 'collaboration']
    sample = {'body': 'evo 项目需要保留可追溯的历史记录。', 'source': '样例'}
    assert service.knowledge().preview(sample)['project'] == 'evo'
    old = dispatch(service, 'GET', 'skills/ownership')
    saved = save_stage(service, 'ownership', instructions='正文只作参考；优先采用已经登记的归属。', settings={'project_alias_matching': False})
    assert saved['revision'] != old['revision']
    assert service.knowledge().preview(sample)['action'] == 'review'
    assert service.knowledge().preview({**sample, 'project':'evo'})['project'] == 'evo'
    with pytest.raises(ValueError, match='revision_conflict'):
        dispatch(service, 'POST', 'skills/ownership', {'expected_revision':old['revision'], 'instructions':'旧版本不可覆盖新版本。'})


def test_cleaning_preview_archive_and_real_extraction_use_saved_skill(service, monkeypatch):
    from evolvmem.session_archive import SessionArchiver
    from evolvmem.history_memory import read
    from evolvmem.session_extraction import prepare_extraction
    from evolvmem import kimi_hooks
    save_stage(service, 'cleaning', instructions='整理需求时保留用户原话依据，不补造验收条件。', settings={'cleaning_drop_lines':['客户端固定提示'], 'cleaning_collapse_duplicates':True})
    messages = [{'role':'user','content':'客户端固定提示\n我需要在原页面编辑规则。'}, {'role':'tool','content':'工具噪声'}]
    result = dispatch(service, 'POST', 'skills/cleaning/preview', {'messages':messages})
    assert result['messages'] == [{'role':'user','content':'我需要在原页面编辑规则。'}]
    assert result['persisted'] == 0
    archive = SessionArchiver(service.config,service.store).archive_session('evo','kimi','stage-clean',json.dumps({'messages':messages}))
    assert read(service,'evo',archive.id)['messages'] == result['messages']
    prompts=[]
    def model(prompt,*a,**kw):
        prompts.append(prompt)
        return json.dumps({'memories':[{'key':'SESSION_SUMMARY','value':'项目正在改进规则编辑方式。'}]})
    monkeypatch.setattr(kimi_hooks,'_call_llm_with_retry',model)
    prepare_extraction(service.config,'evo','stage-clean',messages,object())
    assert '[user]: 我需要在原页面编辑规则。' in prompts[0]
    assert '[user]: 客户端固定提示' not in prompts[0]
    assert '整理需求时保留用户原话依据' in prompts[0]


def test_normalized_requirement_keeps_quote_and_questions_pending(service):
    quote='我想在经验里直接加项目，不要让我到处找。'
    answer='在经验问答中提供项目创建入口，创建后自动选中。'
    row=persist(service,answer,quote=quote,category='task_requirement',question='经验里如何新增项目？',answer=answer,
        messages=[{'role':'user','content':quote}],normalization={'requirement':answer,'acceptance':[], 'questions':['项目名称与标识是否都需要填写？']})
    assert row['status']=='candidate'
    assert row['learning']['normalization']['requirement']==answer
    assert row['learning']['evidence'][0]['quote']==quote
    assert not service.learning().overview()['rules']


def test_fabricated_acceptance_is_not_auto_admitted(service):
    answer='项目可以在经验问答中新增。'
    row=persist(service,answer,category='task_requirement',question='在哪里新增项目？',answer=answer,
        normalization={'requirement':answer,'acceptance':['用户要求 10 万并发。'],'questions':[]})
    assert row['status']=='candidate'
    assert row['learning']['normalization_errors']


def test_stage_edit_preserves_other_custom_settings(service):
    original=service.knowledge().rules.read()
    service.knowledge().rules.save({'expected_revision':original['revision'],'settings':{**original['settings'],'auto_min_confidence':.97},'instructions':'用户已有自定义规则必须保留。'})
    save_stage(service,'cleaning',instructions='去掉噪声，保留表达的条件和否定。')
    final=service.knowledge().rules.read()
    assert final['settings']['auto_min_confidence']==.97
    assert final['instructions']=='用户已有自定义规则必须保留。'


def test_normalization_is_derived_and_original_history_is_unchanged(service):
    from evolvmem.session_archive import SessionArchiver
    from evolvmem.history_memory import read
    quote='经验里加个新建项目的按钮，点完选中它。'
    answer='在经验问答中新增项目按钮，创建成功后自动选中项目。'
    messages=[{'role':'user','content':quote}]
    archive=SessionArchiver(service.config,service.store).archive_session('evo','kimi','normalize-source',json.dumps({'messages':messages}))
    row=persist(service,answer,quote=quote,category='task_requirement',question='经验问答如何添加项目？',answer=answer,messages=messages,
        normalization={'requirement':answer,'acceptance':[quote],'questions':[]})
    assert row['status']=='active'
    assert row['learning']['normalization']['requirement']==answer
    assert read(service,'evo',archive.id)['messages']==messages
    assert not service.learning().overview()['rules']


def test_collaboration_stage_uses_existing_framework_versions(service):
    old=service.learning().settings()
    saved=save_stage(service,'collaboration',instructions=old['framework']+'\n先核对当前业务目标。\n')
    current=service.learning().overview()
    assert saved['revision']==current['revision']>old['revision']
    assert current['framework'].endswith('先核对当前业务目标。\n')
    assert current['versions']
    with pytest.raises(ValueError,match='invalid_rule_settings'):
        save_stage(service,'cleaning',settings={'auto_min_confidence':0})


def test_total_skill_size_rejected_before_overwriting_saved_rules(service):
    save_stage(service,'ingestion',instructions='入库说明。' * 8000)
    save_stage(service,'ownership',instructions='项目归属。' * 2400)
    before=service.knowledge().rules.read()
    with pytest.raises(ValueError,match='invalid_skill_format'):
        save_stage(service,'cleaning',instructions='数据清洗。' * 2400)
    assert service.knowledge().rules.read()['revision']==before['revision']


def exported(service, stage):
    path = Path(service.config.data_dir) / 'skills' / f'evolvmem-{stage}' / 'SKILL.md'
    return path.read_text(encoding='utf-8') if path.exists() else ''


def test_stages_export_as_five_standalone_skill_files(service):
    dispatch(service, 'GET', 'skills')
    for stage in ('ownership', 'cleaning', 'extraction', 'ingestion', 'collaboration'):
        assert f'name: evolvmem-{stage}' in exported(service, stage), stage
    save_stage(service, 'ownership', instructions='先核对已经登记的项目归属。')
    assert '先核对已经登记的项目归属。' in exported(service, 'ownership')
    assert '先核对已经登记的项目归属。' not in exported(service, 'cleaning')


def test_full_rules_save_and_framework_save_refresh_stage_exports(service):
    rules = service.knowledge().rules.read()
    dispatch(service, 'POST', 'rules', {'expected_revision': rules['revision'],
        'settings': {**rules['settings'], 'ownership_instructions': '整体保存也要同步导出。'}})
    assert '整体保存也要同步导出。' in exported(service, 'ownership')
    old = service.learning().settings()
    dispatch(service, 'POST', 'learning/framework', {'expected_revision': old['revision'],
        'framework': old['framework'] + '\n先确认业务目标再动手。\n'})
    assert '先确认业务目标再动手。' in exported(service, 'collaboration')


def test_ownership_verify_parks_sample_with_decision_reason(service):
    result = verify(service, 'ownership', {'body': 'evo 项目需要保留可追溯的历史记录。'})
    assert result['created'] == 1 and result['decision']['project'] == 'evo'
    row = service.knowledge().detail(result['ids'][0])
    assert row['status'] == 'candidate' and row['project'] == ''
    assert 'Skill 验证' in row['ingestion_reason'] and 'evo' in row['ingestion_reason']
    assert 'skill-verify' in row['tags']


def test_ingestion_verify_uses_saved_thresholds(service):
    save_stage(service, 'ingestion', settings={'min_chars': 100})
    result = verify(service, 'ingestion', {'body': '太短。'})
    assert result['decision']['action'] == 'review'
    row = service.knowledge().detail(result['ids'][0])
    assert row['status'] == 'candidate' and '长度' in row['ingestion_reason']
    with pytest.raises(ValueError, match='invalid_content'):
        verify(service, 'ingestion', {'body': ''})
    with pytest.raises(LookupError, match='skill_not_found'):
        verify(service, 'unknown-stage', {'body': '内容'})


def test_cleaning_verify_saves_cleaned_dialogue_as_pending(service):
    save_stage(service, 'cleaning', instructions='保留原话。', settings={'cleaning_drop_lines': ['客户端固定提示']})
    messages = [{'role': 'user', 'content': '客户端固定提示\n我需要直接编辑规则。'}, {'role': 'tool', 'content': '工具噪声'}]
    result = verify(service, 'cleaning', {'messages': messages})
    row = service.knowledge().detail(result['ids'][0])
    assert row['status'] == 'candidate'
    assert '我需要直接编辑规则。' in row['body'] and '工具噪声' not in row['body']
    with pytest.raises(ValueError, match='invalid_preview_messages'):
        verify(service, 'cleaning', {'messages': []})


def test_extraction_verify_parks_model_candidates_for_confirmation(service):
    quote = '以后提交前先跑一遍相关测试。'
    def model(prompt):
        return json.dumps({'memories': [
            {'key': 'SESSION_SUMMARY', 'value': '讨论提交前跑测试的要求。'},
            {'key': 'project:evo:convention:testing', 'value': '提交前先运行相关测试。', 'attribute': 'constraint',
             'confidence': .95, 'learning': {'category': 'project_convention', 'basis': 'explicit', 'quote': quote,
                                             'instruction': '提交前先运行相关测试。', 'topic': 'testing',
                                             'question': '提交代码前要做什么？', 'answer': '先运行相关测试。'}}]})
    result = verify(service, 'extraction', {'project': 'evo', 'messages': [{'role': 'user', 'content': quote}]}, llm=model)
    assert result['created'] == 1 and result['model_calls'] == 1
    row = service.knowledge().detail(result['ids'][0])
    assert row['status'] == 'candidate' and row['project'] == 'evo'
    assert row['title'] == '提交代码前要做什么？' and row['body'] == '先运行相关测试。'
    from evolvmem.qa_memory import detail as qa_detail
    assert qa_detail(service, row['id'])['question'] == '提交代码前要做什么？'


def test_collaboration_verify_reports_empty_scope_without_model(service):
    result = verify(service, 'collaboration', {})
    assert result['status'] == 'empty' and result['created'] == 0


def test_consecutive_verifies_do_not_leave_an_open_transaction(service):
    verify(service, 'ownership', {'body': 'evo 项目需要验证归属判断。'})
    result = verify(service, 'cleaning', {'messages': [{'role': 'user', 'content': '连续两次验证都要成功。'}]})
    assert result['created'] == 1
    again = verify(service, 'ingestion', {'body': 'evo 项目的第三次验证也要正常写入。'})
    assert again['created'] == 1

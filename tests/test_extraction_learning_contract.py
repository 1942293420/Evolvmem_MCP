"""P1: keep the dialogue's evidence and make preview match ingestion."""
import json

import pytest

from evolvmem.context_models import ContextMode
from evolvmem.legacy_models import LegacyExtractionItem, LegacyExtractionRequest
from tests.test_web_server import _make_service


@pytest.fixture
def service(test_config):
    s = _make_service(test_config, mode=ContextMode.SHADOW)
    for project in ('evo', 'shop'):
        s.knowledge().save_project({'project': project})
    yield s
    s.close()


def request(text, *, session='p1-session', key='project:evo:constraint:testing', **learning):
    return LegacyExtractionRequest(
        summary=LegacyExtractionItem(key=f'project:evo:progress:log:{session}',
            value='Evo 正在讨论并完善长期开发协作约定。', attribute='fact'),
        candidates=(LegacyExtractionItem(key=key, value=text, attribute='constraint', confidence=.95,
            learning={'category': 'project_convention', 'basis': 'explicit', 'quote': text,
                      'instruction': text, 'topic': 'testing', **learning}),), source_session=session)


def persist(s, text, **kw):
    messages = kw.pop('messages', [{'role': 'user', 'content': text}])
    r = s.persist_legacy_extraction(request(text, **kw), source_messages=messages)
    return s.knowledge().detail(r.candidates[0].context_id) if r.candidates else None


def test_full_process_retains_roles_and_does_not_turn_assistant_claim_into_success(service):
    messages = [
        {'role': 'user', 'content': '我需要看见知识库学到了什么。'},
        {'role': 'assistant', 'content': '我会新建独立管理站点。'},
        {'role': 'user', 'content': '优化前端别把管理单独做一个页面。'},
        {'role': 'assistant', 'content': '已经做好并测试通过了。'},
    ]
    row = persist(service, messages[2]['content'], messages=messages, process={
        'goal': {'quote': messages[0]['content']}, 'understanding': {'quote': messages[1]['content']},
        'correction': {'quote': messages[2]['content']}, 'decision': {'quote': messages[2]['content']},
        'verification': {'quote': messages[3]['content']}})
    process = row['learning'].get('process', {})
    assert process.get('goal', {}).get('message_index') == 0
    assert process['understanding']['role'] == 'assistant'
    assert process['correction']['quote'] == messages[2]['content']
    assert process['verification']['verified'] is False
    assert row['success_count'] == 0


def test_unconfirmed_same_key_conflict_preserves_old_memory_and_rule(service):
    old = persist(service, '开发完成后先运行相关测试再交付。', session='old')
    new = persist(service, '开发完成后先交付再运行相关测试。', session='new', basis='inferred')
    assert service.knowledge().detail(old['id'])['status'] == 'active'
    assert new['status'] == 'candidate'
    assert old['body'] in service.learning().skill('evo')['skill']
    assert new['body'] not in service.learning().skill('evo')['skill']


def test_explicit_correction_replaces_exact_old_version_and_is_idempotent(service):
    old = persist(service, '开发完成后先交付再运行相关测试。', session='old')
    text = '开发完成后先运行相关测试再交付。'
    correction = f'以后把“{old["body"]}”改为“{text}”'
    args = dict(session='correction', action='replace', target_id=old['id'], target_revision=old['revision'],
                process={'correction': {'quote': correction}}, messages=[{'role': 'user', 'content': correction}])
    new = persist(service, text, **args)
    assert service.knowledge().detail(old['id'])['status'] == 'superseded'
    assert new['supersedes'] == old['id']
    assert text in service.learning().skill('evo')['skill']
    assert old['body'] not in service.learning().skill('evo')['skill']
    before = len(service.learning().overview()['rules'])
    assert persist(service, text, **args) is None
    assert len(service.learning().overview()['rules']) == before


@pytest.mark.parametrize('bad_target', ['stale', 'other_project', 'invented_quote'])
def test_replacement_needs_current_in_scope_target_and_real_correction(service, bad_target):
    old = persist(service, '开发完成后先交付再运行相关测试。', session='old',
                  key='project:shop:constraint:testing' if bad_target == 'other_project' else 'project:evo:constraint:testing')
    text = '开发完成后先运行相关测试再交付。'
    correction = f'以后把“{old["body"]}”改为“{text}”'
    row = persist(service, text, session='new', action='replace', target_id=old['id'],
        target_revision='stale' if bad_target == 'stale' else old['revision'],
        process={'correction': {'quote': correction}},
        messages=[{'role': 'user', 'content': text if bad_target == 'invented_quote' else correction}])
    assert service.knowledge().detail(old['id'])['status'] == 'active'
    assert row['status'] == 'candidate'
    assert not any(r['effective'] and r['instruction'] == text for r in service.learning().overview()['rules'])


def test_supplement_preserves_conditions_and_fabricated_skip_does_not_discard(service):
    old = persist(service, '数据库迁移时运行数据库集成测试。', session='old')
    new = persist(service, '修改静态页面时只检查页面实际显示。', session='new', action='supplement',
                  target_id=old['id'], target_revision=old['revision'])
    assert service.knowledge().detail(old['id'])['status'] == 'active'
    assert new['learning'].get('relation', {}).get('target_id') == old['id']
    skipped = persist(service, '涉及支付功能时需要验证金额计算。', session='skip', action='skip', target_id=old['id'])
    assert skipped is not None and skipped['status'] == 'candidate'


def test_preview_compares_unsaved_rules_without_writes_and_matches_real_ingestion(service):
    from evolvmem.extraction_preview import preview
    kb = service.knowledge()
    policy = kb.rules.read()
    text = '复杂需求讨论时先给出目标和验收例子。'
    messages = [{'role': 'user', 'content': text}]
    response = json.dumps({'memories': [
        {'key': 'SESSION_SUMMARY', 'value': 'Evo 正在完善需求讨论阶段的协作约定。'},
        {'key': 'project:evo:constraint:testing', 'value': text, 'attribute': 'constraint', 'confidence': .9,
         'learning': {'category': 'project_convention', 'basis': 'explicit', 'quote': text, 'instruction': text}}]})
    before = service.store._connection().total_changes
    result = preview(service, {'project': 'evo', 'messages': messages,
        'rules': {'expected_revision': policy['revision'], 'settings': {**policy['settings'], 'auto_min_confidence': .99}}},
        llm=lambda prompt: response)
    assert service.store._connection().total_changes == before
    assert kb.rules.read()['revision'] == policy['revision']
    assert result['current']['candidates'][0]['status'] == 'active'
    assert result['draft']['candidates'][0]['status'] == 'candidate'
    saved = kb.rules.save({'expected_revision': policy['revision'], 'skill': result['draft']['skill']})
    assert saved['revision'] == result['draft']['rule_revision']
    row = persist(service, text)
    assert row['status'] == 'candidate'


def test_old_context_stays_in_project_and_rule_edit_reaches_real_extraction(service, monkeypatch):
    from evolvmem import kimi_hooks
    from evolvmem.session_extraction import prepare_extraction
    from tests.test_kimi_hooks import _llm_config
    mine = persist(service, '复杂需求讨论时先给出目标和验收例子。')
    other = persist(service, '商店项目使用独立的结算流程。', session='shop', key='project:shop:constraint:checkout')
    rules = service.knowledge().rules
    current = rules.read()
    rules.save({'expected_revision': current['revision'], 'instructions': current['instructions'] + '\n保留需求发生变化的原因。',
                'settings': {**current['settings'], 'related_memory_projects': ['evo']}})
    seen = []
    def model(prompt, *_args, **_kwargs):
        seen.append(prompt)
        return json.dumps({'memories': [{'key': 'SESSION_SUMMARY', 'value': 'Evo 正在讨论既有需求约定是否需要变化。'}]})
    monkeypatch.setattr(kimi_hooks, '_call_llm_with_retry', model)
    prepare_extraction(service.config, 'evo', 'adapter', [{'role': 'user', 'content': '需求讨论仍然按以前的约定。'}], _llm_config())
    assert mine['body'] in seen[0]
    assert other['body'] not in seen[0]
    assert '保留需求发生变化的原因。' in seen[0]


def test_kimi_session_preserves_learning_at_real_storage_boundary(monkeypatch, tmp_path, test_config):
    from evolvmem import kimi_hooks as hooks
    from evolvmem.auto_extractor import CandidateMemory
    from tests.test_kimi_hooks import TestSessionEndOutcome, _llm_config
    text = '讨论界面调整时先明确操作目标与验收条件。'
    TestSessionEndOutcome._wire_session(monkeypatch, tmp_path, test_config, text * 15)
    monkeypatch.setattr(hooks, '_load_llm_config', _llm_config)
    monkeypatch.setattr(hooks, '_project_from_wire', lambda *_: 'evo')
    monkeypatch.setattr(hooks, '_run_consolidation_best_effort', lambda *_: None)
    monkeypatch.setattr(hooks, '_llm_callable', lambda *_: lambda _: None)
    monkeypatch.setattr(hooks, '_extract_candidates', lambda *a, **kw: [
        CandidateMemory(key='SESSION_SUMMARY', value='Evo 正在完善需求讨论阶段的协作约定。'),
        CandidateMemory(key='project:evo:constraint:planning', value=text, confidence=.95,
            learning={'category': 'project_convention', 'basis': 'explicit', 'quote': text, 'instruction': text})])
    test_config.context_mode = 'shadow'
    s = _make_service(test_config, mode=ContextMode.SHADOW)
    s.knowledge().save_project({'project': 'evo'})
    s.close()
    assert hooks.session_end({'session_id': 'p1-kimi'}).status == 'completed'
    s = _make_service(test_config, mode=ContextMode.SHADOW)
    try:
        rows = s.learning().overview()['rules']
        assert any(r['instruction'] == text and r['effective'] for r in rows)
    finally:
        s.close()


def test_same_summary_different_conditions_are_not_collapsed(service):
    text = '变更完成后运行相关集成测试。'
    first = persist(service, text, session='schema', trigger='修改数据库结构时')
    second = persist(service, text, session='payments', trigger='修改支付金额计算时',
                     action='supplement', target_id=first['id'], target_revision=first['revision'])
    assert second is not None and second['id'] != first['id']
    assert second['learning']['trigger'] == '修改支付金额计算时'
    assert {r['trigger'] for r in service.learning().overview()['rules']} == {'修改数据库结构时', '修改支付金额计算时'}


def test_rule_version_change_rolls_back_entire_extraction_batch(service):
    old = service.knowledge().rules.read()
    service.knowledge().rules.save({'expected_revision': old['revision'], 'settings': {**old['settings'], 'auto_min_confidence': .99}})
    before = service.store._connection().execute('SELECT count(*) FROM context_items').fetchone()[0]
    with pytest.raises(ValueError, match='extraction_rules_changed'):
        persist(service, '开发完成后先运行相关测试再交付。', rule_revision=old['revision'])
    assert service.store._connection().execute('SELECT count(*) FROM context_items').fetchone()[0] == before


def test_explicit_rule_switch_keeps_knowledge_but_requires_rule_confirmation(service):
    kb = service.knowledge()
    p = kb.rules.read()
    kb.rules.save({'expected_revision': p['revision'], 'settings': {**p['settings'], 'auto_explicit_rules': False}})
    row = persist(service, '开发完成后先运行相关测试再交付。')
    assert row['status'] == 'active'
    assert service.learning().overview()['rules'][0]['status'] == 'candidate'


def test_learning_memories_remain_available_to_legacy_clients(service):
    row = persist(service, '开发完成后先运行相关测试再交付。')
    assert row['legacy_ids'], 'Kimi legacy memory readers need the existing projection mapping'
    projected = service.store.legacy_projection().get_by_id(row['legacy_ids'][0])
    assert projected['value'] == row['body']


def test_preview_respects_same_batch_duplicates_and_eight_write_limit(service):
    from evolvmem.extraction_preview import preview
    texts = [f'第 {i} 项开发约定：每次交付保留相关验收依据。' for i in range(10)]
    memories = [{'key': 'SESSION_SUMMARY', 'value': 'Evo 讨论了多项交付阶段的长期协作约定。'}]
    for i, text in enumerate([texts[0], *texts]):
        memories.append({'key': f'project:evo:constraint:rule-{i}', 'value': text, 'attribute': 'constraint',
                         'confidence': .95, 'learning': {'category': 'project_convention', 'quote': text}})
    result = preview(service, {'project': 'evo', 'messages': [{'role': 'user', 'content': '\n'.join(texts)}]},
                     llm=lambda _: json.dumps({'memories': memories}))
    rows = result['current']['candidates']
    assert len([r for r in rows if r['action'] != 'skip']) == 8

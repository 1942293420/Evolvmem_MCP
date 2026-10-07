"""Deterministic basis: explicit user requirements promote, assistant proposals never do.

All wording here is synthetic and hand-derived. Each test names the production
change it catches: a gate that stops reading a missing ``question``/``answer``
(or a missing ``normalization``) as "nothing to check", and a gate that stops
accepting an assistant-role quote as user evidence.
"""
import json

import pytest

from evolvmem.context_models import ContextMode
from evolvmem.learning_extraction import plan
from evolvmem.legacy_models import LegacyExtractionItem
from tests.test_web_server import _make_service

USER_LINE = '帮我把资料整理成SOP；整理时只做只读查看和无副作用测试，不进行实质性操作。'
ASSISTANT_LINE = '我先只读资料并做无副作用测试，之后再整理可执行 skill。'


@pytest.fixture
def service(test_config):
    svc = _make_service(test_config, mode=ContextMode.SHADOW)
    svc.knowledge().save_project({'project': 'demo'})
    yield svc
    svc.close()


def item(value, learning, *, key='project:demo:constraint:preview', attribute='constraint'):
    return LegacyExtractionItem(key=key, value=value, attribute=attribute, confidence=.95,
                                learning=learning)


def full_learning(value, **extra):
    """A complete, traceable answer: value == answer, quote is the user's own line."""
    return {'category': 'task_requirement', 'basis': 'explicit', 'quote': USER_LINE,
            'question': '整理演示店铺资料时允许做哪些操作？', 'answer': value,
            'normalization': {'requirement': value, 'acceptance': [], 'questions': []}, **extra}


def reviewed(item, messages, *, quote=USER_LINE):
    """Run the production batch review, then keep the deterministic plan gate."""
    from evolvmem import answer_support
    answer_support.support(messages, [item], lambda _prompt: json.dumps(
        [{'id': 1, 'verdict': 'supported', 'reason': '与本条用户原话一致', 'quote': quote}],
        ensure_ascii=False))
    return item


def test_omitting_question_and_answer_does_not_promote_a_user_requirement(service):
    """Catch: a missing question/answer pair must not read as "nothing to check"."""
    value = '整理演示店铺资料时只做只读查看和无副作用测试，不进行任何实质性操作。'
    messages = [{'role': 'assistant', 'content': ASSISTANT_LINE}, {'role': 'user', 'content': USER_LINE}]
    learning = full_learning(value)
    learning.pop('question')
    learning.pop('answer')
    assert plan(service, item(value, learning), messages)['status'] == 'candidate'


def test_complete_answer_promotes_with_or_without_the_optional_normalization(service):
    """Catch: promotion must not depend on optional normalization being present."""
    value = '整理演示店铺资料时只做只读查看和无副作用测试，不进行任何实质性操作。'
    messages = [{'role': 'assistant', 'content': ASSISTANT_LINE}, {'role': 'user', 'content': USER_LINE}]
    assert plan(service, reviewed(item(value, full_learning(value)), messages),
                messages)['status'] == 'active'
    without = full_learning(value)
    without.pop('normalization')
    assert plan(service, reviewed(item(value, without), messages), messages)['status'] == 'active'


def test_answer_that_is_not_the_user_quote_still_promotes_and_keeps_the_source_quote(service):
    """Catch: the extracted answer may be a faithful summary; the quote must stay the user line."""
    value = '整理演示店铺资料时只做只读查看和无副作用测试。'
    messages = [{'role': 'user', 'content': USER_LINE}]
    result = plan(service, reviewed(item(value, full_learning(value)), messages), messages)
    assert result['status'] == 'active'
    assert result['body'] == value
    assert result['normalization']['requirement'] == value
    assert result['body'] != USER_LINE


def test_assistant_only_source_never_becomes_a_confirmed_user_decision(service):
    """Catch: an assistant message must not satisfy the user-evidence gate."""
    value = '演示店铺先做只读核对，再制作可执行 skill。'
    learning = {'category': 'decision', 'basis': 'explicit', 'quote': ASSISTANT_LINE,
                'question': '后续如何整理演示店铺资料？', 'answer': value}
    assert plan(service, item(value, learning, key='project:demo:decision:flow'),
                [{'role': 'assistant', 'content': ASSISTANT_LINE}])['status'] == 'candidate'


def test_paraphrased_quote_is_not_user_evidence(service):
    """Catch: plan() must not accept a quote that no user message contains verbatim."""
    value = '演示店铺的整理结论只来自助手复述。'
    quoted = '用户要求整理演示店铺资料并只做测试'
    learning = {'category': 'project_convention', 'basis': 'explicit', 'quote': quoted,
                'question': '演示店铺资料的整理边界是什么？', 'answer': value}
    assert plan(service, item(value, learning), [{'role': 'user', 'content': USER_LINE}])['status'] == 'candidate'


def test_direct_user_requirement_with_a_complete_answer_can_still_auto_enter(service):
    """Catch: a real user restriction must not be parked by the new QA requirement."""
    value = '整理演示店铺资料时只做只读查看和无副作用测试。'
    messages = [{'role': 'user', 'content': USER_LINE}]
    result = plan(service, reviewed(item(value, full_learning(value)), messages), messages)
    assert result['status'] == 'active'
    assert result['normalization']['requirement'] == value


def test_complete_inferred_answer_stays_isolated_even_with_a_user_quote(service):
    """Catch: a provider-marked inference must not promote on a verbatim user quote alone."""
    value = '整理演示店铺资料时只做只读查看和无副作用测试。'
    learning = full_learning(value, basis='inferred')
    result = plan(service, item(value, learning), [{'role': 'user', 'content': USER_LINE}])
    assert result['status'] == 'candidate'


def test_learning_contract_prompt_states_the_required_final_shape(service):
    """Catch: the base prompt declaring ``learning`` as optional while the tail requires it."""
    from evolvmem.auto_extractor import AutoExtractor
    messages = [{'role': 'user', 'content': USER_LINE}, {'role': 'assistant', 'content': ASSISTANT_LINE}]
    prompt = AutoExtractor().build_extraction_prompt(messages)
    compact = ''.join(prompt.split())
    for required in ('"question"', '"answer"', '"quote"', '"normalization"', '"basis"'):
        assert required in compact, required
    assert '"SESSION_SUMMARY"' in compact, 'the example output must stay a valid memories array'
    assert '逐字一致' in prompt and '助手补充' in prompt
    assert '不得作为用户原话依据' in prompt
    assert '<reviewed_cleaning>' not in prompt


def test_mixed_role_contract_forbids_backing_an_assistant_list_with_a_user_question(service):
    """Catch: a user question used as evidence for the assistant's own enumerated list."""
    from evolvmem.auto_extractor import AutoExtractor
    prompt = AutoExtractor().build_extraction_prompt(
        [{'role': 'user', 'content': USER_LINE}, {'role': 'assistant', 'content': ASSISTANT_LINE}])
    compact = ''.join(prompt.split())
    assert '每一个实质断言' in compact, 'explicit answers must be asserted claim by claim'
    assert '拆成多条' in compact, 'mixed-role content must be split into separate memories'
    assert '还需要哪些数据' in prompt and '先列明待补数据' in prompt, \
        'the canonical mixed-role example from the real review must stay in the contract'
    assert '不能为助手产出的文档标题或流程背书' in prompt
    assert '本次任务范围' in prompt, 'the scope must stay this task, not a permanent store preference'


def test_reviewed_cleaning_block_is_not_presented_as_user_confirmation(service, monkeypatch):
    """Catch: a derived cleaning/segmentation draft read as user approval."""
    from evolvmem import kimi_hooks
    from evolvmem.session_extraction import prepare_extraction
    from tests.test_kimi_hooks import _llm_config
    import json
    prompts = []

    def model(prompt, *a, **kw):
        prompts.append(prompt)
        return json.dumps({'memories': [{'key': 'SESSION_SUMMARY', 'value': '演示项目讨论了整理边界。'}]})

    monkeypatch.setattr(kimi_hooks, '_call_llm_with_retry', model)
    prepare_extraction(service.config, 'demo', 'demo-source',
                       [{'role': 'user', 'content': USER_LINE}], _llm_config(),
                       reviewed_cleaning='整理后的演示摘要：只做只读测试。', related_context=[])
    assert '<reviewed_cleaning>' in prompts[0]
    assert '用户核对后保存的清洗稿' not in prompts[0]
    assert '不能仅凭它视为用户确认' in prompts[0]
    assert '不是原话证据' in prompts[0]
    assert '不得作为用户原话依据' in prompts[0]


def test_reference_material_needs_a_traceable_pair_and_keeps_its_role(service):
    """Catch: a cited reference must wait for a real question/answer instead of being invented."""
    value = '演示店铺的资料里包含两条建联路径。'
    learning = {'category': 'reference', 'basis': 'explicit', 'quote': USER_LINE,
                'instruction': value, 'topic': '资料路径', 'trigger': '核对资料时'}
    result = plan(service, item(value, learning, key='project:demo:reference:paths',
                                attribute='fact'), [{'role': 'user', 'content': USER_LINE}])
    assert result['status'] == 'candidate'
    assert result['decision']['project'] == 'demo'
    assert result['body'] == value
    assert result['action'] == 'add'


def test_answer_that_does_not_equal_the_value_does_not_promote(service):
    """Catch: the QA pair must answer with exactly the memory content (answer == value)."""
    value = '整理演示店铺资料时只做只读查看和无副作用测试。'
    learning = full_learning(value)
    learning['answer'] = '整理演示店铺资料时允许发送邀请和修改佣金。'
    result = plan(service, item(value, learning), [{'role': 'user', 'content': USER_LINE}])
    assert result['status'] == 'candidate'
    assert '问答答案与记忆内容不一致' == result['reason']


def test_full_user_flow_through_prepare_extraction_stays_a_candidate_without_qa(service, monkeypatch):
    """Catch: the defect must be fixed in the shared prepare_extraction -> plan path, not only in plan()."""
    from evolvmem import kimi_hooks
    from evolvmem.learning_extraction import plan
    from evolvmem.session_extraction import prepare_extraction
    from tests.test_kimi_hooks import _llm_config
    import json
    quote = USER_LINE
    value = '整理演示店铺资料时只做只读查看和无副作用测试。'
    response = json.dumps({'memories': [
        {'key': 'SESSION_SUMMARY', 'value': '演示项目讨论了资料整理的操作边界。'},
        {'key': 'project:demo:constraint:preview', 'value': value, 'attribute': 'constraint',
         'confidence': .95,
         'learning': {'category': 'task_requirement', 'basis': 'explicit', 'quote': quote,
                      'instruction': value, 'topic': '整理边界'}}]})
    monkeypatch.setattr(kimi_hooks, '_call_llm_with_retry', lambda *a, **kw: response)
    messages = [{'role': 'user', 'content': quote}]
    request = prepare_extraction(service.config, 'demo', 'demo-source', messages, _llm_config(),
                                 reviewed_cleaning=None, related_context=[])
    candidate = next(c for c in request.candidates if c.key != 'SESSION_SUMMARY')
    result = plan(service, candidate, messages)
    assert candidate.learning.get('question') is None
    assert result['status'] == 'candidate'


@pytest.mark.parametrize('category', ['experience'])
def test_unverified_experience_stays_a_candidate(service, category):
    """Catch: a method without a bound verification result must not enter automatically."""
    value = '通过CLI打开店铺后仍需要人工完成图形验证。'
    learning = {'category': category, 'basis': 'explicit', 'quote': USER_LINE,
                'question': 'CLI打开店铺后如何完成登录？', 'answer': value}
    assert plan(service, item(value, learning), [{'role': 'user', 'content': USER_LINE}])['status'] == 'candidate'

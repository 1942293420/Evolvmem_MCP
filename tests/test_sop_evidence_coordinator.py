"""Independent checks: optional metadata must not turn suggestions into decisions."""
import pytest
from evolvmem.context_models import ContextMode
from evolvmem.legacy_models import LegacyExtractionItem
from evolvmem.learning_extraction import plan
from tests.test_web_server import _make_service

@pytest.fixture
def service(test_config):
    svc = _make_service(test_config, mode=ContextMode.SHADOW)
    svc.knowledge().save_project({'project': 'demo'})
    yield svc
    svc.close()

@pytest.mark.parametrize('qa', [False, True])
def test_assistant_proposal_never_becomes_confirmed_decision_by_omitting_qa(service, qa):
    answer = '演示批处理采用自动重试并在失败后恢复任务。'
    learning = {'category': 'decision', 'basis': 'explicit', 'quote': answer}
    if qa:
        learning.update(question='演示批处理采用什么方式？', answer=answer)
    item = LegacyExtractionItem(key='project:demo:decision:recovery', value=answer,
                                attribute='decision', confidence=.95, learning=learning)
    result = plan(service, item, [{'role': 'assistant', 'content': answer}])
    assert result['status'] == 'candidate'

def _item(answer, basis, **extra):
    return LegacyExtractionItem(key='project:demo:constraint:preview', value=answer,
        attribute='constraint', confidence=.95,
        learning={'category': 'task_requirement', 'basis': basis, 'quote': answer,
                  'question': '本次演示数据操作范围是什么？', 'answer': answer,
                  'normalization': {'requirement': answer, 'acceptance': [], 'questions': []},
                  **extra})


def test_an_explicit_pair_without_the_answer_review_stays_a_candidate(service):
    """Catch: 只凭 basis=explicit 与完整问答不能绕过独立答案核对。"""
    answer = '本次只允许预览演示数据，不执行实际写入。'
    messages = [{'role': 'user', 'content': answer}]
    assert plan(service, _item(answer, 'explicit'), messages)['status'] == 'candidate'
    assert plan(service, _item(answer, 'inferred'), messages)['status'] == 'candidate'


def test_the_same_pair_promotes_only_after_the_real_review_helper(service):
    """Catch: 审核协议先产生合法核对元数据，plan 再据此放行 explicit。"""
    import json

    from evolvmem import answer_support
    answer = '本次只允许预览演示数据，不执行实际写入。'
    messages = [{'role': 'user', 'content': answer}]
    item = _item(answer, 'explicit')
    answer_support.support(messages, [item], lambda prompt: json.dumps(
        [{'id': 1, 'verdict': 'supported', 'reason': '与原话一致', 'quote': answer}],
        ensure_ascii=False))
    assert item.learning['answer_support']['verdict'] == 'supported'
    assert plan(service, item, messages)['status'] == 'active'
    assert plan(service, _item(answer, 'inferred'), messages)['status'] == 'candidate'

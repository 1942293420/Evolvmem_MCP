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

@pytest.mark.parametrize('basis', ['explicit', 'inferred'])
def test_user_basis_controls_promotion_with_complete_question_and_answer(service, basis):
    answer = '本次只允许预览演示数据，不执行实际写入。'
    item = LegacyExtractionItem(key='project:demo:constraint:preview', value=answer,
        attribute='constraint', confidence=.95,
        learning={'category': 'task_requirement', 'basis': basis, 'quote': answer,
                  'question': '本次演示数据操作范围是什么？', 'answer': answer,
                  'normalization': {'requirement': answer, 'acceptance': [], 'questions': []}})
    result = plan(service, item, [{'role': 'user', 'content': answer}])
    assert result['status'] == ('active' if basis == 'explicit' else 'candidate')

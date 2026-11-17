import pytest
from tests.test_experience_service import experiences,case,proof
from tests.test_context_playbook import _ok_response
from evolvmem.context_lifecycle import ContextLifecycleError

class Engine:
    is_loaded=True
    def encode_document(self,text): return [1.,0.]


def test_generated_method_needs_own_verification(experiences):
    core=experiences.core
    for i in range(3):
        experiences.record(case(problem=f'列表打开缓慢分段分析{i}'),
                           evidence=proof(experiences,task=f'task-{i%2}'))
    report=core.run_consolidation(llm=lambda _: _ok_response(),embedding_engine=Engine())
    assert len(report.playbook_created_ids)==1
    item_id=report.playbook_created_ids[0]
    with pytest.raises(ContextLifecycleError): core.confirm(item_id)
    with pytest.raises(ContextLifecycleError): core.record_outcome(item_id,'success')
    method=experiences.read(item_id)
    assert method['status']=='candidate' and method['success_count']==0
    verified=experiences.outcome(item_id,proof(experiences,'method-validation'))
    assert verified['status']=='active' and verified['success_count']==1

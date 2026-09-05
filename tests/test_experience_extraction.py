"""A long case survives extraction as an unverified, source-linked candidate."""
import json
from evolvmem.auto_extractor import AutoExtractor
from tests.test_experience_service import case
from tests.test_context_service import _legacy_schema, _extraction_service, _extraction_request
from evolvmem.context_store import ContextStore
from evolvmem.context_models import ContextMode
from evolvmem.legacy_models import LegacyExtractionItem


def test_structured_extraction_preserves_steps_without_promoting(test_config):
    payload = case()
    payload['steps'] = ['检查关键状态并执行具体操作。' * 20, '记录真实结果']
    candidates = AutoExtractor().parse_response(json.dumps({'memories': [{
        'key':'project:demo:experience:latency','value':'列表打开缓慢时先检查查询执行计划',
        'attribute':'experience', 'case':payload,
    }]}))
    assert candidates[0].experience_case == payload
    _legacy_schema(test_config)
    with ContextStore(test_config) as store:
        core = _extraction_service(test_config, store, mode=ContextMode.SHADOW)
        request = _extraction_request(candidates=(LegacyExtractionItem(
            key=candidates[0].key,value=candidates[0].value,attribute='experience',
            experience_case=candidates[0].experience_case),))
        result = core.persist_legacy_extraction(request)
        item_id = result.candidates[0].context_id
        saved = core.experiences().read(item_id)
        assert saved['steps'] == payload['steps']
        assert saved['status'] == 'candidate' and saved['success_count'] == 0
        assert result.candidates[0].legacy_id is None
        duplicate = core.persist_legacy_extraction(request)
        assert len(duplicate.candidates) == 0
        core.close()

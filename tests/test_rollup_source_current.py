"""Withdrawn source prose must not return through a derived project summary."""
import pytest

from evolvmem.context_models import ContextSearchRequest
from evolvmem.memory_eligibility import eligible
from evolvmem.project_rollup import ProjectRollupGenerator
from tests.test_context_vector_health import service, base_service, Engine
from tests.test_project_rollup import _make_summary, _ok_response, _SpyLlm
from evolvmem.project_store import ProjectStore


def rollup(service):
    source = _make_summary(service.store, 'evo', 'obsolete-policy')
    with service.store.transaction():
        ProjectStore(service.store._connection(), service.store._require_transaction, generic_names=()).record_trusted_resolution(source, 'evo')
    report = ProjectRollupGenerator(service.config, service.store, llm=lambda _: _ok_response(
        l0='旧汇总独有暗号紫色松果', l1='旧汇总独有暗号紫色松果，需要重新核验。')).rollup_project('evo')
    return source, report.context_id


@pytest.mark.parametrize('change', ['candidate', 'archived', 'moved', 'edited', 'expired', 'unconfirmed_rule'])
def test_stale_rollup_is_held_from_index_search_and_seed_but_history_survives(service, change):
    source, summary = rollup(service)
    assert eligible(service.store, summary)
    with service.store.transaction():
        conn = service.store._connection()
        if change in ('candidate', 'archived'):
            conn.execute('UPDATE context_items SET status=? WHERE id=?', (change, source))
        elif change == 'moved':
            conn.execute("UPDATE context_items SET project='shop' WHERE id=?", (source,))
        elif change == 'expired':
            conn.execute("UPDATE context_items SET expires_at='2000-01-01 00:00:00' WHERE id=?", (source,))
        elif change == 'unconfirmed_rule':
            conn.execute('INSERT INTO learning_memories(item_id,category,payload) VALUES(?,?,?)',
                         (source, 'project_convention', '{"basis":"inferred","evidence":[{"role":"assistant","quote":"旧推断"}]}'))
        else:
            conn.execute("UPDATE context_layers SET content='已经纠正的新规则',content_hash='changed' WHERE item_id=? AND layer='l1'", (source,))
    assert not eligible(service.store, summary)
    assert summary not in {d.item_id for d in service.store.list_vector_documents()}
    assert summary not in {r.id for r in service.search(ContextSearchRequest(query='紫色松果', project='evo'))}
    assert summary not in {r.id for r in service._session_candidates('evo', '紫色松果')[0]}
    assert service._ready_project_summary_l1('evo') == ''
    assert '紫色松果' in service.insights().summary(summary)['l1']


def test_edited_source_regenerates_instead_of_reusing_old_id_hash_or_old_prose(service):
    source, summary = rollup(service)
    with service.store.transaction():
        service.store._connection().execute(
            "UPDATE context_layers SET content='已经纠正的新规则',content_hash='changed' WHERE item_id=? AND layer='l1'", (source,))
    llm = _SpyLlm(_ok_response(l0='依据新规则重新生成的摘要'))
    result = ProjectRollupGenerator(service.config, service.store, llm=llm).rollup_project('evo')
    assert result.status == 'ready'
    assert result.context_id != summary
    assert '已经纠正的新规则' in llm.prompts[0]
    assert '紫色松果' not in llm.prompts[0]
    assert eligible(service.store, result.context_id)

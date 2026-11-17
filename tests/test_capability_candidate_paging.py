from types import SimpleNamespace

from evolvmem.capability_store import CapabilityStore
from evolvmem.capability_retriever import CapabilityRetriever
from tests.test_project_modules import modules, create
from tests.test_capability_provenance import provenance_db, add_memory, unit_source


def test_invalid_first_page_does_not_hide_later_valid_knowledge(provenance_db):
    store = provenance_db
    for i in range(25):
        add_memory(store, title=f'Fact {i:03}')
    valid = add_memory(store, title='Fact 999', source=unit_source(store))
    hits = CapabilityRetriever(SimpleNamespace(store=store)).search('Fact', top_k=1)
    assert [h['memory_id'] for h in hits] == [valid]
    first = CapabilityStore(store).search('Fact', top_k=20)
    second = CapabilityStore(store).search('Fact', top_k=20, offset=20)
    assert len(first) == 20 and len(second) == 6
    assert not set(h['memory_id'] for h in first) & set(h['memory_id'] for h in second)


def test_module_filter_backfills_beyond_first_valid_candidate_page(modules):
    store = modules.store
    for i in range(25):
        add_memory(store, title=f'Fact {i:03}', source=unit_source(store))
    valid = add_memory(store, title='Fact 999', source=unit_source(store))
    module = create(modules)
    source = CapabilityStore(store).sources(valid)[0]['source_id']
    modules.bind_source(module['module_id'], source, quote='user: a fact', role='applicable',
                        decision='manual', explicit=True, expected_revision=0)
    hits = CapabilityRetriever(SimpleNamespace(store=store)).search('Fact', top_k=1, module_id=module['module_id'])
    assert [h['memory_id'] for h in hits] == [valid]


def test_candidate_backfill_has_hard_work_limit(provenance_db, monkeypatch):
    store = provenance_db
    identity = add_memory(store)
    calls = []
    def repeated(self, query, categories=(), top_k=10, *, offset=0):
        calls.append((top_k, offset))
        return [self.get(identity)] * top_k
    monkeypatch.setattr(CapabilityStore, 'search', repeated)
    assert CapabilityRetriever(SimpleNamespace(store=store)).search('Fact', top_k=1) == []
    assert sum(size for size, _ in calls) == 500
    assert max(offset for _, offset in calls) == 480

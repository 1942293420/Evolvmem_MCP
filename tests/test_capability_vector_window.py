"""Vector admission visits ranked windows without weakening source checks."""
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from evolvmem.capability_index import CapabilityIndex
from evolvmem.capability_store import CapabilityMemoryDraft, CapabilityStore
from evolvmem.context_store import ContextStore
from tests.test_capability_index import FakeEngine


def ranked_index(store, config, monkeypatch, count, allowed):
    ids = [CapabilityStore(store).upsert(CapabilityMemoryDraft(
        category='frontend' if i % 2 == 0 else 'backend',
        title=f'window {i}', content=f'procedure {i}')) for i in range(count)]
    calls = []
    def admit(batch):
        assert store._connection().in_transaction
        calls.append(list(batch))
        return set(batch) & allowed(ids)
    index = CapabilityIndex(store, config, FakeEngine(), eligible_ids=admit)
    vectors = SimpleNamespace(search=lambda _embedding, k: [
        {'id': ident, 'distance': position / count} for position, ident in enumerate(ids[:k])])
    @contextmanager
    def frozen():
        yield vectors, {'items': {}}, ()
    monkeypatch.setattr(index, '_snapshot', frozen)
    return index, ids, calls


def test_sufficient_leading_hits_do_not_prove_unused_vector_tail(test_config, monkeypatch):
    with ContextStore(test_config) as store:
        index, ids, calls = ranked_index(store, test_config, monkeypatch, 40, set)
        hits = index.search('procedure', top_k=10)
        assert [r['memory_id'] for r in hits] == ids[:10]
        assert [ident for batch in calls for ident in batch] == ids[:10]
        assert not store._connection().in_transaction


def test_category_and_bad_vector_proof_are_filtered_before_deep_source_reads(test_config, monkeypatch):
    with ContextStore(test_config) as store:
        index, ids, calls = ranked_index(store, test_config, monkeypatch, 40, set)
        @contextmanager
        def frozen():
            yield SimpleNamespace(search=lambda _embedding, k: [
                {'id': ident, 'distance': i / 40} for i, ident in enumerate(ids[:k])]), {'items': {str(ids[0]): {}}}, ()
        monkeypatch.setattr(index, '_snapshot', frozen)
        monkeypatch.setattr(index, '_proof_matches', lambda *args, **kwargs: False)
        hits = index.search('procedure', categories=('frontend',), top_k=3)
        assert [r['memory_id'] for r in hits] == ids[2:8:2]
        checked = [ident for batch in calls for ident in batch]
        assert ids[0] not in checked
        assert not set(ids[1::2]) & set(checked)


@pytest.mark.parametrize('first_allowed', [0, 7, 12, 38, 40])
def test_invalid_ranked_windows_backfill_to_same_bounded_answer(test_config, monkeypatch, first_allowed):
    with ContextStore(test_config) as store:
        index, ids, calls = ranked_index(store, test_config, monkeypatch, 60,
            lambda values: set(values[first_allowed:]))
        hits = index.search('procedure', top_k=10)
        assert [r['memory_id'] for r in hits] == ids[first_allowed:min(first_allowed+10, 40)]
        visited = [ident for batch in calls for ident in batch]
        assert len(visited) == len(set(visited))
        assert len(visited) <= 40
        assert all(len(batch) <= 10 for batch in calls)


def test_batch_failure_never_returns_partially_checked_candidates(test_config, monkeypatch):
    with ContextStore(test_config) as store:
        index, ids, _ = ranked_index(store, test_config, monkeypatch, 40, set)
        def broken(batch):
            raise RuntimeError('source proof unavailable')
        index.eligible_ids = broken
        assert index.search('procedure', top_k=10) == []
        assert not store._connection().in_transaction


@pytest.mark.parametrize('batch_gate', [False, True])
@pytest.mark.parametrize('change', ['content', 'category'])
def test_custom_predicate_cannot_bypass_post_predicate_checks(test_config, change, batch_gate):
    test_config.embedding_dim = 2
    with ContextStore(test_config) as store:
        identity = CapabilityStore(store).upsert(CapabilityMemoryDraft(
            category='backend', title='original title', content='original content'))
        index = CapabilityIndex(store, test_config, FakeEngine())
        try:
            assert index.sync(identity, 'original title\noriginal content') == 'synchronized'
            def mutate(row):
                row[change] = 'changed content' if change == 'content' else 'frontend'
                return True
            index.eligible = mutate
            if batch_gate:
                index.eligible_ids = set
            hits = index.search('procedure', categories=('frontend',) if change == 'category' else (), top_k=1)
            if change == 'content':
                assert hits == []  # The certified vector cannot serve changed text.
            else:
                assert [r['memory_id'] for r in hits] == [identity]
                assert hits[0]['category'] == 'frontend'
        finally:
            index.vector_index.close()


def test_production_vector_budget_backfills_and_stops_before_unused_tail(test_config, monkeypatch):
    with ContextStore(test_config) as store:
        index, ids, calls = ranked_index(store, test_config, monkeypatch, 300,
            lambda values: set(values[50:]))
        hits = index.search('procedure', top_k=60)
        assert [row['memory_id'] for row in hits] == ids[50:110]
        assert [ident for batch in calls for ident in batch] == ids[:120]
        assert all(len(batch) == 20 for batch in calls)


def test_later_window_failure_discards_previously_accumulated_hits(test_config, monkeypatch):
    with ContextStore(test_config) as store:
        index, ids, _ = ranked_index(store, test_config, monkeypatch, 240, set)
        calls = []
        def interrupted(batch):
            calls.append(list(batch))
            if len(calls) == 2:
                raise RuntimeError('source validation failed after initial hits')
            return set(batch)
        index.eligible_ids = interrupted
        assert index.search('procedure', top_k=60) == []
        assert calls == [ids[:20], ids[20:40]]
        assert not store._connection().in_transaction

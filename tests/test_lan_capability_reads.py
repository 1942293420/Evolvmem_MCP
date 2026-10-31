import concurrent.futures
import sqlite3
import threading
import pytest

from tests.test_lan_sharing import lan,add
from tests.test_background_health import capability_setup


def search(adapter,**extra):
    return adapter.call_tool('kane','capability_search',{'query':'orchard',**extra})


def test_capability_read_is_independent_of_business_lock(lan):
    runtime,adapter=lan
    capability_setup(runtime,adapter)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        with adapter.lock:
            future=pool.submit(search,adapter)
            try:result=future.result(timeout=2)
            except concurrent.futures.TimeoutError:pytest.fail('capability read waited for business lock')
    assert result['count']==1,result


def test_slow_capability_read_does_not_block_a_write(lan,monkeypatch):
    from evolvmem.capability_retriever import CapabilityRetriever
    runtime,adapter=lan
    capability_setup(runtime,adapter)
    entered,release=threading.Event(),threading.Event()
    original=CapabilityRetriever.search
    def slow(retriever,*args,**kwargs):
        entered.set()
        assert release.wait(4)
        return original(retriever,*args,**kwargs)
    monkeypatch.setattr(CapabilityRetriever,'search',slow)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        recall=pool.submit(search,adapter)
        assert entered.wait(1)
        try:
            write=pool.submit(add,adapter,'kane','another fact','project:lan:fact:other','independent-write')
            assert 'error' not in write.result(timeout=1)
        finally:release.set()
        assert recall.result(timeout=3)['count']==1


def test_capability_read_owns_store_and_closes_native_index(lan,monkeypatch):
    from evolvmem.capability_retriever import CapabilityRetriever
    runtime,adapter=lan
    resident,_=capability_setup(runtime,adapter)
    observed=[]
    original=CapabilityRetriever.search
    def inspect(retriever,*args,**kwargs):
        service=retriever.service
        conn=service.store._connection()
        assert service.store is not resident
        assert conn.execute('PRAGMA query_only').fetchone()[0]==1
        assert not conn.in_transaction
        with pytest.raises(sqlite3.OperationalError,match='readonly'):
            conn.execute('UPDATE context_items SET access_count=999')
        conn.rollback()  # Failed implicit writes can still begin a transaction.
        observed.append((conn,service.capability_index()))
        return original(retriever,*args,**kwargs)
    monkeypatch.setattr(CapabilityRetriever,'search',inspect)
    assert search(adapter)['count']==1
    assert len(observed)==1
    conn,index=observed[0]
    with pytest.raises(sqlite3.ProgrammingError,match='closed'):conn.execute('SELECT 1')
    assert index.vector_index._index is None


def test_capability_final_snapshot_observes_withdrawal(lan,monkeypatch):
    from evolvmem.capability_retriever import CapabilityRetriever
    runtime,adapter=lan
    store,identity=capability_setup(runtime,adapter)
    original=CapabilityRetriever.search
    def withdraw(retriever,*args,**kwargs):
        result=original(retriever,*args,**kwargs)
        assert len(result)==1
        with store.transaction():
            store._connection().execute("UPDATE context_items SET status='archived' WHERE id=?",(identity,))
        return result
    monkeypatch.setattr(CapabilityRetriever,'search',withdraw)
    result=search(adapter)
    assert result.get('error') or result['count']==0,result


def test_invalid_final_structure_keeps_error_contract_and_exit_health(lan):
    runtime,adapter=lan
    store,identity=capability_setup(runtime,adapter)
    with store.transaction():
        store._connection().execute("UPDATE context_items SET tags='{}' WHERE id=?",(identity,))
    result=search(adapter)
    assert result.get('error')=='context_unavailable',result
    assert 'hard_gate' in result['health']


def test_vector_only_hit_uses_owned_index_without_saving_or_closing_engine(lan,monkeypatch):
    import numpy as np
    from evolvmem.capability_index import CapabilityIndex
    from evolvmem.capability_retriever import CapabilityRetriever
    from evolvmem.capability_recall_eligibility import eligible_ids
    from types import SimpleNamespace
    runtime,adapter=lan
    store,_=capability_setup(runtime,adapter)
    config=runtime.server_for('kane').config
    class Engine:
        is_loaded=True
        queries=0
        def encode_document(self,text):return np.ones(config.embedding_dim,dtype=np.float32)
        def encode_query(self,text):
            self.queries+=1
            return self.encode_document(text)
        def close(self):pytest.fail('request closed shared engine')
    engine=Engine()
    writer=CapabilityIndex(store,config,engine,eligible_ids=lambda ids:eligible_ids(store,ids))
    identity=store._connection().execute('SELECT memory_id FROM capability_memories').fetchone()[0]
    assert writer.sync(identity,'unused')=='synchronized'
    original=CapabilityRetriever(SimpleNamespace(store=store,config=config,embedding_engine=engine,capability_index=lambda:writer))
    baseline=original.validate_current_results(original.search('semantically-related-only'))
    assert [row['memory_id'] for row in baseline]==[identity]
    adapter._capability_reader._engines['kane']=engine
    for method in ('sync','reconcile'):
        monkeypatch.setattr(CapabilityIndex,method,lambda *a,**kw:pytest.fail('read wrote index'))
    from evolvmem.vector_index import VectorIndex
    monkeypatch.setattr(VectorIndex,'save',lambda *a,**kw:pytest.fail('read saved index'))
    try:
        result=search(adapter,query='semantically-related-only')
        assert result['count']==1,result
        fields=('memory_id','title','content','source_projects','modules','vector_score')
        assert [{k:r.get(k) for k in fields} for r in result['results']]==[{k:r.get(k) for k in fields} for r in baseline]
        assert engine.queries==2
    finally:writer.vector_index.close()


def test_capability_reader_limit_is_shared_across_adapters(lan):
    from evolvmem.lan_tools import LanTools
    from evolvmem.lan_capability_reads import MAX_READERS
    runtime,first=lan
    second=LanTools(runtime)
    capability_setup(runtime,first)
    slots=first._capability_reader._slots
    for _ in range(MAX_READERS):assert slots.acquire(blocking=False)
    try:assert search(second).get('error')=='capability_read_busy'
    finally:
        for _ in range(MAX_READERS):slots.release()
    assert search(second)['count']==1


def test_capability_exit_health_rejects_prior_candidates(lan,monkeypatch):
    from evolvmem.capability_retriever import CapabilityRetriever
    runtime,adapter=lan
    capability_setup(runtime,adapter)
    original=CapabilityRetriever.search
    def close_health(retriever,*args,**kwargs):
        results=original(retriever,*args,**kwargs)
        runtime._background_health.close()
        return results
    monkeypatch.setattr(CapabilityRetriever,'search',close_health)
    assert search(adapter).get('error')=='health_closed'


def test_config_drift_cannot_be_accepted_by_refreshing_existing_health(lan):
    runtime,adapter=lan
    capability_setup(runtime,adapter)
    runtime.server_for('kane').config.fts_top_k+=1
    manager=runtime._background_health
    assert search(adapter).get('error')=='health_boundary_changed'
    manager._next.clear()
    manager.refresh('kane')
    manager.run_once()
    assert search(adapter).get('error')=='health_boundary_changed'


def test_route_cannot_fall_back_to_resident_state_after_bypassing_lock(lan,monkeypatch):
    runtime,adapter=lan
    capability_setup(runtime,adapter)
    original=adapter._call_tool
    def dispatch(*args,**kwargs):
        runtime._background_health.active=False
        assert kwargs['independent_capability'] is True
        return original(*args,**kwargs)
    monkeypatch.setattr(adapter,'_call_tool',dispatch)
    monkeypatch.setattr(runtime.server_for('kane'),'_contract_view',lambda:pytest.fail('unlocked resident fallback'))
    assert search(adapter)['count']==1


def test_capability_read_does_not_see_resident_uncommitted_withdrawal(lan):
    runtime,adapter=lan
    store,identity=capability_setup(runtime,adapter)
    conn=store._connection()
    conn.execute('BEGIN IMMEDIATE')
    try:
        conn.execute("UPDATE context_items SET status='archived' WHERE id=?",(identity,))
        assert search(adapter)['count']==1
        assert conn.in_transaction
    finally:conn.rollback()


@pytest.mark.parametrize('method,args',[('encode',('text',)),('encode_query',('text',)),('encode_document',('text',)),('rerank',('text',['document']))])
def test_queued_embedding_cannot_run_after_engine_closed(method,args):
    from evolvmem.lan_runtime import _SerializedEmbeddingEngine
    class Engine:
        def close(self):pass
        def __getattr__(self,name):return lambda *args:pytest.fail('used closed underlying engine')
    engine=_SerializedEmbeddingEngine(Engine())
    entered=threading.Event()
    def encode():
        entered.set()
        return getattr(engine,method)(*args)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        with engine._lock:
            future=pool.submit(encode)
            assert entered.wait(1)
            engine.close()
        with pytest.raises(RuntimeError,match='engine is closed'):future.result(timeout=1)

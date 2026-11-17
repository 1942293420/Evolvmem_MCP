"""Native synthetic LAN regressions; root runs these with real dependencies."""
import concurrent.futures
import sqlite3
import threading
import pytest

from tests.test_lan_sharing import lan,add,publish
from tests.test_experience_background_admission import baseline,seed,recall


def test_experience_read_does_not_wait_for_business_lock(lan):
    runtime,adapter=lan
    baseline(runtime,'kane')
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        with adapter.lock:
            future=pool.submit(recall,adapter)
            try:result=future.result(timeout=2)
            except concurrent.futures.TimeoutError:pytest.fail('experience waited for business lock')
    assert 'error' not in result,result


def test_experience_read_owns_query_only_snapshot_and_never_resident_store(lan,monkeypatch):
    from evolvmem.experience_service import ExperienceService
    runtime,adapter=lan
    baseline(runtime,'kane')
    resident=runtime.server_for('kane').context_service.store
    original=ExperienceService.recall
    connections=[]
    def inspect(service,*args,**kwargs):
        assert service.store is not resident,'recall borrowed resident store'
        conn=service.store._connection()
        assert conn.execute('PRAGMA query_only').fetchone()[0]==1
        assert conn.in_transaction
        connections.append(conn)
        return original(service,*args,**kwargs)
    monkeypatch.setattr(ExperienceService,'recall',inspect)
    assert 'error' not in recall(adapter)
    assert len(connections)==1
    with pytest.raises(sqlite3.ProgrammingError):connections[0].execute('SELECT 1')


def test_direct_evidence_change_after_computation_rejects_old_result(lan,tmp_path,monkeypatch):
    from evolvmem.experience_service import ExperienceService
    runtime,adapter=lan
    resident,saved=seed(runtime,tmp_path)
    baseline(runtime,'kane')
    original=ExperienceService.recall
    def withdraw(service,*args,**kwargs):
        result=original(service,*args,**kwargs)
        assert [r['id'] for r in result['results']]==[saved['id']]
        with resident.store.transaction():
            resident._conn().execute("UPDATE context_evidence SET outcome='contradicted' WHERE item_id=?",(saved['id'],))
        return result
    monkeypatch.setattr(ExperienceService,'recall',withdraw)
    result=recall(adapter,constraints={'size':'large'})
    assert result.get('error')=='experience_read_changed',result
    assert 'results' not in result

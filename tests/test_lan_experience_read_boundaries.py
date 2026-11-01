"""Independent recall lifecycle, identical DTO and request-change closure."""
import concurrent.futures
import sqlite3
import threading
import pytest
from tests.test_lan_sharing import lan,add,publish
from tests.test_experience_background_admission import baseline,seed,recall


@pytest.mark.parametrize('space,names',[('personal',('kane',)),('public',('public',)),('both',('kane','public'))])
def test_original_personal_public_result_signatures_and_no_writes(lan,tmp_path,monkeypatch,space,names):
    from datetime import datetime,timezone
    from evolvmem.context_retriever import ContextRetriever
    from evolvmem.vector_index import VectorIndex
    runtime,adapter=lan
    seed(runtime,tmp_path)
    publish(adapter,add(adapter),title='orchard pagination',summary='shared orchard pagination instructions')
    # The existing public DTO exposes the unrounded continuous recency score.
    # Both real paths must observe the same instant for exact DTO comparison.
    moment=datetime.now(timezone.utc)
    monkeypatch.setattr(ContextRetriever,'_now',lambda self:moment)
    expected=recall(adapter,space,constraints={'size':'large'})
    baseline(runtime,*names)
    before={n:runtime.server_for('jiangli','public').context_service.store._connection().total_changes
            if n=='public' else runtime.server_for(n).context_service.store._connection().total_changes for n in names}
    monkeypatch.setattr(VectorIndex,'save',lambda *a:pytest.fail('readonly recall saved vector'))
    actual=recall(adapter,space,constraints={'size':'large'})
    assert actual==expected
    assert all(r['space']=='public' and r['ref'].startswith('public:') for r in actual['shared_knowledge'])
    after={n:runtime.server_for('jiangli','public').context_service.store._connection().total_changes
            if n=='public' else runtime.server_for(n).context_service.store._connection().total_changes for n in names}
    assert after==before


def test_slow_recall_allows_write_and_rejects_changed_snapshot(lan,monkeypatch):
    from evolvmem.experience_service import ExperienceService
    runtime,adapter=lan
    baseline(runtime,'kane')
    entered,release=threading.Event(),threading.Event()
    original=ExperienceService.recall
    def slow(service,*a,**kw):
        entered.set();assert release.wait(5)
        return original(service,*a,**kw)
    monkeypatch.setattr(ExperienceService,'recall',slow)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        reading=pool.submit(recall,adapter)
        assert entered.wait(2)
        try:
            writing=pool.submit(add,adapter,'kane','new orchard fact','project:lan:fact:new','read-concurrency')
            assert 'error' not in writing.result(timeout=2)
        finally:release.set()
        assert reading.result(timeout=3)['error']=='experience_read_changed'


def test_reader_permits_shared_with_capability_and_other_adapters(lan):
    from evolvmem.lan_tools import LanTools
    from evolvmem.lan_capability_reads import MAX_READERS
    runtime,first=lan
    second=LanTools(runtime)
    baseline(runtime,'kane')
    slots=first._capability_reader._slots
    assert first._experience_reader._slots is second._experience_reader._slots is slots
    for _ in range(MAX_READERS):assert slots.acquire(blocking=False)
    try:assert recall(second)['error']=='experience_read_busy'
    finally:
        for _ in range(MAX_READERS):slots.release()
    assert 'error' not in recall(second)


def test_public_withdrawal_after_compute_rejects(lan,monkeypatch):
    runtime,adapter=lan
    public=publish(adapter,add(adapter),title='orchard pagination',summary='shared orchard pagination')
    baseline(runtime,'public')
    original=adapter.sharing.search
    def withdraw(*a,**kw):
        result=original(*a,**kw)
        assert result
        with adapter.sharing.store.transaction():
            adapter.sharing.conn.execute('UPDATE lan_publications SET active=0 WHERE context_id=?',(public['id'],))
        return result
    monkeypatch.setattr(adapter.sharing,'search',withdraw)
    assert recall(adapter,'public')['error']=='experience_read_changed'


def test_other_unread_namespace_commit_does_not_reject(lan,monkeypatch):
    from evolvmem.experience_service import ExperienceService
    runtime,adapter=lan
    baseline(runtime,'kane')
    original=ExperienceService.recall
    def unrelated(service,*a,**kw):
        result=original(service,*a,**kw)
        other=runtime.server_for('jiangli').context_service.store
        with other.transaction():
            other._connection().execute('UPDATE context_health_generation SET generation=generation+1')
        return result
    monkeypatch.setattr(ExperienceService,'recall',unrelated)
    assert 'error' not in recall(adapter)


@pytest.mark.parametrize('change',['health_close','runtime_close','config'])
def test_exit_rejection_and_permit_release(lan,monkeypatch,change):
    from evolvmem.experience_service import ExperienceService
    runtime,adapter=lan
    baseline(runtime,'kane')
    original=ExperienceService.recall
    def changed(service,*a,**kw):
        result=original(service,*a,**kw)
        if change=='health_close':runtime._background_health.close()
        elif change=='runtime_close':runtime._closed=True
        else:runtime.server_for('kane').config.context_vectors_required=True
        return result
    monkeypatch.setattr(ExperienceService,'recall',changed)
    try:
        result=recall(adapter)
        assert 'error' in result and 'results' not in result
        if change=='config':assert result['error']=='health_boundary_changed'
        slots=adapter._experience_reader._slots
        assert slots.acquire(blocking=False)
        assert slots.acquire(blocking=False)
        slots.release();slots.release()
    finally:
        if change=='runtime_close':runtime._closed=False  # allow fixture's real close


def test_native_image_is_request_owned_and_shared_engine_not_closed(lan,tmp_path,monkeypatch):
    from evolvmem.context_retriever import ContextRetriever
    runtime,adapter=lan
    seed(runtime,tmp_path)
    baseline(runtime,'kane')
    resident=runtime.server_for('kane').context_service
    if resident.embedding_engine is not None:
        monkeypatch.setattr(resident.embedding_engine,'close',lambda:pytest.fail('closed shared engine'))
    observed=[]
    original=ContextRetriever.search
    def inspect(retriever,*a,**kw):
        assert retriever.vector_index is not resident.vector_index
        assert retriever.embedding_engine is resident.embedding_engine
        assert retriever.vector_index._shared_cache is None
        observed.append(retriever.vector_index)
        return original(retriever,*a,**kw)
    monkeypatch.setattr(ContextRetriever,'search',inspect)
    assert 'error' not in recall(adapter,constraints={'size':'large'})
    assert observed and all(index._index is None for index in observed)
    # Avoid changing the fixture's later legitimate runtime.close behavior.
    monkeypatch.undo()


def test_selected_expiry_at_final_time_without_commit_is_rejected(lan,tmp_path,monkeypatch):
    from datetime import datetime,timedelta,timezone
    from evolvmem.lan_experience_snapshot import ExperienceSnapshot
    runtime,adapter=lan
    service,saved=seed(runtime,tmp_path)
    expiry=(datetime.now(timezone.utc)+timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
    with service.store.transaction():
        service._conn().execute('UPDATE context_items SET expires_at=? WHERE id=?',(expiry,saved['id']))
    baseline(runtime,'kane')
    original=ExperienceSnapshot.validate
    monkeypatch.setattr(ExperienceSnapshot,'validate',lambda self,ids,**kw:original(self,ids,now=expiry,**kw))
    assert recall(adapter,constraints={'size':'large'})['error']=='experience_read_changed'


def test_selected_effective_until_crossing_without_commit_is_rejected(lan,tmp_path,monkeypatch):
    from datetime import datetime,timedelta,timezone
    from evolvmem.lan_experience_snapshot import ExperienceSnapshot
    runtime,adapter=lan
    service,saved=seed(runtime,tmp_path)
    until=(datetime.now(timezone.utc)+timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S')
    with service.store.transaction():
        service._conn().execute('UPDATE context_items SET effective_until=? WHERE id=?',(until,saved['id']))
    baseline(runtime,'kane')
    assert [row['id'] for row in recall(adapter,constraints={'size':'large'})['results']]==[saved['id']]
    original=ExperienceSnapshot.validate
    monkeypatch.setattr(ExperienceSnapshot,'validate',lambda self,ids,**kw:original(self,ids,now=until,**kw))
    assert recall(adapter,constraints={'size':'large'})['error']=='experience_read_changed'


def test_db_replacement_during_request_fails_closed(lan,tmp_path,monkeypatch):
    import os,sys
    from evolvmem.experience_service import ExperienceService
    if sys.platform=='win32':pytest.skip('native Linux rename-while-open test')
    runtime,adapter=lan
    baseline(runtime,'kane')
    path=runtime.server_for('kane').config.db_path
    replacement=tmp_path/'replacement.db'
    conn=sqlite3.connect(replacement);conn.execute('CREATE TABLE sentinel(id INTEGER)');conn.close()
    original=ExperienceService.recall
    def replace(service,*a,**kw):
        result=original(service,*a,**kw)
        os.replace(replacement,path)
        return result
    monkeypatch.setattr(ExperienceService,'recall',replace)
    assert recall(adapter)['error']=='experience_read_changed'


def test_cleanup_error_still_releases_shared_permit(lan,monkeypatch):
    from evolvmem.lan_experience_snapshot import ExperienceSnapshot
    runtime,adapter=lan
    baseline(runtime,'kane')
    original=ExperienceSnapshot.close
    def fail_after_close(self):
        original(self)
        raise RuntimeError('synthetic cleanup failure')
    monkeypatch.setattr(ExperienceSnapshot,'close',fail_after_close)
    assert 'error' in recall(adapter)
    slots=adapter._experience_reader._slots
    assert slots.acquire(blocking=False) and slots.acquire(blocking=False)
    slots.release();slots.release()


def test_synchronous_cancellation_closes_owned_connection_and_releases_permit(lan,monkeypatch):
    from evolvmem.experience_service import ExperienceService
    class Cancelled(BaseException):pass
    runtime,adapter=lan
    baseline(runtime,'kane')
    connections=[]
    def cancel(service,*a,**kw):
        connections.append(service.store._connection())
        raise Cancelled()
    monkeypatch.setattr(ExperienceService,'recall',cancel)
    with pytest.raises(Cancelled):recall(adapter)
    assert len(connections)==1
    with pytest.raises(sqlite3.ProgrammingError):connections[0].execute('SELECT 1')
    slots=adapter._experience_reader._slots
    assert slots.acquire(blocking=False) and slots.acquire(blocking=False)
    slots.release();slots.release()

"""Native same-namespace concurrency cases for selected-result validation."""
import pytest
from tests.test_lan_sharing import lan,add,publish
from tests.test_experience_background_admission import baseline,seed,recall


@pytest.mark.parametrize('changed_table',['item','evidence'])
def test_unrelated_same_namespace_commit_keeps_selected_result(lan,tmp_path,monkeypatch,changed_table):
    from datetime import datetime,timezone
    from evolvmem.context_retriever import ContextRetriever
    from evolvmem.experience_service import ExperienceService
    runtime,adapter=lan
    resident,saved=seed(runtime,tmp_path)
    other=add(adapter,'kane','unrelated banana inventory','project:lan:fact:unrelated','unrelated-seed')['context_id']
    with resident.store.transaction():
        resident._conn().execute("INSERT INTO context_evidence(item_id,outcome,note,observed_at,created_at) "
                                "VALUES(?,'success','original unrelated evidence',datetime('now'),datetime('now'))",(other,))
    baseline(runtime,'kane')
    moment=datetime.now(timezone.utc)
    monkeypatch.setattr(ContextRetriever,'_now',lambda self:moment)
    expected=recall(adapter,constraints={'size':'large'})
    assert [row['id'] for row in expected['results']]==[saved['id']]
    original=ExperienceService.recall
    def commit_unrelated(service,*a,**kw):
        result=original(service,*a,**kw)
        assert [row['id'] for row in result['results']]==[saved['id']]
        with resident.store.transaction():
            if changed_table=='item':
                resident._conn().execute("UPDATE context_items SET importance=importance+.1 WHERE id=?",(other,))
            else:
                resident._conn().execute("UPDATE context_evidence SET note='changed unrelated evidence' WHERE item_id=?",(other,))
        return result
    monkeypatch.setattr(ExperienceService,'recall',commit_unrelated)
    assert recall(adapter,constraints={'size':'large'})==expected


def test_selected_actual_body_change_with_unchanged_hash_rejects(lan,tmp_path,monkeypatch):
    from evolvmem.experience_service import ExperienceService
    runtime,adapter=lan
    resident,saved=seed(runtime,tmp_path);baseline(runtime,'kane')
    original=ExperienceService.recall
    def change_body(service,*a,**kw):
        result=original(service,*a,**kw)
        assert [row['id'] for row in result['results']]==[saved['id']]
        with resident.store.transaction():
            resident._conn().execute("UPDATE context_layers SET content=content||' changed' WHERE item_id=? AND layer='l2'",(saved['id'],))
        return result
    monkeypatch.setattr(ExperienceService,'recall',change_body)
    assert recall(adapter,constraints={'size':'large'})['error']=='experience_read_changed'


def test_health_exit_without_commit_still_checks_final_time(lan,tmp_path,monkeypatch):
    from datetime import datetime,timedelta,timezone
    from evolvmem import lan_experience_snapshot as snapshot_module
    runtime,adapter=lan
    resident,saved=seed(runtime,tmp_path)
    end=datetime.now(timezone.utc)+timedelta(hours=1)
    with resident.store.transaction():
        resident._conn().execute('UPDATE context_items SET effective_until=? WHERE id=?',
                                (end.strftime('%Y-%m-%d %H:%M:%S'),saved['id']))
    health=baseline(runtime,'kane');original=health.status;calls=[]
    class FinalTime:
        @staticmethod
        def now(zone):return end
    def checked(name):
        result=original(name);calls.append(name)
        if len(calls)==2:monkeypatch.setattr(snapshot_module,'datetime',FinalTime)
        return result
    monkeypatch.setattr(health,'status',checked)
    result=recall(adapter,constraints={'size':'large'})
    assert calls==['kane','kane']
    assert result.get('error')=='experience_read_changed',result


def test_health_exit_unrelated_commit_cannot_hide_native_file_change(lan,tmp_path,monkeypatch):
    runtime,adapter=lan
    resident,saved=seed(runtime,tmp_path)
    other=add(adapter,'kane','unrelated banana','project:lan:fact:other','other-native-fence')['context_id']
    health=baseline(runtime,'kane');original=health.status;calls=[]
    initial=recall(adapter,constraints={'size':'large'})
    assert [row['id'] for row in initial['results']]==[saved['id']]
    def checked(name):
        result=original(name);calls.append(name)
        if len(calls)==2:
            with resident.store.transaction():
                resident._conn().execute('UPDATE context_items SET importance=importance+.1 WHERE id=?',(other,))
            path=resident.config.context_vector_path
            path.with_suffix(path.suffix+'.dirty').write_text('synthetic final drift',encoding='utf-8')
        return result
    monkeypatch.setattr(health,'status',checked)
    result=recall(adapter,constraints={'size':'large'})
    assert calls==['kane','kane']
    assert result.get('error')=='experience_read_changed',result


def test_public_fact_survives_unrelated_public_database_commit(lan,monkeypatch):
    from datetime import datetime,timezone
    from evolvmem.context_retriever import ContextRetriever
    runtime,adapter=lan
    publication=publish(adapter,add(adapter),title='orchard pagination',summary='shared orchard pagination instructions')
    public=runtime.server_for('jiangli','public')
    unrelated=public.handle_tool_call('memory_add',{'key':'project:lan:fact:other',
                                                 'value':'unpublished unrelated banana'})['context_id']
    baseline(runtime,'public')
    moment=datetime.now(timezone.utc)
    monkeypatch.setattr(ContextRetriever,'_now',lambda self:moment)
    expected=recall(adapter,'public')
    assert [row['id'] for row in expected['shared_knowledge']]==[publication['id']]
    original=adapter.sharing.search
    def change_unrelated(*a,**kw):
        result=original(*a,**kw)
        assert [row['id'] for row in result]==[publication['id']]
        with public.context_service.store.transaction():
            public.context_service.store._connection().execute('UPDATE context_items SET importance=importance+.1 WHERE id=?',(unrelated,))
        return result
    monkeypatch.setattr(adapter.sharing,'search',change_unrelated)
    assert recall(adapter,'public')==expected

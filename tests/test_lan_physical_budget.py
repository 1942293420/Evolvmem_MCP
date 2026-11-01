"""Native LAN/SQLite bounded physical checks. Synthetic data, no production calls."""
from contextlib import closing
from types import SimpleNamespace
import sqlite3
import time
import pytest
from evolvmem.context_service import ContextService
from evolvmem.physical_integrity import PhysicalProofOwner, PhysicalScanTimeout, PhysicalScanBusy
from tests.test_lan_sharing import lan
from tests.test_lan_physical_owner import manager_for, claim


def database(tmp_path,rows=1):
    path=tmp_path/'physical-budget.db';conn=sqlite3.connect(path)
    conn.execute('CREATE TABLE sample(id INTEGER PRIMARY KEY,value TEXT)')
    conn.executemany('INSERT INTO sample VALUES(?,?)',((i,'synthetic')for i in range(rows)))
    conn.commit();cookie=conn.execute('PRAGMA schema_version').fetchone()[0];conn.close()
    st=path.stat();return path,(str(path.resolve()),st.st_dev,st.st_ino,cookie)


def test_actual_quick_check_vm_is_interrupted_and_probe_closed(tmp_path,monkeypatch):
    path,expected=database(tmp_path,4000);connect=sqlite3.connect;seen=[];errors=[];clock=[0.]
    class Conn(sqlite3.Connection):
        def __init__(self,*a,**k):super().__init__(*a,**k);self.closed=False;seen.append(self)
        def execute(self,*a,**k):
            try:return super().execute(*a,**k)
            except sqlite3.Error as exc:errors.append(exc.sqlite_errorcode);raise
        def close(self):super().close();self.closed=True
    monkeypatch.setattr(sqlite3,'connect',lambda *a,**k:connect(*a,**k,factory=Conn))
    def now():clock[0]+=.1;return clock[0]
    with pytest.raises(PhysicalScanTimeout):
        ContextService._scan_integrity_readonly(path,expected,deadline=1.,clock=now)
    assert sqlite3.SQLITE_INTERRUPT in errors
    assert len(seen)==1 and seen[0].closed


def test_actual_lock_conflict_is_busy_not_corruption(tmp_path,monkeypatch):
    path,expected=database(tmp_path);connect=sqlite3.connect;writer=connect(path)
    seen=[];codes=[]
    class Conn(sqlite3.Connection):
        def __init__(self,*a,**k):super().__init__(*a,**k);self.closed=False;seen.append(self)
        def execute(self,*a,**k):
            try:return super().execute(*a,**k)
            except sqlite3.Error as exc:codes.append(exc.sqlite_errorcode);raise
        def close(self):super().close();self.closed=True
    writer.execute('BEGIN EXCLUSIVE')
    monkeypatch.setattr(sqlite3,'connect',lambda *a,**k:connect(*a,**k,factory=Conn))
    try:
        with pytest.raises(PhysicalScanBusy):
            ContextService._scan_integrity_readonly(path,expected,deadline=time.monotonic()+1.)
    finally:writer.rollback();writer.close()
    assert sqlite3.SQLITE_BUSY in codes
    assert len(seen)==1 and seen[0].closed


def test_native_late_ok_has_no_success_stamp_and_timeout_can_recover():
    clock=[0.];identity=('synthetic',1,2,3);owner=PhysicalProofOwner(identity,'fixed',refresh=lambda:None,clock=lambda:clock[0])
    def late():clock[0]=301.;return ()
    assert owner.check(identity,'fixed',300.,late,current_config=lambda:'fixed',deadline=300.)==(('quick_check_timeout',),None)
    view=owner.observe(identity,'fixed',300.);assert view.state=='timeout' and view.checked_at is None
    assert owner.check(identity,'fixed',300.,lambda:(),current_config=lambda:'fixed',deadline=601.)==((),301.)


@pytest.mark.parametrize('fault,code,gate',[(PhysicalScanTimeout,'quick_check_timeout','health_physical_timeout'),(PhysicalScanBusy,'quick_check_busy','health_physical_busy')])
def test_native_attempt_failure_invalidates_health_cache_and_cannot_bypass_transaction(lan,monkeypatch,fault,code,gate):
    from evolvmem import background_health,request_health
    runtime,_=lan;clock=[background_health.time.monotonic()]
    monkeypatch.setattr(background_health,'time',SimpleNamespace(monotonic=lambda:clock[0]))
    manager=manager_for(runtime);server=runtime.server_for('kane');service=server.context_service
    original=server._evaluate_live_status;evaluations=[]
    def evaluate():evaluations.append(True);return original()
    monkeypatch.setattr(server,'_evaluate_live_status',evaluate)
    try:
        claim(manager);manager.refresh('kane');assert manager.run_once()
        clock[0]+=181.
        request_health._entry(server).snapshot=None
        assert server._live_status().ready
        assert server._live_status().ready
        count=len(evaluations);token=service._integrity_cache_state()
        def fail(path,expected,**kwargs):
            assert kwargs['deadline']==clock[0]+300. and callable(kwargs['cancel'])
            assert kwargs['clock']()==clock[0]
            raise fault()
        monkeypatch.setattr(ContextService,'_scan_integrity_readonly',staticmethod(fail))
        with closing(manager._connect('kane'))as conn:
            expected=(*manager._identities['kane'],conn.execute('PRAGMA schema_version').fetchone()[0])
        assert manager._physical_result('kane',service.config.db_path,expected)==((code,),None)
        assert service._integrity_cache_state()!=token
        assert manager.status('kane')['hard_gate']==gate
        assert not server._live_status().ready and len(evaluations)>count
        def forbidden():raise AssertionError('transaction bypassed owner withdrawal')
        monkeypatch.setattr(service,'_quick_check_diagnostics',forbidden)
        with service.store.transaction():assert service._integrity_diagnostics()==(code,)
    finally:manager.close()


@pytest.mark.parametrize('fault,gate',[(PhysicalScanTimeout,'health_physical_timeout'),(PhysicalScanBusy,'health_physical_busy')])
def test_native_failed_attempt_skips_semantic_and_releases_worker_for_small_library(lan,monkeypatch,fault,gate):
    runtime,_=lan;manager=manager_for(runtime);bad=runtime.server_for('kane').config.db_path
    original_scan=ContextService._scan_integrity_readonly;original_semantic=ContextService.evaluated_status;scans=[];semantics=[]
    def scan(path,expected,**kwargs):
        scans.append(path)
        if path==bad:raise fault()
        return original_scan(path,expected,**kwargs)
    def semantic(service,*a,**k):semantics.append(service.config.db_path);return original_semantic(service,*a,**k)
    monkeypatch.setattr(ContextService,'_scan_integrity_readonly',staticmethod(scan))
    monkeypatch.setattr(ContextService,'evaluated_status',semantic)
    try:
        claim(manager);manager.refresh('kane');manager.refresh('public')
        assert manager.run_once();assert manager.status('kane')['hard_gate']==gate
        assert bad in scans and bad not in semantics and manager._running is None
        assert manager.run_once();assert manager.status('public')['hard_gate']=='allowed'
        assert runtime.server_for('kane','public').config.db_path in semantics
    finally:manager.close()

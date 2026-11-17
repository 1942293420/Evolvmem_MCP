from contextlib import contextmanager
from types import SimpleNamespace
import pytest

from evolvmem.config import Config
from evolvmem.library_host import LibraryHost
from evolvmem.library_catalog import ControlBinding
from evolvmem.library_foundation import ConnectionBinding, FoundationError
from test_history_repository import LIB,PROJECT,CONTRACT,session


def host(**options):
    return LibraryHost(control_binding=ControlBinding('localhost',5432,'control','reader','control'),
        content_bindings=(ConnectionBinding('one',LIB,LIB,LIB,LIB,'localhost',5432,'content','runtime','content'),),
        control_connector=lambda **k:None,content_connector=lambda **k:None,secret_resolver=lambda ref:'synthetic',
        config=Config(),model_contract=CONTRACT.key,enabled=True,**options)


def test_configured_24_request_host_admits_20_two_library_requests():
    actual=host(max_requests=24)
    groups=[]
    try:
        for _ in range(20):groups.append(actual._read_budget.reserve(2))
        assert len(groups)==20
    finally:actual.close()


def test_default_host_retains_16_branch_bound():
    actual=host()
    try:
        for _ in range(8):actual._read_budget.reserve(2)
        with pytest.raises(FoundationError,match='parallel_read_busy'):actual._read_budget.reserve(1)
    finally:actual.close()


@pytest.mark.parametrize('writer_backend',('postgres','sqlite'))
def test_projection_detects_real_history_before_project_enumeration(monkeypatch,writer_backend):
    import evolvmem.pg_vector_port.worker as worker
    import evolvmem.pg_vector_port.runtime as runtime
    provider=SimpleNamespace(contract=CONTRACT,encode_query=lambda *a:None,close=lambda:None)
    actual=host(recall_enabled=True,embedding_provider=provider)
    read,_,conn=session({'history_readiness':((True,),)})
    read.route.library.writer_backend=writer_backend
    guard=SimpleNamespace(begin_authority=lambda:None,_unhealthy=SimpleNamespace(is_set=lambda:False))
    routes=[]
    def plan(identity,tool,*,library_id):
        routes.append(tool)
        return SimpleNamespace(library=read.route.library,identity=identity,tool=tool,
            action='read' if tool=='context_read' else 'write')
    state=SimpleNamespace(router=SimpleNamespace(plan=plan),guard=guard,
        manager=SimpleNamespace(run=lambda route,fn:fn(read)))
    @contextmanager
    def request_scope(**kwargs):yield guard
    actual.request_scope=request_scope;actual.authenticate=lambda token:object();actual._state=lambda:state
    class Service:
        def __init__(self,factory,**kwargs):self.factory=factory
        def start(self):pass
        def close(self,**kwargs):pass
    class Port:
        def __init__(self,manager,read_routes,contract,*,project_key,embedding_provider):
            self.project_key=project_key
    monkeypatch.setattr(worker,'ProjectionService',Service)
    monkeypatch.setattr(runtime,'VectorPort',Port)
    actual.start_projection(token_loader=lambda:'synthetic',library_ids=(LIB,))
    try:
        if writer_backend=='postgres':
            with actual._projection_worker.factory() as (port,write):
                assert port.project_key is None and write.action=='write'
        else:
            with pytest.raises(FoundationError,match='history_projection_routing_incompatible'):
                with actual._projection_worker.factory():pass
        assert all('list_recall_projects' not in sql for sql,_ in conn.calls)
    finally:actual.close()

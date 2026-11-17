"""Actual application chain with explicit SQL-driver ports and SQLite oracle."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import importlib.util

from tests.pg_context_read_slice.tests.driver_port import Port, TOKEN, L, platform_shim
platform_shim()

from evolvmem.config import Config
from evolvmem.context_models import ContextContentType, ContextSearchRequest
from evolvmem.context_store import ContextStore
from evolvmem.context_retriever import ContextRetriever
from evolvmem.pg_business_core.executor import PgBusinessExecutor

NOW = datetime(2030,1,1,tzinfo=timezone.utc)


class Fixture:
    def __init__(self):
        self.port=Port();self.patch=self.port.driver_patch();self.patch.start()
        self.host=self.port.options(enabled=True)
        self.config=Config(apply_environment=False)
        self.temp=tempfile.TemporaryDirectory()
        self.oracle=ContextStore(Config(data_dir=Path(self.temp.name),apply_environment=False))
        self.oracle.initialize()
        self.now=NOW
        self.counter=0

    def close(self):
        self.oracle.close();self.temp.cleanup();self.host.close();self.patch.stop();self.port.close()

    def write(self,tool,args):
        self.counter+=1
        with self.host.request_scope():
            identity=self.host.authenticate(TOKEN);state=self.host._state()
            route=state.router.plan(identity,tool,library_id=L)
            executor=PgBusinessExecutor(state.manager,self.config,model_contract='synthetic-reviewed-contract',semantic_merge_enabled=True)
            return executor.mutate(route,args,request_id='slice-write-'+str(self.counter))

    def add(self,key,value):
        return self.write('memory_add',{'key':key,'value':value,'tier':'reference'})

    def mirror(self):
        """Synthetic rows only, copied into actual SQLite schema/FTS triggers."""
        source=self.port.databases['content_0'][1];target=self.oracle._connection()
        target.execute('BEGIN')
        target.execute('PRAGMA defer_foreign_keys=ON')
        for table in ('context_items','context_layers','context_sources'):
            cursor=source.execute('SELECT * FROM '+table)
            names=[d[0] for d in cursor.description]
            actual={r[1] for r in target.execute('PRAGMA table_info('+table+')')}
            keep=[name for name in names if name in actual]
            target.execute('DELETE FROM '+table)
            for values in cursor.fetchall():
                row=dict(zip(names,values))
                if table=='context_items' and row['experience_payload'] is None:
                    row['experience_payload']='{}'  # ordinary empty domain, SQLite schema default
                target.execute('INSERT INTO '+table+'('+','.join(keep)+') VALUES('+','.join('?' for _ in keep)+')',tuple(row[k] for k in keep))
        target.commit()
        if target.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise AssertionError('synthetic oracle links are incomplete')

    def request(self,query='synthetic',top_k=5):
        return ContextSearchRequest(query,top_k=top_k,cross_project=True,
            content_types=(ContextContentType.REFERENCE,ContextContentType.FACT))

    def candidates(self,request,vector=()):
        from evolvmem.pg_business_core.recall import CandidateBatch
        r=ContextRetriever(self.config,self.oracle,None,None,clock=lambda:self.now)
        # The oracle performs actual FTS5. No PostgreSQL lexical equivalence is
        # asserted by supplying its output as a trusted prepared candidate set.
        from evolvmem.context_models import ContextStatus,ContextLayer
        hits=self.oracle.search_fts(request.query,top_k=request.top_k*5,
            statuses=(ContextStatus.ACTIVE,ContextStatus.SUPERSEDED),layers=(ContextLayer.L0,ContextLayer.L1),
            now=r._moment(request),content_types=request.content_types,
            min_confidence=self.config.context_min_confidence)
        return CandidateBatch(L,request,tuple(hits),tuple(vector))

    def original_result(self,batch):
        path=Path(__file__).parents[1]/'fixture/contract.py'
        spec=importlib.util.spec_from_file_location('slice_fixture_contract',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        reference=module.sqlite_reference(self.config,self.oracle,clock=lambda:self.now)
        reference._scoped_vector_candidates=lambda *args,**kwargs:list(batch.vector)
        return reference.search(batch.request,allow_rerank=False)

    def recall(self,batch,*,between=None,publish=None):
        from evolvmem.pg_business_core.recall import PgContextRecall
        with self.host.request_scope():
            identity=self.host.authenticate(TOKEN);state=self.host._state()
            route=state.router.plan(identity,'context_read',library_id=L)
            service=PgContextRecall(state.manager,self.config,clock=lambda:self.now)
            # An explicit transaction-port hook acts only after initial commit;
            # runtime has no between-snapshot callback accepting caller effects.
            original=state.manager._transaction
            if between is not None:
                from contextlib import contextmanager
                calls=0
                @contextmanager
                def transaction(route):
                    nonlocal calls
                    with original(route) as session:yield session
                    calls+=1
                    if calls==1:between()
                state.manager._transaction=transaction
            return service.read_candidates(route,batch,publish=publish)

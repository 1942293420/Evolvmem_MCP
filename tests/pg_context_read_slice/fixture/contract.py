"""Same application contract for a root-supplied real PG fixture or explicit port.

No loader, credentials, environment endpoint or CLI. The trusted fixture must
provide the existing manager/guard/route/capacity objects. It is responsible for
fixed synthetic target and once-only marker admission. Existing seed state is
refused, never deleted/adopted; an unknown write must be reconciled by receipt.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import hashlib
import importlib.util
from pathlib import Path
import sys

from evolvmem.config import Config
from evolvmem.context_models import ContextContentType, ContextLayer, ContextSearchRequest, ContextStatus
from evolvmem.context_store import ContextStore
from evolvmem.context_retriever import ContextRetriever
from evolvmem.library_deadline.guard import SyncRequestDeadlineGuard
from evolvmem.pg_business_core.executor import PgBusinessExecutor
from evolvmem.pg_business_core.recall import CandidateBatch, PgContextRecall

KEYS = ('synthetic-pg-read-slice-20261101-a', 'synthetic-pg-read-slice-20261101-b')
VALUES = ('Slicefixture evidence: alpha release requires the second reviewer.',
          'Slicefixture evidence: beta release keeps the rollback procedure.')
IDS = ('synthetic-pg-read-slice-20261101-add-a', 'synthetic-pg-read-slice-20261101-add-b')
AT = datetime(2030, 1, 1, tzinfo=timezone.utc)


def sqlite_reference(config, store, *, clock):
    """Independent frozen f594 full-search implementation, not new rank code."""
    path=Path(__file__).resolve().parents[1]/'baseline/context_retriever.py'
    if hashlib.sha256(path.read_bytes()).hexdigest()!='984ab5f7bfd2df24cc671ae1af92a2449f1e0d54c666b135878cf2036e9f88fe':
        raise RuntimeError('sqlite_reference_changed')
    name='pg_read_slice_frozen_sqlite_reference'
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module)
    return module.ContextRetriever(config,store,None,None,clock=clock)


def exercise(fixture, *, model_contract, oracle_directory):
    """Two supported writes, PG readback, actual SQLite FTS, exact typed DTO.

Root must supply a reviewed existing model-contract ID (pending outbox only).
This creates no model/vector image and does not measure PG lexical generation.
"""
    if not isinstance(fixture.deadline_guard, SyncRequestDeadlineGuard):
        raise RuntimeError('actual_request_guard_required')
    fixture.guard()
    @contextmanager
    def request(tool):
        with fixture.new_request():
            fixture.deadline_guard.begin_authority()
            yield fixture.route(tool)
    with request('context_read') as route:
        def fresh(session):
            lib=route.library.library_id
            memories=session.execute('SELECT id FROM em_content.memories WHERE library_id=%s AND key IN (%s,%s) LIMIT 1',(lib,*KEYS))
            receipts=session.execute('SELECT request_id FROM em_content.lan_write_requests WHERE library_id=%s AND request_id IN (%s,%s) LIMIT 1',(lib,*IDS))
            if memories or receipts:raise RuntimeError('synthetic_slice_not_fresh')
        fixture.manager.run(route,fresh)
    executor=PgBusinessExecutor(fixture.manager,fixture.config,model_contract=model_contract,semantic_merge_enabled=True)
    written=[]
    for key,value,request_id in zip(KEYS,VALUES,IDS):
        with request('memory_add') as route:
            item=executor.mutate(route,{'key':key,'value':value,'tier':'reference',
                'source_session':'synthetic-read-slice-source'},request_id=request_id)
            if item.get('status')!='added' or not item.get('context_id'):
                raise RuntimeError('synthetic_slice_write_failed')
            written.append(item['context_id'])
    with request('context_read') as route:
        lib=route.library.library_id
        def rows(session):
            return {
                'context_items':session.execute('SELECT to_jsonb(c) FROM em_content.context_items c WHERE library_id=%s AND id IN (%s,%s) ORDER BY id',(lib,*written)),
                'context_layers':session.execute('SELECT to_jsonb(c) FROM em_content.context_layers c WHERE library_id=%s AND item_id IN (%s,%s) ORDER BY item_id,layer',(lib,*written)),
                'context_sources':session.execute('SELECT to_jsonb(c) FROM em_content.context_sources c WHERE library_id=%s AND item_id IN (%s,%s) ORDER BY id',(lib,*written)),
            }
        saved=fixture.manager.run(route,rows)
    # Only those two fixed synthetic items, never an original-source copy.
    oracle=ContextStore(Config(data_dir=Path(oracle_directory),apply_environment=False))
    oracle.initialize()
    try:
        conn=oracle._connection()
        if conn.execute('SELECT 1 FROM context_items LIMIT 1').fetchone():
            raise RuntimeError('empty_synthetic_oracle_required')
        for table,values in saved.items():
            columns={r[1] for r in conn.execute('PRAGMA table_info('+table+')')}
            for (source,) in values:
                data={k:v for k,v in source.items() if k in columns}
                if table=='context_items':
                    if data['experience_payload'] is not None:raise RuntimeError('unexpected_synthetic_evidence')
                    data['experience_payload']='{}'
                    for key in ('tags','projects'):data[key]=json.dumps(data[key])
                names=tuple(data)
                conn.execute('INSERT INTO '+table+'('+','.join(names)+') VALUES('+','.join('?' for _ in names)+')',tuple(data.values()))
        conn.commit()
        query=ContextSearchRequest('slicefixture',top_k=5,cross_project=True,
            content_types=(ContextContentType.REFERENCE,ContextContentType.FACT))
        hits=oracle.search_fts(query.query,top_k=25,statuses=(ContextStatus.ACTIVE,ContextStatus.SUPERSEDED),
            layers=(ContextLayer.L0,ContextLayer.L1),now=AT.strftime('%Y-%m-%d %H:%M:%S'),
            content_types=query.content_types,min_confidence=fixture.config.context_min_confidence,item_ids=written)
        reference=sqlite_reference(fixture.config,oracle,clock=lambda:AT)
        # This fixture deliberately has no vector channel/model. FTS, filters,
        # full ranking loop and DTO are the independent frozen SQLite search.
        reference._scoped_vector_candidates=lambda *args,**kwargs:[]
        expected=reference.search(query,allow_rerank=False)
        if len(expected)!=2:raise RuntimeError('synthetic_oracle_witness_missing')
        with request('context_read') as route:
            actual=PgContextRecall(fixture.manager,fixture.config,clock=lambda:AT).read_candidates(
                route,CandidateBatch(lib,query,tuple(hits),()))
        if actual!=expected:raise RuntimeError('synthetic_typed_dto_difference')
        fixture.guard()
        return {'synthetic_items':2,'typed_dto_exact':True,'current_two_snapshot_read':True,
                'postgres_fts_tested':False,'model_loaded':False,'production_registered':False}
    finally:oracle.close()

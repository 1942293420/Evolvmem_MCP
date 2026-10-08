import json
import numpy as np
from evolvmem.context_store import ContextStore
from evolvmem.context_models import ContextStatus
from evolvmem.cutover_vector import rebuild_context_vector_atomically as rebuild
from evolvmem.context_vector_sync import ContextVectorSynchronizer
from evolvmem.vector_index import VectorIndex
from tests.test_cutover_vector import make_draft, DocumentEmbeddingEngine, _set_l0, _reopen_ids, mark_formal_dirty


def setup(store,config):
    config.embedding_dim=3
    rows=[store.create_item(make_draft(f'd{i}',l0=f'text{i}')) for i in range(6)]
    engine=DocumentEmbeddingEngine({f'text{i}':[1.,float(i),.5] for i in range(6)}|{'changed':[0,1,0],'added':[0,0,1]})
    assert rebuild(config,store,engine,catch_up_rounds=3).status=='staged'
    engine.documents.clear()
    return rows,engine


def test_next_recovery_only_encodes_added_and_changed_and_removes_withdrawn(test_config):
    with ContextStore(test_config) as store:
        rows,engine=setup(store,test_config)
        _set_l0(store,rows[0].id,'changed')
        with store.transaction():
            store.set_item_status(rows[1].id,ContextStatus.ARCHIVED)
        added=store.create_item(make_draft('added',l0='added'))
        mark_formal_dirty(test_config)
        report=rebuild(test_config,store,engine,catch_up_rounds=3)
        assert report.status=='staged'
        assert engine.documents==['changed','added']
        assert _reopen_ids(test_config)==sorted([r.id for r in rows if r!=rows[1]]+[added.id])


def test_per_item_update_and_engine_unavailable_do_not_discard_other_proven_vectors(test_config):
    with ContextStore(test_config) as store:
        rows,engine=setup(store,test_config)
        index=VectorIndex(test_config,path=test_config.context_vector_path);index.initialize(dim=3)
        sync=ContextVectorSynchronizer(test_config,store,index,engine)
        _set_l0(store,rows[0].id,'changed')
        assert sync.upsert_active_l0(rows[0].id,'changed').status=='synchronized'
        sync.embedding_engine=None
        _set_l0(store,rows[1].id,'added')
        assert sync.upsert_active_l0(rows[1].id,'added').status=='unavailable'
        index.close();engine.documents.clear()
        assert rebuild(test_config,store,engine,catch_up_rounds=3).status=='staged'
        assert engine.documents==['added']


def test_unknown_or_changed_vector_is_not_reused_from_old_proof(test_config):
    with ContextStore(test_config) as store:
        rows,engine=setup(store,test_config)
        index=VectorIndex(test_config,path=test_config.context_vector_path);index.initialize(dim=3)
        index.remove(rows[0].id);index.add(rows[0].id,np.array([0,0,1],dtype=np.float32));index.save();index.close()
        mark_formal_dirty(test_config)
        assert rebuild(test_config,store,engine,catch_up_rounds=3).status=='staged'
        assert engine.documents==['text0']


def test_changed_contract_reencodes_all_even_when_dimensions_match(test_config):
    with ContextStore(test_config) as store:
        rows,engine=setup(store,test_config)
        test_config.embedding_doc_prefix='changed: '
        mark_formal_dirty(test_config)
        assert rebuild(test_config,store,engine,catch_up_rounds=3).status=='staged'
        assert engine.documents==[f'text{i}' for i in range(6)]


def test_incremental_encoding_failure_keeps_previous_file_and_retry_marker(test_config):
    with ContextStore(test_config) as store:
        rows,engine=setup(store,test_config)
        before=test_config.context_vector_path.read_bytes()
        _set_l0(store,rows[0].id,'unavailable')
        report=rebuild(test_config,store,engine,catch_up_rounds=3)
        assert report.status=='failed'
        assert test_config.context_vector_path.read_bytes()==before
        assert VectorIndex(test_config,path=test_config.context_vector_path).is_dirty()
        assert engine.documents==['unavailable']

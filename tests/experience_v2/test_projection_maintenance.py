"""Backfill preserves authored content and validates under an activation lock."""
from tests.experience_v2.test_hybrid_search import db,seed
from tests.experience_v2.test_writer_embeddings import FakeClient

def test_repair_and_activation_are_complete_and_replay_safe(db):
    from evolvmem.experience_v2.projection_maintenance import repair_projections,projection_health,activate_policy
    seed(db,'repair-me');db.commit();client=FakeClient()
    before=db.execute('SELECT problem,revision,updated_at,status FROM experience_notes').fetchone()
    assert projection_health(db,client)['invalid']==1
    out=repair_projections(db,secret=b'test-maintenance-secret',client=client)
    assert out['repaired']==1
    assert projection_health(db,client)=={'active':1,'legacy_missing':0,'retrieval_missing':0,'invalid':0}
    assert db.execute('SELECT problem,revision,updated_at,status FROM experience_notes').fetchone()==before
    assert repair_projections(db,secret=b'test-maintenance-secret',client=client)['repaired']==0
    activate_policy(db,client)
    assert db.execute('SELECT required FROM experience_embedding_policy').fetchone()==(True,)

def test_activation_rejects_incomplete_library(db):
    import pytest
    from evolvmem.experience_v2.projection_maintenance import activate_policy
    seed(db,'missing')
    with pytest.raises(ValueError,match='projection_backfill_incomplete'):activate_policy(db,FakeClient())
    assert db.execute('SELECT count(*) FROM experience_embedding_policy').fetchone()==(0,)

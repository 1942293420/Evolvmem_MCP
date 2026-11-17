"""Manual search uses the same complete authorized projection as recall."""
import pytest
from psycopg.conninfo import make_conninfo
from tests.stage2.conftest import _MaskedDsn
from tests.stage5.conftest import STAGE5_PW_FILE
from tests.experience_v2.test_hybrid_search import db, seed, vec
from evolvmem.experience_v2.pg_experience_store import PgExperienceStore, PgExperienceSettings

class QueryClient:
    retrieval_contract_id = "test1024"
    def encode_query(self, text, *, deadline): return vec()

def test_manual_semantic_search_is_server_side_and_never_old_dynamic_rank(db):
    for i in range(520): seed(db, f"noise-{i}")
    seed(db, "manual-" + "a"*32, problem="连接等候超时", similarity=.95)
    seed(db, "manual-" + "b"*32, owner="bob", problem="连接等候超时", similarity=.99)
    db.execute("UPDATE experience_notes SET origin='manual'"); db.commit()
    class ForbiddenRank:
        def rank(self, *a): raise AssertionError("must not re-encode documents")
    store=PgExperienceStore(PgExperienceSettings(dsn=_MaskedDsn(make_conninfo(db.info.dsn,password=STAGE5_PW_FILE.read_text().strip()))),embedding_client=QueryClient())
    out=store.read({'user_id':'alice'}, {}, category='',query='database connection wait',page=1,page_size=10,deleted=0,search=ForbiddenRank())
    assert out['search_mode']=='hybrid'
    assert out['total']==1
    assert [x['id'] for x in out['rows']]==['manual-'+'a'*32]
    assert out['summary']['categories']['业务经验']==1

def test_manual_encoder_failure_preserves_full_sql_keyword_search(db):
    seed(db,'manual-'+'a'*32,problem='PostgreSQL connection timeout')
    db.execute("UPDATE experience_notes SET origin='manual'");db.commit()
    class Broken(QueryClient):
        def encode_query(self,*a,**kw): raise RuntimeError('offline')
    store=PgExperienceStore(PgExperienceSettings(dsn=_MaskedDsn(make_conninfo(db.info.dsn,password=STAGE5_PW_FILE.read_text().strip()))),embedding_client=Broken())
    out=store.read({'user_id':'alice'},{},category='',query='PostgreSQL connection timeout',page=1,page_size=10,deleted=0)
    assert out['total']==1 and out['search_mode']=='keywords_only'
    assert out['diagnostics']['embedding_error']=='embedding_unavailable'

"""Web v2 queries against isolated PG, including the actual ACL resolver."""
from contextlib import contextmanager
import time
import pytest
from tests.experience_v2.test_hybrid_search import db,seed,vec
from evolvmem.web_v2 import api_experiences as api
from evolvmem.web_v2.common import V2ApiError

class Console:
    def dispatch(self,method,path,params,session):
        return 200,{},{"current_user":{"id":session},"libraries":[
            {"id":session,"can_read_experience":True},
            {"id":"alice","can_read_experience":session=="reader"}]}

@pytest.fixture
def web(db,monkeypatch):
    @contextmanager
    def opened(**kwargs):
        yield None,db
    monkeypatch.setattr(api,"open_exp_pg",opened)
    from evolvmem.experience_v2.embedding_client import EmbeddingClient
    class Client:
        retrieval_contract_id="test1024"
        def encode_query(self,text,*,deadline):
            assert deadline<time.monotonic()+3.1
            return vec()
    monkeypatch.setattr(EmbeddingClient,"from_environment",classmethod(lambda cls:Client()))
    return db

def test_web_semantic_rewrite_searches_all_project_notes(web):
    for n in range(605):
        seed(web,f"noise-{n}")
    seed(web,"target",problem="数据库连接等候过长",scope={"project":"Evolvmem"},similarity=.95)
    out=api.list_experiences(Console(),"alice",{"q":"database connection wait"})
    assert [n["id"] for n in out["items"]]==["target"]
    assert out["total"]==1
    assert out["diagnostics"]["mode"]=="hybrid"
    assert out["items"][0]["match_types"]==["vector"]
    assert web.execute("SELECT count(*) FROM retrieval_impressions").fetchone()[0]==0

def test_web_acl_filters_pagination_and_browse_unchanged(web):
    for n in range(3):
        seed(web,f"note-{n}",problem="PostgreSQL timeout",similarity=.9)
    seed(web,"retired",status="retired",problem="PostgreSQL timeout",similarity=.95)
    out=api.list_experiences(Console(),"reader",{"owner_id":"alice","q":"PostgreSQL timeout","limit":"1","offset":"1"})
    assert out["total"]==3 and len(out["items"])==1
    assert out["status_counts"]["active"]==3 and out["interception_funnel"] is None
    with pytest.raises(V2ApiError,match="peer_status_forbidden"):
        api.list_experiences(Console(),"reader",{"owner_id":"alice","status":"retired","q":"timeout"})
    with pytest.raises(V2ApiError,match="experience_access_denied"):
        api.list_experiences(Console(),"intruder",{"owner_id":"alice","q":"timeout"})
    browse=api.list_experiences(Console(),"alice",{})
    assert browse["total"]==3 and "diagnostics" not in browse

def test_web_encoder_failure_and_strict_constraints(web,monkeypatch):
    from evolvmem.experience_v2.embedding_client import EmbeddingClient
    class Busy:
        retrieval_contract_id="test1024"
        def encode_query(self,*a,**kw):
            raise RuntimeError("embedding_busy")
    monkeypatch.setattr(EmbeddingClient,"from_environment",classmethod(lambda cls:Busy()))
    seed(web,"keyword",problem="PostgreSQL timeout",scope={"os":"linux"})
    seed(web,"missing",problem="PostgreSQL timeout")
    out=api.list_experiences(Console(),"alice",{"q":"PostgreSQL timeout","constraints":'{"os":"linux"}'})
    assert [r["id"] for r in out["items"]]==["keyword"]
    assert out["diagnostics"]["mode"]=="keywords_only"
    assert out["diagnostics"]["embedding_error"]=="embedding_busy"
    for params in ({"constraints":"[]"},{"constraints":'{"os":2}'},{"project":[]},{"q":12}):
        with pytest.raises(V2ApiError):
            api.list_experiences(Console(),"alice",{"q":"query",**params})

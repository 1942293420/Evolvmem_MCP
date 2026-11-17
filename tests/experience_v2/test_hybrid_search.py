"""Real pgvector retrieval tests, isolated to Stage 5 test PG (55434)."""
import hashlib
import json
import math
import time
import uuid

import psycopg
import pytest

from evolvmem.experience_v2.schema import apply_experience_migrations
from evolvmem.pg_test_guard import assert_safe_test_pg_target
from tests.stage5.conftest import (
    STAGE5_PG_CONTAINER, STAGE5_PG_PORT, STAGE5_PW_FILE, ensure_stage5_pg_running,
)


@pytest.fixture
def db():
    assert_safe_test_pg_target(port=STAGE5_PG_PORT, container_name=STAGE5_PG_CONTAINER)
    ensure_stage5_pg_running()
    cfg = dict(host="127.0.0.1", port=STAGE5_PG_PORT, dbname="postgres",
               user="postgres", password=STAGE5_PW_FILE.read_text().strip())
    schema = "hybrid_" + uuid.uuid4().hex[:12]
    with psycopg.connect(**cfg, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    try:
        with psycopg.connect(**cfg, options=f"-c search_path={schema},public") as conn:
            apply_experience_migrations(conn)
            conn.commit()
            yield conn
    finally:
        with psycopg.connect(**cfg, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


def vec(cosine=1.0):
    return [cosine, math.sqrt(max(0, 1-cosine*cosine))] + [0.0]*1022


def seed(conn, nid, *, owner="alice", problem="完全不同的商品退货问题", solution="按照原流程处理",
         scope=None, similarity=0, contract="test1024", stale=False, status="active",
         pinned=False, valid_from=None, valid_until=None, deleted=False):
    note = dict(category="业务经验", problem=problem, solution=solution,
                reusable_experience="核实真实来源后采用")
    digest = hashlib.sha256("\n".join(note[k] for k in
        ("category", "problem", "solution", "reusable_experience")).encode()).hexdigest()
    embedding = "[" + ",".join(str(v) for v in vec(similarity)) + "]"
    conn.execute("""
        INSERT INTO experience_notes(id,owner_id,category,problem,solution,reusable_experience,
            scope,status,pinned,valid_from,valid_until,deleted_at,retrieval_embedding,
            retrieval_embedding_contract,retrieval_embedding_content_sha256,retrieval_embedding_updated_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,
            CASE WHEN %s THEN now() ELSE NULL END,%s::public.vector,%s,%s,now())
        """, (nid,owner,*[note[k] for k in ("category","problem","solution","reusable_experience")],
              json.dumps(scope or {}),status,pinned,valid_from,valid_until,deleted,embedding,
              contract,"0"*64 if stale else digest))
    return nid


def search(conn, query="PostgreSQL connection timeout", **kw):
    from evolvmem.experience_v2.hybrid_search import hybrid_search
    return hybrid_search(conn, owner_ids=["alice"], query=query,
                         query_vector=vec(), contract_id="test1024", **kw)


def ids(result):
    return [r["id"] for r in result["items"]]


def test_search_covers_past_600_popular_rows_and_does_not_write_exposure(db):
    for n in range(605):
        seed(db, f"noise-{n}", pinned=True)
    seed(db, "late", problem="PostgreSQL connection timeout", similarity=.96)
    out = search(db)
    assert ids(out) == ["late"]
    assert set(out["items"][0]["match_types"]) == {"keyword", "vector"}
    assert out["items"][0]["owner_id"] == "alice"
    assert db.execute("SELECT count(*) FROM retrieval_impressions").fetchone()[0] == 0


def test_semantic_paraphrase_and_strict_gate_before_pinning(db):
    seed(db, "paraphrase", problem="数据库连接等待过长", similarity=.93)
    seed(db, "weak-pinned", problem="商品退货", similarity=.719, pinned=True)
    seed(db, "unrelated", problem="广告账单", similarity=.15)
    out = search(db)
    assert ids(out) == ["paraphrase"]
    assert out["items"][0]["match_types"] == ["vector"]
    assert out["items"][0]["cosine"] == pytest.approx(.93, abs=1e-6)


def test_acl_scope_constraints_and_time_apply_to_both_lanes(db):
    exact = "PostgreSQL connection timeout"
    seed(db,"good",problem=exact,scope={"project":"evolvmem","os":"linux"},similarity=.96)
    for nid, opts in (
        ("other-owner",dict(owner="bob")),
        ("project-conflict",dict(scope={"project":"other","os":"linux"})),
        ("constraint-conflict",dict(scope={"os":"windows"})),
        ("constraint-missing",dict(scope={})),
        ("expired",dict(scope={"os":"linux"},valid_until="2020-01-01")),
        ("future",dict(scope={"os":"linux"},valid_from="2099-01-01")),
        ("retired",dict(scope={"os":"linux"},status="retired")),
        ("deleted",dict(scope={"os":"linux"},deleted=True)),
    ):
        seed(db,nid,problem=exact,similarity=.99,**opts)
    out = search(db, project="evolvmem", constraints={"os":"linux"}, include_total=True)
    assert ids(out) == ["good"]
    assert out["total"] == 1


def test_global_scope_is_available_without_constraints(db):
    seed(db,"global",similarity=.9)
    seed(db,"wrong-project",scope={"project":"other"},similarity=.99)
    assert ids(search(db, project="evolvmem")) == ["global"]


def test_wrong_contract_and_dirty_content_never_supply_semantics(db):
    seed(db,"wrong-model",similarity=.99,contract="wrong")
    seed(db,"dirty",similarity=.99,stale=True)
    assert search(db)["items"] == []


def test_keyword_lane_works_without_vectors_and_escapes_wildcards(db):
    seed(db,"keyword",problem="PostgreSQL connection timeout")
    from evolvmem.experience_v2.hybrid_search import hybrid_search
    kw = dict(owner_ids=["alice"],query_vector=None,contract_id=None)
    out = hybrid_search(db,query="PostgreSQL connection timeout",**kw)
    assert ids(out) == ["keyword"]
    assert out["items"][0]["match_types"] == ["keyword"]
    assert out["diagnostics"]["mode"] == "keywords_only"
    assert hybrid_search(db,query="%_",**kw)["items"] == []


def test_chinese_bigrams_recall_word_order_changes(db):
    seed(db,"cn",problem="页面表格溢出导致布局错乱",solution="限制表格宽度并允许横向滚动")
    from evolvmem.experience_v2.hybrid_search import hybrid_search
    out = hybrid_search(db,owner_ids=["alice"],query="表格布局溢出",query_vector=None,contract_id=None)
    assert ids(out) == ["cn"]


def test_total_is_actual_match_count_with_pagination(db):
    for n in range(25):
        seed(db,f"semantic-{n:02}",similarity=.90)
    for n in range(7):
        seed(db,f"lexical-{n:02}",problem="PostgreSQL connection timeout")
    first = search(db,include_total=True,limit=10)
    second = search(db,include_total=True,limit=10,offset=10)
    assert first["total"] == second["total"] == 32
    assert len(first["items"]) == len(second["items"]) == 10
    assert not set(ids(first)) & set(ids(second))


@pytest.mark.parametrize("overrides", [
    {"query":""}, {"query":"  "}, {"query":12}, {"constraints":[]},
    {"constraints":{"os":12}}, {"owner_ids":[]}, {"query_vector":[0]*768},
    {"query_vector":[0]*1024}, {"query_vector":[float("nan")]*1024},
    {"min_similarity":0}, {"limit":0},
])
def test_invalid_parameters_rejected_before_sql(db, overrides):
    from evolvmem.experience_v2.hybrid_search import hybrid_search
    args=dict(owner_ids=["alice"],query="query",query_vector=vec(),contract_id="test1024")
    args.update(overrides)
    with pytest.raises(ValueError):
        hybrid_search(db,**args)


def test_expired_deadline_is_not_reported_as_empty_success(db):
    with pytest.raises(TimeoutError):
        search(db,deadline=time.monotonic()-1)


def test_recall_adapter_uses_hybrid_but_keeps_exposure_and_output(db):
    from evolvmem.experience_v2.changes_sync import recall_pg_experiences
    seed(db,"adapter",problem="连接等候超时",similarity=.95)
    class Client:
        retrieval_contract_id="test1024"
        def encode_query(self,text,*,deadline):
            assert deadline <= time.monotonic()+2
            return vec()
    out=recall_pg_experiences(db,owner_id="alice",query="database connection wait",
        project="evolvmem",mode="hook",embedding_client=Client())
    assert out["source"] == "pg_experience_v2"
    assert out["results"][0]["id"] == "adapter"
    assert out["diagnostics"]["mode"] == "hybrid"
    assert db.execute("SELECT count(*) FROM retrieval_impressions").fetchone()[0] == 1


def test_recall_adapter_explicit_encoder_failure_is_keywords_only(db):
    from evolvmem.experience_v2.changes_sync import recall_pg_experiences
    seed(db,"adapter",problem="PostgreSQL connection timeout")
    class Client:
        retrieval_contract_id="test1024"
        def encode_query(self,text,*,deadline):
            raise RuntimeError("embedding_busy")
    out=recall_pg_experiences(db,owner_id="alice",query="PostgreSQL connection timeout",
                             embedding_client=Client())
    assert [r["id"] for r in out["results"]] == ["adapter"]
    assert out["diagnostics"]["mode"] == "keywords_only"
    assert out["diagnostics"]["embedding_error"] == "embedding_busy"


@pytest.mark.parametrize("kw", [{"mode":"anything"},{"query":""},{"constraints":{"os":2}}])
def test_recall_adapter_rejects_bad_arguments(db,kw):
    from evolvmem.experience_v2.changes_sync import recall_pg_experiences
    args=dict(owner_id="alice",query="query");args.update(kw)
    with pytest.raises(ValueError):
        recall_pg_experiences(db,**args)


def test_vector_lane_uses_actual_partial_hnsw_index(db):
    seed(db,"indexed",similarity=.95)
    captured = []
    class Trace:
        def __getattr__(self,name): return getattr(db,name)
        def execute(self, statement, params=None):
            if statement.startswith("SELECT n.id,n.owner_id,1-") and ")+0 LIMIT" not in statement:
                captured.append((statement,params))
            return db.execute(statement,params)
    db.execute("SET LOCAL enable_seqscan=off")
    db.execute("SET LOCAL enable_sort=off")
    search(Trace())
    assert len(captured) == 1
    statement, params = captured[0]
    plan = db.execute("EXPLAIN (FORMAT JSON) " + statement, params).fetchone()[0]
    assert "idx_experience_notes_retrieval_hnsw" in json.dumps(plan)


def test_encoder_timeout_reserves_sql_fallback_budget(db):
    from evolvmem.experience_v2.changes_sync import recall_pg_experiences
    seed(db,"fallback",problem="PostgreSQL connection timeout")
    class Client:
        retrieval_contract_id="test1024"
        def encode_query(self,text,*,deadline):
            assert deadline < time.monotonic()+.15
            raise RuntimeError("embedding_deadline_exceeded")
    until=time.monotonic()+.3
    out=recall_pg_experiences(db,owner_id="alice",query="PostgreSQL connection timeout",
        embedding_client=Client(),deadline=until)
    assert ids({"items":out["results"]}) == ["fallback"]
    assert out["diagnostics"]["embedding_error"] == "embedding_deadline_exceeded"
    assert time.monotonic() < until


def test_all_scope_vector_search_survives_600_unauthorized_nearer_neighbors(db):
    for n in range(605):
        seed(db,f"foreign-{n}",owner="bob",similarity=.999)
    seed(db,"authorized-paraphrase",problem="连接等候过久时核查网络",similarity=.90)
    assert ids(search(db,query="database connection wait")) == ["authorized-paraphrase"]


def test_total_counts_matches_beyond_ann_candidate_budget(db):
    for n in range(125):
        seed(db,f"matched-{n}",similarity=.90)
    out=search(db,include_total=True,limit=3)
    assert len(out["items"]) == 3
    assert out["total"] == 125
    assert out["diagnostics"]["total_complete"] is True


@pytest.mark.parametrize("project,expected", [
    ("", ["scoped-note"]), ("evolvmem", ["scoped-note"]), ("other", []),
])
def test_unfiltered_web_search_includes_project_notes_and_named_scope_is_casefolded(db,project,expected):
    seed(db,"scoped-note",problem="PostgreSQL connection timeout",
         scope={"project":"Evolvmem"},similarity=.95)
    assert ids(search(db,project=project)) == expected


def test_vector_leg_forces_only_its_index_and_restores_planner_flags(db):
    seed(db,"indexed-local",similarity=.95)
    evidence=[]
    class Trace:
        def __getattr__(self,name): return getattr(db,name)
        def execute(self,statement,params=None):
            if statement.startswith("SELECT n.id,n.owner_id,1-") and ")+0 LIMIT" not in statement:
                assert db.execute("SHOW enable_sort").fetchone()[0]=="off"
                evidence.append(db.execute("EXPLAIN (FORMAT JSON) "+statement,params).fetchone()[0])
            return db.execute(statement,params)
    search(Trace())
    assert "idx_experience_notes_retrieval_hnsw" in json.dumps(evidence)
    assert db.execute("SHOW enable_sort").fetchone()[0]=="on"
    assert db.execute("SHOW enable_seqscan").fetchone()[0]=="on"


def test_vector_failure_rolls_back_savepoint_and_restores_planner_flags(db):
    seed(db,"indexed-error",similarity=.95)
    class Broken:
        def __getattr__(self,name): return getattr(db,name)
        def execute(self,statement,params=None):
            if statement.startswith("SELECT n.id,n.owner_id,1-") and ")+0 LIMIT" not in statement:
                db.execute("SELECT 1/0")
            return db.execute(statement,params)
    with pytest.raises(psycopg.errors.DivisionByZero):
        search(Broken())
    assert db.execute("SHOW enable_sort").fetchone()[0]=="on"
    assert db.execute("SHOW enable_seqscan").fetchone()[0]=="on"
    assert db.execute("SELECT count(*) FROM experience_notes").fetchone()[0]==1


def test_full_result_pagination_is_stable_across_semantic_ties_and_lexical_overlap(db):
    query="PostgreSQL connection timeout"
    expected=[]
    for n in range(370):
        keyword=n < 350 and n % 11 == 0 or n >= 350
        semantic=n < 350
        nid=f"stable-{n:03}"
        pinned=n % 7 == 0
        seed(db,nid,problem=query if keyword else "完全不同的商品退货问题",
             similarity=.9 if semantic else 0,pinned=pinned)
        importance=n % 10 + 1
        db.execute("UPDATE experience_notes SET importance=%s WHERE id=%s",(importance,nid))
        expected.append((nid,keyword,semantic,pinned,importance))
    expected.sort(key=lambda v: (-(1 if v[1] else .9),-(int(v[1])+int(v[2])),
                                 -v[3],-v[4],"alice",v[0]))
    expected_ids=[r[0] for r in expected]
    for page_size in (60,80):
        collected=[]
        for offset in range(0,370,page_size):
            out=search(db,query=query,include_total=True,limit=page_size,offset=offset,
                       deadline=time.monotonic()+3)
            assert out["total"] == 370
            collected.extend(ids(out))
        assert len(collected) == len(set(collected)) == 370
        assert collected == expected_ids
    beyond=search(db,include_total=True,limit=60,offset=400)
    assert beyond["total"] == 370 and beyond["items"] == []


def test_keyword_only_full_result_pages_use_the_same_popularity_tiebreakers(db):
    from evolvmem.experience_v2.hybrid_search import hybrid_search
    for n in range(350):
        seed(db,f"keyword-tie-{n:03}",problem="PostgreSQL timeout",pinned=n>=300)
    collected=[]
    for offset in range(0,350,60):
        out=hybrid_search(db,owner_ids=["alice"],query="PostgreSQL timeout",
            query_vector=None,contract_id=None,include_total=True,limit=60,offset=offset,
            deadline=time.monotonic()+3)
        assert out["total"] == 350
        collected.extend(ids(out))
    expected=[f"keyword-tie-{n:03}" for n in [*range(300,350),*range(300)]]
    assert collected == expected

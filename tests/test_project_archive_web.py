"""Project directory and complete archive browsing use read-only snapshots."""
from tests.test_project_archive_store import save_manual_event
from types import SimpleNamespace
import json
import pytest
from evolvmem.context_store import ContextStore
from evolvmem.project_archive_store import ProjectArchiveStore, ProjectEventDraft
from tests.test_web_auth import Browser, protected_web


@pytest.fixture
def catalog(test_config):
    with ContextStore(test_config) as store:
        yield SimpleNamespace(store=store, config=test_config)


def register(svc, project, name="", status="active", alias=None):
    conn = svc.store._connection()
    with svc.store.transaction():
        conn.execute("INSERT INTO context_project_registry(project,display_name,status,created_at,updated_at) VALUES(?,?,?,'2026-01-01','2026-01-01')", (project,name,status))
        if alias:
            conn.execute("INSERT INTO context_project_aliases(alias,project,created_at,updated_at) VALUES(?,?,'2026-01-01','2026-01-01')", (alias,project))


def archive(svc, project, count=1):
    store = ProjectArchiveStore(svc.store)
    for i in range(count):
        save_manual_event(store, ProjectEventDraft(project=project,event_type="milestone",title=f"Event {i}",content=f"body {i}",occurred_at=f"2026-01-01T00:{i//60:02}:{i%60:02}"))
    store.rebuild(project)


def directory(svc, **params):
    from evolvmem.project_archive_web import project_archives
    return project_archives(svc, params)


def detail(svc, project, **params):
    from evolvmem.project_archive_web import project_archive
    return project_archive(svc, project, params)


def test_directory_includes_registered_projects_without_archives_and_separate_stats(catalog):
    register(catalog,"ready","Ready",alias="Historical Name")
    register(catalog,"empty","Empty")
    register(catalog,"old","Old","archived")
    archive(catalog,"ready",2)
    conn=catalog.store._connection()
    conn.execute("PRAGMA query_only=ON")
    result=directory(catalog)
    assert result["total"] == 2
    assert {row["project"] for row in result["items"]} == {"ready","empty"}
    assert result["stats"] == dict(registered=3,active=2,archived=1,with_archive=1,without_archive=2,active_with_archive=1,active_without_archive=1)
    ready=next(row for row in result["items"] if row["project"]=="ready")
    assert ready["event_count"]==2 and ready["has_archive"] is True
    assert ready["aliases"]==["Historical Name"]
    assert ready["archive_status"]=="active"
    assert all("summary" not in row and "content" not in row for row in result["items"])
    assert directory(catalog,archive="missing")["items"][0]["project"]=="empty"
    assert directory(catalog,status="archived")["items"][0]["project"]=="old"
    assert directory(catalog,status="all")["total"]==3


def test_directory_filters_before_paging_and_searches_alias_literal_characters(catalog):
    for i in range(5): register(catalog,f"p{i}",f"Name {i}",alias=f"Alias-{i}")
    register(catalog,"under_score","Percent%")
    assert directory(catalog,q="aLiAs-3")["items"][0]["project"]=="p3"
    assert directory(catalog,q="_")["total"]==1
    assert directory(catalog,q="%")['total']==1
    assert directory(catalog,q="' OR 1=1 --")["total"]==0
    result=directory(catalog,q="Name",sort="name",page=99,page_size=2)
    assert (result["total"],result["page"],result["pages"])==(5,3,3)
    assert [r["project"] for r in result["items"]]==["p4"]
    assert directory(catalog,q="no-match",page=99)["page"]==1
    assert directory(catalog,page_size=999)["page_size"]==100


@pytest.mark.parametrize("params", [{"status":"deleted"},{"archive":"yes"},{"sort":"random"},{"page":"bad"},{"page_size":"bad"}])
def test_directory_rejects_invalid_filters(catalog,params):
    with pytest.raises(ValueError): directory(catalog,**params)


def test_archive_detail_pages_all_events_and_preserves_canonical_id_and_old_projects(catalog):
    register(catalog,"same","First","archived",alias="Old Alias")
    register(catalog,"other","same")
    archive(catalog,"same",123)
    from evolvmem.project_archive_pipeline import resolve_archive_project
    # The Web canonical-ID preference does not alter the existing MCP resolver.
    assert resolve_archive_project(catalog, "same") == "other"
    catalog.store._connection().execute("PRAGMA query_only=ON")
    result=detail(catalog,"same",events_page=2,events_page_size=100)
    assert result["project"]=="same" and result["project_status"]=="archived"
    assert result["has_archive"] is True
    assert (result["events_total"],result["events_page"],result["events_pages"])==(123,2,2)
    first=detail(catalog,"old alias",events_page_size=100)
    assert len(first["events"])==100 and len(result["events"])==23
    assert len({e["event_id"] for e in first["events"]+result["events"]})==123
    assert detail(catalog,"same",events_page=99,events_page_size=100)["events_page"]==2
    assert "body 0" not in result["summary"] and "body 122" not in result["summary"]
    assert result["current_events_total"] == 0
    assert all(not e["current_support"] for e in result["events"])


def test_archive_detail_distinguishes_registered_empty_unknown_and_ambiguous(catalog):
    register(catalog,"empty","Shared")
    register(catalog,"other","Shared")
    result=detail(catalog,"empty")
    assert result["has_archive"] is False and result["summary"]=="" and result["events"]==[]
    assert result["events_total"]==0 and result["events_pages"]==1
    with pytest.raises(ValueError,match="project_not_registered"): detail(catalog,"unknown")
    with pytest.raises(ValueError,match="project_ambiguous"): detail(catalog,"Shared")
    with pytest.raises(ValueError,match="invalid_events_page"): detail(catalog,"empty",events_page="nan")


def test_project_directory_and_detail_http_require_auth_and_allow_readonly_viewer(protected_web):
    base,ids,svc,*_=protected_web
    register(svc,"empty","Empty")
    browser=Browser(base)
    for path in ("/api/project-archives","/api/project-archives/empty"):
        assert browser.request(path)[0]==401
    browser.login("viewer")
    code,_,body=browser.request("/api/project-archives")
    assert code==200
    assert json.loads(body)["items"][0]["project"]=="empty"
    code,_,body=browser.request("/api/project-archives/empty")
    assert code==200 and json.loads(body)["has_archive"] is False
    assert browser.request("/api/project-archives/unknown")[0]==404
    assert browser.request("/api/project-archives?page=bad")[0]==400
    assert browser.request("/api/project-archives/empty?events_page=bad")[0]==400
    assert browser.request("/api/capabilities?intake_page=bad")[0]==400
    assert browser.request("/api/capabilities?page=bad")[0]==400
    assert browser.request("/api/knowledge/projects", "POST", {"project":"new"})[0]==403


def test_directory_empty_has_explicit_zero_totals(catalog):
    result=directory(catalog)
    assert result["items"]==[] and result["total"]==0 and result["pages"]==1
    assert set(result["stats"].values())=={0}

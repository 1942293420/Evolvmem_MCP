"""Real subsecond PG connect/query budgets and trusted MCP recall admission."""
import importlib
import socket
import threading
import time
from contextlib import contextmanager
from types import SimpleNamespace
import psycopg
import pytest


def api():
    return importlib.import_module("evolvmem.experience_v2.deadline_pg")


def test_stalled_tcp_handshake_obeys_150ms_deadline():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0)); listener.listen()
    stop = threading.Event()
    def accept():
        conn, _ = listener.accept()
        try: stop.wait(1)
        finally: conn.close()
    thread = threading.Thread(target=accept, daemon=True); thread.start()
    try:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            api().connect_with_deadline(host="127.0.0.1", port=listener.getsockname()[1],
                dbname="postgres", user="postgres", sslmode="disable", deadline=started + .15)
        assert time.monotonic() - started < .3
    finally:
        stop.set(); listener.close(); thread.join(1)


@pytest.fixture
def test_pg():
    from tests.stage5.conftest import (
        STAGE5_PG_PORT, STAGE5_PW_FILE, ensure_stage5_pg_running,
    )
    from evolvmem.pg_test_guard import assert_safe_test_pg_target
    assert_safe_test_pg_target(port=STAGE5_PG_PORT)
    ensure_stage5_pg_running()
    return dict(host="127.0.0.1", port=STAGE5_PG_PORT, dbname="postgres",
                user="postgres", password=STAGE5_PW_FILE.read_text().strip(), sslmode="disable")


def test_real_pg_sleep_cancelled_within_absolute_deadline(test_pg):
    mod = api()
    started = time.monotonic()
    conn = mod.connect_with_deadline(deadline=started + .18, **test_pg)
    pid = conn.info.backend_pid
    try:
        with pytest.raises(TimeoutError):
            conn.execute("SELECT pg_sleep(3)")
        assert time.monotonic() - started < .3
        assert conn.closed
        with psycopg.connect(**test_pg, autocommit=True) as inspector:
            end = time.monotonic() + .3
            while time.monotonic() < end:
                row = inspector.execute("SELECT count(*) FROM pg_stat_activity WHERE pid=%s AND query LIKE 'SELECT pg_sleep%%'", (pid,)).fetchone()
                if row[0] == 0: break
                time.sleep(.01)
            assert row[0] == 0
    finally:
        conn.close()


def test_deadline_connect_wraps_real_connection_and_commits(test_pg):
    with api().connect_with_deadline(deadline=time.monotonic()+1, **test_pg) as conn:
        assert conn.execute("SELECT 7").fetchone() == (7,)
        conn.commit()


def test_dns_resolution_cannot_hide_inside_bounded_connect():
    with pytest.raises(ValueError, match="numeric_pg_host_required"):
        api().connect_with_deadline(host="could-block.invalid", deadline=time.monotonic()+.1)


def test_mcp_entry_passes_same_deadline_trusted_owners_and_real_session(monkeypatch):
    from evolvmem.experience_v2 import mcp_tools as m
    seen = {}
    @contextmanager
    def opened(settings, *, role, deadline):
        seen["connect_deadline"] = deadline
        yield object()
    def recalled(conn, **kwargs):
        seen.update(kwargs); return {"results":[]}
    monkeypatch.setattr(m, "open_experience_deadline", opened)
    monkeypatch.setattr(m, "recall_pg_experiences", recalled)
    end = time.monotonic()+.4
    m.call_pg_experience_recall(dict(query="q", session_id="actual-session", workstream_id="not-a-session"),
        owner_id="alice", settings=object(), deadline=end, authorized_owner_ids=["alice","bob"])
    assert seen["connect_deadline"] == seen["deadline"] == end
    assert seen["session_id"] == "actual-session"
    assert seen["authorized_owner_ids"] == ["alice","bob"]


def test_tool_caller_cannot_supply_owner_collection():
    from evolvmem.experience_v2 import mcp_tools as m
    assert m.call_pg_experience_recall(dict(query="q", owner_ids=["someone-else"]),
        owner_id="alice", settings=object()).get("error") == "invalid_arguments"


def test_host_hook_caps_timeout_and_preserves_true_session(monkeypatch):
    from evolvmem.history_pg.unified_mcp_host import UnifiedMcpHost
    from evolvmem.experience_v2 import mcp_tools
    host = UnifiedMcpHost.__new__(UnifiedMcpHost)
    host.request_timeout_seconds, host.allow_sleep_probe = 5, False
    seen = {}
    host._resolve_user_id = lambda identity, **kw: "alice"
    host._exp_settings = lambda: object()
    host._experience_owners = lambda uid, deadline: (["alice","bob"], "")
    def recalled(args, **kwargs):
        seen.update(kwargs)
        return {"results":[], "diagnostics":{}}
    monkeypatch.setattr(mcp_tools, "call_pg_experience_recall", recalled)
    started=time.monotonic(); output=[]
    host.serve_rpc(SimpleNamespace(principal_id="p"), dict(jsonrpc="2.0",id=1,method="tools/call",
        params=dict(name="experience_recall",arguments=dict(query="q",mode="hook",timeout_ms=1800,
                                                          session_id="real",workstream_id="wrong"))),
        publish=output.append)
    assert 0 < seen["deadline"]-started <= 1.81
    assert seen["authorized_owner_ids"] == ["alice","bob"]
    assert not output[0]["result"]["isError"]


def test_acl_no_database_config_conservatively_returns_self():
    from evolvmem.history_pg.unified_mcp_host import UnifiedMcpHost
    host = UnifiedMcpHost.__new__(UnifiedMcpHost)
    host.control_store = object()
    assert host._experience_owners("alice",time.monotonic()+.1) == (["alice"],"acl_unavailable_self_only")


@pytest.mark.parametrize("values", [[1e40]+[0.0]*1023, [1e-80]*1024])
def test_embedding_vector_must_survive_pgvector_float32(values):
    from evolvmem.experience_v2.embedding_client import validate_vector, EmbeddingFailure
    with pytest.raises(EmbeddingFailure, match="invalid_embedding_vector"):
        validate_vector(values,1024)


def test_embedding_returns_actual_float32_values():
    from evolvmem.experience_v2.embedding_client import validate_vector
    values = validate_vector([1.0,1e-80]+[0.0]*1022,1024)
    assert values[0] == 1.0 and values[1] == 0



def test_acl_uses_experience_grants_and_enabled_owners_only():
    from evolvmem.history_pg.unified_mcp_host import UnifiedMcpHost
    host = UnifiedMcpHost.__new__(UnifiedMcpHost); host.owner_id="configured-owner"
    seen={}
    def rows(sql,params,deadline):
        seen.update(sql=sql,params=params,deadline=deadline)
        return [{"user_id":"alice"},{"user_id":"bob"}]
    host._control_rows=rows
    end=time.monotonic()+1
    assert host._experience_owners("alice",end)==(["alice","bob"],"")
    assert "g.experience_read" in seen["sql"] and "u.enabled" in seen["sql"]
    assert "history_read" not in seen["sql"] and "is_admin" not in seen["sql"]
    assert seen["params"]==("configured-owner","alice","configured-owner","alice")
    assert seen["deadline"]==end


def test_host_honors_small_timeout_without_enlarging_budget(monkeypatch):
    from evolvmem.history_pg.unified_mcp_host import UnifiedMcpHost
    from evolvmem.experience_v2 import mcp_tools
    host=UnifiedMcpHost.__new__(UnifiedMcpHost)
    host.request_timeout_seconds,host.allow_sleep_probe=5,False
    seen={}
    host._resolve_user_id=lambda identity,**kw: "alice"
    host._experience_owners=lambda uid,deadline: (["alice"],"")
    host._exp_settings=lambda: object()
    def recalled(args,**kwargs):
        seen.update(kwargs); return {"results":[]}
    monkeypatch.setattr(mcp_tools,"call_pg_experience_recall",recalled)
    started=time.monotonic()
    host.serve_rpc(SimpleNamespace(principal_id="p"),
        dict(id=1,method="tools/call",params=dict(name="experience_recall",
             arguments=dict(query="q",mode="hook",timeout_ms=120))),publish=lambda x:None)
    assert 0<seen["deadline"]-started<.13



def test_actual_http_auth_stall_is_inside_recall_header_budget(tmp_path):
    import hashlib
    import http.client
    import json
    from types import MappingProxyType
    from evolvmem.history_pg.unified_mcp_host import UnifiedMcpHost
    from evolvmem.lan_server import make_http_server
    listener=socket.socket();listener.bind(("127.0.0.1",0));listener.listen()
    stop=threading.Event(); accepted=threading.Event()
    def stall():
        peer,_=listener.accept();accepted.set()
        try: stop.wait(1)
        finally: peer.close()
    stall_thread=threading.Thread(target=stall,daemon=True);stall_thread.start()
    secret=tmp_path/"reader-password";secret.write_text("synthetic-password");secret.chmod(0o600)
    cc=SimpleNamespace(host="127.0.0.1",port=listener.getsockname()[1],database="postgres",
        reader_role="postgres",reader_password_ref="reader",
        coordinator_role="postgres",coordinator_password_ref="reader",
        secret_files=MappingProxyType({"reader":str(secret)}),allow_loopback_disable_ssl=True)
    host=UnifiedMcpHost.__new__(UnifiedMcpHost);host.control_store=SimpleNamespace(config=cc)
    host.owner_id="alice";host.tokens={hashlib.sha256(b"synthetic-token").hexdigest():{"user_id":"alice","device_id":"d"}}
    host.request_timeout_seconds=5;host._lock=threading.Lock();host._principal_to_user={}
    server=make_http_server(None,host="127.0.0.1",port=0,library_host=host,allow_legacy=False)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    client=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=.6)
    try:
        started=time.monotonic()
        client.request("POST","/mcp",json.dumps(dict(id=1,method="tools/call",
            params=dict(name="experience_recall",arguments=dict(query="q",mode="hook",timeout_ms=120)))),
            {"Content-Type":"application/json","Authorization":"Bearer synthetic-token",
             "X-EvolvMem-Recall-Budget-Ms":"120"})
        response=client.getresponse();response.read()
        assert accepted.is_set()
        assert .1 <= time.monotonic()-started < .3
        assert response.status==503
    finally:
        client.close();stop.set();listener.close();stall_thread.join(1)
        server.shutdown();server.server_close();thread.join(2)



def test_http_header_budget_covers_slow_body_read():
    import http.client
    import uuid
    from evolvmem.lan_server import make_http_server
    from evolvmem.library_foundation import RequestIdentity
    identity=RequestIdentity(principal_id=str(uuid.uuid4()),credential_id=str(uuid.uuid4()),
        device_id=str(uuid.uuid4()),credential_epoch=1)
    class Host:
        request_timeout_seconds=.5
        def authenticate(self,token,*,deadline=None):
            return identity
        def serve_rpc(self,*args,**kwargs):
            pytest.fail("incomplete request cannot dispatch a tool")
    server=make_http_server(None,host="127.0.0.1",port=0,library_host=Host(),allow_legacy=False)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    client=socket.create_connection(("127.0.0.1",server.server_port));client.settimeout(.4)
    try:
        start=time.monotonic()
        client.sendall(b"POST /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nAuthorization: Bearer synthetic\r\nX-EvolvMem-Recall-Budget-Ms: 80\r\nContent-Length: 100\r\n\r\n{")
        response=client.recv(4096)
        assert time.monotonic()-start<.3
        assert b"400" in response or b"408" in response
    finally:
        client.close();server.shutdown();server.server_close();thread.join(2)



def test_manual_update_reaches_writer_with_request_and_revision(monkeypatch):
    from evolvmem.history_pg import unified_mcp_host as u
    seen={}
    class Store:
        def __init__(self,settings): pass
        def write(self,source,actor,body,submitted,**kwargs):
            seen.update(source=source,actor=actor,body=body,submitted=submitted)
            return 200,{"ok":True}
    monkeypatch.setattr(u,"PgExperienceStore",Store)
    host=u.UnifiedMcpHost.__new__(u.UnifiedMcpHost)
    host._exp_settings=lambda:object();host.tokens={}
    args=dict(id="manual-"+"a"*32,revision=3,category="业务经验",
        problem="问题",solution="办法",reusable_experience="规则",request_id="manual-update-request")
    assert host._handle_tool("alice",SimpleNamespace(principal_id="p"),
        "manual_experience_update",args,1000)=={"ok":True}
    assert seen["body"]["action"]=="update"
    assert seen["body"]["revision"]==3
    assert seen["submitted"]["request_id"]=="manual-update-request"
    assert seen["source"]=={"user_id":"alice"}


@pytest.mark.parametrize("value",["0","1801","-1","1.5","NaN","1 0"])
def test_invalid_recall_budget_header_is_rejected_before_auth(value):
    import http.client
    from evolvmem.lan_server import make_http_server
    class Host:
        def authenticate(self,token,*,deadline=None):
            pytest.fail("invalid header cannot reach authentication")
        def serve_rpc(self,*args,**kwargs): pass
    server=make_http_server(None,host="127.0.0.1",port=0,library_host=Host(),allow_legacy=False)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    client=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=.5)
    try:
        client.request("POST","/mcp","{}",{"Content-Type":"application/json",
            "Authorization":"Bearer synthetic","X-EvolvMem-Recall-Budget-Ms":value})
        response=client.getresponse();response.read()
        assert response.status==400
    finally:
        client.close();server.shutdown();server.server_close();thread.join(1)



@pytest.mark.parametrize("change",[{"unexpected":1},{"timeout_ms":1801},{"query":"q"*4001},
    {"project":False},{"constraints":{"x":False}},{"constraints":{str(i):"x" for i in range(21)}},
    {"mode":"unknown"},{"session_id":"x"*129}])
def test_recall_arguments_have_strict_public_contract(change):
    from evolvmem.experience_v2 import mcp_tools as m
    assert m.call_pg_experience_recall({"query":"q",**change},owner_id="alice",
        settings=object()).get("error")=="invalid_arguments"


def test_recall_catalog_is_explicit_and_manual_update_is_registered():
    from evolvmem.history_pg.unified_mcp_host import UnifiedMcpHost,_TOOL_NAMES
    host=UnifiedMcpHost.__new__(UnifiedMcpHost)
    spec=next(x for x in host._tool_specs() if x["name"]=="experience_recall")["inputSchema"]
    assert spec["additionalProperties"] is False
    assert spec["properties"]["timeout_ms"]["maximum"]==1800
    assert spec["properties"]["query"]["maxLength"]==4000
    assert "manual_experience_update" in _TOOL_NAMES


def test_hook_mode_records_hook_channel(monkeypatch):
    from evolvmem.experience_v2 import mcp_tools as m
    @contextmanager
    def opened(*args,**kwargs): yield object()
    seen={}
    def recalled(conn,**kwargs): seen.update(kwargs);return {"results":[]}
    monkeypatch.setattr(m,"open_experience_deadline",opened)
    monkeypatch.setattr(m,"recall_pg_experiences",recalled)
    m.call_pg_experience_recall(dict(query="q",mode="hook"),owner_id="alice",settings=object())
    assert seen["channel"]=="hook"



def test_host_rejects_success_returned_after_recall_deadline(monkeypatch):
    import json
    from evolvmem.history_pg.unified_mcp_host import UnifiedMcpHost
    host=UnifiedMcpHost.__new__(UnifiedMcpHost)
    host.request_timeout_seconds,host.allow_sleep_probe=5,False
    host._resolve_user_id=lambda identity,**kw:"alice"
    def late(*args,**kwargs):
        time.sleep(.04);return {"results":[]}
    host._handle_tool=late;out=[]
    host.serve_rpc(SimpleNamespace(principal_id="p"),dict(id=1,method="tools/call",
        params=dict(name="experience_recall",arguments=dict(query="q",timeout_ms=20))),
        publish=out.append)
    assert out[0]["result"]["isError"]
    assert json.loads(out[0]["result"]["content"][0]["text"])["error"]=="request_timeout"



def test_http_auth_and_tool_share_original_budget(monkeypatch):
    import hashlib,http.client,json,uuid
    from evolvmem.history_pg.unified_mcp_host import UnifiedMcpHost
    from evolvmem.lan_server import make_http_server
    from evolvmem.experience_v2 import mcp_tools
    principal=str(uuid.uuid4());seen={}
    host=UnifiedMcpHost.__new__(UnifiedMcpHost)
    host.owner_id="alice";host.request_timeout_seconds=5;host.allow_sleep_probe=False
    host.tokens={hashlib.sha256(b"synthetic").hexdigest():{"user_id":"alice","device_id":"d"}}
    host._lock=threading.Lock();host._principal_to_user={}
    def rows(sql,params,deadline):
        if "user_id,principal_id,enabled" in sql:
            time.sleep(.05)
            return [{"user_id":"alice","principal_id":principal,"enabled":True}]
        return [{"user_id":"alice"}]
    host._control_rows=rows;host._exp_settings=lambda:object()
    def recalled(args,**kw):seen.update(kw);return {"results":[]}
    monkeypatch.setattr(mcp_tools,"call_pg_experience_recall",recalled)
    server=make_http_server(None,host="127.0.0.1",port=0,library_host=host,allow_legacy=False)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    client=http.client.HTTPConnection("127.0.0.1",server.server_port,timeout=.5)
    try:
        start=time.monotonic()
        client.request("POST","/mcp",json.dumps(dict(id=1,method="tools/call",
            params=dict(name="experience_recall",arguments=dict(query="q",timeout_ms=120)))),
            {"Content-Type":"application/json","Authorization":"Bearer synthetic",
             "X-EvolvMem-Recall-Budget-Ms":"120"})
        response=client.getresponse();response.read()
        assert response.status==200
        assert .1<seen["deadline"]-start<.13
        assert seen["deadline"]-time.monotonic()<.08
    finally:
        client.close();server.shutdown();server.server_close();thread.join(1)

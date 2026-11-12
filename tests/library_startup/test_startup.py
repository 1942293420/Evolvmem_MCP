"""New local startup/routing checks; never bind a port or connect a database."""
import json
from contextlib import contextmanager,ExitStack
from email.message import Message
import io
import os
import stat
import tempfile
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch,Mock
from uuid import UUID

ROOT=Path(__file__).resolve().parents[1]
OLD=ROOT.parent/'pg-library-host-20261101-v2'/'candidate'/'evolvmem'
WIRING=ROOT.parent/'pg-project-acl-wiring-20261101'/'candidate'/'evolvmem'
ACL=ROOT.parent/'pg-project-acl-20261101'/'candidate'/'evolvmem'
artifact_mode=(ROOT/'candidate'/'evolvmem').is_dir()
source_paths=([ROOT/'candidate'/'evolvmem',WIRING,OLD] if artifact_mode else [ROOT.parent/'evolvmem'])
package=types.ModuleType('evolvmem');package.__path__=[str(p) for p in source_paths]
sys.modules['evolvmem']=package
acl_paths=([WIRING/'library_project_acl',ACL/'library_project_acl'] if artifact_mode else [ROOT.parent/'evolvmem'/'library_project_acl'])
acl=types.ModuleType('evolvmem.library_project_acl');acl.__path__=[str(p) for p in acl_paths]
sys.modules[acl.__name__]=acl
from evolvmem import library_startup as startup_module
from evolvmem import lan_server
from evolvmem.library_startup import (parse_library_backend,request_backend,requires_library_backend,
    ServerStartup,build_library_host,read_private_secret,load_server_configuration)
from evolvmem.library_foundation import FoundationError,RequestIdentity
from evolvmem.library_host import LibraryHost
from evolvmem.lan_config import LanSettings

def uid(n):return str(UUID(int=n))
def config():
    secret=str((ROOT/'test-only-password').resolve())
    control=dict(host='localhost',port=5432,database='catalog',role='catalog_reader',password_ref='catalog_password')
    runtime=dict(connection_ref='device_one',library_id=uid(1),principal_id=uid(2),credential_id=uid(3),device_id=uid(4),
                 host='localhost',port=5432,database='content_one',role='runtime_reader',password_ref='content_password')
    return dict(enabled=True,legacy_mode='reject',semantic_merge_enabled=False,model_contract='test-model',
                control=control,content_bindings=[runtime],secret_files=dict(catalog_password=secret,content_password=secret))

def settings():
    return LanSettings(ROOT/'nonexistent-data',ROOT/'nonexistent-owner',dict(jiangli='1'*64,kane='2'*64),embedding_enabled=False)

@contextmanager
def handler(path='/mcp',*,pg=True,allow_legacy=True,backend_header=None,pg_error=None,body=None):
    """Exercise the real nested handler with no socket/server constructor."""
    calls=[];replies=[]
    class Server:
        def __init__(self,address,handler_class):self.handler_class=handler_class
        def server_close(self):pass
    class Adapter:
        def __init__(self,runtime):pass
        def handle_request(self,*args,**kwargs):calls.append('sqlite_dispatch');return dict(result='legacy')
    lan_tools=types.ModuleType('evolvmem.lan_tools');lan_tools.LanTools=Adapter
    def legacy_auth(token):calls.append('sqlite_auth');return 'jiangli'
    runtime=(types.SimpleNamespace(settings=types.SimpleNamespace(authenticate=legacy_auth),
                                   start_capture_worker=lambda adapter:None) if allow_legacy else None)
    host=None
    if pg:
        host=object.__new__(LibraryHost)
        guard=types.SimpleNamespace(check=lambda:None,remaining_seconds=lambda:30)
        @contextmanager
        def scope(connection):calls.append('pg_scope');yield guard
        def authenticate(token):
            calls.append('pg_auth')
            if pg_error:raise FoundationError(pg_error)
            return RequestIdentity(uid(2),uid(3),uid(4),1)
        host.request_scope=scope;host.authenticate=authenticate;host.close=lambda:None
        def serve(identity,request,*,publish):calls.append('pg_dispatch');publish(dict(jsonrpc='2.0',id=request.get('id'),result='pg'))
        host.serve_rpc=serve
    with patch.object(lan_server,'ThreadingHTTPServer',Server),patch.dict(sys.modules,{'evolvmem.lan_tools':lan_tools}):
        server=lan_server.make_http_server(runtime,host='127.0.0.1',port=1234,library_host=host,allow_legacy=allow_legacy)
    instance=object.__new__(server.handler_class)
    instance.path=path;instance.headers=Message();instance.headers['Authorization']='Bearer synthetic-shared-token'
    if backend_header is not None:instance.headers['X-EvolvMem-Backend']=backend_header
    raw=json.dumps(body or dict(jsonrpc='2.0',id=1,method='ping')).encode()
    instance.headers['Content-Type']='application/json';instance.headers['Content-Length']=str(len(raw))
    instance.rfile=io.BytesIO(raw);instance.connection=types.SimpleNamespace(settimeout=lambda _:None)
    instance._deadline_guard=None;instance._response_started=False;instance.client_address=('127.0.0.1',1)
    def reply(status,payload=None,headers=None):
        instance._response_started=True;replies.append((status,payload))
    instance._reply=reply
    with ExitStack() as stack:
        instance._request_stack=stack
        yield instance,calls,replies

class StartupChecks(unittest.TestCase):
    def test_disabled_is_default_and_does_not_require_credentials(self):
        self.assertIsNone(parse_library_backend(None))
        self.assertIsNone(parse_library_backend(dict(enabled=False)))
    def test_semantic_merge_true_or_missing_is_rejected(self):
        for value in (True,None):
            payload=config()
            if value is None:payload.pop('semantic_merge_enabled')
            else:payload['semantic_merge_enabled']=value
            with self.assertRaises(ValueError):parse_library_backend(payload)
    def test_existing_identifiers_and_file_refs_are_preserved(self):
        parsed=parse_library_backend(config())
        self.assertEqual(parsed.content_bindings[0].device_id,uid(4))
        self.assertEqual(parsed.content_bindings[0].library_id,uid(1))
        self.assertEqual(parsed.control_binding.password_ref,'catalog_password')
        self.assertEqual(parsed.legacy_mode,'reject')
    def test_explicit_postgres_never_selects_legacy(self):
        self.assertEqual(request_backend('/mcp',['postgres'],True,True),'postgres')
        self.assertEqual(request_backend('/library/mcp',[],True,True),'postgres')
        self.assertEqual(request_backend('/mcp',[],True,False),'postgres')
        with self.assertRaises(FoundationError):request_backend('/library/mcp',[],False,True)
        with self.assertRaises(FoundationError):request_backend('/mcp',['postgres','postgres'],True,True)
    def test_legacy_body_cannot_silently_carry_pg_selection(self):
        self.assertTrue(requires_library_backend(dict(method='tools/call',params=dict(name='memory_add',arguments=dict(library_id=uid(1))))))
        self.assertTrue(requires_library_backend(dict(method='tools/call',params=dict(name='library_acl_update',arguments={}))))
        self.assertFalse(requires_library_backend(dict(method='tools/call',params=dict(name='memory_add',arguments=dict(key='a',value='b')))))

    def test_actual_builder_constructs_enabled_host_without_connecting(self):
        connector=Mock(side_effect=AssertionError('must not connect at construction'))
        secrets=[]
        host=build_library_host(ServerStartup(settings(),parse_library_backend(config())),connector=connector,
            secret_reader=lambda path:secrets.append(path) or 'synthetic-password')
        self.assertIsInstance(host,LibraryHost)
        self.assertFalse(host._semantic_merge)
        self.assertTrue(host._enabled)
        self.assertEqual(len(secrets),2)
        connector.assert_not_called();host.close()

    def test_main_pg_only_passes_host_into_existing_http_factory_without_sqlite(self):
        selected=ServerStartup(settings(),parse_library_backend(config()))
        host=Mock();server=Mock();server.serve_forever.side_effect=KeyboardInterrupt
        forbidden_runtime=Mock(side_effect=AssertionError('SQLite must not start'))
        runtime_module=types.ModuleType('evolvmem.lan_runtime');runtime_module.LanRuntime=forbidden_runtime
        with patch.object(lan_server,'load_server_configuration',return_value=selected),\
             patch.object(lan_server,'build_library_host',return_value=host),\
             patch.object(lan_server,'make_http_server',return_value=server) as factory,\
             patch.dict(sys.modules,{'evolvmem.lan_runtime':runtime_module}):
            lan_server.main(['--config','synthetic-config.json'])
        factory.assert_called_once_with(None,host=selected.settings.host,port=selected.settings.port,
                                        library_host=host,allow_legacy=False)
        forbidden_runtime.assert_not_called();server.server_close.assert_called_once();host.close.assert_called_once()

    def test_main_default_preserves_legacy_runtime_and_cleans_up(self):
        selected=ServerStartup(settings());runtime=Mock();server=Mock();server.serve_forever.side_effect=KeyboardInterrupt
        runtime_factory=Mock(return_value=runtime)
        runtime_module=types.ModuleType('evolvmem.lan_runtime');runtime_module.LanRuntime=runtime_factory
        with patch.object(lan_server,'load_server_configuration',return_value=selected),\
             patch.object(lan_server,'make_http_server',return_value=server) as factory,\
             patch.dict(sys.modules,{'evolvmem.lan_runtime':runtime_module}):
            lan_server.main(['--config','synthetic-config.json'])
        runtime_factory.assert_called_once_with(selected.settings);runtime.initialize.assert_called_once()
        self.assertIsNone(factory.call_args.kwargs['library_host']);runtime.close.assert_called_once()

    def test_startup_failure_cannot_leave_a_legacy_worker_or_listener(self):
        selected=ServerStartup(settings(),parse_library_backend(config()))
        with patch.object(lan_server,'load_server_configuration',return_value=selected),\
             patch.object(lan_server,'build_library_host',side_effect=FoundationError('library_secret_unavailable')),\
             patch.object(lan_server,'make_http_server') as factory:
            with self.assertRaises(FoundationError):lan_server.main(['--config','synthetic-config.json'])
        factory.assert_not_called()

    def test_failure_after_host_construction_closes_both_host_and_legacy_runtime(self):
        payload=config();payload['legacy_mode']='allow'
        selected=ServerStartup(settings(),parse_library_backend(payload))
        for failure_stage in ('initialize','listener'):
            runtime=Mock();host=Mock();factory=Mock()
            if failure_stage=='initialize':runtime.initialize.side_effect=RuntimeError('synthetic failure')
            else:factory.side_effect=RuntimeError('synthetic failure')
            runtime_module=types.ModuleType('evolvmem.lan_runtime');runtime_module.LanRuntime=lambda _:runtime
            with patch.object(lan_server,'load_server_configuration',return_value=selected),\
                 patch.object(lan_server,'build_library_host',return_value=host),\
                 patch.object(lan_server,'make_http_server',factory),\
                 patch.dict(sys.modules,{'evolvmem.lan_runtime':runtime_module}):
                with self.assertRaises(RuntimeError):lan_server.main(['--config','synthetic-config.json'])
            host.close.assert_called_once();runtime.close.assert_called_once()

    def test_explicit_pg_http_uses_pg_even_when_old_token_would_match(self):
        for path,header in (('/library/mcp',None),('/mcp','postgres')):
            with handler(path,backend_header=header) as (request,calls,replies):request._post()
            self.assertIn('pg_dispatch',calls);self.assertNotIn('sqlite_auth',calls);self.assertNotIn('sqlite_dispatch',calls)
            self.assertEqual(replies[-1][0],200)

    def test_pg_auth_failure_never_retries_sqlite(self):
        for failure,status in (('unauthorized',401),('catalog_unavailable',503)):
            with handler('/library/mcp',pg_error=failure) as (request,calls,replies):request._post()
            self.assertEqual(replies[-1][0],status);self.assertNotIn('sqlite_auth',calls);self.assertNotIn('sqlite_dispatch',calls)

    def test_old_endpoint_pg_body_is_rejected_before_sqlite_dispatch(self):
        body=dict(jsonrpc='2.0',id=1,method='tools/call',params=dict(name='memory_add',arguments=dict(library_id=uid(1),key='a',value='b')))
        with handler(body=body) as (request,calls,replies):request._post()
        self.assertIn('sqlite_auth',calls);self.assertNotIn('sqlite_dispatch',calls)
        self.assertEqual(replies[-1][0],409)

    def test_disabled_pg_endpoint_and_duplicate_header_fail_closed(self):
        with handler('/library/mcp',pg=False) as (request,calls,replies):request._post()
        self.assertEqual(replies[-1][0],503);self.assertFalse(calls)
        with handler('/mcp',backend_header='postgres') as (request,calls,replies):
            request.headers['X-EvolvMem-Backend']='postgres';request._post()
        self.assertEqual(replies[-1][0],400);self.assertFalse(calls)

    def test_default_legacy_http_still_uses_old_adapter(self):
        with handler(pg=False) as (request,calls,replies):request._post()
        self.assertEqual(calls,['sqlite_auth','sqlite_dispatch']);self.assertEqual(replies[-1][0],200)

    def test_private_secret_rejects_bad_owner_permissions_type_and_oversize(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'synthetic-secret';path.write_bytes(b'synthetic-password\n')
            good=types.SimpleNamespace(st_uid=1000,st_mode=stat.S_IFREG|0o600)
            bad=[types.SimpleNamespace(st_uid=1001,st_mode=stat.S_IFREG|0o600),
                 types.SimpleNamespace(st_uid=1000,st_mode=stat.S_IFREG|0o640),
                 types.SimpleNamespace(st_uid=1000,st_mode=stat.S_IFDIR|0o600)]
            with patch.object(startup_module.os,'geteuid',return_value=1000,create=True):
                for info in bad:
                    with patch.object(startup_module.os,'fstat',return_value=info):
                        with self.assertRaisesRegex(FoundationError,'library_secret_unavailable'):read_private_secret(path)
                with patch.object(startup_module.os,'fstat',return_value=good):
                    self.assertEqual(read_private_secret(path),'synthetic-password')
                    path.write_bytes(b'x'*4097)
                    with self.assertRaises(FoundationError):read_private_secret(path)

    def test_nonregular_inputs_are_nonblocking_and_rejected_before_read(self):
        # Windows simulation only: no FIFO is created or opened. Assert the
        # Linux flags and descriptor ownership using a nonregular fstat result.
        path=(ROOT/'synthetic-fifo').resolve()
        fifo=types.SimpleNamespace(st_uid=1000,st_mode=stat.S_IFIFO|0o600)
        flag=getattr(os,'O_NONBLOCK',0x400000)
        for read,expected_error in ((read_private_secret,FoundationError),(load_server_configuration,ValueError)):
            with patch.object(startup_module.os,'O_NONBLOCK',flag,create=True),\
                 patch.object(startup_module.os,'open',return_value=901) as opened,\
                 patch.object(startup_module.os,'fstat',return_value=fifo),\
                 patch.object(startup_module.os,'fdopen',side_effect=AssertionError('must reject before read')) as wrapped,\
                 patch.object(startup_module.os,'close') as closed:
                with self.assertRaises(expected_error):read(path)
            self.assertTrue(opened.call_args.args[1]&flag)
            wrapped.assert_not_called();closed.assert_called_once_with(901)

    def test_fdopen_failure_still_closes_the_untransferred_descriptor(self):
        good=types.SimpleNamespace(st_uid=1000,st_mode=stat.S_IFREG|0o600)
        with patch.object(startup_module.os,'geteuid',return_value=1000,create=True),\
             patch.object(startup_module.os,'open',return_value=902),\
             patch.object(startup_module.os,'fstat',return_value=good),\
             patch.object(startup_module.os,'fdopen',side_effect=OSError('synthetic error')) as wrapped,\
             patch.object(startup_module.os,'close') as closed:
            with self.assertRaises(FoundationError):read_private_secret((ROOT/'synthetic-file').resolve())
        wrapped.assert_called_once();closed.assert_called_once_with(902)

    def test_missing_settings_or_semantic_merge_are_clear_configuration_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'config.json'
            path.write_text('{}')
            with self.assertRaisesRegex(ValueError,'invalid_server_configuration'):load_server_configuration(path)
            data=dict(data_dir=tmp,owner_data_dir=tmp,token_hashes=dict(jiangli='1'*64,kane='2'*64),library_backend=config())
            data['library_backend']['semantic_merge_enabled']=True
            path.write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError,'unsupported_semantic_merge_configuration'):load_server_configuration(path)

if __name__=='__main__':unittest.main()

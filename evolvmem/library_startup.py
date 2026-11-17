"""Trusted LAN startup composition. No credential creation or database bootstrap."""
from dataclasses import dataclass,field
import json
import math
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType

from evolvmem.config import Config
from evolvmem.lan_config import LanSettings
from evolvmem.library_catalog import ControlBinding
from evolvmem.library_foundation import ConnectionBinding,FoundationError
from evolvmem.library_host import LibraryHost
from evolvmem.library_project_acl.worker import ProjectionTarget

_REFERENCE=re.compile(r'[A-Za-z][A-Za-z0-9_.-]{0,127}')

class StartupConfigurationError(ValueError):
    """Content-free configuration diagnostics safe to print at startup."""

def _require(condition,code='invalid_library_startup_configuration'):
    if not condition:raise StartupConfigurationError(code)

def _keys(value,allowed,required):
    _require(isinstance(value,dict) and not(set(value)-set(allowed)) and set(required)<=set(value))

def _private_stat(info):
    _require(hasattr(os,'geteuid') and info.st_uid==os.geteuid()
             and stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode)==0o600,
             'unsafe_private_configuration')

def _read_bounded_file(path,limit,*,private=False):
    descriptor=None
    try:
        # O_NONBLOCK prevents a mistaken FIFO path from hanging before fstat.
        # Normal files retain ordinary reads. Private secret files also refuse
        # symlinks; legacy config-file symlinks keep their existing behavior.
        flags=os.O_RDONLY|getattr(os,'O_NONBLOCK',0)
        if private:flags|=getattr(os,'O_NOFOLLOW',0)
        descriptor=os.open(path,flags)
        info=os.fstat(descriptor)
        _require(stat.S_ISREG(info.st_mode),'nonregular_configuration_file')
        if private:_private_stat(info)
        handle=os.fdopen(descriptor,'rb')
        descriptor=None  # Transfer ownership only AFTER fdopen succeeds.
        with handle:raw=handle.read(limit+1)
        _require(len(raw)<=limit,'configuration_file_too_large')
        return raw,info
    finally:
        if descriptor is not None:os.close(descriptor)

def read_private_secret(path):
    """Same current-owner/0600/bounded-file convention as JsonClient tokens.

    This concrete resolver serves the existing password_ref callback contract.
    It never writes, expands environment variables, or prints secret contents.
    """
    try:
        _require(Path(path).is_absolute())
        value,_info=_read_bounded_file(path,4096,private=True)
        value=value.decode('utf-8').removesuffix('\n').removesuffix('\r')
        _require(bool(value) and not any(c in value for c in ('\0','\r','\n')))
        return value
    except Exception:
        raise FoundationError('library_secret_unavailable') from None

class FileSecretResolver:
    def __init__(self,files,*,reader=read_private_secret):
        self._files=MappingProxyType(dict(files));self._reader=reader
    def __call__(self,reference):
        if reference not in self._files:
            raise FoundationError('library_secret_unavailable')
        return self._reader(self._files[reference])
    def validate(self):
        for reference in self._files:
            secret=self(reference)
            _require(isinstance(secret,str) and bool(secret),'library_secret_unavailable')
            secret=None

@dataclass(frozen=True,slots=True)
class LibraryBackendSettings:
    legacy_mode: str
    control_binding: ControlBinding
    content_bindings: tuple
    secret_files: tuple = field(repr=False)
    model_contract: str = ''
    acl_control_binding: ControlBinding | None = None
    acl_projection_targets: tuple = ()
    max_requests: int = 2
    timeout_seconds: float = 30
    recall_enabled: bool = False
    vector_enabled: bool = False
    projection_enabled: bool = False
    projection_token_file: Path | None = field(default=None,repr=False)
    projection_library_ids: tuple = ()
    projection_interval_seconds: float = 1

@dataclass(frozen=True,slots=True)
class ServerStartup:
    settings: LanSettings = field(repr=False)
    library: LibraryBackendSettings | None = None
    @property
    def allow_legacy(self):return self.library is None or self.library.legacy_mode=='allow'

def parse_library_backend(value):
    if value is None:return None
    allowed={'enabled','legacy_mode','semantic_merge_enabled','control','content_bindings','secret_files',
             'model_contract','acl','max_requests','timeout_seconds','recall'}
    _keys(value,allowed,{'enabled'})
    _require(type(value['enabled']) is bool)
    if not value['enabled']:return None
    _keys(value,allowed,{'enabled','legacy_mode','semantic_merge_enabled','control','content_bindings','secret_files','model_contract'})
    _require(value['semantic_merge_enabled'] is False,'unsupported_semantic_merge_configuration')
    _require(value['legacy_mode'] in ('allow','reject'))
    _require(isinstance(value['model_contract'],str) and 1<=len(value['model_contract'])<=512)
    _require(isinstance(value['content_bindings'],list) and 1<=len(value['content_bindings'])<=2048)
    try:
        control=ControlBinding(**value['control'])
        bindings=tuple(ConnectionBinding(**row) for row in value['content_bindings'])
        acl_control=None;targets=[]
        if value.get('acl') is not None:
            acl=value['acl'];_keys(acl,{'control','targets'},{'control','targets'})
            acl_control=ControlBinding(**acl['control'])
            _require(isinstance(acl['targets'],list) and 1<=len(acl['targets'])<=256)
            for row in acl['targets']:
                _keys(row,{'library_id','connection_ref','connection'},{'library_id','connection_ref','connection'})
                runtime=tuple(b for b in bindings if (b.library_id,b.connection_ref)==(row['library_id'],row['connection_ref']))
                _require(bool(runtime))
                targets.append(ProjectionTarget(row['library_id'],row['connection_ref'],ControlBinding(**row['connection']),runtime))
        _require(len({t.library_id for t in targets})==len(targets))
        references={control.password_ref}|{b.password_ref for b in bindings}
        if acl_control is not None:references.add(acl_control.password_ref)
        references.update(t.connection.password_ref for t in targets)
        files=value['secret_files']
        _require(isinstance(files,dict) and set(files)==references and len(files)<=256)
        _require(all(isinstance(ref,str) and _REFERENCE.fullmatch(ref) and isinstance(path,str)
                     and Path(path).is_absolute() for ref,path in files.items()))
        maximum=value.get('max_requests',2);timeout=value.get('timeout_seconds',30)
        _require(type(maximum) is int and 1<=maximum<=32)
        _require(type(timeout) in (int,float) and math.isfinite(timeout) and 0<timeout<=120)
        recall_enabled=vector_enabled=projection_enabled=False
        token_file=None;worker_ids=();interval=1
        if value.get('recall') is not None:
            recall=value['recall'];_keys(recall,{'enabled','vector_enabled','projection'},{'enabled'})
            recall_enabled=recall['enabled'];vector_enabled=recall.get('vector_enabled',False)
            _require(type(recall_enabled) is bool and type(vector_enabled) is bool)
            _require(not vector_enabled or recall_enabled)
            if recall.get('projection') is not None:
                projection=recall['projection']
                _keys(projection,{'enabled','token_file','library_ids','interval_seconds'},{'enabled'})
                projection_enabled=projection['enabled'];_require(type(projection_enabled) is bool)
                if projection_enabled:
                    _require(recall_enabled and vector_enabled)
                    _keys(projection,{'enabled','token_file','library_ids','interval_seconds'},
                        {'enabled','token_file','library_ids'})
                    _require(isinstance(projection['token_file'],str) and Path(projection['token_file']).is_absolute())
                    token_file=Path(projection['token_file'])
                    ids=projection['library_ids']
                    _require(isinstance(ids,list) and 1<=len(ids)<=64
                        and all(isinstance(x,str) for x in ids) and len(set(ids))==len(ids)
                        and set(ids)<={b.library_id for b in bindings})
                    worker_ids=tuple(ids);interval=projection.get('interval_seconds',1)
                    _require(type(interval) in (int,float) and math.isfinite(interval) and .1<=interval<=60)
        return LibraryBackendSettings(value['legacy_mode'],control,bindings,tuple((key,Path(path)) for key,path in files.items()),
            value['model_contract'],acl_control,tuple(targets),maximum,timeout,
            recall_enabled,vector_enabled,projection_enabled,token_file,worker_ids,interval)
    except (TypeError,KeyError,ValueError,FoundationError):
        raise ValueError('invalid_library_startup_configuration') from None

def _no_duplicate_keys(pairs):
    result={}
    for key,value in pairs:
        _require(key not in result,'duplicate_configuration_key')
        result[key]=value
    return result

def load_server_configuration(path):
    """Read a single bounded snapshot; legacy files need no new secrets/setup."""
    try:
        raw,info=_read_bounded_file(path,1024*1024)
        payload=json.loads(raw,object_pairs_hook=_no_duplicate_keys)
        _require(isinstance(payload,dict))
        settings=LanSettings(data_dir=Path(payload['data_dir']),owner_data_dir=Path(payload['owner_data_dir']),
            token_hashes=payload['token_hashes'],host=payload.get('host','127.0.0.1'),port=payload.get('port',9378),
            embedding_enabled=payload.get('embedding_enabled',True))
        library=parse_library_backend(payload.get('library_backend'))
        if library is not None:_private_stat(info)
        return ServerStartup(settings,library)
    except StartupConfigurationError:
        raise
    except Exception:
        raise ValueError('invalid_server_configuration') from None

def build_library_host(startup,*,connector=None,secret_reader=read_private_secret):
    if not isinstance(startup,ServerStartup):raise ValueError('trusted_server_configuration_required')
    cfg=startup.library
    if cfg is None:return None
    resolver=FileSecretResolver(cfg.secret_files,reader=secret_reader)
    resolver.validate()  # Fail before LAN listeners or SQLite workers start.
    if connector is None:
        try:
            import psycopg
            _require(psycopg.capabilities.has_cancel_safe(check=True),'unsupported_pg_deadline_driver')
            connector=psycopg.connect
        except Exception:
            raise FoundationError('library_driver_unavailable') from None
    config=Config.from_file(startup.settings.owner_data_dir/'config.json',data_dir=startup.settings.owner_data_dir,
                            apply_environment=False,ensure_dirs=False)
    provider=host=None
    try:
        if cfg.projection_enabled:
            # Separate private token, never broaden database password references
            # or use a caller's HTTP credential for background work.
            token=secret_reader(cfg.projection_token_file)
            _require(isinstance(token,str) and bool(token),'library_secret_unavailable')
            token=None
        if cfg.vector_enabled:
            # Preserve the small/default Host's existing single-process owner.
            # Larger trusted admission budgets get four fixed local workers;
            # callers cannot select capacity, CPU affinity, or another model.
            if cfg.max_requests >= 4:
                from evolvmem.pg_vector_port.encoder import ManagedEmbeddingPool
                provider=ManagedEmbeddingPool(config)
            else:
                from evolvmem.pg_vector_port.encoder import ManagedEmbeddingProvider
                provider=ManagedEmbeddingProvider(config)
            provider.initialize()  # Existing configured local model only; no download.
            _require(provider.contract.key==cfg.model_contract,'embedding_contract_mismatch')
        host=LibraryHost(control_binding=cfg.control_binding,content_bindings=cfg.content_bindings,
            control_connector=connector,content_connector=connector,secret_resolver=resolver,config=config,
            model_contract=cfg.model_contract,semantic_merge_enabled=False,enabled=True,
            max_requests=cfg.max_requests,timeout_seconds=cfg.timeout_seconds,
            acl_control_binding=cfg.acl_control_binding,acl_projection_targets=cfg.acl_projection_targets,
            recall_enabled=cfg.recall_enabled,embedding_provider=provider)
        if cfg.projection_enabled:
            host.start_projection(token_loader=lambda:secret_reader(cfg.projection_token_file),
                library_ids=cfg.projection_library_ids,interval_seconds=cfg.projection_interval_seconds)
        return host
    except BaseException:
        try:
            if host is not None:host.close()
            elif provider is not None:provider.close()
        except BaseException:
            raise FoundationError('library_startup_cleanup_incomplete') from None
        raise

def request_backend(path,header_values,pg_enabled,legacy_allowed):
    if len(header_values)>1 or (header_values and header_values[0]!='postgres'):
        raise FoundationError('invalid_backend_selection')
    explicit=path=='/library/mcp' or bool(header_values)
    if explicit and not pg_enabled:raise FoundationError('postgres_backend_disabled')
    if explicit or (pg_enabled and not legacy_allowed):return 'postgres'
    return 'auto' if pg_enabled else 'legacy'

def requires_library_backend(request):
    if not isinstance(request,dict) or request.get('method')!='tools/call':return False
    params=request.get('params')
    if not isinstance(params,dict):return False
    if params.get('name') in ('library_acl_update','library_project_acl_update'):return True
    args=params.get('arguments')
    return isinstance(args,dict) and bool({'library_id','library_ids','selections','project_key','project_id'}&set(args))

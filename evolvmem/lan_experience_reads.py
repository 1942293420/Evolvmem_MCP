"""Independent LAN experience/public reads over unchanged retrieval algorithms."""
from contextlib import ExitStack
from copy import deepcopy
import sqlite3
import threading
import time

from evolvmem.context_retriever import ContextRetriever
from evolvmem.context_service import ContextService
from evolvmem.context_store import ContextStore
from evolvmem.lan_capability_reads import MAX_READERS
from evolvmem.lan_experience_snapshot import ExperienceSnapshot,ExperienceReadError,file_identity
from evolvmem.lan_experience_proof import SelectedExperienceProof
from evolvmem.lan_revision import RevisionReader
from evolvmem.lan_sharing import LanError
from evolvmem.request_health import background_read_scope,background_read_services
from evolvmem.vector_index import VectorIndex
from evolvmem import vector_provenance

SQL_SECONDS=30.


def _config_state(config):
    return tuple(sorted((k,repr(v)) for k,v in vars(config).items()))


def _vector_stamp(config):
    values=[]
    for path in (config.context_vector_path,vector_provenance.path(config),
                 config.context_vector_path.with_suffix(config.context_vector_path.suffix+'.dirty')):
        try:
            stat=path.stat()
            values.append((stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_mode,stat.st_uid,stat.st_gid))
        except FileNotFoundError:values.append(None)
    return tuple(values)


class ExperienceReader:
    def __init__(self,runtime,sharing):
        self.runtime,self.sharing=runtime,sharing
        self._paths=RevisionReader(runtime.settings)._paths
        self._files={name:file_identity(path) for name,path in self._paths.items()}
        self._servers={name:runtime.server_for('jiangli','public') if name=='public'
                       else runtime.server_for(name) for name in self._paths}
        self._residents={name:server.context_service for name,server in self._servers.items()}
        self._configs={name:deepcopy(server.config) for name,server in self._servers.items()}
        self._config_states={name:_config_state(config) for name,config in self._configs.items()}
        self._engines={name:service.embedding_engine for name,service in self._residents.items()}
        # Reuse capability's runtime-owned budget: two heavy recalls in total,
        # including different adapters and namespaces, not two of each kind.
        with runtime._resources_lock:
            if not hasattr(runtime,'_lan_capability_slots'):
                runtime._lan_capability_slots=threading.BoundedSemaphore(MAX_READERS)
            self._slots=runtime._lan_capability_slots

    def _check(self,names):
        if self.runtime._closed:raise ExperienceReadError('experience_read_closed')
        for name in names:
            server=self.runtime.server_for('jiangli','public') if name=='public' else self.runtime.server_for(name)
            if _config_state(server.config)!=self._config_states[name]:
                raise ExperienceReadError('health_boundary_changed')
            if (server is not self._servers[name] or server.context_service is not self._residents[name]
                    or server.context_service.embedding_engine is not self._engines[name]
                    or file_identity(self._paths[name])!=self._files[name]):
                raise ExperienceReadError('experience_read_changed')

    def read(self,user,args,space):
        if user not in ('jiangli','kane') or space not in ('personal','public','both'):
            raise LanError('invalid_arguments')
        names=(() if space=='public' else (user,))+(() if space=='personal' else ('public',))
        if not self._slots.acquire(timeout=.1):raise LanError('experience_read_busy')
        try:
            self._check(names)
            deadline=time.monotonic()+SQL_SECONDS
            health=self.runtime._background_health
            with ExitStack() as owned:
                snapshots,services,stamps,proofs,selected={},{},{},{},{}
                with background_read_scope(health,'experience_recall',tuple((n,self._servers[n]) for n in names)):
                    for name in names:
                        config=deepcopy(self._configs[name])
                        snapshot=ExperienceSnapshot(self._paths[name],expected=self._files[name],
                            deadline=deadline,closed=lambda:self.runtime._closed)
                        owned.callback(snapshot.close)
                        snapshots[name]=snapshot
                        store=ContextStore(config);store._conn=snapshot.conn
                        owned.callback(setattr,store,'_conn',None)
                        store._has_trigram=bool(snapshot.conn.execute(
                            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='context_layers_fts_trigram'").fetchone())
                        stamps[name]=_vector_stamp(config)
                        index_config=deepcopy(config)
                        index_config.lan_shared_vector_cache=False
                        index_config.embedding_http_url='';index_config.lan_mcp_client_config=''
                        index=VectorIndex(index_config,path=config.context_vector_path)
                        owned.callback(index.close)
                        try:index.initialize(dim=config.embedding_dim)
                        except Exception:index.close()  # original retriever's unavailable-vector fallback
                        engine=self._engines[name]
                        service=ContextService(config,store=store,vector_index=index,embedding_engine=engine,
                            retriever=ContextRetriever(config,store,index,engine))
                        service._mode,service._adapter=self._residents[name]._mode,self._residents[name]._adapter
                        services[name]=service
                    with background_read_services(tuple((services[n],self._residents[n]) for n in names)):
                        result=(self._servers[user]._experience_recall(args,service=services[user])
                                if space!='public' else {'cases':[],'methods':[]})
                        result['shared_knowledge']=(self.sharing.search(
                            {k:v for k,v in args.items() if k in ('query','project')},service=services['public'])
                            if space!='personal' else [])
                        for name in names:
                            rows=(result.get('shared_knowledge',[]) if name=='public' else result.get('results',[]))
                            selected[name]=tuple(row['id'] for row in rows)
                            proofs[name]=SelectedExperienceProof(services[name],selected[name],
                                public=name=='public',request_args=args,at=snapshots[name].observed_at)
                            snapshots[name].validate(selected[name],public=name=='public',proof=proofs[name])
                        self._check(names)
                        if any(_vector_stamp(self._configs[n])!=stamps[n] for n in names):
                            raise ExperienceReadError('experience_read_changed')
                # Exit health has now passed; check every monitored connection
                # again without borrowing resident transactions or stale tokens.
                for name,snapshot in snapshots.items():
                    snapshot.validate(selected[name],public=name=='public',proof=proofs[name])
                self._check(names)
                if any(_vector_stamp(self._configs[n])!=stamps[n] for n in names):
                    raise ExperienceReadError('experience_read_changed')
            return result
        except ExperienceReadError as exc:
            raise LanError(str(exc)) from None
        except (sqlite3.Error,OSError):
            raise LanError('experience_read_unavailable') from None
        finally:
            self._slots.release()

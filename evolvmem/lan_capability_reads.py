"""Request-owned capability queries; preserve current-source and vector gates."""
from copy import deepcopy
import sqlite3
import threading
from types import SimpleNamespace

from evolvmem.capability_index import CapabilityIndex
from evolvmem.capability_recall_eligibility import eligible_ids
from evolvmem.capability_retriever import CapabilityRetriever
from evolvmem.context_store import ContextStore
from evolvmem.lan_revision import RevisionReader
from evolvmem.lan_sharing import LanError

MAX_READERS=2


class CapabilityReader:
    def __init__(self,runtime):
        self.runtime=runtime
        self._files=RevisionReader(runtime.settings)
        self._configs={user:deepcopy(runtime.server_for(user).config) for user in ('jiangli','kane')}
        self._engines={user:runtime.server_for(user).context_service.embedding_engine for user in self._configs}
        self._handlers={user:runtime.server_for(user)._capability_search for user in self._configs}
        with runtime._resources_lock:
            if not hasattr(runtime,'_lan_capability_slots'):
                runtime._lan_capability_slots=threading.BoundedSemaphore(MAX_READERS)
            self._slots=runtime._lan_capability_slots

    def read(self,user,args):
        if self.runtime._closed:raise RuntimeError('LAN runtime is closed')
        if not self._slots.acquire(timeout=.1):raise LanError('capability_read_busy')
        store=index=None
        try:
            self._files._check()
            config=self._configs[user]
            store=ContextStore(config)
            path=self._files._paths[user]
            conn=sqlite3.connect(path.absolute().as_uri()+'?mode=ro',uri=True,timeout=.1)
            store._conn=conn
            conn.row_factory=sqlite3.Row
            conn.execute('PRAGMA trusted_schema=OFF')
            conn.execute('PRAGMA query_only=ON')
            # No outer BEGIN: vector encoding runs outside database snapshots,
            # candidate proof and final validation each own their read scope.
            index=CapabilityIndex(store,config,self._engines[user],eligible_ids=lambda ids:eligible_ids(store,ids))
            service=SimpleNamespace(config=config,store=store,embedding_engine=self._engines[user],capability_index=lambda:index)
            result=self._handlers[user](args,service=service)
            if 'error' not in result:
                try:
                    result['results']=CapabilityRetriever(service).validate_current_results(
                        result.get('results',[]),project=args.get('project'),module_id=args.get('module_id'))
                    result['count']=len(result['results'])
                except Exception:
                    result={'error':'context_unavailable'}
            self._files._check()
            if self.runtime._closed:raise RuntimeError('LAN runtime is closed')
            if conn.total_changes:raise RuntimeError('unexpected capability read write')
            return result
        finally:
            try:
                if index is not None:index.vector_index.close()
            finally:
                try:
                    if store is not None:store.close()
                finally:
                    self._slots.release()

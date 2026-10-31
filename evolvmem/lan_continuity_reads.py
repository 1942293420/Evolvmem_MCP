"""Bounded continuation reads with request-owned SQLite and workspace identity."""
from copy import deepcopy
import sqlite3
import threading
import time

from evolvmem.context_store import ContextStore
from evolvmem.continuity_service import ContinuityService
from evolvmem.lan_context import RemoteWorkspaceIdentity
from evolvmem.lan_revision import RevisionReader
from evolvmem.lan_sharing import LanError
from evolvmem.workspace_identity import WorkspaceIdentityProvider

READ_TOOLS=frozenset({'continuity_resume','continuity_list','continuity_find'})
SQL_TIMEOUT_SECONDS=1.0
MAX_READERS=8


class ContinuityReader:
    def __init__(self,runtime):
        self.runtime=runtime
        self._files=RevisionReader(runtime.settings)
        self._configs={user:deepcopy(runtime.server_for(user).config) for user in ('jiangli','kane')}
        # These handlers accept an explicit domain service. They only validate
        # and render; no resident identity, store or lazy service is accessed.
        self._handlers={user:{name:getattr(runtime.server_for(user),'_'+name) for name in READ_TOOLS}
                        for user in self._configs}
        with runtime._resources_lock:
            if not hasattr(runtime,'_lan_continuity_slots'):
                runtime._lan_continuity_slots=threading.BoundedSemaphore(MAX_READERS)
            self._slots=runtime._lan_continuity_slots

    def read(self,user,name,args,*,owner,device_id,snapshot):
        if self.runtime._closed:raise RuntimeError('LAN runtime is closed')
        if not self._slots.acquire(timeout=.1):raise LanError('continuity_read_busy')
        store=None
        try:
            self._files._check()
            config=self._configs[user]
            path=self._files._paths[user]
            store=ContextStore(config)
            conn=sqlite3.connect(path.absolute().as_uri()+'?mode=ro',uri=True,timeout=.1)
            store._conn=conn
            conn.row_factory=sqlite3.Row
            deadline=time.monotonic()+SQL_TIMEOUT_SECONDS
            conn.set_progress_handler(lambda:time.monotonic()>=deadline,100)
            conn.execute('PRAGMA trusted_schema=OFF')
            conn.execute('PRAGMA query_only=ON')
            conn.execute('BEGIN')
            # Pin the snapshot before any gate, binding, focus or body read.
            conn.execute('SELECT name FROM sqlite_master LIMIT 1').fetchone()
            local=WorkspaceIdentityProvider(config.data_dir/'workspace.key')
            provider=local if owner else RemoteWorkspaceIdentity(local,store,device_id,snapshot)
            options={} if owner else dict(repo_anchor=lambda _path:snapshot.copy(),ancestor=lambda *_args:None)
            service=ContinuityService(config,store,provider,**options)
            if not owner and args.get('workspace_path'):provider.private_key(args['workspace_path'])
            result=self._handlers[user][name](args,service=service)
            self._files._check()
            if self.runtime._closed:raise RuntimeError('LAN runtime is closed')
            if conn.total_changes:raise RuntimeError('unexpected continuation read write')
            return result
        finally:
            try:
                if store is not None:store.close()
            finally:
                self._slots.release()

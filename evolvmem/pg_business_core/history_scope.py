"""Trusted history read readiness bound to the actual LibrarySession snapshot."""
from dataclasses import dataclass
import re
import threading
from evolvmem.library_foundation import FoundationError
from evolvmem.library_foundation.sessions import LibrarySession
from evolvmem.library_project_acl.scope import ProjectScope

READ_CONTEXT='''SELECT schema_version,mode,policy_epoch,route_epoch,readiness_revision,mapping_revision,completion_hash
 FROM em_security.history_recall_context(%s,%s)'''

@dataclass(frozen=True,slots=True)
class HistoryReadContext:
    schema_version:int
    mode:str
    policy_epoch:int
    route_epoch:int
    readiness_revision:int
    mapping_revision:int
    completion_hash:str

class HistoryReadScope:
    def __init__(self,session,project_scope):
        if not isinstance(session,LibrarySession) or session._closed or session.route.action!='read':
            raise FoundationError('read_session_required')
        if not isinstance(project_scope,ProjectScope):raise FoundationError('project_scope_required')
        project_scope.require(session)
        self.session,self.project_scope=session,project_scope
        self._thread=threading.get_ident();self._connection=session._connection
        rows=session.execute(READ_CONTEXT,(project_scope.library_id,project_scope.project_id))
        if len(rows)!=1 or len(rows[0])!=7:raise FoundationError('history_read_domain_not_ready')
        row=tuple(rows[0])
        if (row[:4]!=(1,'history-preserved-v1',project_scope.policy_epoch,project_scope.route_epoch)
            or any(type(n) is not int or n<1 for n in row[4:6])
            or not isinstance(row[6],str) or re.fullmatch('[0-9a-f]{64}',row[6]) is None):
            raise FoundationError('history_read_domain_not_ready')
        self.context=HistoryReadContext(*row)
    def require(self):
        self.project_scope.require(self.session)
        if threading.get_ident()!=self._thread or self.session._connection is not self._connection:
            raise FoundationError('history_snapshot_changed')
        return self.context


def history_schema_present(session):
    """Catalog dispatch hint only. Call HistoryReadScope before every actual read."""
    if not isinstance(session,LibrarySession) or session._closed or session.route.action!='read':
        raise FoundationError('read_session_required')
    rows=session.execute("SELECT to_regclass('em_security.history_readiness') IS NOT NULL")
    if len(rows)!=1 or len(rows[0])!=1 or type(rows[0][0]) is not bool:
        raise FoundationError('history_schema_unknown')
    return rows[0][0]

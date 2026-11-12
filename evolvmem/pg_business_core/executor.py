"""Reserve intent, atomically commit effect+result, then confirm/publicize."""
import hashlib
import json
from evolvmem.library_foundation import FoundationError
from evolvmem.library_foundation.sessions import _RemoveDenied, _RemoveReady
from evolvmem.library_project_acl.scope import resolve_project_scope
from evolvmem.library_project_acl.coordinator import project_uuid
from evolvmem.context_migration import LegacyMemoryMigrator
from .core import PgBusinessCore,KeyLease,now
from .domain import UnsupportedBusiness
from .wire import LegacyWire

TOOLS=frozenset(('memory_add','memory_replace','memory_remove'))

def lock_number(*parts):
    payload=json.dumps(parts,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    return int.from_bytes(hashlib.sha256(payload).digest()[:8],'big',signed=True)

class PgBusinessExecutor:
    def __init__(self,manager,config,*,model_contract,semantic_merge_enabled,project_key):
        self.project_key,self.project_id=project_key,None if project_key is None else project_uuid(project_key)
        self.manager,self.config=manager,config
        self.model_contract,self.semantic_merge_enabled=model_contract,semantic_merge_enabled
    def _project_preflight(self,session,errors_as_results):
        try:
            return resolve_project_scope(session,self.project_key)
        except FoundationError as exc:
            # Only this completed, side-effect-free authorization SELECT may be
            # rendered as a denial. SQL failures, deadline/epoch faults and all
            # later effect/commit failures keep their original exception path.
            if not errors_as_results or exc.code!='project_scope_unavailable':
                raise
            return None
    def _core(self,session,lease=None,*,project_scope=None):
        return PgBusinessCore(session,self.config,lease=lease,model_contract=self.model_contract,
            project_scope=project_scope if project_scope is not None else resolve_project_scope(session,self.project_key))
    def _store_result(self,session,route,request_id,payload_hash,result):
        changed=session.execute('''UPDATE em_content.lan_write_requests SET result_json=%s::jsonb,completed_at=%s
            WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND principal_id=%s AND tool=%s AND request_id=%s AND payload_hash=%s AND result_json IS NULL RETURNING request_id''',
            (json.dumps(result,ensure_ascii=False,allow_nan=False),now(),route.library.library_id,self.project_id,
             route.identity.principal_id,route.tool,request_id,payload_hash))
        if len(changed)!=1:raise UnsupportedBusiness('receipt_state_changed')
    def _confirmed_result(self,session,route,request_id,payload_hash):
        rows=session.execute('''SELECT result_json FROM em_content.lan_write_requests
            WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND principal_id=%s AND tool=%s AND request_id=%s AND payload_hash=%s''',
            (route.library.library_id,self.project_id,route.identity.principal_id,route.tool,request_id,payload_hash))
        if len(rows)!=1 or rows[0][0] is None:raise UnsupportedBusiness('receipt_state_changed')
        return rows[0][0]
    def _remove_locator(self,route,args,*,request_id,preflight_errors_as_results):
        # Trusted internal callback: the locator transaction already ran the
        # real role/pin/credential/binding checks. It only returns immutable
        # ready material or an explicit typed denial, never a raw key.
        legacy_id=int(args.get('id',0))
        lib=route.library.library_id;principal=route.identity.principal_id
        def prepare(session):
            selected=self._project_preflight(session,preflight_errors_as_results)
            if selected is None:return _RemoveDenied({'error':'project_scope_unavailable'})
            row=self._core(session,project_scope=selected).get_by_id(legacy_id)
            key=row['key'] if row else 'missing-legacy-id:'+str(legacy_id)
            identity=LegacyMemoryMigrator.identity_key_for(key,1)
            keys=(lock_number('evolvmem-business-key-v2',lib,self.project_id,identity),
                  lock_number('evolvmem-business-request-v1',lib,principal,route.tool,request_id))
            return _RemoveReady(keys,identity)
        return prepare
    def mutate(self,route,args,*,request_id,publish=None,preflight_errors_as_results=False):
        if route.library.kind=='legacy':raise FoundationError('historical_library_read_only')
        if type(preflight_errors_as_results) is not bool:
            raise TypeError('boolean preflight error policy required')
        if self.project_key is None:
            raise FoundationError('project_key_required')
        tool=route.tool
        if tool not in TOOLS or route.action not in ('write','admin'):
            raise UnsupportedBusiness('unsupported_tool')
        if not isinstance(request_id,str) or not 1<=len(request_id)<=200:
            raise ValueError('stable request_id required')
        # Hash the complete input before reservation; NaN is not valid JSON.
        encoded=json.dumps(dict(version=2,project_key=self.project_key,project_id=self.project_id,arguments=args),
            ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
        payload_hash=hashlib.sha256(encoded.encode('utf-8')).hexdigest()
        lib=route.library.library_id;principal=route.identity.principal_id
        if tool=='memory_remove':
            scope_context=self.manager.keyed(route,None,_prepare_remove=self._remove_locator(
                route,args,request_id=request_id,preflight_errors_as_results=preflight_errors_as_results))
        else:
            key=args.get('key','')
            identity=LegacyMemoryMigrator.identity_key_for(key,1)
            keys=(lock_number('evolvmem-business-key-v2',lib,self.project_id,identity),
                  lock_number('evolvmem-business-request-v1',lib,principal,tool,request_id))
            scope_context=self.manager.keyed(route,keys)
        lease=None
        try:
            with scope_context as scope:
                if tool=='memory_remove':
                    if scope.early_denial is not None:
                        # Content already retired; this publishes the exact DTO
                        # once under the same held authority with no locks,
                        # receipt or effect.
                        return scope.publish(scope.early_denial,publish)
                    identity=scope.canonical_identity
                lease=KeyLease(lib,identity,self.project_id)
                with scope.transaction() as session:
                    selected_scope=self._project_preflight(session,preflight_errors_as_results)
                    replay=None;rejection=None
                    if selected_scope is None:
                        rejection='project_scope_unavailable'
                    else:
                        rows=session.execute('''SELECT payload_hash,result_json FROM em_content.lan_write_requests
                            WHERE library_id=%s AND project_id IS NOT DISTINCT FROM %s AND principal_id=%s AND tool=%s AND request_id=%s''',(lib,self.project_id,principal,tool,request_id))
                        if rows:
                            if rows[0][0]!=payload_hash:rejection='request_id_conflict'
                            elif rows[0][1] is None:rejection='request_indeterminate'
                            if rejection and not preflight_errors_as_results:
                                raise UnsupportedBusiness(rejection)
                            if rejection is None:replay=rows[0][1]
                        else:
                            session.execute('''INSERT INTO em_content.lan_write_requests(library_id,project_id,principal_id,tool,request_id,payload_hash,created_at)
                                VALUES(%s,%s,%s,%s,%s,%s,%s)''',(lib,selected_scope.project_id,principal,tool,request_id,payload_hash,now()))
                # Host-only opt-in: an existing receipt rejected the request
                # before any effect/intent write. Finish that read normally and
                # publish under the same live authority/key lease and deadline.
                # Never catch effect, commit, cancellation or cleanup failures.
                if rejection is not None:return scope.publish({'error':rejection},publish)
                if replay is not None:return scope.publish(replay,publish)
                with scope.transaction() as session:
                    wire=LegacyWire(self._core(session,lease),semantic_merge_enabled=self.semantic_merge_enabled)
                    result=getattr(wire,tool)(args)
                    # The exact DTO commits WITH its business effects. A lost
                    # commit reply is reconciled by this receipt, never by the
                    # current mutable key or by blindly repeating the write.
                    self._store_result(session,route,request_id,payload_hash,result)
                with scope.transaction(read_only=True) as session:
                    result=self._confirmed_result(session,route,request_id,payload_hash)
                return scope.publish(result,publish)
        finally:
            if lease is not None:lease.active=False
    def read(self,route,args,*,publish=None,preflight_errors_as_results=False):
        if type(preflight_errors_as_results) is not bool:
            raise TypeError('boolean preflight error policy required')
        if route.tool!='context_read' or route.action!='read':raise UnsupportedBusiness('unsupported_tool')
        if route.library.kind=='legacy':
            return self._history_read(route,args,publish=publish,preflight_errors_as_results=preflight_errors_as_results)
        from evolvmem.context_models import ContextReadRequest,ContextLayer,ContextValidationError
        from .native_store import PgNativeRecallStore
        from evolvmem.mcp_server import MemoryMCPServer
        def operation(session):
            selected=self._project_preflight(session,preflight_errors_as_results)
            if selected is None:return ({'error':'project_scope_unavailable'},None)
            try:request=ContextReadRequest(args.get('id'),ContextLayer(args.get('layer','l1')))
            except (ContextValidationError,ValueError,TypeError):return (MemoryMCPServer._context_error('invalid_arguments'),None)
            store=PgNativeRecallStore(session,selected);result=store.read(request)
            dto=(MemoryMCPServer._context_error(result.error_code) if result.error_code else dict(id=result.id,layer=result.layer.value,content=result.content))
            return dto,store.proof()
        def final(session,prior):
            if prior[0].get('error')=='project_scope_unavailable':return prior
            current=operation(session)
            if current!=prior:raise FoundationError('recall_selected_changed')
            return prior
        return self.manager.run_many((route,),operation,finalize=final,
            combine=lambda gathered:gathered[0][1][0],publish=publish)

    def _history_read(self,route,args,*,publish,preflight_errors_as_results):
        # The trusted legacy library metadata selects a mode probe. The actual
        # definer readiness gate still owns activation; request args cannot.
        from evolvmem.context_models import ContextReadRequest,ContextLayer,ContextValidationError
        from .history_qualification_sql import historical_mode
        from .history_store import PgHistoryRecallStore
        from evolvmem.mcp_server import MemoryMCPServer
        def operation(session):
            selected=self._project_preflight(session,preflight_errors_as_results)
            if selected is None:return ({'error':'project_scope_unavailable'},None,False)
            if not historical_mode(session):
                dto=LegacyWire(self._core(session,project_scope=selected),semantic_merge_enabled=self.semantic_merge_enabled).context_read(args)
                return dto,None,False
            try:request=ContextReadRequest(args.get('id'),ContextLayer(args.get('layer','l1')))
            except (ContextValidationError,ValueError,TypeError):return (MemoryMCPServer._context_error('invalid_arguments'),None,True)
            store=PgHistoryRecallStore(session,selected);result=store.read(request)
            dto=(MemoryMCPServer._context_error(result.error_code) if result.error_code else dict(id=result.id,layer=result.layer.value,content=result.content))
            return dto,store.proof(),True
        def final(session,prior):
            if prior[0].get('error')=='project_scope_unavailable':return prior
            current=operation(session)
            if current!=prior:raise FoundationError('recall_selected_changed')
            return prior
        return self.manager.run_many((route,),operation,finalize=final,
            combine=lambda gathered:gathered[0][1][0],publish=publish)

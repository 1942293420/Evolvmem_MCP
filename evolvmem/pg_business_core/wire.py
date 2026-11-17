"""Legacy wire DTO reuse; no transport registration or health-ready claim."""
from types import SimpleNamespace
from evolvmem.conflict_detector import ConflictDetector
from evolvmem.context_models import ContextLayer,ContextReadRequest,ContextValidationError
from evolvmem.mcp_server import MemoryMCPServer
from .domain import mutation_response,UnsupportedBusiness

class _MergeState:
    def __init__(self,enabled):self.enabled=enabled;self.reference=False
    @property
    def is_loaded(self):
        if self.enabled and not self.reference:raise UnsupportedBusiness('unsupported_semantic_merge')
        return False

class LegacyWire:
    def __init__(self,core,*,semantic_merge_enabled):
        self.core=core;self.config=core.config
        # Reuse the original validation/conflict/DTO methods without constructing
        # a server, opening SQLite, loading a model or fabricating a search hit.
        self.context_service=SimpleNamespace(legacy_facade=lambda:core,
            legacy_add=core.legacy_add,legacy_replace=core.legacy_replace,legacy_remove=core.legacy_remove)
        self.conflict_detector=ConflictDetector(core)
        self.engine=_MergeState(semantic_merge_enabled)
    _mutation_response=staticmethod(mutation_response)
    def memory_add(self,args):
        # Original code excludes references even when an engine is loaded.
        self.engine.reference=args.get('tier')=='reference'
        return MemoryMCPServer._memory_add(self,args)
    def memory_replace(self,args):return MemoryMCPServer._memory_replace(self,args)
    def memory_remove(self,args):return MemoryMCPServer._memory_remove(self,args)
    def context_read(self,args):
        try:
            request=ContextReadRequest(args.get('id'),ContextLayer(args.get('layer','l1')))
        except (ContextValidationError,ValueError,TypeError):
            return MemoryMCPServer._context_error('invalid_arguments')
        result=self.core.read(request)
        if result.error_code:return MemoryMCPServer._context_error(result.error_code)
        return dict(id=result.id,layer=result.layer.value,content=result.content)

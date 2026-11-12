"""Optional adapter for an already-owned VectorIndex and original proof manifest."""
from .model import require
from itertools import islice


class BorrowedVectorCache:
    def __init__(self,library_id,index_kind,index,manifest):
        require(isinstance(library_id,str) and index_kind in ('legacy','context')
                and callable(getattr(index,'vector_copy',None))
                and callable(getattr(index,'is_dirty',None)),'invalid_vector_cache')
        self.library_id,self.index_kind=library_id,index_kind
        self.index,self.manifest=index,manifest

    def __call__(self,library_id,index_kind,entity_id):
        if (library_id,index_kind)!=(self.library_id,self.index_kind):return None
        if type(entity_id) is not int or entity_id<=0:return None
        try:
            manifest=self.manifest
            if (self.index.is_dirty() or not isinstance(manifest,dict) or manifest.get('version')!=1
                    or not isinstance(manifest.get('contract'),list) or len(manifest['contract'])!=4
                    or not isinstance(manifest.get('items'),dict)):return None
            entry=manifest['items'].get(str(entity_id))
            if not isinstance(entry,dict):return None
            proof=dict(contract=list(manifest['contract']),text=entry.get('text'),vector=entry.get('vector'))
            values=self.index.vector_copy(entity_id)
            if values is None:return None
            # Copy only this vector/proof. No initialize/save/close, file rewrite
            # or index replacement. The consumer verifies current text/F32 hash.
            return tuple(islice(iter(values),769)),proof
        except Exception:
            return None  # Optional cache failure leaves normal encoding required.

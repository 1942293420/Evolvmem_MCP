"""Historical lexical retrieval retains canonical temporal/family/ranking rules.

Vector support requires a history-specific scope-aware repository/projection
proof. NULL preserved IDs never enter the native ANN repository.
"""
from evolvmem.context_retriever import ContextRetriever
from .history_knowledge import PgHistoryKnowledgeDirectory

class HistoryRetriever(ContextRetriever):
    def __init__(self,*args,**kwargs):
        super().__init__(*args,**kwargs)
        self.directory=PgHistoryKnowledgeDirectory(self.store)
    def _backing_allows(self,item_id):return self.store.eligible(item_id)
    def _project_results(self,results,request):return self.directory.project_results(results,request)

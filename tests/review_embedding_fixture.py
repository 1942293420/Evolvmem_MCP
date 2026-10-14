"""Deterministic similarity provider; tests exercise real ranking and persistence."""
class ReviewEmbedding:
    is_loaded = True
    reranker_loaded = True
    def encode_document(self, text):return [1., 0.]
    def encode_query(self, text):return [1., 0.]
    def rerank(self, query, documents):return [.9] * len(documents)
    def close(self):pass

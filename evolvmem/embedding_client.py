"""Optional shared embedding provider; never imports or initializes llama."""
import math
from evolvmem.lan_http_client import JsonClient
from evolvmem.reranking import valid_scores


def valid_vector(value, dim):
    return (isinstance(value, list) and len(value) == dim
            and all(type(v) in (int, float) and math.isfinite(v) for v in value))


class HttpEmbeddingProvider:
    def __init__(self, config):
        self.config = config
        self.client = JsonClient(config.embedding_http_url.rstrip('/') + '/embedding', config.embedding_http_token_file)
        self.is_loaded = False
        self.reranker_loaded = False
        self.dim = config.embedding_dim

    def initialize(self):
        self.is_loaded = False
        status_client = JsonClient(self.config.embedding_http_url.rstrip('/') + '/embedding/status', self.config.embedding_http_token_file)
        status = status_client.post({})
        if not isinstance(status, dict) or status.get('available') is not True:
            raise RuntimeError('embedding_unavailable')
        if (status.get('dimension') != self.config.embedding_dim
                or status.get('query_prefix') != self.config.embedding_query_prefix
                or status.get('document_prefix') != self.config.embedding_doc_prefix):
            raise RuntimeError('embedding_contract_mismatch')
        self.is_loaded = True
        self.reranker_loaded = status.get('reranker_available') is True

    def close(self):
        self.is_loaded = False
        self.reranker_loaded = False

    def rerank(self, query, documents):
        if not self.reranker_loaded:
            raise RuntimeError('reranker_unavailable')
        try:
            client = JsonClient(self.config.embedding_http_url.rstrip('/') + '/rerank', self.config.embedding_http_token_file)
            result = client.post({'query': query, 'documents': documents})
            if not isinstance(result, dict) or not valid_scores(result.get('scores'), len(documents)):
                raise RuntimeError('invalid_reranking_response')
            return result['scores']
        except RuntimeError:
            self.reranker_loaded = False
            raise

    def encode(self, text, kind):
        if not self.is_loaded:
            raise RuntimeError('embedding_unavailable')
        if not isinstance(text, str) or len(text) > 32768:
            raise RuntimeError('invalid_embedding_input')
        try:
            result = self.client.post({'kind': kind, 'text': text})
            if not isinstance(result, dict) or not valid_vector(result.get('vector'), self.dim):
                raise RuntimeError('invalid_embedding_response')
        except RuntimeError:
            self.is_loaded = False
            raise
        return result['vector']

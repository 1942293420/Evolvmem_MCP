"""Rank authorized manual notes with the optional local embedding provider."""
from array import array
from collections import OrderedDict
import hashlib
import math
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

from evolvmem.embedding_client import HttpEmbeddingProvider


# Short topic names can be too vague for a document embedding. Keep the
# original text for exact matching and give the semantic leg more context.
QUERY_EXPANSIONS = {
    '布局': '页面布局，容器宽度与间距，窄屏时重排，表格溢出，长文本换行',
}
MIN_SIMILARITY = 0.415
SIMILARITY_WINDOW = 0.07
TOPIC_SIMILARITY_WINDOW = 0.10
MAX_DOCUMENTS = 512
MAX_CACHED_VECTORS = 1024
COLD_SEARCH_SECONDS = 10


def configured_search(config):
    if not isinstance(config.embedding_http_url, str):
        return None
    try:
        endpoint = urlsplit(config.embedding_http_url)
        if endpoint.scheme == 'http' and endpoint.hostname in ('127.0.0.1', '::1'):
            return ManualExperienceSearch(HttpEmbeddingProvider(config))
    except (ValueError, TypeError):
        pass
    return None


def _unit(vector):
    length = math.sqrt(sum(value * value for value in vector))
    if not math.isfinite(length) or length <= 0:
        raise ValueError('invalid_embedding_length')
    return array('f', (value / length for value in vector))


class ManualExperienceSearch:
    def __init__(self, provider):
        self.provider = provider
        self.documents = OrderedDict()
        self.lock = Lock()

    def rank(self, scope, query, rows):
        """Return exact hits first, then close semantic hits. None means fallback."""
        if len(rows) > MAX_DOCUMENTS or not self.lock.acquire(blocking=False):
            return None
        try:
            deadline = monotonic() + COLD_SEARCH_SECONDS
            self.provider.initialize()
            expanded = QUERY_EXPANSIONS.get(query)
            window = TOPIC_SIMILARITY_WINDOW if expanded else SIMILARITY_WINDOW
            query_vector = _unit(self.provider.encode(expanded or query, 'query'))
            exact = []
            semantic = []
            best_by_category = {}
            for row in rows:
                if row['keyword_match']:
                    exact.append(row)
                    continue
                document = '\n'.join(row[field][:1000] for field in
                                     ('problem', 'solution', 'reusable_experience'))
                key = (*scope, row['id'], hashlib.sha256(document.encode()).digest())
                if key not in self.documents:
                    if monotonic() >= deadline:
                        return None
                    self.documents[key] = _unit(self.provider.encode(document, 'document'))
                    if len(self.documents) > MAX_CACHED_VECTORS:
                        self.documents.popitem(last=False)
                vector = self.documents[key]
                self.documents.move_to_end(key)
                if len(vector) != len(query_vector):
                    return None
                score = sum(a * b for a, b in zip(vector, query_vector))
                if score >= MIN_SIMILARITY:
                    semantic.append((score, row))
                    category = row['category']
                    best_by_category[category] = max(score, best_by_category.get(category, 0))
            semantic.sort(key=lambda match: match[0], reverse=True)
            return exact + [row for score, row in semantic
                            if score >= best_by_category[row['category']] - window]
        except (OSError, RuntimeError, ValueError, OverflowError):
            return None
        finally:
            self.lock.release()

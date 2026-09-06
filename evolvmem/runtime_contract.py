"""Canonical local embedding runtime defaults."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EmbeddingRuntimeContract:
    """The model, vector space, and task prefixes used together at runtime."""

    filename: str
    download_url: str
    dimension: int
    query_prefix: str
    document_prefix: str


DEFAULT_EMBEDDING_CONTRACT = EmbeddingRuntimeContract(
    filename="nomic-embed-text-v1.5.f16.gguf",
    download_url=(
        "https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF/resolve/"
        "main/nomic-embed-text-v1.5.f16.gguf"
    ),
    dimension=768,
    query_prefix="search_query: ",
    document_prefix="search_document: ",
)


def known_embedding_contract(filename: str) -> EmbeddingRuntimeContract | None:
    """Return a contract only for model filenames with known vector semantics."""
    if filename == DEFAULT_EMBEDDING_CONTRACT.filename:
        return DEFAULT_EMBEDDING_CONTRACT
    return None

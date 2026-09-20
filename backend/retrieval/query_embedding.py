"""Query-side embedding for retrieval.

Reuses the same Azure OpenAI embedding provider ingestion already uses, so a
query vector lives in the identical embedding space as every indexed chunk
vector -- vector search only works if both sides come from the same model.

Deliberately embeds the raw query string with no synthetic prefix. Chunks are
embedded via `embedding_text` (a "Document: ...\\nContext: ...\\nContent: "
prefix built at chunk-build time, see chunking/builder.py:_context_prefix),
but asymmetric query/document embedding is standard RAG practice, not
something to "fix" by mirroring that prefix here.
"""

from __future__ import annotations

from functools import lru_cache

from backend.ingestion.embedding.azure_openai import AzureOpenAIEmbeddingProvider


@lru_cache(maxsize=1)
def get_embedding_provider() -> AzureOpenAIEmbeddingProvider:
    """Builds one Azure embedding provider per process, reused across every query."""
    return AzureOpenAIEmbeddingProvider.from_runtime_environment()


def embed_query(text: str, provider: AzureOpenAIEmbeddingProvider | None = None) -> list[float]:
    """Embeds one query string into the same vector space chunks were embedded in."""
    provider = provider or get_embedding_provider()
    embeddings = provider.embed([text])
    if not embeddings:
        raise RuntimeError("Azure returned no embedding for the query text.")
    return embeddings[0]

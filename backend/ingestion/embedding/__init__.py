"""Azure-ready embedding boundary for approved JLR ingestion chunks.

Files in this folder:
- ``contracts.py`` defines provider-neutral settings and vector result records.
- ``azure_openai.py`` adapts the Azure OpenAI SDK without exposing credentials.
- ``service.py`` batches chunks, retries transient failures, and validates vectors.

Connection flow: ``chunking/`` creates immutable, quality-gated chunk artifacts
-> this package embeds only their ``embedding_text`` -> a future retrieval/index
package persists vectors in the selected vector database.  Runtime credentials
are supplied from environment variables; no credential belongs in canonical or
chunk JSON.
"""

from backend.ingestion.embedding.azure_openai import AzureOpenAIEmbeddingProvider
from backend.ingestion.embedding.contracts import EmbeddedChunk, EmbeddingBuildResult, EmbeddingModelSettings
from backend.ingestion.embedding.service import EmbeddingService

__all__ = [
    "AzureOpenAIEmbeddingProvider",
    "EmbeddedChunk",
    "EmbeddingBuildResult",
    "EmbeddingModelSettings",
    "EmbeddingService",
]

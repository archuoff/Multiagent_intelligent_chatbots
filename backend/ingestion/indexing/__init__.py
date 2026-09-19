"""Persistent vector-index adapters for the JLR ingestion pipeline.

Files in this folder:
- ``contracts.py`` defines index settings and safe write results.
- ``qdrant_store.py`` maps validated embedding results into Qdrant collections.
- ``service.py`` enforces checksum verification before embedding and indexing.

Connection flow: ``chunking/`` produces immutable source artifacts ->
``embedding/`` returns validated vectors -> this package upserts them into the
local Qdrant index. The query pipeline will later supply authorization-aware
metadata filters before searching this index.
"""

from backend.ingestion.indexing.qdrant_store import QdrantVectorStore
from backend.ingestion.indexing.contracts import QdrantIndexSettings, VectorIndexWriteResult
from backend.ingestion.indexing.service import EmbeddingIndexingService

__all__ = ["EmbeddingIndexingService", "QdrantIndexSettings", "QdrantVectorStore", "VectorIndexWriteResult"]

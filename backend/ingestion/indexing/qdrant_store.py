"""Qdrant persistence adapter for validated JLR embedding results.

Qdrant is a retrieval index, not the canonical source of documents. This
adapter runs in persistent local mode during development and creates one
collection per JLR agent. It preserves source lineage and security payloads so
the future query layer can enforce authorization filters before retrieval.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from backend.ingestion.chunking.contracts import ChunkBuildResult, EmbeddingChunk
from backend.ingestion.embedding.contracts import EmbeddedChunk, EmbeddingBuildResult
from backend.ingestion.indexing.contracts import QdrantIndexSettings, VectorIndexWriteResult


class QdrantVectorStore:
    """Upserts validated vectors into one local persistent Qdrant collection per agent."""

    _SAFE_COMPONENT = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
    _INDEXED_PAYLOAD_FIELDS = ("agent_id", "document_id", "source_version", "security_classification", "allowed_groups", "allowed_users")

    def __init__(self, storage_root: str | Path | None = None, *, settings: QdrantIndexSettings | None = None,
                 client: Any | None = None, models_module: Any | None = None, sparse_provider: Any | None = None) -> None:
        """Uses injected client/models in tests and Qdrant Local Mode only during real index work."""
        self._settings = settings or QdrantIndexSettings()
        self._storage_root = Path(storage_root) if storage_root else Path(__file__).resolve().parents[3] / "storage"
        self._client = client
        self._models = models_module
        self._sparse_provider = sparse_provider

    def upsert(self, chunks: ChunkBuildResult, embeddings: EmbeddingBuildResult) -> VectorIndexWriteResult:
        """Creates the agent collection and upserts matching vectors with filterable payload metadata."""
        chunk_by_id = {chunk.chunk_id: chunk for chunk in chunks.chunks}
        vector_by_id = {item.chunk_id: item for item in embeddings.embedded_chunks}
        if len(chunk_by_id) != len(chunks.chunks):
            raise ValueError("Chunk artifact contains duplicate chunk IDs.")
        if unknown_ids := set(vector_by_id) - set(chunk_by_id):
            raise ValueError("Embedding artifact contains chunk IDs absent from its chunk artifact.")
        if not vector_by_id:
            raise ValueError("Embedding artifact has no vectors to index.")
        selected_chunks = [chunk_by_id[item.chunk_id] for item in embeddings.embedded_chunks]
        self._validate_lineage(selected_chunks, embeddings.embedded_chunks, embeddings)
        first = selected_chunks[0]
        if any(chunk.agent_id != first.agent_id for chunk in selected_chunks):
            raise ValueError("One Qdrant upsert may contain chunks for only one agent.")
        collection_name = self._collection_name(first.agent_id)
        client, models = self._get_client_and_models()
        self._ensure_collection(client, models, collection_name, embeddings.dimensions)
        for start in range(0, len(selected_chunks), self._settings.batch_size):
            chunk_batch = selected_chunks[start:start + self._settings.batch_size]
            vector_batch = embeddings.embedded_chunks[start:start + self._settings.batch_size]
            sparse_batch = self._get_sparse_provider().embed([chunk.embedding_text for chunk in chunk_batch])
            points = [models.PointStruct(id=str(uuid5(NAMESPACE_URL, chunk.chunk_id)),
                vector={"dense": vector.embedding, "sparse": models.SparseVector(**sparse_vector)},
                payload=self._payload(chunk, vector))
                for chunk, vector, sparse_vector in zip(chunk_batch, vector_batch, sparse_batch, strict=True)]
            client.upsert(collection_name=collection_name, points=points, wait=True)
        return VectorIndexWriteResult(collection_name=collection_name,
            indexed_chunk_ids=[chunk.chunk_id for chunk in selected_chunks], document_id=first.document_id,
            source_version=first.source_version)

    def search(self, *, agent_id: str, query_vector: list[float], query_sparse_vector: dict[str, list],
               limit: int = 10) -> list[dict[str, Any]]:
        """Returns the closest chunks in one agent's collection via hybrid (dense + BM25) search,
        fused with Qdrant's native RRF -- newest payload fields included.

        Filters only by agent_id -- ACL and other governance checks happen
        client-side in backend/retrieval/, since expressing the
        empty-list-means-public rule as a Qdrant filter is fragile to hand-write
        and awkward to unit-test against this offline FakeClient/FakeModels
        pattern. Returns [] for a collection that doesn't exist yet (an agent
        with no ingested documents) instead of raising.
        """
        client, models = self._get_client_and_models()
        collection_name = self._collection_name(agent_id)
        if not client.collection_exists(collection_name):
            return []
        query_filter = models.Filter(must=[models.FieldCondition(key="agent_id", match=models.MatchValue(value=agent_id))])
        response = client.query_points(collection_name=collection_name,
            prefetch=[
                models.Prefetch(query=query_vector, using="dense", filter=query_filter, limit=limit),
                models.Prefetch(query=models.SparseVector(**query_sparse_vector), using="sparse", filter=query_filter, limit=limit),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF), limit=limit, with_payload=True)
        return [{"id": point.id, "score": point.score, "payload": point.payload} for point in response.points]

    def _get_sparse_provider(self) -> Any:
        """Creates the fastembed BM25 provider lazily so tests need no fastembed installation."""
        if self._sparse_provider is None:
            from backend.ingestion.embedding.sparse_provider import FastEmbedSparseProvider
            self._sparse_provider = FastEmbedSparseProvider.from_default_model()
        return self._sparse_provider

    def delete_document_version(self, *, agent_id: str, document_id: str, source_version: str) -> None:
        """Removes only one obsolete document version while retaining other agent data and versions."""
        client, models = self._get_client_and_models()
        collection_name = self._collection_name(agent_id)
        query_filter = models.Filter(must=[
            models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id)),
            models.FieldCondition(key="source_version", match=models.MatchValue(value=source_version)),
        ])
        client.delete(collection_name=collection_name, points_selector=models.FilterSelector(filter=query_filter), wait=True)

    def close(self) -> None:
        """Closes the underlying Qdrant client before Python interpreter shutdown."""
        client = self._client
        if client is None:
            return
        close = getattr(client, "close", None)
        if callable(close):
            close()
        self._client = None

    def _get_client_and_models(self) -> tuple[Any, Any]:
        """Creates the official Qdrant local client lazily so tests need no Qdrant installation."""
        if self._client is None or self._models is None:
            try:
                from qdrant_client import QdrantClient, models
            except ImportError as error:
                raise RuntimeError("Qdrant indexing requires the 'qdrant-client' package. Install requirements.txt first.") from error
            if self._client is None:
                path = self._storage_root / "qdrant"
                path.mkdir(parents=True, exist_ok=True)
                self._client = QdrantClient(path=str(path))
            self._models = models
        return self._client, self._models

    def _ensure_collection(self, client: Any, models: Any, collection_name: str, dimensions: int) -> None:
        """Creates named dense+sparse vectors and payload indexes once, before data reaches a new agent collection."""
        if client.collection_exists(collection_name):
            return
        client.create_collection(collection_name=collection_name,
            vectors_config={"dense": models.VectorParams(size=dimensions, distance=models.Distance.COSINE)},
            sparse_vectors_config={"sparse": models.SparseVectorParams()})
        for field_name in self._INDEXED_PAYLOAD_FIELDS:
            client.create_payload_index(collection_name=collection_name, field_name=field_name,
                field_schema=models.PayloadSchemaType.KEYWORD, wait=True)

    def _collection_name(self, agent_id: str) -> str:
        """Creates a stable safe collection name for one of the six JLR agents."""
        normalized_agent = agent_id.lower().replace("_", "-")
        if not self._SAFE_COMPONENT.fullmatch(normalized_agent):
            raise ValueError("agent_id contains unsupported collection-name characters.")
        return f"{self._settings.collection_prefix}-{normalized_agent}-chunks"

    def _validate_lineage(self, chunks: list[EmbeddingChunk], vectors: list[EmbeddedChunk], result: EmbeddingBuildResult) -> None:
        """Prevents mixing vectors from a different model, document, or source version."""
        if any(vector.dimensions != result.dimensions or len(vector.embedding) != result.dimensions for vector in vectors):
            raise ValueError("Embedding vectors do not match their artifact's declared dimensions.")
        document_id, source_version = chunks[0].document_id, chunks[0].source_version
        if any(chunk.document_id != document_id or chunk.source_version != source_version for chunk in chunks):
            raise ValueError("One Qdrant upsert may contain chunks for only one document version.")

    def _payload(self, chunk: EmbeddingChunk, vector: EmbeddedChunk) -> dict[str, Any]:
        """Builds the searchable payload, including the original chunk ID and canonical ACL fields."""
        scope = chunk.metadata.get("security_scope")
        security = scope if isinstance(scope, dict) else {}
        groups = security.get("allowed_groups", [])
        users = security.get("allowed_users", [])
        return {
            "chunk_id": chunk.chunk_id,
            "content_text": chunk.content_text,
            "agent_id": chunk.agent_id,
            "document_id": chunk.document_id,
            "source_type": chunk.source_type,
            "source_version": chunk.source_version,
            "parent_node_id": chunk.parent_node_id,
            "chunk_type": chunk.chunk_type.value,
            "embedding_model": vector.embedding_model,
            "embedding_dimensions": vector.dimensions,
            "security_classification": str(security.get("classification", "internal")),
            "allowed_groups": [str(group) for group in groups] if isinstance(groups, list) else [],
            "allowed_users": [str(user) for user in users] if isinstance(users, list) else [],
            # Citation coordinates and answer-governance metadata -- payload-only, not
            # search-filterable, needed by the query layer for citations and per-field
            # answer_visible redaction (see backend/retrieval/).
            "breadcrumbs": [str(item) for item in chunk.metadata.get("breadcrumbs") or []],
            "page_number": chunk.metadata.get("page_number"),
            "slide_number": chunk.metadata.get("slide_number"),
            "sheet_name": chunk.metadata.get("sheet_name"),
            "cell_range": chunk.metadata.get("cell_range"),
            "table_title": chunk.metadata.get("table_title"),
            "header_context": [str(item) for item in chunk.metadata.get("header_context") or []],
            "field_policies": chunk.metadata.get("field_policies") or [],
            # Image linking -- lets a retrieved VISUAL chunk point back at its
            # actual file, not just its OCR/VLM-derived text.
            "visual_node_type": chunk.metadata.get("visual_node_type"),
            "ocr_status": chunk.metadata.get("ocr_status"),
            "image_description_status": chunk.metadata.get("image_description_status"),
            "vlm_confidence": chunk.metadata.get("vlm_confidence"),
            "image_path": chunk.metadata.get("image_path"),
            "image_storage_status": chunk.metadata.get("image_storage_status"),
        }

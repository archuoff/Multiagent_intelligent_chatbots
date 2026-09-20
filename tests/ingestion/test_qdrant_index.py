"""Offline tests for Qdrant lineage, payload indexing, and version deletion behavior."""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from backend.ingestion.chunking import ChunkArtifactStore, ChunkBuildResult, ChunkType, EmbeddingChunk
from backend.ingestion.embedding.contracts import EmbeddedChunk, EmbeddingBuildResult
from backend.ingestion.indexing.qdrant_store import QdrantVectorStore
from backend.ingestion.indexing.service import EmbeddingIndexingService


class FakeModels:
    """Provides the small Qdrant model surface required by offline store tests."""

    class Distance:
        COSINE = "cosine"

    class PayloadSchemaType:
        KEYWORD = "keyword"

    @staticmethod
    def VectorParams(**payload):
        return payload

    @staticmethod
    def PointStruct(**payload):
        return payload

    @staticmethod
    def MatchValue(**payload):
        return payload

    @staticmethod
    def FieldCondition(**payload):
        return payload

    @staticmethod
    def Filter(**payload):
        return payload

    @staticmethod
    def FilterSelector(**payload):
        return payload


class FakeClient:
    """Captures Qdrant calls without importing the real client or writing local vectors."""

    def __init__(self) -> None:
        self.collections: set[str] = set()
        self.created: list[dict] = []
        self.payload_indexes: list[dict] = []
        self.upserts: list[dict] = []
        self.deletes: list[dict] = []
        self.closed = False

    def collection_exists(self, collection_name: str) -> bool:
        """Reports whether a test collection has already been created."""
        return collection_name in self.collections

    def create_collection(self, **payload) -> None:
        """Records collection configuration."""
        self.collections.add(payload["collection_name"])
        self.created.append(payload)

    def create_payload_index(self, **payload) -> None:
        """Records one filterable payload field."""
        self.payload_indexes.append(payload)

    def upsert(self, **payload) -> None:
        """Records a Qdrant point upsert."""
        self.upserts.append(payload)

    def delete(self, **payload) -> None:
        """Records a Qdrant filtered delete."""
        self.deletes.append(payload)

    def close(self) -> None:
        """Records explicit cleanup."""
        self.closed = True


def chunk(chunk_id: str = "chunk-1") -> EmbeddingChunk:
    """Creates one approved chunk with internal security lineage."""
    return EmbeddingChunk(chunk_id=chunk_id, chunk_type=ChunkType.NARRATIVE, document_id="document-1",
        agent_id="adas-agent", source_type="pdf", source_version="v1", parent_node_id="section-1",
        source_node_ids=["node-1"], content_text="Sensor requirement", embedding_text="Sensor requirement",
        metadata={"security_scope": {"classification": "internal", "allowed_groups": ["adas_engineer"]}})


def vector(chunk_id: str = "chunk-1") -> EmbeddedChunk:
    """Creates a matching three-dimension vector without a live embedding provider."""
    return EmbeddedChunk(chunk_id=chunk_id, embedding=[0.1, 0.2, 0.3], embedding_model="azure-embedding",
        dimensions=3, metadata={})


class QdrantVectorStoreTests(unittest.TestCase):
    """Ensures Qdrant receives only validated points and filterable ACL payloads."""

    def store(self) -> tuple[QdrantVectorStore, FakeClient]:
        """Creates an injected store that stays fully offline."""
        client = FakeClient()
        return QdrantVectorStore(client=client, models_module=FakeModels), client

    def test_upsert_creates_collection_indexes_and_filterable_payload(self):
        """One agent receives a cosine collection and retrieval/security payload fields."""
        store, client = self.store()
        result = store.upsert(ChunkBuildResult(policy_version="v1", chunks=[chunk()]),
            EmbeddingBuildResult(embedding_model="azure-embedding", dimensions=3, embedded_chunks=[vector()]))
        payload = client.upserts[0]["points"][0]["payload"]
        self.assertEqual(result.collection_name, "jlr-adas-agent-chunks")
        self.assertEqual(client.created[0]["vectors_config"]["size"], 3)
        self.assertEqual({item["field_name"] for item in client.payload_indexes},
            {"agent_id", "document_id", "source_version", "security_classification", "allowed_groups", "allowed_users"})
        self.assertEqual(payload["allowed_groups"], ["adas_engineer"])
        self.assertEqual(payload["chunk_id"], "chunk-1")

    def test_rejects_vector_from_another_chunk_artifact(self):
        """A mismatched vector cannot be silently attached to another source chunk."""
        store, _ = self.store()
        with self.assertRaisesRegex(ValueError, "absent"):
            store.upsert(ChunkBuildResult(policy_version="v1", chunks=[chunk()]),
                EmbeddingBuildResult(embedding_model="azure-embedding", dimensions=3, embedded_chunks=[vector("other")]))

    def test_delete_uses_exact_document_version_filter(self):
        """Reingestion can remove only one obsolete document version."""
        store, client = self.store()
        store.delete_document_version(agent_id="adas-agent", document_id="document-1", source_version="v1")
        conditions = client.deletes[0]["points_selector"]["filter"]["must"]
        self.assertEqual([item["key"] for item in conditions], ["document_id", "source_version"])

    def test_close_closes_injected_client(self):
        """Explicit cleanup avoids Qdrant destructor warnings during interpreter shutdown."""
        store, client = self.store()
        store.close()
        self.assertTrue(client.closed)

    def test_orchestrator_verifies_artifact_before_embedding_and_indexing(self):
        """The application path cannot index chunks without calling checksum-verified load first."""
        artifact_store = Mock(spec=ChunkArtifactStore)
        artifact = Mock()
        chunks = ChunkBuildResult(policy_version="v1", chunks=[chunk()])
        artifact_store.load.return_value = chunks
        embedding_service = Mock()
        embeddings = EmbeddingBuildResult(embedding_model="azure-embedding", dimensions=3, embedded_chunks=[vector()])
        embedding_service.embed.return_value = embeddings
        vector_store = Mock()
        EmbeddingIndexingService(artifact_store, embedding_service, vector_store).embed_and_index(artifact)
        artifact_store.load.assert_called_once_with(artifact)
        embedding_service.embed.assert_called_once_with(chunks)
        vector_store.upsert.assert_called_once_with(chunks, embeddings)
        vector_store.close.assert_called_once_with()

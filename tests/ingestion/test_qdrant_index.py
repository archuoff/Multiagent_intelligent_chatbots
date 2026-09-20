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


class FakeScoredPoint:
    """Mirrors the small slice of qdrant_client's ScoredPoint search() consumes."""

    def __init__(self, point_id: str, score: float, payload: dict) -> None:
        self.id = point_id
        self.score = score
        self.payload = payload


class FakeQueryResponse:
    """Mirrors qdrant_client's QueryResponse (query_points()'s return type)."""

    def __init__(self, points: list[FakeScoredPoint]) -> None:
        self.points = points


class FakeClient:
    """Captures Qdrant calls without importing the real client or writing local vectors."""

    def __init__(self) -> None:
        self.collections: set[str] = set()
        self.created: list[dict] = []
        self.payload_indexes: list[dict] = []
        self.upserts: list[dict] = []
        self.deletes: list[dict] = []
        self.query_points_calls: list[dict] = []
        self.query_response = FakeQueryResponse([])
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

    def query_points(self, **payload):
        """Records a search call and returns the test's preconfigured response."""
        self.query_points_calls.append(payload)
        return self.query_response

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

    def test_upsert_payload_includes_citation_and_field_policy_metadata(self):
        """Page/sheet/breadcrumb coordinates and field policies now reach Qdrant's payload."""
        store, client = self.store()
        row_chunk = EmbeddingChunk(chunk_id="row-1", chunk_type=ChunkType.TABLE_ROW, document_id="document-1",
            agent_id="adas-agent", source_type="xlsx", source_version="v1", parent_node_id="table-1",
            source_node_ids=["row-1"], content_text="Density: 1.08", embedding_text="Density: 1.08",
            metadata={"security_scope": {"classification": "internal", "allowed_groups": []},
                "breadcrumbs": ["Sheet1", "Material Table"], "page_number": None, "slide_number": None,
                "sheet_name": "Sheet1", "cell_range": "A2:B2", "table_title": "Material Properties",
                "header_context": ["Property | Value"],
                "field_policies": [{"field_name": "Density", "use": "standard", "retrieval_allowed": True,
                    "answer_visible": True, "reason": "No restrictive policy matched."}]})
        store.upsert(ChunkBuildResult(policy_version="v1", chunks=[row_chunk]),
            EmbeddingBuildResult(embedding_model="azure-embedding", dimensions=3, embedded_chunks=[vector("row-1")]))
        payload = client.upserts[0]["points"][0]["payload"]
        self.assertEqual(payload["breadcrumbs"], ["Sheet1", "Material Table"])
        self.assertEqual(payload["sheet_name"], "Sheet1")
        self.assertEqual(payload["cell_range"], "A2:B2")
        self.assertEqual(payload["table_title"], "Material Properties")
        self.assertEqual(payload["header_context"], ["Property | Value"])
        self.assertEqual(payload["field_policies"][0]["field_name"], "Density")
        self.assertIsNone(payload["page_number"])

    def test_upsert_payload_defaults_new_fields_safely_when_metadata_lacks_them(self):
        """A narrative chunk with no table/page metadata still gets a well-formed payload."""
        store, client = self.store()
        store.upsert(ChunkBuildResult(policy_version="v1", chunks=[chunk()]),
            EmbeddingBuildResult(embedding_model="azure-embedding", dimensions=3, embedded_chunks=[vector()]))
        payload = client.upserts[0]["points"][0]["payload"]
        self.assertEqual(payload["breadcrumbs"], [])
        self.assertEqual(payload["header_context"], [])
        self.assertEqual(payload["field_policies"], [])
        self.assertIsNone(payload["page_number"])
        self.assertIsNone(payload["table_title"])

    def test_search_filters_by_agent_id_and_returns_scored_payloads(self):
        """A search hit's score and full payload (including new citation fields) reach the caller."""
        store, client = self.store()
        client.collections.add("jlr-adas-agent-chunks")
        client.query_response = FakeQueryResponse([
            FakeScoredPoint("point-1", 0.93, {"chunk_id": "chunk-1", "page_number": 4, "content_text": "Sensor spec"}),
            FakeScoredPoint("point-2", 0.81, {"chunk_id": "chunk-2", "page_number": 5, "content_text": "Mounting torque"}),
        ])
        results = store.search(agent_id="adas-agent", query_vector=[0.1, 0.2, 0.3], limit=5)
        call = client.query_points_calls[0]
        self.assertEqual(call["collection_name"], "jlr-adas-agent-chunks")
        self.assertEqual(call["limit"], 5)
        self.assertEqual(call["query_filter"]["must"][0]["key"], "agent_id")
        self.assertEqual(call["query_filter"]["must"][0]["match"]["value"], "adas-agent")
        self.assertEqual([item["score"] for item in results], [0.93, 0.81])
        self.assertEqual(results[0]["payload"]["page_number"], 4)

    def test_search_returns_empty_list_for_agent_with_no_ingested_data(self):
        """An agent whose collection was never created returns no results, not an error."""
        store, client = self.store()
        results = store.search(agent_id="benchmarking-agent", query_vector=[0.1, 0.2, 0.3])
        self.assertEqual(results, [])
        self.assertEqual(client.query_points_calls, [])

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

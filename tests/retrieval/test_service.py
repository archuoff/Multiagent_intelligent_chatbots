"""Unit tests for RetrievalService: embed -> search -> ACL check -> citation lookup.

Uses hand-rolled fake collaborators (constructor DI), matching the repo's
established offline test pattern -- no real Azure, Qdrant, or filesystem
access.
"""

from __future__ import annotations

import unittest

from backend.retrieval.service import RetrievalService


class FakeEmbeddingProvider:
    """Returns one fixed vector per call; the exact value never matters to these tests."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]


class FakeSparseProvider:
    """Returns one fixed sparse vector per call -- required so tests never fall back to
    the real, uncredentialed default BM25 model."""

    def embed(self, texts: list[str]) -> list[dict]:
        return [{"indices": [1], "values": [0.5]} for _ in texts]


class FakeVectorStore:
    """Records the search call and returns a preconfigured list of raw hits."""

    def __init__(self, results: list[dict]) -> None:
        self.results = results
        self.search_calls: list[dict] = []

    def search(self, **kwargs) -> list[dict]:
        self.search_calls.append(kwargs)
        return self.results


class FakeManifest:
    def __init__(self, source_name: str) -> None:
        self.source_name = source_name


class FakeArtifact:
    def __init__(self, source_name: str) -> None:
        self.manifest = FakeManifest(source_name)


class FakeCanonicalStore:
    """Resolves a document title, or raises like the real store does when data is missing."""

    def __init__(self, titles: dict[tuple[str, str, str], str] | None = None, *, raise_missing: bool = False) -> None:
        self.titles = titles or {}
        self.raise_missing = raise_missing
        self.load_calls: list[dict] = []

    def load(self, *, agent_id: str, document_id: str, version: str):
        self.load_calls.append({"agent_id": agent_id, "document_id": document_id, "version": version})
        key = (agent_id, document_id, version)
        if key not in self.titles:
            if self.raise_missing:
                raise FileNotFoundError("Registered canonical artifact or manifest is missing.")
            raise FileNotFoundError("no such artifact in this fake")
        return FakeArtifact(self.titles[key])


def hit(chunk_id: str, score: float, *, document_id: str = "doc-1", source_version: str = "v1",
        allowed_groups: list[str] | None = None, allowed_users: list[str] | None = None,
        page_number: int | None = 4) -> dict:
    """Builds one raw Qdrant search result shaped like store.search()'s real output."""
    return {"id": f"point-{chunk_id}", "score": score, "payload": {
        "chunk_id": chunk_id, "chunk_type": "narrative", "content_text": "Torque spec is 8 Nm",
        "document_id": document_id, "source_version": source_version, "source_type": "pdf",
        "security_classification": "internal", "page_number": page_number, "slide_number": None,
        "sheet_name": None, "cell_range": None, "breadcrumbs": ["Section 2"], "table_title": None,
        "header_context": [], "field_policies": [],
        "allowed_groups": allowed_groups or [], "allowed_users": allowed_users or [],
    }}


def visual_hit(chunk_id: str = "img1", *, image_path: str | None = "storage/visual-assets/adas/doc-1/v1/img.png",
                image_storage_status: str | None = "stored") -> dict:
    """Builds a raw VISUAL-chunk search result, image fields populated."""
    return {"id": f"point-{chunk_id}", "score": 0.85, "payload": {
        "chunk_id": chunk_id, "chunk_type": "visual", "content_text": "OCR visible text: Torque 8Nm",
        "document_id": "doc-1", "source_version": "v1", "source_type": "pdf", "security_classification": "internal",
        "page_number": 2, "slide_number": None, "sheet_name": None, "cell_range": None, "breadcrumbs": [],
        "table_title": None, "header_context": [], "field_policies": [], "allowed_groups": [], "allowed_users": [],
        "visual_node_type": "image", "ocr_status": "completed", "image_description_status": "completed",
        "vlm_confidence": 0.8, "image_path": image_path, "image_storage_status": image_storage_status,
    }}


def service(results: list[dict], titles: dict | None = None) -> tuple[RetrievalService, FakeVectorStore, FakeCanonicalStore]:
    vector_store = FakeVectorStore(results)
    canonical_store = FakeCanonicalStore(titles)
    return (RetrievalService(vector_store, FakeEmbeddingProvider(), canonical_store, FakeSparseProvider()),
        vector_store, canonical_store)


class AclEnforcementTests(unittest.TestCase):
    """The four cases the plan's empty-list-means-public decision must cover."""

    def test_chunk_with_no_acl_entries_is_public(self):
        """Case (a): both allowed_groups and allowed_users empty -> included for anyone."""
        svc, _, _ = service([hit("c1", 0.9)])
        result = svc.retrieve(agent_id="adas-agent", query_text="torque spec",
            principal_group_codes=frozenset({"random_group"}), principal_user_id="anyone", limit=5)
        self.assertEqual([c.chunk_id for c in result.chunks], ["c1"])

    def test_matching_allowed_group_includes_the_chunk(self):
        """Case (b): principal's group intersects allowed_groups -> included."""
        svc, _, _ = service([hit("c1", 0.9, allowed_groups=["adas_engineer"])])
        result = svc.retrieve(agent_id="adas-agent", query_text="q",
            principal_group_codes=frozenset({"adas_engineer"}), principal_user_id="u1", limit=5)
        self.assertEqual(len(result.chunks), 1)

    def test_matching_allowed_user_includes_the_chunk(self):
        """Case (c): principal's user id is explicitly listed -> included even with no group match."""
        svc, _, _ = service([hit("c1", 0.9, allowed_users=["bob"])])
        result = svc.retrieve(agent_id="adas-agent", query_text="q",
            principal_group_codes=frozenset({"unrelated_group"}), principal_user_id="bob", limit=5)
        self.assertEqual(len(result.chunks), 1)

    def test_no_group_or_user_match_excludes_the_chunk(self):
        """Case (d): denied -- neither list matches this principal."""
        svc, _, _ = service([hit("c1", 0.9, allowed_groups=["le_hr_only"], allowed_users=["alice"])])
        result = svc.retrieve(agent_id="le-agent", query_text="q",
            principal_group_codes=frozenset({"le_engineer"}), principal_user_id="bob", limit=5)
        self.assertEqual(result.chunks, [])

    def test_partial_authorization_keeps_only_permitted_hits(self):
        """A mix of public and restricted hits in one search only returns what's actually allowed."""
        svc, _, _ = service([
            hit("public", 0.95),
            hit("restricted", 0.90, allowed_groups=["le_hr_only"]),
        ])
        result = svc.retrieve(agent_id="le-agent", query_text="q",
            principal_group_codes=frozenset({"le_engineer"}), principal_user_id="carol", limit=5)
        self.assertEqual([c.chunk_id for c in result.chunks], ["public"])


class DocumentTitleResolutionTests(unittest.TestCase):
    def test_resolves_document_title_from_canonical_store(self):
        svc, _, canonical_store = service([hit("c1", 0.9)], titles={("adas-agent", "doc-1", "v1"): "Sensor Guide.pdf"})
        result = svc.retrieve(agent_id="adas-agent", query_text="q",
            principal_group_codes=frozenset(), principal_user_id="u1", limit=5)
        self.assertEqual(result.chunks[0].document_title, "Sensor Guide.pdf")
        self.assertEqual(canonical_store.load_calls[0]["document_id"], "doc-1")

    def test_missing_manifest_degrades_to_none_instead_of_raising(self):
        """A citation nicety lost is not a reason to fail an otherwise-valid search result."""
        svc, _, _ = service([hit("c1", 0.9)])  # no titles configured -> FileNotFoundError inside load()
        result = svc.retrieve(agent_id="adas-agent", query_text="q",
            principal_group_codes=frozenset(), principal_user_id="u1", limit=5)
        self.assertEqual(len(result.chunks), 1)
        self.assertIsNone(result.chunks[0].document_title)

    def test_title_lookup_is_cached_per_document_version(self):
        """Two chunks from the same document only trigger one canonical_store.load() call."""
        svc, _, canonical_store = service(
            [hit("c1", 0.9), hit("c2", 0.8)],
            titles={("adas-agent", "doc-1", "v1"): "Sensor Guide.pdf"})
        svc.retrieve(agent_id="adas-agent", query_text="q", principal_group_codes=frozenset(),
            principal_user_id="u1", limit=5)
        self.assertEqual(len(canonical_store.load_calls), 1)


class SearchCallTests(unittest.TestCase):
    def test_search_is_called_with_the_requested_agent_and_limit(self):
        svc, vector_store, _ = service([])
        svc.retrieve(agent_id="benchmarking-agent", query_text="compare suppliers",
            principal_group_codes=frozenset(), principal_user_id="u1", limit=3)
        call = vector_store.search_calls[0]
        self.assertEqual(call["agent_id"], "benchmarking-agent")
        self.assertEqual(call["limit"], 3)

    def test_search_receives_both_dense_and_sparse_query_vectors(self):
        svc, vector_store, _ = service([])
        svc.retrieve(agent_id="adas-agent", query_text="torque spec",
            principal_group_codes=frozenset(), principal_user_id="u1", limit=5)
        call = vector_store.search_calls[0]
        self.assertEqual(call["query_vector"], [0.1, 0.2, 0.3])
        self.assertEqual(call["query_sparse_vector"], {"indices": [1], "values": [0.5]})

    def test_no_search_hits_returns_an_empty_result_not_an_error(self):
        svc, _, _ = service([])
        result = svc.retrieve(agent_id="adas-agent", query_text="q", principal_group_codes=frozenset(),
            principal_user_id="u1", limit=5)
        self.assertEqual(result.chunks, [])
        self.assertEqual(result.query, "q")


class ImageLinkingTests(unittest.TestCase):
    """A VISUAL chunk's real file reference reaches the caller; a text chunk stays None."""

    def test_visual_chunk_image_path_reaches_retrieved_chunk(self):
        svc, _, _ = service([visual_hit()])
        result = svc.retrieve(agent_id="adas-agent", query_text="show me the sensor diagram",
            principal_group_codes=frozenset(), principal_user_id="u1", limit=5)
        image_chunk = result.chunks[0]
        self.assertEqual(image_chunk.image_path, "storage/visual-assets/adas/doc-1/v1/img.png")
        self.assertEqual(image_chunk.image_storage_status, "stored")
        self.assertEqual(image_chunk.vlm_confidence, 0.8)

    def test_narrative_chunk_with_no_image_fields_defaults_to_none(self):
        svc, _, _ = service([hit("c1", 0.9)])
        result = svc.retrieve(agent_id="adas-agent", query_text="q", principal_group_codes=frozenset(),
            principal_user_id="u1", limit=5)
        self.assertIsNone(result.chunks[0].image_path)
        self.assertIsNone(result.chunks[0].image_storage_status)


if __name__ == "__main__":
    unittest.main()

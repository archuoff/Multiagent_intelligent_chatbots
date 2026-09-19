"""Regression tests for immutable chunk artifact storage and lineage manifests."""

import tempfile
import unittest
from pathlib import Path

from backend.ingestion.chunking import ChunkArtifactStore, ChunkBuildResult, ChunkRejection, ChunkType, EmbeddingChunk


def result(*, text="Sensor requirement", policy="canonical-chunks-v1", rejected=None):
    """Creates a small valid embedding artifact without invoking an embedding model."""
    chunk = EmbeddingChunk(chunk_id="chk_test", chunk_type=ChunkType.NARRATIVE, document_id="doc-1",
        agent_id="adas-agent", source_type="pdf", source_version="v1", parent_node_id="page-1",
        source_node_ids=["node-1"], content_text=text, embedding_text=f"Context: {text}",
        metadata={"security_scope": {"classification": "internal"}})
    return ChunkBuildResult(policy_version=policy, chunks=[chunk], rejected=rejected or [])


class ChunkArtifactStoreTests(unittest.TestCase):
    """Checks immutable persistence before a future vector index consumes chunks."""

    def persist(self, root: Path, build_result: ChunkBuildResult):
        """Persists a fixed document lineage into a test-specific storage root."""
        return ChunkArtifactStore(root).persist(document_id="doc-1", agent_id="adas-agent", source_version="v1",
            source_hash="sha256:source", result=build_result)

    def test_persists_loads_and_reuses_identical_chunk_artifact(self):
        """Unchanged chunks reuse one immutable artifact and checksum-verified manifest."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.persist(root, result())
            second = self.persist(root, result())
            loaded = ChunkArtifactStore(root).load(first)
            self.assertTrue(first.chunks_path.is_file())
            self.assertTrue(second.reused_existing_artifact)
            self.assertEqual(loaded.chunks[0].content_text, "Sensor requirement")
            self.assertEqual(first.manifest.status, "ready_for_embedding")

    def test_rejects_changed_content_for_same_document_and_policy(self):
        """A policy version cannot silently point to two different chunk payloads."""
        with tempfile.TemporaryDirectory() as directory:
            self.persist(Path(directory), result())
            with self.assertRaises(FileExistsError):
                self.persist(Path(directory), result(text="Changed source content"))

    def test_policy_version_creates_a_separate_artifact(self):
        """A deliberately versioned chunking change retains both reproducible outputs."""
        with tempfile.TemporaryDirectory() as directory:
            first = self.persist(Path(directory), result(policy="canonical-chunks-v1"))
            second = self.persist(Path(directory), result(policy="canonical-chunks-v2"))
            self.assertNotEqual(first.chunks_path, second.chunks_path)
            self.assertTrue(second.chunks_path.is_file())

    def test_checksum_prevents_corrupted_artifact_reuse(self):
        """Modified JSON cannot be loaded with an old manifest checksum."""
        with tempfile.TemporaryDirectory() as directory:
            artifact = self.persist(Path(directory), result())
            artifact.chunks_path.write_text("{}", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                ChunkArtifactStore(Path(directory)).load(artifact)

    def test_blocking_rejection_marks_artifact_for_review(self):
        """Quality-gated results can be stored for audit but are not ready for embedding."""
        with tempfile.TemporaryDirectory() as directory:
            artifact = self.persist(Path(directory), result(rejected=[ChunkRejection(reason="Extraction conflict", severity="error")]))
            self.assertEqual(artifact.manifest.status, "needs_review")

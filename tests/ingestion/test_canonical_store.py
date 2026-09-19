"""Unit tests for immutable local Master JSON persistence."""

from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from backend.ingestion.models import CanonicalDocument, CanonicalNode, DocumentFamily, DocumentMetadata, NodeType, ParserInfo, Provenance, SourceType
from backend.ingestion.persistence.canonical_store import CanonicalArtifactStore
from backend.ingestion.persistence.source_registry import SourceRegistry


def make_document(version: str = "v1") -> CanonicalDocument:
    """Builds a minimal valid canonical document for persistence tests."""
    document_id = "doc-001"
    return CanonicalDocument(
        document_id=document_id,
        agent_id="adas-agent",
        source_type=SourceType.DOCX,
        file_name="sensor.docx",
        source_path="data/sensor.docx",
        source_name="sensor",
        version=version,
        ingested_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        source_hash="source-hash",
        parser_info=ParserInfo(parser_name="docling", parser_version="1"),
        metadata=DocumentMetadata(document_family=DocumentFamily.HIERARCHICAL),
        root_nodes=[
            CanonicalNode(
                node_id="root",
                node_type=NodeType.DOCUMENT_ROOT,
                provenance=Provenance(document_id=document_id, source_type=SourceType.DOCX, version=version),
            )
        ],
    )


class CanonicalArtifactStoreTests(unittest.TestCase):
    """Covers local artifact creation and immutable-version behaviour."""

    def test_rejects_corrupted_cached_artifact(self) -> None:
        """A modified JSON file must not be reused with its old manifest checksum."""
        with tempfile.TemporaryDirectory() as directory:
            store = CanonicalArtifactStore(directory)
            artifact = store.persist(make_document())
            artifact.master_json_path.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                store.load(agent_id="adas-agent", document_id="doc-001", version="v1")

    def test_changed_processing_settings_require_new_version(self) -> None:
        """Identical bytes cannot reuse output from different processing settings."""
        with tempfile.TemporaryDirectory() as directory:
            store = CanonicalArtifactStore(directory)
            registry = SourceRegistry(directory)
            path = Path(directory) / "test.docx"
            key = registry.source_key(agent_id="adas-agent", source_type=SourceType.DOCX, file_path=path)
            store.persist(make_document(), source_key=key, processing_key="old-settings")
            selected = registry.resolve(agent_id="adas-agent", source_type=SourceType.DOCX, file_path=path, source_hash="source-hash", processing_key="new-settings")
            self.assertFalse(selected.is_unchanged)
            self.assertEqual(selected.version, "v2")

    def test_persists_and_reuses_identical_document_version(self) -> None:
        """Ensures identical persistence calls return the original immutable artifact."""
        with tempfile.TemporaryDirectory() as directory:
            store = CanonicalArtifactStore(Path(directory))
            first = store.persist(make_document())
            second = store.persist(make_document())

            self.assertTrue(first.master_json_path.is_file())
            self.assertTrue(first.manifest_path.is_file())
            self.assertEqual(first.manifest.canonical_artifact_path, "canonical/adas-agent/doc-001/v1/master.json")
            self.assertTrue(second.reused_existing_version)

    def test_rejects_different_content_for_same_version(self) -> None:
        """Ensures a changed document must be given a distinct version identifier."""
        with tempfile.TemporaryDirectory() as directory:
            store = CanonicalArtifactStore(Path(directory))
            original = make_document()
            store.persist(original)
            changed = original.model_copy(update={"source_hash": "changed-source-hash"})

            with self.assertRaises(FileExistsError):
                store.persist(changed)

    def test_registry_reuses_unchanged_source_and_versions_changed_source(self) -> None:
        """Ensures one local source has a stable document identity across versions."""
        with tempfile.TemporaryDirectory() as directory:
            store = CanonicalArtifactStore(Path(directory))
            registry = SourceRegistry(store.storage_root)
            source_path = Path(directory) / "sensor.docx"
            first = registry.resolve(
                agent_id="adas-agent",
                source_type=SourceType.DOCX,
                file_path=source_path,
                source_hash="hash-v1",
            )
            store.persist(
                make_document().model_copy(update={"source_hash": "hash-v1", "document_id": first.document_id}),
                source_key=first.source_key,
            )
            unchanged = registry.resolve(
                agent_id="adas-agent",
                source_type=SourceType.DOCX,
                file_path=source_path,
                source_hash="hash-v1",
            )
            changed = registry.resolve(
                agent_id="adas-agent",
                source_type=SourceType.DOCX,
                file_path=source_path,
                source_hash="hash-v2",
            )

            self.assertTrue(unchanged.is_unchanged)
            self.assertEqual(unchanged.document_id, first.document_id)
            self.assertEqual(changed.document_id, first.document_id)
            self.assertEqual(changed.version, "v2")

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from new_ingestion.persistence.canonical_store import (
    CanonicalPersistenceError, persist_canonical,
)
from new_ingestion.schema.schema_define import (
    CanonicalDocument, DocumentMetadata, ExtractionMetadata, SourceType,
)


def _document(document_id: str, title: str | None = None) -> CanonicalDocument:
    return CanonicalDocument(
        document=DocumentMetadata(
            document_id=document_id, source_file_name="vehicle.xlsx", source_type=SourceType.XLSX,
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            title=title, extraction=ExtractionMetadata(extractor="test"),
        ),
    )


class CanonicalStoreTests(unittest.TestCase):
    def test_writes_master_json_at_the_versioned_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            destination = persist_canonical(_document("doc-1"), agent_id="demo-agent",
                                              version_id="v1", output_root=output_root)
            self.assertEqual(destination, output_root / "demo-agent" / "doc-1" / "v1" / "master.json")
            self.assertTrue(destination.is_file())
            self.assertIn('"document_id": "doc-1"', destination.read_text(encoding="utf-8"))

    def test_rewriting_identical_content_is_a_no_op(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            first = persist_canonical(_document("doc-1"), agent_id="demo-agent",
                                       version_id="v1", output_root=output_root)
            second = persist_canonical(_document("doc-1"), agent_id="demo-agent",
                                        version_id="v1", output_root=output_root)
            self.assertEqual(first, second)

    def test_changed_content_at_an_existing_version_path_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            output_root = Path(tmp)
            persist_canonical(_document("doc-1"), agent_id="demo-agent",
                               version_id="v1", output_root=output_root)
            with self.assertRaises(CanonicalPersistenceError):
                persist_canonical(_document("doc-1", title="changed"), agent_id="demo-agent",
                                   version_id="v1", output_root=output_root)


if __name__ == "__main__":
    unittest.main()

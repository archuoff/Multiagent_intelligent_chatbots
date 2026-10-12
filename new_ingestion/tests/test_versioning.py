"""Focused tests for pure document-versioning decisions."""

from __future__ import annotations

import unittest

from new_ingestion.injestion_control.versioning import (
    DocumentVersionRecord,
    VersioningAction,
    decide_document_version,
)


class VersioningTests(unittest.TestCase):
    HASH_A = "a" * 64
    HASH_B = "b" * 64

    def record(self, number: int, source_hash: str = HASH_A, version_id: str | None = None) -> DocumentVersionRecord:
        return DocumentVersionRecord(
            logical_document_key=" supplier-guide:adas-sensor-mounting ",
            document_id=" doc-1 ",
            version_id=version_id or f"version-{number}",
            version_number=number,
            source_file_hash=source_hash,
        )

    def test_first_ingestion_creates_document(self) -> None:
        result = decide_document_version(
            logical_document_key=" supplier-guide:adas-sensor-mounting ",
            document_id=" doc-1 ",
            current_source_file_hash=f" {self.HASH_A.upper()} ",
            previous_versions=[],
        )
        self.assertEqual(result.action, VersioningAction.CREATE_DOCUMENT)
        self.assertEqual(result.logical_document_key, "supplier-guide:adas-sensor-mounting")
        self.assertEqual(result.document_id, "doc-1")
        self.assertEqual(result.next_version_number, 1)
        self.assertIsNone(result.previous_version_id)

    def test_latest_matching_hash_returns_no_change(self) -> None:
        result = decide_document_version(
            logical_document_key="supplier-guide:adas-sensor-mounting",
            document_id="doc-1",
            current_source_file_hash=self.HASH_A,
            previous_versions=[self.record(1), self.record(2)],
        )
        self.assertEqual(result.action, VersioningAction.NO_CHANGE)
        self.assertEqual(result.previous_version_id, "version-2")
        self.assertEqual(result.previous_version_number, 2)
        self.assertEqual(result.next_version_number, 2)

    def test_changed_latest_hash_creates_next_version(self) -> None:
        result = decide_document_version(
            logical_document_key="supplier-guide:adas-sensor-mounting",
            document_id="doc-1",
            current_source_file_hash=self.HASH_B,
            previous_versions=[self.record(1), self.record(2)],
        )
        self.assertEqual(result.action, VersioningAction.CREATE_VERSION)
        self.assertEqual(result.previous_version_id, "version-2")
        self.assertEqual(result.next_version_number, 3)

    def test_matching_older_hash_still_creates_version(self) -> None:
        result = decide_document_version(
            logical_document_key="supplier-guide:adas-sensor-mounting",
            document_id="doc-1",
            current_source_file_hash=self.HASH_A,
            previous_versions=[self.record(1, self.HASH_A), self.record(2, self.HASH_B)],
        )
        self.assertEqual(result.action, VersioningAction.CREATE_VERSION)
        self.assertEqual(result.previous_version_id, "version-2")
        self.assertEqual(result.next_version_number, 3)

    def test_force_reingestion_creates_version_for_same_hash(self) -> None:
        result = decide_document_version(
            logical_document_key="supplier-guide:adas-sensor-mounting",
            document_id="doc-1",
            current_source_file_hash=self.HASH_A,
            previous_versions=[self.record(4)],
            force_reingestion=True,
        )
        self.assertEqual(result.action, VersioningAction.CREATE_VERSION)
        self.assertEqual(result.next_version_number, 5)

    def test_previous_records_must_match_logical_document_and_document_id(self) -> None:
        mismatched_key = DocumentVersionRecord("other-key", "doc-1", "v1", 1, self.HASH_A)
        with self.assertRaises(ValueError):
            decide_document_version(
                logical_document_key="supplier-guide:adas-sensor-mounting",
                document_id="doc-1",
                current_source_file_hash=self.HASH_A,
                previous_versions=[mismatched_key],
            )
        mismatched_document = DocumentVersionRecord("supplier-guide:adas-sensor-mounting", "doc-2", "v1", 1, self.HASH_A)
        with self.assertRaises(ValueError):
            decide_document_version(
                logical_document_key="supplier-guide:adas-sensor-mounting",
                document_id="doc-1",
                current_source_file_hash=self.HASH_A,
                previous_versions=[mismatched_document],
            )

    def test_duplicate_version_numbers_and_ids_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            decide_document_version(
                logical_document_key="supplier-guide:adas-sensor-mounting",
                document_id="doc-1",
                current_source_file_hash=self.HASH_A,
                previous_versions=[self.record(1), self.record(1, self.HASH_B, "version-2")],
            )
        with self.assertRaises(ValueError):
            decide_document_version(
                logical_document_key="supplier-guide:adas-sensor-mounting",
                document_id="doc-1",
                current_source_file_hash=self.HASH_A,
                previous_versions=[self.record(1), self.record(2, self.HASH_B, "version-1")],
            )

    def test_invalid_inputs_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            DocumentVersionRecord("", "doc-1", "v1", 1, self.HASH_A)
        with self.assertRaises(ValueError):
            DocumentVersionRecord("key", "doc-1", "v1", 0, self.HASH_A)
        with self.assertRaises(ValueError):
            DocumentVersionRecord("key", "doc-1", "v1", 1, "not-a-sha256")
        with self.assertRaises(TypeError):
            decide_document_version(
                logical_document_key="key",
                document_id="doc-1",
                current_source_file_hash=self.HASH_A,
                previous_versions=[object()],  # type: ignore[list-item]
            )


if __name__ == "__main__":
    unittest.main()

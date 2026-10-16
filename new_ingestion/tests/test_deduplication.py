"""Tests for pure source, asset, and element deduplication."""

from __future__ import annotations

import unittest

from new_ingestion.injestion_control.deduplication import (
    AssetReference,
    ElementChangeType,
    ElementFingerprint,
    SourceDeduplicationStatus,
    compare_element_fingerprints,
    deduplicate_assets,
    detect_duplicate_source,
)


class DeduplicationTests(unittest.TestCase):
    HASH_A = "a" * 64
    HASH_B = "b" * 64
    HASH_C = "c" * 64
    HASH_D = "d" * 64
    HASH_X = "e" * 64

    def test_source_hash_duplicate_ignores_case_and_whitespace(self) -> None:
        result = detect_duplicate_source(
            source_file_hash=f"  {'A' * 64} ",
            known_source_hashes=[self.HASH_B, f" {self.HASH_A} "],
        )
        self.assertEqual(result.status, SourceDeduplicationStatus.DUPLICATE)
        self.assertEqual(result.source_file_hash, self.HASH_A)
        self.assertEqual(result.matched_source_hash, self.HASH_A)

    def test_new_source_does_not_use_filename(self) -> None:
        result = detect_duplicate_source(
            source_file_hash=self.HASH_C,
            known_source_hashes=[self.HASH_A, self.HASH_B],
        )
        self.assertEqual(result.status, SourceDeduplicationStatus.NEW)
        self.assertIsNone(result.matched_source_hash)

    def test_asset_deduplication_preserves_first_hash_owner_and_order(self) -> None:
        result = deduplicate_assets([
            AssetReference("asset-a", self.HASH_A.upper()),
            AssetReference("asset-b", f" {self.HASH_A} "),
            AssetReference("asset-c", self.HASH_B),
            AssetReference("asset-d", self.HASH_A),
        ])
        self.assertEqual(result.canonical_asset_ids, ("asset-a", "asset-c"))
        self.assertEqual(result.asset_to_canonical_id, {
            "asset-a": "asset-a",
            "asset-b": "asset-a",
            "asset-c": "asset-c",
            "asset-d": "asset-a",
        })
        self.assertEqual(result.duplicate_asset_ids, ("asset-b", "asset-d"))

    def test_asset_ids_must_be_unique(self) -> None:
        with self.assertRaises(ValueError):
            deduplicate_assets([
                AssetReference("asset-a", self.HASH_A),
                AssetReference("asset-a", self.HASH_B),
            ])

    def test_element_comparison_uses_id_and_content_hash_only(self) -> None:
        changes = compare_element_fingerprints(
            previous=[
                ElementFingerprint("element-1", self.HASH_A),
                ElementFingerprint("element-2", self.HASH_B),
                ElementFingerprint("element-4", self.HASH_D),
            ],
            current=[
                ElementFingerprint("element-1", f" {self.HASH_A.upper()} "),
                ElementFingerprint("element-2", self.HASH_C),
                ElementFingerprint("element-3", self.HASH_X),
            ],
        )
        self.assertEqual([change.element_id for change in changes], [
            "element-1", "element-2", "element-3", "element-4",
        ])
        self.assertEqual([change.change_type for change in changes], [
            ElementChangeType.UNCHANGED,
            ElementChangeType.CHANGED,
            ElementChangeType.NEW,
            ElementChangeType.REMOVED,
        ])
        self.assertEqual(changes[0].previous_content_hash, self.HASH_A)
        self.assertEqual(changes[0].current_content_hash, self.HASH_A)
        self.assertEqual(changes[1].previous_content_hash, self.HASH_B)
        self.assertEqual(changes[1].current_content_hash, self.HASH_C)
        self.assertIsNone(changes[2].previous_content_hash)
        self.assertEqual(changes[3].previous_content_hash, self.HASH_D)
        self.assertIsNone(changes[3].current_content_hash)

    def test_duplicate_element_ids_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            compare_element_fingerprints(
                previous=[ElementFingerprint("element-1", self.HASH_A), ElementFingerprint("element-1", self.HASH_B)],
                current=[],
            )
        with self.assertRaises(ValueError):
            compare_element_fingerprints(
                previous=[],
                current=[ElementFingerprint("element-1", self.HASH_A), ElementFingerprint("element-1", self.HASH_B)],
            )

    def test_invalid_inputs_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            detect_duplicate_source(source_file_hash="", known_source_hashes=[])
        with self.assertRaises(ValueError):
            detect_duplicate_source(source_file_hash="not-a-sha256", known_source_hashes=[])
        with self.assertRaises(ValueError):
            AssetReference("", self.HASH_A)
        with self.assertRaises(ValueError):
            AssetReference("asset-a", "")
        with self.assertRaises(ValueError):
            AssetReference("asset-a", "not-a-sha256")
        with self.assertRaises(ValueError):
            ElementFingerprint("", self.HASH_A)
        with self.assertRaises(ValueError):
            ElementFingerprint("element-1", "")
        with self.assertRaises(ValueError):
            ElementFingerprint("element-1", "not-a-sha256")
        with self.assertRaises(TypeError):
            deduplicate_assets([object()])  # type: ignore[list-item]


if __name__ == "__main__":
    unittest.main()

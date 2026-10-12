"""Focused tests for deterministic ingestion-control hashing helpers."""

from __future__ import annotations

import tempfile
import unittest
import uuid
from pathlib import Path

from new_ingestion.injestion_control.hashing import (
    HashingError,
    canonical_json,
    canonical_json_bytes,
    deterministic_uuid5,
    hash_bytes,
    hash_file,
    hash_normalized_content,
    processing_fingerprint,
)


class HashingTests(unittest.TestCase):
    def test_same_and_different_bytes(self) -> None:
        self.assertEqual(hash_bytes(b"same"), hash_bytes(b"same"))
        self.assertNotEqual(hash_bytes(b"one"), hash_bytes(b"two"))
        self.assertEqual(len(hash_bytes(b"same")), 64)
        self.assertEqual(hash_bytes(b"same"), hash_bytes(b"same").lower())

    def test_file_hash_is_repeatable_and_handles_large_files_incrementally(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "large.bin"
            data = (b"0123456789abcdef" * 262_144) + b"tail"
            path.write_bytes(data)
            self.assertEqual(hash_file(path, chunk_size=4093), hash_bytes(data))
            self.assertEqual(hash_file(path), hash_file(path))

    def test_canonical_json_sorts_keys_but_preserves_list_order(self) -> None:
        first = {"b": 2, "a": {"d": 4, "c": 3}, "list": [1, 2, 3]}
        second = {"list": [1, 2, 3], "a": {"c": 3, "d": 4}, "b": 2}
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(hash_normalized_content(first), hash_normalized_content(second))
        self.assertNotEqual(
            hash_normalized_content(first),
            hash_normalized_content({**first, "list": [3, 2, 1]}),
        )
        self.assertEqual(canonical_json({"message": "café"}), '{"message":"café"}')
        self.assertEqual(canonical_json_bytes(first), canonical_json(first).encode("utf-8"))

    def test_volatile_fields_are_excluded_from_normalized_hash(self) -> None:
        first = {
            "content": {"value": 1},
            "timestamp": "2026-01-01T00:00:00Z",
            "run_id": "run-a",
            "source_path": "C:/one/file.pdf",
            "relative_path": "documents/file.pdf",
        }
        second = {
            "run_id": "run-b",
            "timestamp": "2027-01-01T00:00:00Z",
            "source_path": "C:/one/file.pdf",
            "relative_path": "documents/file.pdf",
            "content": {"value": 1},
        }
        self.assertEqual(hash_normalized_content(first), hash_normalized_content(second))
        self.assertIn("source_path", canonical_json(first))
        self.assertIn("relative_path", canonical_json(first))
        self.assertNotEqual(
            hash_normalized_content(first),
            hash_normalized_content({**first, "source_path": "D:/different/file.pdf"}),
        )

    def test_custom_empty_volatile_field_set_preserves_runtime_fields(self) -> None:
        content = {"timestamp": "keep-this", "run_id": "keep-this-too"}
        self.assertIn(
            "timestamp",
            canonical_json(content, volatile_fields=set()),
        )

    def test_different_normalized_content_has_different_hash(self) -> None:
        self.assertNotEqual(
            hash_normalized_content({"value": 1}),
            hash_normalized_content({"value": 2}),
        )

    def test_processing_fingerprint_is_repeatable_and_sensitive_to_inputs(self) -> None:
        source_hash = hash_bytes(b"source")
        kwargs = {
            "source_file_hash": source_hash,
            "extractor_name": "xlsx",
            "extractor_version": "1.0",
            "extractor_configuration": {"ocr": False, "options": {"b": 2, "a": 1}},
            "canonical_schema_version": "0.1.0",
        }
        first = processing_fingerprint(**kwargs)
        self.assertEqual(first, processing_fingerprint(**kwargs))
        self.assertNotEqual(first, processing_fingerprint(**{**kwargs, "extractor_version": "1.1"}))
        self.assertNotEqual(first, processing_fingerprint(**{**kwargs, "canonical_schema_version": "0.2.0"}))
        self.assertNotEqual(first, processing_fingerprint(**{**kwargs, "extractor_configuration": {"ocr": True}}))

    def test_deterministic_uuid5_is_repeatable(self) -> None:
        namespace = uuid.NAMESPACE_URL
        first = deterministic_uuid5(namespace, "document:file.pdf")
        self.assertEqual(first, deterministic_uuid5(namespace, "document:file.pdf"))
        self.assertNotEqual(first, deterministic_uuid5(namespace, "document:other.pdf"))
        self.assertEqual(first, deterministic_uuid5(str(namespace), "document:file.pdf"))

    def test_invalid_inputs_raise_clear_errors(self) -> None:
        with self.assertRaises(TypeError):
            hash_bytes("text")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            hash_file(Path("missing-file.bin"), chunk_size=0)
        with self.assertRaises(HashingError):
            canonical_json({"bad": {1, 2}})
        with self.assertRaises(HashingError):
            canonical_json({"bad": float("nan")})
        with self.assertRaises(HashingError):
            canonical_json({1: "non-string key"})
        with self.assertRaises(ValueError):
            processing_fingerprint("not-a-sha256", "xlsx", "1", {}, "0.1.0")
        with self.assertRaises(ValueError):
            deterministic_uuid5(uuid.NAMESPACE_URL, "")
        with self.assertRaises(ValueError):
            deterministic_uuid5("not-a-uuid", "key")


if __name__ == "__main__":
    unittest.main()

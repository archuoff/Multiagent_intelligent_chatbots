"""Focused tests for pure idempotency decisions."""

from __future__ import annotations

import json
import unittest

from new_ingestion.injestion_control.idempotency import (
    IdempotencyDecision,
    ProcessingRecord,
    ProcessingStatus,
    decide_idempotency,
)


class IdempotencyTests(unittest.TestCase):
    SOURCE_HASH = "a" * 64
    OTHER_SOURCE_HASH = "b" * 64
    PROCESSING_KEY = "extractor:v1:schema:0.1.0"

    def record(self, *, source: str | None = None, key: str | None = None, status: str = "success", document: str | None = "doc-1") -> ProcessingRecord:
        return ProcessingRecord(
            source_file_hash=source or self.SOURCE_HASH,
            processing_key=key or self.PROCESSING_KEY,
            status=status,
            document_id=document,
            document_version_id="version-1" if document else None,
        )

    def test_no_previous_run_processes(self) -> None:
        result = decide_idempotency(
            source_file_hash=self.SOURCE_HASH,
            processing_key=self.PROCESSING_KEY,
            previous_runs=[],
        )
        self.assertEqual(result.decision, IdempotencyDecision.PROCESS)
        self.assertIsNone(result.matched_previous_run)

    def test_successful_exact_duplicate_skips(self) -> None:
        previous = self.record(status=ProcessingStatus.SUCCESS)
        result = decide_idempotency(
            source_file_hash=self.SOURCE_HASH,
            processing_key=self.PROCESSING_KEY,
            previous_runs=[previous],
        )
        self.assertEqual(result.decision, IdempotencyDecision.SKIP)
        self.assertIs(result.matched_previous_run, previous)

    def test_completed_exact_duplicate_also_skips(self) -> None:
        result = decide_idempotency(
            source_file_hash=self.SOURCE_HASH,
            processing_key=self.PROCESSING_KEY,
            previous_runs=[self.record(status="completed")],
        )
        self.assertEqual(result.decision, IdempotencyDecision.SKIP)

    def test_failed_exact_duplicate_retries(self) -> None:
        previous = self.record(status=ProcessingStatus.FAILED, document=None)
        result = decide_idempotency(
            source_file_hash=self.SOURCE_HASH,
            processing_key=self.PROCESSING_KEY,
            previous_runs=[previous],
        )
        self.assertEqual(result.decision, IdempotencyDecision.RETRY)
        self.assertEqual(result.matched_previous_run.status, "failed")

    def test_changed_source_processes(self) -> None:
        result = decide_idempotency(
            source_file_hash=self.OTHER_SOURCE_HASH,
            processing_key=self.PROCESSING_KEY,
            previous_runs=[self.record()],
        )
        self.assertEqual(result.decision, IdempotencyDecision.PROCESS)
        self.assertIsNone(result.matched_previous_run)

    def test_changed_processing_key_processes(self) -> None:
        result = decide_idempotency(
            source_file_hash=self.SOURCE_HASH,
            processing_key="extractor:v2:schema:0.1.0",
            previous_runs=[self.record()],
        )
        self.assertEqual(result.decision, IdempotencyDecision.PROCESS)
        self.assertIsNone(result.matched_previous_run)

    def test_force_reingestion_overrides_successful_duplicate(self) -> None:
        previous = self.record()
        result = decide_idempotency(
            source_file_hash=self.SOURCE_HASH,
            processing_key=self.PROCESSING_KEY,
            previous_runs=[previous],
            force_reingestion=True,
        )
        self.assertEqual(result.decision, IdempotencyDecision.PROCESS)
        self.assertIs(result.matched_previous_run, previous)

    def test_last_matching_run_is_authoritative(self) -> None:
        older_success = self.record(status="success")
        newer_failure = self.record(status="failed", document=None)
        result = decide_idempotency(
            source_file_hash=self.SOURCE_HASH,
            processing_key=self.PROCESSING_KEY,
            previous_runs=[older_success, self.record(source=self.OTHER_SOURCE_HASH), newer_failure],
        )
        self.assertEqual(result.decision, IdempotencyDecision.RETRY)
        self.assertIs(result.matched_previous_run, newer_failure)

    def test_result_is_json_serializable(self) -> None:
        result = decide_idempotency(
            source_file_hash=self.SOURCE_HASH,
            processing_key=self.PROCESSING_KEY,
            previous_runs=[self.record()],
        )
        payload = result.to_dict()
        json.dumps(payload)
        self.assertEqual(payload["decision"], "skip")
        self.assertEqual(json.loads(result.to_json())["current_source_hash"], self.SOURCE_HASH)

    def test_invalid_inputs_raise_clear_errors(self) -> None:
        with self.assertRaises(ValueError):
            decide_idempotency(source_file_hash="", processing_key=self.PROCESSING_KEY, previous_runs=[])
        with self.assertRaises(ValueError):
            decide_idempotency(source_file_hash=self.SOURCE_HASH, processing_key="", previous_runs=[])
        with self.assertRaises(ValueError):
            ProcessingRecord(source_file_hash="", processing_key=self.PROCESSING_KEY, status="failed")
        with self.assertRaises(TypeError):
            decide_idempotency(source_file_hash=self.SOURCE_HASH, processing_key=self.PROCESSING_KEY, previous_runs=[object()])  # type: ignore[list-item]
        with self.assertRaises(TypeError):
            decide_idempotency(source_file_hash=self.SOURCE_HASH, processing_key=self.PROCESSING_KEY, previous_runs="not-a-sequence")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()

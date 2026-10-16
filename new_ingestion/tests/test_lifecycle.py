"""Focused tests for the pure lifecycle state machine."""

from __future__ import annotations

import unittest

from new_ingestion.injestion_control.lifecycle import (
    InvalidLifecycleTransition,
    LifecycleEvent,
    LifecycleRecord,
    LifecycleStatus,
    transition_lifecycle,
)


class LifecycleTests(unittest.TestCase):
    def record(self, status: LifecycleStatus | str, failure_reason: str | None = None) -> LifecycleRecord:
        return LifecycleRecord(" doc-1 ", " version-1 ", status, failure_reason)

    def test_pending_start_processing(self) -> None:
        original = self.record(LifecycleStatus.PENDING)
        next_record, result = transition_lifecycle(original, LifecycleEvent.START_PROCESSING)
        self.assertEqual(next_record.status, LifecycleStatus.PROCESSING)
        self.assertTrue(result.changed)
        self.assertEqual(result.previous_status, LifecycleStatus.PENDING)
        self.assertEqual(result.new_status, LifecycleStatus.PROCESSING)
        self.assertIsNone(next_record.failure_reason)

    def test_processing_succeeds(self) -> None:
        next_record, result = transition_lifecycle(self.record("processing"), "processing_succeeded")
        self.assertEqual(next_record.status, LifecycleStatus.PROCESSED)
        self.assertTrue(result.changed)

    def test_processing_fails_with_reason(self) -> None:
        next_record, result = transition_lifecycle(
            self.record(LifecycleStatus.PROCESSING),
            LifecycleEvent.PROCESSING_FAILED,
            failure_reason=" Docling conversion failed ",
        )
        self.assertEqual(next_record.status, LifecycleStatus.FAILED)
        self.assertEqual(next_record.failure_reason, "Docling conversion failed")
        self.assertEqual(result.failure_reason, "Docling conversion failed")

    def test_failed_retry_clears_failure_reason(self) -> None:
        next_record, result = transition_lifecycle(
            self.record(LifecycleStatus.FAILED, "previous failure"),
            LifecycleEvent.RETRY_PROCESSING,
        )
        self.assertEqual(next_record.status, LifecycleStatus.PROCESSING)
        self.assertIsNone(next_record.failure_reason)
        self.assertTrue(result.changed)

    def test_allowed_idempotent_events_do_not_change_state(self) -> None:
        processing, start_result = transition_lifecycle(self.record("processing"), "start_processing")
        self.assertEqual(processing.status, LifecycleStatus.PROCESSING)
        self.assertFalse(start_result.changed)

        processed, success_result = transition_lifecycle(self.record("processed"), "processing_succeeded")
        self.assertEqual(processed.status, LifecycleStatus.PROCESSED)
        self.assertFalse(success_result.changed)

        failed, failure_result = transition_lifecycle(
            self.record("failed", "original"),
            "processing_failed",
            failure_reason="duplicate notification",
        )
        self.assertEqual(failed.failure_reason, "original")
        self.assertFalse(failure_result.changed)

    def test_invalid_transitions_raise_clear_exception(self) -> None:
        invalid_cases = [
            ("pending", "processing_succeeded"),
            ("pending", "processing_failed"),
            ("processed", "start_processing"),
            ("processed", "retry_processing"),
        ]
        for status, event in invalid_cases:
            with self.subTest(status=status, event=event):
                with self.assertRaises(InvalidLifecycleTransition) as context:
                    transition_lifecycle(self.record(status), event)
                message = str(context.exception)
                self.assertIn(status, message)
                self.assertIn(event, message)
        with self.assertRaises(InvalidLifecycleTransition):
            transition_lifecycle(self.record("failed", "previous failure"), "start_processing")

    def test_failure_reason_rules(self) -> None:
        with self.assertRaises(ValueError):
            transition_lifecycle(self.record("processing"), "processing_failed")
        with self.assertRaises(ValueError):
            transition_lifecycle(
                self.record("processing"),
                "processing_succeeded",
                failure_reason="not allowed",
            )
        with self.assertRaises(ValueError):
            LifecycleRecord("doc-1", "version-1", "failed")
        with self.assertRaises(ValueError):
            LifecycleRecord("doc-1", "version-1", "processed", "stale reason")

    def test_record_is_normalized_and_original_is_not_mutated(self) -> None:
        original = self.record(" pending ")
        next_record, _ = transition_lifecycle(original, "start_processing")
        self.assertEqual(original.document_id, "doc-1")
        self.assertEqual(original.version_id, "version-1")
        self.assertEqual(original.status, LifecycleStatus.PENDING)
        self.assertEqual(next_record.document_id, original.document_id)
        self.assertIsNot(next_record, original)
        with self.assertRaises(Exception):
            original.status = LifecycleStatus.PROCESSING  # type: ignore[misc]

    def test_invalid_record_and_event_inputs_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            LifecycleRecord("", "version-1", "pending")
        with self.assertRaises(ValueError):
            LifecycleRecord("doc-1", "version-1", "unknown")
        with self.assertRaises(ValueError):
            transition_lifecycle(self.record("pending"), "unknown_event")
        with self.assertRaises(TypeError):
            transition_lifecycle(object(), LifecycleEvent.START_PROCESSING)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()

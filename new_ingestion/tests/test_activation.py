"""Focused tests for pure activation planning."""

from __future__ import annotations

import unittest

from new_ingestion.injestion_control.activation import (
    ActivationAction,
    ActivationVersionRecord,
    InvalidActivationState,
    plan_activation,
)
from new_ingestion.injestion_control.lifecycle import LifecycleStatus


class ActivationTests(unittest.TestCase):
    def version(
        self,
        version_id: str,
        number: int,
        status: LifecycleStatus | str = LifecycleStatus.PROCESSED,
        active: bool = False,
        document_id: str = "doc-1",
    ) -> ActivationVersionRecord:
        return ActivationVersionRecord(document_id, version_id, number, status, active)

    def test_first_processed_version_is_activated(self) -> None:
        plan = plan_activation(candidate=self.version("version-1", 1), existing_versions=[])
        self.assertEqual(plan.action, ActivationAction.ACTIVATE_NEW)
        self.assertEqual(plan.activate_version_id, "version-1")
        self.assertEqual(plan.deactivate_version_ids, ())

    def test_processed_candidate_replaces_existing_active_version(self) -> None:
        existing = self.version("version-1", 1, active=True)
        plan = plan_activation(candidate=self.version("version-2", 2), existing_versions=[existing])
        self.assertEqual(plan.action, ActivationAction.ACTIVATE_NEW)
        self.assertEqual(plan.activate_version_id, "version-2")
        self.assertEqual(plan.deactivate_version_ids, ("version-1",))

    def test_non_processed_candidate_is_rejected_without_deactivation(self) -> None:
        existing = self.version("version-1", 1, active=True)
        for status in (LifecycleStatus.PENDING, LifecycleStatus.PROCESSING, LifecycleStatus.FAILED):
            with self.subTest(status=status):
                candidate = self.version("version-2", 2, status=status)
                plan = plan_activation(candidate=candidate, existing_versions=[existing])
                self.assertEqual(plan.action, ActivationAction.REJECT)
                self.assertIsNone(plan.activate_version_id)
                self.assertEqual(plan.deactivate_version_ids, ())
                self.assertIn("processed", plan.reason)

    def test_candidate_already_active_keeps_current(self) -> None:
        existing = self.version("version-1", 1, active=True)
        candidate = self.version("version-1", 1, active=True)
        plan = plan_activation(candidate=candidate, existing_versions=[existing])
        self.assertEqual(plan.action, ActivationAction.KEEP_CURRENT)
        self.assertIsNone(plan.activate_version_id)
        self.assertEqual(plan.deactivate_version_ids, ())

    def test_lower_version_can_be_activated_by_caller_policy(self) -> None:
        existing = self.version("version-3", 3, active=True)
        plan = plan_activation(candidate=self.version("version-2", 2), existing_versions=[existing])
        self.assertEqual(plan.action, ActivationAction.ACTIVATE_NEW)
        self.assertIn("lower version number", plan.reason)

    def test_multiple_active_versions_are_invalid(self) -> None:
        with self.assertRaises(InvalidActivationState):
            plan_activation(
                candidate=self.version("version-3", 3),
                existing_versions=[
                    self.version("version-1", 1, active=True),
                    self.version("version-2", 2, active=True),
                ],
            )

    def test_existing_identity_and_duplicates_are_validated(self) -> None:
        with self.assertRaises(InvalidActivationState):
            plan_activation(
                candidate=self.version("version-2", 2),
                existing_versions=[self.version("version-1", 1, document_id="other-doc")],
            )
        with self.assertRaises(InvalidActivationState):
            plan_activation(
                candidate=self.version("version-2", 2),
                existing_versions=[self.version("version-1", 1), self.version("version-1", 2)],
            )
        with self.assertRaises(InvalidActivationState):
            plan_activation(
                candidate=self.version("version-3", 3),
                existing_versions=[self.version("version-1", 1), self.version("version-2", 1)],
            )

    def test_candidate_conflicts_with_existing_record(self) -> None:
        existing = self.version("version-2", 2, active=True)
        conflicting = self.version("version-2", 2, active=False)
        with self.assertRaises(InvalidActivationState):
            plan_activation(candidate=conflicting, existing_versions=[existing])

    def test_records_normalize_identifiers_and_are_not_mutated(self) -> None:
        candidate = ActivationVersionRecord(" doc-1 ", " version-1 ", 1, " processed ")
        plan = plan_activation(candidate=candidate, existing_versions=[])
        self.assertEqual(candidate.document_id, "doc-1")
        self.assertEqual(candidate.version_id, "version-1")
        self.assertEqual(plan.document_id, "doc-1")
        with self.assertRaises(Exception):
            candidate.is_active = True  # type: ignore[misc]

    def test_invalid_record_and_input_values_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            ActivationVersionRecord("", "version-1", 1, LifecycleStatus.PROCESSED)
        with self.assertRaises(ValueError):
            ActivationVersionRecord("doc-1", "version-1", 0, LifecycleStatus.PROCESSED)
        with self.assertRaises(ValueError):
            ActivationVersionRecord("doc-1", "version-1", 1, "unknown")
        with self.assertRaises(TypeError):
            ActivationVersionRecord("doc-1", "version-1", 1, LifecycleStatus.PROCESSED, 1)  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            plan_activation(candidate=object(), existing_versions=[])  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            plan_activation(candidate=self.version("version-1", 1), existing_versions="bad")  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()

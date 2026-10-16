"""Pure activation planning for successfully processed document versions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence

from .lifecycle import LifecycleStatus


class ActivationAction(StrEnum):
    """Action a future coordinator should apply atomically."""

    ACTIVATE_NEW = "activate_new"
    KEEP_CURRENT = "keep_current"
    REJECT = "reject"


class InvalidActivationState(ValueError):
    """Raised when existing activation state is internally inconsistent."""


def _required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class ActivationVersionRecord:
    """Immutable activation-relevant state for one document version."""

    document_id: str
    version_id: str
    version_number: int
    lifecycle_status: LifecycleStatus | str
    is_active: bool = False

    def __post_init__(self) -> None:
        document_id = _required_text(self.document_id, "document_id")
        version_id = _required_text(self.version_id, "version_id")
        if isinstance(self.version_number, bool) or not isinstance(self.version_number, int) or self.version_number < 1:
            raise ValueError("version_number must be an integer greater than or equal to 1")
        try:
            status = self.lifecycle_status if isinstance(self.lifecycle_status, LifecycleStatus) else LifecycleStatus(_required_text(self.lifecycle_status, "lifecycle_status"))
        except (TypeError, ValueError) as error:
            raise ValueError(f"lifecycle_status must be a valid LifecycleStatus: {self.lifecycle_status!r}") from error
        if not isinstance(self.is_active, bool):
            raise TypeError("is_active must be a boolean")
        object.__setattr__(self, "document_id", document_id)
        object.__setattr__(self, "version_id", version_id)
        object.__setattr__(self, "lifecycle_status", status)


@dataclass(frozen=True, slots=True)
class ActivationPlan:
    """Pure plan describing activation and deactivation IDs."""

    action: ActivationAction
    document_id: str
    candidate_version_id: str
    activate_version_id: str | None
    deactivate_version_ids: tuple[str, ...]
    reason: str


def _reject_plan(candidate: ActivationVersionRecord, reason: str) -> ActivationPlan:
    return ActivationPlan(
        action=ActivationAction.REJECT,
        document_id=candidate.document_id,
        candidate_version_id=candidate.version_id,
        activate_version_id=None,
        deactivate_version_ids=(),
        reason=reason,
    )


def plan_activation(
    *,
    candidate: ActivationVersionRecord,
    existing_versions: Sequence[ActivationVersionRecord],
) -> ActivationPlan:
    """Return an activation plan without changing any supplied record."""
    if not isinstance(candidate, ActivationVersionRecord):
        raise TypeError("candidate must be an ActivationVersionRecord")
    if isinstance(existing_versions, (str, bytes)) or not isinstance(existing_versions, Sequence):
        raise TypeError("existing_versions must be a sequence of ActivationVersionRecord objects")

    existing_ids: set[str] = set()
    existing_numbers: set[int] = set()
    active_versions: list[ActivationVersionRecord] = []
    candidate_existing: ActivationVersionRecord | None = None
    for version in existing_versions:
        if not isinstance(version, ActivationVersionRecord):
            raise TypeError("existing_versions must contain only ActivationVersionRecord objects")
        if version.document_id != candidate.document_id:
            raise InvalidActivationState("existing versions belong to different document IDs")
        if version.version_id in existing_ids:
            raise InvalidActivationState(f"duplicate version ID: {version.version_id}")
        if version.version_number in existing_numbers:
            raise InvalidActivationState(f"duplicate version number: {version.version_number}")
        existing_ids.add(version.version_id)
        existing_numbers.add(version.version_number)
        if version.is_active:
            active_versions.append(version)
        if version.version_id == candidate.version_id:
            candidate_existing = version

    if len(active_versions) > 1:
        raise InvalidActivationState("more than one existing version is active")
    if candidate_existing is not None:
        if candidate_existing != candidate:
            raise InvalidActivationState(
                f"candidate version {candidate.version_id} conflicts with the existing record"
            )
    elif candidate.version_number in existing_numbers:
        raise InvalidActivationState(
            f"candidate version number {candidate.version_number} belongs to another version ID"
        )

    if candidate.lifecycle_status is not LifecycleStatus.PROCESSED:
        return _reject_plan(
            candidate,
            "only a processed lifecycle version can become active",
        )

    if candidate.is_active:
        if active_versions and active_versions[0].version_id != candidate.version_id:
            raise InvalidActivationState(
                "candidate is active while a different existing version is also active"
            )
        return ActivationPlan(
            action=ActivationAction.KEEP_CURRENT,
            document_id=candidate.document_id,
            candidate_version_id=candidate.version_id,
            activate_version_id=None,
            deactivate_version_ids=(),
            reason="candidate is already the active processed version",
        )

    if not active_versions:
        return ActivationPlan(
            action=ActivationAction.ACTIVATE_NEW,
            document_id=candidate.document_id,
            candidate_version_id=candidate.version_id,
            activate_version_id=candidate.version_id,
            deactivate_version_ids=(),
            reason="candidate is processed and there is no active version",
        )

    active = active_versions[0]
    reason = "candidate is processed and will replace the current active version"
    if candidate.version_number < active.version_number:
        reason += "; lower version number accepted because activation policy belongs to the caller"
    return ActivationPlan(
        action=ActivationAction.ACTIVATE_NEW,
        document_id=candidate.document_id,
        candidate_version_id=candidate.version_id,
        activate_version_id=candidate.version_id,
        deactivate_version_ids=(active.version_id,),
        reason=reason,
    )


__all__ = [
    "ActivationAction",
    "ActivationPlan",
    "ActivationVersionRecord",
    "InvalidActivationState",
    "plan_activation",
]

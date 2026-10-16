"""Pure document-version lifecycle state transitions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class LifecycleStatus(StrEnum):
    """Processing state of one document version."""

    PENDING = "pending"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED = "failed"


class LifecycleEvent(StrEnum):
    """Events that can be applied to a lifecycle record."""

    START_PROCESSING = "start_processing"
    PROCESSING_SUCCEEDED = "processing_succeeded"
    PROCESSING_FAILED = "processing_failed"
    RETRY_PROCESSING = "retry_processing"


class InvalidLifecycleTransition(ValueError):
    """Raised when an event is invalid for the current lifecycle status."""


@dataclass(frozen=True, slots=True)
class LifecycleRecord:
    """Immutable lifecycle state for one document version."""

    document_id: str
    version_id: str
    status: LifecycleStatus | str
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        document_id = _normalize_required_text(self.document_id, "document_id")
        version_id = _normalize_required_text(self.version_id, "version_id")
        try:
            status = self.status if isinstance(self.status, LifecycleStatus) else LifecycleStatus(_normalize_required_text(self.status, "status"))
        except (TypeError, ValueError) as error:
            raise ValueError(f"status must be a valid LifecycleStatus: {self.status!r}") from error
        failure_reason = _normalize_optional_text(self.failure_reason, "failure_reason")
        if status is LifecycleStatus.FAILED and failure_reason is None:
            raise ValueError("failure_reason is required when status is 'failed'")
        if status is not LifecycleStatus.FAILED and failure_reason is not None:
            raise ValueError("failure_reason must be empty or None for non-failed statuses")
        object.__setattr__(self, "document_id", document_id)
        object.__setattr__(self, "version_id", version_id)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "failure_reason", failure_reason)


@dataclass(frozen=True, slots=True)
class LifecycleTransitionResult:
    """Evidence describing one lifecycle transition attempt."""

    document_id: str
    version_id: str
    previous_status: LifecycleStatus
    event: LifecycleEvent
    new_status: LifecycleStatus
    changed: bool
    failure_reason: str | None = None


def _normalize_required_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _normalize_optional_text(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string or None")
    normalized = value.strip()
    return normalized or None


_TRANSITIONS: Mapping[LifecycleStatus, Mapping[LifecycleEvent, LifecycleStatus]] = MappingProxyType({
    LifecycleStatus.PENDING: MappingProxyType({
        LifecycleEvent.START_PROCESSING: LifecycleStatus.PROCESSING,
    }),
    LifecycleStatus.PROCESSING: MappingProxyType({
        LifecycleEvent.START_PROCESSING: LifecycleStatus.PROCESSING,
        LifecycleEvent.PROCESSING_SUCCEEDED: LifecycleStatus.PROCESSED,
        LifecycleEvent.PROCESSING_FAILED: LifecycleStatus.FAILED,
    }),
    LifecycleStatus.PROCESSED: MappingProxyType({
        LifecycleEvent.PROCESSING_SUCCEEDED: LifecycleStatus.PROCESSED,
    }),
    LifecycleStatus.FAILED: MappingProxyType({
        LifecycleEvent.RETRY_PROCESSING: LifecycleStatus.PROCESSING,
        LifecycleEvent.PROCESSING_FAILED: LifecycleStatus.FAILED,
    }),
})


def _normalize_event(event: LifecycleEvent | str) -> LifecycleEvent:
    if isinstance(event, LifecycleEvent):
        return event
    try:
        return LifecycleEvent(event)
    except (TypeError, ValueError) as error:
        raise ValueError(f"event must be a valid LifecycleEvent: {event!r}") from error


def transition_lifecycle(
    record: LifecycleRecord,
    event: LifecycleEvent | str,
    *,
    failure_reason: str | None = None,
) -> tuple[LifecycleRecord, LifecycleTransitionResult]:
    """Apply one pure lifecycle event and return a new record plus its result."""
    if not isinstance(record, LifecycleRecord):
        raise TypeError("record must be a LifecycleRecord")
    lifecycle_event = _normalize_event(event)
    target_status = _TRANSITIONS.get(record.status, {}).get(lifecycle_event)
    if target_status is None:
        allowed = ", ".join(event.value for event in _TRANSITIONS.get(record.status, {})) or "none"
        raise InvalidLifecycleTransition(
            f"Cannot apply event '{lifecycle_event.value}' while status is "
            f"'{record.status.value}'; allowed events: {allowed}"
        )

    normalized_reason = _normalize_optional_text(failure_reason, "failure_reason")
    if target_status is LifecycleStatus.FAILED:
        if normalized_reason is None:
            raise ValueError("failure_reason is required for PROCESSING_FAILED")
        # A repeated failure notification is idempotent and preserves the
        # existing failure detail rather than changing lifecycle state.
        next_reason = record.failure_reason or normalized_reason
    else:
        if normalized_reason is not None:
            raise ValueError("failure_reason is only allowed when the target status is 'failed'")
        next_reason = None

    changed = target_status is not record.status
    next_record = LifecycleRecord(
        document_id=record.document_id,
        version_id=record.version_id,
        status=target_status,
        failure_reason=next_reason,
    )
    result = LifecycleTransitionResult(
        document_id=record.document_id,
        version_id=record.version_id,
        previous_status=record.status,
        event=lifecycle_event,
        new_status=target_status,
        changed=changed,
        failure_reason=next_record.failure_reason,
    )
    return next_record, result


__all__ = [
    "InvalidLifecycleTransition",
    "LifecycleEvent",
    "LifecycleRecord",
    "LifecycleStatus",
    "LifecycleTransitionResult",
    "transition_lifecycle",
]

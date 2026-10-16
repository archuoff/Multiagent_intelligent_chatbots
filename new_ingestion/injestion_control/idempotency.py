"""Pure idempotency decisions for the new ingestion control layer."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Sequence


class IdempotencyDecision(StrEnum):
    """Action permitted for the current source/configuration pair."""

    SKIP = "skip"
    PROCESS = "process"
    RETRY = "retry"


class ProcessingStatus(StrEnum):
    """Statuses relevant to a previous processing attempt."""

    SUCCESS = "success"
    SUCCESSFUL = "successful"
    COMPLETED = "completed"
    FAILED = "failed"
    PENDING = "pending"
    RUNNING = "running"


@dataclass(frozen=True, slots=True)
class ProcessingRecord:
    """Immutable summary of one previously observed processing attempt."""

    source_file_hash: str
    processing_key: str
    status: ProcessingStatus | str
    document_id: str | None = None
    document_version_id: str | None = None

    def __post_init__(self) -> None:
        _require_non_empty(self.source_file_hash, "source_file_hash")
        _require_non_empty(self.processing_key, "processing_key")
        if isinstance(self.status, ProcessingStatus):
            status = self.status.value
        else:
            status = _require_non_empty(self.status, "status").lower()
        object.__setattr__(self, "source_file_hash", self.source_file_hash.strip().lower())
        object.__setattr__(self, "processing_key", self.processing_key.strip())
        object.__setattr__(self, "status", status)
        if self.document_id is not None:
            _require_non_empty(self.document_id, "document_id")
        if self.document_version_id is not None:
            _require_non_empty(self.document_version_id, "document_version_id")


@dataclass(frozen=True, slots=True)
class IdempotencyResult:
    """Deterministic idempotency decision and its matching evidence."""

    decision: IdempotencyDecision
    reason: str
    current_source_hash: str
    current_processing_key: str
    matched_previous_run: ProcessingRecord | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable decision record."""
        result = asdict(self)
        result["decision"] = self.decision.value
        if self.matched_previous_run is not None:
            result["matched_previous_run"]["status"] = str(self.matched_previous_run.status)
        return result

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)


def _require_non_empty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _matching_run(
    source_file_hash: str,
    processing_key: str,
    previous_runs: Sequence[ProcessingRecord],
) -> ProcessingRecord | None:
    """Return the most recent exact match, using sequence order as history order."""
    for record in reversed(previous_runs):
        if (
            record.source_file_hash == source_file_hash
            and record.processing_key == processing_key
        ):
            return record
    return None


def decide_idempotency(
    *,
    source_file_hash: str,
    processing_key: str,
    previous_runs: Sequence[ProcessingRecord],
    force_reingestion: bool = False,
) -> IdempotencyResult:
    """Decide whether the current attempt should skip, process, or retry.

    ``previous_runs`` is read only. Its order is treated as history order, so
    the last exact match is the authoritative previous attempt when multiple
    records have the same source/configuration pair.
    """
    current_hash = _require_non_empty(source_file_hash, "source_file_hash").strip().lower()
    current_key = _require_non_empty(processing_key, "processing_key").strip()
    if not isinstance(previous_runs, Sequence) or isinstance(previous_runs, (str, bytes)):
        raise TypeError("previous_runs must be a sequence of ProcessingRecord objects")
    for record in previous_runs:
        if not isinstance(record, ProcessingRecord):
            raise TypeError("previous_runs must contain only ProcessingRecord objects")

    matched = _matching_run(current_hash, current_key, previous_runs)
    if force_reingestion:
        reason = "force_reingestion=True overrides any matching successful run"
        return IdempotencyResult(IdempotencyDecision.PROCESS, reason, current_hash, current_key, matched)
    if matched is None:
        return IdempotencyResult(
            IdempotencyDecision.PROCESS,
            "no previous matching run exists",
            current_hash,
            current_key,
        )
    if str(matched.status).lower() in {"success", "successful", "completed"}:
        return IdempotencyResult(
            IdempotencyDecision.SKIP,
            "an identical source and processing configuration completed successfully",
            current_hash,
            current_key,
            matched,
        )
    if str(matched.status).lower() == "failed":
        return IdempotencyResult(
            IdempotencyDecision.RETRY,
            "the previous identical processing attempt failed",
            current_hash,
            current_key,
            matched,
        )
    return IdempotencyResult(
        IdempotencyDecision.PROCESS,
        f"the previous matching run has non-terminal status: {matched.status}",
        current_hash,
        current_key,
        matched,
    )


__all__ = [
    "IdempotencyDecision",
    "IdempotencyResult",
    "ProcessingRecord",
    "ProcessingStatus",
    "decide_idempotency",
]

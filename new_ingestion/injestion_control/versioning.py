"""Pure deterministic document-versioning decisions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Sequence

from .deduplication import _normalize_sha256


class VersioningAction(StrEnum):
    """Logical document-version action for the incoming source."""

    CREATE_DOCUMENT = "create_document"
    NO_CHANGE = "no_change"
    CREATE_VERSION = "create_version"


def _normalize_text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True, slots=True)
class DocumentVersionRecord:
    """Immutable summary of one existing logical document version."""

    logical_document_key: str
    document_id: str
    version_id: str
    version_number: int
    source_file_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "logical_document_key", _normalize_text(self.logical_document_key, "logical_document_key"))
        object.__setattr__(self, "document_id", _normalize_text(self.document_id, "document_id"))
        object.__setattr__(self, "version_id", _normalize_text(self.version_id, "version_id"))
        if isinstance(self.version_number, bool) or not isinstance(self.version_number, int) or self.version_number < 1:
            raise ValueError("version_number must be an integer greater than or equal to 1")
        object.__setattr__(self, "source_file_hash", _normalize_sha256(self.source_file_hash, "source_file_hash"))


@dataclass(frozen=True, slots=True)
class VersioningResult:
    """Deterministic versioning action and latest-version evidence."""

    action: VersioningAction
    logical_document_key: str
    document_id: str
    previous_version_id: str | None
    previous_version_number: int | None
    next_version_number: int
    current_source_file_hash: str
    reason: str


def decide_document_version(
    *,
    logical_document_key: str,
    document_id: str,
    current_source_file_hash: str,
    previous_versions: Sequence[DocumentVersionRecord],
    force_reingestion: bool = False,
) -> VersioningResult:
    """Decide create-document, no-change, or create-version without persistence."""
    logical_key = _normalize_text(logical_document_key, "logical_document_key")
    stable_document_id = _normalize_text(document_id, "document_id")
    current_hash = _normalize_sha256(current_source_file_hash, "current_source_file_hash")
    if isinstance(previous_versions, (str, bytes)) or not isinstance(previous_versions, Sequence):
        raise TypeError("previous_versions must be a sequence of DocumentVersionRecord objects")

    version_numbers: set[int] = set()
    version_ids: set[str] = set()
    for record in previous_versions:
        if not isinstance(record, DocumentVersionRecord):
            raise TypeError("previous_versions must contain only DocumentVersionRecord objects")
        if record.logical_document_key != logical_key:
            raise ValueError("all previous versions must use the same logical_document_key")
        if record.document_id != stable_document_id:
            raise ValueError("all previous versions must use the same document_id")
        if record.version_number in version_numbers:
            raise ValueError(f"duplicate version_number: {record.version_number}")
        if record.version_id in version_ids:
            raise ValueError(f"duplicate version_id: {record.version_id}")
        version_numbers.add(record.version_number)
        version_ids.add(record.version_id)

    if not previous_versions:
        return VersioningResult(
            action=VersioningAction.CREATE_DOCUMENT,
            logical_document_key=logical_key,
            document_id=stable_document_id,
            previous_version_id=None,
            previous_version_number=None,
            next_version_number=1,
            current_source_file_hash=current_hash,
            reason="no previous version exists for this logical document",
        )

    latest = max(previous_versions, key=lambda record: record.version_number)
    if not force_reingestion and current_hash == latest.source_file_hash:
        return VersioningResult(
            action=VersioningAction.NO_CHANGE,
            logical_document_key=logical_key,
            document_id=stable_document_id,
            previous_version_id=latest.version_id,
            previous_version_number=latest.version_number,
            next_version_number=latest.version_number,
            current_source_file_hash=current_hash,
            reason="current source hash matches the latest version",
        )

    reason = (
        "force_reingestion=True requires a new version"
        if force_reingestion
        else "current source hash differs from the latest version"
    )
    return VersioningResult(
        action=VersioningAction.CREATE_VERSION,
        logical_document_key=logical_key,
        document_id=stable_document_id,
        previous_version_id=latest.version_id,
        previous_version_number=latest.version_number,
        next_version_number=latest.version_number + 1,
        current_source_file_hash=current_hash,
        reason=reason,
    )


__all__ = [
    "DocumentVersionRecord",
    "VersioningAction",
    "VersioningResult",
    "decide_document_version",
]

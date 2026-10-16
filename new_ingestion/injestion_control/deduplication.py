"""Pure deterministic deduplication logic for ingestion control.

This module compares caller-provided hashes and identifiers only. It does not
read files, delete or copy assets, persist state, or call idempotency logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Sequence


class SourceDeduplicationStatus(StrEnum):
    """Whether a source-content hash has appeared before."""

    NEW = "new"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class SourceDeduplicationResult:
    status: SourceDeduplicationStatus
    source_file_hash: str
    matched_source_hash: str | None = None


def _normalize_non_empty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


_SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")


def _normalize_sha256(value: str, name: str) -> str:
    """Trim, lowercase, and validate one SHA-256 hexadecimal digest."""
    normalized = _normalize_non_empty(value, name).lower()
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise ValueError(f"{name} must be a 64-character hexadecimal SHA-256 hash")
    return normalized


def detect_duplicate_source(
    *,
    source_file_hash: str,
    known_source_hashes: Sequence[str],
) -> SourceDeduplicationResult:
    """Detect duplicate source content by normalized SHA/hash value only."""
    current_hash = _normalize_sha256(source_file_hash, "source_file_hash")
    if isinstance(known_source_hashes, (str, bytes)):
        raise TypeError("known_source_hashes must be a sequence of strings")
    normalized_known = [_normalize_sha256(value, "known_source_hash") for value in known_source_hashes]
    if current_hash in normalized_known:
        return SourceDeduplicationResult(
            status=SourceDeduplicationStatus.DUPLICATE,
            source_file_hash=current_hash,
            matched_source_hash=current_hash,
        )
    return SourceDeduplicationResult(
        status=SourceDeduplicationStatus.NEW,
        source_file_hash=current_hash,
    )


@dataclass(frozen=True, slots=True)
class AssetReference:
    asset_id: str
    sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "asset_id", _normalize_non_empty(self.asset_id, "asset_id"))
        object.__setattr__(self, "sha256", _normalize_sha256(self.sha256, "sha256"))


@dataclass(frozen=True, slots=True)
class AssetDeduplicationResult:
    canonical_asset_ids: tuple[str, ...]
    asset_to_canonical_id: dict[str, str]
    duplicate_asset_ids: tuple[str, ...]


def deduplicate_assets(
    assets: Sequence[AssetReference],
) -> AssetDeduplicationResult:
    """Map every asset to the first asset encountered with the same hash."""
    if isinstance(assets, (str, bytes)):
        raise TypeError("assets must be a sequence of AssetReference objects")
    canonical_asset_ids: list[str] = []
    asset_to_canonical_id: dict[str, str] = {}
    duplicate_asset_ids: list[str] = []
    canonical_by_hash: dict[str, str] = {}
    for asset in assets:
        if not isinstance(asset, AssetReference):
            raise TypeError("assets must contain only AssetReference objects")
        if asset.asset_id in asset_to_canonical_id:
            raise ValueError(f"asset_id values must be unique: {asset.asset_id}")
        canonical_id = canonical_by_hash.get(asset.sha256)
        if canonical_id is None:
            canonical_by_hash[asset.sha256] = asset.asset_id
            canonical_asset_ids.append(asset.asset_id)
            canonical_id = asset.asset_id
        else:
            duplicate_asset_ids.append(asset.asset_id)
        asset_to_canonical_id[asset.asset_id] = canonical_id
    return AssetDeduplicationResult(
        canonical_asset_ids=tuple(canonical_asset_ids),
        asset_to_canonical_id=asset_to_canonical_id,
        duplicate_asset_ids=tuple(duplicate_asset_ids),
    )


class ElementChangeType(StrEnum):
    """Comparison result for one logical element ID."""

    UNCHANGED = "unchanged"
    CHANGED = "changed"
    NEW = "new"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class ElementFingerprint:
    element_id: str
    content_hash: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "element_id", _normalize_non_empty(self.element_id, "element_id"))
        object.__setattr__(self, "content_hash", _normalize_sha256(self.content_hash, "content_hash"))


@dataclass(frozen=True, slots=True)
class ElementChange:
    element_id: str
    change_type: ElementChangeType
    previous_content_hash: str | None = None
    current_content_hash: str | None = None


def compare_element_fingerprints(
    *,
    previous: Sequence[ElementFingerprint],
    current: Sequence[ElementFingerprint],
) -> tuple[ElementChange, ...]:
    """Compare elements by logical ID and content hash, preserving history order."""
    if isinstance(previous, (str, bytes)) or isinstance(current, (str, bytes)):
        raise TypeError("previous and current must be sequences of ElementFingerprint objects")
    previous_map: dict[str, ElementFingerprint] = {}
    current_map: dict[str, ElementFingerprint] = {}
    for fingerprint in previous:
        if not isinstance(fingerprint, ElementFingerprint):
            raise TypeError("previous must contain only ElementFingerprint objects")
        if fingerprint.element_id in previous_map:
            raise ValueError(f"duplicate previous element_id: {fingerprint.element_id}")
        previous_map[fingerprint.element_id] = fingerprint
    for fingerprint in current:
        if not isinstance(fingerprint, ElementFingerprint):
            raise TypeError("current must contain only ElementFingerprint objects")
        if fingerprint.element_id in current_map:
            raise ValueError(f"duplicate current element_id: {fingerprint.element_id}")
        current_map[fingerprint.element_id] = fingerprint

    changes: list[ElementChange] = []
    for fingerprint in current:
        old = previous_map.get(fingerprint.element_id)
        if old is None:
            change_type = ElementChangeType.NEW
            previous_hash = None
        elif old.content_hash == fingerprint.content_hash:
            change_type = ElementChangeType.UNCHANGED
            previous_hash = old.content_hash
        else:
            change_type = ElementChangeType.CHANGED
            previous_hash = old.content_hash
        changes.append(ElementChange(
            element_id=fingerprint.element_id,
            change_type=change_type,
            previous_content_hash=previous_hash,
            current_content_hash=fingerprint.content_hash,
        ))
    for fingerprint in previous:
        if fingerprint.element_id not in current_map:
            changes.append(ElementChange(
                element_id=fingerprint.element_id,
                change_type=ElementChangeType.REMOVED,
                previous_content_hash=fingerprint.content_hash,
            ))
    return tuple(changes)


__all__ = [
    "AssetDeduplicationResult",
    "AssetReference",
    "ElementChange",
    "ElementChangeType",
    "ElementFingerprint",
    "SourceDeduplicationResult",
    "SourceDeduplicationStatus",
    "compare_element_fingerprints",
    "deduplicate_assets",
    "detect_duplicate_source",
]

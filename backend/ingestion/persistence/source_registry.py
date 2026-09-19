"""Stable source identity and version selection for local ingestion storage.

The registry examines persisted manifests before parsing. It reuses an existing
artifact for an unchanged source hash and assigns the next version for changed
content from the same logical local source path.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import re

from backend.ingestion.models import SourceType
from backend.ingestion.persistence.canonical_store import CanonicalArtifactManifest


@dataclass(frozen=True, slots=True)
class SourceRegistration:
    """Identifies the canonical document version selected for one source file."""

    source_key: str
    document_id: str
    version: str
    is_unchanged: bool


class SourceRegistry:
    """Resolves local source files to stable document identities and versions."""

    _SAFE_PATH_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")

    # This function configures the manifest location used to resolve prior source versions.
    def __init__(self, storage_root: str | Path) -> None:
        self._storage_root = Path(storage_root)

    # This function reuses unchanged sources or assigns the next immutable document version.
    def resolve(
        self,
        *,
        agent_id: str,
        source_type: SourceType,
        file_path: str | Path,
        source_hash: str,
        processing_key: str = "",
    ) -> SourceRegistration:
        source_key = self.source_key(agent_id=agent_id, source_type=source_type, file_path=file_path)
        matching_manifests = [
            manifest
            for manifest in self._manifests_for_agent(agent_id)
            if manifest.source_key == source_key
        ]
        for manifest in sorted(matching_manifests, key=lambda item: self._version_number(item.version), reverse=True)[:1]:
            if manifest.source_hash == source_hash and manifest.processing_key == processing_key and manifest.status == "validated":
                return SourceRegistration(source_key, manifest.document_id, manifest.version, True)

        if not matching_manifests:
            document_id = f"doc-{source_key[:24]}"
            return SourceRegistration(source_key, document_id, "v1", False)

        latest = max(matching_manifests, key=lambda manifest: self._version_number(manifest.version))
        next_version = f"v{self._version_number(latest.version) + 1}"
        return SourceRegistration(source_key, latest.document_id, next_version, False)

    # This function derives a deterministic local source identity from ownership, type, and path.
    def source_key(self, *, agent_id: str, source_type: SourceType, file_path: str | Path) -> str:
        normalized_path = Path(file_path).resolve().as_posix().casefold()
        value = f"{agent_id.casefold()}|{source_type.value}|{normalized_path}"
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    # This function loads valid manifests belonging to one agent from local storage.
    def _manifests_for_agent(self, agent_id: str) -> list[CanonicalArtifactManifest]:
        if not self._SAFE_PATH_COMPONENT.fullmatch(agent_id) or agent_id in {".", ".."}:
            raise ValueError(f"agent_id contains unsupported path characters: {agent_id!r}")
        manifest_directory = self._storage_root / "manifests" / agent_id
        if not manifest_directory.is_dir():
            return []
        manifests: list[CanonicalArtifactManifest] = []
        for path in manifest_directory.rglob("*.json"):
            manifests.append(CanonicalArtifactManifest.model_validate_json(path.read_text(encoding="utf-8")))
        return manifests

    # This function returns the numeric portion of a vN version string for ordering.
    def _version_number(self, version: str) -> int:
        if not version.startswith("v") or not version[1:].isdigit():
            raise ValueError(f"Unsupported document version format: {version!r}")
        return int(version[1:])

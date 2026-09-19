"""Immutable local storage for parsed Master JSON documents.

This module is the first persistence boundary after parsing and validation.
It writes one canonical artifact and one manifest for each document version;
it never silently replaces an existing version. The local folder layout mirrors
the object-storage layout intended for production deployment.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from pydantic import BaseModel

from backend.ingestion.models import CanonicalDocument


class CanonicalArtifactManifest(BaseModel):
    """Records where one immutable canonical document version is stored."""

    schema_version: str = "1.0"
    document_id: str
    agent_id: str
    version: str
    source_hash: str
    source_type: str
    source_name: str
    source_path: str = ""
    source_key: str = "unregistered"
    processing_key: str = ""
    parser_name: str
    parser_version: str
    status: str
    canonical_artifact_path: str
    artifact_hash: str
    created_at: datetime


class StoredCanonicalArtifact(BaseModel):
    """Returns the durable locations created or reused by one persist operation."""

    master_json_path: Path
    manifest_path: Path
    manifest: CanonicalArtifactManifest
    reused_existing_version: bool = False


class CanonicalArtifactStore:
    """Writes canonical JSON artifacts under the local storage root atomically."""

    _SAFE_PATH_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")

    # This function configures the local storage root without coupling callers to folders.
    def __init__(self, storage_root: str | Path | None = None) -> None:
        self._storage_root = Path(storage_root) if storage_root else Path(__file__).resolve().parents[3] / "storage"

    @property
    def storage_root(self) -> Path:
        """Returns the configured storage root for coordinated registry access."""
        return self._storage_root

    # This function persists one immutable Master JSON artifact and its matching manifest.
    def persist(
        self,
        document: CanonicalDocument,
        *,
        status: str = "validated",
        source_key: str = "unregistered",
        processing_key: str = "",
    ) -> StoredCanonicalArtifact:
        agent_id = self._safe_component(document.agent_id, "agent_id")
        document_id = self._safe_component(document.document_id, "document_id")
        version = self._safe_component(document.version, "version")
        master_json_path = self._storage_root / "canonical" / agent_id / document_id / version / "master.json"
        manifest_path = self._storage_root / "manifests" / agent_id / document_id / f"{version}.json"
        serialized_document = document.model_dump_json(indent=2).encode("utf-8")

        if master_json_path.exists():
            if master_json_path.read_bytes() != serialized_document:
                raise FileExistsError(
                    "Canonical artifact already exists with different content; create a new document version."
                )
            if not manifest_path.is_file():
                raise RuntimeError("Canonical artifact exists but its manifest is missing.")
            manifest = CanonicalArtifactManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
            return StoredCanonicalArtifact(
                master_json_path=master_json_path,
                manifest_path=manifest_path,
                manifest=manifest,
                reused_existing_version=True,
            )

        artifact_hash = hashlib.sha256(serialized_document).hexdigest()
        manifest = CanonicalArtifactManifest(
            document_id=document.document_id,
            agent_id=document.agent_id,
            version=document.version,
            source_hash=document.source_hash,
            source_type=document.source_type.value,
            source_name=document.source_name,
            source_path=document.source_path,
            source_key=source_key,
            processing_key=processing_key,
            parser_name=document.parser_info.parser_name,
            parser_version=document.parser_info.parser_version,
            status=status,
            canonical_artifact_path=self._relative_path(master_json_path),
            artifact_hash=artifact_hash,
            created_at=datetime.now(timezone.utc),
        )
        self._atomic_write(master_json_path, serialized_document)
        try:
            self._atomic_write(manifest_path, manifest.model_dump_json(indent=2).encode("utf-8"))
        except Exception:
            # Do not leave an artifact that cannot be discovered safely through a manifest.
            master_json_path.unlink(missing_ok=True)
            raise
        return StoredCanonicalArtifact(
            master_json_path=master_json_path,
            manifest_path=manifest_path,
            manifest=manifest,
        )

    # This function loads a previously persisted canonical document for idempotent ingestion reuse.
    def load(self, *, agent_id: str, document_id: str, version: str) -> StoredCanonicalArtifact:
        agent_component = self._safe_component(agent_id, "agent_id")
        document_component = self._safe_component(document_id, "document_id")
        version_component = self._safe_component(version, "version")
        master_json_path = self._storage_root / "canonical" / agent_component / document_component / version_component / "master.json"
        manifest_path = self._storage_root / "manifests" / agent_component / document_component / f"{version_component}.json"
        if not master_json_path.is_file() or not manifest_path.is_file():
            raise FileNotFoundError("Registered canonical artifact or manifest is missing.")
        manifest = CanonicalArtifactManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        if hashlib.sha256(master_json_path.read_bytes()).hexdigest() != manifest.artifact_hash:
            raise ValueError("Canonical artifact checksum mismatch.")
        return StoredCanonicalArtifact(
            master_json_path=master_json_path,
            manifest_path=manifest_path,
            manifest=CanonicalArtifactManifest.model_validate_json(manifest_path.read_text(encoding="utf-8")),
            reused_existing_version=True,
        )

    # This function validates identifiers before they become filesystem path components.
    def _safe_component(self, value: str, field_name: str) -> str:
        if not self._SAFE_PATH_COMPONENT.fullmatch(value) or value in {".", ".."}:
            raise ValueError(f"{field_name} contains unsupported path characters: {value!r}")
        return value

    # This function produces a platform-independent artifact location for manifests.
    def _relative_path(self, path: Path) -> str:
        return path.relative_to(self._storage_root).as_posix()

    # This function atomically writes a file so partial JSON is never exposed to readers.
    def _atomic_write(self, target_path: Path, content: bytes) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target_path.name}.", dir=target_path.parent)
        try:
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.link(temporary_name, target_path)
            Path(temporary_name).unlink()
        except Exception:
            Path(temporary_name).unlink(missing_ok=True)
            raise

"""Reads and writes one JSON manifest per document version.

A manifest is the only durable record of a version's processing outcome. It
is what lets a later run rebuild the ``previous_versions`` history that
``new_ingestion.injestion_control.versioning.decide_document_version`` needs,
without that module (or this one) reading canonical documents themselves.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from new_ingestion.injestion_control.lifecycle import LifecycleStatus
from new_ingestion.injestion_control.versioning import DocumentVersionRecord

MANIFEST_FILENAME = "manifest.json"
INDEX_FILENAME = "index.json"


@dataclass(frozen=True, slots=True)
class DocumentManifest:
    """Durable record of one ingestion attempt for one document version."""

    document_id: str
    version_id: str
    version_number: int
    logical_document_key: str
    source_file_name: str
    source_relative_path: str
    source_file_hash: str
    status: LifecycleStatus
    failure_reason: str | None = None
    created_at: str = ""

    def __post_init__(self) -> None:
        if not self.created_at:
            object.__setattr__(self, "created_at", datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @staticmethod
    def from_dict(data: dict[str, object]) -> "DocumentManifest":
        return DocumentManifest(
            document_id=str(data["document_id"]),
            version_id=str(data["version_id"]),
            version_number=int(data["version_number"]),
            logical_document_key=str(data["logical_document_key"]),
            source_file_name=str(data["source_file_name"]),
            source_relative_path=str(data["source_relative_path"]),
            source_file_hash=str(data["source_file_hash"]),
            status=LifecycleStatus(data["status"]),
            failure_reason=data.get("failure_reason"),
            created_at=str(data["created_at"]),
        )


def _version_dir(agent_id: str, document_id: str, version_id: str, output_root: Path) -> Path:
    return Path(output_root) / agent_id / document_id / version_id


def write_manifest(manifest: DocumentManifest, *, agent_id: str, output_root: Path) -> Path:
    """Writes a version's manifest, creating its version directory if needed."""
    directory = _version_dir(agent_id, manifest.document_id, manifest.version_id, output_root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / MANIFEST_FILENAME
    path.write_text(json.dumps(manifest.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def read_manifest(path: Path) -> DocumentManifest:
    return DocumentManifest.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def list_processed_versions(agent_id: str, document_id: str, output_root: Path) -> list[DocumentVersionRecord]:
    """Returns version history usable by ``decide_document_version``.

    Only manifests whose last recorded status is PROCESSED count as "seen":
    a version whose last attempt FAILED is deliberately left out, so the same
    source hash is retried on the next run instead of being judged NO_CHANGE.
    """
    document_dir = Path(output_root) / agent_id / document_id
    if not document_dir.is_dir():
        return []
    records: list[DocumentVersionRecord] = []
    for version_dir in sorted(document_dir.iterdir()):
        manifest_path = version_dir / MANIFEST_FILENAME
        if not manifest_path.is_file():
            continue
        manifest = read_manifest(manifest_path)
        if manifest.status is not LifecycleStatus.PROCESSED:
            continue
        records.append(DocumentVersionRecord(
            logical_document_key=manifest.logical_document_key,
            document_id=manifest.document_id,
            version_id=manifest.version_id,
            version_number=manifest.version_number,
            source_file_hash=manifest.source_file_hash,
        ))
    return records


def write_agent_index(agent_id: str, output_root: Path) -> Path:
    """Writes output_root/<agent_id>/index.json: document_id -> readable source info.

    Folder names under an agent are document_ids (stable, filesystem-safe
    UUIDs), not the original filename, so this index exists purely so the
    real filename is visible without opening a manifest.json. Rebuilt from
    every document's latest PROCESSED manifest, so it stays correct even for
    documents ingested before this index existed.
    """
    agent_dir = Path(output_root) / agent_id
    index: dict[str, dict[str, object]] = {}
    if agent_dir.is_dir():
        for document_dir in sorted(p for p in agent_dir.iterdir() if p.is_dir()):
            latest: DocumentManifest | None = None
            for version_dir in sorted(document_dir.iterdir()):
                manifest_path = version_dir / MANIFEST_FILENAME
                if not manifest_path.is_file():
                    continue
                manifest = read_manifest(manifest_path)
                if manifest.status is not LifecycleStatus.PROCESSED:
                    continue
                if latest is None or manifest.version_number > latest.version_number:
                    latest = manifest
            if latest is not None:
                index[document_dir.name] = {
                    "source_file_name": latest.source_file_name,
                    "source_relative_path": latest.source_relative_path,
                    "latest_version": latest.version_id,
                    "latest_version_number": latest.version_number,
                }
    path = agent_dir / INDEX_FILENAME
    agent_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(index, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


__all__ = [
    "DocumentManifest", "list_processed_versions", "read_manifest",
    "write_agent_index", "write_manifest",
]

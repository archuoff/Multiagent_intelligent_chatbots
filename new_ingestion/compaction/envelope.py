"""Shared output shape for compacted canonical documents, common to every source type."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from new_ingestion.schema.schema_define import Asset, CanonicalDocument


@dataclass(slots=True)
class ContentUnit:
    """One embeddable unit: text to embed kept separate from citation/filter metadata."""

    id: str
    source_type: str
    container: str
    embedding_text: str
    images: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_type": self.source_type,
            "container": self.container,
            "embedding_text": self.embedding_text,
            "images": self.images,
            "metadata": self.metadata,
        }


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


def build_document_section(document: CanonicalDocument) -> dict[str, Any]:
    """Keeps real document identity/authorship fields and extraction outcome, drops internals."""
    meta = document.document
    section = _drop_none({
        "document_id": meta.document_id,
        "version_id": meta.version_id,
        "logical_document_key": meta.logical_document_key,
        "domain": meta.domain,
        "document_kind": meta.document_kind,
        "source_file_name": meta.source_file_name,
        "source_type": meta.source_type.value,
        "sha256": meta.sha256,
        "title": meta.title,
        "author": meta.author,
        "language": meta.language,
        "revision": meta.revision,
        "vehicle_program": meta.vehicle_program,
        "vehicle_system": meta.vehicle_system,
        "subsystem": meta.subsystem,
    })
    extraction = _drop_none({
        "status": meta.extraction.status.value,
        "warnings": meta.extraction.warnings or None,
        "errors": meta.extraction.errors or None,
    })
    if extraction:
        section["extraction"] = extraction
    return section


def build_assets_section(document: CanonicalDocument) -> dict[str, dict[str, Any]]:
    """Keeps only what's needed to locate/display an asset, drops OOXML/placement internals."""
    assets: dict[str, dict[str, Any]] = {}
    for asset_id, asset in document.assets.items():
        assets[asset_id] = _drop_none(_asset_fields(asset))
    return assets


def _asset_fields(asset: Asset) -> dict[str, Any]:
    return {
        "type": asset.asset_type.value,
        "mime_type": asset.mime_type,
        "path": asset.relative_uri,
        "sha256": asset.sha256,
        "file_size": asset.file_size,
    }


def build_diagnostics_section(document: CanonicalDocument) -> dict[str, Any]:
    return {
        "warnings": list(document.diagnostics.warnings),
        "errors": list(document.diagnostics.errors),
    }


def build_envelope(document: CanonicalDocument, content: list[ContentUnit]) -> dict[str, Any]:
    return {
        "document": build_document_section(document),
        "content": [unit.to_dict() for unit in content],
        "assets": build_assets_section(document),
        "diagnostics": build_diagnostics_section(document),
    }

"""Persists validated canonical documents and their version manifests to disk."""

from __future__ import annotations

from new_ingestion.persistence.canonical_store import persist_canonical
from new_ingestion.persistence.manifest import (
    DocumentManifest, list_processed_versions, read_manifest, write_manifest,
)

__all__ = [
    "DocumentManifest", "list_processed_versions", "persist_canonical",
    "read_manifest", "write_manifest",
]

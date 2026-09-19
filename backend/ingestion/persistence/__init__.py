"""Persistence boundary for locally stored canonical ingestion artifacts.

``canonical_store.py`` writes immutable Master JSON files and their manifests.
``source_registry.py`` selects stable IDs and versions from those manifests,
including the processing fingerprint supplied by the ingestion service.
It receives validated ``CanonicalDocument`` objects from ``service.py`` and
writes to the workspace ``storage/`` folder. Future cloud storage adapters can
implement the same boundary without changing parsers or chunking logic.
"""

from backend.ingestion.persistence.canonical_store import CanonicalArtifactManifest
from backend.ingestion.persistence.canonical_store import CanonicalArtifactStore
from backend.ingestion.persistence.canonical_store import StoredCanonicalArtifact
from backend.ingestion.persistence.source_registry import SourceRegistration
from backend.ingestion.persistence.source_registry import SourceRegistry

__all__ = [
    "CanonicalArtifactManifest",
    "CanonicalArtifactStore",
    "StoredCanonicalArtifact",
    "SourceRegistration",
    "SourceRegistry",
]

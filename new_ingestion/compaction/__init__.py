"""Compacts a validated CanonicalDocument into a chunk/embedding-ready JSON."""

from __future__ import annotations

from new_ingestion.compaction.compactor import compact_document, load_and_compact

__all__ = ["compact_document", "load_and_compact"]

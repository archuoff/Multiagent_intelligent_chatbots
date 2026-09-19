"""Raw extraction contracts shared by Docling and local fallback adapters.

These structures preserve source-level content before parser modules transform
it into the canonical Master JSON node taxonomy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Docling retains its structured payload; local adapters may still supply matrices.
ExtractedTable = dict[str, Any] | list[list[str]]


@dataclass(slots=True)
class ExtractedPage:
    """Represents source content tied to one PDF page or presentation slide."""

    number: int
    text: str = ""
    text_blocks: list[dict[str, Any]] = field(default_factory=list)
    tables: list[ExtractedTable] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)
    notes: str | None = None
    reconciliation: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class ExtractionResult:
    """Represents raw extraction output without imposing canonical-node semantics."""

    extractor_name: str
    extraction_mode: str
    pages: list[ExtractedPage] = field(default_factory=list)
    markdown: str = ""
    exported: dict[str, Any] = field(default_factory=dict)
    blocks: list[dict[str, Any]] = field(default_factory=list)
    tables: list[ExtractedTable] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    requires_review: bool = False
    processing_details: dict[str, Any] = field(default_factory=dict)

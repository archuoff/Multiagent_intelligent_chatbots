"""Lightweight PDF inspection used to select a safe extraction policy.

This module detects whether a PDF has an embedded text layer before Docling is
called, preventing unnecessary OCR initialization and model downloads.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader

from backend.ingestion.utils import normalize_text


@dataclass(frozen=True, slots=True)
class PdfInspection:
    """Records the embedded-text evidence used for PDF extraction routing."""

    page_count: int
    text_page_count: int
    total_text_characters: int

    @property
    def has_usable_embedded_text(self) -> bool:
        """Returns whether the PDF has enough native text for the no-OCR policy."""
        return self.text_page_count > 0 and self.total_text_characters >= 100


class PdfInspector:
    """Inspects PDF embedded text without invoking OCR or layout models."""

    # This function measures native PDF text coverage for the extraction-policy decision.
    def inspect(self, source_path: str | Path) -> PdfInspection:
        reader = PdfReader(str(source_path))
        page_texts = [normalize_text(page.extract_text() or "") or "" for page in reader.pages]
        return PdfInspection(
            page_count=len(page_texts),
            text_page_count=sum(1 for text in page_texts if text),
            total_text_characters=sum(len(text) for text in page_texts),
        )

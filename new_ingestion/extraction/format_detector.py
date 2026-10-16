"""Detect supported document formats before extraction.

This module only identifies the input format. It does not select or execute an
extractor, parse the document, or build canonical JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from zipfile import BadZipFile, ZipFile


class DocumentFormat(StrEnum):
    """Document formats currently supported by the new ingestion system."""

    PDF = "pdf"
    DOCX = "docx"
    PPTX = "pptx"
    XLSX = "xlsx"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class FormatDetectionResult:
    """The detected format and the evidence used to identify it."""

    format: DocumentFormat
    source_path: Path
    detected_by: str
    confidence: float
    reason: str


_EXTENSION_FORMATS = {
    ".pdf": DocumentFormat.PDF,
    ".docx": DocumentFormat.DOCX,
    ".pptx": DocumentFormat.PPTX,
    ".xlsx": DocumentFormat.XLSX,
}

_OOXML_CONTENT_TYPES = {
    "wordprocessingml.document.main+xml": DocumentFormat.DOCX,
    "presentationml.presentation.main+xml": DocumentFormat.PPTX,
    "spreadsheetml.sheet.main+xml": DocumentFormat.XLSX,
}


def _detect_pdf(path: Path) -> FormatDetectionResult | None:
    """Detect a PDF using its file signature."""
    with path.open("rb") as stream:
        signature = stream.read(5)
    if signature == b"%PDF-":
        return FormatDetectionResult(
            format=DocumentFormat.PDF,
            source_path=path,
            detected_by="file_signature",
            confidence=1.0,
            reason="The file starts with the PDF signature '%PDF-'.",
        )
    return None


def _detect_ooxml(path: Path) -> FormatDetectionResult | None:
    """Detect DOCX, PPTX, or XLSX from OOXML package content types."""
    try:
        with ZipFile(path) as archive:
            content_types = archive.read("[Content_Types].xml").decode("utf-8", errors="ignore")
    except (BadZipFile, KeyError, OSError):
        return None

    for content_type, document_format in _OOXML_CONTENT_TYPES.items():
        if content_type in content_types:
            return FormatDetectionResult(
                format=document_format,
                source_path=path,
                detected_by="ooxml_content_types",
                confidence=1.0,
                reason=f"The OOXML package declares the {document_format.value.upper()} main document type.",
            )
    return None


def detect_format(source_path: str | Path) -> FormatDetectionResult:
    """Detect the supported format of one existing file.

    Content signatures are checked before the extension so that a renamed file
    is not silently classified incorrectly. The extension is used as a safe
    fallback for supported formats whose container cannot be inspected.
    """
    path = Path(source_path)
    if not path.exists():
        raise FileNotFoundError(path)
    if not path.is_file():
        raise ValueError(f"Source path is not a file: {path}")

    pdf_result = _detect_pdf(path)
    if pdf_result is not None:
        return pdf_result

    ooxml_result = _detect_ooxml(path)
    if ooxml_result is not None:
        return ooxml_result

    extension = path.suffix.lower()
    extension_format = _EXTENSION_FORMATS.get(extension)
    if extension_format is not None:
        return FormatDetectionResult(
            format=extension_format,
            source_path=path,
            detected_by="file_extension",
            confidence=0.7,
            reason=f"The file has the supported '{extension}' extension, but its container signature was unavailable.",
        )

    return FormatDetectionResult(
        format=DocumentFormat.UNKNOWN,
        source_path=path,
        detected_by="none",
        confidence=0.0,
        reason="The file is not one of the currently supported formats.",
    )

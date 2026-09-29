"""Local extraction fallbacks for sources that Docling cannot process.

These adapters intentionally return raw extraction contracts. Source parsers
remain responsible for canonical-node construction and provenance assignment.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from docx import Document as DocxDocument
from docx.document import Document as DocxDocumentType
from docx.oxml.ns import qn
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pypdf import PdfReader

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - exercised when PyMuPDF is not installed.
    fitz = None

try:
    import pdfplumber
except ImportError:  # pragma: no cover - exercised when pdfplumber is not installed.
    pdfplumber = None

from backend.ingestion.extraction.contracts import ExtractedPage
from backend.ingestion.extraction.contracts import ExtractionResult
from backend.ingestion.utils import normalize_text


class PdfFallbackExtractor:
    """Extracts PDF text, coordinates, image metadata, and tables without Docling."""

    # This function uses PyMuPDF first and retains pypdf as a minimal dependency fallback.
    def extract(self, source_path: str | Path) -> ExtractionResult:
        if fitz is not None:
            try:
                return self._extract_with_pymupdf(source_path)
            except Exception as error:
                result = self._extract_with_pypdf(source_path)
                result.warnings.append(f"PyMuPDF failed: {type(error).__name__}; pypdf recovery used.")
                return result
        return self._extract_with_pypdf(source_path)

    # This function extracts ordered text blocks and image metadata with PyMuPDF.
    def _extract_with_pymupdf(self, source_path: str | Path) -> ExtractionResult:
        pages: list[ExtractedPage] = []
        warnings: list[str] = []
        with fitz.open(str(source_path)) as document:
            for page_number, page in enumerate(document, start=1):
                raw_blocks = page.get_text("blocks", sort=True)
                text_blocks = [
                    {
                        "text": normalize_text(block[4]) or "",
                        "bbox": {"x0": block[0], "y0": block[1], "x1": block[2], "y1": block[3]},
                        "block_number": block[5],
                    }
                    for block in raw_blocks
                    if len(block) >= 7 and block[6] == 0 and normalize_text(block[4])
                ]
                text = "\n".join(block["text"] for block in text_blocks)
                images = [
                    {"xref": image[0], "width": image[2], "height": image[3], "extension": image[7]}
                    for image in page.get_images(full=True)
                ]
                try:
                    tables = self._extract_page_tables(source_path, page_number)
                except Exception as error:
                    tables = []
                    warnings.append(f"Table extraction failed on page {page_number}: {type(error).__name__}")
                if not text:
                    warnings.append(f"Low-text or image-only PDF page detected: {page_number}")
                pages.append(ExtractedPage(number=page_number, text=text, text_blocks=text_blocks, tables=tables, images=images))
        return ExtractionResult(extractor_name="pymupdf", extraction_mode="fallback_library", pages=pages, warnings=warnings)

    # This function uses pdfplumber table extraction only when the optional package is installed.
    def _extract_page_tables(self, source_path: str | Path, page_number: int) -> list[list[list[str]]]:
        if pdfplumber is None:
            return []
        with pdfplumber.open(str(source_path)) as document:
            page = document.pages[page_number - 1]
            return [
                [[normalize_text(str(cell or "")) or "" for cell in row] for row in table]
                for table in page.extract_tables()
                if table
            ]

    # This function extracts embedded PDF text when richer local extractors are unavailable.
    def _extract_with_pypdf(self, source_path: str | Path) -> ExtractionResult:
        reader = PdfReader(str(source_path))
        pages: list[ExtractedPage] = []
        warnings: list[str] = []
        for page_number, page in enumerate(reader.pages, start=1):
            text = normalize_text(page.extract_text() or "") or ""
            if not text:
                warnings.append(f"Low-text or image-only PDF page detected: {page_number}")
            pages.append(ExtractedPage(number=page_number, text=text))
        return ExtractionResult(
            extractor_name="pypdf",
            extraction_mode="fallback_library",
            pages=pages,
            warnings=warnings,
        )


class DocxFallbackExtractor:
    """Extracts DOCX structure locally with python-docx/OOXML."""

    # This function preserves document order, paragraph styles, table matrices, and image references.
    def extract(self, source_path: str | Path) -> ExtractionResult:
        document = DocxDocument(str(source_path))
        blocks: list[dict[str, Any]] = []
        tables: list[list[list[str]]] = []
        images: list[dict[str, Any]] = []
        paragraph_index = 0
        table_index = 0
        image_index = 0
        for item in self._iter_body_items(document):
            if isinstance(item, DocxParagraph):
                paragraph_index += 1
                text = normalize_text(item.text) or ""
                image_refs = self._paragraph_image_refs(item)
                if text:
                    blocks.append({
                        "block_type": "paragraph",
                        "text": text,
                        "style_name": item.style.name if item.style else "",
                        "paragraph_index": paragraph_index,
                        "extractor_role": "native_structure",
                    })
                for image_ref in image_refs:
                    image_index += 1
                    image = {
                        "block_type": "image",
                        "image_index": image_index,
                        "paragraph_index": paragraph_index,
                        "relationship_id": image_ref.get("relationship_id"),
                        "media_name": image_ref.get("media_name"),
                        "nearby_text": text,
                        "extractor_role": "native_structure",
                        "image_storage_status": "pending",
                    }
                    blocks.append(image)
                    images.append(image)
            elif isinstance(item, DocxTable):
                table_index += 1
                rows = [[normalize_text(cell.text) or "" for cell in row.cells] for row in item.rows]
                table_block = {
                    "block_type": "table",
                    "table_index": table_index,
                    "rows": rows,
                    "style_name": item.style.name if item.style else "",
                    "extractor_role": "native_structure",
                }
                blocks.append(table_block)
                tables.append(rows)
        warnings = [] if any(block.get("text") for block in blocks) or tables or images else ["No DOCX content detected"]
        return ExtractionResult(
            extractor_name="python-docx",
            extraction_mode="native_structure",
            blocks=blocks,
            tables=tables,
            images=images,
            warnings=warnings,
            processing_details={
                "paragraph_count": paragraph_index,
                "table_count": table_index,
                "image_count": image_index,
                "native_extractor": "python-docx",
            },
        )

    def _iter_body_items(self, document: DocxDocumentType):
        """Yields paragraphs and tables in DOCX body order."""
        body = document.element.body
        for child in body.iterchildren():
            if child.tag == qn("w:p"):
                yield DocxParagraph(child, document)
            elif child.tag == qn("w:tbl"):
                yield DocxTable(child, document)

    def _paragraph_image_refs(self, paragraph: DocxParagraph) -> list[dict[str, str | None]]:
        """Returns embedded image relationship ids referenced by one paragraph."""
        refs: list[dict[str, str | None]] = []
        namespace = qn("r:embed")
        for element in paragraph._element.iter():
            relationship_id = element.attrib.get(namespace)
            if not relationship_id:
                continue
            media_name = None
            try:
                part = paragraph.part.related_parts.get(relationship_id)
                media_name = str(getattr(part, "partname", "") or "").lstrip("/") or None
            except Exception:
                media_name = None
            refs.append({"relationship_id": relationship_id, "media_name": media_name})
        return refs


class PowerPointFallbackExtractor:
    """Extracts PPTX slide text, tables, notes, and image metadata with python-pptx."""

    # This function preserves slide-level raw content without applying vision analysis.
    def extract(self, source_path: str | Path) -> ExtractionResult:
        presentation = Presentation(str(source_path))
        pages: list[ExtractedPage] = []
        warnings: list[str] = []
        for slide_number, slide in enumerate(presentation.slides, start=1):
            blocks: list[dict[str, Any]] = []
            tables: list[list[list[str]]] = []
            images: list[dict[str, Any]] = []
            for shape_index, shape in enumerate(self._walk_shapes(slide.shapes), start=1):
                shape_metadata = self._shape_metadata(shape, shape_index)
                if getattr(shape, "has_text_frame", False):
                    for paragraph_index, paragraph in enumerate(shape.text_frame.paragraphs, start=1):
                        text = normalize_text(paragraph.text)
                        if text:
                            blocks.append(
                                {
                                    **shape_metadata,
                                    "text": text,
                                    "level": paragraph.level,
                                    "paragraph_index": paragraph_index,
                                    "is_title": bool(
                                        getattr(shape, "is_placeholder", False)
                                        and shape.placeholder_format.idx == 0
                                    ),
                                    "extractor_role": "native_structure",
                                }
                            )
                elif getattr(shape, "has_table", False):
                    tables.append(
                        {
                            "rows": [[normalize_text(cell.text) or "" for cell in row.cells] for row in shape.table.rows],
                            "metadata": {**shape_metadata, "extractor_role": "native_structure"},
                        }
                    )
                elif getattr(shape, "has_chart", False):
                    images.append({**shape_metadata, **self._chart_metadata(shape), "image_type": "chart",
                                   "needs_vision_description": True, "vision_reason": "Native PPTX chart may require visual description for retrieval."})
                elif shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    image_metadata = self._picture_metadata(shape)
                    images.append({**shape_metadata, **image_metadata, "image_type": "picture"})
            note_text = self._extract_notes(slide)
            if not blocks:
                warnings.append(f"Low-text or visual-only slide detected: {slide_number}")
            pages.append(
                ExtractedPage(
                    number=slide_number,
                    text="\n".join(block["text"] for block in blocks),
                    text_blocks=blocks,
                    tables=tables,
                    images=images,
                    notes=note_text,
                )
            )
        return ExtractionResult(
            extractor_name="python-pptx",
            extraction_mode="fallback_library",
            pages=pages,
            warnings=warnings,
        )

    def _walk_shapes(self, shapes: Any):
        """Includes text and tables nested inside grouped presentation shapes."""
        for shape in shapes:
            if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                yield from self._walk_shapes(shape.shapes)
            else:
                yield shape

    def _shape_metadata(self, shape: Any, shape_index: int) -> dict[str, Any]:
        """Returns stable native PPTX identity and position metadata for a slide shape."""
        return {
            "shape_index": shape_index,
            "shape_id": getattr(shape, "shape_id", None),
            "shape_name": normalize_text(str(getattr(shape, "name", "") or "")) or f"shape_{shape_index}",
            "bbox": {"left": shape.left, "top": shape.top, "width": shape.width, "height": shape.height},
            "extractor_role": "native_structure",
        }

    def _picture_metadata(self, shape: Any) -> dict[str, Any]:
        """Reads native picture metadata without decoding visual meaning."""
        image = getattr(shape, "image", None)
        blob = bytes(getattr(image, "blob", b"") or b"") if image is not None else b""
        return {
            "image_hash": hashlib.sha256(blob).hexdigest() if blob else None,
            "image_extension": normalize_text(str(getattr(image, "ext", "") or "")) or "png",
            "image_storage_status": "pending",
        }

    def _chart_metadata(self, shape: Any) -> dict[str, Any]:
        """Preserves lightweight native chart metadata when PowerPoint exposes it."""
        chart = getattr(shape, "chart", None)
        title = ""
        try:
            if chart is not None and chart.has_title:
                title = normalize_text(chart.chart_title.text_frame.text) or ""
        except Exception:
            title = ""
        return {
            "chart_title": title,
            "classification": "chart",
            "image_storage_status": "metadata_only",
        }

    # This function extracts speaker-note text without treating it as slide-body content.
    def _extract_notes(self, slide: Any) -> str | None:
        if not getattr(slide, "has_notes_slide", False):
            return None
        lines = [
            normalize_text(shape.text) or ""
            for shape in slide.notes_slide.shapes
            if getattr(shape, "has_text_frame", False) and normalize_text(shape.text)
        ]
        return normalize_text(" ".join(lines))

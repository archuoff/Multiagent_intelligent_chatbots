"""Docling extraction adapter used by PDF, DOCX, and PowerPoint parsers.

This module is the single boundary between our application and Docling. Parser
policy and canonical-node construction remain outside this adapter.
"""

from __future__ import annotations

import os
import time
from importlib.util import find_spec
from pathlib import Path
from typing import Any


from backend.ingestion.extraction.contracts import ExtractedPage
from backend.ingestion.extraction.contracts import ExtractionResult
from backend.ingestion.utils import normalize_text
from backend.ingestion.extraction.timeout_policy import PdfTimeoutPolicy, PDF_PROCESSING_CAPTION


class DoclingContentAdapter:
    """Converts a source through Docling and returns export-ready raw artifacts."""

    # This function records Docling availability without initializing models during application startup.
    def __init__(self, timeout_policy: PdfTimeoutPolicy | None = None) -> None:
        self._converter = None
        self._timeout_policy = timeout_policy or PdfTimeoutPolicy()

    # This function configures Docling PDF processing for text-based enterprise documents.
    def _build_converter(self, timeout_seconds: int = 120) -> Any:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
        from docling.document_converter import DocumentConverter, PdfFormatOption

        pipeline_options = PdfPipelineOptions()
        pipeline_options.do_ocr = os.getenv("JLR_DOCLING_OCR", "false").lower() == "true"
        pipeline_options.do_table_structure = True
        pipeline_options.table_structure_options.mode = TableFormerMode.ACCURATE
        pipeline_options.generate_page_images = False
        pipeline_options.generate_picture_images = False
        pipeline_options.document_timeout = timeout_seconds
        self._configure_picture_description(pipeline_options)
        artifacts_path = os.getenv("DOCLING_ARTIFACTS_PATH")
        if artifacts_path:
            pipeline_options.artifacts_path = Path(artifacts_path)
        return DocumentConverter(
            format_options={
                InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
            }
        )

    def _configure_picture_description(self, pipeline_options: Any) -> None:
        """Optionally enables remote Azure OpenAI vision descriptions for Docling pictures."""
        if os.getenv("JLR_DOCLING_PICTURE_DESCRIPTION", "false").lower() != "true":
            return
        from docling.datamodel.pipeline_options import PictureDescriptionApiOptions

        endpoint = self._required_env("AZURE_OPENAI_ENDPOINT")
        api_key = self._required_env("AZURE_OPENAI_API_KEY")
        deployment = self._required_env("AZURE_OPENAI_VISION_DEPLOYMENT")
        api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
        url = f"{endpoint.rstrip('/')}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
        pipeline_options.enable_remote_services = True
        pipeline_options.do_picture_description = True
        pipeline_options.generate_picture_images = True
        pipeline_options.picture_description_options = PictureDescriptionApiOptions(
            url=url,
            headers={"api-key": api_key},
            timeout=float(os.getenv("JLR_DOCLING_PICTURE_DESCRIPTION_TIMEOUT", "60")),
            concurrency=int(os.getenv("JLR_DOCLING_PICTURE_DESCRIPTION_CONCURRENCY", "1")),
            picture_area_threshold=float(os.getenv("JLR_DOCLING_PICTURE_AREA_THRESHOLD", "0.03")),
            prompt=os.getenv("JLR_DOCLING_PICTURE_DESCRIPTION_PROMPT", self._default_picture_prompt()),
            provenance="azure_openai_vision",
        )

    def _required_env(self, name: str) -> str:
        """Reads a required runtime setting without logging or returning secrets elsewhere."""
        value = os.getenv(name)
        if not value:
            raise RuntimeError(f"{name} must be set before enabling Docling picture description.")
        return value

    def _default_picture_prompt(self) -> str:
        """Guides the vision model toward retrieval-safe engineering descriptions."""
        return (
            "Describe this engineering slide image for enterprise search. "
            "Extract visible labels, numbers, dimensions, part names, callouts, and warnings. "
            "If the image is decorative or unclear, say so briefly. Do not invent values."
        )

    # This function reports whether Docling extraction can run in the current environment.
    def is_available(self) -> bool:
        return find_spec("docling") is not None

    # This function extracts Docling artifacts and normalized source-level content without canonicalization.
    def extract(self, source_path: str | Path, *, page_count: int | None = None) -> ExtractionResult:
        if not self.is_available():
            raise RuntimeError("Docling is not installed")
        is_pdf = Path(source_path).suffix.lower() == ".pdf"
        details: dict[str, Any] = {}
        warnings: list[str] = []
        if is_pdf:
            if page_count is None:
                from pypdf import PdfReader
                with Path(source_path).open("rb") as stream:
                    page_count = len(PdfReader(stream).pages)
            ocr_enabled = os.getenv("JLR_DOCLING_OCR", "false").lower() == "true"
            allowance = self._timeout_policy.initial_timeout(page_count, ocr_enabled)
            details = {"caption": PDF_PROCESSING_CAPTION, "page_count": page_count, "ocr_enabled": ocr_enabled,
                       "initial_timeout_seconds": allowance, "total_budget_seconds": self._timeout_policy.total_budget_seconds,
                       "attempts": []}
            started = time.monotonic()
            result = None
            for attempt in range(2):
                try:
                    converter = self._build_converter(allowance)
                    candidate = converter.convert(str(source_path), raises_on_error=False)
                except Exception as error:
                    if result is None:
                        raise
                    warnings.append(f"Docling retry failed: {type(error).__name__}; previous partial result retained.")
                    details["attempts"].append({"timeout_seconds": allowance, "status": "exception", "error_type": type(error).__name__})
                    break
                details["attempts"].append({"timeout_seconds": allowance, "status": self._status(candidate),
                                             "errors": [self._error_details(error) for error in getattr(candidate, "errors", [])]})
                # A failed retry must not discard usable output from the first attempt.
                if result is None or self._status(candidate) in {"success", "partial_success"}:
                    result = candidate
                if attempt or self._status(candidate) == "success" or not self._has_timeout(candidate):
                    break
                retry = self._timeout_policy.retry_timeout(allowance, time.monotonic() - started)
                if retry is None:
                    break
                warnings.append(f"Docling timed out at {allowance}s; retrying from the beginning with {retry}s.")
                allowance = retry
            details["elapsed_seconds"] = round(time.monotonic() - started, 2)
        else:
            if self._converter is None:
                self._converter = self._build_converter()
            result = self._converter.convert(str(source_path))
        status = self._status(result)
        requires_review = status != "success"
        details["conversion_status"] = status
        if requires_review:
            warnings.append(f"Docling conversion is {status}; extracted content requires review.")
        document = result.document
        exported = document.export_to_dict()
        pages = self._extract_pages(document)
        try:
            markdown = document.export_to_markdown() or ""
        except Exception as error:
            markdown = ""
            warnings.append(f"Optional Markdown export failed: {type(error).__name__}; structured content retained.")
        return ExtractionResult(
            extractor_name="docling",
            extraction_mode="docling_primary",
            pages=pages,
            markdown=markdown,
            exported=exported,
            blocks=[block for page in pages for block in page.text_blocks],
            tables=[table for page in pages for table in page.tables],
            images=[image for page in pages for image in page.images],
            warnings=warnings,
            requires_review=requires_review,
            processing_details=details,
        )

    def _status(self, result: Any) -> str:
        """Normalizes Docling's conversion-status enum without importing its models."""
        status = getattr(result, "status", "unknown")
        return str(getattr(status, "value", status))

    def _error_details(self, error: Any) -> dict[str, str]:
        """Preserves structured error categories and messages for the run audit."""
        category = getattr(error, "category", "unknown")
        return {"category": str(getattr(category, "value", category)), "message": str(getattr(error, "error_message", ""))}

    def _has_timeout(self, result: Any) -> bool:
        """Retries only explicit timeout errors, never arbitrary partial results."""
        return any(self._error_details(error)["category"] == "timeout" for error in getattr(result, "errors", []))

    # This function reads Docling's live hierarchy, tables, and pictures into a stable raw contract.
    def _extract_pages(self, document: Any) -> list[ExtractedPage]:
        page_map: dict[int, ExtractedPage] = {number: ExtractedPage(number=number) for number in getattr(document, "pages", {})}
        page_heights = self._page_heights(document)
        for item in document.iterate_items():
            element, level = item if isinstance(item, tuple) else (item, 0)
            page_number = self._page_number(element)
            if page_number is None:
                continue
            page = page_map.setdefault(page_number, ExtractedPage(number=page_number))
            text = self._element_text(element)
            if text:
                block = {"text": text, "type": self._element_type(element), "level": level}
                bbox = self._element_bbox(element, page_heights.get(page_number))
                if bbox:
                    block["bbox"] = bbox
                page.text_blocks.append(block)
        for page in page_map.values():
            page.text = "\n".join(block["text"] for block in page.text_blocks)
        for table in getattr(document, "tables", []):
            page_number = self._page_number(table)
            if page_number is None:
                continue
            page = page_map.setdefault(page_number, ExtractedPage(number=page_number))
            page.tables.append(self._extract_table(table, document))
        for picture in getattr(document, "pictures", []):
            page_number = self._page_number(picture)
            if page_number is None:
                continue
            page = page_map.setdefault(page_number, ExtractedPage(number=page_number))
            page.images.append(self._picture_metadata(picture, page_heights.get(page_number)))
        return [page_map[number] for number in sorted(page_map)]

    def _page_heights(self, document: Any) -> dict[int, float]:
        """Reads each page's height once, used to convert a bottom-left-origin
        Docling bbox into the same top-left/y-down space PyMuPDF's raw word
        positions use -- without this, bbox-overlap comparisons could
        silently check a vertically mirrored region of the page."""
        heights: dict[int, float] = {}
        for number, page_item in getattr(document, "pages", {}).items():
            size = getattr(page_item, "size", None)
            height = getattr(size, "height", None) if size is not None else None
            if isinstance(height, (int, float)):
                heights[int(number)] = float(height)
        return heights

    # Preserve the source structure first; a matrix is only a compatibility recovery.
    def _extract_table(self, table: Any, document: Any) -> dict[str, Any]:
        """Retains cells and provenance even when a tabular export cannot be produced."""
        payload: dict[str, Any] = {}
        warnings: list[str] = []
        try:
            payload = table.model_dump(mode="json", exclude={"data": {"grid"}})
        except Exception as error:
            warnings.append(f"Table model export failed: {type(error).__name__}")
            try:
                payload = table.export_to_dict()
            except Exception as export_error:
                warnings.append(f"Table dictionary export failed: {type(export_error).__name__}")
        data = payload.get("data", {})
        if isinstance(data, dict) and isinstance(data.get("table_cells"), list) and data["table_cells"]:
            return {"source_table": payload, "warnings": warnings}
        try:
            dataframe = table.export_to_dataframe(doc=document)
            matrix = [
                [str(column) for column in dataframe.columns],
                *[[normalize_text(str(value)) or "" for value in row] for row in dataframe.values.tolist()],
            ]
            return {"source_table": payload, "matrix": matrix, "warnings": warnings}
        except Exception as error:
            warnings.append(f"Table DataFrame export failed: {type(error).__name__}")
            rows = data if isinstance(data, list) else payload.get("rows")
            if isinstance(rows, list) and rows and all(isinstance(row, list) for row in rows):
                return {"source_table": payload, "matrix": [[str(cell) for cell in row] for row in rows], "warnings": warnings}
            return {"source_table": payload, "warnings": warnings, "requires_review": True}

    # This function preserves Docling picture metadata without exporting images or invoking vision models.
    def _picture_metadata(self, picture: Any, page_height: float | None = None) -> dict[str, Any]:
        metadata: dict[str, Any] = {"caption": "", "description": "", "classification": "", "bbox": None}
        caption = getattr(picture, "caption", None)
        if caption is not None:
            metadata["caption"] = self._element_text(caption)
        description = self._picture_description(picture)
        if description:
            metadata["description"] = description
        metadata["classification"] = str(getattr(picture, "classification", "") or "")
        provenance = getattr(picture, "prov", None)
        if provenance:
            bbox = getattr(provenance[0], "bbox", None)
            if bbox is not None:
                left = getattr(bbox, "l", None)
                top = getattr(bbox, "t", None)
                right = getattr(bbox, "r", None)
                bottom = getattr(bbox, "b", None)
                if all(isinstance(value, (int, float)) for value in (left, top, right, bottom)):
                    metadata["bbox"] = self._normalize_docling_bbox(
                        left, top, right, bottom, getattr(bbox, "coord_origin", None), page_height
                    )
        return metadata

    def _element_bbox(self, element: Any, page_height: float | None = None) -> dict[str, float] | None:
        """Reads the first Docling provenance bbox for text-level verification."""
        provenance = getattr(element, "prov", None)
        if not provenance:
            return None
        bbox = getattr(provenance[0], "bbox", None)
        if bbox is None:
            return None
        left = getattr(bbox, "l", None)
        top = getattr(bbox, "t", None)
        right = getattr(bbox, "r", None)
        bottom = getattr(bbox, "b", None)
        if not all(isinstance(value, (int, float)) for value in (left, top, right, bottom)):
            return None
        return self._normalize_docling_bbox(left, top, right, bottom, getattr(bbox, "coord_origin", None), page_height)

    def _normalize_docling_bbox(
        self,
        left: float,
        top: float,
        right: float,
        bottom: float,
        coord_origin: Any,
        page_height: float | None,
    ) -> dict[str, Any]:
        """Converts a Docling bbox into the same top-left/y-down coordinate
        space PyMuPDF's raw word positions use, so bbox-overlap comparisons
        (conflict verification, new-content checks) compare the same physical
        page region instead of silently mirroring it vertically.

        Docling's PDF-backend provenance is commonly bottom-left-origin
        (native PDF convention, where a larger y is physically higher on the
        page); PyMuPDF's `get_text("words")` is top-left-origin (y increases
        downward). Flipping requires the page height, which is only available
        when Docling exposes page geometry -- when it isn't, this returns
        `coord_space: "unknown"` rather than guessing, so callers can skip
        bbox-based verification instead of trusting a possibly-mirrored box.
        """
        origin = str(coord_origin or "").upper()
        is_bottom_left = "BOTTOM" in origin or (not origin and top > bottom)
        as_is = {"left": min(left, right), "top": min(top, bottom), "width": abs(right - left), "height": abs(top - bottom)}
        if not is_bottom_left:
            return {**as_is, "coord_space": "top_left", "source_coord_origin": origin or "inferred_top_left"}
        if isinstance(page_height, (int, float)) and page_height > 0:
            flipped_top = page_height - max(top, bottom)
            return {**as_is, "top": flipped_top, "coord_space": "top_left", "source_coord_origin": origin or "inferred_bottom_left"}
        return {**as_is, "coord_space": "unknown", "source_coord_origin": origin or "inferred_bottom_left"}

    def _picture_description(self, picture: Any) -> str:
        """Reads Docling picture descriptions from current and deprecated metadata fields."""
        meta = getattr(picture, "meta", None)
        description = getattr(meta, "description", None) if meta is not None else None
        text = getattr(description, "text", None)
        if isinstance(text, str) and normalize_text(text):
            return normalize_text(text) or ""
        for annotation in getattr(picture, "annotations", []) or []:
            text = getattr(annotation, "text", None)
            if isinstance(text, str) and normalize_text(text):
                return normalize_text(text) or ""
        return ""

    # This function extracts readable text from a Docling element without using representation fallbacks.
    def _element_text(self, element: Any) -> str:
        for attribute in ("text", "content", "value"):
            value = getattr(element, attribute, None)
            if isinstance(value, str) and normalize_text(value):
                return normalize_text(value) or ""
        return ""

    # This function maps a Docling item to a stable extraction-level label.
    def _element_type(self, element: Any) -> str:
        type_name = type(element).__name__.lower()
        if "heading" in type_name or "title" in type_name:
            return "heading"
        if "list" in type_name:
            return "list_item"
        if "table" in type_name:
            return "table_ref"
        if "picture" in type_name or "figure" in type_name:
            return "picture_ref"
        return "paragraph"

    # This function reads the first Docling provenance page using the JLR one-based page convention.
    def _page_number(self, element: Any) -> int | None:
        provenance = getattr(element, "prov", None)
        if not provenance:
            return None
        page_number = getattr(provenance[0], "page_no", None)
        return page_number if isinstance(page_number, int) and page_number > 0 else None

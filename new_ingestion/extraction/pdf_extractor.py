"""Docling-only raw PDF extraction for the new ingestion system.

This module deliberately stops at the Docling boundary.  It does not build a
canonical document model, persist artifacts, chunk content, or call any AI or
remote validation service.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class PdfExtractionError(RuntimeError):
    """Base error raised by the isolated PDF extraction layer."""


class DoclingUnavailableError(PdfExtractionError):
    """Raised when the active Python environment does not provide Docling."""


@dataclass(slots=True)
class PdfExtractionResult:
    """Raw artifacts returned by one Docling PDF conversion."""

    source_path: Path
    conversion_status: Any
    document: Any = None
    exported: dict[str, Any] | None = None
    markdown: str | None = None
    warnings: list[Any] = field(default_factory=list)
    errors: list[Any] = field(default_factory=list)
    processing_configuration: dict[str, Any] = field(default_factory=dict)


def _require_pdf(source_path: str | Path) -> Path:
    """Validate existence, regular-file status, and the PDF file signature."""
    path = Path(source_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(path)
    if not path.is_file():
        raise ValueError(f"Source path is not a file: {path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Source file does not have a .pdf extension: {path}")
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError(f"Source file is not a PDF: {path}")
    return path.resolve()


def _docling_version() -> str:
    try:
        return importlib.metadata.version("docling")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def _build_converter() -> tuple[Any, dict[str, Any]]:
    """Build Docling's standard PDF pipeline using the installed API."""
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            LayoutObjectDetectionOptions,
            PdfPipelineOptions,
            TableFormerMode,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.picture_classification_options import (
            DocumentPictureClassifierOptions,
        )
    except ImportError as error:
        raise DoclingUnavailableError(
            "Docling is not installed in the active Python environment."
        ) from error

    options = PdfPipelineOptions()
    options.layout_options = LayoutObjectDetectionOptions.from_preset(
        "layout_heron_default"
    )
    options.picture_classification_options = (
        DocumentPictureClassifierOptions.from_preset(
            "document_figure_classifier_v2"
        )
    )
    options.do_ocr = True
    options.do_table_structure = True
    options.table_structure_options.mode = TableFormerMode.ACCURATE
    options.do_code_enrichment = True
    options.do_formula_enrichment = True
    options.generate_picture_images = True
    options.enable_remote_services = False

    # Picture classification is available in Docling 2.132.0. Keep this
    # guarded because it was introduced after some older Docling releases.
    picture_classification_supported = hasattr(options, "do_picture_classification")
    if picture_classification_supported:
        options.do_picture_classification = True

    artifacts_value = os.environ.get("DOCLING_ARTIFACTS_PATH")
    artifacts_path: Path | None = None
    if artifacts_value:
        artifacts_path = Path(artifacts_value).expanduser()
        if not artifacts_path.is_dir():
            raise ValueError(
                "DOCLING_ARTIFACTS_PATH must point to an existing directory: "
                f"{artifacts_path}"
            )
        options.artifacts_path = artifacts_path

    configuration = {
        "engine": "docling",
        "docling_version": _docling_version(),
        "pipeline": "StandardPdfPipeline",
        "layout_detection": True,
        "layout_model": "layout_heron_default",
        "ocr": options.do_ocr,
        "table_structure": options.do_table_structure,
        "tableformer_mode": options.table_structure_options.mode.value,
        "code_enrichment": options.do_code_enrichment,
        "formula_enrichment": options.do_formula_enrichment,
        "picture_classification": picture_classification_supported,
        "picture_classifier": "document_figure_classifier_v2",
        "picture_images": options.generate_picture_images,
        "remote_services": options.enable_remote_services,
        "artifacts_path": str(artifacts_path) if artifacts_path else None,
    }
    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(pipeline_options=options),
        }
    )
    return converter, configuration


def _export_document(document: Any) -> tuple[dict[str, Any] | None, str | None]:
    exported = document.export_to_dict() if hasattr(document, "export_to_dict") else None
    markdown = document.export_to_markdown() if hasattr(document, "export_to_markdown") else None
    return exported, markdown


def extract_pdf(source_path: str | Path, *, artifact_dir: str | Path | None = None) -> PdfExtractionResult:
    """Convert one PDF through Docling and return its unmodified raw artifacts.

    ``artifact_dir``, when given, is recorded in ``processing_configuration``
    so ``normalize_pdf`` can persist extracted picture assets under it (the
    same mechanism the xlsx/pptx extractors use).
    """
    path = _require_pdf(source_path)
    converter, configuration = _build_converter()
    configuration["artifact_dir"] = str(Path(artifact_dir).expanduser()) if artifact_dir else None
    try:
        conversion = converter.convert(str(path), raises_on_error=False)
    except Exception as error:
        raise PdfExtractionError(f"Docling PDF conversion failed for {path}: {error}") from error

    document = getattr(conversion, "document", None)
    exported, markdown = _export_document(document) if document is not None else (None, None)
    return PdfExtractionResult(
        source_path=path,
        conversion_status=getattr(conversion, "status", None),
        document=document,
        exported=exported,
        markdown=markdown,
        warnings=list(getattr(conversion, "warnings", []) or []),
        errors=list(getattr(conversion, "errors", []) or []),
        processing_configuration=configuration,
    )


def _main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the Docling PDF extractor.")
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--artifact-dir", type=Path, default=None)
    args = parser.parse_args()
    result = extract_pdf(args.pdf, artifact_dir=args.artifact_dir)
    print(f"status={result.conversion_status}")
    print(f"source={result.source_path}")
    print(f"markdown={result.markdown is not None}")
    print(f"warnings={len(result.warnings)} errors={len(result.errors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

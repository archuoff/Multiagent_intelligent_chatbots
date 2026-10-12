from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from new_ingestion.extraction.format_detector import DocumentFormat, FormatDetectionResult
from new_ingestion.orchestration.ingestion_orchestrator import (
    OrchestrationStatus, ingest_to_canonical,
)
from new_ingestion.schema.schema_define import (
    CanonicalDocument, DocumentMetadata, ExtractionMetadata, ExtractionStatus,
    SourceType,
)


@dataclass
class FakeRaw:
    warnings: list[str]
    errors: list[str]
    binary_data: bytes = b"image-bytes"

    def to_dict(self) -> dict:
        return {"warnings": self.warnings, "errors": self.errors, "binary_data": self.binary_data}


def canonical(source_type: SourceType = SourceType.PDF) -> CanonicalDocument:
    return CanonicalDocument(
        document=DocumentMetadata(
            document_id="doc-1", version_id="version-1", source_file_name="source.file",
            source_type=source_type, mime_type="application/octet-stream", sha256="a" * 64,
            extraction=ExtractionMetadata(extractor="test", status=ExtractionStatus.SUCCESS),
        )
    )


class IngestionOrchestratorTests(unittest.TestCase):
    def run_route(self, document_format: DocumentFormat, extractor_name: str, normalizer_name: str,
                  *, artifact_dir: Path | None = None):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f"source.{document_format.value}"
            path.write_bytes(b"source-content")
            detection = FormatDetectionResult(document_format, path, "test", 1.0, "test")
            raw = FakeRaw([], [])
            with patch("new_ingestion.orchestration.ingestion_orchestrator.detect_format", return_value=detection), \
                 patch(f"new_ingestion.orchestration.ingestion_orchestrator.{extractor_name}", return_value=raw) as extractor, \
                 patch(f"new_ingestion.orchestration.ingestion_orchestrator.{normalizer_name}", return_value=canonical(SourceType(document_format.value))) as normalizer:
                result = ingest_to_canonical(
                    path, document_id="doc-1", version_id="version-1",
                    logical_document_key="logical-1", artifact_dir=artifact_dir,
                )
            return result, extractor, normalizer, path

    def test_pdf_pptx_xlsx_routes_and_passes_arguments(self) -> None:
        for document_format, extractor_name, normalizer_name in (
            (DocumentFormat.PDF, "extract_pdf", "normalize_pdf"),
            (DocumentFormat.PPTX, "extract_pptx", "normalize_pptx"),
            (DocumentFormat.XLSX, "extract_xlsx", "normalize_xlsx"),
        ):
            with self.subTest(document_format=document_format):
                result, extractor, normalizer, path = self.run_route(document_format, extractor_name, normalizer_name)
                self.assertEqual(result.status, OrchestrationStatus.SUCCESS)
                self.assertEqual(result.detected_format, document_format)
                self.assertEqual(result.source_file_hash, hashlib.sha256(b"source-content").hexdigest())
                extractor.assert_called_once()
                normalizer.assert_called_once()
                kwargs = normalizer.call_args.kwargs
                self.assertEqual(kwargs["document_id"], "doc-1")
                self.assertEqual(kwargs["version_id"], "version-1")
                self.assertEqual(kwargs["logical_document_key"], "logical-1")
                self.assertEqual(kwargs["source_file_hash"], result.source_file_hash)

    def test_hash_is_calculated_and_artifact_dir_is_forwarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            artifact_dir = Path(directory) / "artifacts"
            path = Path(directory) / "source.pptx"
            path.write_bytes(b"source-content")
            detection = FormatDetectionResult(DocumentFormat.PPTX, path, "test", 1.0, "test")
            with patch("new_ingestion.orchestration.ingestion_orchestrator.detect_format", return_value=detection), \
                 patch("new_ingestion.orchestration.ingestion_orchestrator.extract_pptx", return_value=FakeRaw([], [])) as extractor, \
                 patch("new_ingestion.orchestration.ingestion_orchestrator.normalize_pptx", return_value=canonical(SourceType.PPTX)) as normalizer:
                result = ingest_to_canonical(path, document_id="doc", version_id="version", artifact_dir=artifact_dir)
            self.assertEqual(result.source_file_hash, hashlib.sha256(b"source-content").hexdigest())
            self.assertEqual(extractor.call_args.kwargs["artifact_dir"], artifact_dir)
            self.assertEqual(normalizer.call_args.kwargs["source_file_hash"], result.source_file_hash)

    def test_unsupported_docx_and_unknown_are_structured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for suffix, detected, expected in (
                (".docx", DocumentFormat.DOCX, "DOCX extraction is not implemented yet"),
                (".bin", DocumentFormat.UNKNOWN, "unsupported document format"),
            ):
                path = Path(directory) / f"source{suffix}"; path.write_bytes(b"data")
                detection = FormatDetectionResult(detected, path, "test", 1.0, "test")
                with patch("new_ingestion.orchestration.ingestion_orchestrator.detect_format", return_value=detection):
                    result = ingest_to_canonical(path, document_id="doc", version_id="version")
                self.assertEqual(result.status, OrchestrationStatus.UNSUPPORTED)
                self.assertIsNone(result.canonical_document)
                self.assertIn(expected, result.errors)

    def test_expected_failures_return_failed(self) -> None:
        missing = Path(tempfile.gettempdir()) / "definitely-missing-ingestion-file.bin"
        result = ingest_to_canonical(missing, document_id="doc", version_id="version")
        self.assertEqual(result.status, OrchestrationStatus.FAILED)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.pdf"; path.write_bytes(b"data")
            detection = FormatDetectionResult(DocumentFormat.PDF, path, "test", 1.0, "test")
            with patch("new_ingestion.orchestration.ingestion_orchestrator.detect_format", return_value=detection), \
                 patch("new_ingestion.orchestration.ingestion_orchestrator.extract_pdf", side_effect=RuntimeError("extract failed")):
                result = ingest_to_canonical(path, document_id="doc", version_id="version")
            self.assertEqual(result.status, OrchestrationStatus.FAILED)
            self.assertIn("extract failed", result.errors[0])

    def test_warnings_make_successful_result_partial_and_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.xlsx"; path.write_bytes(b"data")
            detection = FormatDetectionResult(DocumentFormat.XLSX, path, "test", 1.0, "test")
            raw = FakeRaw(["raw warning"], [])
            document = canonical(SourceType.XLSX)
            document.diagnostics.warnings.append("normalization warning")
            with patch("new_ingestion.orchestration.ingestion_orchestrator.detect_format", return_value=detection), \
                 patch("new_ingestion.orchestration.ingestion_orchestrator.extract_xlsx", return_value=raw), \
                 patch("new_ingestion.orchestration.ingestion_orchestrator.normalize_xlsx", return_value=document):
                result = ingest_to_canonical(path, document_id="doc", version_id="version")
            self.assertEqual(result.status, OrchestrationStatus.PARTIAL)
            self.assertTrue(any("raw warning" in warning for warning in result.warnings))
            self.assertTrue(any("normalization warning" in warning for warning in result.warnings))

    def test_result_serialization_is_json_safe_and_does_not_embed_bytes(self) -> None:
        result, _, _, _ = self.run_route(DocumentFormat.PDF, "extract_pdf", "normalize_pdf")
        payload = result.to_dict()
        json.dumps(payload)
        self.assertEqual(payload["raw_result"]["binary_data"], None)
        self.assertIsInstance(payload["source_path"], str)

    def test_source_is_not_modified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source.xlsx"; path.write_bytes(b"unchanged")
            before = path.read_bytes()
            detection = FormatDetectionResult(DocumentFormat.XLSX, path, "test", 1.0, "test")
            with patch("new_ingestion.orchestration.ingestion_orchestrator.detect_format", return_value=detection), \
                 patch("new_ingestion.orchestration.ingestion_orchestrator.extract_xlsx", return_value=FakeRaw([], [])), \
                 patch("new_ingestion.orchestration.ingestion_orchestrator.normalize_xlsx", return_value=canonical(SourceType.XLSX)):
                ingest_to_canonical(path, document_id="doc", version_id="version")
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()

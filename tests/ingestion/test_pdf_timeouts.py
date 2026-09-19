"""Tests PDF budgets and retry outcomes without downloading or running models."""

from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from backend.ingestion.extraction.contracts import ExtractedPage, ExtractionResult
from backend.ingestion.extraction.docling import DoclingContentAdapter
from backend.ingestion.extraction.merger import ExtractionResultMerger
from backend.ingestion.extraction.timeout_policy import PdfTimeoutPolicy, PDF_PROCESSING_CAPTION
from backend.ingestion.models import SourceType
from backend.ingestion.parsers.base import IngestionSource, ParserContext
from backend.ingestion.parsers.document import PdfDocumentParser


def conversion(status="success", category=None):
    """Builds a small Docling-shaped result with usable content and an explicit status."""
    document = SimpleNamespace(pages={1: object()}, tables=[], pictures=[],
                               export_to_dict=lambda: {}, export_to_markdown=lambda: "Sensor requirements",
                               iterate_items=lambda: [(SimpleNamespace(text="Sensor requirements", prov=[SimpleNamespace(page_no=1)]), 0)])
    errors = [] if category is None else [SimpleNamespace(category=category, error_message="test conversion error")]
    return SimpleNamespace(status=status, errors=errors, document=document)


class PdfTimeoutTests(unittest.TestCase):
    """Checks limits, retry classification, and downstream review handling."""

    def test_page_and_ocr_allowances(self):
        """Matches the agreed examples and caps very large documents."""
        policy = PdfTimeoutPolicy()
        self.assertEqual([policy.initial_timeout(pages, False) for pages in (10, 30, 100)], [120, 240, 660])
        self.assertEqual([policy.initial_timeout(pages, True) for pages in (10, 30, 100)], [180, 420, 900])
        self.assertEqual(policy.initial_timeout(10000, False), 900)
        self.assertGreaterEqual(policy.process_timeout(10, False), 120 + 300)
        with self.assertRaises(ValueError):
            policy.initial_timeout(0, False)

    def test_no_retry_if_budget_or_larger_allowance_unavailable(self):
        """Does not allocate more than the configured cap or remaining budget."""
        policy = PdfTimeoutPolicy()
        self.assertIsNone(policy.retry_timeout(900, 900))
        self.assertIsNone(policy.retry_timeout(120, 1790))
        self.assertEqual(policy.retry_timeout(120, 120), 300)

    def run_adapter(self, results, page_count=10):
        """Injects conversion outcomes while exercising the real retry controller."""
        adapter = DoclingContentAdapter()
        converters = [SimpleNamespace(convert=Mock(return_value=result)) for result in results]
        with patch.object(adapter, "is_available", return_value=True), patch.object(adapter, "_build_converter", side_effect=converters) as build, patch.dict("os.environ", {"JLR_DOCLING_OCR": "false"}):
            output = adapter.extract("sample.pdf", page_count=page_count)
        return output, build.call_args_list

    def test_timeout_retries_once_and_success_clears_review(self):
        """A confirmed timeout gets one larger attempt with retained history."""
        output, calls = self.run_adapter([conversion("partial_success", "timeout"), conversion()])
        self.assertEqual([call.args[0] for call in calls], [120, 300])
        self.assertFalse(output.requires_review)
        self.assertEqual(len(output.processing_details["attempts"]), 2)
        self.assertEqual(output.processing_details["caption"], PDF_PROCESSING_CAPTION)

    def test_other_partial_failures_do_not_retry(self):
        """Extra time is not used for backend or inference failures."""
        output, calls = self.run_adapter([conversion("partial_success", "backend_failure")])
        self.assertEqual(len(calls), 1)
        self.assertTrue(output.requires_review)

    def test_repeated_timeout_keeps_partial_content_for_review(self):
        """Retries are bounded and incomplete text remains available for inspection."""
        output, calls = self.run_adapter([conversion("partial_success", "timeout"), conversion("partial_success", "timeout")])
        self.assertEqual(len(calls), 2)
        self.assertTrue(output.requires_review)
        self.assertEqual(output.pages[0].text, "Sensor requirements")

    def test_failed_retry_retains_first_partial_result(self):
        """A failed second conversion does not discard the first usable document."""
        output, _ = self.run_adapter([conversion("partial_success", "timeout"), conversion("failure", "backend_failure")])
        self.assertTrue(output.requires_review)
        self.assertEqual(output.processing_details["conversion_status"], "partial_success")

    def test_each_pdf_gets_its_own_timeout(self):
        """A reused adapter must not inherit the preceding PDF's time limit."""
        adapter = DoclingContentAdapter()
        converter = SimpleNamespace(convert=Mock(return_value=conversion()))
        with patch.object(adapter, "is_available", return_value=True), patch.object(adapter, "_build_converter", return_value=converter) as build, patch.dict("os.environ", {"JLR_DOCLING_OCR": "false"}):
            adapter.extract("short.pdf", page_count=10)
            adapter.extract("long.pdf", page_count=30)
        self.assertEqual([call.args[0] for call in build.call_args_list], [120, 240])

    def test_partial_outcome_reaches_canonical_quality(self):
        """Incomplete conversion must block automatic ready-for-chunking status."""
        parser = PdfDocumentParser()
        raw = ExtractionResult(extractor_name="docling", extraction_mode="docling_primary", pages=[ExtractedPage(number=1, text="Sensor requirements")], markdown="Sensor requirements", requires_review=True, processing_details={"conversion_status": "partial_success"})
        with patch.object(parser._inspector, "inspect", return_value=SimpleNamespace(page_count=1, has_usable_embedded_text=True)), patch.object(parser._docling, "is_available", return_value=True), patch.object(parser._docling, "extract", return_value=raw), patch("backend.ingestion.parsers.document.hash_file", return_value="test-hash"):
            document = parser.parse(IngestionSource("test", SourceType.PDF, Path("sample.pdf")), ParserContext())
        self.assertTrue(document.quality.errors)
        self.assertEqual(document.parser_info.processing_details["conversion_status"], "partial_success")

    def test_recovery_does_not_erase_incomplete_status(self):
        """Merging fallback text retains the primary conversion audit and review flag."""
        partial = ExtractionResult("docling", "docling_primary", requires_review=True, processing_details={"attempts": [120, 300]})
        merged = ExtractionResultMerger().merge(partial, ExtractionResult("pypdf", "fallback_library"))
        self.assertTrue(merged.requires_review)
        self.assertEqual(merged.processing_details, partial.processing_details)

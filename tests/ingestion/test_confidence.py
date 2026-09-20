"""Tests for confidence-tier assignment on merger reconciliation events."""

from pathlib import Path
import unittest

from backend.ingestion.extraction import confidence
from backend.ingestion.extraction.contracts import ExtractedPage, ExtractionResult
from backend.ingestion.extraction.merger import ExtractionResultMerger
from backend.ingestion.extraction.raw_text_index import PdfRawTextIndex, RawPdfWord
from backend.ingestion.extraction.reconciliation import ExtractionReconciler
from backend.ingestion.models import NodeType, SourceType
from backend.ingestion.parsers.base import IngestionSource
from backend.ingestion.parsers.document import PdfDocumentParser


class ConfidenceWeakestTests(unittest.TestCase):
    def test_weakest_of_mixed_tiers_is_low(self):
        self.assertEqual(confidence.weakest(confidence.HIGH, confidence.LOW, confidence.MEDIUM), confidence.LOW)

    def test_weakest_of_all_high_is_high(self):
        self.assertEqual(confidence.weakest(confidence.HIGH, confidence.HIGH), confidence.HIGH)

    def test_weakest_with_no_known_tiers_defaults_low(self):
        self.assertEqual(confidence.weakest(), confidence.LOW)
        self.assertEqual(confidence.weakest("unrecognized"), confidence.LOW)


class MergerConfidenceTierTests(unittest.TestCase):
    """Every reconciliation event the merger produces must carry a tier."""

    def setUp(self):
        self.merger = ExtractionResultMerger(ExtractionReconciler())

    def test_duplicate_text_is_high_confidence(self):
        primary = ExtractionResult("docling", "primary", pages=[
            ExtractedPage(1, text="Sensor mounting guidance.", text_blocks=[{"text": "Sensor mounting guidance."}])
        ])
        fallback = ExtractionResult("pymupdf", "fallback", pages=[
            ExtractedPage(1, text_blocks=[{"text": "Sensor mounting guidance."}])
        ])
        merged = self.merger.merge(primary, fallback)
        events = [event for event in merged.pages[0].reconciliation if event["content_type"] == "text"]
        self.assertTrue(events)
        self.assertEqual(events[0]["confidence_tier"], confidence.HIGH)
        self.assertEqual(events[0]["confidence_reason"], "exact_duplicate")

    def test_unresolved_conflict_is_low_confidence(self):
        primary = ExtractionResult("docling", "primary", pages=[
            ExtractedPage(1, text="Maximum operating temperature is 90 C for this sensor.")
        ])
        fallback = ExtractionResult("pymupdf", "fallback", pages=[
            ExtractedPage(1, text="Maximum operating temperature is 80 C for this sensor.")
        ])
        merged = self.merger.merge(primary, fallback)  # no raw_text_index -> stays unresolved
        event = merged.pages[0].reconciliation[-1]
        self.assertEqual(event["decision"], "conflict")
        self.assertEqual(event["confidence_tier"], confidence.LOW)
        self.assertEqual(event["confidence_reason"], "conflict_unresolved")

    def test_low_confidence_text_never_enters_page_text(self):
        """Finding B's root cause, tested at the source: the merger must never
        fold a low-confidence block into primary.text, since PDF
        canonicalization later rebuilds paragraph nodes from that merged
        string, not from individual blocks -- once it's in .text, it can no
        longer be excluded without either dropping good neighboring text or
        keeping the bad text. It must stay excluded starting here."""
        primary = ExtractionResult("docling", "primary", pages=[
            ExtractedPage(1,
                text="Sensor mounting guidance is provided in section two of this document. "
                     "Maximum operating temperature is 90 C for this sensor.",
                text_blocks=[
                    {"text": "Sensor mounting guidance is provided in section two of this document."},
                    {"text": "Maximum operating temperature is 90 C for this sensor."},
                ])
        ])
        fallback = ExtractionResult("pymupdf", "fallback", pages=[
            ExtractedPage(1, text_blocks=[{"text": "Maximum operating temperature is 80 C for this sensor."}])
        ])
        merged = self.merger.merge(primary, fallback)  # no raw_text_index -> the conflict stays unresolved
        page = merged.pages[0]
        low_block = next(block for block in page.text_blocks if block.get("retrieval_allowed") is False)
        self.assertEqual(low_block["text"], "Maximum operating temperature is 80 C for this sensor.")
        self.assertTrue(low_block["requires_review"])
        self.assertNotIn("80 C", page.text)
        self.assertIn("90 C", page.text)
        self.assertIn("mounting guidance", page.text)

    def test_conflict_resolved_by_independent_referee_is_high_confidence(self):
        primary = ExtractionResult("docling", "primary", pages=[
            ExtractedPage(1, text="Maximum operating temperature is 90 C for this sensor.",
                          text_blocks=[{"text": "Maximum operating temperature is 90 C for this sensor.",
                                        "bbox": {"x0": 0, "y0": 0, "x1": 300, "y1": 20}}])
        ])
        fallback = ExtractionResult("pdfplumber", "fallback", pages=[
            ExtractedPage(1, text_blocks=[{"text": "Maximum operating temperature is 80 C for this sensor.",
                                           "bbox": {"x0": 0, "y0": 0, "x1": 300, "y1": 20}}])
        ])
        raw_index = PdfRawTextIndex(words=[
            RawPdfWord(1, 0, 0, 60, 10, "Maximum"), RawPdfWord(1, 65, 0, 120, 10, "operating"),
            RawPdfWord(1, 125, 0, 190, 10, "temperature"), RawPdfWord(1, 195, 0, 205, 10, "is"),
            RawPdfWord(1, 210, 0, 225, 10, "90"), RawPdfWord(1, 230, 0, 240, 10, "C"),
            RawPdfWord(1, 245, 0, 260, 10, "for"), RawPdfWord(1, 265, 0, 280, 10, "this"), RawPdfWord(1, 285, 0, 315, 10, "sensor."),
        ])
        # raw_index words came from PyMuPDF-style RawPdfWord objects (engine "provided" here),
        # fallback engine is "pdfplumber" -> the referee is independent of the fallback engine.
        merged = self.merger.merge(primary, fallback, raw_text_index=raw_index)
        event = merged.pages[0].reconciliation[-1]
        self.assertEqual(event["decision"], "conflict_resolved")
        self.assertEqual(event["confidence_tier"], confidence.HIGH)
        self.assertEqual(event["confidence_reason"], "raw_text_position_match")

    def test_conflict_resolved_via_shared_engine_referee_is_capped_at_medium(self):
        """Unit-level check of _assign_confidence's own rule: a
        conflict_resolved decision whose verification says independent=False
        (the referee shared an engine with the extractor it verified) must
        never be reported as high confidence, regardless of which side won."""
        merger = ExtractionResultMerger(ExtractionReconciler())
        decision = {
            "decision": "conflict_resolved",
            "winning_source": "fallback",
            "verification": {"method": "raw_text_position_match", "independent": False, "referee_engine": "pymupdf"},
        }
        result = merger._assign_confidence(decision)
        self.assertEqual(result["confidence_tier"], confidence.MEDIUM)
        self.assertEqual(result["confidence_reason"], "raw_text_position_match_shared_engine")

    def test_conflict_resolved_via_independent_referee_is_high(self):
        merger = ExtractionResultMerger(ExtractionReconciler())
        decision = {
            "decision": "conflict_resolved",
            "winning_source": "fallback",
            "verification": {"method": "raw_text_position_match", "independent": True, "referee_engine": "pdfplumber"},
        }
        result = merger._assign_confidence(decision)
        self.assertEqual(result["confidence_tier"], confidence.HIGH)
        self.assertEqual(result["confidence_reason"], "raw_text_position_match")

    def test_shared_engine_end_to_end_is_reflected_in_document_warning(self):
        """document.py appends a warning (not tested here) when engine_used
        matches the fallback's engine; this confirms is_independent_of itself
        reports False for that exact scenario, which is what the warning and
        the confidence cap both key off."""
        raw_index = PdfRawTextIndex(words=[RawPdfWord(1, 0, 0, 10, 10, "x")])
        raw_index._engine_used = "pymupdf"  # simulate what a real pymupdf-loaded page would set
        self.assertFalse(raw_index.is_independent_of("pymupdf"))
        self.assertTrue(raw_index.is_independent_of("pdfplumber"))

    def test_new_content_is_medium_confidence_before_item_4a(self):
        primary = ExtractionResult("docling", "primary", pages=[
            ExtractedPage(1, text="Sensor mounting guidance.", text_blocks=[{"text": "Sensor mounting guidance."}])
        ])
        fallback = ExtractionResult("pymupdf", "fallback", pages=[
            ExtractedPage(1, text_blocks=[{"text": "Use M6 bolts with a 12 Nm tightening torque."}])
        ])
        merged = self.merger.merge(primary, fallback)
        events = [event for event in merged.pages[0].reconciliation if event["decision"] == "fallback_only"]
        self.assertTrue(events)
        self.assertEqual(events[0]["confidence_tier"], confidence.MEDIUM)
        self.assertEqual(events[0]["confidence_reason"], "new_content_unverified")

    def test_page_selection_event_is_medium_confidence(self):
        primary = ExtractionResult("docling", "primary", pages=[ExtractedPage(1, text="Sensor overview.")])
        fallback = ExtractionResult("pymupdf", "fallback", pages=[
            ExtractedPage(1, text="Sensor overview. Mounting torque is 12 Nm. Operating voltage is 12 V.")
        ])
        merged = self.merger.merge(primary, fallback)
        selection_event = merged.pages[0].reconciliation[0]
        self.assertTrue(selection_event["decision"].startswith("selected_"))
        self.assertEqual(selection_event["confidence_tier"], confidence.MEDIUM)
        self.assertEqual(selection_event["confidence_reason"], "page_scoring_rule")


class NewContentVerificationTests(unittest.TestCase):
    """Item 4a: 'new content' (fallback_only) blocks now get checked too,
    not just conflicts -- these are all realistic multi-block pages, not
    degenerate single-block fixtures, per this project's test discipline."""

    def _multi_block_primary(self):
        """A base page with 4 distinct, non-overlapping legitimate blocks."""
        return ExtractionResult("docling", "primary", pages=[
            ExtractedPage(1,
                text="Torque spec value. Volume resistivity reading. Surface finish grade. Material code number.",
                text_blocks=[
                    {"text": "Torque spec value.", "bbox": {"left": 0, "top": 0, "width": 130, "height": 10}},
                    {"text": "Volume resistivity reading.", "bbox": {"left": 0, "top": 20, "width": 190, "height": 10}},
                    {"text": "Surface finish grade.", "bbox": {"left": 0, "top": 40, "width": 180, "height": 10}},
                    {"text": "Material code number.", "bbox": {"left": 0, "top": 60, "width": 160, "height": 10}},
                ])
        ])

    def _matching_raw_index(self, extra_words=()):
        words = [
            RawPdfWord(1, 0, 0, 60, 10, "Torque"), RawPdfWord(1, 65, 0, 90, 10, "spec"), RawPdfWord(1, 95, 0, 130, 10, "value."),
            RawPdfWord(1, 0, 20, 60, 30, "Volume"), RawPdfWord(1, 65, 20, 130, 30, "resistivity"), RawPdfWord(1, 135, 20, 190, 30, "reading."),
            RawPdfWord(1, 0, 40, 60, 50, "Surface"), RawPdfWord(1, 65, 40, 130, 50, "finish"), RawPdfWord(1, 135, 40, 180, 50, "grade."),
            RawPdfWord(1, 0, 60, 60, 70, "Material"), RawPdfWord(1, 65, 60, 100, 70, "code"), RawPdfWord(1, 105, 60, 160, 70, "number."),
            *extra_words,
        ]
        return PdfRawTextIndex(words=words)

    def test_fallback_only_block_present_at_its_bbox_is_high_confidence(self):
        """A new block whose text genuinely matches the raw text at its
        claimed position is confirmed, not just trusted on faith."""
        primary = self._multi_block_primary()
        fallback = ExtractionResult("pdfplumber", "fallback", pages=[
            ExtractedPage(1, text_blocks=[{"text": "Revision C update notes.",
                                           "bbox": {"x0": 0, "y0": 80, "x1": 150, "y1": 90}}])
        ])
        raw_index = self._matching_raw_index(extra_words=[
            RawPdfWord(1, 0, 80, 60, 90, "Revision"), RawPdfWord(1, 65, 80, 90, 90, "C"),
            RawPdfWord(1, 95, 80, 150, 90, "update"), RawPdfWord(1, 155, 80, 200, 90, "notes."),
        ])
        merger = ExtractionResultMerger(ExtractionReconciler())
        merged = merger.merge(primary, fallback, raw_text_index=raw_index)
        event = next(e for e in merged.pages[0].reconciliation if e["decision"] == "fallback_only")
        self.assertEqual(event["confidence_tier"], confidence.HIGH)
        self.assertEqual(event["confidence_reason"], "new_content_position_verified")
        self.assertIn("Revision C update notes.", merged.pages[0].text)

    def test_fallback_only_block_absent_from_its_bbox_is_low_confidence(self):
        """A new block claiming a bbox that actually contains different
        words entirely must not be silently trusted."""
        primary = self._multi_block_primary()
        fallback = ExtractionResult("pdfplumber", "fallback", pages=[
            ExtractedPage(1, text_blocks=[{"text": "Completely fabricated unrelated wording.",
                                           "bbox": {"x0": 0, "y0": 80, "x1": 200, "y1": 90}}])
        ])
        raw_index = self._matching_raw_index(extra_words=[
            RawPdfWord(1, 0, 80, 60, 90, "Revision"), RawPdfWord(1, 65, 80, 90, 90, "C"), RawPdfWord(1, 95, 80, 150, 90, "notes."),
        ])
        merger = ExtractionResultMerger(ExtractionReconciler())
        merged = merger.merge(primary, fallback, raw_text_index=raw_index)
        event = next(e for e in merged.pages[0].reconciliation if e["decision"] == "fallback_only")
        self.assertEqual(event["confidence_tier"], confidence.LOW)
        self.assertEqual(event["confidence_reason"], "new_content_absent_at_bbox")
        self.assertNotIn("fabricated", merged.pages[0].text)

    def test_noise_block_is_rejected_even_without_a_raw_index(self):
        """The plausibility check (item 4b) protects scanned pages / no-referee
        cases where positional verification (item 4a) simply isn't possible."""
        primary = self._multi_block_primary()
        fallback = ExtractionResult("pymupdf", "fallback", pages=[
            ExtractedPage(1, text_blocks=[{"text": "(cid:12)(cid:87)(cid:3) garbled extraction artifact"}])
        ])
        merger = ExtractionResultMerger(ExtractionReconciler())
        merged = merger.merge(primary, fallback)  # no raw_text_index at all
        event = next(e for e in merged.pages[0].reconciliation if e["decision"] == "fallback_only")
        self.assertEqual(event["confidence_tier"], confidence.LOW)
        self.assertEqual(event["confidence_reason"], "noise_heuristic_failed")
        self.assertNotIn("garbled extraction artifact", merged.pages[0].text)

    def test_whole_page_recovery_guard_skips_position_check(self):
        """Direct unit test of _verify_new_content's whole_page_recovery
        guard: when there's nothing to compare against (the base page was
        empty), clean content must not be penalized just because a position
        check couldn't run -- it stays at _assign_confidence's default
        (medium, new_content_unverified), never demoted to low."""
        merger = ExtractionResultMerger(ExtractionReconciler())
        base_page = ExtractedPage(1)  # empty -- nothing to compare against
        block = {"text": "Section overview and scope of this specification."}
        decision = {"decision": "fallback_only", "reason": "Primary page or slide has no readable text."}
        result = merger._verify_new_content(base_page, block, decision, raw_text_index=None, whole_page_recovery=True)
        self.assertNotIn("content_verification", result)  # guard returns unchanged, no verification attempted
        tiered = merger._assign_confidence(result)
        self.assertEqual(tiered["confidence_tier"], confidence.MEDIUM)
        self.assertEqual(tiered["confidence_reason"], "new_content_unverified")

    def test_whole_page_recovery_still_rejects_actual_noise(self):
        """The whole_page_recovery guard skips the *position* check, not the
        noise check -- garbled text is still garbled even with nothing to
        compare it against."""
        merger = ExtractionResultMerger(ExtractionReconciler())
        base_page = ExtractedPage(1)
        block = {"text": "(cid:12)(cid:87)(cid:3) garbled extraction artifact"}
        decision = {"decision": "fallback_only", "reason": "Primary page or slide has no readable text."}
        result = merger._verify_new_content(base_page, block, decision, raw_text_index=None, whole_page_recovery=True)
        self.assertEqual(result["content_verification"]["result"], "rejected")
        tiered = merger._assign_confidence(result)
        self.assertEqual(tiered["confidence_tier"], confidence.LOW)

    def test_whole_page_recovery_via_merge_never_reaches_block_level_verification(self):
        """The realistic path for "Docling found nothing on this page" is
        the page-existence/page-selection logic in merge() adopting the
        fallback page wholesale (verified: _select_base_page always picks
        the non-empty side as base) -- confirming no per-block fallback_only
        events get generated in that case, and the content passes through
        exactly as extracted, since there's nothing to reconcile it against."""
        primary = ExtractionResult("docling", "primary", pages=[ExtractedPage(1)])  # nothing at all
        fallback = ExtractionResult("pymupdf", "fallback", pages=[
            ExtractedPage(1, text_blocks=[
                {"text": "Section overview and scope of this specification."},
                {"text": "Applicable standards and reference documents."},
            ])
        ])
        merger = ExtractionResultMerger(ExtractionReconciler())
        merged = merger.merge(primary, fallback)
        events = [e for e in merged.pages[0].reconciliation if e.get("content_type") == "text"]
        self.assertEqual(events, [])  # nothing to reconcile -- the whole fallback page was adopted directly
        self.assertIn("Section overview", merged.pages[0].text)
        self.assertIn("Applicable standards", merged.pages[0].text)

    def test_scanned_page_new_content_is_medium_not_low(self):
        """A raw_text_index exists (document has some pages with text) but
        this specific page has no text layer -- position verification can't
        run, and clean content shouldn't be penalized for that."""
        primary = self._multi_block_primary()
        fallback = ExtractionResult("pdfplumber", "fallback", pages=[
            ExtractedPage(1, text_blocks=[{"text": "Recovered caption text from a scanned figure.",
                                           "bbox": {"x0": 0, "y0": 80, "x1": 200, "y1": 90}}])
        ])
        raw_index = PdfRawTextIndex(words=[])  # page 1 has zero raw words -> no text layer
        merger = ExtractionResultMerger(ExtractionReconciler())
        merged = merger.merge(primary, fallback, raw_text_index=raw_index)
        event = next(e for e in merged.pages[0].reconciliation if e["decision"] == "fallback_only")
        self.assertEqual(event["confidence_tier"], confidence.MEDIUM)
        self.assertEqual(event["confidence_reason"], "new_content_unverified")


class LowConfidenceBlockNodeTests(unittest.TestCase):
    """Confirms Finding B's fix: an excluded block gets its own canonical
    node instead of silently disappearing once it's kept out of page.text
    (which `_text_to_nodes` is the only thing that ever reads for narrative
    content)."""

    def test_excluded_block_becomes_its_own_node_and_stays_out_of_paragraphs(self):
        page = ExtractedPage(
            1,
            text="Sensor mounting guidance is provided in section two of this document.",
            text_blocks=[
                {"text": "Sensor mounting guidance is provided in section two of this document."},
                {"text": "Maximum operating temperature is 80 C for this sensor.",
                 "retrieval_allowed": False, "requires_review": True,
                 "reconciliation": {"decision": "conflict", "confidence_tier": "low", "confidence_reason": "conflict_unresolved"}},
            ],
        )
        extraction = ExtractionResult("docling+pymupdf", "quality_reconciled", pages=[page])
        source = IngestionSource("agent", SourceType.PDF, Path("spec.pdf"))
        parser = PdfDocumentParser()
        page_nodes = parser._build_fallback_pages("doc", "root", source, extraction)

        self.assertEqual(len(page_nodes), 1)
        low_nodes = [child for child in page_nodes[0].children if child.node_type == NodeType.LOW_CONFIDENCE_BLOCK]
        self.assertEqual(len(low_nodes), 1)
        self.assertEqual(low_nodes[0].text, "Maximum operating temperature is 80 C for this sensor.")
        self.assertIs(low_nodes[0].attributes["retrieval_allowed"], False)
        self.assertEqual(low_nodes[0].attributes["extraction_confidence"], "low")
        self.assertEqual(page_nodes[0].attributes["low_confidence_block_count"], 1)

        narrative_nodes = [child for child in page_nodes[0].children if child.node_type != NodeType.LOW_CONFIDENCE_BLOCK]
        self.assertTrue(narrative_nodes)
        for node in narrative_nodes:
            self.assertNotIn("80 C", node.text or "")

    def test_no_low_confidence_blocks_means_no_low_confidence_nodes(self):
        page = ExtractedPage(1, text="Everything here is trusted.", text_blocks=[{"text": "Everything here is trusted."}])
        extraction = ExtractionResult("docling", "docling_native", pages=[page])
        source = IngestionSource("agent", SourceType.PDF, Path("spec.pdf"))
        page_nodes = PdfDocumentParser()._build_fallback_pages("doc", "root", source, extraction)
        self.assertFalse([c for c in page_nodes[0].children if c.node_type == NodeType.LOW_CONFIDENCE_BLOCK])
        self.assertEqual(page_nodes[0].attributes["low_confidence_block_count"], 0)


if __name__ == "__main__":
    unittest.main()

"""Tests for Docling/PyMuPDF bbox coordinate-space normalization and the
runtime axis-agreement calibration check."""

import unittest

from backend.ingestion.extraction.bbox_calibration import audit_axis_agreement
from backend.ingestion.extraction.contracts import ExtractedPage, ExtractionResult
from backend.ingestion.extraction.docling import DoclingContentAdapter
from backend.ingestion.extraction.merger import ExtractionResultMerger
from backend.ingestion.extraction.raw_text_index import PdfRawTextIndex, RawPdfWord
from backend.ingestion.extraction.reconciliation import ExtractionReconciler


def _element(text, l, t, r, b, coord_origin=None):
    """Builds a minimal fake Docling element with one provenance bbox."""
    bbox = type("BBox", (), {"l": l, "t": t, "r": r, "b": b, "coord_origin": coord_origin})()
    provenance = type("Prov", (), {"bbox": bbox})()
    return type("Element", (), {"text": text, "prov": [provenance]})()


class DoclingBboxNormalizationTests(unittest.TestCase):
    """Exercises _normalize_docling_bbox / _element_bbox directly."""

    def setUp(self):
        self.adapter = DoclingContentAdapter()

    def test_bottom_left_provenance_is_converted_to_top_left(self):
        """A bottom-left-origin bbox with a page height gets flipped, not just relabeled."""
        element = _element("Torque spec", l=10, t=700, r=200, b=680, coord_origin="BOTTOMLEFT")
        bbox = self.adapter._element_bbox(element, page_height=792)
        self.assertEqual(bbox["coord_space"], "top_left")
        self.assertEqual(bbox["top"], 92)  # 792 - max(700, 680)
        self.assertEqual(bbox["height"], 20)
        self.assertEqual(bbox["left"], 10)
        self.assertEqual(bbox["width"], 190)

    def test_top_left_provenance_is_not_flipped(self):
        """An already-top-left bbox is passed through unchanged (just min/maxed)."""
        element = _element("Torque spec", l=10, t=92, r=200, b=112, coord_origin="TOPLEFT")
        bbox = self.adapter._element_bbox(element, page_height=792)
        self.assertEqual(bbox["coord_space"], "top_left")
        self.assertEqual(bbox["top"], 92)
        self.assertEqual(bbox["height"], 20)

    def test_missing_page_height_marks_coord_space_unknown(self):
        """A bottom-left-origin bbox with no page height to flip with is never guessed."""
        element = _element("Torque spec", l=10, t=700, r=200, b=680, coord_origin="BOTTOMLEFT")
        bbox = self.adapter._element_bbox(element, page_height=None)
        self.assertEqual(bbox["coord_space"], "unknown")

    def test_no_coord_origin_infers_from_which_edge_is_larger(self):
        """When coord_origin isn't exposed, t > b is treated as bottom-left (matches the
        existing repo convention seen in test_same_image_bbox_is_not_appended_twice)."""
        element = _element("Torque spec", l=10, t=700, r=200, b=680, coord_origin=None)
        bbox = self.adapter._element_bbox(element, page_height=792)
        self.assertEqual(bbox["source_coord_origin"], "inferred_bottom_left")
        self.assertEqual(bbox["top"], 92)


class MergerSkipsUnknownCoordSpaceTests(unittest.TestCase):
    """The verifier must never trust a bbox whose orientation is unresolved."""

    def setUp(self):
        self.merger = ExtractionResultMerger(ExtractionReconciler())

    def test_verifier_skips_unknown_coord_space(self):
        primary = ExtractionResult("docling", "primary", pages=[
            ExtractedPage(1, text="Maximum operating temperature is 90 C for this sensor.",
                          text_blocks=[{"text": "Maximum operating temperature is 90 C for this sensor.",
                                        "bbox": {"left": 0, "top": 0, "width": 300, "height": 20, "coord_space": "unknown"}}])
        ])
        fallback = ExtractionResult("pdfplumber", "fallback", pages=[
            ExtractedPage(1, text_blocks=[{"text": "Maximum operating temperature is 80 C for this sensor.",
                                           "bbox": {"x0": 0, "y0": 0, "x1": 300, "y1": 20}}])
        ])
        raw_index = PdfRawTextIndex(words=[
            RawPdfWord(1, 0, 0, 60, 10, "Maximum"), RawPdfWord(1, 65, 0, 120, 10, "operating"),
            RawPdfWord(1, 125, 0, 190, 10, "temperature"), RawPdfWord(1, 195, 0, 205, 10, "is"),
            RawPdfWord(1, 210, 0, 225, 10, "90"), RawPdfWord(1, 230, 0, 240, 10, "C"),
        ])
        merged = self.merger.merge(primary, fallback, raw_text_index=raw_index)
        event = merged.pages[0].reconciliation[-1]
        self.assertEqual(event["decision"], "conflict")
        self.assertNotIn("winning_source", event)


class AxisCalibrationAuditTests(unittest.TestCase):
    """Exercises the runtime sanity check on already-normalized bboxes."""

    def _page_with_blocks(self, blocks):
        page = ExtractedPage(1)
        page.text_blocks = blocks
        return page

    def test_agreeing_blocks_are_trusted(self):
        """A 5-block page where every bbox genuinely lands on its own text agrees."""
        raw_index = PdfRawTextIndex(words=[
            RawPdfWord(1, 0, 0, 60, 10, "Torque"), RawPdfWord(1, 65, 0, 90, 10, "spec"), RawPdfWord(1, 95, 0, 130, 10, "value"),
            RawPdfWord(1, 0, 20, 60, 30, "Volume"), RawPdfWord(1, 65, 20, 130, 30, "resistivity"), RawPdfWord(1, 135, 20, 190, 30, "reading"),
            RawPdfWord(1, 0, 40, 60, 50, "Surface"), RawPdfWord(1, 65, 40, 130, 50, "finish"), RawPdfWord(1, 135, 40, 180, 50, "grade"),
            RawPdfWord(1, 0, 60, 60, 70, "Material"), RawPdfWord(1, 65, 60, 100, 70, "code"), RawPdfWord(1, 105, 60, 160, 70, "number"),
            RawPdfWord(1, 0, 80, 60, 90, "Revision"), RawPdfWord(1, 65, 80, 90, 90, "C"), RawPdfWord(1, 95, 80, 150, 90, "update"),
        ])
        blocks = [
            {"text": "Torque spec value", "bbox": {"left": 0, "top": 0, "width": 130, "height": 10, "coord_space": "top_left"}},
            {"text": "Volume resistivity reading", "bbox": {"left": 0, "top": 20, "width": 190, "height": 10, "coord_space": "top_left"}},
            {"text": "Surface finish grade", "bbox": {"left": 0, "top": 40, "width": 180, "height": 10, "coord_space": "top_left"}},
            {"text": "Material code number", "bbox": {"left": 0, "top": 60, "width": 160, "height": 10, "coord_space": "top_left"}},
            {"text": "Revision C update", "bbox": {"left": 0, "top": 80, "width": 150, "height": 10, "coord_space": "top_left"}},
        ]
        # duplicate the fixture to clear the minimum-sample floor (10 blocks)
        page = self._page_with_blocks(blocks * 2)
        report = audit_axis_agreement([page], raw_index, sample_limit=40)
        self.assertEqual(report["verdict"], "agrees")
        self.assertTrue(report["trust_verification"])

    def test_mirrored_blocks_are_flagged(self):
        """Bboxes vertically mirrored from their real text produce a mismatch verdict."""
        raw_index = PdfRawTextIndex(words=[
            RawPdfWord(1, 0, 0, 60, 10, "Torque"), RawPdfWord(1, 65, 0, 90, 10, "spec"), RawPdfWord(1, 95, 0, 130, 10, "value"),
            RawPdfWord(1, 0, 20, 60, 30, "Volume"), RawPdfWord(1, 65, 20, 130, 30, "resistivity"), RawPdfWord(1, 135, 20, 190, 30, "reading"),
            RawPdfWord(1, 0, 40, 60, 50, "Surface"), RawPdfWord(1, 65, 40, 130, 50, "finish"), RawPdfWord(1, 135, 40, 180, 50, "grade"),
            RawPdfWord(1, 0, 60, 60, 70, "Material"), RawPdfWord(1, 65, 60, 100, 70, "code"), RawPdfWord(1, 105, 60, 160, 70, "number"),
            RawPdfWord(1, 0, 80, 60, 90, "Revision"), RawPdfWord(1, 65, 80, 90, 90, "C"), RawPdfWord(1, 95, 80, 150, 90, "update"),
        ])
        # Each block's bbox is offset to a *different* row than its own text
        # (simulating a systematic vertical mirror), so none should match.
        blocks = [
            {"text": "Torque spec value", "bbox": {"left": 0, "top": 80, "width": 130, "height": 10, "coord_space": "top_left"}},
            {"text": "Volume resistivity reading", "bbox": {"left": 0, "top": 60, "width": 190, "height": 10, "coord_space": "top_left"}},
            {"text": "Surface finish grade", "bbox": {"left": 0, "top": 0, "width": 180, "height": 10, "coord_space": "top_left"}},
            {"text": "Material code number", "bbox": {"left": 0, "top": 20, "width": 160, "height": 10, "coord_space": "top_left"}},
            {"text": "Revision C update", "bbox": {"left": 0, "top": 40, "width": 150, "height": 10, "coord_space": "top_left"}},
        ]
        page = self._page_with_blocks(blocks * 2)
        report = audit_axis_agreement([page], raw_index, sample_limit=40)
        self.assertEqual(report["verdict"], "mismatch_suspected")
        self.assertFalse(report["trust_verification"])

    def test_small_sample_defaults_to_trusting_verification(self):
        """Fewer than the minimum sample size never disables verification on a hunch."""
        raw_index = PdfRawTextIndex(words=[RawPdfWord(1, 0, 0, 60, 10, "Torque"), RawPdfWord(1, 65, 0, 90, 10, "spec")])
        blocks = [{"text": "Completely unrelated wording here", "bbox": {"left": 0, "top": 0, "width": 90, "height": 10, "coord_space": "top_left"}}]
        page = self._page_with_blocks(blocks)
        report = audit_axis_agreement([page], raw_index, sample_limit=40)
        self.assertEqual(report["verdict"], "insufficient_sample")
        self.assertTrue(report["trust_verification"])


if __name__ == "__main__":
    unittest.main()

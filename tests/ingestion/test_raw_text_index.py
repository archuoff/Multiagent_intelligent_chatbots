"""Tests for position-indexed PDF raw text verification."""

import unittest

from backend.ingestion.extraction.raw_text_index import PdfRawTextIndex
from backend.ingestion.extraction.raw_text_index import RawPdfWord


class PdfRawTextIndexTests(unittest.TestCase):
    """Checks deterministic bbox-scoped text lookup without needing a real PDF."""

    def test_text_at_bbox_returns_only_overlapping_words_in_reading_order(self):
        """Region lookup must not use unrelated words elsewhere on the page."""
        index = PdfRawTextIndex(words=[
            RawPdfWord(1, 10, 10, 30, 20, "Volume"),
            RawPdfWord(1, 35, 10, 70, 20, "Resistivity"),
            RawPdfWord(1, 80, 10, 100, 20, "10^12"),
            RawPdfWord(1, 10, 90, 50, 100, "Unrelated"),
        ])
        self.assertEqual(index.text_at_bbox(1, {"x0": 5, "y0": 5, "x1": 110, "y1": 25}), "Volume Resistivity 10^12")

    def test_match_candidates_resolves_exactly_one_candidate(self):
        """A conflict is auto-resolved only when one candidate appears in-region."""
        index = PdfRawTextIndex(words=[
            RawPdfWord(1, 10, 10, 40, 20, "Surface"),
            RawPdfWord(1, 45, 10, 80, 20, "Resistivity"),
            RawPdfWord(1, 85, 10, 110, 20, "10^13"),
        ])
        result = index.match_candidates(1, {"left": 0, "top": 0, "right": 120, "bottom": 30},
            "Surface Resistivity 101133", "Surface Resistivity 10^13")
        self.assertEqual(result.winner, "B")
        self.assertEqual(result.method, "raw_text_position_match")
        self.assertEqual(result.confidence, "high")

    def test_match_candidates_is_inconclusive_when_both_or_neither_match(self):
        """Ambiguous regions must fall through to review/VLM instead of guessing."""
        index = PdfRawTextIndex(words=[RawPdfWord(1, 10, 10, 50, 20, "Density")])
        both = index.match_candidates(1, {"x0": 0, "y0": 0, "x1": 60, "y1": 30}, "Density", "Density")
        neither = index.match_candidates(1, {"x0": 0, "y0": 0, "x1": 60, "y1": 30}, "Temperature", "Resistivity")
        self.assertIsNone(both.winner)
        self.assertIsNone(neither.winner)
        self.assertEqual(both.method, "raw_text_position_inconclusive")

    def test_numeric_candidate_does_not_match_inside_larger_token(self):
        """Boundary-safe matching prevents 80 from matching inside 1980."""
        index = PdfRawTextIndex(words=[RawPdfWord(1, 10, 10, 50, 20, "1980")])
        result = index.match_candidates(1, {"x0": 0, "y0": 0, "x1": 60, "y1": 30}, "80", "90")
        self.assertIsNone(result.winner)
        self.assertFalse(result.candidate_a_matches)

    def test_engine_used_is_provided_for_explicit_word_lists(self):
        """Test/synthetic word lists are labeled 'provided', never a real engine name."""
        index = PdfRawTextIndex(words=[RawPdfWord(1, 0, 0, 10, 10, "x")])
        self.assertEqual(index.engine_used, "provided")
        self.assertFalse(index.is_independent_of("provided"))
        self.assertTrue(index.is_independent_of("pymupdf"))

    def test_is_independent_of_before_any_page_loaded(self):
        """Independence is unknown (False, not a guess) until a page has actually loaded."""
        index = PdfRawTextIndex("does-not-exist.pdf")
        self.assertIsNone(index.engine_used)
        self.assertFalse(index.is_independent_of("pymupdf"))


class PdfRawTextIndexEngineSelectionTests(unittest.TestCase):
    """Checks word-source selection and coordinate translation with fake engine modules."""

    def setUp(self):
        import backend.ingestion.extraction.raw_text_index as module
        self.module = module
        self._original_pdfplumber = module.pdfplumber
        self._original_fitz = module.fitz

    def tearDown(self):
        self.module.pdfplumber = self._original_pdfplumber
        self.module.fitz = self._original_fitz

    def test_pdfplumber_words_are_translated_to_cropbox_origin(self):
        """pdfplumber's MediaBox-relative coordinates must be shifted to CropBox
        origin so they land in the same space as PyMuPDF's coordinates --
        without this, a document whose CropBox differs from its MediaBox
        (common with bleed/trim boxes) would silently mis-position every word."""
        fake_word = {"text": "Torque", "x0": 120.0, "top": 220.0, "x1": 160.0, "bottom": 230.0}
        fake_page = type("Page", (), {
            "cropbox": (100.0, 200.0, 500.0, 700.0),
            "extract_words": lambda self, **kwargs: [fake_word],
        })()
        fake_document = type("Document", (), {"pages": [fake_page], "close": lambda self: None})()
        self.module.pdfplumber = type("FakePdfplumber", (), {"open": staticmethod(lambda path: fake_document)})()

        index = PdfRawTextIndex("fake.pdf", engine="pdfplumber")
        # word is at x0=120,top=220 in MediaBox space; cropbox origin is (100,200),
        # so it must appear at x0=20,y0=20 in the index's (CropBox-relative) space.
        region_at_translated_position = index.text_at_bbox(1, {"x0": 0, "y0": 0, "x1": 100, "y1": 50})
        region_at_raw_mediabox_position = index.text_at_bbox(1, {"x0": 115, "y0": 215, "x1": 165, "y1": 235})
        self.assertEqual(region_at_translated_position, "Torque")
        self.assertEqual(region_at_raw_mediabox_position, "")
        self.assertEqual(index.engine_used, "pdfplumber")

    def test_engine_auto_falls_back_to_pymupdf_when_pdfplumber_missing(self):
        """A missing pdfplumber library degrades to PyMuPDF, never raises."""
        self.module.pdfplumber = None
        fake_page = type("Page", (), {
            "get_text": lambda self, mode, sort=False: [(0.0, 0.0, 30.0, 10.0, "Torque", 0, 0, 0)],
        })()
        fake_document = type("Document", (), {
            "page_count": 1,
            "__getitem__": lambda self, index: fake_page,
            "close": lambda self: None,
        })()
        self.module.fitz = type("FakeFitz", (), {"open": staticmethod(lambda path: fake_document)})()

        index = PdfRawTextIndex("fake.pdf", engine="auto")
        region = index.text_at_bbox(1, {"x0": 0, "y0": 0, "x1": 50, "y1": 20})
        self.assertEqual(region, "Torque")
        self.assertEqual(index.engine_used, "pymupdf")


if __name__ == "__main__":
    unittest.main()

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


if __name__ == "__main__":
    unittest.main()

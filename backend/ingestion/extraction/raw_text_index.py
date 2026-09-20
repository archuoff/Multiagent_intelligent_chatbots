"""Position-indexed PDF text lookup for deterministic conflict verification.

This module intentionally reads raw PDF words without paragraph/table
interpretation. It is used as a cheap referee when two extractors disagree:
check the disputed region's actual text layer before using AI or human review.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from backend.ingestion.utils import normalize_text

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - environment dependent.
    fitz = None


@dataclass(frozen=True, slots=True)
class RawPdfWord:
    """One raw PDF word with source-page coordinates."""

    page_number: int
    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    block_number: int = 0
    line_number: int = 0
    word_number: int = 0


@dataclass(frozen=True, slots=True)
class CandidateMatch:
    """Reports whether disputed candidates appear in the source region text."""

    winner: str | None
    method: str
    region_text: str
    candidate_a_matches: bool
    candidate_b_matches: bool
    confidence: str


class PdfRawTextIndex:
    """Indexes raw PDF words by page and bbox without semantic interpretation."""

    _TOKEN_PATTERN = re.compile(r"[a-z0-9]+(?:[./_^-][a-z0-9]+)*", re.IGNORECASE)

    def __init__(self, source_path: str | Path | None = None, *, words: list[RawPdfWord] | None = None) -> None:
        """Builds an index from a PDF path or explicit test words."""
        if words is not None:
            self._words = words
        elif source_path is not None:
            self._words = self._load_words(source_path)
        else:
            self._words = []

    def page_has_text_layer(self, page_number: int) -> bool:
        """Returns whether the page exposes any raw digital text words."""
        return any(word.page_number == page_number and normalize_text(word.text) for word in self._words)

    def text_at_bbox(self, page_number: int, bbox: dict[str, Any], *, tolerance: float = 2.0) -> str:
        """Returns raw words overlapping the bbox in reading order."""
        normalized = self._normalize_bbox(bbox)
        if normalized is None:
            return ""
        left, top, right, bottom = normalized
        matched = [
            word for word in self._words
            if word.page_number == page_number
            and self._overlaps(
                (word.x0, word.y0, word.x1, word.y1),
                (left - tolerance, top - tolerance, right + tolerance, bottom + tolerance),
            )
        ]
        matched.sort(key=lambda word: (round(word.y0, 1), word.x0, word.block_number, word.line_number, word.word_number))
        return normalize_text(" ".join(word.text for word in matched)) or ""

    def match_candidates(self, page_number: int, bbox: dict[str, Any], candidate_a: str, candidate_b: str) -> CandidateMatch:
        """Chooses a candidate only when exactly one appears in the raw region text."""
        region_text = self.text_at_bbox(page_number, bbox)
        a_matches = self._contains_candidate(region_text, candidate_a)
        b_matches = self._contains_candidate(region_text, candidate_b)
        if a_matches ^ b_matches:
            return CandidateMatch(
                winner="A" if a_matches else "B",
                method="raw_text_position_match",
                region_text=region_text,
                candidate_a_matches=a_matches,
                candidate_b_matches=b_matches,
                confidence="high",
            )
        return CandidateMatch(
            winner=None,
            method="raw_text_position_inconclusive",
            region_text=region_text,
            candidate_a_matches=a_matches,
            candidate_b_matches=b_matches,
            confidence="low",
        )

    def _load_words(self, source_path: str | Path) -> list[RawPdfWord]:
        """Loads PyMuPDF word tuples using only raw coordinates and text."""
        if fitz is None:
            raise RuntimeError("PyMuPDF is required for PDF raw text indexing.")
        words: list[RawPdfWord] = []
        with fitz.open(str(source_path)) as document:
            for page_number, page in enumerate(document, start=1):
                for item in page.get_text("words", sort=False):
                    if len(item) < 5:
                        continue
                    text = normalize_text(str(item[4])) or ""
                    if not text:
                        continue
                    words.append(RawPdfWord(
                        page_number=page_number,
                        x0=float(item[0]),
                        y0=float(item[1]),
                        x1=float(item[2]),
                        y1=float(item[3]),
                        text=text,
                        block_number=int(item[5]) if len(item) > 5 and type(item[5]) is int else 0,
                        line_number=int(item[6]) if len(item) > 6 and type(item[6]) is int else 0,
                        word_number=int(item[7]) if len(item) > 7 and type(item[7]) is int else 0,
                    ))
        return words

    def _contains_candidate(self, region_text: str, candidate: str) -> bool:
        """Matches candidate text after normalization and token fallback."""
        region = normalize_text(region_text) or ""
        value = normalize_text(candidate) or ""
        if not region or not value:
            return False
        if value.casefold() in region.casefold():
            return True
        region_tokens = self._TOKEN_PATTERN.findall(region.casefold())
        candidate_tokens = self._TOKEN_PATTERN.findall(value.casefold())
        if not candidate_tokens or len(candidate_tokens) > len(region_tokens):
            return False
        width = len(candidate_tokens)
        return any(region_tokens[index:index + width] == candidate_tokens for index in range(len(region_tokens) - width + 1))

    def _normalize_bbox(self, bbox: dict[str, Any]) -> tuple[float, float, float, float] | None:
        """Accepts common bbox shapes: x0/y0/x1/y1, left/top/right/bottom, or left/top/width/height."""
        if not isinstance(bbox, dict):
            return None
        if all(isinstance(bbox.get(key), (int, float)) for key in ("x0", "y0", "x1", "y1")):
            return (float(bbox["x0"]), float(bbox["y0"]), float(bbox["x1"]), float(bbox["y1"]))
        if all(isinstance(bbox.get(key), (int, float)) for key in ("left", "top", "right", "bottom")):
            return (float(bbox["left"]), float(bbox["top"]), float(bbox["right"]), float(bbox["bottom"]))
        if all(isinstance(bbox.get(key), (int, float)) for key in ("left", "top", "width", "height")):
            return (float(bbox["left"]), float(bbox["top"]), float(bbox["left"] + bbox["width"]), float(bbox["top"] + bbox["height"]))
        return None

    def _overlaps(self, first: tuple[float, float, float, float], second: tuple[float, float, float, float]) -> bool:
        """Checks rectangle overlap in PDF coordinate space."""
        first_left, first_top, first_right, first_bottom = first
        second_left, second_top, second_right, second_bottom = second
        return first_left <= second_right and first_right >= second_left and first_top <= second_bottom and first_bottom >= second_top

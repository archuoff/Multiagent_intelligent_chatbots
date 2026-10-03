"""Position-indexed PDF text lookup for deterministic conflict verification.

This module intentionally reads raw PDF words without paragraph/table
interpretation. It is used as a cheap referee when two extractors disagree:
check the disputed region's actual text layer before using AI or human review.

Word source selection matters for independence: when the fallback extractor
being verified is itself PyMuPDF-based, a PyMuPDF-word referee is not fully
independent of it (though it still checks PyMuPDF's *word-layer* output
against its own *block-layer* grouping, which catches real grouping/ordering
mistakes). Preferring pdfplumber (a different library, different content
stream decoder) gives a genuinely independent second opinion whenever it's
available; PyMuPDF remains the automatic fallback if pdfplumber can't be
used, so this referee never raises just because one library is missing.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any

from backend.ingestion.utils import normalize_text

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover - environment dependent.
    fitz = None

try:
    import pdfplumber
except ImportError:  # pragma: no cover - environment dependent.
    pdfplumber = None


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

    def __init__(
        self,
        source_path: str | Path | None = None,
        *,
        words: list[RawPdfWord] | None = None,
        engine: str = "auto",
    ) -> None:
        """Builds an index from a PDF path (loaded lazily, per page, on first
        use) or an explicit test word list. `engine` selects the word
        source: "auto" prefers pdfplumber and falls back to PyMuPDF per page
        if pdfplumber fails or is unavailable; "pdfplumber" and "pymupdf"
        force one source (still falling back to the other rather than
        raising, since a missing referee should degrade a conflict to
        unverified, not crash ingestion)."""
        self._source_path = Path(source_path) if source_path is not None else None
        self._requested_engine = engine
        self._engine_used: str | None = None
        self._words_by_page: dict[int, list[RawPdfWord]] = {}
        self._eager = words is not None
        self._pdfplumber_doc: Any = None
        self._pymupdf_doc: Any = None
        if words is not None:
            for word in words:
                self._words_by_page.setdefault(word.page_number, []).append(word)
            self._engine_used = "provided"

    @property
    def engine_used(self) -> str | None:
        """The word source actually used so far ("pdfplumber", "pymupdf",
        "provided", or None if no page has been loaded yet)."""
        return self._engine_used

    def is_independent_of(self, extractor_name: str) -> bool:
        """Returns whether this referee's word source is a different library
        than the extractor being verified. A PyMuPDF fallback extraction
        verified by a PyMuPDF-word referee is not independent; anything
        verified by pdfplumber words is."""
        if not self._engine_used:
            return False
        return self._engine_used != extractor_name

    def close(self) -> None:
        """Releases any open document handles. Safe to call multiple times."""
        for attribute in ("_pdfplumber_doc", "_pymupdf_doc"):
            handle = getattr(self, attribute, None)
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
                setattr(self, attribute, None)

    def __enter__(self) -> "PdfRawTextIndex":
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def page_has_text_layer(self, page_number: int) -> bool:
        """Returns whether the page exposes any raw digital text words."""
        self._ensure_page(page_number)
        return any(normalize_text(word.text) for word in self._words_by_page.get(page_number, []))

    def text_at_bbox(self, page_number: int, bbox: dict[str, Any], *, tolerance: float = 2.0) -> str:
        """Returns raw words overlapping the bbox in reading order."""
        normalized = self._normalize_bbox(bbox)
        if normalized is None:
            return ""
        self._ensure_page(page_number)
        left, top, right, bottom = normalized
        matched = [
            word for word in self._words_by_page.get(page_number, [])
            if self._overlaps(
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

    def token_coverage_at_bbox(self, page_number: int, bbox: dict[str, Any], text: str, *, tolerance: float = 3.0) -> float:
        """Returns the fraction of `text`'s own wording found in the raw
        region at bbox -- a looser measure than `_contains_candidate`'s exact
        contiguous-sequence match, appropriate for checking a whole new-content
        block against a region assembled from independently-ordered raw words."""
        region_text = self.text_at_bbox(page_number, bbox, tolerance=tolerance)
        return self._token_overlap(text, region_text)

    def _token_overlap(self, candidate: str, region: str) -> float:
        """Multiset token overlap of candidate against region, as a fraction of candidate's own tokens."""
        candidate_tokens = Counter(self._TOKEN_PATTERN.findall((normalize_text(candidate) or "").casefold()))
        region_tokens = Counter(self._TOKEN_PATTERN.findall((normalize_text(region) or "").casefold()))
        total = sum(candidate_tokens.values())
        if not total:
            return 0.0
        shared = sum(min(candidate_tokens[token], count) for token, count in region_tokens.items())
        return shared / total

    def _ensure_page(self, page_number: int) -> None:
        """Loads one page's words on first access; a no-op for eager/test construction."""
        if self._eager or self._source_path is None or page_number in self._words_by_page:
            return
        words, engine = self._load_page(page_number)
        self._words_by_page[page_number] = words
        if engine:
            self._engine_used = self._engine_used or engine

    def _load_page(self, page_number: int) -> tuple[list[RawPdfWord], str | None]:
        """Tries the requested engine first, always keeping the other as a
        silent fallback -- a missing/broken library degrades this page to
        unverified, it never raises out of ingestion."""
        if self._requested_engine == "pymupdf":
            order = ("pymupdf", "pdfplumber")
        else:
            order = ("pdfplumber", "pymupdf")
        for engine in order:
            try:
                words = self._load_page_pdfplumber(page_number) if engine == "pdfplumber" else self._load_page_pymupdf(page_number)
            except Exception:
                words = None
            if words is not None:
                return words, engine
        return [], None

    def _pdfplumber_document(self) -> Any:
        if pdfplumber is None:
            raise RuntimeError("pdfplumber is not installed.")
        if self._pdfplumber_doc is None:
            self._pdfplumber_doc = pdfplumber.open(str(self._source_path))
        return self._pdfplumber_doc

    def _pymupdf_document(self) -> Any:
        if fitz is None:
            raise RuntimeError("PyMuPDF is not installed.")
        if self._pymupdf_doc is None:
            self._pymupdf_doc = fitz.open(str(self._source_path))
        return self._pymupdf_doc

    def _load_page_pdfplumber(self, page_number: int) -> list[RawPdfWord] | None:
        """Loads one page's words via pdfplumber, translated from its
        MediaBox-relative coordinates into CropBox-relative ones -- PyMuPDF's
        coordinates are CropBox-relative, so without this translation the two
        libraries disagree on where (0, 0) is whenever a document's CropBox
        differs from its MediaBox (common on print-ready documents with
        bleed/trim boxes), silently reintroducing an axis-style mismatch."""
        document = self._pdfplumber_document()
        if page_number - 1 < 0 or page_number - 1 >= len(document.pages):
            return []
        page = document.pages[page_number - 1]
        origin_x, origin_y = page.cropbox[0], page.cropbox[1]
        words: list[RawPdfWord] = []
        for index, word in enumerate(page.extract_words(use_text_flow=False, keep_blank_chars=False)):
            text = normalize_text(str(word.get("text", ""))) or ""
            if not text:
                continue
            words.append(RawPdfWord(
                page_number=page_number,
                x0=float(word["x0"]) - origin_x, y0=float(word["top"]) - origin_y,
                x1=float(word["x1"]) - origin_x, y1=float(word["bottom"]) - origin_y,
                text=text, block_number=0, line_number=0, word_number=index,
            ))
        return words

    def _load_page_pymupdf(self, page_number: int) -> list[RawPdfWord] | None:
        """Loads one page's raw word tuples using only PyMuPDF's coordinates and text."""
        document = self._pymupdf_document()
        if page_number - 1 < 0 or page_number - 1 >= document.page_count:
            return []
        page = document[page_number - 1]
        words: list[RawPdfWord] = []
        for item in page.get_text("words", sort=False):
            if len(item) < 5:
                continue
            text = normalize_text(str(item[4])) or ""
            if not text:
                continue
            words.append(RawPdfWord(
                page_number=page_number,
                x0=float(item[0]), y0=float(item[1]), x1=float(item[2]), y1=float(item[3]),
                text=text,
                block_number=int(item[5]) if len(item) > 5 and type(item[5]) is int else 0,
                line_number=int(item[6]) if len(item) > 6 and type(item[6]) is int else 0,
                word_number=int(item[7]) if len(item) > 7 and type(item[7]) is int else 0,
            ))
        return words

    def _contains_candidate(self, region_text: str, candidate: str) -> bool:
        """Matches candidate text as a token sequence, never as a loose substring."""
        region = normalize_text(region_text) or ""
        value = normalize_text(candidate) or ""
        if not region or not value:
            return False
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

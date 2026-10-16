"""Diagnose whether Docling's PDF bbox coordinates and PyMuPDF's raw word
coordinates share the same axis/origin convention.

This is a standalone diagnostic tool, not part of the ingestion pipeline. It
requires the real `docling` package to be installed and importable -- it is
not installed in every environment (notably not in CI/review sandboxes), so
this script must be run manually against real PDFs before trusting any
bbox-based logic in backend/ingestion/extraction/{docling.py,
raw_text_index.py, merger.py}.

Usage:
    python -m tests.ingestion.tools.diagnose_bbox_axis <file.pdf> [--pages 1,2,3] [--json]

For each sampled Docling text element with a bbox, this compares the raw
PyMuPDF word text found at that bbox as-is versus vertically flipped
(page_height - y), and scores which orientation better matches Docling's own
text for that element (token overlap of the element's text against the raw
region text found at each candidate bbox).

A page only contributes to the count if it has at least 3 distinct text
elements with a bbox (fewer makes the axis question ambiguous on that page),
and an element is skipped if its bbox sits within 10% of the page's vertical
center, since a near-centered box is close to flip-invariant and would
artificially inflate the tie count. The reported skip counts let you judge
whether the verdict's sample size is trustworthy.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re


_TOKEN_PATTERN = re.compile(r"[a-z0-9]+", re.IGNORECASE)


def _tokens(text: str) -> list[str]:
    """Tokenizes for a reproducible, dependency-free overlap comparison."""
    return _TOKEN_PATTERN.findall((text or "").casefold())


def _overlap_score(candidate_text: str, region_text: str) -> float:
    """Scores how much of the candidate's wording is present in the region."""
    candidate = Counter(_tokens(candidate_text))
    region = Counter(_tokens(region_text))
    total = sum(candidate.values())
    if not total:
        return 0.0
    shared = sum(min(candidate[token], count) for token, count in region.items())
    return shared / total


def _element_text(element) -> str:
    """Mirrors docling.py::DoclingContentAdapter._element_text's attribute order."""
    for attribute in ("text", "content", "value"):
        value = getattr(element, attribute, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _page_number(element) -> int | None:
    """Mirrors docling.py::DoclingContentAdapter._page_number exactly."""
    provenance = getattr(element, "prov", None)
    if not provenance:
        return None
    page_number = getattr(provenance[0], "page_no", None)
    return page_number if isinstance(page_number, int) and page_number > 0 else None


def _element_bbox_raw(element) -> dict | None:
    """Reads Docling's raw l/t/r/b and coord_origin, unflipped and un-min/maxed --
    deliberately keeps more information than docling.py::_element_bbox does,
    since that function's min()/max() normalization is exactly what this
    script needs to look underneath to test the axis hypothesis."""
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
    origin = getattr(bbox, "coord_origin", None)
    return {
        "l": float(left), "t": float(top), "r": float(right), "b": float(bottom),
        "coord_origin": str(origin) if origin is not None else None,
    }


def diagnose(pdf_path: str | Path, page_numbers: set[int] | None = None) -> dict:
    """Runs the as-is-vs-flipped comparison and returns a verdict report."""
    from docling.document_converter import DocumentConverter

    from backend.ingestion.extraction.raw_text_index import PdfRawTextIndex

    converter = DocumentConverter()
    result = converter.convert(str(pdf_path))
    document = result.document

    raw_index = PdfRawTextIndex(pdf_path)

    page_heights: dict[int, float] = {}
    for number, page_item in getattr(document, "pages", {}).items():
        size = getattr(page_item, "size", None)
        height = getattr(size, "height", None) if size is not None else None
        if isinstance(height, (int, float)):
            page_heights[int(number)] = float(height)

    by_page: dict[int, list[tuple[str, dict]]] = {}
    coord_origins_seen: set[str] = set()
    for item in document.iterate_items():
        element, _level = item if isinstance(item, tuple) else (item, 0)
        page_number = _page_number(element)
        if page_number is None:
            continue
        if page_numbers is not None and page_number not in page_numbers:
            continue
        text = _element_text(element)
        bbox = _element_bbox_raw(element)
        if not text or bbox is None:
            continue
        by_page.setdefault(page_number, []).append((text, bbox))
        if bbox["coord_origin"]:
            coord_origins_seen.add(bbox["coord_origin"])

    total_checked = 0
    total_skipped_ambiguous = 0
    total_skipped_small_page = 0
    total_skipped_no_page_height = 0
    as_is_wins = 0
    flipped_wins = 0
    ties = 0

    for page_number, elements in by_page.items():
        if len(elements) < 3:
            total_skipped_small_page += len(elements)
            continue
        page_height = page_heights.get(page_number)
        if not page_height:
            total_skipped_no_page_height += len(elements)
            continue
        for text, bbox in elements:
            l, t, r, b = bbox["l"], bbox["t"], bbox["r"], bbox["b"]
            as_is = {"left": min(l, r), "top": min(t, b), "width": abs(r - l), "height": abs(t - b)}
            box_center = as_is["top"] + as_is["height"] / 2
            if abs(box_center - page_height / 2) <= 0.1 * page_height:
                total_skipped_ambiguous += 1
                continue
            flipped = {"left": as_is["left"], "top": page_height - max(t, b), "width": as_is["width"], "height": as_is["height"]}

            region_as_is = raw_index.text_at_bbox(page_number, as_is, tolerance=2.0)
            region_flipped = raw_index.text_at_bbox(page_number, flipped, tolerance=2.0)
            score_as_is = _overlap_score(text, region_as_is)
            score_flipped = _overlap_score(text, region_flipped)

            total_checked += 1
            if score_as_is > score_flipped:
                as_is_wins += 1
            elif score_flipped > score_as_is:
                flipped_wins += 1
            else:
                ties += 1

    if total_checked < 10:
        verdict = "INCONCLUSIVE (fewer than 10 comparable elements found -- try more/different pages)"
    elif flipped_wins > as_is_wins * 2:
        verdict = "BOTTOM_LEFT (flip required -- see docling.py::_element_bbox)"
    elif as_is_wins > flipped_wins * 2:
        verdict = "TOP_LEFT (no change needed)"
    else:
        verdict = "INCONCLUSIVE (as-is and flipped scores too close to call)"

    return {
        "pdf": str(pdf_path),
        "coord_origin_values_seen": sorted(coord_origins_seen) or ["(not exposed by this docling version)"],
        "elements_checked": total_checked,
        "as_is_wins": as_is_wins,
        "flipped_wins": flipped_wins,
        "ties": ties,
        "skipped_ambiguous_center": total_skipped_ambiguous,
        "skipped_page_too_few_elements": total_skipped_small_page,
        "skipped_no_page_height": total_skipped_no_page_height,
        "verdict": verdict,
    }


def main() -> int:
    """Parses arguments and prints the diagnostic report."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("pdf", help="Path to a real PDF with a digital text layer.")
    parser.add_argument("--pages", help="Comma-separated 1-based page numbers to check (default: all pages).")
    parser.add_argument("--json", action="store_true", help="Print raw JSON instead of a formatted summary.")
    args = parser.parse_args()

    try:
        import docling  # noqa: F401
    except ImportError:
        raise SystemExit(
            "docling is not installed in this environment. This diagnostic must be run "
            "somewhere with the real `docling` package available -- the axis question "
            "cannot be answered without it. Install with: pip install docling"
        )

    page_numbers = {int(value) for value in args.pages.split(",")} if args.pages else None
    report = diagnose(args.pdf, page_numbers)

    if args.json:
        print(json.dumps(report, indent=2))
        return 0

    print(f"PDF: {report['pdf']}")
    print(f"Docling coord_origin values observed: {', '.join(report['coord_origin_values_seen'])}")
    print(
        f"Elements checked: {report['elements_checked']} "
        f"(skipped {report['skipped_ambiguous_center']} as vertically-ambiguous, "
        f"{report['skipped_page_too_few_elements']} on pages with <3 elements, "
        f"{report['skipped_no_page_height']} with no page height available)"
    )
    print(f"as-is wins: {report['as_is_wins']}  flipped wins: {report['flipped_wins']}  ties: {report['ties']}")
    print(f"VERDICT: {report['verdict']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

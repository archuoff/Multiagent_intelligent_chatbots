"""Per-document runtime sanity check for extracted PDF text-block bboxes.

This does not try to detect or correct a Docling/PyMuPDF axis mismatch --
that requires a real Docling install and is handled by the standalone
`tests/ingestion/tools/diagnose_bbox_axis.py` diagnostic, run manually
against real documents. This module answers a narrower, cheaper question at
ingest time: do the bboxes we already normalized actually land on their own
text in the source document?

If a large fraction of sampled blocks show almost no overlap with the raw
text at their own claimed bbox, that's a strong signal something is
systematically wrong -- most plausibly an axis mismatch that slipped past
the best-effort flip in `docling.py::_normalize_docling_bbox` (e.g. a
Docling version that reports bottom-left-origin bboxes without exposing a
`coord_origin` attribute for our heuristic to detect). The response is to
disable position verification for that document, never to silently
auto-correct it: a mis-aimed "fix" attempted at runtime with no ground truth
is worse than degrading to "unverified", which the confidence-tier scheme
already expresses safely.
"""

from __future__ import annotations

from collections import Counter
import re
from typing import Any

from backend.ingestion.extraction.contracts import ExtractedPage
from backend.ingestion.extraction.raw_text_index import PdfRawTextIndex
from backend.ingestion.utils import normalize_text

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+", re.IGNORECASE)

SAMPLE_LIMIT = 40
MIN_SAMPLE_FOR_VERDICT = 10
LOW_OVERLAP_THRESHOLD = 0.2
LOW_OVERLAP_RATIO_LIMIT = 0.3


def _tokens(text: str) -> list[str]:
    """Tokenizes for a dependency-free, reproducible overlap comparison."""
    return _TOKEN_PATTERN.findall((text or "").casefold())


def _overlap_score(block_text: str, region_text: str) -> float:
    """Scores how much of the block's own wording is present in its own region."""
    block_tokens = Counter(_tokens(block_text))
    region_tokens = Counter(_tokens(region_text))
    total = sum(block_tokens.values())
    if not total:
        return 0.0
    shared = sum(min(block_tokens[token], count) for token, count in region_tokens.items())
    return shared / total


def audit_axis_agreement(
    pages: list[ExtractedPage],
    raw_index: PdfRawTextIndex,
    *,
    sample_limit: int = SAMPLE_LIMIT,
) -> dict[str, Any]:
    """Samples top-left-space blocks and checks they land on their own text.

    Only blocks already marked `coord_space == "top_left"` are sampled --
    blocks marked "unknown" are already excluded from verification elsewhere
    and don't need auditing here. Returns `trust_verification: False` only
    when there's both a real sample size and a real disagreement rate; a
    small or ambiguous sample defaults to trusting verification rather than
    disabling it on a hunch.
    """
    checked = 0
    low_overlap = 0
    for page in pages:
        if checked >= sample_limit:
            break
        for block in page.text_blocks:
            if checked >= sample_limit:
                break
            bbox = block.get("bbox")
            if not isinstance(bbox, dict) or bbox.get("coord_space") != "top_left":
                continue
            text = normalize_text(str(block.get("text", ""))) or ""
            if len(_tokens(text)) < 3:
                continue
            region = raw_index.text_at_bbox(page.number, bbox, tolerance=2.0)
            score = _overlap_score(text, region)
            checked += 1
            if score < LOW_OVERLAP_THRESHOLD:
                low_overlap += 1

    if checked < MIN_SAMPLE_FOR_VERDICT:
        return {
            "checked": checked, "low_overlap": low_overlap,
            "verdict": "insufficient_sample", "trust_verification": True,
        }
    ratio = low_overlap / checked
    trust = ratio < LOW_OVERLAP_RATIO_LIMIT
    return {
        "checked": checked, "low_overlap": low_overlap,
        "low_overlap_ratio": round(ratio, 3),
        "verdict": "agrees" if trust else "mismatch_suspected",
        "trust_verification": trust,
    }

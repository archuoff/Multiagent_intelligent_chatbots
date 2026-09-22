"""Confidence tier vocabulary for extraction reconciliation events.

Every reconciliation event -- and, downstream, every canonical node built
from PDF extraction -- carries an explicit confidence tier alongside its
decision category. A decision label like "conflict_resolved" or
"fallback_only" only says *what kind* of outcome occurred; it doesn't say
*how sure* the pipeline is about it. Two outcomes that look identical as a
bare decision label ("nothing flagged here") can mean very different
things -- "we checked this against the real source and it's fine" versus
"nothing could confirm this either way" -- and only the confidence tier
distinguishes them. This is what lets the document-level gate
(validation/quality.py) make a real judgment call instead of treating any
absence of a red flag as proof of correctness.

This module intentionally does not overload `CanonicalNode.confidence`
(models.py) -- that field is an existing, unrelated float used elsewhere in
the codebase (structural completeness scoring) and conflating the two would
produce confusing double meanings for the same attribute name.
"""

from __future__ import annotations

HIGH = "high"
MEDIUM = "medium"
LOW = "low"

_ORDER = {LOW: 0, MEDIUM: 1, HIGH: 2}


def weakest(*tiers: str) -> str:
    """Combines several tiers into the weakest one -- a node's overall
    confidence can never exceed the confidence of its least-certain piece
    of evidence. Unrecognized/missing tiers are treated as LOW rather than
    silently ignored, since an untagged event is exactly the "nothing
    confirmed this" case this vocabulary exists to make visible."""
    known = [tier for tier in tiers if tier in _ORDER]
    if not known:
        return LOW
    return min(known, key=lambda tier: _ORDER[tier])

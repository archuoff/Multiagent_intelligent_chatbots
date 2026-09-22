"""Minimal query normalization before embedding.

No LLM call: resolved_query/sub_queries are already clean natural language
(vector search doesn't need keyword extraction), and no field beyond
agent_id is filterable in Qdrant yet, so there's nothing for a heavier
rewrite step to do. Revisit once more payload fields are filterable.
"""

from __future__ import annotations

import re

_MAX_LENGTH = 500
_WHITESPACE = re.compile(r"\s+")


def rewrite_query(text: str, *, max_length: int = _MAX_LENGTH) -> str:
    """Trims, collapses whitespace, and caps length before a query is embedded."""
    cleaned = _WHITESPACE.sub(" ", text).strip()
    return cleaned[:max_length]

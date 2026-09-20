"""Redacts answer_visible=False field values from a TABLE_ROW chunk's text
before it reaches an LLM prompt or a citation shown to a user.

retrieval_allowed=False content never becomes a chunk in the first place
(filtered at ingestion, before chunking) -- nothing to redact for that flag.
answer_visible=False fields DO reach retrieval (the row itself is
searchable), so enforcement happens here, at the point content is about to
be shown or used, not at retrieval-filtering time.
"""

from __future__ import annotations

from backend.retrieval.contracts import RetrievedChunk

_REDACTED_PLACEHOLDER = "[redacted: internal-only field]"


def redact_for_synthesis(chunk: RetrievedChunk) -> str:
    """Returns chunk.content_text with any answer_visible=False field values replaced.

    Best-effort label match: content_text segments and field_policies both
    derive from the same column_name/label in the common ingestion path, but
    this isn't a guaranteed-exact join.
    """
    if chunk.chunk_type != "table_row" or not chunk.field_policies:
        return chunk.content_text
    hidden_labels = {str(policy.get("field_name", "")) for policy in chunk.field_policies
                      if not policy.get("answer_visible", True)}
    if not hidden_labels:
        return chunk.content_text
    segments = chunk.content_text.split(" | ")
    redacted = []
    for segment in segments:
        label = segment.split(":", 1)[0].strip() if ":" in segment else segment.strip()
        redacted.append(f"{label}: {_REDACTED_PLACEHOLDER}" if label in hidden_labels else segment)
    return " | ".join(redacted)

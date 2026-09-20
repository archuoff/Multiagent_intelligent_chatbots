"""Query decomposition: splits a genuinely multi-part domain question into
separate, independently-searchable sub-questions.

Only reached when classify_intent's requires_decomposition flag is true --
the common case (a single-lookup domain question) skips this entirely and
retrieval runs directly on the one resolved_query instead. This is the
second LLM call in the "one call classifies + flags, a second call only
splits when needed" design, as opposed to always asking one call to do both
regardless of whether splitting is actually needed.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from backend.agent.azure_chat import AzureOpenAIChatClient, get_chat_client

logger = logging.getLogger(__name__)

_MAX_SUB_QUERIES = 4

DECOMPOSITION_SYSTEM_PROMPT = """You split one user question into separate,
independently-searchable sub-questions, only when it genuinely asks about
more than one distinct thing.

Rules:
- Each sub-question must stand alone: someone reading only that one
  sub-question, with no other context, must be able to search for and
  answer it.
- Preserve the user's actual wording and intent; do not add facts or
  narrow the question beyond what was actually asked.
- If the question turns out to only need one lookup after all, return a
  single-item list containing the original question unchanged.
- Return between 1 and 4 sub-questions. Do not over-split a question that
  reads as multiple clauses but is really one lookup.

OUTPUT — respond with ONLY this JSON object, no markdown fences, no other text:
{"sub_queries": ["...", "..."]}"""


def decompose_query(query: str, chat_client: AzureOpenAIChatClient | None = None) -> list[str]:
    """Splits one query into sub-queries, failing open to the original query unchanged."""
    client = chat_client or get_chat_client()
    messages = [{"role": "system", "content": DECOMPOSITION_SYSTEM_PROMPT}, {"role": "user", "content": query}]
    try:
        raw = client.complete(messages, temperature=0.1)
        sub_queries = _parse_sub_queries(raw)
    except Exception as error:
        logger.error(f"[decompose_query] LLM call failed: {error}")
        return [query]
    return sub_queries or [query]


def _parse_sub_queries(raw: str) -> list[str]:
    """Strips markdown fences, validates the sub_queries list, and caps its length."""
    json_str = raw.strip()
    if json_str.startswith("```"):
        json_str = json_str.split("```")[1]
        if json_str.startswith("json"):
            json_str = json_str[4:]
    try:
        payload: dict[str, Any] = json.loads(json_str)
        sub_queries = [str(item).strip() for item in payload.get("sub_queries", []) if str(item).strip()]
    except (json.JSONDecodeError, IndexError, AttributeError) as error:
        logger.warning(f"[decompose_query] Failed to parse sub_queries JSON: {raw!r} ({error})")
        return []
    return sub_queries[:_MAX_SUB_QUERIES]

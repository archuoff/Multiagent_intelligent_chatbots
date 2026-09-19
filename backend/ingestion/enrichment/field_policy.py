"""Deterministic field-use policy for Excel and other structured source fields.

The policy labels fields; it does not delete source values. ``service.py``
applies labels to canonical table cells before chunking. Answer generation must
later enforce ``answer_visible`` when presenting source-grounded information.
"""

from __future__ import annotations

import re
from typing import Mapping

from backend.ingestion.enrichment.contracts import FieldPolicy
from backend.ingestion.enrichment.contracts import FieldUse
from backend.ingestion.models import CanonicalDocument
from backend.ingestion.models import NodeType


class FieldPolicyResolver:
    """Classifies source columns with conservative, explainable name rules."""

    _REFERENCE_PATTERN = re.compile(r"\b(link|url|webinar|presentation|ppt|pdf|document path|file path|reference location)\b", re.IGNORECASE)
    _INTERNAL_PATTERN = re.compile(r"\b(internal note|backend|system field|record id|database id|created by|modified by|internal owner|record owner|system owner|email|password|token|secret)\b", re.IGNORECASE)

    def __init__(self, overrides: Mapping[str, FieldPolicy | dict] | None = None) -> None:
        """Loads explicit per-agent column decisions ahead of the conservative defaults."""
        self._overrides = {name.casefold(): policy if isinstance(policy, FieldPolicy) else FieldPolicy.model_validate(policy)
                           for name, policy in (overrides or {}).items()}

    def resolve(self, field_name: str) -> FieldPolicy:
        """Returns a policy label without inferring content from one cell value."""
        label = field_name.strip() or "unlabelled_field"
        if override := self._overrides.get(label.casefold()):
            return override.model_copy(update={"field_name": label})
        if self._REFERENCE_PATTERN.search(label):
            return FieldPolicy(field_name=label, use=FieldUse.REFERENCE_LINK, retrieval_allowed=True,
                answer_visible=True, reason="Column name indicates a source reference or document link.")
        if self._INTERNAL_PATTERN.search(label):
            return FieldPolicy(field_name=label, use=FieldUse.INTERNAL_ONLY, retrieval_allowed=True,
                answer_visible=False, reason="Column name indicates operational or internal-only data.")
        return FieldPolicy(field_name=label, use=FieldUse.STANDARD, retrieval_allowed=True,
            answer_visible=True, reason="No restrictive field policy matched the column name.")

    def apply(self, document: CanonicalDocument) -> int:
        """Adds field policy attributes to every canonical table cell and returns the count."""
        applied = 0
        for root in document.root_nodes:
            for node in self._walk(root):
                if node.node_type != NodeType.TABLE_CELL:
                    continue
                policy = self.resolve(str(node.attributes.get("column_name", "")))
                node.attributes["field_policy"] = policy.model_dump(mode="json")
                applied += 1
        return applied

    def _walk(self, node):
        """Yields a canonical subtree without importing the generic branching helper."""
        yield node
        for child in node.children:
            yield from self._walk(child)

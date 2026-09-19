"""Deterministic primary-versus-fallback reconciliation before canonicalization.

This module compares content only within the same source page or slide. It
does not use an LLM or embeddings, so numeric and code differences stay
auditable. ``merger.py`` applies these decisions to extraction contracts;
parsers later expose the retained evidence in canonical JSON.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from backend.ingestion.utils import normalize_text


class ExtractionReconciler:
    """Classifies fallback content without silently discarding technical conflicts."""

    _WORD_PATTERN = re.compile(r"[a-z0-9]+(?:[./_-][a-z0-9]+)*", re.IGNORECASE)
    _TECHNICAL_PATTERN = re.compile(r"\b(?:[a-z]*\d[\w./-]*|\d+(?:\.\d+)?)\b", re.IGNORECASE)
    _NEAR_DUPLICATE_COVERAGE = 0.9
    _CONFLICT_COVERAGE = 0.7
    _MIN_COMPARABLE_TOKENS = 4

    def compare_text(self, primary_text: str, fallback_text: str) -> dict[str, Any]:
        """Classifies a fallback block against primary page or slide text."""
        primary = normalize_text(primary_text) or ""
        fallback = normalize_text(fallback_text) or ""
        if not fallback:
            return {"decision": "empty", "reason": "Fallback block has no readable text."}
        if not primary:
            return {"decision": "fallback_only", "reason": "Primary page or slide has no readable text."}
        if primary.casefold() == fallback.casefold():
            return {"decision": "duplicate", "reason": "Normalized text is identical."}
        primary_tokens = self._tokens(primary)
        fallback_tokens = self._tokens(fallback)
        coverage = self._coverage(primary_tokens, fallback_tokens)
        technical_difference = self._technical_difference(primary, fallback)
        details = {"token_coverage": round(coverage, 3), "primary_technical_values": sorted(self._technical_values(primary)),
                   "fallback_technical_values": sorted(self._technical_values(fallback))}
        if len(fallback_tokens) >= self._MIN_COMPARABLE_TOKENS and coverage >= self._CONFLICT_COVERAGE and technical_difference:
            return {"decision": "conflict", "reason": "Comparable text contains different numeric or coded values.", **details}
        if fallback.casefold() in primary.casefold() or coverage >= self._NEAR_DUPLICATE_COVERAGE:
            return {"decision": "near_duplicate", "reason": "Fallback text is substantially covered by primary text.", **details}
        return {"decision": "fallback_only", "reason": "Fallback block contributes content not covered by primary text.", **details}

    def compare_tables(self, primary: Any, fallback: Any) -> dict[str, Any]:
        """Compares table cells by source coordinate and exact normalized value."""
        primary_cells = self._table_cells(primary)
        fallback_cells = self._table_cells(fallback)
        if not fallback_cells:
            return {"decision": "fallback_only", "reason": "Fallback table has no comparable non-empty cells."}
        if not primary_cells:
            return {"decision": "fallback_only", "reason": "Primary table has no comparable non-empty cells."}
        shared = set(primary_cells) & set(fallback_cells)
        differing = sorted(coordinate for coordinate in shared if primary_cells[coordinate] != fallback_cells[coordinate])
        if differing:
            return {"decision": "conflict", "reason": "Tables contain different values at the same cell coordinates.",
                    "conflicting_coordinates": [list(coordinate) for coordinate in differing]}
        if primary_cells == fallback_cells:
            return {"decision": "duplicate", "reason": "Comparable table cells match exactly."}
        return {"decision": "fallback_only", "reason": "Fallback table contains additional non-conflicting cells."}

    def _tokens(self, text: str) -> list[str]:
        """Produces normalized terms for a bounded lexical comparison."""
        return self._WORD_PATTERN.findall(text.casefold())

    def _coverage(self, primary: list[str], fallback: list[str]) -> float:
        """Measures how much fallback wording is already present in primary text."""
        if not fallback:
            return 1.0
        primary_counts = Counter(primary)
        fallback_counts = Counter(fallback)
        shared = sum(min(primary_counts[token], count) for token, count in fallback_counts.items())
        return shared / len(fallback)

    def _technical_values(self, text: str) -> set[str]:
        """Extracts values and identifiers that must not be fuzzy-deduplicated."""
        return {value.casefold() for value in self._TECHNICAL_PATTERN.findall(text)}

    def _technical_difference(self, primary: str, fallback: str) -> bool:
        """Reports conflicting values while allowing one extractor to be more complete."""
        primary_values = self._technical_values(primary)
        fallback_values = self._technical_values(fallback)
        if not primary_values or not fallback_values:
            return False
        if primary_values.issubset(fallback_values) or fallback_values.issubset(primary_values):
            return False
        return primary_values != fallback_values

    def _table_cells(self, table: Any) -> dict[tuple[int, int], str]:
        """Reads non-empty coordinates from either structured tables or legacy matrices."""
        if isinstance(table, list):
            return {(row, column): normalize_text(str(value)) or "" for row, values in enumerate(table)
                    for column, value in enumerate(values) if normalize_text(str(value))}
        if not isinstance(table, dict):
            return {}
        if isinstance(table.get("matrix"), list):
            return self._table_cells(table["matrix"])
        cells = table.get("source_table", {}).get("data", {}).get("table_cells", [])
        if not isinstance(cells, list):
            return {}
        result: dict[tuple[int, int], str] = {}
        for cell in cells:
            if not isinstance(cell, dict):
                continue
            row, column = cell.get("start_row_offset_idx"), cell.get("start_col_offset_idx")
            text = normalize_text(str(cell.get("text", "")))
            if type(row) is int and type(column) is int and text:
                result[(row, column)] = text
        return result

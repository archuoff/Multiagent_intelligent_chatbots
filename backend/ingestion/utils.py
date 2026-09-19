"""Utilities shared across the ingestion pipeline.

This file contains helper functions for stable IDs, file hashing, range formatting,
and generic text normalization used by parsers and branching code.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from uuid import uuid4


# This function generates stable-looking internal identifiers for documents and nodes.
def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


# This function hashes a source file so re-ingestion and lineage checks can compare versions.
def hash_file(path: str | Path) -> str:
    hasher = hashlib.sha256()
    with Path(path).open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


# This function normalizes whitespace from extracted text without changing domain meaning.
def normalize_text(text: str | None) -> str | None:
    if text is None:
        return None
    collapsed = re.sub(r"\s+", " ", text).strip()
    return collapsed or None


# This function converts 1-based Excel row/column coordinates into an A1-style range string.
def make_cell_range(
    min_row: int,
    min_col: int,
    max_row: int,
    max_col: int,
) -> str:
    return f"{_excel_column(min_col)}{min_row}:{_excel_column(max_col)}{max_row}"


# This function converts a 1-based column index into the matching Excel column label.
def _excel_column(column_index: int) -> str:
    letters: list[str] = []
    current = column_index
    while current > 0:
        current, remainder = divmod(current - 1, 26)
        letters.append(chr(65 + remainder))
    return "".join(reversed(letters))

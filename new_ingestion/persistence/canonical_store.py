"""Writes a validated CanonicalDocument to its versioned master.json location."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from new_ingestion.schema.schema_define import CanonicalDocument

MASTER_FILENAME = "master.json"


class CanonicalPersistenceError(ValueError):
    """Raised when an existing version's content would silently change."""


def persist_canonical(document: CanonicalDocument, *, agent_id: str, version_id: str, output_root: Path) -> Path:
    """Writes output_root/<agent_id>/<document_id>/<version_id>/master.json atomically.

    A version's content must never change once written: if master.json
    already exists at this path with different content, this raises instead
    of overwriting it. Identical content is a no-op (safe to call twice).
    """
    directory = Path(output_root) / agent_id / document.document.document_id / version_id
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / MASTER_FILENAME
    payload = document.model_dump_json(indent=2)

    if destination.exists():
        if destination.read_text(encoding="utf-8") == payload:
            return destination
        raise CanonicalPersistenceError(
            f"refusing to overwrite existing version content at {destination}"
        )

    fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".master-", suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temp_path, destination)
    except BaseException:
        Path(temp_path).unlink(missing_ok=True)
        raise
    return destination


__all__ = ["CanonicalPersistenceError", "persist_canonical"]

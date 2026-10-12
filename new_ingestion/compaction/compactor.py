"""Entry point: load a canonical document and compact it for chunking/embedding."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from new_ingestion.compaction.envelope import build_envelope
from new_ingestion.compaction.xlsx_compactor import compact_xlsx
from new_ingestion.schema.schema_define import CanonicalDocument, SourceType

_COMPACTORS = {
    SourceType.XLSX: compact_xlsx,
}


def compact_document(document: CanonicalDocument) -> dict[str, Any]:
    """Builds the compact, chunk/embedding-ready JSON for a validated canonical document."""
    compactor = _COMPACTORS.get(document.document.source_type)
    if compactor is None:
        raise NotImplementedError(
            f"compaction for source_type={document.document.source_type!r} is not implemented yet"
        )
    content = compactor(document)
    return build_envelope(document, content)


def load_and_compact(input_path: str | Path) -> dict[str, Any]:
    raw = json.loads(Path(input_path).read_text(encoding="utf-8"))
    document = CanonicalDocument.model_validate(raw)
    return compact_document(document)


def _main() -> int:
    parser = argparse.ArgumentParser(description="Compact a canonical document JSON for chunking/embedding.")
    parser.add_argument("input_path", type=Path)
    parser.add_argument("output_path", type=Path)
    args = parser.parse_args()
    compact = load_and_compact(args.input_path)
    args.output_path.write_text(json.dumps(compact, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {args.output_path} ({len(compact['content'])} content units, {len(compact['assets'])} assets)")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = ["compact_document", "load_and_compact"]

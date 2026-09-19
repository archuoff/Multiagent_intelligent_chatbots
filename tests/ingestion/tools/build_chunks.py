"""Build immutable chunk artifacts from one JLR source document.

This developer tool connects the completed extraction/canonicalization pipeline
to the embedding boundary. It persists or reuses canonical Master JSON, applies
the structure-aware chunk builder, and writes a matching chunks.json plus
checksum manifest. It never calls Azure or Qdrant.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


# Direct execution starts inside tests/ingestion/tools, not at the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.ingestion.chunking import ChunkArtifactStore, ChunkingService
from backend.ingestion.models import SourceType
from backend.ingestion.service import IngestionService


def parse_arguments() -> argparse.Namespace:
    """Reads the source identity required to create a traceable chunk artifact."""
    parser = argparse.ArgumentParser(description="Ingest one source and create a checksum-verified chunks.json artifact.")
    parser.add_argument("source_path", type=Path, help="Path to a PDF, DOCX, PPTX, or XLSX source file.")
    parser.add_argument("--source-type", required=True, choices=[item.value for item in SourceType],
        help="Source type matching the supplied file.")
    parser.add_argument("--agent-id", required=True, help="Logical JLR agent that owns the source.")
    return parser.parse_args()


def main() -> int:
    """Creates canonical and chunk artifacts, then prints their safe, copyable locations."""
    arguments = parse_arguments()
    if not arguments.source_path.is_file():
        raise FileNotFoundError(f"Source file was not found: {arguments.source_path}")
    ingestion = IngestionService()
    document, canonical = ingestion.ingest_and_persist(agent_id=arguments.agent_id,
        source_type=SourceType(arguments.source_type), file_path=arguments.source_path)
    chunks, artifact = ChunkingService().build_and_persist(document, ChunkArtifactStore())
    print(f"Document ID: {document.document_id}")
    print(f"Source version: {document.version}")
    print(f"Canonical JSON: {canonical.master_json_path}")
    print(f"Chunks: {artifact.chunks_path}")
    print(f"Chunk manifest: {artifact.manifest_path}")
    print(f"Embedding-ready chunks: {len(chunks.chunks)}")
    print(f"Chunk rejections: {len(chunks.rejected)}")
    print(f"Chunk status: {artifact.manifest.status}")
    return 0 if artifact.manifest.status == "ready_for_embedding" else 1


if __name__ == "__main__":
    raise SystemExit(main())

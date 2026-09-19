"""Run one source file through the complete JLR ingestion pipeline.

This developer entry point is intentionally thin: it coordinates the existing
production-style services instead of duplicating parsing, chunking, embedding,
or indexing logic. Use it to test the Day-0/Day-N flow for one real source:
source file -> Master JSON -> chunks -> Azure embeddings -> local Qdrant.
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
from backend.ingestion.embedding import AzureOpenAIEmbeddingProvider, EmbeddingService
from backend.ingestion.indexing import EmbeddingIndexingService, QdrantVectorStore
from backend.ingestion.models import SourceType
from backend.ingestion.service import IngestionService


def parse_arguments() -> argparse.Namespace:
    """Reads the minimum source identity needed for traceable full ingestion."""
    parser = argparse.ArgumentParser(
        description="Ingest, chunk, embed, and index one JLR source file."
    )
    parser.add_argument("source_path", type=Path, help="Path to a PDF, DOCX, PPTX, or XLSX source file.")
    parser.add_argument(
        "--source-type",
        required=True,
        choices=[item.value for item in SourceType],
        help="Source type matching the supplied file.",
    )
    parser.add_argument("--agent-id", required=True, help="Logical JLR agent that owns the source.")
    parser.add_argument(
        "--skip-index",
        action="store_true",
        help="Stop after chunk artifact creation. Useful when Azure/Qdrant settings are not ready.",
    )
    return parser.parse_args()


def main() -> int:
    """Runs the full pipeline and prints only safe artifact locations and counts."""
    arguments = parse_arguments()
    if not arguments.source_path.is_file():
        raise FileNotFoundError(f"Source file was not found: {arguments.source_path}")

    ingestion = IngestionService()
    document, canonical = ingestion.ingest_and_persist(
        agent_id=arguments.agent_id,
        source_type=SourceType(arguments.source_type),
        file_path=arguments.source_path,
    )

    chunk_store = ChunkArtifactStore()
    chunks, chunk_artifact = ChunkingService().build_and_persist(document, chunk_store)

    print(f"Document ID: {document.document_id}")
    print(f"Source version: {document.version}")
    print(f"Canonical JSON: {canonical.master_json_path}")
    print(f"Canonical manifest: {canonical.manifest_path}")
    print(f"Chunks: {chunk_artifact.chunks_path}")
    print(f"Chunk manifest: {chunk_artifact.manifest_path}")
    print(f"Embedding-ready chunks: {len(chunks.chunks)}")
    print(f"Chunk rejections: {len(chunks.rejected)}")
    print(f"Chunk status: {chunk_artifact.manifest.status}")

    if chunk_artifact.manifest.status != "ready_for_embedding":
        print("Indexing skipped because the chunk artifact is not ready for embedding.")
        return 1

    if arguments.skip_index:
        print("Indexing skipped by --skip-index.")
        return 0

    provider = AzureOpenAIEmbeddingProvider.from_runtime_environment()
    indexing = EmbeddingIndexingService(chunk_store, EmbeddingService(provider), QdrantVectorStore())
    embeddings, indexed = indexing.embed_and_index(chunk_artifact)

    print(f"Embedding deployment: {embeddings.embedding_model}")
    print(f"Embedded chunks: {len(embeddings.embedded_chunks)}")
    print(f"Qdrant collection: {indexed.collection_name}")
    print(f"Indexed chunks: {len(indexed.indexed_chunk_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

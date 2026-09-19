"""Intentionally embeds and indexes one checksum-verified JLR chunk artifact.

This direct-run tool makes live Azure embedding requests and writes a local
Qdrant index only when a developer explicitly invokes it. It prints safe
counts and collection names, never source text, vectors, or credentials.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

# Direct execution starts inside tests/ingestion/tools, not at the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.ingestion.chunking.chunk_store import ChunkArtifactManifest, ChunkArtifactStore, StoredChunkArtifact
from backend.ingestion.embedding import AzureOpenAIEmbeddingProvider, EmbeddingService
from backend.ingestion.indexing import EmbeddingIndexingService, QdrantVectorStore


def main() -> int:
    """Verifies a chunks manifest before making a live embedding/indexing operation."""
    parser = argparse.ArgumentParser(description="Embed and index a verified JLR chunks.json artifact in local Qdrant.")
    parser.add_argument("chunks_path", help="Path to the stored chunks.json artifact.")
    parser.add_argument("manifest_path", help="Path to its matching chunk manifest JSON.")
    arguments = parser.parse_args()
    chunks_path, manifest_path = Path(arguments.chunks_path), Path(arguments.manifest_path)
    if not chunks_path.is_file() or not manifest_path.is_file():
        raise FileNotFoundError("Both the chunks artifact and its matching manifest must exist.")
    manifest = ChunkArtifactManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    artifact = StoredChunkArtifact(chunks_path=chunks_path, manifest_path=manifest_path, manifest=manifest)
    provider = AzureOpenAIEmbeddingProvider.from_runtime_environment()
    service = EmbeddingIndexingService(ChunkArtifactStore(), EmbeddingService(provider), QdrantVectorStore())
    embeddings, indexed = service.embed_and_index(artifact)
    print(f"Embedding deployment: {embeddings.embedding_model}")
    print(f"Embedded chunks: {len(embeddings.embedded_chunks)}")
    print(f"Qdrant collection: {indexed.collection_name}")
    print(f"Indexed chunks: {len(indexed.indexed_chunk_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

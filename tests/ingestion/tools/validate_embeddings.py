"""Manually validates one stored chunk artifact against the configured Azure deployment.

Run this tool only when a developer intentionally wants to make Azure embedding
requests. It loads runtime environment variables inside its own process, reads
the immutable chunk JSON, and prints only counts and dimensions, never secrets,
source text, or vectors.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

# Direct execution starts inside tests/ingestion/tools, not at the project root.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.ingestion.chunking.contracts import ChunkBuildResult
from backend.ingestion.embedding import AzureOpenAIEmbeddingProvider, EmbeddingService


def main() -> int:
    """Loads an approved chunk artifact, calls Azure once or more, and prints safe diagnostics."""
    parser = argparse.ArgumentParser(description="Validate Azure embeddings for an immutable chunks.json artifact.")
    parser.add_argument("chunks_path", help="Path to a stored chunks.json file.")
    arguments = parser.parse_args()
    path = Path(arguments.chunks_path)
    if not path.is_file():
        raise FileNotFoundError(f"Chunk artifact was not found: {path}")
    chunks = ChunkBuildResult.model_validate_json(path.read_text(encoding="utf-8"))
    provider = AzureOpenAIEmbeddingProvider.from_runtime_environment()
    result = EmbeddingService(provider).embed(chunks)
    print(f"Embedding deployment: {result.embedding_model}")
    print(f"Embedded chunks: {len(result.embedded_chunks)}")
    print(f"Skipped empty chunks: {len(result.rejected_chunk_ids)}")
    print(f"Vector dimensions: {result.dimensions}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

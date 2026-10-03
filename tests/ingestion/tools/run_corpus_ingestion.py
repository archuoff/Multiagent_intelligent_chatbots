"""Run Day-0 ingestion for every supported source in ``storage/raw``.

This developer entry point scans ``storage/raw/<agent-id>`` folders and runs
the same production-style services used by the single-file ingestion tool:
parse -> Master JSON -> chunks -> optional Azure embeddings -> Qdrant index.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import sys
from time import perf_counter


PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.ingestion.chunking import ChunkArtifactStore, ChunkingService
from backend.ingestion.embedding import AzureOpenAIEmbeddingProvider, EmbeddingService
from backend.ingestion.indexing import EmbeddingIndexingService, QdrantVectorStore
from backend.ingestion.models import SourceType
from backend.ingestion.service import IngestionService


SUPPORTED_SUFFIXES = {
    ".pdf": SourceType.PDF,
    ".ppt": SourceType.PPT,
    ".pptx": SourceType.PPTX,
    ".docx": SourceType.DOCX,
    ".xlsx": SourceType.XLSX,
}


@dataclass(slots=True)
class FileResult:
    """Stores one file's ingestion outcome for the final report."""

    agent_id: str
    source_path: Path
    status: str
    document_id: str = ""
    version: str = ""
    chunks: int = 0
    rejections: int = 0
    indexed: int = 0
    collection: str = ""
    message: str = ""
    elapsed_seconds: float = 0.0


def parse_arguments() -> argparse.Namespace:
    """Reads corpus location and whether vectors should be indexed."""
    parser = argparse.ArgumentParser(description="Ingest every supported JLR raw source file.")
    parser.add_argument("raw_root", type=Path, nargs="?", default=Path("storage") / "raw",
                        help="Root folder shaped as storage/raw/<agent-id>/<file>.")
    parser.add_argument("--agent-id", help="Optional single agent folder to ingest.")
    parser.add_argument("--skip-index", action="store_true",
                        help="Stop after chunk creation; do not call Azure embeddings or Qdrant.")
    parser.add_argument("--fail-fast", action="store_true", help="Stop on the first failed file.")
    return parser.parse_args()


def discover_sources(raw_root: Path, agent_id: str | None = None) -> list[tuple[str, Path, SourceType]]:
    """Finds supported files under agent folders and ignores README/gitkeep files."""
    if agent_id:
        agent_folders = [raw_root / agent_id]
    else:
        agent_folders = [path for path in sorted(raw_root.iterdir()) if path.is_dir()]
    sources: list[tuple[str, Path, SourceType]] = []
    for folder in agent_folders:
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if not path.is_file():
                continue
            source_type = SUPPORTED_SUFFIXES.get(path.suffix.lower())
            if source_type is not None:
                sources.append((folder.name, path, source_type))
    return sources


def ingest_file(agent_id: str, source_path: Path, source_type: SourceType, *, skip_index: bool) -> FileResult:
    """Runs the full existing ingestion stack for one discovered source file."""
    started = perf_counter()
    try:
        ingestion = IngestionService()
        document, _canonical = ingestion.ingest_and_persist(
            agent_id=agent_id,
            source_type=source_type,
            file_path=source_path,
        )
        chunk_store = ChunkArtifactStore()
        chunk_result, chunk_artifact = ChunkingService().build_and_persist(document, chunk_store)
        result = FileResult(agent_id=agent_id, source_path=source_path, status=chunk_artifact.manifest.status,
            document_id=document.document_id, version=document.version, chunks=len(chunk_result.chunks),
            rejections=len(chunk_result.rejected))
        if chunk_artifact.manifest.status != "ready_for_embedding":
            result.message = "Chunk artifact is not ready for embedding."
            return _with_elapsed(result, started)
        if skip_index:
            result.message = "Indexing skipped."
            return _with_elapsed(result, started)
        provider = AzureOpenAIEmbeddingProvider.from_runtime_environment()
        embeddings, indexed = EmbeddingIndexingService(
            chunk_store, EmbeddingService(provider), QdrantVectorStore()
        ).embed_and_index(chunk_artifact)
        result.indexed = len(indexed.indexed_chunk_ids)
        result.collection = indexed.collection_name
        result.message = f"Embedded {len(embeddings.embedded_chunks)} chunks."
        return _with_elapsed(result, started)
    except Exception as error:
        return _with_elapsed(FileResult(agent_id=agent_id, source_path=source_path, status="failed",
            message=f"{type(error).__name__}: {error}"), started)


def _with_elapsed(result: FileResult, started: float) -> FileResult:
    """Adds elapsed wall-clock time before reporting."""
    result.elapsed_seconds = round(perf_counter() - started, 2)
    return result


def print_result(result: FileResult) -> None:
    """Prints one compact per-file outcome line."""
    print(
        f"[{result.status}] agent={result.agent_id} file=\"{result.source_path.name}\" "
        f"version={result.version or '-'} chunks={result.chunks} rejections={result.rejections} "
        f"indexed={result.indexed} collection={result.collection or '-'} elapsed={result.elapsed_seconds}s "
        f"{result.message}"
    )


def print_summary(results: list[FileResult]) -> None:
    """Prints totals for the corpus run."""
    succeeded = [item for item in results if item.status == "ready_for_embedding"]
    failed = [item for item in results if item.status == "failed"]
    print("\nSummary")
    print(f"Files processed: {len(results)}")
    print(f"Ready files: {len(succeeded)}")
    print(f"Failed files: {len(failed)}")
    print(f"Total chunks: {sum(item.chunks for item in results)}")
    print(f"Total rejections: {sum(item.rejections for item in results)}")
    print(f"Total indexed chunks: {sum(item.indexed for item in results)}")
    if failed:
        print("\nFailures")
        for item in failed:
            print(f"- {item.agent_id}/{item.source_path.name}: {item.message}")


def main() -> int:
    """Scans raw storage and ingests every supported file."""
    arguments = parse_arguments()
    if not arguments.raw_root.is_dir():
        raise FileNotFoundError(f"Raw root was not found: {arguments.raw_root}")
    sources = discover_sources(arguments.raw_root, arguments.agent_id)
    if not sources:
        print("No supported source files found.")
        return 0
    print(f"Discovered {len(sources)} supported source file(s).")
    results: list[FileResult] = []
    for agent_id, source_path, source_type in sources:
        print(f"\nIngesting {agent_id}/{source_path.name} ({source_type.value})")
        result = ingest_file(agent_id, source_path, source_type, skip_index=arguments.skip_index)
        print_result(result)
        results.append(result)
        if arguments.fail_fast and result.status == "failed":
            break
    print_summary(results)
    return 1 if any(item.status == "failed" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

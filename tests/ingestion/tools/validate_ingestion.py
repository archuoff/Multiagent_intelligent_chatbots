"""Run one real source file through JLR ingestion and print a quality summary.

This script is a developer validation tool. It does not create embeddings or
write to a database. Optional explicit flags can save an inspection copy or
persist an immutable local Master JSON artifact.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


# This setup makes direct script execution resolve the project-level backend package.
PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.ingestion.models import CanonicalDocument
from backend.ingestion.models import CanonicalNode
from backend.ingestion.models import SourceType
from backend.ingestion.service import IngestionService
from backend.ingestion.extraction.timeout_policy import PDF_PROCESSING_CAPTION


# This function parses the source path and ingestion metadata supplied at the command line.
def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate one file through the JLR ingestion pipeline.")
    parser.add_argument("source_path", type=Path, help="Path to a PDF, DOCX, PPTX, or XLSX file.")
    parser.add_argument("--source-type", required=True, choices=[item.value for item in SourceType], help="Source type matching the file.")
    parser.add_argument("--agent-id", default="validation", help="Logical agent that owns this source.")
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path to write an indented canonical JSON inspection copy.",
    )
    parser.add_argument(
        "--persist",
        action="store_true",
        help="Persist an immutable Master JSON artifact and manifest under storage/.",
    )
    return parser.parse_args()


# This function walks canonical nodes to calculate a small, readable output summary.
def count_nodes(node: CanonicalNode) -> int:
    return 1 + sum(count_nodes(child) for child in node.children)


# This function writes a requested inspection artifact without creating a production store.
def write_inspection_json(document: CanonicalDocument, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        document.model_dump_json(indent=2),
        encoding="utf-8",
    )


# This function executes one ingestion pass and displays extraction and quality evidence.
def main() -> int:
    arguments = parse_arguments()
    if not arguments.source_path.is_file():
        raise FileNotFoundError(f"Source file was not found: {arguments.source_path}")

    service = IngestionService()
    if arguments.source_type == "pdf":
        print(PDF_PROCESSING_CAPTION, flush=True)
    persisted_artifact = None
    if arguments.persist:
        document, persisted_artifact = service.ingest_and_persist(
            agent_id=arguments.agent_id,
            source_type=SourceType(arguments.source_type),
            file_path=arguments.source_path,
        )
    else:
        document = service.ingest_source(
            agent_id=arguments.agent_id,
            source_type=SourceType(arguments.source_type),
            file_path=arguments.source_path,
        )
    if arguments.output:
        write_inspection_json(document, arguments.output)
    total_nodes = sum(count_nodes(root) for root in document.root_nodes)
    print(f"File: {document.file_name}")
    print(f"Parser: {document.parser_info.parser_name}")
    print(f"Extraction mode: {document.parser_info.extraction_mode}")
    details = document.parser_info.processing_details
    if details.get("initial_timeout_seconds"):
        print(f"Initial PDF timeout: {details['initial_timeout_seconds']}s; attempts: {len(details.get('attempts', []))}")
    print(f"Root nodes: {len(document.root_nodes)}")
    print(f"Canonical nodes: {total_nodes}")
    print(f"Warnings: {len(document.quality.warnings)}")
    print(f"Errors: {len(document.quality.errors)}")
    if arguments.output:
        print(f"Canonical JSON: {arguments.output}")
    if persisted_artifact:
        print(f"Stored Master JSON: {persisted_artifact.master_json_path}")
        print(f"Stored manifest: {persisted_artifact.manifest_path}")
    for warning in document.quality.warnings:
        print(f"WARNING: {warning}")
    for error in document.quality.errors:
        print(f"ERROR: {error}")
    return 1 if document.quality.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

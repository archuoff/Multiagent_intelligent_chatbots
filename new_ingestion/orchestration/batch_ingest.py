"""Batch-ingests every file under new_ingestion/data/<agent_id>/ into
new_ingestion/output/<agent_id>/<document_id>/<version>/master.json.

Each agent is simply a subfolder of ``data_root``; there is no external
config dependency. Versioning, retry-after-failure, and processing status
are all decided by the existing pure modules in
``new_ingestion/injestion_control/`` — this module only supplies their
history (read from manifests on disk) and drives the file loop.
"""

from __future__ import annotations

import argparse
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from new_ingestion.injestion_control.hashing import deterministic_uuid5, hash_file
from new_ingestion.injestion_control.lifecycle import (
    LifecycleEvent, LifecycleRecord, LifecycleStatus, transition_lifecycle,
)
from new_ingestion.injestion_control.versioning import (
    VersioningAction, decide_document_version,
)
from new_ingestion.orchestration.ingestion_orchestrator import (
    OrchestrationStatus, ingest_to_canonical,
)
from new_ingestion.persistence.canonical_store import persist_canonical
from new_ingestion.persistence.manifest import (
    DocumentManifest, list_processed_versions, write_agent_index, write_manifest,
)

DEFAULT_DATA_ROOT = Path("new_ingestion/data")
DEFAULT_OUTPUT_ROOT = Path("new_ingestion/output")

# Fixed namespace so the same (agent_id, relative path) always yields the
# same document_id across runs and machines.
_APP_NAMESPACE = uuid.UUID("c9f5a6d2-6b8a-4e8e-9b0a-8f2b2c9d4a11")


@dataclass(slots=True)
class BatchIngestResult:
    agent_id: str
    succeeded: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"{self.agent_id}: {len(self.succeeded)} succeeded, "
                 f"{len(self.skipped)} skipped, {len(self.failed)} failed"]
        for relative_path, reason in self.failed:
            lines.append(f"  FAILED {relative_path}: {reason}")
        return "\n".join(lines)


def ingest_agent_folder(
    agent_id: str,
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
    force_reingestion: bool = False,
) -> BatchIngestResult:
    """Ingests every file under data_root/<agent_id>/ (recursively) for one agent."""
    result = BatchIngestResult(agent_id=agent_id)
    agent_dir = Path(data_root) / agent_id
    if not agent_dir.is_dir():
        return result

    for path in sorted(p for p in agent_dir.rglob("*") if p.is_file()):
        relative_path = path.relative_to(agent_dir).as_posix()
        try:
            _ingest_one_file(
                path, relative_path, agent_id=agent_id, output_root=output_root,
                force_reingestion=force_reingestion, result=result,
            )
        except Exception as error:
            result.failed.append((relative_path, str(error)))

    write_agent_index(agent_id, output_root)
    return result


def _ingest_one_file(
    path: Path, relative_path: str, *, agent_id: str, output_root: Path,
    force_reingestion: bool, result: BatchIngestResult,
) -> None:
    source_file_hash = hash_file(path)
    logical_document_key = f"{agent_id}:{relative_path}"
    document_id = str(deterministic_uuid5(_APP_NAMESPACE, logical_document_key))

    previous_versions = list_processed_versions(agent_id, document_id, output_root)
    decision = decide_document_version(
        logical_document_key=logical_document_key, document_id=document_id,
        current_source_file_hash=source_file_hash, previous_versions=previous_versions,
        force_reingestion=force_reingestion,
    )
    if decision.action is VersioningAction.NO_CHANGE:
        result.skipped.append(relative_path)
        return

    version_id = f"v{decision.next_version_number}"
    record = LifecycleRecord(document_id=document_id, version_id=version_id, status=LifecycleStatus.PENDING)
    record, _ = transition_lifecycle(record, LifecycleEvent.START_PROCESSING)

    def save_manifest(current: LifecycleRecord) -> None:
        manifest = DocumentManifest(
            document_id=document_id, version_id=version_id, version_number=decision.next_version_number,
            logical_document_key=logical_document_key, source_file_name=path.name,
            source_relative_path=relative_path, source_file_hash=source_file_hash,
            status=current.status, failure_reason=current.failure_reason,
        )
        write_manifest(manifest, agent_id=agent_id, output_root=output_root)

    save_manifest(record)

    artifact_dir = Path(output_root) / agent_id / document_id / version_id / "assets"
    orchestration_result = ingest_to_canonical(
        path, document_id=document_id, version_id=version_id,
        logical_document_key=logical_document_key, artifact_dir=artifact_dir,
    )

    if orchestration_result.status is OrchestrationStatus.FAILED or orchestration_result.canonical_document is None:
        reason = "; ".join(orchestration_result.errors) or "ingestion failed"
        record, _ = transition_lifecycle(record, LifecycleEvent.PROCESSING_FAILED, failure_reason=reason)
        save_manifest(record)
        result.failed.append((relative_path, reason))
        return

    persist_canonical(orchestration_result.canonical_document, agent_id=agent_id,
                       version_id=version_id, output_root=output_root)
    record, _ = transition_lifecycle(record, LifecycleEvent.PROCESSING_SUCCEEDED)
    save_manifest(record)
    result.succeeded.append(relative_path)


def ingest_all_agents(
    *, data_root: Path = DEFAULT_DATA_ROOT, output_root: Path = DEFAULT_OUTPUT_ROOT,
    force_reingestion: bool = False,
) -> dict[str, BatchIngestResult]:
    """Ingests every agent subfolder found directly under data_root."""
    data_root = Path(data_root)
    if not data_root.is_dir():
        return {}
    results: dict[str, BatchIngestResult] = {}
    for agent_dir in sorted(p for p in data_root.iterdir() if p.is_dir()):
        results[agent_dir.name] = ingest_agent_folder(
            agent_dir.name, data_root=data_root, output_root=output_root,
            force_reingestion=force_reingestion,
        )
    return results


def _main() -> int:
    parser = argparse.ArgumentParser(description="Batch-ingest agent input folders into canonical JSON.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--agent", help="Ingest only this agent_id (a subfolder of data_root).")
    group.add_argument("--all", action="store_true", help="Ingest every agent subfolder under data_root.")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--force-reingestion", action="store_true")
    args = parser.parse_args()

    if args.agent:
        results = {args.agent: ingest_agent_folder(
            args.agent, data_root=args.data_root, output_root=args.output_root,
            force_reingestion=args.force_reingestion,
        )}
    else:
        results = ingest_all_agents(
            data_root=args.data_root, output_root=args.output_root,
            force_reingestion=args.force_reingestion,
        )

    had_failures = False
    for batch_result in results.values():
        print(batch_result.summary())
        had_failures = had_failures or bool(batch_result.failed)
    return 1 if had_failures else 0


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = ["BatchIngestResult", "ingest_agent_folder", "ingest_all_agents"]

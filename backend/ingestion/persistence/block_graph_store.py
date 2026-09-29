"""Persistence for compact Textract-style BlockGraph artifacts.

BlockGraph files are generated from the canonical document after enrichment and
quality validation. They are persisted beside the canonical artifacts so the
production ingestion run keeps the compact relationship graph available for
retrieval experiments, audit, and future BlockGraph-native chunking.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

from pydantic import BaseModel

from backend.ingestion.block_graph import BlockGraphBuilder
from backend.ingestion.models import CanonicalDocument


class BlockGraphArtifactManifest(BaseModel):
    """Records where one persisted BlockGraph artifact is stored."""

    schema_version: str = "1.0"
    document_id: str
    agent_id: str
    version: str
    source_hash: str
    source_type: str
    source_name: str
    block_graph_artifact_path: str
    artifact_hash: str


class StoredBlockGraphArtifact(BaseModel):
    """Returns durable BlockGraph artifact locations."""

    block_graph_path: Path
    manifest_path: Path
    manifest: BlockGraphArtifactManifest
    reused_existing_version: bool = False


class BlockGraphArtifactStore:
    """Writes compact BlockGraph JSON artifacts under the local storage root."""

    _SAFE_PATH_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")

    def __init__(self, storage_root: str | Path | None = None) -> None:
        self._storage_root = Path(storage_root) if storage_root else Path(__file__).resolve().parents[3] / "storage"
        self._builder = BlockGraphBuilder()

    @property
    def storage_root(self) -> Path:
        return self._storage_root

    def persist(self, document: CanonicalDocument) -> StoredBlockGraphArtifact:
        """Builds and persists one immutable BlockGraph artifact for a document version."""
        agent_id = self._safe_component(document.agent_id, "agent_id")
        document_id = self._safe_component(document.document_id, "document_id")
        version = self._safe_component(document.version, "version")
        block_graph_path = self._storage_root / "block-graphs" / agent_id / document_id / version / "block_graph.json"
        manifest_path = self._storage_root / "block-graph-manifests" / agent_id / document_id / f"{version}.json"
        serialized_graph = json.dumps(self._builder.build(document), indent=2, ensure_ascii=False, default=str).encode("utf-8")

        if block_graph_path.exists():
            if block_graph_path.read_bytes() != serialized_graph:
                raise FileExistsError(
                    "BlockGraph artifact already exists with different content; create a new document version."
                )
            if not manifest_path.is_file():
                raise RuntimeError("BlockGraph artifact exists but its manifest is missing.")
            manifest = BlockGraphArtifactManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
            return StoredBlockGraphArtifact(
                block_graph_path=block_graph_path,
                manifest_path=manifest_path,
                manifest=manifest,
                reused_existing_version=True,
            )

        artifact_hash = hashlib.sha256(serialized_graph).hexdigest()
        manifest = BlockGraphArtifactManifest(
            document_id=document.document_id,
            agent_id=document.agent_id,
            version=document.version,
            source_hash=document.source_hash,
            source_type=document.source_type.value,
            source_name=document.source_name,
            block_graph_artifact_path=self._relative_path(block_graph_path),
            artifact_hash=artifact_hash,
        )
        self._atomic_write(block_graph_path, serialized_graph)
        try:
            self._atomic_write(manifest_path, manifest.model_dump_json(indent=2).encode("utf-8"))
        except Exception:
            block_graph_path.unlink(missing_ok=True)
            raise
        return StoredBlockGraphArtifact(block_graph_path=block_graph_path, manifest_path=manifest_path, manifest=manifest)

    def load(self, *, agent_id: str, document_id: str, version: str) -> StoredBlockGraphArtifact:
        """Loads a previously persisted BlockGraph artifact and validates its checksum."""
        agent_component = self._safe_component(agent_id, "agent_id")
        document_component = self._safe_component(document_id, "document_id")
        version_component = self._safe_component(version, "version")
        block_graph_path = self._storage_root / "block-graphs" / agent_component / document_component / version_component / "block_graph.json"
        manifest_path = self._storage_root / "block-graph-manifests" / agent_component / document_component / f"{version_component}.json"
        if not block_graph_path.is_file() or not manifest_path.is_file():
            raise FileNotFoundError("Registered BlockGraph artifact or manifest is missing.")
        manifest = BlockGraphArtifactManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        if hashlib.sha256(block_graph_path.read_bytes()).hexdigest() != manifest.artifact_hash:
            raise ValueError("BlockGraph artifact checksum mismatch.")
        return StoredBlockGraphArtifact(
            block_graph_path=block_graph_path,
            manifest_path=manifest_path,
            manifest=manifest,
            reused_existing_version=True,
        )

    def _safe_component(self, value: str, field_name: str) -> str:
        if not self._SAFE_PATH_COMPONENT.fullmatch(value) or value in {".", ".."}:
            raise ValueError(f"{field_name} contains unsupported path characters: {value!r}")
        return value

    def _relative_path(self, path: Path) -> str:
        return path.relative_to(self._storage_root).as_posix()

    def _atomic_write(self, target_path: Path, content: bytes) -> None:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target_path.name}.", dir=target_path.parent)
        try:
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())
            os.link(temporary_name, target_path)
            Path(temporary_name).unlink()
        except Exception:
            Path(temporary_name).unlink(missing_ok=True)
            raise

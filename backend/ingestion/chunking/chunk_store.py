"""Immutable local storage for embedding-ready chunk artifacts.

This is the persistence boundary between canonical chunking and a future
embedding/indexing layer. It stores chunks and explicit rejections together,
with a checksum manifest, so vectors can always be traced to the exact source
document version and chunking policy that produced them.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import tempfile

from pydantic import BaseModel

from backend.ingestion.chunking.contracts import ChunkBuildResult


class ChunkArtifactManifest(BaseModel):
    """Describes one immutable chunk artifact and its source lineage."""

    schema_version: str = "1.0"
    document_id: str
    agent_id: str
    source_version: str
    source_hash: str
    chunking_policy_version: str
    chunk_count: int
    rejection_count: int
    status: str
    chunk_artifact_path: str
    artifact_hash: str
    created_at: datetime


class StoredChunkArtifact(BaseModel):
    """Returns durable artifact paths created or reused by one chunk persistence call."""

    chunks_path: Path
    manifest_path: Path
    manifest: ChunkArtifactManifest
    reused_existing_artifact: bool = False


class ChunkArtifactStore:
    """Writes chunk records atomically without coupling to an embedding database."""

    _SAFE_PATH_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")

    def __init__(self, storage_root: str | Path | None = None) -> None:
        """Uses the shared project storage root unless a test or deployment overrides it."""
        self._storage_root = Path(storage_root) if storage_root else Path(__file__).resolve().parents[3] / "storage"

    def persist(self, *, document_id: str, agent_id: str, source_version: str, source_hash: str,
                result: ChunkBuildResult) -> StoredChunkArtifact:
        """Stores one policy-versioned chunk result, rejecting unsafe overwrite attempts."""
        agent = self._safe_component(agent_id, "agent_id")
        document = self._safe_component(document_id, "document_id")
        version = self._safe_component(source_version, "source_version")
        policy = self._safe_component(result.policy_version, "policy_version")
        chunks_path = self._storage_root / "chunks" / agent / document / version / policy / "chunks.json"
        manifest_path = self._storage_root / "chunk-manifests" / agent / document / version / f"{policy}.json"
        serialized = result.model_dump_json(indent=2).encode("utf-8")
        if chunks_path.exists():
            if chunks_path.read_bytes() != serialized:
                raise FileExistsError("Chunk artifact already exists with different content; use a new chunking policy version.")
            if not manifest_path.is_file():
                raise RuntimeError("Chunk artifact exists but its manifest is missing.")
            manifest = ChunkArtifactManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
            self._verify_manifest(chunks_path, manifest)
            return StoredChunkArtifact(chunks_path=chunks_path, manifest_path=manifest_path,
                manifest=manifest, reused_existing_artifact=True)
        artifact_hash = hashlib.sha256(serialized).hexdigest()
        has_blocking_rejection = any(rejection.severity == "error" for rejection in result.rejected)
        manifest = ChunkArtifactManifest(document_id=document_id, agent_id=agent_id, source_version=source_version,
            source_hash=source_hash, chunking_policy_version=result.policy_version, chunk_count=len(result.chunks),
            rejection_count=len(result.rejected), status="needs_review" if has_blocking_rejection else "ready_for_embedding",
            chunk_artifact_path=self._relative_path(chunks_path), artifact_hash=artifact_hash,
            created_at=datetime.now(timezone.utc))
        self._atomic_write(chunks_path, serialized)
        try:
            self._atomic_write(manifest_path, manifest.model_dump_json(indent=2).encode("utf-8"))
        except Exception:
            chunks_path.unlink(missing_ok=True)
            raise
        return StoredChunkArtifact(chunks_path=chunks_path, manifest_path=manifest_path, manifest=manifest)

    def load(self, artifact: StoredChunkArtifact) -> ChunkBuildResult:
        """Loads a chunk artifact only after confirming its manifest checksum."""
        self._verify_manifest(artifact.chunks_path, artifact.manifest)
        return ChunkBuildResult.model_validate_json(artifact.chunks_path.read_text(encoding="utf-8"))

    def _verify_manifest(self, chunks_path: Path, manifest: ChunkArtifactManifest) -> None:
        """Prevents a modified chunk file from being silently reused for indexing."""
        if not chunks_path.is_file():
            raise FileNotFoundError(f"Chunk artifact is missing: {chunks_path}")
        actual_hash = hashlib.sha256(chunks_path.read_bytes()).hexdigest()
        if actual_hash != manifest.artifact_hash:
            raise RuntimeError("Chunk artifact checksum does not match its manifest.")

    def _atomic_write(self, target_path: Path, content: bytes) -> None:
        """Publishes a complete file atomically so readers never observe partial JSON."""
        target_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".chunk-", suffix=".tmp", dir=target_path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary_name, target_path)
            except FileExistsError:
                raise FileExistsError("Chunk artifact appeared while being written; retry after reading the existing version.")
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    def _safe_component(self, value: str, field_name: str) -> str:
        """Rejects path traversal in identities controlled by source or application metadata."""
        if not self._SAFE_PATH_COMPONENT.fullmatch(value):
            raise ValueError(f"{field_name} contains unsupported path characters.")
        return value

    def _relative_path(self, path: Path) -> str:
        """Stores portable manifest paths relative to the configured storage root."""
        return str(path.relative_to(self._storage_root))

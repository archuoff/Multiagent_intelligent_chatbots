"""Structure-aware chunking package for the JLR canonical ingestion pipeline.

Files in this folder:
- ``contracts.py`` defines provider-neutral embedding chunk records and policy.
- ``builder.py`` maps canonical narrative nodes and table rows to parent-linked chunks.
- ``validation.py`` checks chunk invariants before any embedding call.
- ``service.py`` combines building and validation for application callers.
- ``chunk_store.py`` writes immutable chunk JSON and checksum manifests.

Connection flow: ``parsers/`` create Master JSON -> ``validation/`` approves it
-> this package creates child chunks -> a future retrieval/indexing package adds
embeddings and vectors. ``preparation/branching.py`` remains responsible for
the independent SQL path; this package never replaces source rows with text.
"""

from backend.ingestion.chunking.builder import CanonicalChunkBuilder
from backend.ingestion.chunking.chunk_store import ChunkArtifactManifest, ChunkArtifactStore, StoredChunkArtifact
from backend.ingestion.chunking.contracts import ChunkBuildResult, ChunkRejection, ChunkType, ChunkingPolicy, EmbeddingChunk
from backend.ingestion.chunking.service import ChunkingService
from backend.ingestion.chunking.validation import ChunkValidator

__all__ = ["CanonicalChunkBuilder", "ChunkArtifactManifest", "ChunkArtifactStore", "ChunkBuildResult", "ChunkRejection", "ChunkType", "ChunkingPolicy", "ChunkingService", "ChunkValidator", "EmbeddingChunk", "StoredChunkArtifact"]

"""Public boundary for the JLR canonical ingestion foundation.

Package map:
- ``models.py`` defines the versioned canonical Master JSON contract.
- ``extraction/`` invokes source tools and returns raw ``ExtractionResult``.
- ``parsers/`` transforms raw source output into ``CanonicalDocument``.
- ``service.py`` routes files through the parser registry.
- ``persistence/`` stores immutable canonical JSON artifacts and manifests.
- ``validation/`` checks canonical quality before persistence.
- ``chunking/`` creates parent-linked, embedding-ready chunks after validation.
- ``embedding/`` batches approved chunks through a configured embedding provider.
- ``indexing/`` persists validated vectors in the local Qdrant retrieval index.
- ``enrichment/`` applies field, source-link, and visual-asset governance metadata.
- ``preparation/branching.py`` separates canonical nodes for future SQL, semantic, catalog,
  and visual indexing paths.
- ``utils.py`` contains shared deterministic helper functions.

External connection: application routes call ``IngestionService`` from this
package; completed canonical documents later feed the retrieval/indexing layer.
"""

from backend.ingestion.preparation.branching import BranchingOutputs
from backend.ingestion.preparation.branching import branch_document
from backend.ingestion.chunking.service import ChunkingService
from backend.ingestion.chunking.chunk_store import ChunkArtifactStore
from backend.ingestion.embedding.service import EmbeddingService
from backend.ingestion.indexing.qdrant_store import QdrantVectorStore
from backend.ingestion.indexing.service import EmbeddingIndexingService
from backend.ingestion.enrichment.service import CanonicalEnrichmentService
from backend.ingestion.parsers.document import DocxDocumentParser
from backend.ingestion.parsers.document import PdfDocumentParser
from backend.ingestion.parsers.excel import ExcelWorkbookParser
from backend.ingestion.models import CanonicalDocument
from backend.ingestion.models import CanonicalNode
from backend.ingestion.models import DocumentFamily
from backend.ingestion.models import NodeType
from backend.ingestion.models import SourceType
from backend.ingestion.parsers.base import IngestionSource
from backend.ingestion.parsers.base import ParserContext
from backend.ingestion.parsers.base import ParserRegistry
from backend.ingestion.parsers.base import SourceParser
from backend.ingestion.parsers.powerpoint import PowerPointParser
from backend.ingestion.persistence.canonical_store import CanonicalArtifactStore
from backend.ingestion.service import IngestionService

__all__ = [
    "BranchingOutputs",
    "CanonicalDocument",
    "CanonicalArtifactStore",
    "CanonicalNode",
    "ChunkingService",
    "ChunkArtifactStore",
    "EmbeddingService",
    "QdrantVectorStore",
    "EmbeddingIndexingService",
    "CanonicalEnrichmentService",
    "DocxDocumentParser",
    "DocumentFamily",
    "ExcelWorkbookParser",
    "IngestionService",
    "IngestionSource",
    "NodeType",
    "ParserContext",
    "ParserRegistry",
    "PdfDocumentParser",
    "PowerPointParser",
    "SourceParser",
    "SourceType",
    "branch_document",
]

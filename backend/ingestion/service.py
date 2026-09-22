"""Ingestion service orchestrator for Phase 1 canonicalization.

This file wires parser registration, source routing, canonical document creation,
and branching output generation into one simple application-facing service.
"""

from __future__ import annotations

from pathlib import Path
from dataclasses import asdict
import hashlib
import json
import os
from typing import Mapping

from backend.ingestion.preparation.branching import BranchingOutputs
from backend.ingestion.preparation.branching import branch_document
from backend.ingestion.enrichment import CanonicalEnrichmentService
from backend.ingestion.parsers.document import DocxDocumentParser
from backend.ingestion.parsers.document import PdfDocumentParser
from backend.ingestion.parsers.excel import ExcelWorkbookParser
from backend.ingestion.models import CanonicalDocument
from backend.ingestion.models import SourceType
from backend.ingestion.persistence import CanonicalArtifactStore
from backend.ingestion.persistence import SourceRegistry
from backend.ingestion.persistence.canonical_store import StoredCanonicalArtifact
from backend.ingestion.parsers.base import IngestionSource
from backend.ingestion.parsers.base import ParserContext
from backend.ingestion.parsers.base import ParserRegistry
from backend.ingestion.parsers.powerpoint import PowerPointParser
from backend.ingestion.validation.quality import CanonicalQualityValidator
from backend.ingestion.validation.quality import QualityReport
from backend.ingestion.utils import hash_file


class IngestionService:
    """Coordinates parser lookup, canonical parsing, and downstream branching."""

    def __init__(self, *, trusted_reference_hosts: tuple[str, ...] = (),
                 field_policy_overrides: Mapping[str, dict] | None = None) -> None:
        self._registry = ParserRegistry()
        self._quality_validator = CanonicalQualityValidator()
        self._enrichment_service = CanonicalEnrichmentService(trusted_reference_hosts, field_policy_overrides)
        self._register_default_parsers()

    # This function registers the source parsers supported by the current implementation.
    def _register_default_parsers(self) -> None:
        self._registry.register(ExcelWorkbookParser())
        self._registry.register(PdfDocumentParser())
        self._registry.register(DocxDocumentParser())
        self._registry.register(PowerPointParser())

    # This function parses a raw source into the shared canonical document representation.
    def ingest_source(
        self,
        *,
        agent_id: str,
        source_type: SourceType,
        file_path: str | Path,
        version: str = "v1",
        document_id: str | None = None,
        context: ParserContext | None = None,
    ) -> CanonicalDocument:
        parser_context = context or ParserContext()
        source = IngestionSource(
            agent_id=agent_id,
            source_type=source_type,
            file_path=Path(file_path),
            version=version,
            document_id=document_id,
        )
        parser = self._registry.resolve(source_type)
        document = parser.parse(source, parser_context)
        self._enrichment_service.enrich(document)
        self._apply_quality_report(document, self._quality_validator.validate(document))
        return document

    # This function resolves source identity, skips unchanged files, then persists a new immutable version.
    def ingest_and_persist(
        self,
        *,
        agent_id: str,
        source_type: SourceType,
        file_path: str | Path,
        context: ParserContext | None = None,
        artifact_store: CanonicalArtifactStore | None = None,
    ) -> tuple[CanonicalDocument, StoredCanonicalArtifact]:
        store = artifact_store or CanonicalArtifactStore()
        path = Path(file_path)
        source_hash = hash_file(path)
        parser_context = context or ParserContext()
        self._load_runtime_environment()
        processing_key = hashlib.sha256(json.dumps({
            "revision": "governance-enrichment-v9",
            "context": asdict(parser_context),
            "ocr": os.getenv("JLR_DOCLING_OCR", "false"),
            "image_description": os.getenv("JLR_IMAGE_DESCRIPTION", "false"),
            "image_description_scope": os.getenv("JLR_IMAGE_DESCRIPTION_SCOPE", "candidates"),
            "vision_deployment": os.getenv("AZURE_OPENAI_VISION_DEPLOYMENT", ""),
        }, sort_keys=True).encode()).hexdigest()
        registration = SourceRegistry(store.storage_root).resolve(
            agent_id=agent_id,
            source_type=source_type,
            file_path=path,
            source_hash=source_hash,
            processing_key=processing_key,
        )
        if registration.is_unchanged:
            artifact = store.load(
                agent_id=agent_id,
                document_id=registration.document_id,
                version=registration.version,
            )
            return CanonicalDocument.model_validate_json(artifact.master_json_path.read_text(encoding="utf-8")), artifact

        document = self.ingest_source(
            agent_id=agent_id,
            source_type=source_type,
            file_path=path,
            version=registration.version,
            document_id=registration.document_id,
            context=context,
        )
        status = "validated" if not document.quality.errors else "needs_review"
        if document.source_hash != source_hash or hash_file(path) != source_hash:
            raise RuntimeError("Source changed during extraction; retry ingestion.")
        artifact = store.persist(document, status=status, source_key=registration.source_key, processing_key=processing_key)
        return document, artifact

    def _load_runtime_environment(self) -> None:
        """Loads optional local runtime flags before source-change detection."""
        try:
            from dotenv import load_dotenv
        except ImportError:
            return
        load_dotenv(override=False)

    # This function validates a document explicitly when callers need the full report.
    def validate_document(self, document: CanonicalDocument) -> QualityReport:
        return self._quality_validator.validate(document)

    # This function parses a source and immediately returns the branching outputs for downstream stores.
    def ingest_and_branch(
        self,
        *,
        agent_id: str,
        source_type: SourceType,
        file_path: str | Path,
        version: str = "v1",
        context: ParserContext | None = None,
    ) -> tuple[CanonicalDocument, BranchingOutputs]:
        document = self.ingest_source(
            agent_id=agent_id,
            source_type=source_type,
            file_path=file_path,
            version=version,
            context=context,
        )
        return document, branch_document(document)

    # This function records deterministic validation findings in the canonical quality metadata.
    def _apply_quality_report(self, document: CanonicalDocument, report: QualityReport) -> None:
        for issue in report.issues:
            message = f"[{issue.code}] {issue.message}"
            target = document.quality.errors if issue.severity == "error" else document.quality.warnings
            if message not in target:
                target.append(message)

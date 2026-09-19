"""Canonical enrichment coordinator for UAT-driven field, reference, and visual policy.

``IngestionService`` calls this after parsing and before quality validation,
persistence, and chunking. It is deterministic, makes no remote network calls,
and preserves all original source content in canonical nodes.
"""

from __future__ import annotations

from typing import Mapping

from backend.ingestion.enrichment.contracts import FieldPolicy
from backend.ingestion.enrichment.field_policy import FieldPolicyResolver
from backend.ingestion.enrichment.image_description import ImageDescriptionEnrichment
from backend.ingestion.enrichment.image_ocr import ImageOcrEnrichment
from backend.ingestion.enrichment.references import ReferenceRegistry
from backend.ingestion.enrichment.visual_assets import VisualAssetRegistry
from backend.ingestion.models import CanonicalDocument


class CanonicalEnrichmentService:
    """Adds answer-governance metadata without changing extracted source values."""

    def __init__(self, trusted_reference_hosts: tuple[str, ...] = (),
                 field_policy_overrides: Mapping[str, FieldPolicy | dict] | None = None) -> None:
        """Configures the remote hosts approved by deployment policy for answer citations."""
        self._field_policy = FieldPolicyResolver(field_policy_overrides)
        self._references = ReferenceRegistry(trusted_reference_hosts)
        self._image_ocr = ImageOcrEnrichment()
        self._image_description = ImageDescriptionEnrichment()
        self._visual_assets = VisualAssetRegistry()

    def enrich(self, document: CanonicalDocument) -> CanonicalDocument:
        """Labels fields, registers references/assets, and appends non-blocking audit warnings."""
        field_count = self._field_policy.apply(document)
        references = self._references.apply(document)
        ocr_count = self._image_ocr.apply(document)
        description_count = self._image_description.apply(document)
        assets = self._visual_assets.apply(document)
        if document.root_nodes:
            document.root_nodes[0].attributes["enrichment"] = {"version": "governance-v1",
                "field_policy_count": field_count, "reference_count": len(references), "visual_asset_count": len(assets),
                "image_ocr_count": ocr_count, "image_description_count": description_count}
        for reference in references:
            if reference.status in {"missing_local", "unverified_remote", "unverified_network_path", "invalid"}:
                warning = f"[reference_{reference.status}] Reference {reference.raw_value} is not answer-eligible until approved or available."
                if warning not in document.quality.warnings:
                    document.quality.warnings.append(warning)
        return document

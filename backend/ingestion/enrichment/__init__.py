"""UAT-driven canonical enrichment for references, field visibility, and visual assets.

``field_policy.py`` labels structured cells for retrieval and answer display.
``references.py`` registers local/remote source links without network access.
``image_ocr.py`` stores supported embedded images and extracts local OCR text.
``image_description.py`` optionally calls Azure vision for inferred descriptions.
``visual_assets.py`` registers image metadata for downstream retrieval/auditing.
``service.py`` coordinates the enrichments after parsers create Master JSON.

Connection flow: ``parsers/`` -> this package -> ``validation/`` and
``persistence/`` -> ``chunking/``. Query answer generation will later enforce
the persisted policy flags and cite only answer-eligible source references.
"""

from backend.ingestion.enrichment.service import CanonicalEnrichmentService

__all__ = ["CanonicalEnrichmentService"]

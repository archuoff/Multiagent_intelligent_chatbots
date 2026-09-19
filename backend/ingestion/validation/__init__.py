"""Canonical quality checks used by the ingestion service.

``quality.py`` checks documents from ``parsers/`` against ``models.py``.
``service.py`` records its findings before ``persistence/`` stores artifacts.
Structured tables with unreadable cells retain their source payload and receive
blocking review findings rather than being treated as successful empty tables.
Tests for these checks live separately in ``tests/ingestion/test_quality.py``.
"""

from backend.ingestion.validation.quality import CanonicalQualityValidator, QualityIssue, QualityReport

__all__ = ["CanonicalQualityValidator", "QualityIssue", "QualityReport"]

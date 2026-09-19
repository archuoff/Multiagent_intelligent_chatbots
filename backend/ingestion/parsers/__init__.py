"""Canonical source parsers for the JLR ingestion pipeline.

Files in this package:
- ``base.py`` defines the source, context, registry, and parser interface used
  by ``backend.ingestion.service`` to route ingestion jobs.
- ``document.py`` maps PDF and DOCX extraction output to page/section-based
  canonical nodes, provenance, quality warnings, and fallback behavior.
  Its shared table builder retains structured cells/spans from extraction contracts
  while accepting legacy matrices; PowerPoint uses this same builder.
- ``powerpoint.py`` maps PPT/PPTX extraction output to slide-based canonical
  nodes, including text, tables, notes, and image metadata.
- ``excel.py`` maps openpyxl workbook analysis to sheet, region, row, and cell
  canonical nodes for hybrid SQL and semantic retrieval.

Connection flow:
``backend.ingestion.service`` resolves one parser from this package. The parser
calls ``backend.ingestion.extraction`` for raw source content, then constructs
``backend.ingestion.models.CanonicalDocument``. That document is passed to
``backend.ingestion.preparation.branching`` after ingestion.
"""

from .base import IngestionSource
from .base import ParserContext
from .base import ParserRegistry
from .base import SourceParser
from .document import DocxDocumentParser
from .document import PdfDocumentParser
from .excel import ExcelWorkbookParser
from .powerpoint import PowerPointParser

__all__ = [
    "IngestionSource",
    "DocxDocumentParser",
    "ExcelWorkbookParser",
    "ParserContext",
    "ParserRegistry",
    "PdfDocumentParser",
    "PowerPointParser",
    "SourceParser",
]

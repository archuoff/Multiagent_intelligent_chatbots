"""Raw extraction layer for the canonical ingestion pipeline.

Files in this package:
- ``contracts.py`` defines ``ExtractionResult`` and ``ExtractedPage``, the
  common handoff from all extraction libraries to ``backend.ingestion.parsers``.
  ``ExtractedTable`` retains structured Docling payloads or legacy library matrices;
  canonical builders preserve cell positions/spans and validation flags unsafe tables.
- ``timeout_policy.py`` calculates PDF budgets and exposes the processing caption
  for Docling, CLI tools, and a future UI. Incomplete outcomes travel through
  the extraction contract into canonical quality metadata and persistence.
- ``docling.py`` configures and invokes Docling for PDF, DOCX, and PPTX,
  returning raw hierarchy, Markdown, exported JSON, tables, and image metadata.
  Docling imports are lazy; ``JLR_DOCLING_OCR`` explicitly enables local OCR.
- ``pdf_inspector.py`` detects usable embedded PDF text before extraction, so
  scanned PDFs do not unexpectedly initialize OCR models.
- ``raw_text_index.py`` reads raw PDF word positions without interpretation,
  giving reconciliation a cheap source-position referee for disputed values.
- ``fallbacks.py`` provides local PyMuPDF/pdfplumber/pypdf, python-docx, and
  python-pptx recovery paths when a primary extractor fails.
- ``merger.py`` deterministically chooses the stronger page/slide output and
  merges non-duplicate recovery evidence before a parser creates canonical nodes.
  ``reconciliation.py``
  supplies its page-local lexical, technical-value, and table-cell rules.

Connection flow:
``backend.ingestion.service`` selects a parser -> a parser calls this package
-> this package returns ``ExtractionResult`` -> the parser creates canonical
Master JSON using ``backend.ingestion.models``. The resulting document then
flows to ``backend.ingestion.preparation.branching`` for later SQL and semantic indexing.
"""

from backend.ingestion.extraction.docling import DoclingContentAdapter
from backend.ingestion.extraction.contracts import ExtractedPage
from backend.ingestion.extraction.contracts import ExtractionResult
from backend.ingestion.extraction.fallbacks import DocxFallbackExtractor
from backend.ingestion.extraction.fallbacks import PdfFallbackExtractor
from backend.ingestion.extraction.fallbacks import PowerPointFallbackExtractor
from backend.ingestion.extraction.pdf_inspector import PdfInspection
from backend.ingestion.extraction.pdf_inspector import PdfInspector
from backend.ingestion.extraction.raw_text_index import CandidateMatch
from backend.ingestion.extraction.raw_text_index import PdfRawTextIndex
from backend.ingestion.extraction.raw_text_index import RawPdfWord
from backend.ingestion.extraction.merger import ExtractionResultMerger

__all__ = [
    "DoclingContentAdapter",
    "DocxFallbackExtractor",
    "ExtractedPage",
    "ExtractionResult",
    "ExtractionResultMerger",
    "PdfFallbackExtractor",
    "PowerPointFallbackExtractor",
    "CandidateMatch",
    "PdfRawTextIndex",
    "PdfInspection",
    "PdfInspector",
    "RawPdfWord",
]

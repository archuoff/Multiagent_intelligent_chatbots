# JLR Ingestion And Extraction Architecture

## Purpose

This document explains how the current Phase 1 ingestion files connect. Source parsing, extraction, validation and local persistence are implemented. Chunking, embeddings and retrieval remain future work.

## End-To-End Flow

```text
Application caller
  -> backend.ingestion.service.IngestionService
  -> backend.ingestion.parsers (selects source parser)
  -> backend.ingestion.extraction (Docling or local fallback)
  -> ExtractionResult
  -> parser builds CanonicalDocument / Master JSON
  -> backend.ingestion.validation.quality validates it
  -> backend.ingestion.persistence optionally stores it
  -> backend.ingestion.preparation.branching prepares later indexing paths
```

## Folder Map

### `backend/ingestion`

| File | Responsibility | Connects To |
| --- | --- | --- |
| `models.py` | Canonical Master JSON Pydantic models and node taxonomy. | Used by parsers, quality validation, and branching. |
| `service.py` | Routes one source file to the registered parser. | Called by application code; uses `parsers/`. |
| `validation/quality.py` | Validates canonical structure, provenance, duplicate content, confidence, and tables. | Runs after a parser produces `CanonicalDocument`. |
| `preparation/branching.py` | Groups canonical nodes for future SQL, semantic, catalog, and visual indexing. | Consumes `CanonicalDocument`. |
| `persistence/` | Stores canonical JSON and manifests; resolves source identities and versions. | Used by `service.py` for opt-in persistence. |
| `utils.py` | IDs, hashes, text normalization, and cell-range helpers. | Used across ingestion. |

### `backend/ingestion/extraction`

| File | Responsibility | Connects To |
| --- | --- | --- |
| `contracts.py` | Defines `ExtractionResult` and `ExtractedPage`. | Shared handoff to all parsers. |
| `docling.py` | Lazy Docling adapter with explicit PDF options. Extracts raw hierarchy, tables, pictures, Markdown, and Docling JSON. | Used by PDF, DOCX, and PPTX parsers. |
| `timeout_policy.py` | Calculates page/OCR-based PDF time limits and the processing caption. | Used by Docling and corpus validation. |
| `pdf_inspector.py` | Detects native PDF text before Docling. | PDF parser uses it to defer OCR for scanned files. |
| `fallbacks.py` | PyMuPDF/pdfplumber/pypdf PDF recovery; python-docx DOCX recovery; python-pptx PPTX recovery. | Used when Docling is unavailable or fails. |
| `merger.py` | Deterministically merges primary and fallback page content without an LLM. | Used by PDF recovery. |

### `backend/ingestion/parsers`

| File | Responsibility | Connects To |
| --- | --- | --- |
| `base.py` | `SourceParser`, source metadata, context, and registry. | Used by service and all parsers. |
| `document.py` | PDF and DOCX parsing into canonical page/section/table nodes. | Uses extraction results and `models.py`. |
| `powerpoint.py` | PPT/PPTX parsing into canonical slide/text/table/note/image nodes. | Uses extraction results and `models.py`. |
| `excel.py` | XLSX hybrid parsing into workbook, sheet, region, row, and cell nodes. | Uses `openpyxl` and `models.py`. |

## PDF Policy

1. `PdfInspector` checks for usable native text.
2. PDFs use accurate table extraction with a page-based timeout (120-900 seconds per attempt). OCR defaults off; `JLR_DOCLING_OCR=true` explicitly enables it.
3. Low-text/scanned PDFs use local fallback and emit an OCR-deferred warning until a local OCR model policy is configured.
4. Weak Docling content can be merged with local PDF extraction without duplicating text, tables, or images.
5. Confirmed timeout errors allow one capped retry. Unresolved partial results
   are preserved with review errors; see `EXTRACTION_VALIDATION.md` for budgets.

Set `DOCLING_ARTIFACTS_PATH` to a pre-downloaded Docling artifact directory in
offline or corporate-network environments. The adapter passes this directory to
`PdfPipelineOptions.artifacts_path`, preventing runtime model downloads.

## Optional Picture Description Policy

Image understanding is disabled by default. Set `JLR_DOCLING_PICTURE_DESCRIPTION=true`
only for a controlled canary because this sends picture crops to a remote Azure
OpenAI vision deployment through Docling's `PictureDescriptionApiOptions`.

Required runtime variables:

- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_API_VERSION`
- `AZURE_OPENAI_VISION_DEPLOYMENT`

Optional tuning variables:

- `JLR_DOCLING_PICTURE_AREA_THRESHOLD`, default `0.03`
- `JLR_DOCLING_PICTURE_DESCRIPTION_TIMEOUT`, default `60`
- `JLR_DOCLING_PICTURE_DESCRIPTION_CONCURRENCY`, default `1`
- `JLR_DOCLING_PICTURE_DESCRIPTION_PROMPT`

Canary sequence:

1. Validate the Azure vision endpoint directly with
   `tests/ingestion/tools/validate_vision_endpoint.py`.
2. Run one PPTX through `validate_ingestion.py` with
   `JLR_DOCLING_PICTURE_DESCRIPTION=true`.
3. Check image nodes for `attributes.description`.
4. Spot-check low-text, diagram-heavy slides before enabling this for a corpus.

Docling uses `picture_area_threshold` for this version; do not use the older
or external term `bitmap_area_threshold` in code.

## Quality Gates

`CanonicalQualityValidator` currently flags:

- duplicate node IDs
- provenance document-ID mismatches
- empty page/slide containers
- duplicate canonical text
- low-confidence nodes
- incomplete tables

Quality findings must be persisted with the Master JSON before chunking is introduced.

## Real-File Validation

Use `tests/ingestion/tools/validate_ingestion.py` to run one source without persisting data by default; `--persist` explicitly saves its artifact and manifest.
It calls `IngestionService`, prints parser and extraction mode, counts canonical
nodes, and lists quality warnings/errors. This script is the recommended first
check after changing extraction or parser logic.

## Current Folder Structure

```text
JLR/
  backend/ingestion/
    __init__.py
    models.py
    service.py
    utils.py
    extraction/
      __init__.py
      contracts.py
      docling.py
      timeout_policy.py
      fallbacks.py
      pdf_inspector.py
      merger.py
    parsers/
      __init__.py
      base.py
      document.py
      powerpoint.py
      excel.py
    validation/
      __init__.py
      quality.py
    persistence/
      __init__.py
      canonical_store.py
      source_registry.py
    preparation/
      __init__.py
      branching.py
  tests/ingestion/
    test_quality.py
    test_canonical_store.py
    test_content_preservation.py
    tools/
      __init__.py
      validate_ingestion.py
      audit_extraction.py
  storage/
  docs/
```

Implementation stays under `backend/ingestion/`. Automated tests and manual
verification tools stay under `tests/ingestion/`. Sources and generated artifacts
remain in their existing data/output/storage locations. No old-script wrappers
are retained; use the new paths below from the project root.

```powershell
.\.JLR\Scripts\python.exe -B -m unittest discover -s tests/ingestion -v
.\.JLR\Scripts\python.exe tests\ingestion\tools\validate_ingestion.py --help
.\.JLR\Scripts\python.exe tests\ingestion\tools\audit_extraction.py --help
```

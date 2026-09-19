# Extraction validation

Docling remains primary for PDF, DOCX and PPTX; openpyxl reads Excel. Parsers
convert the extracted content to canonical JSON before storage or chunking.

Recent preservation changes retain blank-header Excel columns, short narrative
rows, source cell coordinates, formula strings and merged-range metadata.
DOCX uses Docling body references to preserve source order and repeated text.
PPTX recovery uses actual native slide boundaries and includes grouped shapes.
PDF table descendants inherit page provenance. Failed PDF table extraction
does not discard readable page text. Empty canonical content blocks validation.

Set `JLR_DOCLING_OCR=true` to enable Docling OCR for scanned PDF processing.
OCR requires the selected local engine and its models; it has not been benchmarked
here. Image interpretation remains a separate phase. Unknown layouts, charts,
handwriting, multi-level headers and nested tables require representative tests.
Excel rejects sheets above `JLR_MAX_SHEET_CELLS` (default 2,000,000 cells in the
used rectangle), rather than silently truncating them. Workbook loading is in
memory; this is not a streaming solution for unbounded source sizes.

## Dynamic PDF Timeouts

`backend/ingestion/extraction/timeout_policy.py` defines the current estimates:

- Initial allowance: `60 + page_count * 6` seconds, or `* 12` with OCR enabled.
- Minimum 120 seconds; maximum 900 seconds per attempt.
- Retry once only for Docling's structured `timeout` error category.
- Retry allowance: at least 300 seconds or twice the first allowance, capped
  at 900 seconds and the remaining 1,800-second overall policy budget.
- If there is no room for a larger attempt, do not retry. A retry starts over.
- Incomplete results retain their extracted content, warnings, error details,
  attempt history and a blocking review finding in canonical quality metadata.
  Persistence records these artifacts as `needs_review`.

Docling's timeout is cooperative and does not forcibly terminate model loading
or native-library threads. The corpus tool now calculates a separate default
process deadline that allows both attempts and 120 seconds of overhead; an
explicit `--timeout` overrides that deadline. Direct service/CLI use does not
provide a hard process watchdog. Tune the defaults using local measurements.

`PDF_PROCESSING_CAPTION` is displayed by the PDF validation CLI and stored in
`parser_info.processing_details` for a future UI:

> Processing time adjusts automatically based on page count and whether OCR is
> enabled. Larger or scanned documents may take longer.

There is no application frontend in this workspace yet. This caption is ready
for a future upload/status view; it is not an image/table caption.

Run `tests/ingestion/tools/validate_ingestion.py` with `--output` to save inspection JSON.
Then run `tests/ingestion/tools/audit_extraction.py SOURCE CANONICAL_JSON` to compare native
source token coverage. This metric does not establish 80-90% extraction quality:
it cannot prove correct cell alignment, reading order, OCR or image interpretation.
Review those separately against labelled source examples, stratified by format
and layout. Keep difficult and failed files in the evaluation set.

Storage verifies checksums on reuse, refuses replacement at file publication,
and includes processing settings in reuse decisions. Local file storage still
needs transactional coordination for concurrent workers and crash recovery.
Existing pre-registry artifacts are readable but do not get automatically migrated.

## Initial evidence

The ADAS Sensor Study workbook, PDC design-rule DOCX, LE grooming presentation,
and Styrolution Luran specification PDF each reached 100% native-text token
coverage on the sampled inspection exports. This is a four-file smoke test, not
a claim of 100% fidelity or an 80-90% benchmark across the JLR corpus.
Repeated text can mask omissions in a token-count metric; assess source blocks,
exact numeric values, table coordinates and reading order separately.

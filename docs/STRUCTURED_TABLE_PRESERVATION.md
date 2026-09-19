# Structured Table Preservation

Docling table content is retained before any optional DataFrame conversion.
This preserves extracted content; it does not guarantee that source extraction
captured every cell correctly.

## File Connections

1. `extraction/docling.py` exports table model data, including cells and provenance.
   Structured cells do not need DataFrame conversion. If unavailable, the adapter
   tries dictionary export and then a compatibility DataFrame/list representation.
2. `extraction/contracts.py` accepts structured table payloads and legacy matrices.
   PDF/DOCX/PPTX local fallback adapters can continue returning matrices.
3. `extraction/merger.py` preserves structured tables during recovery. Simple tables
   with identical cell text and coordinates are matched across representations;
   complex merged tables are not guessed to be equivalent.
4. `parsers/document.py` provides the canonical table builder shared by PDF, DOCX
   and `parsers/powerpoint.py`. It groups cells by their source starting row without
   expanding spans or assuming that the first row is a header.
5. `validation/quality.py` reports export warnings and blocks clean validation of
   uninterpretable tables. The original table payload remains in node attributes.
6. `service.py` uses a new processing revision so cached older extractions are not
   silently reused. Tests live separately in `tests/ingestion/test_structured_tables.py`.

## Canonical Representation

Structured tables have `representation: structured_cells`, row nodes, and cell
nodes. Cells preserve original header flags, spans, offsets and bounding boxes.
Canonical row/column indices are one-based; original Docling offsets remain
zero-based. `source_table` and `raw_provenance` preserve the original export.
Multiple header rows remain labelled cells rather than one flattened header.
Row semantic text is a convenience view, not an authoritative reconstruction.

Empty, malformed, or unresolved rich-cell tables are retained and flagged for
review. A Markdown export failure cannot discard otherwise available structured
content. This does not add a new source extraction engine or change Excel's
openpyxl path. Future chunking/SQL consumers must understand the representation
and must not assume every table has a separate TABLE_HEADER node.

## Verification

Run `.\.JLR\Scripts\python.exe -B -m unittest discover -s tests/ingestion -v`.
Tests cover structured cells, merged/multi-row headers, invalid coordinates,
empty tables, export failures, dictionary compatibility, native matrices,
merging, DOCX reference traversal and the installed Docling table model.
Source-to-output checks remain necessary for real extraction accuracy.

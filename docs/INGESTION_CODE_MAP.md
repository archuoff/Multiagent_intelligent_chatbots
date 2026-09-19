# Ingestion Code Map

This document explains every Python file under `backend/ingestion/`.

## Overall Flow

```text
raw source file
  -> extraction/
  -> parsers/
  -> Canonical Master JSON
  -> enrichment/ + validation/
  -> persistence/
  -> chunking/
  -> embedding/
  -> indexing/ (Qdrant)
```

## Root Files

- `__init__.py`: public entry point. It exports the main services and models so
  other backend code can import them from `backend.ingestion`.

- `models.py`: defines the Master JSON schema.
  - `SourceType`: supported source kinds such as PDF, DOCX, PPTX, and XLSX.
  - `DocumentFamily`: broad source-layout family such as page-centric or workbook.
  - `NodeType`: canonical node labels such as section, paragraph, table, row, cell, and image.
  - `SecurityScope`: security metadata attached to canonical content.
  - `ParserInfo`: parser name, extraction mode, warnings, and processing details.
  - `LifecycleMetadata`: creation/update lifecycle details.
  - `QualityMetadata`: validation warnings and blocking errors.
  - `Provenance`: source location such as page, slide, sheet, cell range, and parent node.
  - `SurroundingText`: text connected to a table, image, or other content block.
  - `SheetAttributes`, `RegionAttributes`, `TableAttributes`, `TableHeaderAttributes`,
    `TableRowAttributes`, `TableCellAttributes`: structured Excel/table details.
  - `ImageAttributes`, `EmbeddedDatasetAttributes`, `UnknownBlockAttributes`: metadata for
    visual, embedded-data, and uncertain source content.
  - `CanonicalNode`: one node in the normalized document hierarchy.
  - `DocumentMetadata`: document-level title, family, and business metadata.
  - `CanonicalDocument`: complete Master JSON for one document version.

- `utils.py`: small shared deterministic helpers.
  - `make_id()`: creates a stable unique identifier with a readable prefix.
  - `hash_file()`: calculates the source file SHA-256 hash for duplicate/update detection.
  - `normalize_text()`: cleans whitespace while preserving readable content.
  - `make_cell_range()`: converts row/column positions into Excel notation such as `B4:D4`.
  - `_excel_column()`: converts a numeric column number into letters such as `27 -> AA`.

- `service.py`: main ingestion coordinator.
  - `IngestionService.__init__()`: creates the parser registry, quality validator, and enrichment service.
  - `_register_default_parsers()`: registers Excel, PDF, DOCX, and PowerPoint parsers.
  - `ingest_source()`: parses one file into Master JSON and applies enrichment and validation.
  - `ingest_and_persist()`: detects unchanged sources, creates a new version only when changed,
    then persists canonical JSON and its manifest.
  - `validate_document()`: returns a detailed quality report for one Master JSON document.
  - `ingest_and_branch()`: parses a document and produces downstream SQL/semantic/visual branches.
  - `_apply_quality_report()`: writes validator findings into the document quality metadata.

## `extraction/`

- `__init__.py`: exports the extraction contracts, Docling adapter, fallback extractors, timeout
  policy, merge service, and reconciliation service.

- `contracts.py`: raw extraction structures before canonicalization.
  - `ExtractedPage`: raw text blocks, tables, images, and notes from one PDF page or slide.
  - `ExtractionResult`: all raw extractor output, including warnings and processing details.

- `docling.py`: primary Docling extraction adapter.
  - `DoclingContentAdapter.__init__()`: sets PDF timeout policy and prepares lazy converter creation.
  - `_build_converter()`: configures Docling conversion and PDF pipeline options.
  - `is_available()`: checks whether Docling is installed and importable.
  - `extract()`: runs Docling, retries a timed-out PDF once, and returns raw extraction results.
  - `_status()`, `_error_details()`, `_has_timeout()`: interpret Docling conversion status safely.
  - `_extract_pages()`: reads Docling hierarchy into page/slide-level raw contracts.
  - `_extract_table()`: preserves Docling structured table cells, headers, spans, and raw payload.
  - `_picture_metadata()`: records caption, classification, and bounding box without vision analysis.
  - `_element_text()`, `_element_type()`, `_page_number()`: obtain stable text, labels, and page provenance.

- `fallbacks.py`: local-library recovery when Docling is unavailable or inadequate.
  - `PdfFallbackExtractor.extract()`: chooses a PDF fallback path.
  - `_extract_with_pymupdf()`: extracts page text and available table content through PyMuPDF.
  - `_extract_page_tables()`: gets page tables through pdfplumber when possible.
  - `_extract_with_pypdf()`: simpler text-only PDF fallback.
  - `DocxFallbackExtractor.extract()`: extracts paragraphs and tables using `python-docx`.
  - `PowerPointFallbackExtractor.extract()`: extracts slides, shape text, tables, groups, and notes using `python-pptx`.
  - `_walk_shapes()`: recursively reads grouped presentation shapes.
  - `_extract_notes()`: retrieves speaker notes when available.

- `pdf_inspector.py`: decides whether a PDF has usable embedded text before expensive recovery work.
  - `PdfInspection`: reports page count and embedded-text usability.
  - `PdfInspection.has_usable_embedded_text()`: tells callers whether OCR/recovery is likely needed.
  - `PdfInspector.inspect()`: checks a PDF with a lightweight reader.

- `timeout_policy.py`: bounded Docling PDF runtime policy.
  - `PdfTimeoutPolicy.__post_init__()`: validates timeout configuration.
  - `initial_timeout()`: calculates the first attempt limit from page count and OCR state.
  - `retry_timeout()`: calculates one allowed larger retry without exceeding total budget.
  - `process_timeout()`: gives the final process-level limit.

- `reconciliation.py`: deterministic comparison between Docling and fallback output.
  - `ExtractionReconciler.compare_text()`: classifies text as duplicate, near duplicate, fallback-only, or conflict.
  - `compare_tables()`: compares table cells and technical values before merging.
  - `_tokens()`, `_coverage()`: calculate lexical overlap.
  - `_technical_values()`, `_technical_difference()`: protect differing values such as dimensions or limits.
  - `_table_cells()`: converts table content to coordinate/value pairs for comparison.

- `merger.py`: safely combines Docling and fallback recovery output.
  - `ExtractionResultMerger.merge()`: keeps primary extraction, adds missing fallback evidence, and marks conflicts.
  - `_copy_page()`: copies a raw page without mutating source data.
  - `_merge_page()`: merges one page/slide after reconciliation.
  - `_blocks()`: normalizes a page into comparable raw blocks.
  - `_best_table_decision()`: chooses the reconciliation decision for one fallback table.
  - `_annotate_table()`: preserves table evidence with its reconciliation decision.
  - `_summary()`: counts duplicate, recovery, and conflict events.

## `parsers/`

- `__init__.py`: exports source parsers and parser base contracts.

- `base.py`: common parser interfaces.
  - `IngestionSource`: input identity: agent, file path, source type, version, and document ID.
  - `ParserContext`: optional parsing controls supplied by the caller.
  - `SourceParser`: abstract parser contract.
  - `SourceParser.supports()`: declares supported source types.
  - `SourceParser.parse()`: requires each parser to return `CanonicalDocument`.
  - `ParserRegistry.register()`: adds a parser.
  - `ParserRegistry.resolve()`: finds the correct parser for a source type.

- `document.py`: converts PDF/DOCX Docling or fallback output into canonical nodes.
  - `DoclingCanonicalBuilder.build_page_nodes()`: creates page-level canonical nodes.
  - `build_section_nodes()`: creates sections/subsections from headings and text hierarchy.
  - `_ordered_docling_nodes()`: reads Docling exported structure in source order.
  - `build_table_nodes()`: creates canonical tables from raw extracted tables.
  - `build_matrix_table_node()`: converts simple matrix tables into table/header/row/cell nodes.
  - `build_structured_table_node()`: preserves rich Docling cell positions, spans, and headers.
  - `_find_page_sections()`, `_extract_text_blocks()`, `_extract_text_fragments()`,
    `_find_payload_list()`, `_find_table_candidates()`: locate usable content in variable Docling payload shapes.
  - `_normalize_table_matrix()`: normalizes legacy table matrices.
  - `_text_to_nodes()`: converts narrative text to title, paragraph, and bullet nodes.
  - `_normalize_column_names()`, `_row_semantic_text()`, `_sanitize_column_name()`: prepare table rows for structured and semantic retrieval.
  - `_make_provenance()`: creates page/source traceability metadata.
  - `_is_heading()`, `_strip_heading()`, `_is_bullet()`, `_strip_bullet()`: recognize narrative structure.
  - `_infer_title()`, `_split_markdown_sections()`, `_split_markdown_blocks()`, `_dedupe_preserve_order()`:
    organize markdown-derived fallback content without losing order.
  - `PdfDocumentParser.parse()`: primary PDF parser: Docling first, then fallback/reconciliation when needed.
  - `_build_fallback_pages()`: turns raw fallback pages into canonical page nodes.
  - `_set_page_provenance()`: applies page number to a canonical branch.
  - `_collect_docling_warnings()`: keeps relevant Docling status warnings in Master JSON.
  - `DocxDocumentParser.parse()`: primary DOCX parser: Docling first, then fallback/reconciliation when needed.
  - `_build_fallback_children()`: creates canonical children from DOCX fallback results.

- `powerpoint.py`: converts presentation output into slide-centric Master JSON.
  - `PowerPointParser.parse()`: primary PPT/PPTX parser with Docling and local fallback handling.
  - `_build_docling_slides()`: creates slide nodes from Docling hierarchy.
  - `_build_extracted_slides()`: creates slide nodes from fallback extraction output.
  - `_collect_docling_warnings()`: records conversion/reconciliation warnings.

- `excel.py`: parses workbooks directly with `openpyxl` into canonical workbook/sheet/region/table nodes.
  - `ExcelWorkbookParser.parse()`: creates the full workbook Master JSON.
  - `_build_workbook_node()`, `_build_sheet_node()`, `_build_region_node()`: create Excel hierarchy containers.
  - `_build_table_node()`, `_build_row_node()`: create structured table/row/cell canonical nodes.
  - `_detect_regions()`: separates a sheet into non-empty content regions.
  - `_classify_sheet()`: labels a sheet as structured, document-like, or mixed.
  - `_collect_workbook_warnings()`: records hidden sheets, weak ranges, and other workbook risks.
  - `_collect_non_empty_rows()`, `_group_consecutive_rows()`, `_row_values()`: locate data blocks.
  - `_analyze_region()`, `_looks_like_table()`: decide whether a region is tabular or narrative.
  - `_infer_title()`, `_infer_header_row_index()`, `_infer_column_indexes()`, `_infer_columns()`:
    identify title/header/column structure without assuming one template.
  - `_materialize_rows()`: preserves every row and cell with values and source coordinates.
  - `_infer_units()`, `_infer_unit()`, `_value_type()`: preserve units and data types for SQL use.
  - `_extract_region_notes()`: retains nearby narrative notes.
  - `_table_name()`, `_sanitize_column_name()`: generate stable SQL-safe names.
  - `_row_semantic_text()`: builds text such as `OEM: X | Sensor: Y` for vector retrieval.
  - `_used_range()`: records the worksheet’s occupied cell range.

## `validation/`

- `__init__.py`: exports canonical validation classes.

- `quality.py`: validates Master JSON before automatic chunking.
  - `QualityIssue`: one warning or error with code and message.
  - `QualityReport`: all validation findings.
  - `QualityReport.is_valid()`: true only when there are no blocking errors.
  - `CanonicalQualityValidator.validate()`: runs all canonical checks.
  - `_walk_nodes()`: flattens the node hierarchy for checks.
  - `_validate_node_ids()`: detects duplicate or missing node IDs.
  - `_validate_reconciliation()`: converts unresolved extraction conflicts into blocking errors.
  - `_validate_provenance()`: checks page/sheet/document traceability.
  - `_validate_content()`: detects empty/repeated/weak content.
  - `_validate_tables()`: detects incomplete or malformed tables.

## `enrichment/`

- `__init__.py`: exports enrichment contracts and the enrichment service.

- `contracts.py`: governance metadata structures.
  - `FieldUse`: standard, reference-link, or internal-only field classification.
  - `FieldPolicy`: answer/retrieval visibility policy for one field.
  - `ReferenceRecord`: a detected URL, local path, or network reference and its status.
  - `VisualAssetRecord`: traceable metadata for an image or visual asset.

- `field_policy.py`: decides how source fields may be used.
  - `FieldPolicyResolver.__init__()`: accepts optional agent-specific field overrides.
  - `resolve()`: classifies one field name using shared rules and overrides.
  - `apply()`: attaches field policies to relevant canonical table cells/rows.
  - `_walk()`: traverses all canonical nodes.

- `references.py`: discovers and registers source links.
  - `ReferenceRegistry.__init__()`: sets trusted remote hosts.
  - `apply()`: finds references across canonical content and attaches records.
  - `_extract_node_references()`: finds URL/path values in one node.
  - `_record()`: classifies a candidate reference.
  - `_local_path_record()`: checks a local path without accessing network shares.
  - `_invalid_record()`: records malformed references safely.
  - `_looks_like_windows_path()`, `_file_uri_path()`: interpret Windows/file URI paths.
  - `_walk()`: traverses nodes.

- `visual_assets.py`: records visual evidence without attempting image understanding.
  - `VisualAssetRegistry.apply()`: creates a document visual-asset registry.
  - `_node_assets()`: converts image-node metadata to visual records.
  - `_walk()`: traverses nodes.

- `service.py`: applies all enrichment modules.
  - `CanonicalEnrichmentService.__init__()`: configures field, reference, and visual services.
  - `enrich()`: applies those policies/registries to one Master JSON document.

## `persistence/`

- `__init__.py`: exports canonical artifact and source-registry classes.

- `canonical_store.py`: immutable Master JSON persistence.
  - `CanonicalArtifactManifest`: source hash, parser, status, version, and file-location record.
  - `StoredCanonicalArtifact`: returned paths for Master JSON and manifest.
  - `CanonicalArtifactStore.persist()`: atomically saves a new canonical version and checksum manifest.
  - `load()`: loads a canonical artifact only after checksum validation.
  - `_safe_component()`: prevents unsafe identifiers from becoming paths.
  - `_relative_path()`: stores portable manifest paths.
  - `_atomic_write()`: prevents incomplete JSON files.

- `source_registry.py`: source change/version detection.
  - `SourceRegistration`: resolved document ID, version, and unchanged/changed status.
  - `SourceRegistry.resolve()`: compares a source hash and processing key with prior manifests.
  - `source_key()`: creates a stable identity for the source location and agent.
  - `_manifests_for_agent()`: reads past manifests for one agent.
  - `_version_number()`: parses version labels such as `v2`.

## `preparation/`

- `__init__.py`: exports branching helpers.

- `branching.py`: separates canonical content for future downstream stores.
  - `BranchingOutputs`: lists nodes intended for SQL, semantic, catalog, and visual paths.
  - `branch_document()`: classifies canonical nodes into those downstream branches.
  - `walk_nodes()`: traverses all nodes in a document.

## `chunking/`

- `__init__.py`: exports chunking contracts, builder, validator, store, and service.

- `contracts.py`: chunking data structures.
  - `ChunkType`: `narrative` or `table_row` retrieval behavior.
  - `ChunkingPolicy`: maximum size and overlap rules.
  - `EmbeddingChunk`: one parent-linked embedding-ready retrieval record.
  - `ChunkRejection`: content withheld from automatic embedding.
  - `ChunkBuildResult`: accepted chunks plus explicit rejections.

- `builder.py`: creates structure-aware chunks.
  - `CanonicalChunkBuilder.build()`: creates chunks from Master JSON.
  - `_entries()`: flattens nodes while retaining parent and breadcrumb context.
  - `_requires_review()`: blocks nodes with extraction conflicts.
  - `_narrative_chunks()`: groups related paragraphs under a shared parent section.
  - `_table_chunks()`: produces one searchable chunk per table row.
  - `_table_headers()`, `_table_header_context()`: retain header meaning with each row.
  - `_row_text()`: converts row cells into labelled semantic text.
  - `_new_chunk()`: creates a deterministic chunk ID and full metadata.
  - `_context_prefix()`: adds document/section context to embedding text.
  - `_source_metadata()`: attaches source, provenance, and security metadata.
  - `_field_policies()`: carries answer/retrieval field rules to chunks.
  - `_references_for_nodes()`: carries eligible source references to chunks.
  - `_overlap_nodes()`: retains complete neighboring nodes when a chunk splits.
  - `_split_text()`, `_hard_split()`: split oversized narrative text safely.

- `validation.py`: validates chunks before embedding.
  - `ChunkValidator.validate()`: checks chunk IDs, text, parent links, metadata, and source lineage.

- `chunk_store.py`: immutable chunk persistence.
  - `ChunkArtifactManifest`: checksum, source lineage, policy version, and ready/review status.
  - `StoredChunkArtifact`: returned chunk and manifest paths.
  - `ChunkArtifactStore.persist()`: saves `chunks.json` and manifest atomically.
  - `load()`: verifies checksum and loads chunks.
  - `_verify_manifest()`, `_atomic_write()`, `_safe_component()`, `_relative_path()`: safety helpers.

- `service.py`: simple chunking coordinator.
  - `ChunkingService.build()`: calls builder then validator.
  - `build_and_persist()`: creates chunks and writes their artifact.

## `embedding/`

- `__init__.py`: exports the Azure adapter, contracts, and embedding service.

- `contracts.py`: embedding data structures.
  - `EmbeddingModelSettings`: non-secret deployment, dimensions, batch, and retry settings.
  - `EmbeddedChunk`: one chunk ID paired with a validated vector.
  - `EmbeddingBuildResult`: all vectors generated from one chunk artifact.

- `azure_openai.py`: Azure OpenAI SDK adapter.
  - `AzureOpenAIEmbeddingProvider.__init__()`: accepts non-secret settings and a live/test client.
  - `from_runtime_environment()`: reads required Azure settings only at application runtime.
  - `embed()`: calls the deployment and restores response order using Azure indexes.
  - `_load_dotenv_if_available()`: loads local runtime variables without overwriting process values.
  - `_required_environment_value()`: fails safely when a needed setting is absent.

- `service.py`: provider-independent embedding processing.
  - `EmbeddingProvider`: minimal interface required from any embedding provider.
  - `EmbeddingService.embed()`: blocks invalid chunks, batches text, and creates vector results.
  - `_embed_with_retry()`: retries a failed provider request with bounded backoff.
  - `_validate_vector()`: checks numeric values and expected dimension count.
  - `_embedding_metadata()`: carries only retrieval lineage to the future vector index.

## `indexing/`

- `__init__.py`: exports Qdrant settings, store, and safe indexing service.

- `contracts.py`: index configuration/results.
  - `QdrantIndexSettings`: collection prefix and write batch size.
  - `VectorIndexWriteResult`: collection name, indexed chunk IDs, document, and version.

- `qdrant_store.py`: local persistent Qdrant adapter.
  - `QdrantVectorStore.upsert()`: verifies lineage and writes vectors with filterable payload metadata.
  - `delete_document_version()`: removes one obsolete document version from one agent collection.
  - `_get_client_and_models()`: creates Qdrant Local Mode client lazily.
  - `_collection_name()`: creates safe, agent-specific collection names.
  - `_validate_lineage()`: prevents mixing wrong document/version/model vectors.
  - `_payload()`: carries chunk content, lineage, and canonical ACL metadata as Qdrant payload.

- `service.py`: final safe handoff.
  - `EmbeddingIndexingService.embed_and_index()`: checksum-verifies chunks, calls embeddings,
    then writes matching vectors to Qdrant.

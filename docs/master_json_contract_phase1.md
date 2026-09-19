# JLR Master JSON Contract

## Phase 1

**Status:** Implementation-ready design contract for the canonical ingestion model.  
**Purpose:** Convert the agreed architecture into a concrete schema, mapping rules, branching rules, and real-file examples.

---

## 1. Why This Contract Exists

We already agreed on:

- one shared ingestion pipeline
- one shared master JSON
- hybrid Excel handling
- different structure families for different source types

This document makes that usable in implementation.

It defines:

1. the formal schema shape
2. parser-to-schema mapping rules
3. downstream branching rules
4. sample mappings from real JLR files

---

## 2. Formal Schema

## 2.1 Top-level document envelope

Every ingested source should produce one canonical document object.

```json
{
  "document_id": "doc_uuid",
  "agent_id": "adas",
  "source_type": "xlsx",
  "file_name": "ADAS Sensor Study Data.xlsx",
  "source_path": "data/003_ADAS Data/ADAS Sensor Study Data.xlsx",
  "source_name": "ADAS Sensor Study Data",
  "version": "v1",
  "ingested_at": "2026-08-31T00:00:00Z",
  "source_hash": "sha256:...",
  "lifecycle": {
    "status": "active",
    "is_latest": true,
    "effective_from": null,
    "effective_to": null
  },
  "security_scope": {
    "classification": "internal",
    "allowed_groups": [],
    "allowed_users": [],
    "tags": []
  },
  "parser_info": {
    "parser_name": "excel_analyzer",
    "parser_version": "1.0.0",
    "extraction_mode": "canonical"
  },
  "metadata": {
    "title": "ADAS Sensor Study Data",
    "document_family": "Workbook-Based Document",
    "language": "en",
    "page_count": null,
    "sheet_count": 4,
    "slide_count": null,
    "keywords": [],
    "source_system": null
  },
  "root_nodes": [],
  "unassigned_content": [],
  "quality": {
    "overall_confidence": 1.0,
    "warnings": [],
    "errors": []
  }
}
```

## 2.2 Required top-level fields

- `document_id`
- `agent_id`
- `source_type`
- `file_name`
- `source_path`
- `version`
- `ingested_at`
- `source_hash`
- `security_scope`
- `parser_info`
- `metadata`
- `root_nodes`
- `unassigned_content`
- `quality`

## 2.3 Base node schema

Every node must follow this shape.

```json
{
  "node_id": "node_uuid",
  "node_type": "section",
  "title": "Introduction",
  "text": null,
  "children": [],
  "attributes": {},
  "provenance": {},
  "confidence": 0.95
}
```

## 2.4 Required node fields

- `node_id`
- `node_type`
- `children`
- `attributes`
- `provenance`
- `confidence`

## 2.5 Optional node fields

- `title`
- `text`

Why optional:

- a `table` may not need `text`
- a `paragraph` may not need `title`
- a `page` or `sheet` may have neither

## 2.6 Canonical node types

### Structural container nodes

- `document_root`
- `chapter`
- `section`
- `subsection`
- `page`
- `slide`
- `workbook`
- `sheet`
- `region`
- `table_group`
- `form_section`

### Textual content nodes

- `paragraph`
- `text_block`
- `bullet_block`
- `title_block`
- `note_block`
- `guideline_block`
- `reference_block`
- `measurement_block`
- `label_value_block`
- `status_block`
- `caption`

### Structured content nodes

- `table`
- `table_header`
- `table_row`
- `table_cell`
- `field_group`
- `embedded_dataset`

### Visual content nodes

- `image`
- `image_region`
- `callout_label`
- `ocr_block`

### Fallback nodes

- `unknown_block`
- `unassigned_block`
- `low_confidence_block`

## 2.7 Provenance schema

Each node should carry this structure when applicable:

```json
{
  "document_id": "doc_uuid",
  "source_type": "pdf",
  "version": "v1",
  "parent_node_id": "parent_uuid",
  "page_number": 3,
  "slide_number": null,
  "sheet_name": null,
  "cell_range": null,
  "bbox": [0, 0, 100, 100],
  "char_start": null,
  "char_end": null
}
```

### Required provenance fields

- `document_id`
- `source_type`
- `version`

### Optional provenance fields

- `parent_node_id`
- `page_number`
- `slide_number`
- `sheet_name`
- `cell_range`
- `bbox`
- `char_start`
- `char_end`

## 2.8 Node-specific attributes

### `sheet.attributes`

```json
{
  "sheet_name": "Front PDC Sensor",
  "sheet_index": 1,
  "classification": "MIXED",
  "used_range": "A1:F119",
  "is_hidden": false
}
```

### `region.attributes`

```json
{
  "region_kind": "STRUCTURED_TABLE",
  "classification": "STRUCTURED",
  "range": "B4:F40",
  "header_depth": 2
}
```

### `table.attributes`

```json
{
  "table_name": "pdc_sensor_specifications",
  "header_depth": 2,
  "column_count": 5,
  "has_numeric_data": true,
  "is_nested": false,
  "units": ["cm"],
  "surrounding_text": {
    "text_above": "PDC Sensor Specifications",
    "text_below": null
  }
}
```

### `table_row.attributes`

```json
{
  "row_index": 7,
  "row_id": "row_007",
  "is_total_row": false,
  "is_header_repeat": false,
  "semantic_text": "For Mercedes-Benz GLC 300, object detection minimum range is 15 cm."
}
```

### `table_cell.attributes`

```json
{
  "row_index": 7,
  "column_index": 3,
  "column_name": "mercedes_benz_glc_300",
  "value": "15 cm",
  "normalized_value": 15,
  "unit": "cm",
  "value_type": "measurement",
  "formula": null
}
```

### `image.attributes`

```json
{
  "image_id": "img_001",
  "image_type": "part_diagram",
  "saved_path": "extracted/images/...",
  "width": 640,
  "height": 480,
  "description": null,
  "technical_details": null,
  "is_authoritative": false
}
```

### `embedded_dataset.attributes`

```json
{
  "dataset_name": "components",
  "dataset_format": "json_script",
  "query_mode": "SQL_AND_SEMANTIC_SUPPORT",
  "record_count": 4
}
```

---

## 3. Parser-to-Schema Mapping Rules

This section answers:

- how do we convert real files into the schema?
- when do we pick one node type over another?
- what do we do when parsing is unclear?

## 3.1 General rules

1. Preserve detected hierarchy when reliable.
2. Do not invent missing hierarchy just to make the tree look neat.
3. If structure is weak, fall back to page/block-based representation.
4. Keep uncertain content in `unknown_block` or `unassigned_content`.
5. Every meaningful node must get provenance.

## 3.2 PDF mapping rules

### If headings are confidently detected

Map as:

- `document_root`
  - `chapter`
    - `section`
      - `subsection`
        - `paragraph`
        - `table`
        - `image`

### If headings are weak or absent

Map as:

- `document_root`
  - `page`
    - `text_block`
    - `table`
    - `image`
    - `caption`

### Table handling

If a PDF table is detected:

- create a `table` node
- preserve:
  - headers
  - rows
  - surrounding text
  - page number
  - bounding box

If the table is messy:

- still create a `table`
- mark `is_nested=true` or add parsing warning in attributes/quality

### Image handling

If a meaningful engineering image is detected:

- create `image`
- add nearby `caption` if present
- add `ocr_block` if OCR text exists
- add `callout_label` children if labels are detected

## 3.3 PPT/PPTX mapping rules

Map as:

- `document_root`
  - `slide`
    - `title_block`
    - `bullet_block`
    - `text_block`
    - `table`
    - `image`
    - `note_block`

Rules:

- slide title becomes `title_block`
- bullet lists become `bullet_block`
- free text becomes `text_block`
- embedded tables become `table`
- diagrams/screenshots become `image`

## 3.4 DOCX mapping rules

If Word heading styles or heading-like patterns are detected:

- `document_root`
  - `section`
    - `subsection`
      - `paragraph`
      - `table`

If structure is weak:

- `document_root`
  - `page` or top-level `text_block`
    - `paragraph`
    - `table`

## 3.5 HTML mapping rules

### Static content HTML

Map as:

- `document_root`
  - `section`
    - `title_block`
    - `text_block`
    - `table`
    - `list-like blocks as bullet_block`

### Dataset-embedded HTML

If the page contains embedded JSON or interactive filter/data schema:

- `document_root`
  - `section`
    - `field_group` or `label_value_block` for filters
    - `embedded_dataset` for JSON data payload
    - `text_block` for visible helper text

Ignore:

- CSS
- theme logic
- layout classes
- general DOM manipulation code

Only retain script-derived content when it carries:

- real dataset payload
- query vocabulary hints
- filter schema

## 3.6 Excel mapping rules

Map as:

- `document_root`
  - `workbook`
    - `sheet`
      - `region`
        - `table`
        - `text_block`
        - `note_block`
        - `image`

### Region classification rules

Each sheet region should be classified as:

- `STRUCTURED`
- `DOCUMENT_LIKE`
- `MIXED`

### When to create a `table`

Create a `table` when the region shows:

- stable column alignment
- header rows
- row-wise data repetition
- structured measurements or categorical values

### When to create `text_block` or `note_block`

Use for:

- narrative text
- instructions
- comments
- title areas
- explanatory note regions

### Multi-row headers

If Excel has multi-row headers:

- keep them in `table_header`
- set `header_depth > 1`

### Formula cells

Keep:

- formula
- displayed value
- normalized value if derivable

### Mixed sheets

If a sheet contains both narrative and tables:

- do not flatten the whole sheet into one table
- create separate `region` nodes

## 3.7 Fallback rules

If parser confidence is low:

- create `low_confidence_block`
- preserve raw text
- preserve location

If content cannot be placed in hierarchy:

- add to `unassigned_content`

---

## 4. Branching Rules

This section answers:

- after canonical JSON is created, what happens next?

## 4.1 Main principle

The master JSON is not the final store.
It branches into downstream stores based on node type.

## 4.2 SQL materialization path

Send these node types to SQL processing:

- `table`
- `table_header`
- `table_row`
- `table_cell`
- `embedded_dataset`

And for Excel:

- `sheet`
- `region`

should supply metadata for dataset/table registration.

### Why

These nodes contain structured information suited to:

- filtering
- counting
- grouping
- sorting
- aggregation
- deterministic row retrieval

## 4.3 Semantic chunking path

Send these node types to chunking/embedding:

- `paragraph`
- `text_block`
- `bullet_block`
- `note_block`
- `guideline_block`
- `reference_block`
- `measurement_block`
- `caption`

Also optionally:

- `table_row`
  as semantic row text
- `table`
  as table summary text
- `ocr_block`
  when meaningful

## 4.4 Catalog registration path

Register metadata from:

- document envelope
- `chapter`
- `section`
- `slide`
- `sheet`
- `region`
- `table`
- `embedded_dataset`
- `image`

Catalog should store:

- source identity
- structure identity
- dataset/table names
- section names
- provenance
- version
- security scope
- query modes

## 4.5 Visual metadata path

Process and retain:

- `image`
- `caption`
- `ocr_block`
- `callout_label`

Use for:

- image-aware retrieval support
- metadata enrichment
- diagram lookup

Do not treat generated image descriptions as primary authoritative facts by default.

## 4.6 Ignore / non-knowledge path

Do not send these to retrieval as core knowledge:

- CSS
- UI wiring logic
- non-semantic frontend script code
- formatting-only layout text

---

## 5. Real-File Sample Mapping Rules For JLR Data

## 5.1 LE Grooming Excel

Source:

- `data/001_LE Grooming/Lead Engineer Grooming.xlsx`

Observed structure:

- one sheet
- stable header row
- text-heavy business columns

Mapping:

- `document_root`
  - `workbook`
    - `sheet`
      - `region`
        - `table`
          - `table_row`
            - `table_cell`

Branching:

- SQL path for full table
- semantic row text path for issue/solution discovery
- catalog path for columns like:
  - `Issue`
  - `Area`
  - `Issue Description`
  - `Solution/Comments / Actions`
  - `LL/Q&A/KSS`
  - `Status`

## 5.2 ADAS Sensor Study Excel

Source:

- `data/003_ADAS Data/ADAS Sensor Study Data.xlsx`

Observed structure:

- multiple sheets
- title rows
- comparison tables
- mixed structured and decorative rows

Mapping:

- `document_root`
  - `workbook`
    - `sheet`
      - `region` title area as `text_block` or `title_block`
      - `region` comparison table as `table`

Branching:

- SQL path for comparison tables
- semantic path for title/note areas
- semantic row text for row-level query matching

## 5.3 Ultrasonic Sensors FMEA Excel

Source:

- `data/003_ADAS Data/Ultrasonic Sensors FMEA.xlsx`

Observed structure:

- multi-row headers
- pre-table administrative text
- mixed layout

Mapping:

- `document_root`
  - `workbook`
    - `sheet`
      - `region` narrative/admin text as `text_block`
      - `region` structured FMEA/control plan as `table`
        - `table_header` with `header_depth > 1`

Branching:

- SQL for table structure
- semantic for notes/admin context only if useful

## 5.4 EDS PPTX

Source examples:

- `data/005_EDS Database For initial study/EDS_Carbon Fiber.pptx`
- `data/005_EDS Database For initial study/EDS_DESIGN GUIDELINES_HEATING ELEMENT..pptx`

Observed structure:

- slide titles
- index slides
- topic slides
- diagrams and text

Mapping:

- `document_root`
  - `slide`
    - `title_block`
    - `bullet_block`
    - `text_block`
    - `image`

Branching:

- semantic path for slide text
- visual metadata path for images
- catalog for slide titles/topics

## 5.5 ADAS DOCX

Source:

- `data/003_ADAS Data/Design rule for PDC sensor.docx`

Observed structure:

- heading-like sections:
  - Introduction
  - Purpose
  - Scope
  - Revision History

Mapping:

- `document_root`
  - `section`
    - `paragraph`

Branching:

- semantic path
- catalog for section names

## 5.6 Material PDF Datasheets

Source example:

- `data/006_New Material and New Edge Technologies/Material Data sheets/Prospector Bayblend PC ABS specification sheet.pdf`

Observed structure:

- title
- product description
- property tables
- units
- methods

Mapping:

- `document_root`
  - `page` or `section`
    - `title_block`
    - `text_block`
    - `table`
    - `caption`

Branching:

- SQL-like structured extraction if tables are clean enough for material property indexing
- semantic path for product description and summaries
- catalog for material name and property groups

## 5.7 Benchmarking HTML

Source:

- `data/002_Benchmarking/TTL-Sample Benchmarking-Webpage Cum AI Chat bot.html`

Observed structure:

- filter schema
- embedded JSON dataset
- UI text

Mapping:

- `document_root`
  - `section`
    - `field_group`
    - `embedded_dataset`
    - `text_block`

Branching:

- SQL path for embedded dataset
- semantic path only for meaningful helper text
- catalog for filter dimensions and dataset fields

---

## 6. Acceptance Rules

The schema is good enough for implementation if:

1. one PDF can be mapped without inventing fake hierarchy
2. one PPT can be mapped slide by slide
3. one Excel workbook can represent both structured and narrative regions
4. one HTML file with embedded data can map both UI schema and dataset payload
5. no uncertain content is silently dropped
6. every retrieval-relevant node has provenance

---

## 7. Immediate Implementation Order

1. implement the envelope and base node models
2. implement provenance and quality models
3. implement node-specific models for:
   - sheet
   - region
   - table
   - table_row
   - table_cell
   - image
4. implement parser mapping adapters
5. validate against:
   - LE Grooming Excel
   - ADAS Sensor Study Excel
   - one EDS PPTX
   - one material PDF

---

## 8. Core Summary

This contract gives you:

- one common master JSON
- one node system
- clear mapping rules per source type
- clear branching rules after canonicalization
- validation against the actual JLR source files

This is the point where architecture becomes implementable.

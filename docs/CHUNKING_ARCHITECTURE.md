# Canonical Chunking Architecture

The chunking package converts validated Master JSON into embedding-ready child
records. It is structure-aware and embedding-provider neutral.

```
Canonical Master JSON -> quality gate -> chunking -> embedding/indexing (future)
                              |
                              +-> SQL materialization remains a separate path
```

## Package Connections

`backend/ingestion/chunking/contracts.py` defines versioned `EmbeddingChunk`,
policy, and rejection records. `builder.py` walks the canonical node tree.
`validation.py` enforces chunk invariants. `service.py` is the application
entry point combining both stages. Tests live in `tests/ingestion/test_chunking.py`.

The package reads `backend/ingestion/models.py`; it never reparses source files.
It runs after canonical validation and before a future embedding/vector-index
layer. `backend/ingestion/preparation/branching.py` remains responsible for
the SQL path and continues to preserve Excel source rows separately.

Before embedding, `chunk_store.py` writes `chunks.json` plus a checksum manifest
under `storage/chunks/<agent>/<document>/<source-version>/<policy-version>/`.
It never overwrites a different artifact at the same source and policy version.
Its manifest records source hash, policy version, chunk/rejection counts, and
whether the artifact is `ready_for_embedding` or `needs_review`.

## Chunk Rules

| Source structure | Chunk strategy | Parent link |
| --- | --- | --- |
| PDF/DOCX narrative | Group ordered text nodes only inside the same canonical parent | Section or page |
| PPTX | Group text only inside the same slide | Slide |
| Excel | Semantic row chunks after canonical row construction | Table/region/sheet |
| PDF/DOCX/PPT tables | One row per chunk, with header context | Table |
| Image content | Deferred until a vision/OCR phase adds authoritative canonical text | Image parent |

Each chunk has `content_text` for display, `embedding_text` for search, stable
`chunk_id`, parent/source node links, provenance coordinates, source version,
and ACL metadata. Narrative overlap uses complete preceding canonical nodes,
not arbitrary fixed-word overlap. Oversized text splits at paragraphs or
sentences, using a whitespace boundary only as a final safety limit.

In strict mode, document-level extraction or reconciliation errors yield no
chunks. Locally conflict-marked branches are excluded even when a caller has
already cleared the document-level gate. The output contains rejection records
for batch reporting; no LLM or embedding model makes these decisions.

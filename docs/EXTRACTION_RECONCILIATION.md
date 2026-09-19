# Extraction Reconciliation

This layer runs only when a primary extractor and a local fallback both provide
content for the same PDF page or PowerPoint slide. It sits after extraction and
before parsers create canonical Master JSON.

```
Docling + local fallback -> reconciliation rules -> canonical parser -> quality validation
```

`extraction/reconciliation.py` compares normalized text and table cells. It is
deterministic, local, and does not call an LLM or an embedding model.
`extraction/merger.py` uses those decisions to retain Docling as primary,
append only useful fallback evidence, and record an audit event per page/slide.
`parsers/document.py` and `parsers/powerpoint.py` transfer events to canonical
page, slide, or table attributes. `validation/quality.py` blocks automatic
progression when an event is a conflict. The source payload and both values stay
available for an SME to inspect.

## Decisions

| Decision | Rule | Result |
| --- | --- | --- |
| `duplicate` | Exact normalized text or identical table cells | Keep primary only. |
| `near_duplicate` | At least 90% of fallback tokens are covered and technical values agree | Keep primary only. |
| `fallback_only` | Fallback adds material not present in primary | Add fallback recovery evidence. |
| `conflict` | Similar text has different numeric/code values, or matching table cells disagree | Retain fallback evidence and require review. |

Text is compared only within one page or slide. Tables are compared by row and
column coordinates plus normalized values. Complex tables without a safe shared
coordinate interpretation are retained as fallback-only; the system does not
invent a match. Embeddings may later nominate ambiguous candidates, but they
must never automatically resolve technical values.

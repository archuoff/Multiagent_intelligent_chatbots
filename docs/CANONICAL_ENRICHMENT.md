# Canonical Enrichment and UAT Governance

This package adds governance metadata after parsing and before validation,
persistence, and chunking. It preserves extracted source values and makes no
remote network request during ingestion.

## Connections

`enrichment/field_policy.py` labels table cells as `standard`,
`reference_link`, or `internal_only`. Every policy includes `retrieval_allowed`
and `answer_visible`. Generic rules are deliberately conservative; each agent
can provide explicit column-policy overrides through `IngestionService`.

`references.py` registers URLs, file URIs, Windows paths, and UNC paths.
Remote and network references are unverified by default; only explicitly trusted
remote hosts or available local files are `answer_eligible`.

`visual_assets.py` records image metadata with `metadata_only` status until the
future vision/OCR phase. `service.py` coordinates all modules. `IngestionService`
invokes it before canonical quality validation, and the chunk builder copies
field policy plus direct source references into chunk metadata.

## UAT Mapping

| UAT need | Current enforcement point |
| --- | --- |
| Do not expose backend Excel fields | `field_policy.answer_visible` |
| Show only verified PPT/PDF/reference links | `reference.answer_eligible` |
| Explain unavailable or personal-folder links | Reference status and quality warning |
| Preserve image/PDF/PPT source association | Visual asset registry and provenance |
| Label AI versus source facts | Future answer layer; canonical provenance supplies evidence |

The registry proves only format and locally accessible paths. A deployment-level
reference-health job can later validate approved intranet or SharePoint URLs
using authorized credentials; it must not run inside the ingestion parser.

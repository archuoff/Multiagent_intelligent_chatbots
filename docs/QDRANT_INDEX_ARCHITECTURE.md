# Qdrant Index Architecture

## Purpose

Qdrant is the persistent vector retrieval index for JLR development. It is not
the canonical source of documents or a replacement for structured SQL data.

```text
Master JSON -> chunks.json -> Azure embedding vectors -> Qdrant collection
                                              |
                                              v
                                   future hybrid retrieval pipeline
```

## Storage and Collections

Qdrant Local Mode persists development data under `storage/qdrant/`. Each of
the six agents receives an isolated collection:

```text
jlr-adas-agent-chunks
jlr-benchmarking-agent-chunks
jlr-eds-agent-chunks
jlr-le-agent-chunks
jlr-material-net-agent-chunks
jlr-supplier-agent-chunks
```

Each point stores a dense vector plus payload: chunk ID, content text, agent,
document/version, parent node, source/chunk type, embedding model/dimensions,
security classification, allowed groups, and allowed users. Qdrant creates keyword indexes
for `agent_id`, `document_id`, `source_version`, `security_classification`, and
`allowed_groups`, and `allowed_users` before the first agent data is written.

## Guardrails

- Only an `EmbeddingBuildResult` matching the current chunk IDs may be indexed.
- One write contains a single agent and a single document version.
- Stable chunk IDs become deterministic Qdrant UUID point IDs; the original
  chunk ID remains in payload for traceability.
- Reingestion can delete one specific document version from its agent collection.
- Canonical JSON and `chunks.json` remain immutable audit artifacts.
- The future query layer must create an AuthZ-derived Qdrant filter before every
  search. Qdrant filtering supports the retrieval enforcement; it does not
  replace application-level authorization.

## Application Path

`EmbeddingIndexingService` loads a `StoredChunkArtifact` through
`ChunkArtifactStore`, verifying its SHA-256 checksum. Only then does it call
Azure embeddings and Qdrant upsert. The manual
`tests/ingestion/tools/embed_and_index.py` tool follows the same path.

# Local Ingestion Storage

This folder is the local-development equivalent of the future production object
store and metadata database. Generated files are intentionally ignored by Git.

## Folders

- `raw/`: immutable copies of original uploaded source files.
- `canonical/`: immutable, versioned Master JSON artifacts.
- `manifests/`: small ingestion records used to track source hashes, versions,
  parser status, ownership, and canonical artifact locations.
- `chunks/`: immutable, policy-versioned embedding-ready chunk artifacts.
- `chunk-manifests/`: checksum and lineage records for chunk artifacts.
- `embeddings/`: reserved for a future vector-index writer; this project does
  not persist dense vectors to local JSON by default.
- `qdrant/`: local Qdrant persistence files, created only after a vector index
  write and ignored by Git.

## Canonical artifact layout

```text
storage/
  canonical/
    <agent_id>/
      <document_id>/
        <version>/
          master.json
```

The future persistence service will create these paths. It will never overwrite
an existing version: an unchanged source hash is reused, and a changed hash
creates a new version.

## Chunk artifact layout

```text
storage/
  chunks/
    <agent_id>/
      <document_id>/
        <source_version>/
          <chunking_policy_version>/
            chunks.json
```

`chunks.json` contains both accepted embedding-ready chunks and rejected
records. Its matching manifest stores a SHA-256 checksum, source hash, policy
version, and an explicit `ready_for_embedding` or `needs_review` status. A
future vector-index service must consume only checksum-verified artifacts.

## Embedding boundary

The Azure embedding adapter reads a verified `chunks.json` artifact, blocks
artifacts marked `needs_review`, and returns vectors in memory for the future
vector database writer. This keeps large vector data out of the local file
store while preserving the immutable input artifact needed for reproducibility.

# Azure Embedding Boundary

## Purpose

This layer turns approved, canonical chunks into dense vectors. It does not
parse documents, change Master JSON, generate answers, or create a vector
database index.

## Flow

```text
canonical Master JSON
        |
        v
chunking/ -> chunks.json + checksum manifest
        |
        | only ready_for_embedding artifacts
        v
embedding/AzureOpenAIEmbeddingProvider
        |
        v
EmbeddingService: batch, retry, dimension validation
        |
        v
future vector-index writer
```

## Files

- `backend/ingestion/embedding/contracts.py`: non-secret model settings and
  provider-neutral embedding result records.
- `backend/ingestion/embedding/azure_openai.py`: Azure OpenAI SDK adapter. It
  loads runtime environment variables only during application execution and
  never logs API keys or source text.
- `backend/ingestion/embedding/service.py`: quality gate, batching, bounded
  retries, response-order restoration, numeric and vector-dimension checks.
- `tests/ingestion/tools/validate_embeddings.py`: intentional manual live
  validation utility. It prints counts and dimensions only.

## Runtime settings

The local `.env` supplies these values and must remain ignored by Git:

- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_API_VERSION`
- `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`
- `AZURE_OPENAI_EMBEDDING_DIMENSIONS`

`model` in the Azure SDK call is the Azure deployment name. For the configured
`text-embedding-ada-002` deployment, the service validates the returned vector
length against the configured `1536` dimensions. It does not send a dimensions
parameter because that control is for newer embedding model families.

## Safety rules

- Chunk artifacts with an error-level rejection never reach Azure.
- Empty embedding text is recorded as skipped and never sent.
- Every response must have one vector per input and the expected numeric shape.
- Retries reveal only the exception type, never source text or credentials.
- A later vector-index writer must preserve chunk ID, document/version lineage,
  parent node ID, and security scope as vector metadata.

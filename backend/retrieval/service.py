"""Retrieval orchestration: embed a query, search Qdrant, and enforce access control.

Deliberately stops here -- this module knows nothing about intent
classification, query decomposition/rewrite, or answer synthesis. Those are
built on top of this in a later round, and each will call `retrieve()` once
per (sub-)query rather than this module needing to know how many times it's
being called or why.

Process-lifetime note: `QdrantVectorStore` (and this service) must be
constructed once at application startup (a module-level singleton / a
cached FastAPI dependency), never per-request. Local-mode Qdrant
(`QdrantClient(path=...)`) is single-process -- re-opening the storage path
per request both hurts performance and risks lock conflicts with any
concurrent ingestion run on the same machine.
"""

from __future__ import annotations

import re
from typing import Any

from backend.ingestion.embedding.azure_openai import AzureOpenAIEmbeddingProvider
from backend.ingestion.embedding.sparse_provider import FastEmbedSparseProvider
from backend.ingestion.indexing.qdrant_store import QdrantVectorStore
from backend.ingestion.persistence.canonical_store import CanonicalArtifactStore
from backend.retrieval.contracts import RetrievalResult, RetrievedChunk
from backend.retrieval.query_embedding import embed_query, embed_query_sparse


class RetrievalService:
    """Turns one query string into a list of chunks this principal may see."""

    def __init__(self, vector_store: QdrantVectorStore, embedding_provider: AzureOpenAIEmbeddingProvider,
                 canonical_store: CanonicalArtifactStore, sparse_provider: FastEmbedSparseProvider | None = None) -> None:
        self._vector_store = vector_store
        self._embedding_provider = embedding_provider
        self._canonical_store = canonical_store
        self._sparse_provider = sparse_provider
        self._title_cache: dict[tuple[str, str, str], str | None] = {}

    def retrieve(self, *, agent_id: str, query_text: str, principal_group_codes: frozenset[str],
                 principal_user_id: str, limit: int = 8) -> RetrievalResult:
        """Embeds the query (dense + sparse), searches one agent's collection via hybrid
        search, and drops any chunk this principal can't see."""
        query_vector = embed_query(query_text, provider=self._embedding_provider)
        query_sparse_vector = embed_query_sparse(query_text, provider=self._sparse_provider)
        raw_results = self._vector_store.search(agent_id=agent_id, query_vector=query_vector,
            query_sparse_vector=query_sparse_vector, limit=limit)
        authorized_results = [
            item for item in raw_results
            if self._is_authorized(item["payload"], principal_group_codes, principal_user_id)
        ]
        retrieval_mode = self._retrieval_mode(query_text)
        if retrieval_mode == "exact":
            exact_results = [item for item in authorized_results if self._direct_keyword_match(query_text, item["payload"])]
            if exact_results:
                authorized_results = exact_results
        chunks = [self._to_retrieved_chunk(agent_id, item) for item in authorized_results]
        return RetrievalResult(query=query_text, chunks=chunks, retrieval_mode=retrieval_mode)


    def _retrieval_mode(self, query_text: str) -> str:
        """Classifies short/specific queries for strict lexical filtering after hybrid search."""
        normalized = query_text.strip()
        if not normalized:
            return "hybrid"
        if self._quoted_terms(normalized):
            return "exact"
        tokens = self._query_tokens(normalized)
        if any(character.isdigit() for character in normalized):
            return "exact"
        if len(tokens) <= 4 and not self._looks_like_broad_question(normalized):
            return "exact"
        return "hybrid"

    def _direct_keyword_match(self, query_text: str, payload: dict[str, Any]) -> bool:
        """Returns true only when the retrieved chunk itself contains the requested keyword terms."""
        haystack = "\n".join(str(payload.get(key) or "") for key in (
            "content_text", "table_title", "cell_range", "sheet_name", "source_type"
        ))
        haystack_tokens = set(self._query_tokens(haystack))
        quoted = self._quoted_terms(query_text)
        haystack_folded = haystack.casefold()
        if quoted:
            return any(term.casefold() in haystack_folded for term in quoted)
        query_tokens = self._query_tokens(query_text)
        required = [token for token in query_tokens if token not in _QUERY_STOPWORDS]
        if not required:
            return False
        if len(required) == 1:
            return required[0] in haystack_tokens
        return all(token in haystack_tokens for token in required)

    def _query_tokens(self, value: str) -> list[str]:
        """Tokenizes query/chunk text for exact-mode filtering."""
        return re.findall(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", value.casefold())

    def _quoted_terms(self, query_text: str) -> list[str]:
        """Extracts explicit quoted phrases that should be matched literally."""
        return [term.strip() for term in re.findall(r'"([^"]+)"', query_text) if term.strip()]


    def _looks_like_broad_question(self, query_text: str) -> bool:
        """Keeps explanatory questions in hybrid mode even when they are short."""
        lowered = query_text.casefold().strip()
        broad_prefixes = ("what ", "why ", "how ", "explain", "compare", "summarize", "list ", "show ")
        return lowered.endswith("?") or lowered.startswith(broad_prefixes)

    def _is_authorized(self, payload: dict[str, Any], principal_group_codes: frozenset[str],
                        principal_user_id: str) -> bool:
        """A chunk with no ACL entries at all is public; otherwise the principal must match one."""
        allowed_groups = set(payload.get("allowed_groups") or [])
        allowed_users = set(payload.get("allowed_users") or [])
        if not allowed_groups and not allowed_users:
            return True
        return bool(allowed_groups & principal_group_codes) or principal_user_id in allowed_users

    def _to_retrieved_chunk(self, agent_id: str, item: dict[str, Any]) -> RetrievedChunk:
        """Maps one raw Qdrant search hit onto the typed, citation-ready contract."""
        payload = item["payload"]
        return RetrievedChunk(
            chunk_id=payload.get("chunk_id", ""),
            score=item["score"],
            chunk_type=payload.get("chunk_type", ""),
            content_text=payload.get("content_text", ""),
            document_id=payload.get("document_id", ""),
            document_title=self._resolve_document_title(agent_id, payload),
            page_number=payload.get("page_number"),
            slide_number=payload.get("slide_number"),
            sheet_name=payload.get("sheet_name"),
            cell_range=payload.get("cell_range"),
            breadcrumbs=payload.get("breadcrumbs") or [],
            table_title=payload.get("table_title"),
            header_context=payload.get("header_context") or [],
            field_policies=payload.get("field_policies") or [],
            security_classification=payload.get("security_classification", "internal"),
            source_type=payload.get("source_type", ""),
            source_version=payload.get("source_version", ""),
            visual_node_type=payload.get("visual_node_type"),
            visual_object_type=payload.get("visual_object_type"),
            ocr_status=payload.get("ocr_status"),
            image_description_status=payload.get("image_description_status"),
            vlm_confidence=_numeric_confidence(payload.get("vlm_confidence")),
            image_path=payload.get("image_path"),
            image_storage_status=payload.get("image_storage_status"),
        )

    def _resolve_document_title(self, agent_id: str, payload: dict[str, Any]) -> str | None:
        """Looks up the source filename for a citation, cached per document version this request."""
        document_id = payload.get("document_id")
        source_version = payload.get("source_version")
        if not document_id or not source_version:
            return None
        cache_key = (agent_id, document_id, source_version)
        if cache_key in self._title_cache:
            return self._title_cache[cache_key]
        try:
            artifact = self._canonical_store.load(agent_id=agent_id, document_id=document_id, version=source_version)
            title = artifact.manifest.source_name
        except (FileNotFoundError, ValueError):
            # A missing or corrupted manifest is a citation nicety lost, not a
            # reason to fail an otherwise-valid search result.
            title = None
        self._title_cache[cache_key] = title
        return title


def _numeric_confidence(value: Any) -> float | None:
    """Converts confidence payloads to numbers without failing retrieval on labels."""
    if value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if not normalized:
            return None
        label_scores = {"low": 0.33, "medium": 0.66, "high": 1.0}
        if normalized in label_scores:
            return label_scores[normalized]
        try:
            return float(normalized)
        except ValueError:
            return None
    return None


_QUERY_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "it", "of",
    "on", "or", "the", "to", "with", "about", "give", "me", "show", "find", "tell", "please",
}

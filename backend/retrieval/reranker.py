"""Cross-encoder reranking: a second, more precise scoring pass over hybrid
search results.

Uses a local sentence-transformers CrossEncoder (cross-encoder/ms-marco-MiniLM-L-6-v2,
already cached locally -- no download needed) instead of a hosted API, per
user direction. Reads the query and each candidate chunk together, which is
more accurate than comparing pre-computed vectors, at the cost of some
extra latency per candidate.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Any

from backend.retrieval.contracts import RetrievedChunk

_DEFAULT_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"
logger = logging.getLogger(__name__)


class CrossEncoderReranker:
    """Re-scores and re-sorts retrieved chunks against the actual query text."""

    def __init__(self, model: Any) -> None:
        """Stores a prebuilt model so application code can inject or test it safely."""
        self._model = model

    @classmethod
    def from_default_model(cls) -> "CrossEncoderReranker":
        """Loads the local cross-encoder model."""
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as error:
            raise RuntimeError("Reranking requires the 'sentence-transformers' package. Install requirements.txt first.") from error
        return cls(model=CrossEncoder(_DEFAULT_MODEL))

    def rerank(self, query: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """Scores each (query, chunk) pair and returns chunks sorted by that score, descending."""
        if not chunks:
            return []
        scores = self._model.predict([(query, chunk.content_text) for chunk in chunks])
        rescored = [chunk.model_copy(update={"rerank_score": float(score)}) for chunk, score in zip(chunks, scores)]
        rescored.sort(key=lambda chunk: chunk.rerank_score, reverse=True)
        return rescored


class ScoreOnlyReranker:
    """Safe fallback that preserves vector-store ranking when the cross-encoder is unavailable."""

    def rerank(self, query: str, chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
        """Returns chunks sorted by the existing retrieval score without loading a model."""
        _ = query
        return sorted(chunks, key=lambda chunk: chunk.score, reverse=True)


@lru_cache(maxsize=1)
def get_reranker() -> CrossEncoderReranker | ScoreOnlyReranker:
    """Builds one reranker per process, falling back to retrieval-score ordering when unavailable."""
    try:
        return CrossEncoderReranker.from_default_model()
    except RuntimeError as error:
        logger.warning("cross_encoder_reranker_unavailable", extra={"error": str(error)})
        return ScoreOnlyReranker()

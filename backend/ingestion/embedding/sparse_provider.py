"""BM25 sparse-vector adapter for Qdrant's native hybrid search.

Mirrors azure_openai.py's injectable-client pattern. Uses fastembed's
document-independent BM25 encoder (Qdrant's own recommended model for this)
rather than a classic corpus-refit BM25 implementation, since this codebase
ingests one document at a time -- a per-document-independent encoder needs
no whole-corpus refit on every new document.
"""

from __future__ import annotations

from typing import Any


class FastEmbedSparseProvider:
    """Computes BM25-style sparse vectors through the fastembed SDK."""

    def __init__(self, model: Any) -> None:
        """Stores a prebuilt model so application code can inject or test it safely."""
        self._model = model

    @classmethod
    def from_default_model(cls) -> "FastEmbedSparseProvider":
        """Builds the provider using Qdrant's recommended BM25 sparse model."""
        try:
            from fastembed import SparseTextEmbedding
        except ImportError as error:
            raise RuntimeError("Sparse embedding requires the 'fastembed' package. Install requirements.txt first.") from error
        return cls(model=SparseTextEmbedding(model_name="Qdrant/bm25"))

    def embed(self, texts: list[str]) -> list[dict[str, list]]:
        """Embeds an ordered batch into {"indices": [...], "values": [...]} sparse vectors.

        fastembed returns numpy arrays; converts to plain Python int/float so
        callers (Qdrant's SparseVector, JSON serialization) never see numpy types.
        """
        if not texts:
            return []
        return [{"indices": [int(index) for index in vector.indices], "values": [float(value) for value in vector.values]}
                for vector in self._model.embed(texts)]

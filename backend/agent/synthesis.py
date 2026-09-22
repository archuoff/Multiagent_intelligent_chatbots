"""Answer synthesis: the final LLM call that turns reranked chunks into a
written, source-grounded answer.

Reuses the same Azure chat client as classify_intent/decompose_query -- one
consistent provider throughout, per earlier decision. Applies redaction
before any chunk text reaches the prompt, so an answer_visible=False field
can never leak into a generated answer.
"""

from __future__ import annotations

import logging

from backend.agent.azure_chat import AzureOpenAIChatClient, get_chat_client
from backend.retrieval.contracts import RetrievedChunk
from backend.retrieval.redaction import redact_for_synthesis

logger = logging.getLogger(__name__)

_MAX_CONTEXT_CHUNKS = 6

SYNTHESIS_SYSTEM_PROMPT = """You are answering an engineer's question using only
the numbered context passages provided below -- not prior knowledge.

Rules:
- Answer only from the context. If the context does not contain enough
  information to answer, say so plainly instead of guessing.
- Cite the passage(s) you used with their number, e.g. "[1]", inline in
  your answer.
- If you must add a brief clarifying note not directly stated in the
  context (for example a unit conversion), clearly label it as inferred,
  not quoted from a source.
- Be concise and technical -- this is for an engineer, not a general
  audience."""

_NO_CONTEXT_ANSWER = "I couldn't find anything relevant to answer that in the knowledge base."
_LLM_FAILURE_ANSWER = "I found relevant information but couldn't generate an answer right now. Please try again."


def synthesize_answer(query: str, chunks: list[RetrievedChunk],
                       chat_client: AzureOpenAIChatClient | None = None) -> dict:
    """Builds a source-grounded answer from the top reranked chunks, or a safe fallback message."""
    top_chunks = chunks[:_MAX_CONTEXT_CHUNKS]
    if not top_chunks:
        return {"answer": _NO_CONTEXT_ANSWER, "sources": [], "model_used": None}

    context_blocks = []
    sources = []
    for index, chunk in enumerate(top_chunks, start=1):
        text = redact_for_synthesis(chunk)
        context_blocks.append(f"[{index}] {text}\n(Source: {chunk.document_title or chunk.document_id}{_citation_location(chunk)})")
        sources.append({
            "index": index, "chunk_id": chunk.chunk_id, "document_title": chunk.document_title,
            "page_number": chunk.page_number, "sheet_name": chunk.sheet_name, "slide_number": chunk.slide_number,
            "score": chunk.rerank_score if chunk.rerank_score is not None else chunk.score,
        })

    client = chat_client or get_chat_client()
    messages = [{"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
        {"role": "user", "content": f"Context:\n\n{chr(10).join(context_blocks)}\n\nQuestion: {query}"}]
    try:
        answer = client.complete(messages, temperature=0.1)
    except Exception as error:
        logger.error(f"[synthesize_answer] LLM call failed: {error}")
        return {"answer": _LLM_FAILURE_ANSWER, "sources": sources, "model_used": None}
    return {"answer": answer, "sources": sources, "model_used": client.deployment}


def _citation_location(chunk: RetrievedChunk) -> str:
    """Builds a short page/sheet/slide suffix for a citation, whichever coordinate is present."""
    if chunk.page_number is not None:
        return f", page {chunk.page_number}"
    if chunk.sheet_name:
        return f", sheet {chunk.sheet_name}"
    if chunk.slide_number is not None:
        return f", slide {chunk.slide_number}"
    return ""

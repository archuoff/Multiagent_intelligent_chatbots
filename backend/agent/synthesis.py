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
- If a context passage includes a visual asset path and the user asks for an
  image, figure, chart, diagram, or formula screenshot, output that exact
  path on its own line, unmodified -- no markdown link syntax, no
  rewording, no truncation. Copy it character-for-character from the
  context's "Visual asset:" line.
- Be concise and technical -- this is for an engineer, not a general
  audience.

Formatting:
- Use markdown to structure the answer, not a single flat paragraph.
- If the answer has multiple distinct items or steps, use a numbered or
  bulleted list -- one item per line, not comma-joined prose.
- If the context contains tabular/comparison data (specs, dimensions,
  multiple named values side by side), render it as a markdown table.
- Use short headers (##/###) only when the answer naturally has more than
  one section; skip headers for a short, single-topic answer.
- Keep citation markers like "[1]" attached to the specific list item or
  table row they support, not bunched at the end of the whole answer."""

_NO_CONTEXT_ANSWER = "I couldn't find anything relevant to answer that in the knowledge base."
_LLM_FAILURE_ANSWER = "I found relevant information but couldn't generate an answer right now. Please try again."


def build_synthesis_messages(query: str, chunks: list[RetrievedChunk]) -> tuple[list[dict[str, str]], list[dict]]:
    """Builds the synthesis prompt messages and parallel source-citation list.

    Shared by synchronous synthesize_answer() and the streaming
    stream_answer_tokens() generator so both paths build identical context
    from identical chunks -- the only difference is how the LLM response is
    consumed (one string vs. a token stream).
    """
    top_chunks = chunks[:_MAX_CONTEXT_CHUNKS]
    context_blocks = []
    sources = []
    for index, chunk in enumerate(top_chunks, start=1):
        text = redact_for_synthesis(chunk)
        visual_suffix = _visual_context(chunk)
        context_blocks.append(
            f"[{index}] {text}{visual_suffix}\n"
            f"(Source: {chunk.document_title or chunk.document_id}{_citation_location(chunk)})"
        )
        sources.append({
            "index": index, "chunk_id": chunk.chunk_id, "document_title": chunk.document_title,
            "page_number": chunk.page_number, "sheet_name": chunk.sheet_name, "slide_number": chunk.slide_number,
            "image_path": chunk.image_path, "visual_object_type": chunk.visual_object_type,
            "score": chunk.rerank_score if chunk.rerank_score is not None else chunk.score,
        })
    messages = [{"role": "system", "content": SYNTHESIS_SYSTEM_PROMPT},
        {"role": "user", "content": f"Context:\n\n{chr(10).join(context_blocks)}\n\nQuestion: {query}"}]
    return messages, sources


def synthesize_answer(query: str, chunks: list[RetrievedChunk],
                       chat_client: AzureOpenAIChatClient | None = None) -> dict:
    """Builds a source-grounded answer from the top reranked chunks, or a safe fallback message."""
    if not chunks:
        return {"answer": _NO_CONTEXT_ANSWER, "sources": [], "model_used": None}

    messages, sources = build_synthesis_messages(query, chunks)
    client = chat_client or get_chat_client()
    try:
        answer = client.complete(messages, temperature=0.1)
    except Exception as error:
        logger.error(f"[synthesize_answer] LLM call failed: {error}")
        return {"answer": _LLM_FAILURE_ANSWER, "sources": sources, "model_used": None}
    return {"answer": answer, "sources": sources, "model_used": client.deployment}


def stream_answer_tokens(query: str, chunks: list[RetrievedChunk], chat_client: AzureOpenAIChatClient | None = None):
    """Yields the answer incrementally as text deltas, for the streaming /chat/stream endpoint.

    Mirrors synthesize_answer()'s prompt construction exactly (via
    build_synthesis_messages) so a streamed answer is built from the same
    context a non-streamed call would use. On an LLM failure mid-stream,
    yields the same fallback message synthesize_answer() would have
    returned, so the client still ends up with a sensible final answer.
    """
    if not chunks:
        yield _NO_CONTEXT_ANSWER
        return

    messages, _sources = build_synthesis_messages(query, chunks)
    client = chat_client or get_chat_client()
    try:
        yield from client.stream_complete(messages, temperature=0.1)
    except Exception as error:
        logger.error(f"[stream_answer_tokens] LLM call failed: {error}")
        yield _LLM_FAILURE_ANSWER


def _visual_context(chunk: RetrievedChunk) -> str:
    """Adds visual asset evidence to the LLM context when retrieval found an image-backed chunk."""
    if not chunk.image_path:
        return ""
    labels = []
    if chunk.visual_object_type:
        labels.append(f"type={chunk.visual_object_type}")
    if chunk.visual_node_type:
        labels.append(f"node={chunk.visual_node_type}")
    label_text = f" ({', '.join(labels)})" if labels else ""
    return f"\nVisual asset{label_text}: {chunk.image_path}"


def _citation_location(chunk: RetrievedChunk) -> str:
    """Builds a short page/sheet/slide suffix for a citation, whichever coordinate is present."""
    if chunk.page_number is not None:
        return f", page {chunk.page_number}"
    if chunk.sheet_name:
        return f", sheet {chunk.sheet_name}"
    if chunk.slide_number is not None:
        return f", slide {chunk.slide_number}"
    return ""

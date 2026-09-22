"""FastAPI routes for agent discovery and query-time chat.

This module is intentionally thin: request validation and HTTP response
formatting live here, while classification, retrieval, reranking, and answer
generation stay inside the agent graph/retrieval modules.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from backend.agent.config import get_agent_config, load_all_configs
from backend.agent.graph import get_compiled_graph
from backend.api.dependencies import CurrentUser, get_current_user
from backend.api.schemas import AgentSummary, ChatRequest, ChatResponse, Citation, RetrievedContext

logger = logging.getLogger(__name__)

router = APIRouter(tags=["agents"])


@router.get("/agents", response_model=list[AgentSummary])
def list_agents(user: CurrentUser = Depends(get_current_user)) -> list[AgentSummary]:
    """Returns the available agent cards the UI can render.

    The current dev dependency does not yet filter by user entitlement. Once
    PostgreSQL group auth is fully enabled, this route should filter configs
    using the same group membership source used by chat retrieval ACLs.
    """
    _ = user
    try:
        configs = load_all_configs()
    except RuntimeError as error:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    return [
        AgentSummary(
            agent_id=config.agent_id,
            display_name=config.display_name,
            route=config.route,
            domain_description=config.domain_description,
        )
        for config in sorted(configs.values(), key=lambda item: item.display_name.lower())
    ]


@router.post("/chat", response_model=ChatResponse, tags=["chat"])
def chat(request: ChatRequest, user: CurrentUser = Depends(get_current_user)) -> ChatResponse:
    """Runs one user message through the query graph and returns answer + citations."""
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Message cannot be empty.")

    try:
        get_agent_config(request.agent_id)
    except KeyError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    initial_state = {
        "agent_id": request.agent_id,
        "user_query": message,
        "conversation_history": _safe_history(request.conversation_history),
        "principal_group_codes": list(user.groups),
        "principal_user_id": user.user_id,
    }

    try:
        result = get_compiled_graph().invoke(initial_state)
    except RuntimeError as error:
        logger.exception("query_pipeline_runtime_error")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except Exception as error:
        logger.exception("query_pipeline_unhandled_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Query pipeline failed. Please check backend logs.",
        ) from error

    return _chat_response_from_state(request, result)


def _safe_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """Keeps only role/content pairs accepted by the LLM classifier."""
    allowed_roles = {"user", "assistant", "system"}
    safe_messages: list[dict[str, str]] = []
    for item in history[-10:]:
        role = str(item.get("role") or "").strip()
        content = str(item.get("content") or "").strip()
        if role in allowed_roles and content:
            safe_messages.append({"role": role, "content": content})
    return safe_messages


def _chat_response_from_state(request: ChatRequest, state: dict[str, Any]) -> ChatResponse:
    """Maps graph state onto the public API response schema."""
    if state.get("needs_clarification"):
        return ChatResponse(
            agent_id=request.agent_id,
            session_id=request.session_id,
            answer=state.get("ask_user") or "Could you clarify your question?",
            status="needs_clarification",
        )

    chunks = state.get("reranked_chunks") or state.get("retrieved_chunks") or []
    contexts = [_context_from_chunk(chunk) for chunk in chunks]
    return ChatResponse(
        agent_id=request.agent_id,
        session_id=request.session_id,
        answer=state.get("answer") or "I couldn't generate an answer for that request.",
        citations=_unique_citations(context.citation for context in contexts),
        retrieved_context=contexts,
        status="success",
    )


def _context_from_chunk(chunk: dict[str, Any]) -> RetrievedContext:
    """Converts an internal retrieved chunk dict into the API context shape."""
    citation = Citation(
        document_id=chunk.get("document_id"),
        source_type=chunk.get("source_type"),
        page_number=chunk.get("page_number"),
        slide_number=chunk.get("slide_number"),
        sheet_name=chunk.get("sheet_name"),
        cell_range=chunk.get("cell_range"),
        source_version=chunk.get("source_version"),
    )
    score = chunk.get("rerank_score") if chunk.get("rerank_score") is not None else chunk.get("score")
    return RetrievedContext(
        chunk_id=str(chunk.get("chunk_id") or ""),
        score=score,
        content_text=str(chunk.get("content_text") or ""),
        citation=citation,
        document_title=chunk.get("document_title"),
        chunk_type=chunk.get("chunk_type"),
        breadcrumbs=chunk.get("breadcrumbs") or [],
        image_path=chunk.get("image_path"),
    )


def _unique_citations(citations: Any) -> list[Citation]:
    """Deduplicates citations while preserving the retrieval order."""
    seen: set[tuple[Any, ...]] = set()
    unique: list[Citation] = []
    for citation in citations:
        key = (
            citation.document_id,
            citation.source_type,
            citation.page_number,
            citation.slide_number,
            citation.sheet_name,
            citation.cell_range,
            citation.source_version,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(citation)
    return unique

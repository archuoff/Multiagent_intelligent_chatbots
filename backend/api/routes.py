"""FastAPI routes for agent discovery and query-time chat.

This module is intentionally thin: request validation and HTTP response
formatting live here, while classification, retrieval, reranking, and answer
generation stay inside the agent graph/retrieval modules.
"""

from __future__ import annotations

import json
import logging
from uuid import uuid4
from typing import Any, Generator

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse

from backend.agent.config import get_agent_config, load_all_configs
from backend.agent.decomposition import decompose_query
from backend.agent.graph import get_compiled_graph, make_retrieve_node, make_rerank_node, _default_retrieval_service
from backend.agent.nodes import classify_intent
from backend.agent.synthesis import stream_answer_tokens
from backend.api.dependencies import CurrentUser, get_current_user
from backend.api.schemas import AgentSummary, ChatRequest, ChatResponse, Citation, RetrievedContext
from backend.retrieval.contracts import RetrievedChunk
from backend.retrieval.reranker import get_reranker

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

    session_id = request.session_id or uuid4().hex
    initial_state = {
        "agent_id": request.agent_id,
        "user_query": message,
        "conversation_history": [] if request.session_id else _safe_history(request.conversation_history),
        "principal_group_codes": list(user.groups),
        "principal_user_id": user.user_id,
        "sub_queries": [],
        "retrieved_chunks": [],
        "reranked_chunks": [],
        "image_chunks": [],
        "merged_context": "",
        "answer": "",
        "sources": [],
    }
    graph_config = {"configurable": {"thread_id": session_id}}

    try:
        result = get_compiled_graph().invoke(initial_state, config=graph_config)
    except RuntimeError as error:
        logger.exception("query_pipeline_runtime_error")
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(error)) from error
    except Exception as error:
        logger.exception("query_pipeline_unhandled_error")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Query pipeline failed. Please check backend logs.",
        ) from error

    return _chat_response_from_state(request, result, session_id)


@router.post("/chat/stream", tags=["chat"])
def chat_stream(request: ChatRequest, user: CurrentUser = Depends(get_current_user)) -> StreamingResponse:
    """Streams one answer as Server-Sent Events instead of waiting for the full response.

    Kept entirely separate from POST /chat (which keeps working unchanged) --
    this orchestrates the same classify -> (decompose) -> retrieve -> rerank
    -> synthesize steps by calling the same node functions graph.py's
    StateGraph wires together, but drives them directly in sequence instead
    of through a compiled-graph invoke(), since only the final synthesis
    step actually benefits from token-level streaming. Conversation history
    is read from and written back to the same LangGraph checkpointer/thread
    POST /chat uses (get_state/update_state), so history stays consistent
    whichever endpoint a given turn used.
    """
    message = request.message.strip()
    if not message:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Message cannot be empty.")
    try:
        get_agent_config(request.agent_id)
    except KeyError as error:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(error)) from error

    session_id = request.session_id or uuid4().hex
    return StreamingResponse(
        _chat_stream_events(request, user, message, session_id), media_type="text/event-stream")


def _sse(event: str, data: dict[str, Any]) -> str:
    """Formats one Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _chat_stream_events(request: ChatRequest, user: CurrentUser, message: str, session_id: str) -> Generator[str, None, None]:
    """Generator driving the SSE response body for POST /chat/stream."""
    graph = get_compiled_graph()
    graph_config = {"configurable": {"thread_id": session_id}}
    try:
        history = _resolve_history(graph, graph_config, request)
        state: dict[str, Any] = {
            "agent_id": request.agent_id,
            "user_query": message,
            "conversation_history": history,
            "principal_group_codes": list(user.groups),
            "principal_user_id": user.user_id,
        }
        yield _sse("status", {"stage": "understanding", "label": "Thinking"})
        state.update(classify_intent(state))

        if state["intent"] != "Domain_qn":
            if state.get("needs_clarification"):
                answer_text = state.get("ask_user") or "Could you clarify your question?"
                response_status = "needs_clarification"
            else:
                answer_text = state.get("answer") or "I couldn't generate an answer for that request."
                response_status = "success"
            yield _sse("sources", {"retrieved_context": []})
            yield _sse("delta", {"text": answer_text})
            yield _sse("done", {"answer": answer_text, "session_id": session_id, "status": response_status})
            _persist_turn(graph, graph_config, message, answer_text)
            return

        state["sub_queries"] = decompose_query(state.get("resolved_query") or message) if state.get("requires_decomposition") else []

        yield _sse("status", {"stage": "searching", "label": "Grepping"})
        state.update(make_retrieve_node(_default_retrieval_service())(state))

        yield _sse("status", {"stage": "ranking", "label": "Verifying"})
        state.update(make_rerank_node(get_reranker())(state))

        reranked = state.get("reranked_chunks") or []
        contexts = [_context_from_chunk(item) for item in reranked]
        yield _sse("sources", {"retrieved_context": [context.model_dump() for context in contexts]})

        yield _sse("status", {"stage": "writing", "label": "Answering"})
        query_text = state.get("resolved_query") or message
        chunks = [RetrievedChunk(**item) for item in reranked]
        full_answer = ""
        for token in stream_answer_tokens(query_text, chunks):
            full_answer += token
            yield _sse("delta", {"text": token})

        if not full_answer:
            full_answer = "I couldn't generate an answer for that request."
        yield _sse("done", {"answer": full_answer, "session_id": session_id, "status": "success"})
        _persist_turn(graph, graph_config, message, full_answer)
    except Exception:
        logger.exception("chat_stream_error")
        yield _sse("error", {"detail": "Query pipeline failed. Please check backend logs."})


def _resolve_history(graph: Any, graph_config: dict[str, Any], request: ChatRequest) -> list[dict[str, str]]:
    """Loads prior turns from the checkpointer for an existing session, matching
    POST /chat's own rule (ignore client-sent history once a session_id exists)."""
    if not request.session_id:
        return _safe_history(request.conversation_history)
    snapshot = graph.get_state(graph_config)
    if snapshot and snapshot.values:
        return snapshot.values.get("conversation_history") or []
    return []


def _persist_turn(graph: Any, graph_config: dict[str, Any], user_query: str, assistant_text: str) -> None:
    """Appends the completed turn to the checkpointer, mirroring graph.py's _record_turn_node
    so history stays identical regardless of which endpoint produced a given turn."""
    messages: list[dict[str, str]] = []
    if user_query.strip():
        messages.append({"role": "user", "content": user_query.strip()})
    if assistant_text.strip():
        messages.append({"role": "assistant", "content": assistant_text.strip()})
    if messages:
        graph.update_state(graph_config, {"conversation_history": messages})


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


def _chat_response_from_state(request: ChatRequest, state: dict[str, Any], session_id: str | None = None) -> ChatResponse:
    """Maps graph state onto the public API response schema."""
    if state.get("needs_clarification"):
        return ChatResponse(
            agent_id=request.agent_id,
            session_id=session_id or request.session_id,
            answer=state.get("ask_user") or "Could you clarify your question?",
            status="needs_clarification",
        )

    chunks = state.get("reranked_chunks") or state.get("retrieved_chunks") or []
    contexts = [_context_from_chunk(chunk) for chunk in chunks]
    return ChatResponse(
        agent_id=request.agent_id,
        session_id=session_id or request.session_id,
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
        visual_node_type=chunk.get("visual_node_type"),
        visual_object_type=chunk.get("visual_object_type"),
        ocr_status=chunk.get("ocr_status"),
        image_description_status=chunk.get("image_description_status"),
        vlm_confidence=chunk.get("vlm_confidence"),
        image_storage_status=chunk.get("image_storage_status"),
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

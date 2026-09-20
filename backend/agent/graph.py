"""LangGraph wiring for the query pipeline: classify intent, then route.

Only a domain question reaches retrieval; every other intent (general_chat,
out_of_scope, clarification_qn) short-circuits straight to END with the
canned/clarification response classify_intent already produced. A domain
question that classify_intent flagged as requires_decomposition first
passes through decompose_query (a second, separate LLM call -- most domain
questions skip this and go straight to retrieval on the single
resolved_query).

Deliberately stops at raw retrieved chunks -- query rewrite (per-sub-query
search-term/filter refinement) and answer synthesis (turning chunks into a
written answer) are both separate, later rounds. This graph proves the
classify -> (decompose) -> retrieve wiring works end-to-end, not a finished
chat response.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from backend.agent.decomposition import decompose_query
from backend.agent.nodes import classify_intent
from backend.agent.state import AgentState
from backend.ingestion.embedding.azure_openai import AzureOpenAIEmbeddingProvider
from backend.ingestion.indexing.qdrant_store import QdrantVectorStore
from backend.ingestion.persistence.canonical_store import CanonicalArtifactStore
from backend.retrieval.service import RetrievalService

GraphNode = Callable[[AgentState], dict[str, Any]]


def _default_decompose_node(state: AgentState) -> dict[str, Any]:
    """Splits the resolved query into sub-queries; only reached when requires_decomposition is true."""
    query_text = state.get("resolved_query") or state["user_query"]
    return {"sub_queries": decompose_query(query_text)}


def make_retrieve_node(service: RetrievalService) -> GraphNode:
    """Builds the retrieve node around one RetrievalService, injectable for tests.

    Runs once per entry in sub_queries when decomposition happened, or once
    on the single resolved_query otherwise -- the same node handles both
    shapes uniformly. Results are merged by chunk_id (a chunk matching more
    than one sub-query is kept once) and re-sorted by score.
    """

    def retrieve_node(state: AgentState) -> dict[str, Any]:
        queries = state.get("sub_queries") or [state.get("resolved_query") or state["user_query"]]
        principal_group_codes = frozenset(state.get("principal_group_codes") or [])
        principal_user_id = state.get("principal_user_id", "")
        agent_id = state["agent_id"]
        seen_chunk_ids: set[str] = set()
        combined_chunks: list[dict[str, Any]] = []
        for query_text in queries:
            result = service.retrieve(agent_id=agent_id, query_text=query_text,
                principal_group_codes=principal_group_codes, principal_user_id=principal_user_id)
            for chunk in result.chunks:
                if chunk.chunk_id in seen_chunk_ids:
                    continue
                seen_chunk_ids.add(chunk.chunk_id)
                combined_chunks.append(chunk.model_dump())
        combined_chunks.sort(key=lambda item: item["score"], reverse=True)
        return {"retrieved_chunks": combined_chunks}

    return retrieve_node


@lru_cache(maxsize=1)
def _default_retrieval_service() -> RetrievalService:
    """Builds the one process-lifetime RetrievalService instance production code uses.

    QdrantVectorStore is local-mode (single-process) -- constructing it once
    per process, not per request, matches the constraint documented on
    RetrievalService itself.
    """
    return RetrievalService(QdrantVectorStore(), AzureOpenAIEmbeddingProvider.from_runtime_environment(),
        CanonicalArtifactStore())


def _route_after_intent(state: AgentState) -> str:
    """Domain questions needing a split go through decompose first; other domain
    questions go straight to retrieve; every non-domain intent stops here."""
    if state["intent"] != "Domain_qn":
        return END
    return "decompose" if state.get("requires_decomposition") else "retrieve"


def build_agent_graph(*, classify_node: GraphNode | None = None, decompose_node: GraphNode | None = None,
                       retrieve_node: GraphNode | None = None) -> StateGraph:
    """Assembles the classify -> (decompose) -> retrieve graph. Every node is injectable for tests."""
    classify = classify_node or classify_intent
    decompose = decompose_node or _default_decompose_node
    retrieve = retrieve_node or make_retrieve_node(_default_retrieval_service())
    graph = StateGraph(AgentState)
    graph.add_node("classify_intent", classify)
    graph.add_node("decompose", decompose)
    graph.add_node("retrieve", retrieve)
    graph.add_edge(START, "classify_intent")
    graph.add_conditional_edges("classify_intent", _route_after_intent,
        {"decompose": "decompose", "retrieve": "retrieve", END: END})
    graph.add_edge("decompose", "retrieve")
    graph.add_edge("retrieve", END)
    return graph


@lru_cache(maxsize=1)
def get_compiled_graph() -> CompiledStateGraph:
    """Compiles the production graph once per process. No checkpointer configured yet --
    conversation-history persistence across turns is separate, later work."""
    return build_agent_graph().compile()

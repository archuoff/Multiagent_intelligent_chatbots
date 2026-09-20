"""LangGraph wiring for the query pipeline: classify intent, then route.

Only a domain question reaches retrieval; every other intent (general_chat,
out_of_scope, clarification_qn) short-circuits straight to END with the
canned/clarification response classify_intent already produced.

Deliberately stops at raw retrieved chunks for a domain question -- query
decomposition/rewrite (which decide what to search for, before search
happens) and answer synthesis (which turns chunks into a written answer,
after all retrieval-improving stages) are both separate, later rounds. This
graph proves the classify -> route -> retrieve wiring works end-to-end, not
a finished chat response.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Callable

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from backend.agent.nodes import classify_intent
from backend.agent.state import AgentState
from backend.ingestion.embedding.azure_openai import AzureOpenAIEmbeddingProvider
from backend.ingestion.indexing.qdrant_store import QdrantVectorStore
from backend.ingestion.persistence.canonical_store import CanonicalArtifactStore
from backend.retrieval.service import RetrievalService

RetrieveNode = Callable[[AgentState], dict[str, Any]]


def make_retrieve_node(service: RetrievalService) -> RetrieveNode:
    """Builds the retrieve node around one RetrievalService, injectable for tests."""

    def retrieve_node(state: AgentState) -> dict[str, Any]:
        """Calls RetrievalService for a Domain_qn intent; only reached via the router below."""
        query_text = state.get("resolved_query") or state["user_query"]
        result = service.retrieve(
            agent_id=state["agent_id"],
            query_text=query_text,
            principal_group_codes=frozenset(state.get("principal_group_codes") or []),
            principal_user_id=state.get("principal_user_id", ""),
        )
        return {"retrieved_chunks": [chunk.model_dump() for chunk in result.chunks]}

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
    """Sends only a Domain_qn question to retrieval; every other intent stops here."""
    return "retrieve" if state["intent"] == "Domain_qn" else END


def build_agent_graph(*, classify_node: RetrieveNode | None = None, retrieve_node: RetrieveNode | None = None) -> StateGraph:
    """Assembles the classify -> (route) -> retrieve graph. Both nodes are injectable for tests."""
    retrieve = retrieve_node or make_retrieve_node(_default_retrieval_service())
    classify = classify_node or classify_intent
    graph = StateGraph(AgentState)
    graph.add_node("classify_intent", classify)
    graph.add_node("retrieve", retrieve)
    graph.add_edge(START, "classify_intent")
    graph.add_conditional_edges("classify_intent", _route_after_intent, {"retrieve": "retrieve", END: END})
    graph.add_edge("retrieve", END)
    return graph


@lru_cache(maxsize=1)
def get_compiled_graph() -> CompiledStateGraph:
    """Compiles the production graph once per process. No checkpointer configured yet --
    conversation-history persistence across turns is separate, later work."""
    return build_agent_graph().compile()

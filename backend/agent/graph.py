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

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
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
from backend.retrieval.query_rewrite import rewrite_query
from backend.retrieval.service import RetrievalService

GraphNode = Callable[[AgentState], dict[str, Any]]

logger = logging.getLogger(__name__)


def _default_decompose_node(state: AgentState) -> dict[str, Any]:
    """Splits the resolved query into sub-queries; only reached when requires_decomposition is true."""
    query_text = state.get("resolved_query") or state["user_query"]
    return {"sub_queries": decompose_query(query_text)}


def make_retrieve_node(service: RetrievalService) -> GraphNode:
    """Builds the retrieve node around one RetrievalService, injectable for tests.

    Runs once per entry in sub_queries when decomposition happened, or once
    on the single resolved_query otherwise -- the same node handles both
    shapes uniformly. When there's more than one query, each retrieve()
    call is a real network round-trip (embed + Qdrant search), so they run
    concurrently via a thread pool rather than one after another -- with up
    to 4 sub-queries, running them sequentially could mean several extra
    seconds of wait for one user question. A single query skips the thread
    pool entirely (the common case, no concurrency overhead needed).
    Results are merged by chunk_id (a chunk matching more than one
    sub-query is kept once) and re-sorted by score; a failure retrieving
    for one sub-query is logged and skipped rather than failing the whole
    response when other sub-queries succeeded.
    """

    def run_one(query_text: str, *, agent_id: str, principal_group_codes: frozenset[str], principal_user_id: str):
        return service.retrieve(agent_id=agent_id, query_text=rewrite_query(query_text),
            principal_group_codes=principal_group_codes, principal_user_id=principal_user_id)

    def retrieve_node(state: AgentState) -> dict[str, Any]:
        queries = state.get("sub_queries") or [state.get("resolved_query") or state["user_query"]]
        principal_group_codes = frozenset(state.get("principal_group_codes") or [])
        principal_user_id = state.get("principal_user_id", "")
        agent_id = state["agent_id"]

        results = []
        if len(queries) == 1:
            results = [run_one(queries[0], agent_id=agent_id, principal_group_codes=principal_group_codes,
                principal_user_id=principal_user_id)]
        else:
            with ThreadPoolExecutor(max_workers=len(queries)) as executor:
                future_to_query = {executor.submit(run_one, query_text, agent_id=agent_id,
                    principal_group_codes=principal_group_codes, principal_user_id=principal_user_id): query_text
                    for query_text in queries}
                for future in as_completed(future_to_query):
                    try:
                        results.append(future.result())
                    except Exception as error:
                        logger.error(f"[retrieve_node] Retrieval failed for sub-query {future_to_query[future]!r}: {error}")

        seen_chunk_ids: set[str] = set()
        combined_chunks: list[dict[str, Any]] = []
        for result in results:
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

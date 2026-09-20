"""Unit tests for the classify -> route -> retrieve LangGraph wiring.

Both nodes are injected as plain fake functions (constructor DI, matching
the repo's established pattern), so these tests never touch a real Azure
LLM or a real Qdrant collection -- only the routing/state-merging behavior
of the graph itself is under test.
"""

from __future__ import annotations

import unittest

from backend.agent.graph import build_agent_graph, make_retrieve_node


def base_state(**overrides) -> dict:
    state = {"agent_id": "adas-agent", "user_query": "hello", "conversation_history": [],
        "principal_group_codes": [], "principal_user_id": "u1"}
    state.update(overrides)
    return state


class RoutingTests(unittest.TestCase):
    """One test per intent branch, confirming who does and doesn't reach retrieve."""

    def graph(self, classify_result: dict, *, track_retrieve_calls: list):
        def fake_classify(state):
            return classify_result

        def fake_retrieve(state):
            track_retrieve_calls.append(state)
            return {"retrieved_chunks": [{"chunk_id": "should-not-appear-unless-called"}]}

        return build_agent_graph(classify_node=fake_classify, retrieve_node=fake_retrieve).compile()

    def test_domain_qn_reaches_retrieve(self):
        calls: list = []
        compiled = self.graph({"intent": "Domain_qn", "needs_clarification": False, "resolved_query": "torque spec"}, track_retrieve_calls=calls)
        result = compiled.invoke(base_state(user_query="torque spec"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["retrieved_chunks"], [{"chunk_id": "should-not-appear-unless-called"}])

    def test_general_chat_never_reaches_retrieve(self):
        calls: list = []
        compiled = self.graph({"intent": "general_chat", "needs_clarification": False, "answer": "Hi!"}, track_retrieve_calls=calls)
        result = compiled.invoke(base_state(user_query="hi"))
        self.assertEqual(calls, [])
        self.assertEqual(result["answer"], "Hi!")
        self.assertNotIn("retrieved_chunks", result)

    def test_out_of_scope_never_reaches_retrieve(self):
        calls: list = []
        compiled = self.graph({"intent": "out_of_scope", "needs_clarification": False, "answer": "Not my domain."}, track_retrieve_calls=calls)
        compiled.invoke(base_state(user_query="cook pasta"))
        self.assertEqual(calls, [])

    def test_clarification_qn_never_reaches_retrieve(self):
        calls: list = []
        compiled = self.graph({"intent": "clarification_qn", "needs_clarification": True, "ask_user": "Which component?"}, track_retrieve_calls=calls)
        result = compiled.invoke(base_state(user_query="check the value"))
        self.assertEqual(calls, [])
        self.assertEqual(result["ask_user"], "Which component?")


class RetrieveNodeInputTests(unittest.TestCase):
    """Confirms retrieve receives exactly what it needs from state."""

    def test_retrieve_node_receives_resolved_query_and_principal_fields(self):
        captured: list = []

        def fake_classify(state):
            return {"intent": "Domain_qn", "needs_clarification": False, "resolved_query": "resolved version of query"}

        def fake_retrieve(state):
            captured.append(state)
            return {"retrieved_chunks": []}

        compiled = build_agent_graph(classify_node=fake_classify, retrieve_node=fake_retrieve).compile()
        compiled.invoke(base_state(user_query="original query", principal_group_codes=["adas_engineer"], principal_user_id="bob"))
        seen_state = captured[0]
        self.assertEqual(seen_state["resolved_query"], "resolved version of query")
        self.assertEqual(seen_state["agent_id"], "adas-agent")
        self.assertEqual(seen_state["principal_group_codes"], ["adas_engineer"])
        self.assertEqual(seen_state["principal_user_id"], "bob")


class DecompositionRoutingTests(unittest.TestCase):
    """Confirms decompose only runs when classify_intent flags requires_decomposition."""

    def test_requires_decomposition_true_routes_through_decompose_first(self):
        decompose_calls: list = []
        retrieve_calls: list = []

        def fake_classify(state):
            return {"intent": "Domain_qn", "needs_clarification": False, "resolved_query": "compare A and B",
                "requires_decomposition": True}

        def fake_decompose(state):
            decompose_calls.append(state)
            return {"sub_queries": ["spec for A", "spec for B"]}

        def fake_retrieve(state):
            retrieve_calls.append(state.get("sub_queries"))
            return {"retrieved_chunks": []}

        compiled = build_agent_graph(classify_node=fake_classify, decompose_node=fake_decompose,
            retrieve_node=fake_retrieve).compile()
        compiled.invoke(base_state(user_query="compare A and B"))
        self.assertEqual(len(decompose_calls), 1)
        self.assertEqual(retrieve_calls, [["spec for A", "spec for B"]])

    def test_requires_decomposition_false_skips_decompose_entirely(self):
        decompose_calls: list = []
        retrieve_calls: list = []

        def fake_classify(state):
            return {"intent": "Domain_qn", "needs_clarification": False, "resolved_query": "single lookup",
                "requires_decomposition": False}

        def fake_decompose(state):
            decompose_calls.append(state)
            return {"sub_queries": ["should never run"]}

        def fake_retrieve(state):
            retrieve_calls.append(state.get("sub_queries"))
            return {"retrieved_chunks": []}

        compiled = build_agent_graph(classify_node=fake_classify, decompose_node=fake_decompose,
            retrieve_node=fake_retrieve).compile()
        compiled.invoke(base_state(user_query="single lookup"))
        self.assertEqual(decompose_calls, [])
        self.assertEqual(retrieve_calls, [None])


class FakeRetrievalResultChunk:
    """Mirrors just enough of RetrievedChunk for make_retrieve_node's model_dump() call."""

    def __init__(self, chunk_id: str, score: float) -> None:
        self.chunk_id = chunk_id
        self.score = score

    def model_dump(self) -> dict:
        return {"chunk_id": self.chunk_id, "score": self.score}


class FakeRetrievalResult:
    def __init__(self, chunks: list[FakeRetrievalResultChunk]) -> None:
        self.chunks = chunks


class FakeRetrievalService:
    """Returns a preconfigured result per query_text so merge/dedupe logic can be tested directly."""

    def __init__(self, results_by_query: dict[str, list[FakeRetrievalResultChunk]]) -> None:
        self.results_by_query = results_by_query
        self.calls: list[str] = []

    def retrieve(self, *, agent_id, query_text, principal_group_codes, principal_user_id, **_):
        self.calls.append(query_text)
        return FakeRetrievalResult(self.results_by_query.get(query_text, []))


class RetrieveNodeMergeTests(unittest.TestCase):
    """Unit-level tests of make_retrieve_node's multi-query merge/dedupe, bypassing the graph."""

    def test_single_query_behavior_is_unchanged_when_no_sub_queries_present(self):
        service = FakeRetrievalService({"resolved query": [FakeRetrievalResultChunk("c1", 0.9)]})
        node = make_retrieve_node(service)
        result = node(base_state(resolved_query="resolved query"))
        self.assertEqual(service.calls, ["resolved query"])
        self.assertEqual(result["retrieved_chunks"], [{"chunk_id": "c1", "score": 0.9}])

    def test_runs_once_per_sub_query_and_merges_results(self):
        service = FakeRetrievalService({
            "spec for A": [FakeRetrievalResultChunk("c1", 0.9)],
            "spec for B": [FakeRetrievalResultChunk("c2", 0.8)],
        })
        node = make_retrieve_node(service)
        result = node(base_state(sub_queries=["spec for A", "spec for B"]))
        self.assertEqual(service.calls, ["spec for A", "spec for B"])
        self.assertEqual([c["chunk_id"] for c in result["retrieved_chunks"]], ["c1", "c2"])

    def test_a_chunk_matching_multiple_sub_queries_is_deduplicated(self):
        service = FakeRetrievalService({
            "spec for A": [FakeRetrievalResultChunk("shared", 0.7), FakeRetrievalResultChunk("only-a", 0.6)],
            "spec for B": [FakeRetrievalResultChunk("shared", 0.7), FakeRetrievalResultChunk("only-b", 0.5)],
        })
        node = make_retrieve_node(service)
        result = node(base_state(sub_queries=["spec for A", "spec for B"]))
        chunk_ids = [c["chunk_id"] for c in result["retrieved_chunks"]]
        self.assertEqual(chunk_ids, ["shared", "only-a", "only-b"])
        self.assertEqual(len(chunk_ids), len(set(chunk_ids)))

    def test_merged_results_are_sorted_by_score_descending(self):
        service = FakeRetrievalService({
            "low first": [FakeRetrievalResultChunk("low", 0.4)],
            "high second": [FakeRetrievalResultChunk("high", 0.95)],
        })
        node = make_retrieve_node(service)
        result = node(base_state(sub_queries=["low first", "high second"]))
        self.assertEqual([c["chunk_id"] for c in result["retrieved_chunks"]], ["high", "low"])


if __name__ == "__main__":
    unittest.main()

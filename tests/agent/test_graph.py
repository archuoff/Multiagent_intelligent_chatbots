"""Unit tests for the classify -> route -> retrieve LangGraph wiring.

Both nodes are injected as plain fake functions (constructor DI, matching
the repo's established pattern), so these tests never touch a real Azure
LLM or a real Qdrant collection -- only the routing/state-merging behavior
of the graph itself is under test.
"""

from __future__ import annotations

import unittest

from backend.agent.graph import build_agent_graph


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


if __name__ == "__main__":
    unittest.main()

"""Regression test for the missing resolved_query field on AgentState (Phase 0).

nodes.state_update() has always written result["resolved_query"] for
Domain_qn intents, but AgentState's TypedDict never declared that key. This
pins the fix so a static/type checker (and any future LangGraph node relying
on the declared state shape) sees the field.
"""

import unittest

from backend.agent.nodes import state_update
from backend.agent.state import AgentState


class ResolvedQueryFieldTests(unittest.TestCase):
    def test_agent_state_declares_resolved_query(self):
        self.assertIn("resolved_query", AgentState.__annotations__)

    def test_domain_qn_state_update_includes_resolved_query(self):
        classification = {"intent": "Domain_qn", "resolved_query": "what is the torque spec for part X"}
        result = state_update("torque spec for it", classification)
        self.assertEqual(result["resolved_query"], "what is the torque spec for part X")

    def test_domain_qn_falls_back_to_original_query_when_unresolved(self):
        classification = {"intent": "Domain_qn"}
        result = state_update("original question", classification)
        self.assertEqual(result["resolved_query"], "original question")

    def test_non_domain_intents_do_not_set_resolved_query(self):
        for intent in ("general_chat", "out_of_scope", "clarification_qn"):
            classification = {"intent": intent, "clarification_question": "which one?"}
            result = state_update("hi", classification)
            self.assertNotIn("resolved_query", result)


class RequiresDecompositionFieldTests(unittest.TestCase):
    def test_agent_state_declares_requires_decomposition_and_sub_queries(self):
        self.assertIn("requires_decomposition", AgentState.__annotations__)
        self.assertIn("sub_queries", AgentState.__annotations__)

    def test_domain_qn_carries_requires_decomposition_true(self):
        classification = {"intent": "Domain_qn", "requires_decomposition": True}
        result = state_update("compare A and B", classification)
        self.assertTrue(result["requires_decomposition"])

    def test_domain_qn_defaults_requires_decomposition_to_false(self):
        classification = {"intent": "Domain_qn"}
        result = state_update("single lookup", classification)
        self.assertFalse(result["requires_decomposition"])

    def test_non_domain_intents_do_not_set_requires_decomposition(self):
        for intent in ("general_chat", "out_of_scope", "clarification_qn"):
            classification = {"intent": intent, "clarification_question": "which one?"}
            result = state_update("hi", classification)
            self.assertNotIn("requires_decomposition", result)


if __name__ == "__main__":
    unittest.main()

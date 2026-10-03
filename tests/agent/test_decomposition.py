"""Unit tests for query decomposition: the second, separate LLM call that
only runs when classify_intent's requires_decomposition flag is true.
"""

from __future__ import annotations

import unittest

from backend.agent.decomposition import decompose_query


class FakeChatClient:
    def __init__(self, reply: str | None = None, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[dict] = []

    def complete(self, messages: list[dict], *, temperature: float = 0.1) -> str:
        self.calls.append({"messages": messages, "temperature": temperature})
        if self.error is not None:
            raise self.error
        return self.reply or ""


class DecomposeQueryTests(unittest.TestCase):
    def test_splits_a_genuinely_multi_part_question(self):
        client = FakeChatClient(reply='{"sub_queries": ["torque spec for sensor A", "torque spec for sensor B"]}')
        result = decompose_query("compare torque specs for sensor A and sensor B", chat_client=client)
        self.assertEqual(result, ["torque spec for sensor A", "torque spec for sensor B"])
        self.assertEqual(client.calls[0]["messages"][-1]["content"], "compare torque specs for sensor A and sensor B")

    def test_single_item_response_passes_through_unchanged(self):
        client = FakeChatClient(reply='{"sub_queries": ["what is the torque spec for X"]}')
        result = decompose_query("what is the torque spec for X", chat_client=client)
        self.assertEqual(result, ["what is the torque spec for X"])

    def test_falls_back_to_original_query_on_client_error(self):
        client = FakeChatClient(error=RuntimeError("Azure is down"))
        result = decompose_query("compare A and B", chat_client=client)
        self.assertEqual(result, ["compare A and B"])

    def test_falls_back_to_original_query_on_malformed_json(self):
        client = FakeChatClient(reply="not valid json")
        result = decompose_query("compare A and B", chat_client=client)
        self.assertEqual(result, ["compare A and B"])

    def test_falls_back_to_original_query_when_sub_queries_list_is_empty(self):
        client = FakeChatClient(reply='{"sub_queries": []}')
        result = decompose_query("compare A and B", chat_client=client)
        self.assertEqual(result, ["compare A and B"])

    def test_strips_markdown_fences(self):
        client = FakeChatClient(reply='```json\n{"sub_queries": ["a", "b"]}\n```')
        result = decompose_query("a and b", chat_client=client)
        self.assertEqual(result, ["a", "b"])

    def test_caps_at_four_sub_queries(self):
        client = FakeChatClient(reply='{"sub_queries": ["a", "b", "c", "d", "e", "f"]}')
        result = decompose_query("many things", chat_client=client)
        self.assertEqual(len(result), 4)
        self.assertEqual(result, ["a", "b", "c", "d"])

    def test_drops_blank_sub_query_entries(self):
        client = FakeChatClient(reply='{"sub_queries": ["real question", "  ", ""]}')
        result = decompose_query("real question", chat_client=client)
        self.assertEqual(result, ["real question"])


if __name__ == "__main__":
    unittest.main()

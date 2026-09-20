"""Unit tests for call_intent_classifier's migration off Ollama to Azure OpenAI.

Confirms the classifier now goes through an injected AzureOpenAIChatClient
(not a local Ollama HTTP call), and that its existing fail-safe fallback
behavior (LLM error, malformed JSON) still works with the new client.
"""

from __future__ import annotations

import unittest

from backend.agent.nodes import call_intent_classifier


class FakeChatClient:
    """Records every completion request and returns one fixed reply."""

    def __init__(self, reply: str | None = None, error: Exception | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[dict] = []

    def complete(self, messages: list[dict], *, temperature: float = 0.1) -> str:
        self.calls.append({"messages": messages, "temperature": temperature})
        if self.error is not None:
            raise self.error
        return self.reply or ""


class CallIntentClassifierTests(unittest.TestCase):
    def test_uses_the_injected_azure_chat_client(self):
        """Confirms the classifier no longer talks to a local Ollama server."""
        client = FakeChatClient(reply='{"intent": "Domain_qn", "resolved_query": "torque spec for X"}')
        result = call_intent_classifier("adas-agent", "torque spec for X", chat_client=client)
        self.assertEqual(result["intent"], "Domain_qn")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.calls[0]["messages"][-1], {"role": "user", "content": "torque spec for X"})
        self.assertIn("ADAS", client.calls[0]["messages"][0]["content"])

    def test_falls_back_when_the_chat_client_raises(self):
        """A live LLM outage still fails open to Domain_qn, not a crash."""
        client = FakeChatClient(error=RuntimeError("Azure is down"))
        result = call_intent_classifier("adas-agent", "anything", chat_client=client)
        self.assertEqual(result["intent"], "Domain_qn")
        self.assertEqual(result["resolved_query"], "anything")

    def test_falls_back_on_malformed_json_response(self):
        client = FakeChatClient(reply="not valid json at all")
        result = call_intent_classifier("adas-agent", "anything", chat_client=client)
        self.assertEqual(result["intent"], "Domain_qn")

    def test_strips_markdown_fences_from_a_well_formed_response(self):
        client = FakeChatClient(reply='```json\n{"intent": "general_chat"}\n```')
        result = call_intent_classifier("adas-agent", "hi", chat_client=client)
        self.assertEqual(result["intent"], "general_chat")

    def test_includes_recent_conversation_history_in_the_request(self):
        client = FakeChatClient(reply='{"intent": "Domain_qn"}')
        history = [{"role": "user", "content": "earlier question"}, {"role": "assistant", "content": "earlier answer"}]
        call_intent_classifier("adas-agent", "follow-up", history, chat_client=client)
        sent_messages = client.calls[0]["messages"]
        self.assertIn({"role": "user", "content": "earlier question"}, sent_messages)

    def test_requires_decomposition_true_is_preserved(self):
        client = FakeChatClient(reply='{"intent": "Domain_qn", "requires_decomposition": true}')
        result = call_intent_classifier("adas-agent", "compare A and B", chat_client=client)
        self.assertTrue(result["requires_decomposition"])

    def test_requires_decomposition_defaults_to_false_when_absent(self):
        """Backward-safe default for an LLM response that omits the new field."""
        client = FakeChatClient(reply='{"intent": "Domain_qn"}')
        result = call_intent_classifier("adas-agent", "single lookup", chat_client=client)
        self.assertFalse(result["requires_decomposition"])

    def test_requires_decomposition_defaults_to_false_on_fallback(self):
        client = FakeChatClient(error=RuntimeError("Azure is down"))
        result = call_intent_classifier("adas-agent", "anything", chat_client=client)
        self.assertFalse(result["requires_decomposition"])


if __name__ == "__main__":
    unittest.main()

"""Unit tests for the Azure OpenAI chat completion client.

Follows the same offline Fake-SDK-client pattern as
tests/ingestion/test_azure_embeddings.py -- no real Azure SDK or network call.
"""

from __future__ import annotations

import unittest

from backend.agent.azure_chat import AzureOpenAIChatClient


class FakeMessage:
    def __init__(self, content: str) -> None:
        self.content = content


class FakeChoice:
    def __init__(self, content: str) -> None:
        self.message = FakeMessage(content)


class FakeCompletionsApi:
    """Records the request shape and returns one fixed assistant reply."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list[dict] = []

    def create(self, *, model: str, messages: list[dict], temperature: float):
        self.calls.append({"model": model, "messages": messages, "temperature": temperature})
        return type("Response", (), {"choices": [FakeChoice(self.reply)]})()


class FakeChatApi:
    def __init__(self, reply: str) -> None:
        self.completions = FakeCompletionsApi(reply)


class FakeClient:
    """Exposes the same nested chat API shape used by the Azure adapter."""

    def __init__(self, reply: str) -> None:
        self.chat = FakeChatApi(reply)


class AzureChatClientTests(unittest.TestCase):
    def test_complete_returns_the_assistant_text(self):
        client = FakeClient(reply='{"intent": "Domain_qn"}')
        adapter = AzureOpenAIChatClient(deployment="chat-deployment", client=client)
        result = adapter.complete([{"role": "user", "content": "torque spec?"}], temperature=0.1)
        self.assertEqual(result, '{"intent": "Domain_qn"}')
        self.assertEqual(client.chat.completions.calls[0]["model"], "chat-deployment")
        self.assertEqual(client.chat.completions.calls[0]["temperature"], 0.1)

    def test_returns_empty_string_when_content_is_none(self):
        """Guards against a content-filtered or empty SDK response crashing the caller."""
        client = FakeClient(reply=None)
        adapter = AzureOpenAIChatClient(deployment="chat-deployment", client=client)
        result = adapter.complete([{"role": "user", "content": "hi"}])
        self.assertEqual(result, "")


if __name__ == "__main__":
    unittest.main()

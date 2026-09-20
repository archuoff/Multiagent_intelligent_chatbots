"""Azure OpenAI chat completion client for the agent layer.

Mirrors backend.ingestion.embedding.azure_openai.AzureOpenAIEmbeddingProvider's
pattern (same env vars, same from_runtime_environment() shape) so intent
classification and, later, answer synthesis use the same credentialed
provider embeddings already use -- one consistent backend instead of a
local Ollama server the production deployment can't depend on being up.
"""

from __future__ import annotations

import os
from typing import Any


class AzureOpenAIChatClient:
    """Calls an Azure OpenAI chat completion deployment through the official SDK."""

    def __init__(self, deployment: str, client: Any) -> None:
        """Stores a prebuilt client so application code can inject or test it safely."""
        self.deployment = deployment
        self._client = client

    @classmethod
    def from_runtime_environment(cls) -> "AzureOpenAIChatClient":
        """Builds the client from local environment settings without returning its API key."""
        cls._load_dotenv_if_available()
        endpoint = cls._required_environment_value("AZURE_OPENAI_ENDPOINT")
        api_key = cls._required_environment_value("AZURE_OPENAI_API_KEY")
        deployment = cls._required_environment_value("AZURE_OPENAI_CHAT_DEPLOYMENT")
        api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
        try:
            from openai import AzureOpenAI
        except ImportError as error:
            raise RuntimeError("Azure chat completion requires the 'openai' package. Install requirements.txt first.") from error
        return cls(deployment=deployment, client=AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version))

    def complete(self, messages: list[dict[str, str]], *, temperature: float = 0.1) -> str:
        """Sends one chat completion request and returns the assistant's text content."""
        response = self._client.chat.completions.create(model=self.deployment, messages=messages, temperature=temperature)
        return response.choices[0].message.content or ""

    @staticmethod
    def _load_dotenv_if_available() -> None:
        """Loads local variables for application execution without overwriting process settings."""
        try:
            from dotenv import load_dotenv
        except ImportError:
            return
        load_dotenv(override=False)

    @staticmethod
    def _required_environment_value(name: str) -> str:
        """Fails safely when a required setting is absent without printing its value."""
        value = os.getenv(name)
        if not value:
            raise RuntimeError(f"Required runtime setting is missing: {name}.")
        return value

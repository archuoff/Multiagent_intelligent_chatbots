"""Azure OpenAI adapter for the JLR embedding boundary.

The adapter loads Azure settings only inside the running application process.
It never logs API keys, source text, or complete SDK errors.  Tests inject a
fake client, so installing the Azure SDK is not required to run unit tests.
"""

from __future__ import annotations

import os
from typing import Any

from backend.ingestion.embedding.contracts import EmbeddingModelSettings


class AzureOpenAIEmbeddingProvider:
    """Calls an Azure OpenAI embedding deployment through the official SDK."""

    def __init__(self, settings: EmbeddingModelSettings, client: Any) -> None:
        """Stores a prebuilt client so application code can inject or test it safely."""
        if settings.provider != "azure_openai":
            raise ValueError("AzureOpenAIEmbeddingProvider requires provider='azure_openai'.")
        self.settings = settings
        self._client = client

    @classmethod
    def from_runtime_environment(cls) -> "AzureOpenAIEmbeddingProvider":
        """Builds the provider from local environment settings without returning its API key."""
        cls._load_dotenv_if_available()
        endpoint = cls._required_environment_value("AZURE_OPENAI_ENDPOINT")
        api_key = cls._required_environment_value("AZURE_OPENAI_API_KEY")
        settings = EmbeddingModelSettings(
            deployment=cls._required_environment_value("AZURE_OPENAI_EMBEDDING_DEPLOYMENT"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01"),
            dimensions=int(os.getenv("AZURE_OPENAI_EMBEDDING_DIMENSIONS", "1536")),
        )
        try:
            from openai import AzureOpenAI
        except ImportError as error:
            raise RuntimeError("Azure embedding requires the 'openai' package. Install requirements.txt first.") from error
        return cls(settings=settings, client=AzureOpenAI(azure_endpoint=endpoint, api_key=api_key,
            api_version=settings.api_version))

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embeds one ordered batch and restores Azure's explicit response index order."""
        if not texts:
            return []
        response = self._client.embeddings.create(input=texts, model=self.settings.deployment)
        records = sorted(response.data, key=lambda record: record.index)
        if len(records) != len(texts):
            raise RuntimeError("Azure returned a different number of embeddings than requested.")
        return [list(record.embedding) for record in records]

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

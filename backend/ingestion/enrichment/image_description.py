"""Optional Azure OpenAI vision descriptions for stored image assets.

This enrichment reads images already extracted into ``storage/visual-assets``
and adds an inferred description to image-node metadata. It is disabled by
default because it makes remote calls, costs money, and produces AI-inferred
content that must stay separate from authoritative OCR/source text.
"""

from __future__ import annotations

import base64
import mimetypes
import os
from pathlib import Path
from typing import Any, Protocol

from backend.ingestion.models import CanonicalDocument
from backend.ingestion.models import CanonicalNode
from backend.ingestion.models import NodeType
from backend.ingestion.utils import normalize_text


class VisionDescriptionProvider(Protocol):
    """Minimal provider contract used by image-description enrichment."""

    deployment: str

    def describe(self, image_path: Path, *, context: str) -> str:
        """Returns a concise image description for retrieval enrichment."""


class AzureOpenAIVisionDescriptionProvider:
    """Calls an Azure OpenAI vision-capable chat deployment for image summaries."""

    def __init__(self, *, endpoint: str, api_key: str, api_version: str, deployment: str,
                 prompt: str | None = None, client: Any | None = None) -> None:
        """Stores non-secret settings and lazily builds the Azure OpenAI client."""
        self.deployment = deployment
        self._prompt = prompt or self._default_prompt()
        self._client = client
        self._endpoint = endpoint
        self._api_key = api_key
        self._api_version = api_version

    @classmethod
    def from_runtime_environment(cls) -> "AzureOpenAIVisionDescriptionProvider":
        """Builds the provider from environment variables without logging secrets."""
        cls._load_dotenv_if_available()
        return cls(
            endpoint=cls._required("AZURE_OPENAI_ENDPOINT"),
            api_key=cls._required("AZURE_OPENAI_API_KEY"),
            api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01"),
            deployment=cls._required("AZURE_OPENAI_VISION_DEPLOYMENT"),
            prompt=os.getenv("JLR_IMAGE_DESCRIPTION_PROMPT"),
        )

    def describe(self, image_path: Path, *, context: str) -> str:
        """Sends one stored image to Azure vision and returns normalized text."""
        data_url = self._data_url(image_path)
        response = self._get_client().chat.completions.create(
            model=self.deployment,
            temperature=0,
            max_tokens=int(os.getenv("JLR_IMAGE_DESCRIPTION_MAX_TOKENS", "220")),
            messages=[
                {
                    "role": "system",
                    "content": "Describe engineering images for retrieval. Do not invent values that are not visible.",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"{self._prompt}\n\nNearby source context:\n{context}"},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                },
            ],
        )
        content = response.choices[0].message.content
        return normalize_text(str(content or "")) or ""

    def _get_client(self) -> Any:
        """Creates the SDK client only when image description is enabled and used."""
        if self._client is None:
            try:
                from openai import AzureOpenAI
            except ImportError as error:
                raise RuntimeError("Azure image description requires the 'openai' package.") from error
            self._client = AzureOpenAI(azure_endpoint=self._endpoint, api_key=self._api_key,
                                       api_version=self._api_version)
        return self._client

    def _data_url(self, image_path: Path) -> str:
        """Encodes a stored image as a data URL accepted by vision chat models."""
        media_type = mimetypes.guess_type(str(image_path))[0] or "image/png"
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        return f"data:{media_type};base64,{encoded}"

    def _default_prompt(self) -> str:
        """Keeps image descriptions retrieval-oriented and clearly non-authoritative."""
        return (
            "Describe the visible engineering content in this image in 3-6 concise bullets. "
            "Include visible labels, dimensions, part names, relationships, and diagram intent. "
            "If details are unclear, say so instead of guessing."
        )

    @staticmethod
    def _load_dotenv_if_available() -> None:
        """Loads local runtime values without overwriting process-provided variables."""
        try:
            from dotenv import load_dotenv
        except ImportError:
            return
        load_dotenv(override=False)

    @staticmethod
    def _required(name: str) -> str:
        """Fails safely when a required setting is missing."""
        value = os.getenv(name)
        if not value:
            raise RuntimeError(f"Required runtime setting is missing: {name}.")
        return value


class ImageDescriptionEnrichment:
    """Adds optional AI-inferred descriptions to canonical image nodes."""

    def __init__(self, provider: VisionDescriptionProvider | None = None) -> None:
        """Allows tests to inject a fake provider while production uses Azure on demand."""
        self._provider = provider

    def apply(self, document: CanonicalDocument) -> int:
        """Describes stored images only when explicitly enabled by runtime config."""
        if os.getenv("JLR_IMAGE_DESCRIPTION", "false").lower() != "true":
            return 0
        provider = self._provider or AzureOpenAIVisionDescriptionProvider.from_runtime_environment()
        processed = 0
        for root in document.root_nodes:
            for node in self._walk(root):
                if node.node_type != NodeType.IMAGE:
                    continue
                if not self._should_describe(node):
                    self._mark_skipped(node, "Image does not match the configured vision-description scope.")
                    continue
                if self._describe_node(node, provider):
                    processed += 1
        return processed

    def _should_describe(self, node: CanonicalNode) -> bool:
        """Limits VLM usage to likely useful image nodes unless explicitly overridden."""
        scope = os.getenv("JLR_IMAGE_DESCRIPTION_SCOPE", "candidates").lower()
        if scope == "all":
            return True
        if scope == "vision_candidates":
            return bool(node.attributes.get("needs_vision_description"))
        ocr_text = normalize_text(str(node.attributes.get("ocr_text") or ""))
        return bool(node.attributes.get("needs_vision_description")) or not bool(ocr_text)

    def _describe_node(self, node: CanonicalNode, provider: VisionDescriptionProvider) -> bool:
        """Adds description metadata without changing OCR text or canonical source text."""
        image_path = Path(str(node.attributes.get("saved_path") or ""))
        if not image_path.is_file():
            self._mark_skipped(node, "No stored image file is available for description.")
            return False
        try:
            description = provider.describe(image_path, context=self._context_for(node))
        except Exception as error:
            node.attributes.update({
                "image_description_status": "failed",
                "image_description": "",
                "image_description_source": "azure_openai_vision",
                "image_description_is_inferred": True,
                "image_description_warnings": [f"Image description failed: {type(error).__name__}"],
            })
            return False
        node.attributes.update({
            "image_description_status": "success" if description else "no_description",
            "image_description": description,
            "image_description_source": "azure_openai_vision",
            "image_description_model": provider.deployment,
            "image_description_is_inferred": True,
            "image_description_warnings": [],
        })
        return bool(description)

    def _context_for(self, node: CanonicalNode) -> str:
        """Passes non-sensitive local node context to improve caption specificity."""
        parts = [
            str(node.title or ""),
            str(node.attributes.get("shape_name") or ""),
            str(node.attributes.get("nearby_text") or ""),
            str(node.attributes.get("ocr_text") or ""),
        ]
        return "\n".join(part for part in (normalize_text(part) for part in parts) if part)[:1200]

    def _mark_skipped(self, node: CanonicalNode, reason: str) -> None:
        """Records why description did not run so audits can distinguish skipped images."""
        node.attributes.update({
            "image_description_status": "skipped",
            "image_description": "",
            "image_description_source": "azure_openai_vision",
            "image_description_is_inferred": True,
            "image_description_warnings": [reason],
        })

    def _walk(self, node: CanonicalNode):
        """Yields the canonical tree in source order."""
        yield node
        for child in node.children:
            yield from self._walk(child)

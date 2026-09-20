"""Optional Azure OpenAI vision descriptions for stored image assets.

This enrichment reads images already extracted into ``storage/visual-assets``
and adds an inferred description to image-node metadata. It is disabled by
default because it makes remote calls, costs money, and produces AI-inferred
content that must stay separate from authoritative OCR/source text.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
from pathlib import Path
from typing import Any, Protocol

from backend.ingestion.models import CanonicalDocument
from backend.ingestion.models import CanonicalNode
from backend.ingestion.models import NodeType
from backend.ingestion.models import SourceType
from backend.ingestion.utils import normalize_text


class VisionDescriptionProvider(Protocol):
    """Minimal provider contract used by image-description enrichment."""

    deployment: str

    def describe(self, image_path: Path, *, context: str) -> Any:
        """Returns visible text and a concise image description for retrieval enrichment."""


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

    def describe(self, image_path: Path, *, context: str) -> dict[str, str]:
        """Sends one stored image to Azure vision and returns normalized JSON fields."""
        data_url = self._data_url(image_path)
        response = self._get_client().chat.completions.create(
            model=self.deployment,
            temperature=0,
            max_tokens=int(os.getenv("JLR_IMAGE_DESCRIPTION_MAX_TOKENS", "220")),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You extract engineering image evidence for retrieval. "
                        "Return only valid JSON. Do not invent values that are not visible."
                    ),
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
        content = str(response.choices[0].message.content or "")
        return self._parse_response(content)

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
            "Read this image and return JSON with these string keys: "
            "visible_text, description, uncertainty, confidence. "
            "visible_text must transcribe visible labels/numbers exactly where readable. "
            "description must summarize the visible engineering meaning in 3-6 concise bullets. "
            "uncertainty must list unclear or unreadable areas. "
            "confidence must be high, medium, or low."
        )

    def _parse_response(self, content: str) -> dict[str, str]:
        """Parses strict JSON output while keeping a safe fallback for imperfect model replies."""
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return {"visible_text": "", "description": normalize_text(content) or "",
                    "uncertainty": "Model response was not valid JSON.", "confidence": "low"}
        if not isinstance(payload, dict):
            return {"visible_text": "", "description": "", "uncertainty": "Model response was not an object.",
                    "confidence": "low"}
        return {
            "visible_text": normalize_text(str(payload.get("visible_text") or "")) or "",
            "description": normalize_text(str(payload.get("description") or "")) or "",
            "uncertainty": normalize_text(str(payload.get("uncertainty") or "")) or "",
            "confidence": normalize_text(str(payload.get("confidence") or "")) or "low",
        }

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
        self._apply_required_policy(document)
        if os.getenv("JLR_IMAGE_DESCRIPTION", "false").lower() != "true":
            self._mark_required_nodes(document, "Azure vision description is not enabled.")
            return 0
        try:
            provider = self._provider or AzureOpenAIVisionDescriptionProvider.from_runtime_environment()
        except Exception as error:
            self._mark_required_nodes(document, f"Azure vision description is not configured: {type(error).__name__}.")
            return 0
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

    def _apply_required_policy(self, document: CanonicalDocument) -> None:
        """Marks visual-heavy images across file types so OCR failure can fall back to VLM."""
        for root in document.root_nodes:
            for node in self._walk(root):
                if node.node_type != NodeType.IMAGE:
                    continue
                if self._is_vlm_required(document, node):
                    node.attributes["needs_vision_description"] = True
                    node.attributes.setdefault("vision_reason", self._vision_reason(document, node))

    def _is_vlm_required(self, document: CanonicalDocument, node: CanonicalNode) -> bool:
        """Decides when image understanding is required rather than optional decoration."""
        if node.attributes.get("needs_vision_description"):
            return True
        if not node.attributes.get("saved_path"):
            return False
        if document.source_type == SourceType.XLSX:
            return False
        if document.source_type in {SourceType.PPT, SourceType.PPTX}:
            return self._slide_has_little_text(document, node)
        if document.source_type in {SourceType.PDF, SourceType.DOCX}:
            return self._nearby_context_is_weak(node)
        return False

    def _vision_reason(self, document: CanonicalDocument, node: CanonicalNode) -> str:
        """Explains why Azure vision is required for this image node."""
        if node.attributes.get("vision_reason"):
            return str(node.attributes["vision_reason"])
        if document.source_type in {SourceType.PPT, SourceType.PPTX}:
            return "Image is on a slide with limited extracted text, so VLM transcription/description is required."
        if document.source_type == SourceType.PDF:
            return "PDF image has limited nearby extracted text, so VLM transcription/description is required."
        if document.source_type == SourceType.DOCX:
            return "DOCX image has limited locator text, so VLM transcription/description is required."
        return "Image requires VLM transcription/description."

    def _slide_has_little_text(self, document: CanonicalDocument, node: CanonicalNode) -> bool:
        """Detects diagram-heavy PowerPoint slides without hard-coding slide numbers."""
        parent_id = node.provenance.parent_node_id
        parent = self._find_node(document, parent_id) if parent_id else None
        if parent is None:
            return True
        text = self._subtree_text(parent, exclude_node_id=node.node_id)
        return len(text) < int(os.getenv("JLR_VLM_MIN_SURROUNDING_TEXT_CHARS", "120"))

    def _nearby_context_is_weak(self, node: CanonicalNode) -> bool:
        """Uses local image context fields to identify likely image-only PDF/DOCX content."""
        context = "\n".join([
            str(node.title or ""),
            str(node.attributes.get("caption") or ""),
            str(node.attributes.get("nearby_text") or ""),
            str(node.attributes.get("locator") or ""),
        ])
        return len(normalize_text(context) or "") < int(os.getenv("JLR_VLM_MIN_SURROUNDING_TEXT_CHARS", "120"))

    def _describe_node(self, node: CanonicalNode, provider: VisionDescriptionProvider) -> bool:
        """Adds description metadata without changing OCR text or canonical source text."""
        image_path = Path(str(node.attributes.get("saved_path") or ""))
        if not image_path.is_file():
            self._mark_skipped(node, "No stored image file is available for description.")
            self._mark_required_review(node)
            return False
        try:
            result = self._coerce_result(provider.describe(image_path, context=self._context_for(node)))
        except Exception as error:
            node.attributes.update({
                "image_description_status": "failed",
                "image_description": "",
                "vlm_transcribed_text": "",
                "vlm_confidence": "low",
                "vlm_uncertainty": "",
                "image_description_source": "azure_openai_vision",
                "image_description_is_inferred": True,
                "image_description_warnings": [f"Image description failed: {type(error).__name__}"],
            })
            self._mark_required_review(node)
            return False
        description = result["description"]
        transcribed_text = result["visible_text"]
        node.attributes.update({
            "image_description_status": "success" if description or transcribed_text else "no_description",
            "image_description": description,
            "vlm_transcribed_text": transcribed_text,
            "vlm_confidence": result["confidence"],
            "vlm_uncertainty": result["uncertainty"],
            "image_description_source": "azure_openai_vision",
            "image_description_model": provider.deployment,
            "image_description_is_inferred": True,
            "image_description_warnings": [],
        })
        if node.attributes["image_description_status"] != "success":
            self._mark_required_review(node)
        return bool(description or transcribed_text)

    def _coerce_result(self, result: Any) -> dict[str, str]:
        """Supports older providers/tests that returned a plain description string."""
        if isinstance(result, dict):
            return {
                "visible_text": normalize_text(str(result.get("visible_text") or "")) or "",
                "description": normalize_text(str(result.get("description") or "")) or "",
                "uncertainty": normalize_text(str(result.get("uncertainty") or "")) or "",
                "confidence": normalize_text(str(result.get("confidence") or "")) or "low",
            }
        return {"visible_text": "", "description": normalize_text(str(result or "")) or "",
                "uncertainty": "", "confidence": "medium"}

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
            "vlm_transcribed_text": "",
            "vlm_confidence": "low",
            "vlm_uncertainty": "",
            "image_description_source": "azure_openai_vision",
            "image_description_is_inferred": True,
            "image_description_warnings": [reason],
        })

    def _mark_required_nodes(self, document: CanonicalDocument, reason: str) -> None:
        """Flags image-heavy candidates when required VLM enrichment cannot run."""
        for root in document.root_nodes:
            for node in self._walk(root):
                if node.node_type == NodeType.IMAGE and node.attributes.get("needs_vision_description"):
                    self._mark_skipped(node, reason)
                    self._mark_required_review(node)

    def _mark_required_review(self, node: CanonicalNode) -> None:
        """Blocks image-heavy visual claims from retrieval until VLM or human review succeeds."""
        if node.attributes.get("needs_vision_description"):
            node.attributes["requires_review"] = True
            node.attributes["retrieval_allowed"] = False

    def _find_node(self, document: CanonicalDocument, node_id: str | None) -> CanonicalNode | None:
        """Finds a canonical node by id for local context checks."""
        if not node_id:
            return None
        for root in document.root_nodes:
            for node in self._walk(root):
                if node.node_id == node_id:
                    return node
        return None

    def _subtree_text(self, node: CanonicalNode, *, exclude_node_id: str | None = None) -> str:
        """Collects nearby source text without including the image node itself."""
        parts: list[str] = []
        for item in self._walk(node):
            if item.node_id == exclude_node_id:
                continue
            if item.node_type != NodeType.IMAGE:
                parts.extend([str(item.title or ""), str(item.text or "")])
        return normalize_text("\n".join(part for part in parts if part)) or ""

    def _walk(self, node: CanonicalNode):
        """Yields the canonical tree in source order."""
        yield node
        for child in node.children:
            yield from self._walk(child)

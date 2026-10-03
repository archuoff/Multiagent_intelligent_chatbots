"""Optional Azure OpenAI visual understanding for stored visual assets.

This enrichment reads visual assets already extracted into ``storage/visual-assets``
and adds inferred descriptions/transcriptions to visual-node metadata. It is
disabled by default because it makes remote calls, costs money, and produces
AI-inferred content that must stay separate from authoritative OCR/source text.
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
    """Adds optional AI-inferred descriptions to visual candidates."""

    def __init__(self, provider: VisionDescriptionProvider | None = None) -> None:
        """Allows tests to inject a fake provider while production uses Azure on demand."""
        self._provider = provider

    def apply(self, document: CanonicalDocument) -> int:
        """Describes stored visual candidates only when explicitly enabled by runtime config."""
        AzureOpenAIVisionDescriptionProvider._load_dotenv_if_available()
        self._apply_required_policy(document)
        if not self._vision_enabled():
            self._mark_required_nodes(document, "Azure visual enrichment is not enabled.")
            return 0
        try:
            provider = self._provider or AzureOpenAIVisionDescriptionProvider.from_runtime_environment()
        except Exception as error:
            self._mark_required_nodes(document, f"Azure vision description is not configured: {type(error).__name__}.")
            return 0
        processed = 0
        for root in document.root_nodes:
            for node in self._walk(root):
                if not self._is_visual_candidate_node(node):
                    continue
                if not self._should_describe(node):
                    self._mark_skipped(node, "Image does not match the configured vision-description scope.")
                    continue
                if self._describe_node(node, provider):
                    processed += 1
        return processed

    def _vision_enabled(self) -> bool:
        """Supports the old image flag and the broader visual-enrichment flag."""
        return (os.getenv("JLR_VISUAL_ENRICHMENT", "false").lower() == "true"
                or os.getenv("JLR_IMAGE_DESCRIPTION", "false").lower() == "true")

    def _is_visual_candidate_node(self, node: CanonicalNode) -> bool:
        """Returns nodes that can request visual understanding metadata."""
        return node.node_type in {NodeType.IMAGE, NodeType.TABLE, NodeType.EMBEDDED_DATASET}

    def _should_describe(self, node: CanonicalNode) -> bool:
        """Limits VLM usage to meaningful visual candidates.

        Empty OCR alone is not enough to call a VLM because many useful images
        are pure visuals and many decorative images also contain no text. The
        decision must come from visual importance, extraction weakness, or an
        explicit candidate signal.
        """
        scope = os.getenv("JLR_IMAGE_DESCRIPTION_SCOPE", "candidates").lower()
        if self._is_decorative_visual(node) and scope != "all":
            self._set_vlm_gate(node, "skipped", "decorative_or_small_visual")
            return False
        if scope == "all":
            accepted = not self._is_small_decorative_visual(node)
            self._set_vlm_gate(node, "accepted" if accepted else "skipped", "scope_all" if accepted else "small_decorative_visual")
            return accepted
        accepted = bool(node.attributes.get("needs_vision_description"))
        if not accepted:
            self._set_vlm_gate(node, "skipped", "not_a_strong_visual_candidate")
        return accepted

    def _apply_required_policy(self, document: CanonicalDocument) -> None:
        """Marks visual-heavy nodes so weak extraction can fall back to VLM enrichment."""
        for root in document.root_nodes:
            for node in self._walk(root):
                if not self._is_visual_candidate_node(node):
                    continue
                required, reason, score = self._vlm_decision(document, node)
                self._set_vlm_gate(node, "accepted" if required else "skipped", reason, score)
                if required:
                    node.attributes["needs_vision_description"] = True
                    node.attributes.setdefault("vision_reason", self._vision_reason(document, node))

    def _is_vlm_required(self, document: CanonicalDocument, node: CanonicalNode) -> bool:
        """Decides when image understanding is required rather than optional decoration."""
        required, _, _ = self._vlm_decision(document, node)
        return required

    def _vlm_decision(self, document: CanonicalDocument, node: CanonicalNode) -> tuple[bool, str, int | None]:
        """Returns the VLM decision, reason, and optional image score for audit."""
        if node.attributes.get("needs_vision_description"):
            return True, "explicitly_marked_for_vision", None
        if document.source_type == SourceType.XLSX:
            return False, "xlsx_visual_enrichment_disabled", None
        if node.node_type == NodeType.TABLE:
            weak = self._table_is_weak(node)
            return weak, "weak_table_extraction" if weak else "table_extraction_strong", None
        if node.node_type == NodeType.EMBEDDED_DATASET:
            needs = self._dataset_needs_vision(node)
            return needs, "chart_or_visual_dataset" if needs else "dataset_not_visual_candidate", None
        if node.node_type == NodeType.IMAGE:
            return self._image_vlm_decision(document, node)
        return False, "unsupported_visual_node", None

    def _image_vlm_decision(self, document: CanonicalDocument, node: CanonicalNode) -> tuple[bool, str, int]:
        """Scores image nodes without treating empty OCR as a VLM reason by itself."""
        if self._is_decorative_visual(node):
            return False, "decorative_or_small_visual", 0
        if self._image_content_is_complex(node):
            return True, "complex_visual_keyword", 5
        if self._has_meaningful_ocr(node) and not self._nearby_context_is_weak(node):
            return False, "ocr_and_context_sufficient", 0
        score = 0
        reasons: list[str] = []
        area_ratio = self._visual_area_ratio(node)
        if area_ratio >= float(os.getenv("JLR_VLM_LARGE_IMAGE_AREA_RATIO", "0.08")):
            score += 2
            reasons.append("large_visual")
        if area_ratio >= float(os.getenv("JLR_VLM_VERY_LARGE_IMAGE_AREA_RATIO", "0.18")):
            score += 1
            reasons.append("very_large_visual")
        if node.attributes.get("docling_visual_hint") and area_ratio >= float(os.getenv("JLR_VLM_HINT_MIN_AREA_RATIO", "0.05")):
            score += 1
            reasons.append("docling_visual_hint")
        if self._nearby_context_is_weak(node):
            score += 1
            reasons.append("weak_nearby_context")
        if document.source_type in {SourceType.PPT, SourceType.PPTX} and self._slide_has_little_text(document, node):
            score += 1
            reasons.append("weak_slide_context")
        min_score = int(os.getenv("JLR_VLM_MIN_VISUAL_SCORE", "4"))
        if score >= min_score:
            return True, "+".join(reasons) or "image_score_threshold", score
        return False, "empty_or_weak_ocr_without_visual_importance" if not self._has_meaningful_ocr(node) else "image_score_below_threshold", score

    def _image_needs_vision(self, document: CanonicalDocument, node: CanonicalNode) -> bool:
        """Backward-compatible boolean wrapper for image VLM gating."""
        required, _, _ = self._image_vlm_decision(document, node)
        return required


    def _set_vlm_gate(self, node: CanonicalNode, decision: str, reason: str, score: int | None = None) -> None:
        """Stores compact audit metadata for VLM gating decisions."""
        gate = {"decision": decision, "reason": reason}
        if score is not None:
            gate["score"] = score
        node.attributes["vlm_gate"] = gate

    def _has_meaningful_ocr(self, node: CanonicalNode) -> bool:
        """Returns true when OCR already gives useful text for retrieval."""
        text = normalize_text(str(node.attributes.get("ocr_text") or "")) or ""
        confidence = node.attributes.get("ocr_confidence")
        min_chars = int(os.getenv("JLR_VLM_MIN_OCR_TEXT_CHARS", "24"))
        min_confidence = float(os.getenv("JLR_VLM_STRONG_OCR_CONFIDENCE", "0.75"))
        return len(text) >= min_chars and (not isinstance(confidence, (int, float)) or float(confidence) >= min_confidence)

    def _is_decorative_visual(self, node: CanonicalNode) -> bool:
        """Detects small/decorative assets that should stay OCR-only."""
        fields = " ".join([
            str(node.title or ""),
            str(node.attributes.get("shape_name") or ""),
            str(node.attributes.get("image_type") or ""),
            str(node.attributes.get("classification") or ""),
            str(node.attributes.get("alt_text") or ""),
        ]).lower()
        if any(keyword in fields for keyword in ("chart", "diagram", "flow", "architecture", "screenshot", "graph", "process", "cad", "3d")):
            return False
        if self._formula_extraction_failed(node):
            return False
        decorative_terms = ("logo", "icon", "background", "watermark", "footer", "header", "separator", "divider", "decorative", "pattern")
        if any(term in fields for term in decorative_terms):
            return True
        return self._is_small_decorative_visual(node)

    def _is_small_decorative_visual(self, node: CanonicalNode) -> bool:
        """Uses area and text signals to skip tiny pictures without useful OCR."""
        area_ratio = self._visual_area_ratio(node)
        if area_ratio <= 0:
            return False
        small_limit = float(os.getenv("JLR_VLM_DECORATIVE_MAX_AREA_RATIO", "0.025"))
        return area_ratio < small_limit and not self._has_meaningful_ocr(node)

    def _visual_area_ratio(self, node: CanonicalNode) -> float:
        """Approximates visual area as a fraction of a default 16:9 PPTX slide when possible."""
        bbox = node.attributes.get("bbox") or node.provenance.bbox
        if not isinstance(bbox, dict) or not all(key in bbox for key in ("width", "height")):
            return 0.0
        try:
            width = float(bbox["width"])
            height = float(bbox["height"])
            slide_width = float(os.getenv("JLR_PPTX_SLIDE_WIDTH_EMU", "12192000"))
            slide_height = float(os.getenv("JLR_PPTX_SLIDE_HEIGHT_EMU", "6858000"))
            if width <= 0 or height <= 0 or slide_width <= 0 or slide_height <= 0:
                return 0.0
            return min((width * height) / (slide_width * slide_height), 1.0)
        except Exception:
            return 0.0

    def _vision_reason(self, document: CanonicalDocument, node: CanonicalNode) -> str:
        """Explains why Azure vision is required for this image node."""
        if node.attributes.get("vision_reason"):
            return str(node.attributes["vision_reason"])
        if node.node_type == NodeType.TABLE:
            return "Table extraction is weak or incomplete, so VLM visual enrichment is required when a table image/crop is available."
        if node.node_type == NodeType.EMBEDDED_DATASET:
            return "Chart or embedded visual dataset needs VLM enrichment when a rendered visual asset is available."
        if document.source_type in {SourceType.PPT, SourceType.PPTX}:
            return "PPTX visual is complex or on a slide with limited extracted text, so VLM transcription/description is required."
        if document.source_type == SourceType.PDF:
            return "PDF visual has limited nearby extracted text or complex visual content, so VLM transcription/description is required."
        if document.source_type == SourceType.DOCX:
            return "DOCX visual has limited locator text, so VLM transcription/description is required."
        return "Visual node requires VLM transcription/description."

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

    def _image_content_is_complex(self, node: CanonicalNode) -> bool:
        """Checks only image-owned metadata for complex visual signals."""
        fields = " ".join([
            str(node.title or ""),
            str(node.attributes.get("shape_name") or ""),
            str(node.attributes.get("image_type") or ""),
            str(node.attributes.get("classification") or ""),
            str(node.attributes.get("alt_text") or ""),
        ]).lower()
        keywords = ("chart", "diagram", "flow", "architecture", "screenshot", "figure", "graph", "process", "cad", "3d")
        if any(keyword in fields for keyword in keywords):
            return True
        if "formula" in fields or "equation" in fields or "math" in fields:
            return self._formula_extraction_failed(node)
        return False

    def _visual_content_is_complex(self, node: CanonicalNode) -> bool:
        """Identifies diagram/chart/screenshot-like visual assets for VLM enrichment."""
        fields = " ".join([
            str(node.title or ""),
            str(node.attributes.get("shape_name") or ""),
            str(node.attributes.get("image_type") or ""),
            str(node.attributes.get("classification") or ""),
            str(node.attributes.get("docling_visual_hint") or ""),
            str(node.attributes.get("vision_reason") or ""),
        ]).lower()
        keywords = ("chart", "diagram", "flow", "architecture", "screenshot", "figure", "graph", "process", "cad", "3d")
        if any(keyword in fields for keyword in keywords):
            return True
        if "formula" in fields or "equation" in fields or "math" in fields:
            return self._formula_extraction_failed(node)
        return False


    def _formula_extraction_failed(self, node: CanonicalNode) -> bool:
        """Uses formula/codeformula status before escalating a formula visual to VLM."""
        attributes = node.attributes
        if attributes.get("requires_review"):
            return True
        warning_fields = (
            attributes.get("formula_warnings"),
            attributes.get("code_formula_warnings"),
            attributes.get("warnings"),
        )
        if any(warning_fields):
            return True
        status_values = [
            attributes.get("formula_extraction_status"),
            attributes.get("code_formula_status"),
            attributes.get("codeformula_status"),
            attributes.get("docling_formula_status"),
        ]
        failed_statuses = {"failed", "error", "partial", "incomplete", "missing", "low_confidence", "requires_review"}
        if any(str(status or "").lower() in failed_statuses for status in status_values):
            return True
        confidence = attributes.get("formula_confidence") or attributes.get("code_formula_confidence")
        if isinstance(confidence, (int, float)) and float(confidence) < float(os.getenv("JLR_VLM_MIN_FORMULA_CONFIDENCE", "0.75")):
            return True
        extracted = attributes.get("formula") or attributes.get("formula_text") or attributes.get("extracted_formula")
        if ("formula" in " ".join([str(node.title or ""), str(attributes.get("classification") or ""), str(attributes.get("image_type") or ""), str(attributes.get("shape_name") or "")]).lower()
                and not normalize_text(str(extracted or ""))):
            return True
        return False

    def _table_is_weak(self, node: CanonicalNode) -> bool:
        """Marks incomplete or review-required tables as candidates for visual validation."""
        if node.attributes.get("requires_review") or node.attributes.get("table_warnings"):
            return True
        has_header = any(child.node_type == NodeType.TABLE_HEADER for child in node.children)
        has_rows = any(child.node_type == NodeType.TABLE_ROW for child in node.children)
        if not has_header or not has_rows:
            return True
        non_empty_cells = sum(
            1 for row in node.children for cell in row.children
            if cell.node_type == NodeType.TABLE_CELL and normalize_text(str(cell.text or cell.attributes.get("value") or ""))
        )
        return non_empty_cells < int(os.getenv("JLR_VLM_MIN_TABLE_CELLS", "4"))

    def _dataset_needs_vision(self, node: CanonicalNode) -> bool:
        """Marks chart-like embedded datasets for VLM if a rendered asset is later available."""
        fields = " ".join([str(node.title or ""), str(node.attributes.get("classification") or ""),
                            str(node.attributes.get("image_type") or "")]).lower()
        return any(keyword in fields for keyword in ("chart", "graph", "plot", "diagram"))

    def _describe_node(self, node: CanonicalNode, provider: VisionDescriptionProvider) -> bool:
        """Adds visual understanding metadata without changing source text/cells."""
        image_path = Path(str(node.attributes.get("saved_path") or ""))
        if not image_path.is_file():
            if node.node_type in {NodeType.TABLE, NodeType.EMBEDDED_DATASET}:
                node.attributes.setdefault("visual_asset_status", "render_required")
                node.attributes.setdefault("image_storage_status", "render_required")
                node.attributes.setdefault(
                    "image_description_warnings",
                    ["A rendered crop/screenshot is required before OCR/VLM enrichment can run for this non-image visual node."],
                )
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
            str(node.attributes.get("chart_title") or ""),
            str(node.attributes.get("vision_reason") or ""),
            str(node.attributes.get("nearby_text") or ""),
            str(node.attributes.get("ocr_text") or ""),
            self._subtree_text(node),
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
                if self._is_visual_candidate_node(node) and node.attributes.get("needs_vision_description"):
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

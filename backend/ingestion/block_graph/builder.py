"""Textract-style BlockGraph export for canonical ingestion documents.

The canonical tree remains the source of truth. This module creates a compact,
flat block graph with Textract-like Blocks, Relationships, Geometry, and Assets
for retrieval/indexing systems that prefer graph-shaped JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.ingestion.models import CanonicalDocument
from backend.ingestion.models import CanonicalNode
from backend.ingestion.models import NodeType


@dataclass(slots=True)
class _AssetRef:
    """Tracks one visual asset referenced by a block."""

    block_id: str
    path: str
    asset_type: str
    status: str


class BlockGraphBuilder:
    """Converts canonical document nodes into a flat Textract-style graph."""

    def build(self, document: CanonicalDocument) -> dict[str, Any]:
        """Builds a serializable block graph from one canonical document."""
        blocks: list[dict[str, Any]] = []
        assets: list[dict[str, Any]] = []
        seen_assets: set[tuple[str, str]] = set()
        for root in document.root_nodes:
            self._append_node(document, root, blocks, assets, seen_assets)
        return {
            "DocumentMetadata": self._metadata(document),
            "Blocks": blocks,
            "Assets": assets,
        }

    def _metadata(self, document: CanonicalDocument) -> dict[str, Any]:
        """Creates compact document-level metadata for BlockGraph consumers."""
        return {
            "DocumentId": document.document_id,
            "AgentId": document.agent_id,
            "SourceType": document.source_type.value,
            "FileName": document.file_name,
            "SourcePath": document.source_path,
            "SourceName": document.source_name,
            "Version": document.version,
            "SourceHash": document.source_hash,
            "ParserName": document.parser_info.parser_name,
            "ExtractionMode": document.parser_info.extraction_mode,
            "Title": document.metadata.title,
            "DocumentFamily": document.metadata.document_family.value,
            "PageCount": document.metadata.page_count,
            "SlideCount": document.metadata.slide_count,
            "SheetCount": document.metadata.sheet_count,
            "Quality": document.quality.model_dump(mode="json"),
            "SecurityScope": document.security_scope.model_dump(mode="json"),
        }

    def _append_node(
        self,
        document: CanonicalDocument,
        node: CanonicalNode,
        blocks: list[dict[str, Any]],
        assets: list[dict[str, Any]],
        seen_assets: set[tuple[str, str]],
    ) -> None:
        """Appends one node block and recursively appends children."""
        block = self._node_block(document, node)
        child_ids = [child.node_id for child in node.children]
        relationships = []
        if child_ids:
            relationships.append({"Type": "CHILD", "Ids": child_ids})
        asset_ids = self._append_assets(node, assets, seen_assets)
        if asset_ids:
            relationships.append({"Type": "ASSET", "Ids": asset_ids})
        if relationships:
            block["Relationships"] = relationships
        blocks.append(block)
        for child in node.children:
            self._append_node(document, child, blocks, assets, seen_assets)

    def _node_block(self, document: CanonicalDocument, node: CanonicalNode) -> dict[str, Any]:
        """Converts one canonical node to a Textract-style block."""
        block: dict[str, Any] = {
            "Id": node.node_id,
            "BlockType": self._block_type(node),
            "Confidence": node.confidence,
        }
        if node.title:
            block["Title"] = node.title
        if node.text:
            block["Text"] = node.text
        page = node.provenance.page_number or node.attributes.get("page_number")
        slide = node.provenance.slide_number or node.attributes.get("slide_number")
        if page is not None:
            block["Page"] = page
        if slide is not None:
            block["Slide"] = slide
        geometry = self._geometry(node)
        if geometry:
            block["Geometry"] = geometry
        entity_types = self._entity_types(node)
        if entity_types:
            block["EntityTypes"] = entity_types
        compact = self._compact_attributes(node)
        if compact:
            block["Attributes"] = compact
        return block

    def _block_type(self, node: CanonicalNode) -> str:
        """Maps canonical node types to BlockGraph block types."""
        mapping = {
            NodeType.DOCUMENT_ROOT: "DOCUMENT",
            NodeType.PAGE: "PAGE",
            NodeType.SLIDE: "PAGE",
            NodeType.SECTION: "SECTION",
            NodeType.TITLE_BLOCK: "LAYOUT_TITLE",
            NodeType.PARAGRAPH: "LAYOUT_TEXT",
            NodeType.TEXT_BLOCK: "LAYOUT_TEXT",
            NodeType.BULLET_BLOCK: "LAYOUT_LIST",
            NodeType.NOTE_BLOCK: "NOTE",
            NodeType.CAPTION: "CAPTION",
            NodeType.TABLE: "TABLE",
            NodeType.TABLE_HEADER: "TABLE_HEADER",
            NodeType.TABLE_ROW: "ROW",
            NodeType.TABLE_CELL: "CELL",
            NodeType.IMAGE: "IMAGE",
            NodeType.IMAGE_REGION: "IMAGE",
            NodeType.EMBEDDED_DATASET: "CHART",
            NodeType.OCR_BLOCK: "OCR_BLOCK",
            NodeType.LABEL_VALUE_BLOCK: "KEY_VALUE_SET",
            NodeType.FORM_SECTION: "FORM_SECTION",
        }
        return mapping.get(node.node_type, node.node_type.value.upper())

    def _entity_types(self, node: CanonicalNode) -> list[str]:
        """Adds optional Textract-like entity types for special blocks."""
        if node.node_type == NodeType.TABLE_CELL and node.attributes.get("is_header"):
            return ["COLUMN_HEADER"]
        if node.node_type == NodeType.TABLE:
            return ["STRUCTURED_TABLE"] if not node.attributes.get("layout_table_converted") else ["LAYOUT_TABLE"]
        return []

    def _geometry(self, node: CanonicalNode) -> dict[str, Any] | None:
        """Normalizes bbox metadata into Textract-style Geometry."""
        bbox = node.attributes.get("bbox") or node.provenance.bbox
        normalized = self._bbox_values(bbox)
        if normalized is None:
            rendered = node.attributes.get("rendered_layout")
            if isinstance(rendered, dict):
                normalized = self._bbox_values(rendered.get("bbox") or rendered.get("context_bbox"))
        if normalized is None:
            return None
        left, top, right, bottom = normalized
        bounding_box = {"Left": left, "Top": top, "Width": max(right - left, 0.0), "Height": max(bottom - top, 0.0)}
        return {
            "BoundingBox": bounding_box,
            "Polygon": [
                {"X": left, "Y": top},
                {"X": right, "Y": top},
                {"X": right, "Y": bottom},
                {"X": left, "Y": bottom},
            ],
        }

    def _bbox_values(self, bbox: Any) -> tuple[float, float, float, float] | None:
        """Accepts list and common dict bbox formats."""
        if isinstance(bbox, list | tuple) and len(bbox) == 4:
            return tuple(float(value) for value in bbox)  # type: ignore[return-value]
        if not isinstance(bbox, dict):
            return None
        if all(key in bbox for key in ("x0", "y0", "x1", "y1")):
            return float(bbox["x0"]), float(bbox["y0"]), float(bbox["x1"]), float(bbox["y1"])
        if all(key in bbox for key in ("left", "top", "right", "bottom")):
            return float(bbox["left"]), float(bbox["top"]), float(bbox["right"]), float(bbox["bottom"])
        if all(key in bbox for key in ("left", "top", "width", "height")):
            left = float(bbox["left"])
            top = float(bbox["top"])
            return left, top, left + float(bbox["width"]), top + float(bbox["height"])
        return None

    def _compact_attributes(self, node: CanonicalNode) -> dict[str, Any]:
        """Keeps useful retrieval/audit fields without copying heavy metadata."""
        keys = (
            "row_index", "column_index", "column_name", "value", "normalized_value", "value_type",
            "semantic_text", "table_name", "columns", "source_table_type", "shape_name", "shape_id",
            "media_name", "relationship_id", "nearby_text", "ocr_status", "ocr_text", "ocr_confidence",
            "vlm_gate", "needs_vision_description", "vision_reason", "image_description_status",
            "vlm_transcribed_text", "vlm_confidence", "vlm_uncertainty", "image_match", "image_storage_status",
            "rendered_layout", "extractor_role", "docling_visual_hint", "classification", "image_type",
            "image_width", "image_height", "width", "height", "image_occurrence_count", "is_decorative", "visual_object_type",
        )
        compact = {key: node.attributes[key] for key in keys if key in node.attributes and node.attributes[key] not in (None, "", [], {})}
        if node.attributes.get("image_description"):
            compact["image_description"] = node.attributes["image_description"]
        return compact

    def _append_assets(self, node: CanonicalNode, assets: list[dict[str, Any]], seen: set[tuple[str, str]]) -> list[str]:
        """Creates one compact asset record for visual paths on a node."""
        path = node.attributes.get("saved_path")
        if not path:
            return []
        asset_id = f"asset_{len(assets) + 1:06d}"
        key = (node.node_id, str(path))
        if key in seen:
            return []
        seen.add(key)
        asset = {
            "AssetId": asset_id,
            "BlockId": node.node_id,
            "AssetType": self._asset_type(node),
            "VisualObjectType": self._visual_object_type(node),
            "Path": path,
            "Status": node.attributes.get("image_description_status") or node.attributes.get("image_storage_status") or "stored",
        }
        for source_key, target_key in (
            ("image_hash", "Hash"),
            ("image_extension", "Extension"),
            ("classification", "Classification"),
            ("image_type", "ImageType"),
            ("image_width", "Width"),
            ("image_height", "Height"),
            ("width", "Width"),
            ("height", "Height"),
            ("image_occurrence_count", "OccurrenceCount"),
            ("is_decorative", "IsDecorative"),
            ("ocr_text", "OcrText"),
            ("ocr_confidence", "OcrConfidence"),
            ("image_description", "Description"),
            ("vlm_confidence", "VlmConfidence"),
        ):
            if node.attributes.get(source_key) not in (None, "", [], {}):
                asset[target_key] = node.attributes[source_key]
        assets.append(asset)
        return [asset_id]

    def _visual_object_type(self, node: CanonicalNode) -> str:
        """Classifies a visual asset for retrieval before chunking/embedding."""
        fields = " ".join(str(value or "") for value in (
            node.node_type.value,
            node.title,
            node.text,
            node.attributes.get("visual_object_type"),
            node.attributes.get("classification"),
            node.attributes.get("image_type"),
            node.attributes.get("shape_name"),
            node.attributes.get("nearby_text"),
            node.attributes.get("ocr_text"),
            node.attributes.get("vlm_transcribed_text"),
            node.attributes.get("image_description"),
            node.attributes.get("source_table_type"),
        )).lower()
        if node.attributes.get("is_decorative"):
            return "icon"
        if node.node_type == NodeType.TABLE:
            return "table_snapshot"
        if node.node_type == NodeType.EMBEDDED_DATASET:
            return "chart"
        for name, keywords in {
            "formula": ("formula", "equation", "math"),
            "chart": ("chart", "graph", "plot"),
            "architecture_diagram": ("architecture",),
            "flow_diagram": ("flow", "process"),
            "cad_3d_visual": ("cad", "3d"),
            "diagram": ("diagram", "figure"),
            "screenshot": ("screenshot",),
        }.items():
            if any(keyword in fields for keyword in keywords):
                return name
        if node.node_type in {NodeType.IMAGE, NodeType.IMAGE_REGION}:
            return "image"
        return "visual"

    def _asset_type(self, node: CanonicalNode) -> str:
        """Returns compact asset type."""
        if node.node_type == NodeType.TABLE:
            return "table_crop"
        if node.node_type == NodeType.EMBEDDED_DATASET:
            return "chart_or_visual_crop"
        if node.node_type in {NodeType.IMAGE, NodeType.IMAGE_REGION}:
            return "image"
        return "visual_asset"

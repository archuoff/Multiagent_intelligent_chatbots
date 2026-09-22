"""Visual asset registry for PDF and PowerPoint extraction metadata.

The registry does not perform OCR or vision analysis. It gives every discovered
image a stable identity and source-node connection so the Phase 2 vision path
can attach authoritative extracted content later.
"""

from __future__ import annotations

import hashlib

from backend.ingestion.enrichment.contracts import VisualAssetRecord
from backend.ingestion.models import CanonicalDocument
from backend.ingestion.models import CanonicalNode
from backend.ingestion.models import NodeType


class VisualAssetRegistry:
    """Registers image metadata while keeping it separate from factual text extraction."""

    def apply(self, document: CanonicalDocument) -> list[VisualAssetRecord]:
        """Adds direct asset metadata and a document-level visual registry."""
        records: list[VisualAssetRecord] = []
        for root in document.root_nodes:
            for node in self._walk(root):
                assets = self._node_assets(node)
                if assets:
                    node.attributes["visual_assets"] = [asset.model_dump(mode="json") for asset in assets]
                    records.extend(assets)
        if document.root_nodes:
            document.root_nodes[0].attributes["visual_asset_registry"] = [asset.model_dump(mode="json") for asset in records]
        return records

    def _node_assets(self, node: CanonicalNode) -> list[VisualAssetRecord]:
        """Normalizes explicit image nodes and page/slide image metadata lists."""
        raw_assets = [node.attributes] if node.node_type == NodeType.IMAGE else node.attributes.get("images", [])
        if not isinstance(raw_assets, list):
            raw_assets = []
        assets: list[VisualAssetRecord] = []
        for index, raw in enumerate(raw_assets, start=1):
            if not isinstance(raw, dict):
                continue
            locator = str(raw.get("saved_path") or raw.get("xref") or raw.get("shape_name") or "") or None
            identity = f"{node.node_id}|{index}|{locator or ''}"
            assets.append(VisualAssetRecord(asset_id=f"asset_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}",
                source_node_id=node.node_id, parent_node_id=node.provenance.parent_node_id,
                asset_type=str(raw.get("image_type") or raw.get("extension") or "image"),
                status=self._status(raw), locator=locator, metadata=dict(raw)))
        return assets

    def _status(self, raw: dict) -> str:
        """Reflects the richest image processing state available on the node."""
        description_status = raw.get("image_description_status")
        if description_status in {"success", "no_description", "failed", "skipped"}:
            return f"description_{description_status}"
        ocr_status = raw.get("ocr_status")
        if ocr_status in {"success", "no_text", "failed"}:
            return f"ocr_{ocr_status}"
        if raw.get("saved_path"):
            return "stored"
        return "metadata_only"

    def _walk(self, node):
        """Yields the canonical tree while retaining original page and slide ordering."""
        yield node
        for child in node.children:
            yield from self._walk(child)

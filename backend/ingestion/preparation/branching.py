"""Branching helpers for canonical ingestion outputs.

This file walks canonical nodes and decides which parts of a document should
feed SQL materialization, semantic chunking, visual metadata, and the catalog.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.ingestion.models import CanonicalDocument
from backend.ingestion.models import CanonicalNode
from backend.ingestion.models import NodeType


@dataclass(slots=True)
class BranchingOutputs:
    """Groups canonical nodes by the downstream path they should feed."""

    sql_nodes: list[CanonicalNode]
    semantic_nodes: list[CanonicalNode]
    catalog_nodes: list[CanonicalNode]
    visual_nodes: list[CanonicalNode]


SQL_NODE_TYPES = {
    NodeType.TABLE,
    NodeType.TABLE_HEADER,
    NodeType.TABLE_ROW,
    NodeType.TABLE_CELL,
    NodeType.EMBEDDED_DATASET,
}

SEMANTIC_NODE_TYPES = {
    NodeType.PARAGRAPH,
    NodeType.TEXT_BLOCK,
    NodeType.BULLET_BLOCK,
    NodeType.TITLE_BLOCK,
    NodeType.NOTE_BLOCK,
    NodeType.GUIDELINE_BLOCK,
    NodeType.REFERENCE_BLOCK,
    NodeType.MEASUREMENT_BLOCK,
    NodeType.CAPTION,
    NodeType.TABLE_ROW,
}

CATALOG_NODE_TYPES = {
    NodeType.CHAPTER,
    NodeType.SECTION,
    NodeType.SUBSECTION,
    NodeType.PAGE,
    NodeType.SLIDE,
    NodeType.SHEET,
    NodeType.REGION,
    NodeType.TABLE,
    NodeType.EMBEDDED_DATASET,
    NodeType.IMAGE,
}

VISUAL_NODE_TYPES = {
    NodeType.IMAGE,
    NodeType.IMAGE_REGION,
    NodeType.CALLOUT_LABEL,
    NodeType.OCR_BLOCK,
    NodeType.CAPTION,
}


# This function walks a canonical document and groups nodes by downstream processing path.
def branch_document(document: CanonicalDocument) -> BranchingOutputs:
    sql_nodes: list[CanonicalNode] = []
    semantic_nodes: list[CanonicalNode] = []
    catalog_nodes: list[CanonicalNode] = []
    visual_nodes: list[CanonicalNode] = []

    for root_node in document.root_nodes:
        for node in walk_nodes(root_node):
            if node.node_type in SQL_NODE_TYPES:
                sql_nodes.append(node)
            if node.node_type in SEMANTIC_NODE_TYPES:
                semantic_nodes.append(node)
            if node.node_type in CATALOG_NODE_TYPES:
                catalog_nodes.append(node)
            if node.node_type in VISUAL_NODE_TYPES:
                visual_nodes.append(node)

    return BranchingOutputs(
        sql_nodes=sql_nodes,
        semantic_nodes=semantic_nodes,
        catalog_nodes=catalog_nodes,
        visual_nodes=visual_nodes,
    )


# This function yields a node and all descendants in depth-first order for branching or indexing.
def walk_nodes(node: CanonicalNode) -> list[CanonicalNode]:
    nodes = [node]
    for child in node.children:
        nodes.extend(walk_nodes(child))
    return nodes

"""Quality validation for canonical documents produced by the ingestion layer.

This module checks structural and provenance issues before canonical output is
persisted or sent to future chunking and indexing stages.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.ingestion.models import CanonicalDocument
from backend.ingestion.models import CanonicalNode
from backend.ingestion.models import NodeType


@dataclass(frozen=True, slots=True)
class QualityIssue:
    """Describes one deterministic quality finding for a canonical document."""

    severity: str
    code: str
    message: str
    node_id: str | None = None


@dataclass(frozen=True, slots=True)
class QualityReport:
    """Groups validation issues and exposes a simple pass/fail result."""

    issues: list[QualityIssue]

    @property
    def is_valid(self) -> bool:
        """Returns whether the report contains no blocking errors."""
        return not any(issue.severity == "error" for issue in self.issues)


class CanonicalQualityValidator:
    """Runs deterministic quality checks without changing the canonical document."""

    _DUPLICATE_TEXT_NODE_TYPES = {
        NodeType.PARAGRAPH,
        NodeType.TEXT_BLOCK,
        NodeType.BULLET_BLOCK,
        NodeType.TITLE_BLOCK,
        NodeType.NOTE_BLOCK,
        NodeType.GUIDELINE_BLOCK,
        NodeType.REFERENCE_BLOCK,
        NodeType.MEASUREMENT_BLOCK,
        NodeType.LABEL_VALUE_BLOCK,
        NodeType.STATUS_BLOCK,
        NodeType.CAPTION,
    }

    # Content node types counted for the document-wide low-confidence ratio
    # -- everything a reader would actually notice missing, narrative and
    # tabular alike, including the low-confidence blocks themselves.
    _CONTENT_NODE_TYPES = _DUPLICATE_TEXT_NODE_TYPES | {
        NodeType.TABLE_ROW, NodeType.TABLE_CELL, NodeType.LOW_CONFIDENCE_BLOCK,
    }

    # A single unresolved item anywhere no longer blocks the whole document
    # (that was the old, overly blunt behavior) -- only a *broad* failure
    # does, measured as a fraction of content nodes that came back
    # low-confidence. The absolute floor keeps a tiny document (a handful of
    # nodes) from tripping the ratio over one unlucky value.
    LOW_CONFIDENCE_DOCUMENT_RATIO = 0.25
    LOW_CONFIDENCE_DOCUMENT_FLOOR = 3

    # This function validates a canonical document before it enters persistence or indexing.
    def validate(self, document: CanonicalDocument) -> QualityReport:
        issues: list[QualityIssue] = []
        nodes = [node for root in document.root_nodes for node in self._walk_nodes(root)]
        issues.extend(self._validate_node_ids(nodes))
        issues.extend(self._validate_provenance(document, nodes))
        issues.extend(self._validate_content(nodes))
        issues.extend(self._validate_reconciliation(nodes))
        issues.extend(self._validate_tables(nodes))
        if not any((node.text or "").strip() or node.attributes.get("semantic_text") or node.attributes.get("header_rows") for node in nodes):
            issues.append(QualityIssue("error", "no_extracted_content", "No readable source content was extracted; review or OCR is required."))
        if not document.root_nodes:
            issues.append(QualityIssue("error", "missing_root", "Document has no root nodes."))
        return QualityReport(issues=issues)

    # This function walks a canonical tree in depth-first order.
    def _walk_nodes(self, node: CanonicalNode) -> list[CanonicalNode]:
        return [node, *[descendant for child in node.children for descendant in self._walk_nodes(child)]]

    # This function detects duplicate canonical node identifiers.
    def _validate_node_ids(self, nodes: list[CanonicalNode]) -> list[QualityIssue]:
        seen: set[str] = set()
        issues: list[QualityIssue] = []
        for node in nodes:
            if node.node_id in seen:
                issues.append(QualityIssue("error", "duplicate_node_id", "Node ID is duplicated.", node.node_id))
            seen.add(node.node_id)
        return issues

    # This function judges reconciliation confidence per node and, separately, for the document as a whole.
    def _validate_reconciliation(self, nodes: list[CanonicalNode]) -> list[QualityIssue]:
        """Replaces the old "any conflict anywhere blocks everything" rule
        with a confidence-tier-aware judgment: a low-confidence node that's
        already excluded from retrieval is fine on its own (the exclusion
        mechanism worked); the document as a whole only gets hard-blocked
        when low-confidence content is a *broad* problem, or when an
        unresolved numeric/technical conflict couldn't be safely excluded --
        a single corrupted safety-critical value must never slip through
        just because the rest of a large document is fine.
        """
        issues: list[QualityIssue] = []
        low_confidence_nodes: list[CanonicalNode] = []
        for node in nodes:
            tier = node.attributes.get("extraction_confidence")
            reconciliation = node.attributes.get("reconciliation")
            events = reconciliation if isinstance(reconciliation, list) else [reconciliation]
            legacy_conflict = any(
                isinstance(item, dict) and item.get("decision") == "conflict" and "confidence_tier" not in item
                for item in events
            )
            if tier != "low" and not legacy_conflict:
                continue
            if legacy_conflict and tier != "low":
                # Reconciliation metadata from before confidence tiers existed
                # (e.g. a persisted document re-loaded without re-running the
                # merger) -- treated as low-confidence for safety, surfaced
                # as a warning rather than the old hard error so re-loading
                # existing Master JSON doesn't newly start rejecting it outright.
                issues.append(QualityIssue("warning", "extraction_conflict",
                    "Legacy reconciliation conflict with no confidence tier; treated as low-confidence.", node.node_id))
                low_confidence_nodes.append(node)
                continue
            low_confidence_nodes.append(node)
            if node.attributes.get("retrieval_allowed") is False:
                issues.append(QualityIssue("warning", "low_confidence_excluded",
                    "Unverified extracted content was excluded from retrieval and retained for review.", node.node_id))
            else:
                issues.append(QualityIssue("error", "low_confidence_retrievable",
                    "Unverified extracted content could not be excluded at node level and remains retrievable.", node.node_id))
            if self._has_unresolved_technical_conflict(events) and node.attributes.get("retrieval_allowed") is not False:
                issues.append(QualityIssue("error", "unresolved_numeric_conflict",
                    "A numeric/technical value conflict could not be resolved or excluded from retrieval.", node.node_id))

        content_nodes = [node for node in nodes if node.node_type in self._CONTENT_NODE_TYPES]
        if content_nodes and len(low_confidence_nodes) >= self.LOW_CONFIDENCE_DOCUMENT_FLOOR:
            ratio = len(low_confidence_nodes) / len(content_nodes)
            if ratio >= self.LOW_CONFIDENCE_DOCUMENT_RATIO:
                issues.append(QualityIssue("error", "broad_extraction_failure",
                    f"{len(low_confidence_nodes)} of {len(content_nodes)} content nodes are unverified; "
                    "the document needs review as a whole."))
        return issues

    def _has_unresolved_technical_conflict(self, events: list) -> bool:
        """True when a reconciliation event carries differing numeric/technical
        values that were never actually resolved -- the one case that must
        escalate regardless of how small a fraction of the document it is."""
        for item in events:
            if not isinstance(item, dict):
                continue
            if item.get("decision") != "conflict":
                continue
            if item.get("primary_technical_values") or item.get("fallback_technical_values"):
                return True
        return False

    # This function ensures every node points back to the document being validated.
    def _validate_provenance(self, document: CanonicalDocument, nodes: list[CanonicalNode]) -> list[QualityIssue]:
        return [
            QualityIssue("error", "provenance_document_mismatch", "Node provenance document ID does not match the envelope.", node.node_id)
            for node in nodes
            if node.provenance.document_id != document.document_id
        ]

    # This function flags empty containers, duplicated text, and low-confidence content.
    def _validate_content(self, nodes: list[CanonicalNode]) -> list[QualityIssue]:
        issues: list[QualityIssue] = []
        seen_text: set[tuple[NodeType, str, str | None]] = set()
        for node in nodes:
            if node.node_type in {NodeType.PAGE, NodeType.SLIDE} and not node.children:
                issues.append(QualityIssue("warning", "empty_container", "Page or slide has no extracted child content.", node.node_id))
            if node.text and node.node_type in self._DUPLICATE_TEXT_NODE_TYPES:
                key = (node.node_type, node.text.strip().casefold(), node.provenance.parent_node_id)
                if key in seen_text:
                    issues.append(QualityIssue("warning", "duplicate_content", "Duplicate canonical text detected.", node.node_id))
                seen_text.add(key)
            if node.confidence < 0.5:
                issues.append(QualityIssue("warning", "low_confidence", "Node confidence is below 0.5.", node.node_id))
        return issues

    # This function flags tables that cannot support structured retrieval safely.
    def _validate_tables(self, nodes: list[CanonicalNode]) -> list[QualityIssue]:
        issues: list[QualityIssue] = []
        for node in nodes:
            if node.node_type != NodeType.TABLE:
                continue
            if node.attributes.get("requires_review"):
                issues.append(QualityIssue("warning", "unreadable_table", "Table structure could not be mapped safely; retained source payload requires review.", node.node_id))
            for warning in node.attributes.get("table_warnings", []):
                issues.append(QualityIssue("warning", "table_export", warning, node.node_id))
            if node.attributes.get("representation") == "structured_cells":
                continue
            has_header = any(child.node_type == NodeType.TABLE_HEADER for child in node.children)
            has_rows = any(child.node_type == NodeType.TABLE_ROW for child in node.children)
            if not has_header or not has_rows:
                issues.append(QualityIssue("warning", "incomplete_table", "Table is missing a header or data rows.", node.node_id))
        return issues

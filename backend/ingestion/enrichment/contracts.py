"""Governance records added to canonical documents before chunking and retrieval.

These structures carry field-use decisions, source reference status, and visual
asset metadata. They are serialized into node attributes so existing Master JSON
remains backward compatible while future answer generation can enforce policy.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class FieldUse(str, Enum):
    """Describes how a structured source field may participate in the system."""

    STANDARD = "standard"
    REFERENCE_LINK = "reference_link"
    INTERNAL_ONLY = "internal_only"


class FieldPolicy(BaseModel):
    """Stores answer-display and retrieval permissions for one source column."""

    field_name: str
    use: FieldUse
    retrieval_allowed: bool
    answer_visible: bool
    reason: str


class ReferenceRecord(BaseModel):
    """Records a normalized source reference without making a network request."""

    reference_id: str
    source_node_id: str
    raw_value: str
    normalized_uri: str | None = None
    reference_type: str
    status: str
    answer_eligible: bool
    file_extension: str | None = None


class VisualAssetRecord(BaseModel):
    """Records visual source metadata while image understanding remains deferred."""

    asset_id: str
    source_node_id: str
    parent_node_id: str | None = None
    asset_type: str
    status: str
    locator: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)

"""Preparation of canonical nodes for future downstream consumers.

``branching.py`` groups nodes from ``models.py`` for SQL, semantic, catalog,
and visual processing. ``service.py`` calls it after parsing; it does not yet
create chunks, embeddings or database records.
"""

from backend.ingestion.preparation.branching import BranchingOutputs, branch_document

__all__ = ["BranchingOutputs", "branch_document"]

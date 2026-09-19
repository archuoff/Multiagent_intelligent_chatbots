"""Abstract parser contracts for the canonical ingestion pipeline.

This file defines the shared source object, parser context, and parser registry
used to route different file types into the same canonical document contract.
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from dataclasses import dataclass
from pathlib import Path

from backend.ingestion.models import CanonicalDocument
from backend.ingestion.models import SourceType


@dataclass(slots=True)
class IngestionSource:
    """Carries the raw source identity and routing metadata for one ingestion job."""

    agent_id: str
    source_type: SourceType
    file_path: Path
    version: str = "v1"
    document_id: str | None = None


@dataclass(slots=True)
class ParserContext:
    """Carries parser configuration and security metadata shared across adapters."""

    classification: str = "internal"
    allowed_groups: tuple[str, ...] = ()
    parser_version: str = "1.0.0"


class SourceParser(ABC):
    """Defines the interface every source-specific parser must implement."""

    source_types: tuple[SourceType, ...]

    # This function advertises whether the parser can handle a given source type.
    def supports(self, source_type: SourceType) -> bool:
        return source_type in self.source_types

    # This function converts one raw source into the shared canonical document contract.
    @abstractmethod
    def parse(self, source: IngestionSource, context: ParserContext) -> CanonicalDocument:
        raise NotImplementedError


class ParserRegistry:
    """Stores available parsers and resolves the correct adapter for each source."""

    def __init__(self) -> None:
        self._parsers: list[SourceParser] = []

    # This function registers a parser implementation for later source-type routing.
    def register(self, parser: SourceParser) -> None:
        self._parsers.append(parser)

    # This function returns the parser responsible for the given source type.
    def resolve(self, source_type: SourceType) -> SourceParser:
        for parser in self._parsers:
            if parser.supports(source_type):
                return parser
        raise ValueError(f"No parser registered for source type '{source_type.value}'")

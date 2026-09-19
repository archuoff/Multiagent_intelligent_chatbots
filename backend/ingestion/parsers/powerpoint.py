"""Source parser that assembles canonical PowerPoint documents.

This file uses Docling as the primary extractor for PPT/PPTX files and falls
back to python-pptx when Docling is unavailable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from backend.ingestion.extraction.contracts import ExtractionResult
from backend.ingestion.extraction.docling import DoclingContentAdapter
from backend.ingestion.extraction.fallbacks import PowerPointFallbackExtractor
from backend.ingestion.parsers.document import DoclingCanonicalBuilder
from backend.ingestion.models import CanonicalDocument
from backend.ingestion.models import CanonicalNode
from backend.ingestion.models import DocumentFamily
from backend.ingestion.models import DocumentMetadata
from backend.ingestion.models import NodeType
from backend.ingestion.models import ParserInfo
from backend.ingestion.models import Provenance
from backend.ingestion.models import QualityMetadata
from backend.ingestion.models import SecurityScope
from backend.ingestion.models import SourceType
from backend.ingestion.parsers.base import IngestionSource
from backend.ingestion.parsers.base import ParserContext
from backend.ingestion.parsers.base import SourceParser
from backend.ingestion.utils import hash_file
from backend.ingestion.utils import make_id
from backend.ingestion.utils import normalize_text


class PowerPointParser(SourceParser):
    """Parses PowerPoint files into slide-oriented canonical nodes."""

    source_types = (SourceType.PPT, SourceType.PPTX)

    def __init__(self) -> None:
        self._docling = DoclingContentAdapter()
        self._fallback = PowerPointFallbackExtractor()
        self._builder = DoclingCanonicalBuilder()

    # This function loads a PowerPoint file and converts each slide into canonical content nodes.
    def parse(self, source: IngestionSource, context: ParserContext) -> CanonicalDocument:
        document_id = source.document_id or make_id("doc")
        root_node_id = make_id("document")
        docling_used = False
        parser_name = "ppt_python_pptx_fallback_parser"
        extraction_mode = "fallback_library"
        processing_details: dict[str, Any] = {}
        requires_review = False

        if self._docling.is_available():
            try:
                extracted = self._docling.extract(source.file_path)
                native = self._fallback.extract(source.file_path)
                if extracted.pages and {p.number for p in extracted.pages} == {p.number for p in native.pages}:
                    from backend.ingestion.extraction.merger import ExtractionResultMerger
                    extracted = ExtractionResultMerger().merge(extracted, native)
                else:
                    extracted = native
                slide_nodes = self._build_extracted_slides(document_id, root_node_id, source, extracted)
                warnings = list(extracted.warnings)
                processing_details = extracted.processing_details
                requires_review = extracted.requires_review
                docling_used = True
                parser_name = "ppt_docling_parser"
                extraction_mode = extracted.extraction_mode if extracted is not native else "fallback_library"
                if extracted is native:
                    parser_name = "ppt_python_pptx_recovery_parser"
            except Exception as error:
                fallback = self._fallback.extract(source.file_path)
                slide_nodes = self._build_extracted_slides(document_id, root_node_id, source, fallback)
                warnings = [f"Docling PPTX extraction failed; python-pptx fallback used: {type(error).__name__}", *fallback.warnings]
        else:
            fallback = self._fallback.extract(source.file_path)
            slide_nodes = self._build_extracted_slides(document_id, root_node_id, source, fallback)
            warnings = fallback.warnings

        return CanonicalDocument(
            document_id=document_id,
            agent_id=source.agent_id,
            source_type=source.source_type,
            file_name=source.file_path.name,
            source_path=str(source.file_path),
            source_name=source.file_path.stem,
            version=source.version,
            ingested_at=datetime.utcnow(),
            source_hash=hash_file(source.file_path),
            security_scope=SecurityScope(
                classification=context.classification,
                allowed_groups=list(context.allowed_groups),
            ),
            parser_info=ParserInfo(
                parser_name=parser_name,
                parser_version=context.parser_version,
                extraction_mode=extraction_mode,
                processing_details=processing_details,
            ),
            metadata=DocumentMetadata(
                title=source.file_path.stem,
                document_family=DocumentFamily.SLIDE_BASED,
                slide_count=len(slide_nodes) or None,
            ),
            root_nodes=[
                CanonicalNode(
                    node_id=root_node_id,
                    node_type=NodeType.DOCUMENT_ROOT,
                    title=source.file_path.stem,
                    children=slide_nodes,
                    attributes={"slide_count": len(slide_nodes), "docling_enabled": docling_used},
                    provenance=Provenance(
                        document_id=document_id,
                        source_type=source.source_type,
                        version=source.version,
                    ),
                    confidence=0.97,
                )
            ],
            quality=QualityMetadata(
                overall_confidence=0.88 if not warnings else 0.8,
                warnings=warnings,
                errors=(["[reconciliation_conflict] Primary and fallback extraction disagree; review retained evidence before chunking."]
                        if processing_details.get("reconciliation", {}).get("summary", {}).get("conflict")
                        else ["[incomplete_extraction] Docling did not complete successfully; review the preserved output before chunking."])
                if requires_review else [],
            ),
        )

    # This function builds slide nodes from Docling exported structure first and markdown second.
    def _build_docling_slides(
        self,
        document_id: str,
        root_node_id: str,
        source: IngestionSource,
        extracted: dict[str, Any],
    ) -> list[CanonicalNode]:
        slide_sections = self._builder._find_page_sections(extracted.exported)
        if not slide_sections:
            slide_sections = [
                {"page_number": index, "text": text}
                for index, text in enumerate(self._builder._split_markdown_sections(extracted.markdown), start=1)
            ]

        slide_nodes: list[CanonicalNode] = []
        for section in slide_sections:
            slide_number = section["page_number"]
            slide_text = section["text"]
            slide_node_id = make_id("slide")
            children = self._builder._text_to_nodes(
                document_id=document_id,
                source_type=source.source_type,
                version=source.version,
                parent_node_id=slide_node_id,
                location_key="slide_number",
                location_value=slide_number,
                text=slide_text,
            )
            slide_nodes.append(
                CanonicalNode(
                    node_id=slide_node_id,
                    node_type=NodeType.SLIDE,
                    title=self._builder._infer_title(slide_text, f"Slide {slide_number}"),
                    children=children,
                    attributes={"slide_number": slide_number, "docling_enabled": True},
                    provenance=Provenance(
                        document_id=document_id,
                        source_type=source.source_type,
                        version=source.version,
                        parent_node_id=root_node_id,
                        slide_number=slide_number,
                    ),
                    confidence=0.9 if children else 0.5,
                )
            )
        return slide_nodes

    # This function maps shared fallback slide extraction results into canonical nodes.
    def _build_extracted_slides(
        self,
        document_id: str,
        root_node_id: str,
        source: IngestionSource,
        extraction: ExtractionResult,
    ) -> list[CanonicalNode]:
        slide_nodes: list[CanonicalNode] = []
        for extracted_slide in extraction.pages:
            slide_node_id = make_id("slide")
            children: list[CanonicalNode] = []
            title = None
            for block in extracted_slide.text_blocks:
                text = normalize_text(str(block.get("text", "")))
                if not text:
                    continue
                is_title = bool(block.get("is_title")) or block.get("type") == "heading"
                is_list = block.get("type") == "list_item" or ("type" not in block and block.get("level", 0) > 0)
                node_type = NodeType.TITLE_BLOCK if is_title else NodeType.BULLET_BLOCK if is_list else NodeType.PARAGRAPH
                if is_title and title is None:
                    title = text
                children.append(CanonicalNode(
                    node_id=make_id("text"), node_type=node_type, title=text if is_title else None, text=text,
                    attributes={"level": block.get("level", 0)},
                    provenance=Provenance(document_id=document_id, source_type=source.source_type, version=source.version, parent_node_id=slide_node_id, slide_number=extracted_slide.number),
                    confidence=0.88,
                ))
            children.extend(
                self._builder.build_matrix_table_node(document_id=document_id, source_type=source.source_type, version=source.version, parent_node_id=slide_node_id, table_index=index, rows=rows, slide_number=extracted_slide.number)
                for index, rows in enumerate(extracted_slide.tables, start=1) if rows
            )
            children.extend(CanonicalNode(
                node_id=make_id("image"), node_type=NodeType.IMAGE, attributes=image,
                provenance=Provenance(document_id=document_id, source_type=source.source_type, version=source.version, parent_node_id=slide_node_id, slide_number=extracted_slide.number), confidence=0.8,
            ) for image in extracted_slide.images)
            if extracted_slide.notes:
                children.append(CanonicalNode(
                    node_id=make_id("note"), node_type=NodeType.NOTE_BLOCK, text=extracted_slide.notes,
                    provenance=Provenance(document_id=document_id, source_type=source.source_type, version=source.version, parent_node_id=slide_node_id, slide_number=extracted_slide.number), confidence=0.82,
                ))
            slide_nodes.append(CanonicalNode(
                node_id=slide_node_id, node_type=NodeType.SLIDE, title=title or f"Slide {extracted_slide.number}", children=children,
                attributes={"slide_number": extracted_slide.number, "image_count": len(extracted_slide.images),
                            "reconciliation": extracted_slide.reconciliation},
                provenance=Provenance(document_id=document_id, source_type=source.source_type, version=source.version, parent_node_id=root_node_id, slide_number=extracted_slide.number),
                confidence=0.91 if children else 0.55,
            ))
        return slide_nodes

    # This function collects warning signals from Docling slide extraction.
    def _collect_docling_warnings(self, exported: dict[str, Any], markdown: str) -> list[str]:
        warnings: list[str] = []
        if not self._builder._find_page_sections(exported) and not normalize_text(markdown):
            warnings.append("No slide-like structure detected in Docling presentation output")
        return warnings

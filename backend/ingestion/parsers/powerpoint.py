"""Source parser that assembles canonical PowerPoint documents.

This file uses python-pptx as the native structural source of truth for
PPT/PPTX files and uses Docling as semantic enrichment when available.
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
        parser_name = "ppt_python_pptx_native_parser"
        extraction_mode = "native_structure"
        processing_details: dict[str, Any] = {}
        requires_review = False

        native = self._fallback.extract(source.file_path)
        extracted = native
        warnings = list(native.warnings)
        if self._docling.is_available():
            try:
                semantic = self._docling.extract(source.file_path)
                extracted = self._enrich_native_extraction(native, semantic)
                warnings = [*warnings, *semantic.warnings]
                processing_details = {
                    **semantic.processing_details,
                    "native_extractor": native.extractor_name,
                    "semantic_extractor": semantic.extractor_name,
                    "pptx_reconciliation": "native_structure_with_docling_semantics",
                }
                requires_review = semantic.requires_review
                docling_used = True
                parser_name = "ppt_native_docling_semantic_parser"
                extraction_mode = "native_structure_semantic_enriched"
            except Exception as error:
                warnings = [f"Docling PPTX semantic enrichment failed; native python-pptx extraction used: {type(error).__name__}", *warnings]
        slide_nodes = self._build_extracted_slides(document_id, root_node_id, source, extracted)

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
                self._table_node(document_id, source, slide_node_id, extracted_slide.number, index, table)
                for index, table in enumerate(extracted_slide.tables, start=1) if table
            )
            slide_text = "\n".join(str(child.text or child.title or "") for child in children if child.node_type != NodeType.IMAGE)
            children.extend(
                self._image_node(document_id, source, slide_node_id, extracted_slide.number, index, image, slide_text)
                for index, image in enumerate(extracted_slide.images, start=1)
            )
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

    def _enrich_native_extraction(self, native: ExtractionResult, semantic: ExtractionResult) -> ExtractionResult:
        """Keeps native PPTX objects authoritative while adding Docling semantic evidence."""
        semantic_pages = {page.number: page for page in semantic.pages}
        for page in native.pages:
            semantic_page = semantic_pages.get(page.number)
            if semantic_page is None:
                continue
            page.reconciliation.append({
                "type": "semantic_enrichment",
                "native_text_blocks": len(page.text_blocks),
                "docling_text_blocks": len(semantic_page.text_blocks),
                "native_tables": len(page.tables),
                "docling_tables": len(semantic_page.tables),
                "native_images": len(page.images),
                "docling_images": len(semantic_page.images),
            })
            if not page.text_blocks and semantic_page.text_blocks:
                page.text_blocks = [{**block, "extractor_role": "docling_semantic_recovery"} for block in semantic_page.text_blocks]
                page.text = "\n".join(normalize_text(str(block.get("text") or "")) or "" for block in page.text_blocks)
            if not page.tables and semantic_page.tables:
                page.tables = [self._mark_docling_table(table) for table in semantic_page.tables]
            if not page.images and semantic_page.images:
                page.images = [{**image, "extractor_role": "docling_semantic_recovery"} for image in semantic_page.images]
            for image in page.images:
                image.setdefault("docling_image_count_on_slide", len(semantic_page.images))
                if semantic_page.images:
                    image.setdefault("docling_visual_hint", True)
        return ExtractionResult(
            extractor_name="python-pptx+docling",
            extraction_mode="native_structure_semantic_enriched",
            pages=native.pages,
            markdown=semantic.markdown,
            exported=semantic.exported,
            blocks=[*native.blocks, *semantic.blocks],
            tables=[table for page in native.pages for table in page.tables],
            images=[image for page in native.pages for image in page.images],
            warnings=[*native.warnings, *semantic.warnings],
            requires_review=semantic.requires_review,
            processing_details={**semantic.processing_details, "base_extractor": native.extractor_name},
        )

    def _mark_docling_table(self, table: Any) -> Any:
        """Labels Docling tables used only when no native table exists on a slide."""
        if isinstance(table, dict):
            return {**table, "extractor_role": "docling_semantic_recovery"}
        return table

    def _table_node(self, document_id: str, source: IngestionSource, slide_node_id: str, slide_number: int,
                    table_index: int, table: Any) -> CanonicalNode:
        """Builds a table node while preserving native PPTX shape metadata."""
        if isinstance(table, dict) and "rows" in table:
            node = self._builder.build_matrix_table_node(
                document_id=document_id, source_type=source.source_type, version=source.version,
                parent_node_id=slide_node_id, table_index=table_index, rows=table["rows"], slide_number=slide_number,
            )
            node.attributes.update(table.get("metadata", {}))
            node.attributes["source_table_type"] = "native_pptx_table"
            return node
        return self._builder.build_matrix_table_node(
            document_id=document_id, source_type=source.source_type, version=source.version,
            parent_node_id=slide_node_id, table_index=table_index, rows=table, slide_number=slide_number,
        )

    def _image_node(self, document_id: str, source: IngestionSource, slide_node_id: str, slide_number: int,
                    image_index: int, image: dict[str, Any], slide_text: str) -> CanonicalNode:
        """Builds one canonical image-like node under the source slide with local context."""
        attributes = dict(image)
        attributes.update({
            "slide_number": slide_number,
            "image_index": image_index,
            "nearby_text": normalize_text("\n".join([slide_text, str(attributes.get("chart_title") or "")])) or "",
            "image_storage_status": attributes.get("image_storage_status", "metadata_only"),
        })
        title = normalize_text(str(attributes.get("chart_title") or attributes.get("shape_name") or "")) or f"Image {image_index}"
        node_type = NodeType.EMBEDDED_DATASET if attributes.get("image_type") == "chart" else NodeType.IMAGE
        node_prefix = "chart" if node_type == NodeType.EMBEDDED_DATASET else "image"
        return CanonicalNode(
            node_id=make_id(node_prefix), node_type=node_type, title=title,
            text=normalize_text(str(attributes.get("description") or attributes.get("caption") or attributes.get("chart_title") or "")),
            attributes=attributes,
            provenance=Provenance(document_id=document_id, source_type=source.source_type, version=source.version,
                                  parent_node_id=slide_node_id, slide_number=slide_number),
            confidence=0.84 if attributes.get("extractor_role") == "native_structure" else 0.72,
        )

    # This function collects warning signals from Docling slide extraction.
    def _collect_docling_warnings(self, exported: dict[str, Any], markdown: str) -> list[str]:
        warnings: list[str] = []
        if not self._builder._find_page_sections(exported) and not normalize_text(markdown):
            warnings.append("No slide-like structure detected in Docling presentation output")
        return warnings

from pathlib import Path
import logging
import io
import json
import re
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from PIL import Image

from docling.datamodel.base_models import ConversionStatus, InputFormat
from docling.datamodel.pipeline_options import ThreadedPdfPipelineOptions, TableStructureOptions, TableFormerMode
from docling.datamodel.accelerator_options import AcceleratorOptions, AcceleratorDevice
from docling.document_converter import DocumentConverter, PdfFormatOption
from docling.pipeline.threaded_standard_pdf_pipeline import ThreadedStandardPdfPipeline
from docling_core.types.doc import DocItemLabel, TableItem, PictureItem, TextItem
from docling.datamodel.pipeline_options import (
    ThreadedPdfPipelineOptions, TableStructureOptions, TableFormerMode,
    LayoutObjectDetectionOptions, CodeFormulaVlmOptions,
)

import pymupdf
from openpyxl import load_workbook
from pptx import Presentation
from docx import Document

from .neuroverse import call_vlm, call_llm

logger = logging.getLogger(__name__)


def apply_vlm_response_to_block(block, response):
    """
    Apply a VLM response to a block based on its type.
    Handles tables, formulas, and images with type-specific response parsing.

    Args:
        block: Block dict to update (mutated in place)
        response: VLM response dict, or None if the VLM call failed
    """
    if response is None:
        block["needs_review"] = True
        return

    block_type = block.get("type")

    if block_type == "table":
        if response.get("data"):
            block["content"] = normalize_table(response["data"])
            block["vlm_verified"] = True
        else:
            block["needs_review"] = True

    elif block_type == "formula":
        if response.get("formula"):
            block["content"] = response["formula"]
            block["explanation"] = response.get("explanation")
            block["variables"] = response.get("variables", [])
            block["use_case"] = response.get("use_case")
            block["vlm_verified"] = True
        else:
            block["needs_review"] = True

    elif block_type == "image":
        if response:
            block["description"] = response.get("description")
            block["classification"] = response.get("classification")
            block["extracted_text"] = response.get("extracted_text")


def run_parallel_vlm_verification(pending_jobs, settings, max_workers=8):
    """
    Run VLM verification for multiple blocks in parallel using a thread pool.

    Args:
        pending_jobs: List of (block, vlm_job_dict) tuples where vlm_job_dict has keys: image_path, prompt
        settings: Config dict from load_extraction_config()
        max_workers: Number of concurrent threads (default 8)
    """
    if not pending_jobs:
        return

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_block = {
            executor.submit(
                call_vlm,
                job["image_path"],
                job["prompt"],
                settings,
                label=f"{block.get('type')}:{block.get('id')}:p{block.get('page')}",
                max_tokens=job.get("max_tokens"),
            ): block
            for block, job in pending_jobs
        }

        for future in as_completed(future_to_block):
            block = future_to_block[future]
            try:
                response = future.result()
            except Exception as e:
                label = f"{block.get('type')}:{block.get('id')}:p{block.get('page')}"
                logger.error(f"[{label}] VLM call raised exception: {e}")
                response = None
            apply_vlm_response_to_block(block, response)


def detect_file_type(file_path):
    """
    Detect the file type from its extension.

    Args:
        file_path: Path to the file (string or Path object).

    Returns:
        One of: "pdf", "docx", "pptx", "xlsx".

    Raises:
        ValueError: If the file extension is not supported.
    """
    path = Path(file_path)
    ext = path.suffix.lower()

    type_map = {
        ".pdf": "pdf",
        ".docx": "docx",
        ".pptx": "pptx",
        ".xlsx": "xlsx",
    }

    if ext not in type_map:
        raise ValueError(f"Unsupported file type: {ext}. Supported: {list(type_map.keys())}")

    return type_map[ext]


def initialize_docling_converter(settings):
    """
    Initialize a Docling DocumentConverter with local models, GPU acceleration, and threaded pipeline.

    Args:
        settings: Dict from config.load_extraction_config() containing docling_artifacts_path and docling_device.

    Returns:
        A configured DocumentConverter object (singleton-like, intended to be reused).

    Raises:
        ValueError: If the artifacts path does not exist or device is invalid.
    """
    import os

    artifacts_path = Path(settings["docling_artifacts_path"])

    if not artifacts_path.exists():
        raise ValueError(f"Docling artifacts path does not exist: {artifacts_path}")

    os.environ["HF_HOME"] = str(artifacts_path)

    device_str = settings["docling_device"].lower()
    device_map = {
        "cuda": AcceleratorDevice.CUDA,
        "cpu": AcceleratorDevice.CPU,
        "auto": AcceleratorDevice.AUTO,
    }

    if device_str not in device_map:
        raise ValueError(f"Invalid docling_device: {device_str}. Must be cuda, cpu, or auto.")

    device = device_map[device_str]

    accelerator_options = AcceleratorOptions(device=device)

    pdf_options = ThreadedPdfPipelineOptions(
        accelerator_options=accelerator_options,
        do_table_structure=True,
        table_structure_options=TableStructureOptions(
            mode=TableFormerMode.ACCURATE,
            do_cell_matching=True,
        ),
        layout_options=LayoutObjectDetectionOptions.from_preset("layout_heron_default"),
        do_formula_enrichment=True,
        do_code_enrichment=True,
        code_formula_options=CodeFormulaVlmOptions.from_preset("codeformulav2"),
        do_ocr=True,
        generate_picture_images=True,
        generate_page_images=True,
        images_scale=4.0,
        layout_batch_size=64,
        table_batch_size=4,
        ocr_batch_size=4,
    )

    converter = DocumentConverter(
        format_options={
            InputFormat.PDF: PdfFormatOption(
                pipeline_cls=ThreadedStandardPdfPipeline,
                pipeline_options=pdf_options,
            )
        }
    )

    logger.info(f"Docling converter initialized with HF_HOME={artifacts_path}, device={device_str}")
    return converter


def convert_with_docling(converter, file_path):
    """
    Run Docling's document conversion on a single file.

    Args:
        converter: DocumentConverter object from initialize_docling_converter().
        file_path: Path to the document file.

    Returns:
        ConversionResult if successful, None if conversion failed.
    """
    try:
        result = converter.convert(str(file_path))
        if result.status != ConversionStatus.SUCCESS:
            logger.warning(f"Docling conversion failed for {file_path}: status={result.status}")
            return None
        logger.info(f"Successfully converted {file_path} with Docling")
        return result
    except Exception as e:
        logger.warning(f"Docling conversion failed for {file_path}: {e}")
        return None


def is_conversion_valid(result):
    """
    Check if a Docling ConversionResult is non-empty and usable.

    Args:
        result: ConversionResult from convert_with_docling(), or None.

    Returns:
        True if result is valid (has content), False otherwise.
    """
    if result is None:
        return False

    doc = result.document
    if doc is None or len(doc.pages) == 0:
        logger.warning("Conversion result is empty (no pages)")
        return False

    return True


def get_fallback_extractor(file_type):
    """
    Get the fallback extractor function for a given file type.

    Args:
        file_type: One of "pdf", "docx", "pptx", "xlsx".

    Returns:
        The corresponding fallback function.

    Raises:
        ValueError: If file_type is not supported.
    """
    extractors = {
        "pdf": fallback_extract_pdf,
        "docx": fallback_extract_docx,
        "pptx": fallback_extract_pptx,
        "xlsx": fallback_extract_xlsx,
    }

    if file_type not in extractors:
        raise ValueError(f"No fallback extractor for format: {file_type}")

    return extractors[file_type]


def fallback_extract_pdf(file_path):
    """
    Extract content from a PDF using pymupdf when Docling fails.

    Args:
        file_path: Path to the PDF file.

    Returns:
        A dict with 'pages' key containing a list of page dicts with text, images, tables.
        Returns empty dict on error.
    """
    try:
        doc = pymupdf.open(str(file_path))
        pages = []

        for page_num, page in enumerate(doc, start=1):
            page_data = {
                "page": page_num,
                "text": page.get_text(),
                "images": [],
                "tables": [],
            }

            for img_index, img in enumerate(page.get_images()):
                try:
                    xref = img[0]
                    image_data = doc.extract_image(xref)
                    page_data["images"].append({
                        "index": img_index,
                        "size": (image_data["width"], image_data["height"]),
                        "data": image_data["image"],
                    })
                except Exception as e:
                    logger.warning(f"Failed to extract image from PDF page {page_num}: {e}")

            pages.append(page_data)

        doc.close()
        logger.info(f"Fallback PDF extraction succeeded: {len(pages)} pages")
        return {"pages": pages}

    except Exception as e:
        logger.error(f"Fallback PDF extraction failed: {e}")
        return {}


def fallback_extract_xlsx(file_path):
    """
    Extract content from Excel when Docling fails.

    Args:
        file_path: Path to the XLSX file.

    Returns:
        A dict with 'sheets' key containing sheet data (rows, cells, formulas).
        Returns empty dict on error.
    """
    try:
        wb = load_workbook(str(file_path))
        sheets = []

        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            sheet_data = {
                "name": sheet_name,
                "rows": [],
                "merged_cells": list(ws.merged_cells.ranges) if ws.merged_cells else [],
            }

            for row in ws.iter_rows(values_only=False):
                row_data = []
                for cell in row:
                    cell_info = {
                        "value": cell.value,
                        "formula": cell.value if isinstance(cell.value, str) and cell.value.startswith("=") else None,
                    }
                    row_data.append(cell_info)
                sheet_data["rows"].append(row_data)

            sheets.append(sheet_data)

        wb.close()
        logger.info(f"Fallback XLSX extraction succeeded: {len(sheets)} sheets")
        return {"sheets": sheets}

    except Exception as e:
        logger.error(f"Fallback XLSX extraction failed: {e}")
        return {}


def fallback_extract_pptx(file_path):
    """
    Extract content from PowerPoint when Docling fails.

    Args:
        file_path: Path to the PPTX file.

    Returns:
        A dict with 'slides' key containing slide data (shapes, text, images).
        Returns empty dict on error.
    """
    try:
        prs = Presentation(str(file_path))
        slides = []

        for slide_num, slide in enumerate(prs.slides, start=1):
            slide_data = {
                "slide": slide_num,
                "text": [],
                "tables": [],
                "images": [],
            }

            for shape in slide.shapes:
                if hasattr(shape, "text"):
                    slide_data["text"].append(shape.text)

                if shape.has_table:
                    table = shape.table
                    table_data = []
                    for row in table.rows:
                        row_cells = [cell.text for cell in row.cells]
                        table_data.append(row_cells)
                    slide_data["tables"].append(table_data)

                if shape.shape_type == 13:
                    slide_data["images"].append({"shape_id": shape.shape_id})

            slides.append(slide_data)

        logger.info(f"Fallback PPTX extraction succeeded: {len(slides)} slides")
        return {"slides": slides}

    except Exception as e:
        logger.error(f"Fallback PPTX extraction failed: {e}")
        return {}


def fallback_extract_docx(file_path):
    """
    Extract content from Word when Docling fails.

    Args:
        file_path: Path to the DOCX file.

    Returns:
        A dict with 'paragraphs' and 'tables' keys containing document structure.
        Returns empty dict on error.
    """
    try:
        doc = Document(str(file_path))
        data = {
            "paragraphs": [],
            "tables": [],
            "images": [],
        }

        for para in doc.paragraphs:
            if para.text.strip():
                data["paragraphs"].append({
                    "text": para.text,
                    "style": para.style.name if para.style else None,
                })

        for table in doc.tables:
            table_data = []
            for row in table.rows:
                row_cells = [cell.text for cell in row.cells]
                table_data.append(row_cells)
            data["tables"].append(table_data)

        for rel in doc.part.rels.values():
            if "image" in rel.target_ref:
                data["images"].append({"target": rel.target_ref})

        logger.info(f"Fallback DOCX extraction succeeded: {len(data['paragraphs'])} paragraphs, {len(data['tables'])} tables")
        return data

    except Exception as e:
        logger.error(f"Fallback DOCX extraction failed: {e}")
        return {}


def extract_pdf_pages(input_pdf, start_page, end_page, output_pdf):
    """
    Extract a page range from a PDF and save to a new file.

    Args:
        input_pdf: Path to source PDF.
        start_page: First page to extract (1-indexed).
        end_page: Last page to extract (inclusive, 1-indexed).
        output_pdf: Path where subset PDF will be saved.

    Returns:
        Path to output PDF if successful, None on error.
    """
    try:
        doc = pymupdf.open(str(input_pdf))

        if start_page < 1 or end_page > len(doc) or start_page > end_page:
            logger.error(f"Invalid page range: {start_page}-{end_page} (doc has {len(doc)} pages)")
            return None

        new_doc = pymupdf.open()
        for page_num in range(start_page - 1, end_page):
            new_doc.insert_pdf(doc, from_page=page_num, to_page=page_num)

        new_doc.save(str(output_pdf))
        new_doc.close()
        doc.close()

        logger.info(f"Extracted pages {start_page}-{end_page} to {output_pdf}")
        return Path(output_pdf)

    except Exception as e:
        logger.error(f"Failed to extract PDF pages: {e}")
        return None


def normalize_text(text):
    """
    Normalize extracted text: strip, collapse spaces, remove control chars.

    Args:
        text: Raw text string or None.

    Returns:
        Normalized string, or empty string if None/empty.
    """
    if text is None or not isinstance(text, str):
        return ""

    text = text.strip()
    text = re.sub(r'\s+', ' ', text)
    text = re.sub(r'[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]', '', text)

    return text


def normalize_table(table_data):
    """
    Normalize table structure: strip cells, handle None, remove empty rows, validate columns.

    Args:
        table_data: 2D list of table cells (rows × columns).

    Returns:
        Normalized 2D list, or empty list if invalid.
    """
    if not table_data or not isinstance(table_data, list):
        return []

    normalized = []
    max_cols = 0

    for row in table_data:
        if not isinstance(row, list):
            continue

        normalized_row = [normalize_text(cell) if cell is not None else "" for cell in row]

        if any(cell.strip() for cell in normalized_row):
            normalized.append(normalized_row)
            max_cols = max(max_cols, len(normalized_row))

    if not normalized:
        return []

    for row in normalized:
        while len(row) < max_cols:
            row.append("")

    return normalized


def cap_image_dimension(pil_image, max_dimension):
    """
    Cap image width to max_dimension via LANCZOS resize, preserving aspect ratio.
    Does not resize if already below max_dimension (preserves native quality).

    Args:
        pil_image: PIL Image object.
        max_dimension: Maximum width in pixels.

    Returns:
        Resized PIL Image if original width > max_dimension, otherwise original image.
    """
    if pil_image.width > max_dimension:
        ratio = max_dimension / pil_image.width
        new_height = int(pil_image.height * ratio)
        return pil_image.resize((max_dimension, new_height), Image.Resampling.LANCZOS)
    return pil_image


def get_page_and_bbox(item):
    """
    Extract the true page number and bounding box from a Docling item's provenance.

    Docling items have no 'page_number' attribute; the real page and bbox live in
    item.prov[0] (a ProvenanceItem). Falls back to (1, None) when provenance is absent.

    Args:
        item: A Docling DocItem (TextItem, TableItem, PictureItem, etc.).

    Returns:
        Tuple of (page_no: int, bbox: list[float, float, float, float] | None).
    """
    prov = getattr(item, "prov", None)
    if prov:
        p = prov[0]
        bbox = [p.bbox.l, p.bbox.t, p.bbox.r, p.bbox.b]
        return p.page_no, bbox
    return 1, None


def classify_text_label(element, text_content):
    """
    Classify text element's label (heading, body, caption, etc.).

    Args:
        element: Element dict/object with potential style info.
        text_content: The text string itself.

    Returns:
        Label string: "heading", "body", "caption", or "footnote".
    """
    if isinstance(element, dict):
        style = element.get("style", "").lower() if "style" in element else ""
        if "heading" in style or "title" in style:
            return "heading"
        elif "caption" in style:
            return "caption"
        elif "footer" in style or "footnote" in style:
            return "footnote"
        return "body"

    label = getattr(element, "label", None)
    if label in (DocItemLabel.TITLE, DocItemLabel.SECTION_HEADER):
        return "heading"
    elif label == DocItemLabel.CAPTION:
        return "caption"
    elif label in (DocItemLabel.FOOTNOTE, DocItemLabel.PAGE_FOOTER):
        return "footnote"
    return "body"


def walk_content(source, file_type, settings=None, use_vlm=True):
    """
    Walk document content in reading order, normalize elements, build blocks.

    Args:
        source: Docling ConversionResult or fallback extraction dict.
        file_type: One of "pdf", "docx", "pptx", "xlsx".
        settings: Optional dict from config.load_extraction_config() for VLM calls.
        use_vlm: Whether to use VLM/LLM for formula and image analysis (default True).

    Returns:
        A list of normalized block dicts, ordered by appearance.
    """
    blocks = []
    pending_vlm_jobs = []
    section_heading = ""
    page_num = 0

    try:
        if hasattr(source, 'document'):
            doc = source.document

            for item, _level in doc.iterate_items():
                page_num, bbox = get_page_and_bbox(item)
                ctx_base = {"page": page_num, "bbox": bbox, "file_type": file_type}

                if isinstance(item, TableItem):
                    block, vlm_job = build_table_block(
                        item,
                        {**ctx_base, "section_heading": section_heading},
                        doc,
                        settings,
                        use_vlm,
                    )
                    if block:
                        blocks.append(block)
                        if vlm_job:
                            pending_vlm_jobs.append((block, vlm_job))

                elif isinstance(item, PictureItem):
                    block, vlm_job = build_image_block(
                        item,
                        {**ctx_base, "section_heading": section_heading},
                        settings,
                        Path(settings["outputs_images_dir"]) if settings else Path("outputs/images"),
                        doc,
                        use_vlm,
                    )
                    if block:
                        blocks.append(block)
                        if vlm_job:
                            pending_vlm_jobs.append((block, vlm_job))

                elif isinstance(item, TextItem):
                    label = getattr(item, "label", None)

                    if label in (DocItemLabel.SECTION_HEADER, DocItemLabel.TITLE):
                        section_heading = normalize_text(getattr(item, 'text', ''))
                        if section_heading:
                            block = build_text_block(
                                item,
                                {**ctx_base, "section_heading": ""}
                            )
                            if block:
                                block["section_heading"] = ""
                                blocks.append(block)

                    elif label == DocItemLabel.FORMULA:
                        block, vlm_job = build_formula_block(
                            item,
                            {**ctx_base, "section_heading": section_heading},
                            doc,
                            settings,
                            use_vlm,
                        )
                        if block:
                            blocks.append(block)
                            if vlm_job:
                                pending_vlm_jobs.append((block, vlm_job))

                    elif label == DocItemLabel.CODE:
                        block = build_text_block(
                            item,
                            {**ctx_base, "section_heading": section_heading}
                        )
                        if block:
                            block["label"] = "code"
                            blocks.append(block)

                    else:
                        block = build_text_block(
                            item,
                            {**ctx_base, "section_heading": section_heading}
                        )
                        if block:
                            blocks.append(block)

            # Run all pending VLM verifications in parallel
            if use_vlm and settings and pending_vlm_jobs:
                run_parallel_vlm_verification(pending_vlm_jobs, settings, max_workers=8)

        else:
            if "pages" in source:
                for page_data in source.get("pages", []):
                    page_num = page_data.get("page", 0)
                    text = normalize_text(page_data.get("text", ""))

                    if text:
                        block = build_text_block(
                            {"text": text, "style": "body"},
                            {"section_heading": section_heading, "page": page_num, "file_type": file_type}
                        )
                        if block:
                            blocks.append(block)

                    for table_data in page_data.get("tables", []):
                        block, _ = build_table_block(
                            {"rows": table_data},
                            {"section_heading": section_heading, "page": page_num, "file_type": file_type},
                            None,
                            settings,
                            use_vlm,
                        )
                        if block:
                            blocks.append(block)

            elif "sheets" in source:
                for sheet_data in source.get("sheets", []):
                    sheet_name = normalize_text(sheet_data.get("name", ""))
                    if sheet_name:
                        section_heading = sheet_name

                    rows = sheet_data.get("rows", [])
                    if rows:
                        table_data = [[cell.get("value", "") for cell in row] for row in rows]
                        block, _ = build_table_block(
                            {"rows": table_data},
                            {"section_heading": section_heading, "page": 0, "file_type": file_type},
                            None,
                            settings,
                            use_vlm,
                        )
                        if block:
                            blocks.append(block)

            elif "slides" in source:
                for slide_data in source.get("slides", []):
                    page_num = slide_data.get("slide", 0)

                    for text_item in slide_data.get("text", []):
                        text = normalize_text(text_item)
                        if text:
                            block = build_text_block(
                                {"text": text, "style": "body"},
                                {"section_heading": section_heading, "page": page_num, "file_type": file_type}
                            )
                            if block:
                                blocks.append(block)

                    for table_data in slide_data.get("tables", []):
                        block, _ = build_table_block(
                            {"rows": table_data},
                            {"section_heading": section_heading, "page": page_num, "file_type": file_type},
                            None,
                            settings,
                            use_vlm,
                        )
                        if block:
                            blocks.append(block)

            elif "paragraphs" in source:
                for para_data in source.get("paragraphs", []):
                    text = normalize_text(para_data.get("text", ""))
                    style = para_data.get("style", "body")

                    if text:
                        if "heading" in style.lower():
                            section_heading = text
                            block = build_text_block(
                                {"text": text, "style": style},
                                {"section_heading": "", "page": 0, "file_type": file_type}
                            )
                            if block:
                                block["section_heading"] = ""
                                blocks.append(block)
                        else:
                            block = build_text_block(
                                {"text": text, "style": style},
                                {"section_heading": section_heading, "page": 0, "file_type": file_type}
                            )
                            if block:
                                blocks.append(block)

                for table_data in source.get("tables", []):
                    block, _ = build_table_block(
                        {"rows": table_data},
                        {"section_heading": section_heading, "page": 0, "file_type": file_type},
                        None,
                        settings,
                        use_vlm,
                    )
                    if block:
                        blocks.append(block)

        logger.info(f"Content walk completed: {len(blocks)} blocks extracted")
        return blocks

    except Exception as e:
        logger.error(f"Error walking content: {e}")
        return []


def build_text_block(element, context):
    """
    Build a normalized text block with label classification.

    Args:
        element: Text element (dict or Docling object) with 'text' or text attribute.
        context: Dict with 'section_heading', 'page', 'file_type'.

    Returns:
        A block dict with normalized content and metadata, or None if empty.
    """
    text = ""
    style = ""

    if isinstance(element, dict):
        text = normalize_text(element.get("text", ""))
        style = element.get("style", "body")
    else:
        text = normalize_text(getattr(element, "text", ""))
        style = getattr(element, "style", "body")

    if not text:
        return None

    label = classify_text_label(element, text)

    block = {
        "id": str(uuid.uuid4()),
        "type": "text",
        "label": label,
        "content": text,
        "page": context.get("page", 0),
        "bbox": context.get("bbox"),
        "section_heading": context.get("section_heading", ""),
        "confidence": 1.0,
        "remediated": False,
        "needs_review": False,
        "source_file": "",
        "extraction_source": "docling" if context.get("file_type") != "fallback" else "fallback",
    }

    return block


def build_table_block(element, context, doc, settings, use_vlm=True):
    """
    Build a normalized table block with structure validation and deferred VLM verification.

    Args:
        element: Table element (dict or Docling TableItem) with row data.
        context: Dict with 'section_heading', 'page', 'file_type'.
        doc: Docling document object (used to crop the table image for VLM verification).
        settings: Dict from config.load_extraction_config() with VLM credentials and output paths.
        use_vlm: Whether to verify the extracted table against its image via VLM (default True).

    Returns:
        Tuple (block, vlm_job) where vlm_job is None or {"image_path": str, "prompt": str}.
        Block is None if extraction yielded no data.
    """
    table_data = []

    if isinstance(element, dict):
        if "rows" in element:
            table_data = element.get("rows", [])
    else:
        if hasattr(element, "data") and element.data is not None:
            table_data = [[cell.text for cell in row] for row in element.data.grid]

    normalized_table = normalize_table(table_data)

    if not normalized_table or len(normalized_table) == 0:
        return None, None

    block = {
        "id": str(uuid.uuid4()),
        "type": "table",
        "label": "table",
        "content": normalized_table,
        "raw_extraction": normalized_table,
        "vlm_verified": False,
        "page": context.get("page", 0),
        "bbox": context.get("bbox"),
        "section_heading": context.get("section_heading", ""),
        "caption": None,
        "confidence": 1.0,
        "remediated": False,
        "needs_review": False,
        "source_file": "",
        "extraction_source": "docling" if context.get("file_type") != "fallback" else "fallback",
    }

    vlm_job = None
    if settings and use_vlm and not isinstance(element, dict) and hasattr(element, "get_image"):
        pil_image = element.get_image(doc)
        if pil_image is not None:
            table_id = str(uuid.uuid4())
            tables_dir = Path(settings.get("outputs_tables_dir", "outputs/tables"))
            tables_dir.mkdir(parents=True, exist_ok=True)
            image_path = tables_dir / f"{table_id}.png"
            pil_image = pil_image.convert("RGB")
            pil_image = cap_image_dimension(pil_image, 4000)
            pil_image.save(image_path, "PNG")
            block["source_file"] = str(image_path)


            prompt = f"""Analyze this table image from an engineering document.
Our extraction pipeline read the following content, which may have misaligned cells:
{json.dumps(normalized_table)}

Compare each cell against the image and return the CORRECTED table. The "data" array must have
exactly the rows/columns visible in the image, with each cell matching the image exactly.
Return JSON with this exact structure: {{"data": [["cell","cell",...], ...], "had_errors": true/false, "notes": "..."}}"""

            vlm_job = {"image_path": str(image_path), "prompt": prompt}
        else:
            block["needs_review"] = True
    else:
        block["needs_review"] = True

    return block, vlm_job


def extract_and_save_image(element, outputs_images_dir, doc, settings=None):
    """
    Extract image from element and save to outputs directory.
    Skips small images (logos/icons) based on min_image_width and min_image_size_kb.

    Args:
        element: Image element (dict or Docling object) with image data.
        outputs_images_dir: Path to outputs/images/ directory.
        doc: Docling document object (used to extract PictureItem images).
        settings: Optional config dict with min_image_width and min_image_size_kb.

    Returns:
        Path to saved image (relative to project root), or None if failed/filtered.
    """
    try:
        image_id = str(uuid.uuid4())
        outputs_path = Path(outputs_images_dir)
        outputs_path.mkdir(parents=True, exist_ok=True)

        pil_image = None
        if isinstance(element, dict):
            image_data = element.get("data")
            if image_data:
                pil_image = Image.open(io.BytesIO(image_data))
        else:
            if hasattr(element, "get_image"):
                pil_image = element.get_image(doc)

        if pil_image is None:
            logger.warning("No image available for element (get_image returned None)")
            return None

        # Check minimum dimensions (skip logos/icons)
        if settings:
            min_width = settings.get("min_image_width", 250)
            min_size_kb = settings.get("min_image_size_kb", 25)

            if pil_image.width < min_width:
                logger.info(f"Skipping small image (width {pil_image.width}px < {min_width}px, likely logo/icon)")
                return None

        saved_path = outputs_path / f"{image_id}.png"
        pil_image = pil_image.convert("RGB")
        pil_image = cap_image_dimension(pil_image, 4000)
        pil_image.save(saved_path, "PNG")

        # Check file size after saving
        if settings:
            min_size_kb = settings.get("min_image_size_kb", 25)
            file_size_kb = saved_path.stat().st_size / 1024

            if file_size_kb < min_size_kb:
                logger.info(f"Skipping small image ({file_size_kb:.1f}KB < {min_size_kb}KB, likely logo/icon)")
                saved_path.unlink()
                return None

        logger.info(f"Image saved: {saved_path}")
        return f"images/{image_id}.png"

    except Exception as e:
        logger.error(f"Failed to extract and save image: {e}")
        return None


def build_formula_block(element, context, doc, settings, use_vlm=True):
    """
    Build a formula block with deferred VLM verification of transcription.

    Args:
        element: Formula element with formula text.
        context: Dict with 'section_heading', 'page', 'file_type'.
        doc: Docling document object (used to crop the formula image for VLM verification).
        settings: Dict from config.load_extraction_config() with VLM credentials and output paths.
        use_vlm: Whether to verify the formula against its image via VLM (default True).

    Returns:
        Tuple (block, vlm_job) where vlm_job is None or {"image_path": str, "prompt": str}.
        Block is None if formula_text is empty.
    """
    formula_text = ""

    if isinstance(element, dict):
        formula_text = normalize_text(element.get("content", ""))
    else:
        formula_text = normalize_text(getattr(element, "text", ""))

    if not formula_text:
        return None, None

    block = {
        "id": str(uuid.uuid4()),
        "type": "formula",
        "label": "formula",
        "content": formula_text,
        "raw_extraction": formula_text,
        "vlm_verified": False,
        "page": context.get("page", 0),
        "bbox": context.get("bbox"),
        "section_heading": context.get("section_heading", ""),
        "confidence": 0.8,
        "remediated": False,
        "needs_review": False,
        "source_file": "",
        "extraction_source": "docling" if context.get("file_type") != "fallback" else "fallback",
        "explanation": None,
        "variables": [],
        "use_case": None,
    }

    vlm_job = None
    if settings and use_vlm and not isinstance(element, dict) and hasattr(element, "get_image"):
        pil_image = element.get_image(doc)
        if pil_image is not None:
            formula_id = str(uuid.uuid4())
            formulas_dir = Path(settings.get("outputs_formulas_dir", "outputs/formulas"))
            formulas_dir.mkdir(parents=True, exist_ok=True)
            image_path = formulas_dir / f"{formula_id}.png"

            formula_img = pil_image.convert("RGB")
            formula_img = cap_image_dimension(formula_img, 1200)
            formula_img.save(image_path, "PNG")

            prompt = f"""Analyze this formula image. Our extraction pipeline transcribed it as:
"{formula_text}"

Verify the transcription against the image and correct it if wrong. Be concise: no reasoning,
no preamble, just the JSON answer below. Keep "explanation" and "use_case" short and to the
point (a sentence or two, not a paragraph) - this is for a technical database, not an essay.
List ALL variables that appear in the formula, however many there are. Return ONLY this JSON
structure:
{{"formula": "exact corrected formula as shown in image", "explanation": "short technical sentence(s) on what this formula calculates", "variables": ["list every variable name that appears"], "use_case": "short phrase on typical use"}}"""

            vlm_job = {"image_path": str(image_path), "prompt": prompt, "max_tokens": 600}
        else:
            block["needs_review"] = True
    else:
        block["needs_review"] = True

    return block, vlm_job


def build_image_block(element, context, settings, outputs_images_dir, doc, use_vlm=True):
    """
    Build an image block with VLM-generated description and classification.
    Skips small images (logos/icons) based on settings.

    Args:
        element: Image element (dict or Docling object) with image data.
        context: Dict with 'section_heading', 'page', 'file_type'.
        settings: Dict from config.load_extraction_config() with neuroverse credentials.
        outputs_images_dir: Path to outputs/images/ directory.
        doc: Docling document object (used to extract PictureItem images).
        use_vlm: Whether to use VLM for image analysis (default True).

    Returns:
        A block dict with image path, description, classification, etc., or None if failed/filtered.
    """
    image_rel_path = extract_and_save_image(element, outputs_images_dir, doc, settings)

    if not image_rel_path:
        return None, None  # Image was filtered out (logo/icon) or extraction failed

    image_abs_path = Path(outputs_images_dir) / Path(image_rel_path).name

    block = {
        "id": str(uuid.uuid4()),
        "type": "image",
        "label": "image",
        "content": image_rel_path,
        "page": context.get("page", 0),
        "bbox": context.get("bbox"),
        "section_heading": context.get("section_heading", ""),
        "confidence": 0.8,
        "remediated": False,
        "needs_review": False,
        "source_file": "",
        "extraction_source": "docling" if context.get("file_type") != "fallback" else "fallback",
        "description": None,
        "classification": None,
        "extracted_text": None,
    }

    vlm_job = None
    if settings and use_vlm:
        prompt = """Analyze this image and provide structured response as JSON:
{
    "description": "detailed description of image content",
    "classification": "type of image (diagram/chart/photo/table/equation/icon/other)",
    "extracted_text": "any visible text in the image, or null if none"
}"""

        vlm_job = {"image_path": str(image_abs_path), "prompt": prompt}
    else:
        block["needs_review"] = True

    return block, vlm_job

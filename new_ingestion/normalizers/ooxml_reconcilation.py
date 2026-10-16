"""OOXML-authoritative reconciliation for XLSX media and IMAGE formulas.

The module inspects only package relationships, drawing anchors, media bytes,
and worksheet formula XML. It does not re-parse ordinary worksheet values.
"""

from __future__ import annotations

import hashlib
import mimetypes
import posixpath
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile
from xml.etree import ElementTree

from new_ingestion.extraction.excel_extractor import ExcelExtractionResult


_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_DOC_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_DRAWING_MAIN_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_IMAGE_FORMULA = re.compile(r"IMAGE\s*\(", re.IGNORECASE)
_URL_LITERAL = re.compile(r"IMAGE\s*\(\s*\"([^\"]+)\"", re.IGNORECASE)
_MEDIA_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp", ".tif": "image/tiff",
    ".tiff": "image/tiff", ".webp": "image/webp",
}


@dataclass(slots=True)
class ReconciledImage:
    image_id: str
    status: str
    content_type: str | None = None
    sha256: str | None = None
    worksheet_name: str | None = None
    worksheet_xml_path: str | None = None
    drawing_xml_path: str | None = None
    media_path: str | None = None
    relationship_ids: list[str] = field(default_factory=list)
    anchor_start: str | None = None
    anchor_end: str | None = None
    anchors: list[dict[str, Any]] = field(default_factory=list)
    drawing_ids: list[str] = field(default_factory=list)
    provenance: list[str] = field(default_factory=lambda: ["ooxml_reconciliation"])
    pixel_width: int | None = None
    pixel_height: int | None = None
    asset_reference: str | None = None
    formula: str | None = None
    source_cell: str | None = None
    external_url: str | None = None
    image_kind: str | None = None
    value_metadata_index: int | None = None
    rich_value_index: int | None = None
    local_image_identifier: int | None = None
    binary_data: bytes | None = field(default=None, repr=False)


@dataclass(slots=True)
class ReconciledFormula:
    worksheet_name: str
    worksheet_xml_path: str
    coordinate: str
    formula: str
    cached_value: str | None
    classification: str
    status: str
    external_url: str | None = None
    value_metadata_index: int | None = None
    rich_value_index: int | None = None
    local_image_identifier: int | None = None
    relationship_id: str | None = None
    media_path: str | None = None
    image_kind: str | None = None


@dataclass(slots=True)
class _RichValueRecord:
    index: int
    structure_index: int
    structure_type: str | None
    fields: dict[str, str | None]


@dataclass(slots=True)
class _RichValueContext:
    vm_to_rich: dict[int, int] = field(default_factory=dict)
    records: dict[int, _RichValueRecord] = field(default_factory=dict)
    relation_ids: list[str] = field(default_factory=list)
    relation_targets: dict[str, tuple[str, str | None]] = field(default_factory=dict)


@dataclass(slots=True)
class XlsxReconciliationResult:
    images: list[ReconciledImage] = field(default_factory=list)
    formulas: list[ReconciledFormula] = field(default_factory=list)
    media_inventory: list[ReconciledImage] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _rels(archive: ZipFile, source: str) -> dict[str, str]:
    rel_path = posixpath.join(posixpath.dirname(source), "_rels", f"{posixpath.basename(source)}.rels")
    try:
        root = ElementTree.fromstring(archive.read(rel_path))
    except KeyError:
        return {}
    except ElementTree.ParseError:
        return {}
    result: dict[str, str] = {}
    for relation in root.findall(f"{{{_REL_NS}}}Relationship"):
        rel_id = relation.attrib.get("Id")
        target = relation.attrib.get("Target")
        if rel_id and target:
            result[rel_id] = posixpath.normpath(posixpath.join(posixpath.dirname(source), target))
    return result


def _cell(node: Any, child_name: str) -> str | None:
    if node is None:
        return None
    child = next((item for item in node if _local(item.tag) == child_name), None)
    if child is None:
        return None
    return child.text


def _anchor(node: Any) -> tuple[str | None, str | None]:
    if node is None:
        return None, None
    start_node = next((item for item in node if _local(item.tag) == "from"), None)
    end_node = next((item for item in node if _local(item.tag) == "to"), None)
    def convert(point: Any) -> str | None:
        if point is None:
            return None
        try:
            column = int(_cell(point, "col")) + 1
            row = int(_cell(point, "row")) + 1
        except (TypeError, ValueError):
            return None
        letters = ""
        while column:
            column, remainder = divmod(column - 1, 26)
            letters = chr(65 + remainder) + letters
        return f"{letters}{row}"
    return convert(start_node), convert(end_node)


def _drawing_id(anchor: Any) -> str | None:
    for node in anchor.iter():
        if _local(node.tag) == "cNvPr":
            return node.attrib.get("id") or node.attrib.get("name")
    return None


def _warning(result: XlsxReconciliationResult, message: str) -> None:
    if message not in result.warnings:
        result.warnings.append(message)


def _read_xml(archive: ZipFile, path: str, result: XlsxReconciliationResult, context: str) -> Any | None:
    try:
        return ElementTree.fromstring(archive.read(path))
    except KeyError as error:
        _warning(result, f"missing rich-value XML: part={path}, context={context}, error={error}")
    except ElementTree.ParseError as error:
        _warning(result, f"malformed rich-value XML: part={path}, context={context}, error={error}")
    return None


def _rich_context(archive: ZipFile, result: XlsxReconciliationResult) -> _RichValueContext | None:
    """Parse rich-value parts once, returning None when the feature is absent."""
    required = [
        "xl/metadata.xml", "xl/richData/rdrichvalue.xml",
        "xl/richData/rdrichvaluestructure.xml", "xl/richData/richValueRel.xml",
        "xl/richData/_rels/richValueRel.xml.rels",
    ]
    present = set(archive.namelist())
    if not any(path in present for path in required):
        return None
    context = _RichValueContext()
    metadata = _read_xml(archive, "xl/metadata.xml", result, "rich-value metadata")
    structures = _read_xml(archive, "xl/richData/rdrichvaluestructure.xml", result, "rich-value structures")
    values = _read_xml(archive, "xl/richData/rdrichvalue.xml", result, "rich-value records")
    relation_xml = _read_xml(archive, "xl/richData/richValueRel.xml", result, "rich-value relation order")
    if metadata is None or structures is None or values is None or relation_xml is None:
        return context

    has_xlrichvalue = any(
        node.attrib.get("name") == "XLRICHVALUE"
        for node in metadata.iter() if _local(node.tag) in {"metadataType", "futureMetadata"}
    )
    value_metadata = next((node for node in metadata.iter() if _local(node.tag) == "valueMetadata"), None)
    if has_xlrichvalue and value_metadata is not None:
        for index, node in enumerate(item for item in value_metadata if _local(item.tag) == "bk"):
            rc = next((child for child in node.iter() if _local(child.tag) == "rc"), None)
            if rc is None:
                continue
            try:
                # vm indexes valueMetadata entries; rc@v is the rich record index.
                context.vm_to_rich[index] = int(rc.attrib["v"])
            except (KeyError, TypeError, ValueError):
                _warning(result, f"invalid rich-value metadata mapping: value_metadata_index={index}")
    elif value_metadata is not None:
        _warning(result, "rich-value metadata has no XLRICHVALUE metadata type")

    structure_rows = list(node for node in structures if _local(node.tag) == "s")
    structure_fields: dict[int, tuple[str | None, list[str]]] = {}
    for index, structure in enumerate(structure_rows):
        fields = [child.attrib.get("n", "") for child in structure if _local(child.tag) == "k"]
        structure_fields[index] = (structure.attrib.get("t"), fields)
    for index, record in enumerate(node for node in values.iter() if _local(node.tag) == "rv"):
        try:
            structure_index = int(record.attrib.get("s", "-1"))
        except ValueError:
            _warning(result, f"invalid rich-value structure index: rich_value_index={index}")
            continue
        structure_type, fields = structure_fields.get(structure_index, (None, []))
        if not fields:
            _warning(result, f"missing rich-value structure: rich_value_index={index}, structure_index={structure_index}")
        values_in_record = [child.text for child in record if _local(child.tag) == "v"]
        mapped = {field: values_in_record[position] if position < len(values_in_record) else None
                  for position, field in enumerate(fields)}
        context.records[index] = _RichValueRecord(index, structure_index, structure_type, mapped)

    for relation in relation_xml.iter():
        if _local(relation.tag) != "rel":
            continue
        relation_id = next((value for key, value in relation.attrib.items() if _local(key) == "id"), None)
        if relation_id:
            context.relation_ids.append(relation_id)
    try:
        rel_root = ElementTree.fromstring(archive.read("xl/richData/_rels/richValueRel.xml.rels"))
        for relation in rel_root.iter():
            if _local(relation.tag) != "Relationship":
                continue
            relation_id = relation.attrib.get("Id")
            target = relation.attrib.get("Target")
            if relation_id and target:
                target_path = target if "://" in target else posixpath.normpath(posixpath.join("xl/richData", target))
                context.relation_targets[relation_id] = (target_path, relation.attrib.get("Type"))
    except (KeyError, ElementTree.ParseError) as error:
        _warning(result, f"malformed rich-value relationship XML: part=xl/richData/_rels/richValueRel.xml.rels, error={error}")
    return context


def _worksheet_parts(archive: ZipFile, result: XlsxReconciliationResult) -> list[tuple[str, str]]:
    try:
        root = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        workbook_rels = _rels(archive, "xl/workbook.xml")
    except KeyError as error:
        result.errors.append(f"malformed XML: missing workbook.xml: {error}")
        return []
    except ElementTree.ParseError as error:
        result.errors.append(f"malformed XML: xl/workbook.xml: {error}")
        return []
    parts: list[tuple[str, str]] = []
    for sheet in root.findall(f"{{{_MAIN_NS}}}sheets/{{{_MAIN_NS}}}sheet"):
        name = sheet.attrib.get("name", "")
        rel_id = sheet.attrib.get(f"{{{_DOC_REL_NS}}}id")
        path = workbook_rels.get(rel_id or "")
        if not path:
            _warning(result, f"missing worksheet relationship: worksheet={name!r}, relationship={rel_id!r}")
        else:
            parts.append((name, path))
    return parts


def _formula_records(root: Any, sheet_name: str, sheet_path: str) -> list[ReconciledFormula]:
    records: list[ReconciledFormula] = []
    for cell in root.findall(f".//{{{_MAIN_NS}}}c"):
        reference = cell.attrib.get("r")
        formula_node = next((item for item in cell if _local(item.tag) == "f"), None)
        value_node = next((item for item in cell if _local(item.tag) == "v"), None)
        formula = formula_node.text or "" if formula_node is not None else ""
        if formula and not formula.startswith("="):
            formula = f"={formula}"
        cached = value_node.text if value_node is not None else None
        if formula:
            if _IMAGE_FORMULA.search(formula):
                url_match = _URL_LITERAL.search(formula)
                records.append(ReconciledFormula(
                    worksheet_name=sheet_name, worksheet_xml_path=sheet_path,
                    coordinate=reference or "", formula=formula, cached_value=cached,
                    classification="external_formula_image",
                    status="image_formula_unresolved",
                    external_url=url_match.group(1) if url_match else None,
                ))
            elif cached == "#VALUE!":
                records.append(ReconciledFormula(
                    worksheet_name=sheet_name, worksheet_xml_path=sheet_path,
                    coordinate=reference or "", formula=formula, cached_value=cached,
                    classification="formula_error", status="formula_error",
                ))
        elif cached == "#VALUE!":
            records.append(ReconciledFormula(
                worksheet_name=sheet_name, worksheet_xml_path=sheet_path,
                coordinate=reference or "", formula="", cached_value=cached,
                classification="literal_error_value", status="literal_error_value",
            ))
    return records


def _rich_cell_records(
    root: Any,
    sheet_name: str,
    sheet_path: str,
    context: _RichValueContext,
    media_by_path: dict[str, ReconciledImage],
    result: XlsxReconciliationResult,
) -> tuple[list[ReconciledFormula], list[ReconciledImage]]:
    formulas: list[ReconciledFormula] = []
    images: list[ReconciledImage] = []
    for cell in root.findall(f".//{{{_MAIN_NS}}}c"):
        vm_text = cell.attrib.get("vm")
        if vm_text is None:
            continue
        coordinate = cell.attrib.get("r", "")
        try:
            vm_index = int(vm_text)
        except ValueError:
            _warning(result, f"invalid rich-value VM: worksheet={sheet_name}, cell={coordinate}, vm={vm_text}")
            continue
        rich_index = context.vm_to_rich.get(vm_index)
        if rich_index is None:
            _warning(result, f"invalid or missing rich-value metadata record: worksheet={sheet_name}, cell={coordinate}, vm={vm_index}")
            continue
        record = context.records.get(rich_index)
        if record is None:
            _warning(result, f"missing rich-value record: worksheet={sheet_name}, cell={coordinate}, vm={vm_index}, rich_value_index={rich_index}")
            continue
        if record.structure_type != "_localImage":
            _warning(result, f"non-image rich value: worksheet={sheet_name}, cell={coordinate}, vm={vm_index}, rich_value_index={rich_index}, structure={record.structure_type}")
            continue
        identifier_text = record.fields.get("_rvRel:LocalImageIdentifier")
        try:
            identifier = int(identifier_text) if identifier_text is not None else None
        except ValueError:
            identifier = None
        if identifier is None:
            _warning(result, f"missing LocalImageIdentifier: worksheet={sheet_name}, cell={coordinate}, vm={vm_index}, rich_value_index={rich_index}")
            continue
        if identifier < 0 or identifier >= len(context.relation_ids):
            _warning(result, f"missing rich relationship: worksheet={sheet_name}, cell={coordinate}, vm={vm_index}, local_image_identifier={identifier}")
            continue
        relationship_id = context.relation_ids[identifier]
        target_info = context.relation_targets.get(relationship_id)
        if target_info is None:
            _warning(result, f"missing rich relationship target: worksheet={sheet_name}, cell={coordinate}, vm={vm_index}, relationship={relationship_id}")
            continue
        media_path, relationship_type = target_info
        if "://" in media_path:
            _warning(result, f"external rich image relationship: worksheet={sheet_name}, cell={coordinate}, vm={vm_index}, relationship={relationship_id}, target={media_path}")
            continue
        if relationship_type and not relationship_type.endswith("/image"):
            _warning(result, f"non-image rich relationship: worksheet={sheet_name}, cell={coordinate}, relationship={relationship_id}, target={media_path}")
            continue
        media = media_by_path.get(media_path)
        if media is None:
            _warning(result, f"missing rich media target: worksheet={sheet_name}, cell={coordinate}, vm={vm_index}, relationship={relationship_id}, media={media_path}")
            continue
        cached_node = next((child for child in cell if _local(child.tag) == "v"), None)
        cached_value = cached_node.text if cached_node is not None else None
        formulas.append(ReconciledFormula(
            worksheet_name=sheet_name, worksheet_xml_path=sheet_path,
            coordinate=coordinate, formula="", cached_value=cached_value,
            classification="mapped_rich_cell_image", status="mapped_rich_cell_image",
            value_metadata_index=vm_index, rich_value_index=rich_index,
            local_image_identifier=identifier, relationship_id=relationship_id,
            media_path=media_path, image_kind="rich_cell_image",
        ))
        images.append(ReconciledImage(
            image_id=f"rich:{sheet_name}!{coordinate}:{media.sha256}",
            status="mapped_rich_cell_image", image_kind="rich_cell_image",
            value_metadata_index=vm_index, rich_value_index=rich_index,
            local_image_identifier=identifier, content_type=media.content_type,
            sha256=media.sha256, worksheet_name=sheet_name,
            worksheet_xml_path=sheet_path, media_path=media_path,
            relationship_ids=[relationship_id], source_cell=coordinate,
            asset_reference=media.asset_reference,
            provenance=["ooxml_reconciliation", "excel_rich_value", "local_image"],
            binary_data=media.binary_data,
        ))
        media.status = "mapped_rich_cell_image"
    return formulas, images


def reconcile_xlsx(source: ExcelExtractionResult | str | Path) -> XlsxReconciliationResult:
    """Reconcile XLSX media/formulas from the package's authoritative XML."""
    path = Path(source.source_path if isinstance(source, ExcelExtractionResult) else source)
    result = XlsxReconciliationResult()
    try:
        archive = ZipFile(path)
    except (OSError, BadZipFile) as error:
        result.errors.append(f"malformed XLSX ZIP package: {path}: {error}")
        return result

    try:
        names = sorted(name for name in archive.namelist() if name.startswith("xl/media/"))
        media_by_path: dict[str, ReconciledImage] = {}
        for media_path in names:
            try:
                data = archive.read(media_path)
                digest = hashlib.sha256(data).hexdigest()
                record = ReconciledImage(
                    image_id=f"media:{media_path}", status="unmapped_media",
                    content_type=_MEDIA_TYPES.get(Path(media_path).suffix.lower()) or mimetypes.guess_type(media_path)[0],
                    sha256=digest, media_path=media_path, binary_data=data,
                    asset_reference=f"images/image-{digest}{Path(media_path).suffix.lower() or '.bin'}",
                )
                media_by_path[media_path] = record
                result.media_inventory.append(record)
            except Exception as error:
                result.errors.append(f"media path={media_path}: extraction error: {error}")

        rich_context = _rich_context(archive, result)
        placement_counts: dict[tuple[str, str | None, str], int] = {}
        for sheet_name, sheet_path in _worksheet_parts(archive, result):
            try:
                worksheet_root = ElementTree.fromstring(archive.read(sheet_path))
            except KeyError as error:
                _warning(result, f"malformed XML: worksheet={sheet_name}, path={sheet_path}, error={error}")
                continue
            except ElementTree.ParseError as error:
                _warning(result, f"malformed XML: worksheet={sheet_name}, path={sheet_path}, error={error}")
                continue
            worksheet_formulas = _formula_records(worksheet_root, sheet_name, sheet_path)
            worksheet_rich_formulas: list[ReconciledFormula] = []
            if rich_context is not None:
                worksheet_rich_formulas, rich_images = _rich_cell_records(
                    worksheet_root, sheet_name, sheet_path, rich_context, media_by_path, result,
                )
                rich_coordinates = {(item.worksheet_name, item.coordinate) for item in worksheet_rich_formulas}
                worksheet_formulas = [
                    item for item in worksheet_formulas
                    if (item.worksheet_name, item.coordinate) not in rich_coordinates
                ]
                result.images.extend(rich_images)
            result.formulas.extend(worksheet_formulas)
            result.formulas.extend(worksheet_rich_formulas)
            worksheet_relationships = _rels(archive, sheet_path)
            drawing_nodes = [node for node in worksheet_root if _local(node.tag) == "drawing"]
            for drawing_node in drawing_nodes:
                worksheet_rel_id = drawing_node.attrib.get(f"{{{_DOC_REL_NS}}}id")
                drawing_path = worksheet_relationships.get(worksheet_rel_id or "")
                if not drawing_path:
                    _warning(result, f"missing drawing relationship: worksheet={sheet_name}, relationship={worksheet_rel_id}")
                    continue
                try:
                    drawing_root = ElementTree.fromstring(archive.read(drawing_path))
                except (KeyError, ElementTree.ParseError) as error:
                    _warning(result, f"malformed XML: worksheet={sheet_name}, drawing={drawing_path}, error={error}")
                    continue
                drawing_relationships = _rels(archive, drawing_path)
                for anchor in drawing_root:
                    if _local(anchor.tag) not in {"twoCellAnchor", "oneCellAnchor", "absoluteAnchor"}:
                        continue
                    start, end = _anchor(anchor)
                    drawing_id = _drawing_id(anchor)
                    blips = [node for node in anchor.iter() if _local(node.tag) == "blip"]
                    for blip in blips:
                        drawing_rel_id = blip.attrib.get(f"{{{_DOC_REL_NS}}}embed")
                        media_path = drawing_relationships.get(drawing_rel_id or "")
                        if not media_path:
                            _warning(result, f"missing media relationship: worksheet={sheet_name}, drawing={drawing_path}, relationship={drawing_rel_id}")
                            continue
                        media = media_by_path.get(media_path)
                        if media is None:
                            _warning(result, f"unresolved relationship: worksheet={sheet_name}, drawing={drawing_path}, media={media_path}")
                            continue
                        status = "mapped_floating_image"
                        if any(image.sha256 == media.sha256 for image in result.images):
                            status = "duplicate_asset"
                        placement_key = (sheet_name, start, media.sha256 or "")
                        placement_counts[placement_key] = placement_counts.get(placement_key, 0) + 1
                        image = ReconciledImage(
                            image_id=f"placement:{sheet_name}!{start or '?'}:{media.sha256}:{placement_counts[placement_key]:03d}",
                            status=status, content_type=media.content_type, sha256=media.sha256,
                            worksheet_name=sheet_name, worksheet_xml_path=sheet_path,
                            drawing_xml_path=drawing_path, media_path=media_path,
                            relationship_ids=[worksheet_rel_id or "", drawing_rel_id or ""],
                            anchor_start=start, anchor_end=end,
                            anchors=[{"worksheet": sheet_name, "start": start, "end": end}],
                            drawing_ids=[drawing_id] if drawing_id else [],
                            asset_reference=media.asset_reference, binary_data=media.binary_data,
                        )
                        result.images.append(image)
                        media.status = "mapped_floating_image"
                        if start is None:
                            _warning(result, f"missing anchor: worksheet={sheet_name}, media={media_path}, drawing={drawing_path}")
        for media in result.media_inventory:
            if media.status == "unmapped_media":
                _warning(result, f"unmapped media: media={media.media_path}, sha256={media.sha256}")
    finally:
        archive.close()
    return result


__all__ = [
    "ReconciledFormula", "ReconciledImage", "XlsxReconciliationResult", "reconcile_xlsx",
]

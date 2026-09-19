"""Regression tests for loss-aware Docling table extraction and canonical mapping."""

import unittest
from unittest.mock import Mock

from backend.ingestion.extraction.docling import DoclingContentAdapter
from backend.ingestion.extraction.contracts import ExtractedPage, ExtractionResult
from backend.ingestion.extraction.merger import ExtractionResultMerger
from backend.ingestion.models import SourceType
from backend.ingestion.parsers.document import DoclingCanonicalBuilder
from backend.ingestion.validation.quality import CanonicalQualityValidator


class StructuredTableTests(unittest.TestCase):
    """Checks content and failure behavior without downloading models."""

    def payload(self):
        """Returns a two-column table with a merged heading and numeric data."""
        return {"label": "table", "prov": [{"page_no": 2}], "data": {
            "num_rows": 2, "num_cols": 2, "table_cells": [
                {"text": "Temperature", "start_row_offset_idx": 0, "end_row_offset_idx": 1,
                 "start_col_offset_idx": 0, "end_col_offset_idx": 2, "row_span": 1,
                 "col_span": 2, "column_header": True},
                {"text": "90", "start_row_offset_idx": 1, "end_row_offset_idx": 2,
                 "start_col_offset_idx": 1, "end_col_offset_idx": 2, "row_span": 1, "col_span": 1}]}}

    def build(self, table):
        """Runs the shared PDF/PPT/DOCX canonical table builder."""
        return DoclingCanonicalBuilder().build_matrix_table_node(document_id="doc", source_type=SourceType.PDF,
            version="v1", parent_node_id="page", table_index=1, rows=table)

    def test_structured_cells_do_not_depend_on_dataframe(self):
        """DataFrame failures are irrelevant when structured cells are available."""
        table = Mock()
        table.model_dump.return_value = self.payload()
        table.export_to_dataframe.side_effect = RuntimeError("cannot flatten")
        result = DoclingContentAdapter()._extract_table(table, None)
        table.export_to_dataframe.assert_not_called()
        node = self.build(result)
        self.assertEqual(node.children[0].children[0].attributes["col_span"], 2)
        self.assertTrue(node.children[0].children[0].attributes["column_header"])
        self.assertEqual(node.children[1].children[0].text, "90")
        self.assertEqual(node.attributes["source_table"], self.payload())
        self.assertFalse(node.attributes["requires_review"])
        self.assertEqual(CanonicalQualityValidator()._validate_tables([node]), [])

    def test_failed_exports_retain_placeholder_and_raise_quality_error(self):
        """An unreadable table cannot silently disappear as an empty list."""
        table = Mock()
        table.model_dump.return_value = {"data": {"unknown": "retained"}}
        table.export_to_dataframe.side_effect = ValueError("bad export")
        node = self.build(DoclingContentAdapter()._extract_table(table, None))
        self.assertEqual(node.attributes["source_table"]["data"]["unknown"], "retained")
        issues = CanonicalQualityValidator()._validate_tables([node])
        self.assertTrue(any(issue.code == "unreadable_table" for issue in issues))

    def test_docx_body_uses_cells_without_grid(self):
        """DOCX canonicalization retains tables even without a computed grid."""
        exported = {"body": {"children": [{"$ref": "#/tables/0"}]}, "tables": [self.payload()]}
        nodes = DoclingCanonicalBuilder().build_section_nodes(document_id="doc", source_type=SourceType.DOCX,
            version="v1", parent_node_id="root", exported=exported, markdown="")
        self.assertEqual(nodes[0].attributes["source_table"], self.payload())

    def test_invalid_positions_are_preserved_and_flagged(self):
        """Malformed cells remain in raw data and prevent clean validation."""
        payload = self.payload()
        payload["data"]["table_cells"][0]["start_row_offset_idx"] = -1
        node = self.build({"source_table": payload})
        self.assertTrue(node.attributes["requires_review"])
        self.assertEqual(len(node.attributes["source_table"]["data"]["table_cells"]), 2)

    def test_empty_table_is_retained_for_review(self):
        """Empty extracted tables remain visible, not silently discarded."""
        node = self.build({"source_table": {"data": {"table_cells": []}}})
        self.assertTrue(node.attributes["requires_review"])

    def test_merger_retains_structured_tables(self):
        """Recovery merging keeps structured primary table payloads unchanged."""
        table = {"source_table": self.payload()}
        primary = ExtractionResult("docling", "primary", pages=[ExtractedPage(2, tables=[table])])
        merged = ExtractionResultMerger().merge(primary, ExtractionResult("native", "fallback"))
        self.assertEqual(merged.pages[0].tables, [table])

    def test_actual_docling_table_model(self):
        """Checks the installed Docling cell serialization without running inference."""
        from docling_core.types.doc import DoclingDocument
        from docling_core.types.doc.items.table.table_data import TableData
        document = DoclingDocument(name="test")
        table = document.add_table(data=TableData.model_validate(self.payload()["data"]))
        result = DoclingContentAdapter()._extract_table(table, document)
        self.assertNotIn("grid", result["source_table"]["data"])
        self.assertEqual(len(self.build(result).children), 2)

    def test_legacy_matrix_still_builds_cells(self):
        """Local fallback matrix contracts remain compatible."""
        node = self.build([["Material", "Temperature"], ["ASA", "90"]])
        self.assertEqual(node.children[1].children[1].text, "90")

    def test_matrix_table_header_has_cell_children(self):
        """PowerPoint tables keep the first row as searchable header cells."""
        node = self.build([
            ["Issue", "Description", "Date", "Owner"],
            ["01", "New Document", "31-12-2025", "Tulin Kale/Vishvajeet Mane"],
            ["", "", "", ""],
            ["", "", "", ""],
        ])
        header = node.children[0]
        self.assertEqual([cell.text for cell in header.children], ["Issue", "Description", "Date", "Owner"])
        self.assertTrue(all(cell.attributes["is_header"] for cell in header.children))
        self.assertEqual(len(node.children), 4)

    def test_multirow_headers_remain_explicit(self):
        """Every header cell retains its header label and source row."""
        payload = self.payload()
        payload["data"]["table_cells"][1]["column_header"] = True
        node = self.build({"source_table": payload})
        self.assertTrue(node.children[1].children[0].attributes["column_header"])
        self.assertEqual(node.children[1].attributes["row_index"], 2)

    def test_dictionary_export_compatibility(self):
        """Older adapters can expose dictionary export instead of model_dump."""
        table = Mock()
        table.model_dump.side_effect = AttributeError("unavailable")
        table.export_to_dict.return_value = self.payload()
        result = DoclingContentAdapter()._extract_table(table, None)
        self.assertEqual(result["source_table"], self.payload())
        table.export_to_dataframe.assert_not_called()

    def test_simple_native_table_does_not_duplicate_structured_table(self):
        """Matching text at matching coordinates is not appended a second time."""
        payload = self.payload()
        payload["data"]["table_cells"][0].update(col_span=1, end_col_offset_idx=1)
        primary = ExtractedPage(1, tables=[{"source_table": payload}])
        fallback = ExtractedPage(1, tables=[[["Temperature", ""], ["", "90"]]])
        ExtractionResultMerger()._merge_page(primary, fallback)
        self.assertEqual(len(primary.tables), 1)

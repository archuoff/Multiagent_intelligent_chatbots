from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import openpyxl

from new_ingestion.orchestration.batch_ingest import ingest_agent_folder


def _write_xlsx(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    sheet["A1"] = "Part"
    sheet["A2"] = value
    workbook.save(path)


class BatchIngestTests(unittest.TestCase):
    def test_ingests_a_new_file_and_persists_master_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_root, output_root = Path(tmp) / "data", Path(tmp) / "output"
            _write_xlsx(data_root / "test-agent" / "sample.xlsx", "Sensor A")

            result = ingest_agent_folder("test-agent", data_root=data_root, output_root=output_root)

            self.assertEqual(result.succeeded, ["sample.xlsx"])
            self.assertEqual(result.skipped, [])
            self.assertEqual(result.failed, [])
            document_dirs = [p for p in (output_root / "test-agent").iterdir() if p.is_dir()]
            self.assertEqual(len(document_dirs), 1)
            master = document_dirs[0] / "v1" / "master.json"
            self.assertTrue(master.is_file())
            self.assertEqual(json.loads(master.read_text(encoding="utf-8"))["document"]["source_file_name"], "sample.xlsx")

    def test_rerunning_unchanged_file_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_root, output_root = Path(tmp) / "data", Path(tmp) / "output"
            _write_xlsx(data_root / "test-agent" / "sample.xlsx", "Sensor A")

            ingest_agent_folder("test-agent", data_root=data_root, output_root=output_root)
            second = ingest_agent_folder("test-agent", data_root=data_root, output_root=output_root)

            self.assertEqual(second.succeeded, [])
            self.assertEqual(second.skipped, ["sample.xlsx"])

    def test_changed_file_creates_a_new_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_root, output_root = Path(tmp) / "data", Path(tmp) / "output"
            path = data_root / "test-agent" / "sample.xlsx"
            _write_xlsx(path, "Sensor A")
            ingest_agent_folder("test-agent", data_root=data_root, output_root=output_root)
            document_dir = next(p for p in (output_root / "test-agent").iterdir() if p.is_dir())

            _write_xlsx(path, "Sensor B")
            second = ingest_agent_folder("test-agent", data_root=data_root, output_root=output_root)

            self.assertEqual(second.succeeded, ["sample.xlsx"])
            self.assertTrue((document_dir / "v2" / "master.json").is_file())

    def test_recursive_scan_finds_files_in_subfolders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_root, output_root = Path(tmp) / "data", Path(tmp) / "output"
            _write_xlsx(data_root / "test-agent" / "sub" / "nested.xlsx", "Sensor C")

            result = ingest_agent_folder("test-agent", data_root=data_root, output_root=output_root)

            self.assertEqual(result.succeeded, ["sub/nested.xlsx"])

    def test_writes_an_index_mapping_document_id_to_source_filename(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_root, output_root = Path(tmp) / "data", Path(tmp) / "output"
            _write_xlsx(data_root / "test-agent" / "sample.xlsx", "Sensor A")

            ingest_agent_folder("test-agent", data_root=data_root, output_root=output_root)

            index_path = output_root / "test-agent" / "index.json"
            self.assertTrue(index_path.is_file())
            index = json.loads(index_path.read_text(encoding="utf-8"))
            document_id = next(iter(index))
            self.assertEqual(index[document_id]["source_file_name"], "sample.xlsx")
            self.assertEqual(index[document_id]["latest_version"], "v1")

    def test_index_reflects_the_newest_version_after_a_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_root, output_root = Path(tmp) / "data", Path(tmp) / "output"
            path = data_root / "test-agent" / "sample.xlsx"
            _write_xlsx(path, "Sensor A")
            ingest_agent_folder("test-agent", data_root=data_root, output_root=output_root)

            _write_xlsx(path, "Sensor B")
            ingest_agent_folder("test-agent", data_root=data_root, output_root=output_root)

            index = json.loads((output_root / "test-agent" / "index.json").read_text(encoding="utf-8"))
            document_id = next(iter(index))
            self.assertEqual(index[document_id]["latest_version"], "v2")

    def test_unsupported_file_is_recorded_as_failed_without_aborting_the_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            data_root, output_root = Path(tmp) / "data", Path(tmp) / "output"
            (data_root / "test-agent").mkdir(parents=True)
            (data_root / "test-agent" / "notes.txt").write_text("not a spreadsheet", encoding="utf-8")
            _write_xlsx(data_root / "test-agent" / "sample.xlsx", "Sensor A")

            result = ingest_agent_folder("test-agent", data_root=data_root, output_root=output_root)

            self.assertEqual(result.succeeded, ["sample.xlsx"])
            failed_paths = [relative_path for relative_path, _ in result.failed]
            self.assertEqual(failed_paths, ["notes.txt"])


if __name__ == "__main__":
    unittest.main()

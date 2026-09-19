"""Run bounded real-file ingestion checks and save a per-source evidence report.

Calls the adjacent validator in isolated subprocesses, then uses audit_extraction
for native text coverage. Unsupported files and failures stay in the denominator.
Generated artifacts remain separate from source code and production storage.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import zipfile
import xml.etree.ElementTree as ET

from audit_extraction import canonical_text, source_text, tokens

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from backend.ingestion.extraction.timeout_policy import PdfTimeoutPolicy

SUPPORTED = {".pdf", ".docx", ".pptx", ".xlsx"}


def walk_nodes(document: dict) -> list[dict]:
    """Enumerates the canonical hierarchy for independent structural counts."""
    nodes = []
    pending = list(document["root_nodes"])
    while pending:
        node = pending.pop()
        nodes.append(node)
        pending.extend(node.get("children", []))
    return nodes


def structure_check(source: Path, document: dict) -> dict:
    """Compares native page/slide/sheet counts and Office table counts."""
    nodes = walk_nodes(document)
    source_tables = None
    if source.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        native_count, kind = len(PdfReader(source).pages), "page"
    else:
        with zipfile.ZipFile(source) as archive:
            if source.suffix.lower() == ".xlsx":
                root = ET.fromstring(archive.read("xl/workbook.xml"))
                native_count = sum(element.tag.endswith("}sheet") for element in root.iter())
                kind = "sheet"
            elif source.suffix.lower() == ".pptx":
                root = ET.fromstring(archive.read("ppt/presentation.xml"))
                native_count = sum(element.tag.endswith("}sldId") for element in root.iter())
                kind = "slide"
                import re
                names = [name for name in archive.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", name)]
                source_tables = sum(element.tag.endswith("}tbl") for name in names for element in ET.fromstring(archive.read(name)).iter())
            else:
                native_count, kind = None, "section"
                root = ET.fromstring(archive.read("word/document.xml"))
                source_tables = sum(element.tag.endswith("}tbl") for element in root.iter())
    canonical_count = sum(node["node_type"] == kind for node in nodes)
    return {
        "unit": kind, "source_count": native_count, "canonical_count": canonical_count,
        "count_matches": canonical_count == native_count if native_count is not None else None,
        "source_tables": source_tables,
        "canonical_tables": sum(node["node_type"] == "table" for node in nodes),
        "limitation": "Counts do not verify reading order, cell alignment, or image interpretation.",
    }


def evaluate(source: Path, data_root: Path, output: Path, timeout: int | None) -> dict:
    """Runs one parser with a deadline and retains its logs, JSON and audit evidence."""
    relative = source.relative_to(data_root).as_posix()
    key = hashlib.sha256(relative.encode()).hexdigest()[:16]
    result = {"source": relative, "bytes": source.stat().st_size, "format": source.suffix.lower(), "status": "unsupported"}
    if source.suffix.lower() not in SUPPORTED:
        result["reason"] = "No registered parser for this extension; no format conversion attempted."
        return result
    folder = output / "artifacts" / key
    folder.mkdir(parents=True, exist_ok=True)
    artifact = folder / "master.json"
    command = [sys.executable, str(Path(__file__).with_name("validate_ingestion.py")), str(source), "--source-type", source.suffix.lower()[1:], "--output", str(artifact)]
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    started = time.monotonic()
    try:
        if timeout is None:
            timeout = 240
            if source.suffix.lower() == ".pdf":
                from pypdf import PdfReader
                with source.open("rb") as stream:
                    page_count = len(PdfReader(stream).pages)
                timeout = PdfTimeoutPolicy().process_timeout(page_count, os.getenv("JLR_DOCLING_OCR", "false").lower() == "true")
        result["process_timeout_seconds"] = timeout
        with (folder / "execution.log").open("w", encoding="utf-8") as log:
            completed = subprocess.run(command, cwd=PROJECT_ROOT, env=environment, stdout=log, stderr=log, timeout=timeout)
        result["exit_code"] = completed.returncode
        if not artifact.is_file():
            result.update(status="failed", reason="Parser did not produce canonical JSON; inspect execution.log.")
            return result
        document = json.loads(artifact.read_text(encoding="utf-8"))
        result.update(parser=document["parser_info"], warnings=document["quality"]["warnings"], errors=document["quality"]["errors"], canonical_nodes=len(walk_nodes(document)), artifact=artifact.relative_to(output).as_posix())
        result["status"] = "parsed" if completed.returncode == 0 else "validation_failed"
        expected, actual = tokens(source_text(source)), tokens(canonical_text(document))
        total, covered = sum(expected.values()), sum((expected & actual).values())
        result["coverage"] = {"native_tokens": total, "covered_tokens": covered, "native_text_token_coverage": covered / total if total else None, "missing_token_examples": (expected - actual).most_common(15)}
        result["structure"] = structure_check(source, document)
        result["review_required"] = bool(result["warnings"] or result["errors"] or not total or covered / total < 0.9 or result["structure"]["count_matches"] is False or (result["structure"]["source_tables"] is not None and result["structure"]["source_tables"] != result["structure"]["canonical_tables"]))
    except subprocess.TimeoutExpired:
        result.update(status="timeout", reason=f"Extraction exceeded {timeout} seconds.")
    except Exception as error:
        result.update(status="audit_failed" if artifact.exists() else "failed", reason=f"{type(error).__name__}: {error}")
    finally:
        result["elapsed_seconds"] = round(time.monotonic() - started, 2)
        result["log"] = (folder / "execution.log").relative_to(output).as_posix()
        if artifact.is_file():
            result["artifact"] = artifact.relative_to(output).as_posix()
    return result


def save_report(results: list[dict], output: Path, settings: dict) -> None:
    """Saves machine-readable evidence and a concise human-readable report."""
    rows = sorted(results, key=lambda result: result["source"])
    payload = {"created_at": datetime.now(timezone.utc).isoformat(), "settings": settings, "results": rows,
               "limitation": "Native token coverage is not overall extraction accuracy; repeated content can mask missing blocks. OCR, images, table values/alignment and reading order need labelled review."}
    (output / "report.json").write_text(json.dumps(payload, indent=2, ensure_ascii=True), encoding="utf-8")
    lines = ["# JLR Corpus Extraction Validation", "", payload["limitation"], "", f"Files inventoried: {len(rows)}", "", "| Source | Status | Native text coverage | Review |", "| --- | --- | --- | --- |"]
    for row in rows:
        coverage = row.get("coverage", {}).get("native_text_token_coverage")
        percentage = f"{coverage:.1%}" if coverage is not None else "Not measured"
        source = row["source"].replace("|", "\\|")
        lines.append(f"| {source} | {row['status']} | {percentage} | {'Yes' if row.get('review_required', True) else 'Manual fidelity review'} |")
    lines.extend(["", "See report.json for warnings, missing tokens, source/canonical counts, and artifact/log paths."])
    (output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def reaudit_report(report_path: Path) -> int:
    """Recomputes audit metrics from saved artifacts without repeating extraction."""
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    data_root = Path(payload["settings"]["data"])
    output = report_path.parent
    for result in payload["results"]:
        if result.get("artifact") is None and result.get("log"):
            candidate = (output / result["log"]).with_name("master.json")
            if candidate.is_file():
                result["artifact"] = candidate.relative_to(output).as_posix()
        if result.get("artifact") is None:
            continue
        source = data_root / result["source"]
        try:
            document = json.loads((output / result["artifact"]).read_text(encoding="utf-8"))
            result.update(parser=document["parser_info"], warnings=document["quality"]["warnings"], errors=document["quality"]["errors"])
            if result["status"] == "timeout":
                result["timeout_note"] = "Canonical JSON was written before the process deadline, but the validator did not exit in time. Artifact audits do not change timeout status."
            with source.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != document["source_hash"]:
                raise ValueError("Source changed since extraction; rerun extraction before auditing.")
            expected, actual = tokens(source_text(source)), tokens(canonical_text(document))
            total, covered = sum(expected.values()), sum((expected & actual).values())
            result["coverage"] = {"native_tokens": total, "covered_tokens": covered, "native_text_token_coverage": covered / total if total else None, "missing_token_examples": (expected - actual).most_common(15)}
            result["structure"] = structure_check(source, document)
            result["review_required"] = bool(result["status"] != "parsed" or result["warnings"] or result["errors"] or not total or covered / total < 0.9 or result["structure"]["count_matches"] is False or (result["structure"]["source_tables"] is not None and result["structure"]["source_tables"] != result["structure"]["canonical_tables"]))
        except Exception as error:
            result.pop("coverage", None)
            result.update(status="audit_failed", review_required=True, reason=f"{type(error).__name__}: {error}")
    payload["settings"]["audit_baseline"] = "paragraph-aware-native-text-v2"
    save_report(payload["results"], output, payload["settings"])
    print(f"Re-audited {len(payload['results'])} inventory entries: {report_path}")
    return 1 if any(row["status"] == "audit_failed" for row in payload["results"]) else 0


def main() -> int:
    """Inventories every file and evaluates supported sources with bounded concurrency."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=PROJECT_ROOT / "data")
    parser.add_argument("--output", type=Path, default=PROJECT_ROOT / "output" / "corpus-validation")
    parser.add_argument("--timeout", type=int, default=None, help="Explicit process deadline override; default allows the PDF dynamic budget and retry, or 240s for other formats.")
    parser.add_argument("--workers", type=int, choices=[1, 2], default=1)
    parser.add_argument("--reaudit", type=Path, help="Recompute audit metrics for an existing report.json without parsing again.")
    args = parser.parse_args()
    if args.reaudit:
        return reaudit_report(args.reaudit.resolve())
    if (args.timeout is not None and args.timeout < 1) or not args.data.is_dir():
        parser.error("Provide an existing data directory and a positive timeout.")
    data_root, output = args.data.resolve(), args.output.resolve()
    if output == data_root or data_root in output.parents:
        parser.error("Output must be outside the source directory.")
    output.mkdir(parents=True, exist_ok=True)
    sources = sorted(path for path in data_root.rglob("*") if path.is_file())
    # Separate runs so a previous artifact cannot be mistaken for a new success.
    output = output / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output.mkdir()
    settings = {"data": str(data_root), "timeout_seconds": args.timeout, "workers": args.workers, "ocr": os.getenv("JLR_DOCLING_OCR", "false"), "source_count": len(sources)}
    results = []
    print(f"Report directory: {output}", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(evaluate, source, data_root, output, args.timeout) for source in sources]
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            save_report(results, output, settings)
            print(f"[{len(results)}/{len(sources)}] {result['status']}: {result['source']}", flush=True)
    return 1 if any(row["status"] != "parsed" for row in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())

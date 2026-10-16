"""Connect format detection, extraction, and canonical normalization.

This module intentionally stops at a validated ``CanonicalDocument``. It does
not perform idempotency, deduplication, versioning, lifecycle, activation, or
persistence decisions.
"""

from __future__ import annotations

import argparse
import dataclasses
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from new_ingestion.extraction.excel_extractor import extract_xlsx
from new_ingestion.extraction.format_detector import (
    DocumentFormat, detect_format,
)
from new_ingestion.extraction.pdf_extractor import extract_pdf
from new_ingestion.extraction.pptx_extractor import extract_pptx
from new_ingestion.injestion_control.hashing import hash_file
from new_ingestion.normalizers.pdf_normalizer import normalize_pdf
from new_ingestion.normalizers.pptx_normalizer import normalize_pptx
from new_ingestion.normalizers.xlsx_normalizer import normalize_xlsx
from new_ingestion.schema.schema_define import CanonicalDocument


class OrchestrationStatus(StrEnum):
    SUCCESS = "success"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class OrchestrationResult:
    status: OrchestrationStatus
    source_path: Path
    detected_format: DocumentFormat
    source_file_hash: str | None = None
    raw_result: Any | None = None
    canonical_document: CanonicalDocument | None = None
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe summary without exposing binary data or addresses."""
        return {
            "status": self.status.value,
            "source_path": str(self.source_path),
            "detected_format": self.detected_format.value,
            "source_file_hash": self.source_file_hash,
            "raw_result": _json_safe(self.raw_result),
            "canonical_document": (
                self.canonical_document.model_dump(mode="json")
                if self.canonical_document is not None else None
            ),
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (Path,)):
        return str(value)
    if isinstance(value, StrEnum):
        return value.value
    if isinstance(value, bytes):
        return None
    if hasattr(value, "model_dump"):
        try:
            return _json_safe(value.model_dump(mode="json"))
        except Exception:
            return {"type": f"{type(value).__module__}.{type(value).__qualname__}"}
    if hasattr(value, "to_dict"):
        try:
            return _json_safe(value.to_dict())
        except Exception:
            return {"type": f"{type(value).__module__}.{type(value).__qualname__}"}
    if dataclasses.is_dataclass(value):
        return _json_safe(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    # Avoid repr(value), which commonly contains a memory address.
    return {"type": f"{type(value).__module__}.{type(value).__qualname__}"}


def _append_unique(target: list[str], messages: Any, prefix: str | None = None) -> None:
    for message in messages or ():
        text = str(message)
        if prefix and not text.startswith(prefix):
            text = f"{prefix}: {text}"
        if text not in target:
            target.append(text)


def _failed(path: Path, detected: DocumentFormat, error: Exception | str,
            source_hash: str | None = None, raw: Any = None) -> OrchestrationResult:
    return OrchestrationResult(
        status=OrchestrationStatus.FAILED,
        source_path=path,
        detected_format=detected,
        source_file_hash=source_hash,
        raw_result=raw,
        errors=(str(error),),
    )


def ingest_to_canonical(
    source_path: str | Path,
    *,
    document_id: str,
    version_id: str,
    logical_document_key: str | None = None,
    artifact_dir: str | Path | None = None,
) -> OrchestrationResult:
    """Run format detection, extraction, and canonical normalization."""
    path = Path(source_path).expanduser()
    try:
        if not path.exists():
            return _failed(path, DocumentFormat.UNKNOWN, f"source path does not exist: {path}")
        if not path.is_file():
            return _failed(path, DocumentFormat.UNKNOWN, f"source path is not a regular file: {path}")
        detection = detect_format(path)
    except Exception as error:
        return _failed(path, DocumentFormat.UNKNOWN, f"format detection failed: {error}")

    detected = detection.format
    if detected is DocumentFormat.UNKNOWN:
        return OrchestrationResult(
            status=OrchestrationStatus.UNSUPPORTED, source_path=path,
            detected_format=detected, errors=("unsupported document format",),
        )
    if detected is DocumentFormat.DOCX:
        return OrchestrationResult(
            status=OrchestrationStatus.UNSUPPORTED, source_path=path,
            detected_format=detected, errors=("DOCX extraction is not implemented yet",),
        )

    try:
        source_hash = hash_file(path)
    except Exception as error:
        return _failed(path, detected, f"source hashing failed: {error}")

    extractors: dict[DocumentFormat, Callable[..., Any]] = {
        DocumentFormat.PDF: extract_pdf,
        DocumentFormat.PPTX: extract_pptx,
        DocumentFormat.XLSX: extract_xlsx,
    }
    normalizers: dict[DocumentFormat, Callable[..., CanonicalDocument]] = {
        DocumentFormat.PDF: normalize_pdf,
        DocumentFormat.PPTX: normalize_pptx,
        DocumentFormat.XLSX: normalize_xlsx,
    }
    extractor = extractors[detected]
    try:
        raw_result = extractor(path, artifact_dir=artifact_dir)
    except Exception as error:
        return _failed(path, detected, f"{detected.value.upper()} extraction failed: {error}", source_hash)

    extraction_warnings: list[str] = []
    _append_unique(extraction_warnings, getattr(raw_result, "warnings", ()), "extraction_warning")
    extraction_errors: list[str] = []
    _append_unique(extraction_errors, getattr(raw_result, "errors", ()), "extraction_error")
    try:
        normalizer_kwargs = {
            "document_id": document_id,
            "version_id": version_id,
            "source_file_hash": source_hash,
            "logical_document_key": logical_document_key,
        }
        if detected is DocumentFormat.XLSX:
            normalizer_kwargs["artifact_dir"] = artifact_dir
        canonical = normalizers[detected](raw_result, **normalizer_kwargs)
        if not isinstance(canonical, CanonicalDocument):
            raise TypeError("normalizer did not return a CanonicalDocument")
        canonical = CanonicalDocument.model_validate(canonical.model_dump(mode="python"))
    except Exception as error:
        return _failed(path, detected, f"{detected.value.upper()} normalization failed: {error}", source_hash, raw_result)

    warnings: list[str] = []
    errors: list[str] = []
    _append_unique(warnings, extraction_warnings)
    _append_unique(errors, extraction_errors)
    _append_unique(warnings, canonical.diagnostics.warnings)
    _append_unique(errors, canonical.diagnostics.errors)
    status = OrchestrationStatus.PARTIAL if warnings or errors else OrchestrationStatus.SUCCESS
    return OrchestrationResult(
        status=status, source_path=path, detected_format=detected,
        source_file_hash=source_hash, raw_result=raw_result,
        canonical_document=canonical, warnings=tuple(warnings), errors=tuple(errors),
    )


def _main() -> int:
    parser = argparse.ArgumentParser(description="Run lightweight canonical ingestion.")
    parser.add_argument("source_file", type=Path)
    parser.add_argument("--document-id", default="smoke-document")
    parser.add_argument("--version-id", default="smoke-version")
    args = parser.parse_args()
    result = ingest_to_canonical(args.source_file, document_id=args.document_id, version_id=args.version_id)
    print(f"status={result.status.value}")
    print(f"detected_format={result.detected_format.value}")
    print(f"source_hash={result.source_file_hash}")
    print(f"canonical_document_available={result.canonical_document is not None}")
    print(f"warning_count={len(result.warnings)}")
    print(f"error_count={len(result.errors)}")
    return 0 if result.status is not OrchestrationStatus.FAILED else 1


if __name__ == "__main__":
    raise SystemExit(_main())


__all__ = ["OrchestrationResult", "OrchestrationStatus", "ingest_to_canonical"]

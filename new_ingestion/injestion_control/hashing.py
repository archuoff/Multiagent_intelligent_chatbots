"""Deterministic hashing and identity helpers for ingestion control.

This module is deliberately side-effect free apart from reading a file in
``hash_file``. It does not persist hashes, generate random IDs, or know about
the canonical schema implementation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from pathlib import Path
from typing import Any, Mapping


class HashingError(ValueError):
    """Raised when hashing input cannot be represented deterministically."""


_VOLATILE_FIELD_NAMES = frozenset(
    {
        "timestamp",
        "timestamps",
        "created_at",
        "updated_at",
        "extracted_at",
        "processed_at",
        "processing_timestamp",
        "run_id",
        "execution_id",
        "request_id",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def hash_bytes(data: bytes) -> str:
    """Return the lowercase SHA-256 digest of ``data``."""
    if not isinstance(data, bytes):
        raise TypeError(f"data must be bytes, got {type(data).__name__}")
    return hashlib.sha256(data).hexdigest()


def hash_file(path: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """Hash a file incrementally with SHA-256.

    ``chunk_size`` controls read-buffer size and must be a positive integer.
    """
    if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size <= 0:
        raise ValueError("chunk_size must be a positive integer")
    file_path = Path(path).expanduser()
    if not file_path.exists():
        raise FileNotFoundError(file_path)
    if not file_path.is_file():
        raise ValueError(f"Path is not a regular file: {file_path}")
    digest = hashlib.sha256()
    try:
        with file_path.open("rb") as stream:
            while chunk := stream.read(chunk_size):
                digest.update(chunk)
    except OSError as error:
        raise HashingError(f"Could not read file for hashing: {file_path}") from error
    return digest.hexdigest()


def _is_volatile_key(key: str, volatile_fields: frozenset[str]) -> bool:
    return key.casefold() in volatile_fields


def _canonical_value(value: Any, volatile_fields: frozenset[str], location: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise HashingError(f"Unsupported non-finite number at {location}")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise HashingError(
                    f"JSON object keys must be strings at {location}; got {type(key).__name__}"
                )
            if _is_volatile_key(key, volatile_fields):
                continue
            result[key] = _canonical_value(item, volatile_fields, f"{location}.{key}")
        return {key: result[key] for key in sorted(result)}
    if isinstance(value, list):
        return [
            _canonical_value(item, volatile_fields, f"{location}[{index}]")
            for index, item in enumerate(value)
        ]
    raise HashingError(
        f"Unsupported value at {location}: {type(value).__name__}; "
        "expected JSON-compatible values"
    )


def canonicalize_content(
    content: Any,
    *,
    volatile_fields: set[str] | frozenset[str] | None = None,
) -> Any:
    """Return a deterministic JSON-compatible representation of ``content``.

    Dictionary keys are sorted recursively. List order is preserved. Known
    volatile fields are removed by name, case-insensitively, before hashing.
    """
    selected_fields = _VOLATILE_FIELD_NAMES if volatile_fields is None else volatile_fields
    field_names = frozenset(name.casefold() for name in selected_fields)
    return _canonical_value(content, field_names, "$")


def canonical_json_bytes(
    content: Any,
    *,
    volatile_fields: set[str] | frozenset[str] | None = None,
) -> bytes:
    """Serialize content as compact, sorted, UTF-8 JSON bytes."""
    canonical = canonicalize_content(content, volatile_fields=volatile_fields)
    try:
        return json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise HashingError(f"Content could not be serialized as canonical JSON: {error}") from error


def canonical_json(
    content: Any,
    *,
    volatile_fields: set[str] | frozenset[str] | None = None,
) -> str:
    """Return compact canonical JSON text encoded as a Python string."""
    return canonical_json_bytes(content, volatile_fields=volatile_fields).decode("utf-8")


def hash_normalized_content(content: Any) -> str:
    """Canonicalize content and return its lowercase SHA-256 hash."""
    return hash_bytes(canonical_json_bytes(content))


def processing_fingerprint(
    source_file_hash: str,
    extractor_name: str,
    extractor_version: str,
    extractor_configuration: Any,
    canonical_schema_version: str,
) -> str:
    """Return a deterministic fingerprint for one processing configuration."""
    for name, value in (
        ("source_file_hash", source_file_hash),
        ("extractor_name", extractor_name),
        ("extractor_version", extractor_version),
        ("canonical_schema_version", canonical_schema_version),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string")
    if not _SHA256_RE.fullmatch(source_file_hash):
        raise ValueError("source_file_hash must be a 64-character hexadecimal SHA-256 hash")
    payload = {
        "source_file_hash": source_file_hash.lower(),
        "extractor_name": extractor_name,
        "extractor_version": extractor_version,
        "extractor_configuration": extractor_configuration,
        "canonical_schema_version": canonical_schema_version,
    }
    return hash_bytes(canonical_json_bytes(payload))


def deterministic_uuid5(namespace: uuid.UUID | str, stable_key: str) -> uuid.UUID:
    """Generate a repeatable UUID5 from a namespace and stable key."""
    if isinstance(namespace, str):
        try:
            namespace = uuid.UUID(namespace)
        except ValueError as error:
            raise ValueError(f"namespace is not a valid UUID: {namespace!r}") from error
    if not isinstance(namespace, uuid.UUID):
        raise TypeError("namespace must be a uuid.UUID or UUID string")
    if not isinstance(stable_key, str) or not stable_key:
        raise ValueError("stable_key must be a non-empty string")
    return uuid.uuid5(namespace, stable_key)


deterministic_uuid = deterministic_uuid5


__all__ = [
    "HashingError",
    "canonical_json",
    "canonical_json_bytes",
    "canonicalize_content",
    "deterministic_uuid",
    "deterministic_uuid5",
    "hash_bytes",
    "hash_file",
    "hash_normalized_content",
    "processing_fingerprint",
]

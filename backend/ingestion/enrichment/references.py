"""Local-only source reference normalization and validation for canonical nodes.

Remote URLs are never fetched during ingestion. They are marked unverified
unless their host is explicitly trusted. Local paths are checked only where the
ingestion process can access them, making this safe for internal environments.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Iterable
from urllib.parse import unquote, urlparse, urlunparse

from backend.ingestion.enrichment.contracts import ReferenceRecord
from backend.ingestion.models import CanonicalDocument
from backend.ingestion.models import CanonicalNode


class ReferenceRegistry:
    """Finds and normalizes references without treating arbitrary text as approved evidence."""

    _URL_PATTERN = re.compile(r"(?:https?://|file://)[^\s<>\"]+", re.IGNORECASE)

    def __init__(self, trusted_hosts: Iterable[str] = ()) -> None:
        """Accepts only explicitly configured remote hosts as answer-eligible references."""
        self._trusted_hosts = {host.casefold() for host in trusted_hosts}

    def apply(self, document: CanonicalDocument) -> list[ReferenceRecord]:
        """Stores per-node references and a document-level registry for answer citations."""
        records: list[ReferenceRecord] = []
        for root in document.root_nodes:
            for node in self._walk(root):
                node_records = self._extract_node_references(node)
                if node_records:
                    node.attributes["references"] = [record.model_dump(mode="json") for record in node_records]
                    records.extend(node_records)
        if document.root_nodes:
            document.root_nodes[0].attributes["reference_registry"] = [record.model_dump(mode="json") for record in records]
        return records

    def _extract_node_references(self, node: CanonicalNode) -> list[ReferenceRecord]:
        """Extracts URL-looking values from narrative text and structured cell values."""
        candidates = [node.text or ""]
        value = node.attributes.get("value")
        if isinstance(value, str):
            candidates.append(value)
        records = [self._record(node.node_id, candidate) for text in candidates for candidate in self._URL_PATTERN.findall(text)]
        policy = node.attributes.get("field_policy", {})
        if isinstance(value, str) and isinstance(policy, dict) and policy.get("use") == "reference_link":
            records.append(self._record(node.node_id, value))
        return list({record.reference_id: record for record in records}.values())

    def _record(self, node_id: str, raw_value: str) -> ReferenceRecord:
        """Classifies a syntactically recognized reference without connecting to it."""
        raw = raw_value.rstrip(".,;:)")
        if self._looks_like_windows_path(raw):
            return self._local_path_record(node_id, raw)
        parsed = urlparse(raw)
        if parsed.scheme not in {"http", "https", "file"}:
            return self._invalid_record(node_id, raw)
        normalized = urlunparse((parsed.scheme.casefold(), parsed.netloc.casefold(), parsed.path, "", parsed.query, ""))
        reference_type = "url" if parsed.scheme in {"http", "https"} else "file"
        extension = Path(unquote(parsed.path)).suffix.lower() or None
        if parsed.scheme in {"http", "https"}:
            trusted = parsed.hostname is not None and parsed.hostname.casefold() in self._trusted_hosts
            status = "trusted_remote" if trusted else "unverified_remote"
            eligible = trusted
        else:
            local_path = self._file_uri_path(parsed)
            exists = local_path.is_file()
            status = "available_local" if exists else "missing_local"
            eligible = exists
        identity = f"{node_id}|{normalized}"
        return ReferenceRecord(reference_id=f"ref_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}",
            source_node_id=node_id, raw_value=raw, normalized_uri=normalized, reference_type=reference_type,
            status=status, answer_eligible=eligible, file_extension=extension)

    def _local_path_record(self, node_id: str, raw: str) -> ReferenceRecord:
        """Registers local Windows paths without probing network shares during ingestion."""
        is_network = raw.startswith("\\\\")
        path = Path(raw)
        exists = False if is_network else path.is_file()
        status = "unverified_network_path" if is_network else "available_local" if exists else "missing_local"
        normalized = f"file:///{path.as_posix()}"
        identity = f"{node_id}|{normalized}"
        return ReferenceRecord(reference_id=f"ref_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}",
            source_node_id=node_id, raw_value=raw, normalized_uri=normalized, reference_type="file", status=status,
            answer_eligible=exists, file_extension=path.suffix.lower() or None)

    def _invalid_record(self, node_id: str, raw: str) -> ReferenceRecord:
        """Keeps malformed values visible when a source field claims to be a reference."""
        identity = f"{node_id}|{raw}"
        return ReferenceRecord(reference_id=f"ref_{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:20]}",
            source_node_id=node_id, raw_value=raw, reference_type="unknown", status="invalid",
            answer_eligible=False, file_extension=Path(raw).suffix.lower() or None)

    def _looks_like_windows_path(self, value: str) -> bool:
        """Recognizes drive and UNC paths before URL parsing mistakes a drive letter for a scheme."""
        return bool(re.match(r"^[A-Za-z]:[\\/]", value)) or value.startswith("\\\\")

    def _file_uri_path(self, parsed) -> Path:
        """Converts a file URI into a Windows-compatible local path for existence checking."""
        path = unquote(parsed.path)
        if re.match(r"^/[A-Za-z]:/", path):
            path = path[1:]
        return Path(path)

    def _walk(self, node):
        """Yields a canonical subtree in source order for deterministic registry output."""
        yield node
        for child in node.children:
            yield from self._walk(child)

"""Local DOCX rendering helpers.

The renderer is intentionally optional: ingestion keeps native DOCX structure even
when Microsoft Word is unavailable. When available, Word COM exports a DOCX to a
local PDF that later layout/crop enrichment can use for page images and VLM
fallbacks.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any


@dataclass(slots=True)
class DocxRenderResult:
    """Records the local rendered artifact produced for a DOCX."""

    status: str
    renderer: str
    pdf_path: str | None = None
    error_type: str | None = None
    warning: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Returns compact JSON-serializable render metadata."""
        payload = {"status": self.status, "renderer": self.renderer}
        if self.pdf_path:
            payload["pdf_path"] = self.pdf_path
        if self.error_type:
            payload["error_type"] = self.error_type
        if self.warning:
            payload["warning"] = self.warning
        return payload


class WordComDocxRenderer:
    """Renders DOCX to PDF locally using Microsoft Word COM on Windows."""

    def __init__(self, output_root: Path | str = Path("storage") / "rendered") -> None:
        self._output_root = Path(output_root)

    def render_pdf(self, source_path: Path, agent_id: str, document_id: str, version: str) -> DocxRenderResult:
        """Exports one DOCX to a deterministic local PDF path."""
        try:
            import win32com.client
        except ImportError:
            return DocxRenderResult(status="unavailable", renderer="word_com", warning="pywin32/win32com is not installed.")
        folder = self._output_root / self._safe_component(agent_id) / self._safe_component(document_id) / self._safe_component(version)
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{self._safe_component(source_path.stem)}.pdf"
        app = None
        document = None
        try:
            app = win32com.client.DispatchEx("Word.Application")
            app.Visible = False
            app.DisplayAlerts = 0
            document = app.Documents.Open(str(source_path.resolve()), ReadOnly=True, AddToRecentFiles=False, Visible=False)
            # 17 is wdExportFormatPDF. Numeric constant avoids depending on generated COM constants.
            document.ExportAsFixedFormat(str(target.resolve()), 17)
            return DocxRenderResult(status="success", renderer="word_com", pdf_path=target.as_posix())
        except Exception as error:
            return DocxRenderResult(status="failed", renderer="word_com", error_type=type(error).__name__, warning=str(error)[:240])
        finally:
            if document is not None:
                try:
                    document.Close(False)
                except Exception:
                    pass
            if app is not None:
                try:
                    app.Quit()
                except Exception:
                    pass

    def _safe_component(self, value: str) -> str:
        """Creates deterministic path components for rendered artifacts."""
        cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
        return cleaned or "unknown"

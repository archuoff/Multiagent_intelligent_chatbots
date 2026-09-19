"""Page-based PDF processing budgets shared by Docling and validation tools.

These defaults are tuning estimates, not performance guarantees. Docling's
timeout is cooperative; the corpus runner supplies a separate process deadline.
The caption can be displayed by a future upload UI or the current CLI.
"""

from dataclasses import dataclass


PDF_PROCESSING_CAPTION = (
    "Processing time adjusts automatically based on page count and whether OCR "
    "is enabled. Larger or scanned documents may take longer."
)


@dataclass(frozen=True, slots=True)
class PdfTimeoutPolicy:
    """Defines bounded initial and retry allowances for one PDF conversion."""

    base_seconds: int = 60
    seconds_per_page: int = 6
    ocr_seconds_per_page: int = 12
    minimum_seconds: int = 120
    maximum_seconds: int = 900
    total_budget_seconds: int = 1800

    def __post_init__(self) -> None:
        """Rejects inconsistent limits before a conversion starts."""
        values = (self.base_seconds, self.seconds_per_page, self.ocr_seconds_per_page,
                  self.minimum_seconds, self.maximum_seconds, self.total_budget_seconds)
        if any(value <= 0 for value in values) or not self.minimum_seconds <= self.maximum_seconds <= self.total_budget_seconds:
            raise ValueError("PDF timeout values must be positive and min <= max <= total budget.")

    def initial_timeout(self, page_count: int, ocr_enabled: bool) -> int:
        """Estimates the first attempt from page count and OCR cost."""
        if page_count < 1:
            raise ValueError("A PDF must contain at least one page.")
        rate = self.ocr_seconds_per_page if ocr_enabled else self.seconds_per_page
        return min(self.maximum_seconds, max(self.minimum_seconds, self.base_seconds + page_count * rate))

    def retry_timeout(self, previous: int, elapsed: float) -> int | None:
        """Allows one larger attempt only when sufficient total budget remains."""
        remaining = max(0, int(self.total_budget_seconds - elapsed))
        allowance = min(self.maximum_seconds, remaining, max(300, previous * 2))
        return allowance if allowance > previous else None

    def process_timeout(self, page_count: int, ocr_enabled: bool) -> int:
        """Budgets both attempts plus setup/shutdown grace for an outer watchdog."""
        first = self.initial_timeout(page_count, ocr_enabled)
        retry = self.retry_timeout(first, elapsed=first) or 0
        return min(self.total_budget_seconds, first + retry) + 120

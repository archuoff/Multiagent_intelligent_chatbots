"""Deterministic reconciliation and quality-first merger for extraction results.

The merger compares Docling output with local recovery output page by page,
chooses the stronger page as the base, and then appends non-duplicate evidence
from the other extractor. Conflicts remain explicit so unsafe content cannot
quietly move into chunking.
"""

from __future__ import annotations

from typing import Any

from backend.ingestion.extraction.contracts import ExtractedPage
from backend.ingestion.extraction.contracts import ExtractionResult
from backend.ingestion.extraction.reconciliation import ExtractionReconciler
from backend.ingestion.utils import normalize_text


class ExtractionResultMerger:
    """Merges complementary extraction results without using an LLM."""

    _MIN_TEXT_GAIN_TO_REPLACE = 1.3

    # This function merges page-level content by quality, not by extractor priority.
    def merge(self, primary: ExtractionResult, fallback: ExtractionResult) -> ExtractionResult:
        primary_pages = {page.number: page for page in primary.pages}
        fallback_pages = {page.number: page for page in fallback.pages}
        pages: dict[int, ExtractedPage] = {}
        reconciliation: list[dict[str, Any]] = []
        selected_fallback_pages = 0

        for page_number in sorted(set(primary_pages) | set(fallback_pages)):
            primary_page = primary_pages.get(page_number)
            fallback_page = fallback_pages.get(page_number)
            if primary_page is None and fallback_page is not None:
                page = self._copy_page(fallback_page)
                event = self._source_event(page_number, "fallback", {}, self._page_score(fallback_page))
                page.reconciliation.append(event)
                reconciliation.append(event)
                pages[page_number] = page
                selected_fallback_pages += 1
                continue
            if fallback_page is None and primary_page is not None:
                page = self._copy_page(primary_page)
                event = self._source_event(page_number, "primary", self._page_score(primary_page), {})
                page.reconciliation.append(event)
                reconciliation.append(event)
                pages[page_number] = page
                continue
            if primary_page is None or fallback_page is None:
                continue

            base, recovery, selected_role, recovery_role, primary_score, fallback_score = self._select_base_page(primary_page, fallback_page)
            if selected_role == "fallback":
                selected_fallback_pages += 1
            page = self._copy_page(base)
            event = self._source_event(page_number, selected_role, primary_score, fallback_score)
            page.reconciliation.append(event)
            reconciliation.append(event)
            reconciliation.extend(self._merge_page(page, recovery, recovery_role=recovery_role))
            pages[page_number] = page

        conflicts = [event for event in reconciliation if event["decision"] == "conflict"]
        processing_details = dict(primary.processing_details)
        if reconciliation:
            processing_details["reconciliation"] = {"version": "quality-rules-v2", "events": reconciliation,
                "summary": self._summary(reconciliation)}
        if selected_fallback_pages:
            processing_details["quality_recovery"] = {
                "selected_fallback_pages": selected_fallback_pages,
                "reason": "Fallback extraction scored higher for one or more pages or slides.",
            }
        requires_review = self._requires_review(primary, fallback, conflicts, selected_fallback_pages)
        return ExtractionResult(
            extractor_name=f"{primary.extractor_name}+{fallback.extractor_name}",
            extraction_mode="quality_reconciled",
            pages=[pages[number] for number in sorted(pages)],
            markdown=primary.markdown or fallback.markdown,
            exported=primary.exported or fallback.exported,
            warnings=[*primary.warnings, *fallback.warnings, *[f"Reconciliation conflict on {event['content_type']} {event['location']}; review retained evidence." for event in conflicts]],
            requires_review=requires_review,
            processing_details=processing_details,
        )

    # This function creates an independent page copy so merging never mutates a source result.
    def _copy_page(self, page: ExtractedPage) -> ExtractedPage:
        return ExtractedPage(
            number=page.number,
            text=page.text,
            text_blocks=[*page.text_blocks],
            tables=[*page.tables],
            images=[*page.images],
            notes=page.notes,
            reconciliation=[*page.reconciliation],
        )

    def _select_base_page(
        self,
        primary: ExtractedPage,
        fallback: ExtractedPage,
    ) -> tuple[ExtractedPage, ExtractedPage, str, str, dict[str, Any], dict[str, Any]]:
        """Chooses the page with stronger recoverable content as the merge base."""
        primary_score = self._page_score(primary)
        fallback_score = self._page_score(fallback)
        primary_text = primary_score["text_characters"]
        fallback_text = fallback_score["text_characters"]
        fallback_has_clear_text_gain = primary_text == 0 < fallback_text or fallback_text >= primary_text * self._MIN_TEXT_GAIN_TO_REPLACE
        fallback_has_more_structure = fallback_score["score"] > primary_score["score"] and fallback_score["tables"] >= primary_score["tables"]
        if fallback_has_clear_text_gain or fallback_has_more_structure:
            return fallback, primary, "fallback", "primary_recovery", primary_score, fallback_score
        return primary, fallback, "primary", "fallback_recovery", primary_score, fallback_score

    def _page_score(self, page: ExtractedPage) -> dict[str, Any]:
        """Scores extractable evidence without assuming one extractor is better."""
        text = self._page_text(page)
        score = len(text)
        score += len(self._blocks(page)) * 40
        score += len(page.tables) * 220
        score += len(page.images) * 25
        score += 30 if normalize_text(page.notes or "") else 0
        return {
            "score": score,
            "text_characters": len(text),
            "blocks": len(self._blocks(page)),
            "tables": len(page.tables),
            "images": len(page.images),
            "has_notes": bool(normalize_text(page.notes or "")),
        }

    def _source_event(
        self,
        location: int,
        selected_role: str,
        primary_score: dict[str, Any],
        fallback_score: dict[str, Any],
    ) -> dict[str, Any]:
        """Records why one extractor became the base for this page or slide."""
        return {
            "content_type": "page_quality",
            "location": location,
            "decision": f"selected_{selected_role}",
            "reason": "Higher-quality page or slide used as merge base.",
            "primary_score": primary_score,
            "fallback_score": fallback_score,
        }

    # This function reconciles one page or slide before appending recovery evidence.
    def _merge_page(self, primary: ExtractedPage, fallback: ExtractedPage, *, recovery_role: str = "fallback_recovery") -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        comparable_text = self._page_text(primary)
        for fallback_block in self._blocks(fallback):
            decision = self._reconciler.compare_text(comparable_text, fallback_block["text"])
            event = {"content_type": "text", "location": primary.number, **decision}
            events.append(event)
            if decision["decision"] in {"fallback_only", "conflict"}:
                primary.text_blocks.append({**fallback_block, "extractor_role": recovery_role, "reconciliation": decision})
                comparable_text = "\n".join((comparable_text, fallback_block["text"])).strip()
        original_text = self._page_text(primary)
        if not primary.text and fallback.text and not primary.text_blocks:
            primary.text = fallback.text
        if primary.text_blocks:
            block_text = "\n".join(block.get("text", "") for block in primary.text_blocks if normalize_text(str(block.get("text", ""))))
            if original_text and normalize_text(block_text) and not self._reconciler.compare_text(block_text, original_text)["decision"] in {"duplicate", "near_duplicate"}:
                primary.text = "\n".join((original_text, block_text)).strip()
            elif original_text:
                primary.text = original_text
            else:
                primary.text = block_text
        for table in fallback.tables:
            decision = self._best_table_decision(primary.tables, table)
            event = {"content_type": "table", "location": primary.number, **decision}
            events.append(event)
            if decision["decision"] in {"fallback_only", "conflict"}:
                primary.tables.append(self._annotate_table(table, decision))
        events.extend(self._merge_images(primary, fallback, recovery_role))
        if not primary.notes:
            primary.notes = fallback.notes
        primary.reconciliation.extend(events)
        return events

    def _blocks(self, page: ExtractedPage) -> list[dict[str, Any]]:
        """Uses native blocks when available, otherwise treats page text as one recovery block."""
        blocks = [block for block in page.text_blocks if normalize_text(str(block.get("text", "")))]
        return blocks or ([{"text": page.text}] if normalize_text(page.text) else [])

    def _page_text(self, page: ExtractedPage) -> str:
        """Returns comparable page text from the richest available source fields."""
        text = normalize_text(page.text or "")
        if text:
            return text
        return "\n".join(block["text"] for block in self._blocks(page) if normalize_text(str(block.get("text", ""))))

    def _best_table_decision(self, primary_tables: list[Any], fallback_table: Any) -> dict[str, Any]:
        """Chooses the strongest same-page comparison without guessing table identity."""
        decisions = [self._reconciler.compare_tables(table, fallback_table) for table in primary_tables]
        for expected in ("duplicate", "conflict"):
            match = next((decision for decision in decisions if decision["decision"] == expected), None)
            if match:
                return match
        return {"decision": "fallback_only", "reason": "No matching primary table was found on this page or slide."}

    def _annotate_table(self, table: Any, decision: dict[str, Any]) -> Any:
        """Attaches recovery evidence without changing an existing table representation."""
        if isinstance(table, dict):
            return {**table, "reconciliation": decision}
        return {"matrix": table, "reconciliation": decision}

    def _merge_images(self, primary: ExtractedPage, fallback: ExtractedPage, recovery_role: str) -> list[dict[str, Any]]:
        """Deduplicates image metadata from primary and fallback extractors."""
        events: list[dict[str, Any]] = []
        existing_keys = {self._image_key(image) for image in primary.images}
        for image in fallback.images:
            key = self._image_key(image)
            if key in existing_keys:
                events.append({
                    "content_type": "image",
                    "location": primary.number,
                    "decision": "duplicate",
                    "reason": "Image metadata matches an existing extractor result after bbox normalization.",
                    "image_key": key,
                })
                continue
            primary.images.append({**image, "extractor_role": recovery_role})
            existing_keys.add(key)
            events.append({
                "content_type": "image",
                "location": primary.number,
                "decision": "fallback_only",
                "reason": "Image metadata was found only in the recovery extractor.",
                "image_key": key,
            })
        for image in primary.images:
            if isinstance(image, dict) and isinstance(image.get("bbox"), dict):
                image["bbox"] = self._normalized_bbox(image["bbox"]) or image["bbox"]
        return events

    def _image_key(self, image: Any) -> tuple[Any, ...]:
        """Builds a stable identity from image locator and normalized box coordinates."""
        if not isinstance(image, dict):
            return ("unknown", repr(image))
        bbox = self._normalized_bbox(image.get("bbox"))
        if bbox:
            return ("bbox", bbox["left"], bbox["top"], bbox["width"], bbox["height"])
        locator = normalize_text(str(image.get("shape_name") or image.get("caption") or image.get("classification") or ""))
        return ("locator", locator)

    def _normalized_bbox(self, bbox: Any) -> dict[str, int | float] | None:
        """Standardizes left/top/right/bottom and left/top/width/height boxes."""
        if not isinstance(bbox, dict):
            return None
        left, top = bbox.get("left"), bbox.get("top")
        if not isinstance(left, (int, float)) or not isinstance(top, (int, float)):
            return None
        if isinstance(bbox.get("width"), (int, float)) and isinstance(bbox.get("height"), (int, float)):
            width, height = abs(bbox["width"]), abs(bbox["height"])
            return {"left": left, "top": top, "width": width, "height": height}
        right, bottom = bbox.get("right"), bbox.get("bottom")
        if not isinstance(right, (int, float)) or not isinstance(bottom, (int, float)):
            return None
        return {"left": min(left, right), "top": min(top, bottom), "width": abs(right - left), "height": abs(bottom - top)}

    def _summary(self, events: list[dict[str, Any]]) -> dict[str, int]:
        """Counts reconciliation outcomes for parser metadata and operational audit."""
        return {decision: sum(event["decision"] == decision for event in events)
                for decision in ("selected_primary", "selected_fallback", "duplicate", "near_duplicate", "fallback_only", "conflict")}

    def _requires_review(
        self,
        primary: ExtractionResult,
        fallback: ExtractionResult,
        conflicts: list[dict[str, Any]],
        selected_fallback_pages: int,
    ) -> bool:
        """Allows clean fallback recovery while still blocking unresolved risk."""
        if conflicts or fallback.requires_review:
            return True
        if primary.requires_review and not selected_fallback_pages:
            return True
        return False

    def __init__(self, reconciler: ExtractionReconciler | None = None) -> None:
        """Creates a merger with explicit, testable comparison rules."""
        self._reconciler = reconciler or ExtractionReconciler()

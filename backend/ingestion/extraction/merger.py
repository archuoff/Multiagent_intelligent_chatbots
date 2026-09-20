"""Deterministic reconciliation and quality-first merger for extraction results.

The merger compares Docling output with local recovery output page by page,
chooses the stronger page as the base, and then appends non-duplicate evidence
from the other extractor. Conflicts remain explicit so unsafe content cannot
quietly move into chunking.
"""

from __future__ import annotations

from typing import Any

from backend.ingestion.extraction import confidence
from backend.ingestion.extraction import text_plausibility
from backend.ingestion.extraction.contracts import ExtractedPage
from backend.ingestion.extraction.contracts import ExtractionResult
from backend.ingestion.extraction.raw_text_index import PdfRawTextIndex
from backend.ingestion.extraction.reconciliation import ExtractionReconciler
from backend.ingestion.utils import normalize_text


class ExtractionResultMerger:
    """Merges complementary extraction results without using an LLM."""

    _MIN_TEXT_GAIN_TO_REPLACE = 1.3
    _NEW_CONTENT_CONFIRM = 0.75
    _NEW_CONTENT_PARTIAL = 0.4
    # Kept in sync with validation/quality.py's document-level thresholds --
    # both places judge "is this document broadly unreliable", one at the
    # extraction-result level (here) and one at the canonical-node level.
    _LOW_CONFIDENCE_DOCUMENT_RATIO = 0.25
    _LOW_CONFIDENCE_DOCUMENT_FLOOR = 3

    # This function merges page-level content by quality, not by extractor priority.
    def merge(self, primary: ExtractionResult, fallback: ExtractionResult, *,
              raw_text_index: PdfRawTextIndex | None = None) -> ExtractionResult:
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
                event = self._source_event(page_number, "docling", self._page_score(primary_page), {})
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
            reconciliation.extend(self._merge_page(page, recovery, base_source=selected_role,
                recovery_source=self._source_from_recovery_role(recovery_role), recovery_role=recovery_role,
                raw_text_index=raw_text_index, fallback_engine=fallback.extractor_name))
            pages[page_number] = page

        conflicts = [event for event in reconciliation if event["decision"] == "conflict"]
        confidence_summary = self._confidence_summary(reconciliation)
        processing_details = dict(primary.processing_details)
        if reconciliation:
            processing_details["reconciliation"] = {"version": "quality-rules-v2", "events": reconciliation,
                "summary": self._summary(reconciliation), "confidence": confidence_summary}
        if selected_fallback_pages:
            processing_details["quality_recovery"] = {
                "selected_fallback_pages": selected_fallback_pages,
                "reason": "Fallback extraction scored higher for one or more pages or slides.",
            }
        requires_review = self._requires_review(primary, fallback, reconciliation, confidence_summary, selected_fallback_pages)
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
            return fallback, primary, "fallback", "docling_recovery", primary_score, fallback_score
        return primary, fallback, "docling", "fallback_recovery", primary_score, fallback_score

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
            "docling_score": primary_score,
            "fallback_score": fallback_score,
            "confidence_tier": confidence.MEDIUM,
            "confidence_reason": "page_scoring_rule",
        }

    def _assign_confidence(self, decision: dict[str, Any]) -> dict[str, Any]:
        """Attaches an explicit confidence tier + reason code to a
        reconciliation decision. A bare decision label ("fallback_only",
        "conflict") only says what kind of outcome occurred, not how sure
        the pipeline actually is about it -- this is what a node-level
        exclusion or a document-level gate needs to act on, instead of
        treating "nothing was flagged" as proof of correctness."""
        kind = decision.get("decision")
        if kind == "duplicate":
            return {**decision, "confidence_tier": confidence.HIGH, "confidence_reason": "exact_duplicate"}
        if kind == "near_duplicate":
            return {**decision, "confidence_tier": confidence.MEDIUM, "confidence_reason": "coverage_rule_only"}
        if kind == "conflict_resolved":
            independent = (decision.get("verification") or {}).get("independent", True)
            if independent:
                return {**decision, "confidence_tier": confidence.HIGH, "confidence_reason": "raw_text_position_match"}
            return {**decision, "confidence_tier": confidence.MEDIUM, "confidence_reason": "raw_text_position_match_shared_engine"}
        if kind == "conflict":
            return {**decision, "confidence_tier": confidence.LOW, "confidence_reason": "conflict_unresolved"}
        if kind == "fallback_only":
            verification = decision.get("content_verification")
            result = (verification or {}).get("result")
            if result == "rejected":
                return {**decision, "confidence_tier": confidence.LOW, "confidence_reason": "noise_heuristic_failed"}
            if result == "absent":
                return {**decision, "confidence_tier": confidence.LOW, "confidence_reason": "new_content_absent_at_bbox"}
            if result == "confirmed":
                return {**decision, "confidence_tier": confidence.HIGH, "confidence_reason": "new_content_position_verified"}
            # Nothing could confirm or reject this content either way --
            # trusted-but-unverified is the honest classification.
            return {**decision, "confidence_tier": confidence.MEDIUM, "confidence_reason": "new_content_unverified"}
        return decision

    def _verify_new_content(
        self,
        base_page: ExtractedPage,
        fallback_block: dict[str, Any],
        decision: dict[str, Any],
        raw_text_index: PdfRawTextIndex | None,
        *,
        whole_page_recovery: bool,
    ) -> dict[str, Any]:
        """Checks a "new content" block (the base reader missed it entirely)
        against the real source and a noise heuristic before trusting it.

        Without this, garbled fallback text that doesn't happen to overlap
        enough with existing content to be classified a "conflict" would
        silently pass through as if it were a legitimate correction -- the
        gap this item exists to close. Gathers evidence into
        `content_verification`; `_assign_confidence` makes the final tier
        call from it, so tier logic stays in one place."""
        text = normalize_text(str(fallback_block.get("text", ""))) or ""
        if not text:
            return decision
        if text_plausibility.is_probably_noise(text):
            return {**decision, "content_verification": {
                "method": "noise_heuristic", "result": "rejected",
                "noise_signals": text_plausibility.noise_signals(text),
            }}
        if whole_page_recovery:
            # Nothing to compare against -- the base page was empty, so this
            # is a full-page recovery, not a suspicious addition next to
            # otherwise-trusted content. The noise check above is still the
            # right (and only) gate for this case.
            return decision
        bbox = fallback_block.get("bbox")
        if raw_text_index is None or not self._bbox_is_usable(bbox):
            return decision
        try:
            if not raw_text_index.page_has_text_layer(base_page.number):
                return decision  # scanned page -- the noise check is the only defense available
            coverage = raw_text_index.token_coverage_at_bbox(base_page.number, bbox, text)
        except Exception:
            return decision
        if coverage >= self._NEW_CONTENT_CONFIRM:
            return {**decision, "content_verification": {"method": "raw_text_position_coverage", "result": "confirmed", "coverage": round(coverage, 3)}}
        if coverage >= self._NEW_CONTENT_PARTIAL:
            return {**decision, "content_verification": {"method": "raw_text_position_coverage", "result": "inconclusive", "coverage": round(coverage, 3)}}
        return {**decision, "content_verification": {"method": "raw_text_position_coverage", "result": "absent", "coverage": round(coverage, 3)}}

    # This function reconciles one page or slide before appending recovery evidence.
    def _merge_page(self, primary: ExtractedPage, fallback: ExtractedPage, *, base_source: str = "docling",
                    recovery_source: str = "fallback", recovery_role: str = "fallback_recovery",
                    raw_text_index: PdfRawTextIndex | None = None, fallback_engine: str | None = None) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        comparable_text = self._page_text(primary)
        # The base reader found nothing at all on this page -- a common,
        # legitimate case (e.g. Docling missing a whole page). Every
        # fallback block here is a full-page recovery, not evidence to be
        # second-guessed against an empty comparison.
        whole_page_recovery = not comparable_text
        for fallback_block in self._blocks(fallback):
            decision = self._reconciler.compare_text(comparable_text, fallback_block["text"])
            if decision["decision"] == "conflict":
                decision = self._verify_text_conflict(primary, fallback_block, decision, raw_text_index,
                    base_source=base_source, recovery_source=recovery_source, fallback_engine=fallback_engine)
            elif decision["decision"] == "fallback_only":
                decision = self._verify_new_content(primary, fallback_block, decision, raw_text_index,
                    whole_page_recovery=whole_page_recovery)
            decision = self._assign_confidence(decision)
            event = {"content_type": "text", "location": primary.number, **decision}
            events.append(event)
            if decision["decision"] in {"fallback_only", "conflict", "conflict_resolved"} and decision.get("winning_source") != base_source:
                stored_block = {**fallback_block, "extractor_role": recovery_role, "reconciliation": decision}
                if decision.get("confidence_tier") == confidence.LOW:
                    # Unverified/unresolved content is retained as evidence
                    # (still appended below) but never trusted into the
                    # page's own searchable text -- see _build_fallback_pages,
                    # which turns retrieval_allowed=False blocks into their
                    # own LOW_CONFIDENCE_BLOCK nodes instead of letting them
                    # get folded into a legitimate paragraph.
                    stored_block["retrieval_allowed"] = False
                    stored_block["requires_review"] = True
                primary.text_blocks.append(stored_block)
                if stored_block.get("retrieval_allowed") is not False:
                    comparable_text = "\n".join((comparable_text, fallback_block["text"])).strip()
        original_text = self._page_text(primary)
        if not primary.text and fallback.text and not primary.text_blocks:
            primary.text = fallback.text
        if primary.text_blocks:
            block_text = "\n".join(
                block.get("text", "") for block in primary.text_blocks
                if normalize_text(str(block.get("text", ""))) and block.get("retrieval_allowed") is not False
            )
            # Argument order matters here: block_text is built by joining
            # primary's OWN original blocks with newly-appended recovery
            # blocks, so it always contains original_text as a literal
            # prefix. Checking compare_text(block_text, original_text) asks
            # "is original_text's content covered by block_text" -- which is
            # trivially always true once block_text contains it as a
            # substring, misclassifying any combination as "near_duplicate"
            # and silently dropping genuinely new (already-verified) content.
            # The right question is the reverse: "is block_text's content
            # already covered by original_text alone" -- i.e. did appending
            # the recovery blocks add anything, or just repeat what's there.
            if original_text and normalize_text(block_text) and not self._reconciler.compare_text(original_text, block_text)["decision"] in {"duplicate", "near_duplicate"}:
                primary.text = "\n".join((original_text, block_text)).strip()
            elif original_text:
                primary.text = original_text
            else:
                primary.text = block_text
        for table in fallback.tables:
            decision = self._assign_confidence(self._best_table_decision(primary.tables, table))
            event = {"content_type": "table", "location": primary.number, **decision}
            events.append(event)
            if decision["decision"] == "fallback_only":
                primary.tables.append(self._annotate_table(table, decision))
        events.extend(self._merge_images(primary, fallback, recovery_role))
        if not primary.notes:
            primary.notes = fallback.notes
        primary.reconciliation.extend(events)
        return events

    def _verify_text_conflict(
        self,
        base_page: ExtractedPage,
        fallback_block: dict[str, Any],
        decision: dict[str, Any],
        raw_text_index: PdfRawTextIndex | None,
        *,
        base_source: str,
        recovery_source: str,
        fallback_engine: str | None = None,
    ) -> dict[str, Any]:
        """Uses PDF raw text at the disputed bbox to resolve a conflicting text block."""
        bbox = fallback_block.get("bbox")
        fallback_text = normalize_text(str(fallback_block.get("text", ""))) or ""
        if raw_text_index is None or not self._bbox_is_usable(bbox) or not fallback_text:
            return decision
        base_block = self._best_overlapping_block(base_page, bbox)
        if base_block is None:
            return {**decision, "verification": {"method": "raw_text_position_skipped", "reason": "No base text block with an overlapping bbox was found."}}
        base_text = normalize_text(str(base_block.get("text", ""))) or ""
        if not base_text:
            return decision
        try:
            match = raw_text_index.match_candidates(base_page.number, bbox, base_text, fallback_text)
        except Exception as error:
            return {**decision, "verification": {"method": "raw_text_position_error", "error_type": type(error).__name__}}
        independent = raw_text_index.is_independent_of(fallback_engine) if fallback_engine else True
        verification = {
            "method": match.method,
            "bbox_used": bbox,
            "base_bbox_used": base_block.get("bbox"),
            "raw_region_text": match.region_text,
            "candidate_a_matches": match.candidate_a_matches,
            "candidate_b_matches": match.candidate_b_matches,
            "confidence": match.confidence,
            "referee_engine": raw_text_index.engine_used,
            "independent": independent,
        }
        if match.winner == "A":
            # Resolving in favor of the base (Docling-derived) side is always
            # informative even when the referee shares an engine with the
            # fallback: the referee's word-layer text disagreed with the
            # fallback's own block-layer text, which a shared engine can
            # still catch (see module docstring).
            return {**decision, "decision": "conflict_resolved", "winning_source": base_source, "verification": verification}
        if match.winner == "B":
            # Resolving in favor of the recovery/fallback side when the
            # referee shares that side's engine is the one case where the
            # "independent" check isn't truly independent -- item 5a caps
            # this at medium confidence rather than high.
            return {**decision, "decision": "conflict_resolved", "winning_source": recovery_source, "verification": verification}
        return {**decision, "verification": verification}

    def _source_from_recovery_role(self, recovery_role: str) -> str:
        """Maps internal recovery roles to source names used in audit metadata."""
        return "docling" if recovery_role == "docling_recovery" else "fallback"

    def _bbox_is_usable(self, bbox: Any) -> bool:
        """Rejects a bbox whose coordinate space is known to be unresolved --
        e.g. a bottom-left-origin Docling bbox with no page height available
        to flip it. Verifying against an unknown-orientation box risks
        silently checking a vertically mirrored page region, which is worse
        than not verifying at all."""
        return isinstance(bbox, dict) and bbox.get("coord_space") in (None, "top_left")

    def _best_overlapping_block(self, page: ExtractedPage, bbox: dict[str, Any]) -> dict[str, Any] | None:
        """Finds the base text block that best overlaps a recovery block bbox."""
        target = self._normalized_bbox(bbox)
        if target is None:
            return None
        candidates: list[tuple[float, dict[str, Any]]] = []
        for block in page.text_blocks:
            if not self._bbox_is_usable(block.get("bbox")):
                continue
            block_box = self._normalized_bbox(block.get("bbox"))
            text = normalize_text(str(block.get("text", "")))
            if block_box is None or not text:
                continue
            overlap = self._overlap_area(target, block_box)
            if overlap > 0:
                candidates.append((overlap, block))
        if candidates:
            return max(candidates, key=lambda item: item[0])[1]
        return None

    def _overlap_area(self, first: dict[str, float], second: dict[str, float]) -> float:
        """Computes bbox overlap area in normalized left/top/width/height space."""
        left = max(first["left"], second["left"])
        top = max(first["top"], second["top"])
        right = min(first["left"] + first["width"], second["left"] + second["width"])
        bottom = min(first["top"] + first["height"], second["top"] + second["height"])
        return max(0.0, right - left) * max(0.0, bottom - top)

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
        return {"decision": "fallback_only", "reason": "No matching Docling/base table was found on this page or slide."}

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
                    "confidence_tier": confidence.HIGH,
                    "confidence_reason": "exact_duplicate",
                })
                continue
            primary.images.append({**image, "extractor_role": recovery_role})
            existing_keys.add(key)
            events.append({
                "content_type": "image",
                "location": primary.number,
                "confidence_tier": confidence.MEDIUM,
                "confidence_reason": "new_content_unverified",
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
        """Standardizes x0/y0/x1/y1, left/top/right/bottom, and left/top/width/height boxes."""
        if not isinstance(bbox, dict):
            return None
        if all(isinstance(bbox.get(key), (int, float)) for key in ("x0", "y0", "x1", "y1")):
            left, top, right, bottom = bbox["x0"], bbox["y0"], bbox["x1"], bbox["y1"]
            return {"left": min(left, right), "top": min(top, bottom), "width": abs(right - left), "height": abs(bottom - top)}
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
                for decision in ("selected_docling", "selected_fallback", "duplicate", "near_duplicate", "fallback_only", "conflict", "conflict_resolved")}

    def _confidence_summary(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        """Counts text/table reconciliation events by confidence tier -- the
        evidence _requires_review judges severity from, and useful
        operational visibility on its own (e.g. "9 resolved by heuristic
        only" in a document-level warning)."""
        content_events = [event for event in events if event.get("content_type") in {"text", "table"}]
        counts = {confidence.HIGH: 0, confidence.MEDIUM: 0, confidence.LOW: 0}
        for event in content_events:
            tier = event.get("confidence_tier")
            if tier in counts:
                counts[tier] += 1
        total = len(content_events)
        return {
            "high": counts[confidence.HIGH], "medium": counts[confidence.MEDIUM], "low": counts[confidence.LOW],
            "total": total, "low_ratio": round(counts[confidence.LOW] / total, 3) if total else 0.0,
        }

    def _has_unresolved_numeric_conflict(self, events: list[dict[str, Any]]) -> bool:
        """True when a reconciliation event carries differing numeric/technical
        values that were never actually resolved -- this always escalates
        regardless of how small a fraction of the document it is, since a
        single corrupted safety-critical value must never slip through
        just because the rest of a large document is fine."""
        return any(
            event.get("decision") == "conflict" and (event.get("primary_technical_values") or event.get("fallback_technical_values"))
            for event in events
        )

    def _requires_review(
        self,
        primary: ExtractionResult,
        fallback: ExtractionResult,
        reconciliation: list[dict[str, Any]],
        confidence_summary: dict[str, Any],
        selected_fallback_pages: int,
    ) -> bool:
        """Allows clean fallback recovery while still blocking real risk.

        A single isolated low-confidence item no longer blocks the whole
        document by itself -- that content is excluded at node level
        instead (see merger._merge_page / document.py's LOW_CONFIDENCE_BLOCK
        nodes), which is a real, working safeguard on its own. Document-wide
        review is now reserved for two cases: an unresolved numeric/technical
        conflict (always, regardless of scale), or a broad fraction of the
        document coming back unverified (suggesting extraction failed
        widely, not just at one spot)."""
        if fallback.requires_review:
            return True
        if primary.requires_review and not selected_fallback_pages:
            return True
        if self._has_unresolved_numeric_conflict(reconciliation):
            return True
        if confidence_summary["low"] >= self._LOW_CONFIDENCE_DOCUMENT_FLOOR and confidence_summary["low_ratio"] >= self._LOW_CONFIDENCE_DOCUMENT_RATIO:
            return True
        return False

    def __init__(self, reconciler: ExtractionReconciler | None = None) -> None:
        """Creates a merger with explicit, testable comparison rules."""
        self._reconciler = reconciler or ExtractionReconciler()

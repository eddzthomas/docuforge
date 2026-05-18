"""
DocuForge — Smart PDF Split Detection
=======================================
Detects multi-document boundaries within a single PDF using
heuristic features (blank pages, page number resets, header changes)
and/or vision model analysis via Ollama.

Supports three engines:
  - "heuristic": Fast CPU-only detection using Tesseract and image analysis
  - "hybrid":    Heuristic pre-scan + vision model confirmation
  - "off":       No split detection

The heuristic methods use synchronous pytesseract calls directly
to avoid blocking the async pipeline with per-page OCR overhead.
"""

import asyncio
import base64
import io
import json
import logging
import re
from pathlib import Path

import httpx
import pytesseract
from PIL import Image

from app.config import Settings

logger = logging.getLogger(__name__)

# ---- Module constants ----
HEADER_PCT = 0.10
FOOTER_PCT = 0.15
CONTACT_SHEET_BATCH = 15
BOUNDARY_PROXIMITY = 2
VISION_CONCURRENCY = 3


class SplitDetectionError(Exception):
    """Raised when split detection encounters an unrecoverable error."""

    pass


class SplitDetector:
    """
    Detects document boundaries within a multi-document PDF.

    Uses heuristic methods (blank pages, page number resets, header changes)
    and optionally a vision model to identify where one document ends
    and another begins.
    """

    def __init__(self, settings: Settings):
        """
        Initialize the split detector.

        Args:
            settings: Application settings with split configuration.
        """
        self.settings = settings

    async def detect(
        self,
        pdf_path: Path,
        page_images: list[Path],
        settings: Settings,
        progress_callback=None,
    ) -> dict:
        """
        Main entry point for split detection.

        Dispatches to heuristic and/or vision detection based on
        the configured split_engine.

        Args:
            pdf_path: Path to the source PDF.
            page_images: List of rendered page image paths.
            settings: Application settings.
            progress_callback: Optional callable(phase: str, pct: int)
                called at detection checkpoints. phase is one of:
                "blank", "page_numbers", "headers", "vision", "merging", "done".

        Returns:
            dict with keys:
                - boundaries: list[int] — page indices where new documents start
                - confidences: list[float] — confidence scores per boundary
                - blank_pages: list[int] — detected blank page indices
                - engine: str — detection engine used ("heuristic", "hybrid", "off")
        """
        engine = settings.split_engine

        if engine == "off":
            logger.info("Split detection disabled (split_engine=off)")
            return {
                "boundaries": [],
                "confidences": [],
                "blank_pages": [],
                "engine": "off",
            }

        total_pages = len(page_images)
        min_pages = getattr(settings, "split_min_pages", 3)

        if total_pages < min_pages:
            logger.info(
                f"Too few pages for split detection: {total_pages} < {min_pages}"
            )
            return {
                "boundaries": [],
                "confidences": [],
                "blank_pages": [],
                "engine": engine,
            }

        logger.info(
            f"Starting split detection: engine={engine}, pages={total_pages}, "
            f"pdf={pdf_path.name}"
        )

        blank_pages: list[int] = []
        heuristic_boundaries: list[int] = []
        vision_boundaries: list[dict] = []

        pcb = progress_callback or (lambda phase, pct: None)

        # ---- Heuristic detection (runs for both "heuristic" and "hybrid") ----
        if engine in ("heuristic", "hybrid"):
            pcb("blank", 0)
            blank_pages = self._detect_blank_pages(page_images)
            logger.info(f"Blank pages detected: {blank_pages}")
            pcb("blank", 8)

            # Infer boundaries from blank pages: a non-blank page
            # following a blank page is a strong boundary signal
            pcb("page_numbers", 8)
            blank_boundaries = self._detect_blank_boundaries(
                blank_pages, page_images, total_pages, settings
            )
            logger.info(f"Blank-inferred boundaries: {blank_boundaries}")

            page_reset_boundaries = self._detect_page_number_reset(
                page_images, settings, blank_pages
            )
            logger.info(f"Page number reset boundaries: {page_reset_boundaries}")
            pcb("page_numbers", 20)

            pcb("headers", 20)
            header_boundaries = self._detect_header_changes(
                page_images, settings, blank_pages
            )
            logger.info(f"Header change boundaries: {header_boundaries}")
            pcb("headers", 35)

            # Combine heuristic signals
            raw_heuristic = sorted(set(
                page_reset_boundaries + header_boundaries
            ))
            heuristic_boundaries = self._deduplicate_boundaries(raw_heuristic)
            logger.info(f"Combined heuristic boundaries: {heuristic_boundaries}")

        # ---- Vision detection (hybrid only) ----
        if engine == "hybrid":
            pcb("vision", 35)
            vision_boundaries = await self._detect_vision(
                page_images, settings, progress_callback=pcb
            )
            logger.info(f"Vision boundaries: {vision_boundaries}")

        # ---- Merge and finalize ----
        pcb("merging", 85)
        if engine == "heuristic":
            boundaries = heuristic_boundaries
            confidences = [0.5] * len(boundaries)
        else:
            boundaries, confidences = self._merge_boundaries(
                heuristic_boundaries, blank_boundaries, vision_boundaries, total_pages
            )

        pcb("done", 100)

        logger.info(
            f"Split detection complete: {len(boundaries)} boundaries, "
            f"{len(blank_pages)} blank pages"
        )

        return {
            "boundaries": boundaries,
            "confidences": confidences,
            "blank_pages": blank_pages,
            "engine": engine,
        }

    # ------------------------------------------------------------------
    # Heuristic: Blank page detection
    # ------------------------------------------------------------------

    def _detect_blank_pages(self, page_images: list[Path]) -> list[int]:
        """
        Detect blank pages using histogram-based pixel whiteness ratio.

        A page is considered blank if a configured percentage of its
        pixels fall at or above the whiteness threshold (0-255).
        This handles scanner noise, off-white backgrounds, and
        faint bleed-through better than raw pixel variance.

        Args:
            page_images: List of paths to rendered page PNGs.

        Returns:
            List of 0-based blank page indices.
        """
        whiteness = getattr(self.settings, "split_blank_threshold", 245)
        required_pct = getattr(self.settings, "split_blank_pixel_pct", 0.95)

        blank = []
        for i, img_path in enumerate(page_images):
            try:
                with Image.open(img_path) as img:
                    ratio = self._compute_blank_ratio(img, whiteness)
                if ratio >= required_pct:
                    blank.append(i)
                    logger.debug(
                        f"Page {i}: blank (white_pct={ratio:.1%}, "
                        f"threshold={whiteness}, required={required_pct:.0%})"
                    )
            except Exception as exc:
                logger.warning(f"Page {i}: failed to check blank status: {exc}")
        return blank

    def _compute_blank_ratio(
        self, img: Image.Image, whiteness: int = 245
    ) -> float:
        """
        Compute the fraction of near-white pixels in the image.

        Downsamples to 400x400 for speed, converts to grayscale,
        and counts pixels at or above the whiteness threshold.

        Args:
            img: PIL Image of a rendered page.
            whiteness: Pixel value (0-255) above which a pixel counts
                as "white" / near-white.

        Returns:
            Float 0.0–1.0 representing the fraction of white pixels.
        """
        thumb = img.copy()
        thumb.thumbnail((400, 400), Image.LANCZOS)
        gray = thumb.convert("L")
        pixels = list(gray.getdata())
        if not pixels:
            return 0.0
        white_count = sum(1 for p in pixels if p >= whiteness)
        return white_count / len(pixels)

    def _detect_blank_boundaries(
        self,
        blank_pages: list[int],
        page_images: list[Path],
        total_pages: int,
        settings: Settings,
    ) -> list[dict]:
        """
        Infer document boundaries from blank page positions.

        A non-blank page immediately following a blank page is a
        strong signal for a new document boundary — blank pages are
        typically separators between documents.

        Performs a continuation check: if the page after a blank
        shares the same header as the page before the blank AND
        the page numbering continues normally, skip the boundary
        (the blank page is likely just the blank back of a sheet).

        Args:
            blank_pages: List of 0-based blank page indices.
            page_images: List of paths to rendered page PNGs.
            total_pages: Total number of pages in the PDF.
            settings: Application settings.

        Returns:
            List of dicts with page (0-based) and confidence (float, 0.8–0.95).
        """
        blank_set = set(blank_pages)
        boundaries: list[dict] = []

        for blank_idx in blank_pages:
            next_page = blank_idx + 1
            while next_page in blank_set and next_page < total_pages:
                next_page += 1

            if next_page >= total_pages:
                continue

            prev_non_blank = blank_idx - 1
            while prev_non_blank in blank_set and prev_non_blank >= 0:
                prev_non_blank -= 1

            confidence = 0.9

            if prev_non_blank >= 0:
                is_continuation = self._check_continuation(
                    page_images, prev_non_blank, next_page, blank_idx, settings
                )
                if is_continuation:
                    logger.debug(
                        f"Blank boundary suppressed: page {next_page} "
                        f"continues from page {prev_non_blank}"
                    )
                    continue
                confidence = 0.85

            boundaries.append({"page": next_page, "confidence": confidence})
            logger.debug(
                f"Blank boundary: blank at {blank_idx}, "
                f"next non-blank={next_page}, confidence={confidence:.2f}"
            )

        return boundaries

    def _check_continuation(
        self,
        page_images: list[Path],
        prev_page: int,
        next_page: int,
        blank_page: int,
        settings: Settings,
    ) -> bool:
        """
        Check if page after a blank is a continuation of the page before.

        Uses two signals:
          1. Header similarity — if headers are similar, likely same doc
          2. Page numbering — if numbering continues across the blank, same doc

        Both signals must agree for a continuation.

        Args:
            page_images: List of rendered page PNGs.
            prev_page: Index of the page before the blank.
            next_page: Index of the page after the blank.
            blank_page: The blank page index (for logging).
            settings: Application settings.

        Returns:
            True if next_page continues from prev_page.
        """
        # Signal 1: Header similarity
        prev_header = self._ocr_header(page_images[prev_page])
        next_header = self._ocr_header(page_images[next_page])
        header_match = _word_overlap(prev_header, next_header) >= 0.5 if prev_header and next_header else False

        # Signal 2: Page numbering continuity
        prev_num = _extract_page_number(
            self._ocr_footer(page_images[prev_page])
        )
        next_num = _extract_page_number(
            self._ocr_footer(page_images[next_page])
        )
        numbering_continues = (
            prev_num is not None
            and next_num is not None
            and next_num == prev_num + 1
        )

        if header_match and numbering_continues:
            logger.debug(
                f"Continuation across blank: pages {prev_page}→{next_page} "
                f"(header_match, num {prev_num}→{next_num})"
            )
            return True

        return False

    def _ocr_header(self, img_path: Path) -> str:
        """OCR the header region of a page and return first meaningful line."""
        try:
            with Image.open(img_path) as img:
                header_text = self._ocr_region(img, 0.0, HEADER_PCT)
            return _first_meaningful_line(header_text)
        except Exception:
            return ""

    def _ocr_footer(self, img_path: Path) -> str:
        """OCR the footer region of a page and return the text."""
        try:
            with Image.open(img_path) as img:
                return self._ocr_region(img, 1.0 - FOOTER_PCT, 1.0)
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Heuristic: Page number reset detection
    # ------------------------------------------------------------------

    def _detect_page_number_reset(
        self, page_images: list[Path], settings: Settings,
        blank_pages: list[int] | None = None,
    ) -> list[int]:
        """
        Detect document boundaries by finding page number resets.

        OCRs the footer region of each non-blank page and looks for
        page numbers. Resets to 1, significant drops, or sequence
        breaks after blank pages indicate new document boundaries.

        Args:
            page_images: List of paths to rendered page PNGs.
            settings: Application settings.
            blank_pages: Optional list of 0-based blank page indices to skip.

        Returns:
            List of 0-based page indices where new documents start.
        """
        blank_set = set(blank_pages) if blank_pages else set()
        boundaries: list[int] = []
        page_numbers: dict[int, int | None] = {}  # keyed by page index

        for i, img_path in enumerate(page_images):
            if i in blank_set:
                page_numbers[i] = None
                continue
            try:
                with Image.open(img_path) as img:
                    footer_text = self._ocr_region(img, 1.0 - FOOTER_PCT, 1.0)
                num = _extract_page_number(footer_text)
                page_numbers[i] = num
                logger.debug(
                    f"Page {i}: footer OCR='{footer_text[:80]}', number={num}"
                )
            except Exception as exc:
                logger.warning(f"Page {i}: footer OCR failed: {exc}")
                page_numbers[i] = None

        # Detect resets and drops across non-blank pages
        expected: int | None = None
        last_num: int | None = None
        for i in sorted(page_numbers.keys()):
            num = page_numbers[i]

            if expected is None:
                if num is not None:
                    expected = num
                    last_num = num
                continue

            if num is None:
                continue

            # Check if there was a blank page gap between this and the last
            had_blank_gap = any(
                b in blank_set for b in range(i - 1, max(0, i - 3), -1)
            )
            if had_blank_gap and expected is not None:
                expected = None  # Reset expectation after blank gap

            if expected is None:
                if num is not None:
                    expected = num
                    last_num = num
                continue

            # Explicit reset to page 1
            if num == 1 and expected > 1:
                boundaries.append(i)
                expected = 1
                last_num = 1
            # Significant page number drop
            elif expected > 0 and num < expected - 2:
                boundaries.append(i)
                expected = num
                last_num = num
            # Normal page number increase (handles +1, +2, etc.)
            elif num > expected:
                expected = num
                last_num = num
            # Same number (could be continuation of a spread)
            elif num == expected:
                pass
            else:
                expected = num
                last_num = num

        return boundaries

    # ------------------------------------------------------------------
    # Heuristic: Header change detection
    # ------------------------------------------------------------------

    def _detect_header_changes(
        self, page_images: list[Path], settings: Settings,
        blank_pages: list[int] | None = None,
    ) -> list[int]:
        """
        Detect document boundaries by finding header text changes.

        OCRs the header region of each non-blank page and compares
        consecutive non-blank pages. Blank pages are skipped and
        treated as natural comparison breakpoints.

        Args:
            page_images: List of paths to rendered page PNGs.
            settings: Application settings.
            blank_pages: Optional list of 0-based blank page indices to skip.

        Returns:
            List of 0-based page indices where new documents start.
        """
        blank_set = set(blank_pages) if blank_pages else set()
        boundaries: list[int] = []
        headers: dict[int, str] = {}  # keyed by page index

        for i, img_path in enumerate(page_images):
            if i in blank_set:
                continue
            try:
                with Image.open(img_path) as img:
                    header_text = self._ocr_region(img, 0.0, HEADER_PCT)
                first_line = _first_meaningful_line(header_text)
                headers[i] = first_line
                logger.debug(f"Page {i}: header='{first_line[:80]}'")
            except Exception as exc:
                logger.warning(f"Page {i}: header OCR failed: {exc}")
                headers[i] = ""

        # Compare consecutive non-blank pages
        non_blank_pages = sorted(headers.keys())
        for idx in range(1, len(non_blank_pages)):
            prev_page = non_blank_pages[idx - 1]
            curr_page = non_blank_pages[idx]
            prev = headers[prev_page]
            curr = headers[curr_page]

            if not prev or not curr:
                continue

            # Check if there's a blank gap between these pages
            had_blank_gap = any(
                b in blank_set for b in range(prev_page + 1, curr_page)
            )
            # Lower threshold when a blank gap is present (header change
            # across a blank page is a strong boundary signal)
            threshold = 0.5 if had_blank_gap else 0.3

            overlap = _word_overlap(prev, curr)
            if overlap < threshold:
                boundaries.append(curr_page)
                logger.debug(
                    f"Header change: page {prev_page}->{curr_page}, "
                    f"overlap={overlap:.2f}, gap={had_blank_gap}, "
                    f"prev='{prev[:60]}', curr='{curr[:60]}'"
                )

        return boundaries

    # ------------------------------------------------------------------
    # Vision model detection
    # ------------------------------------------------------------------

    async def _detect_vision(
        self,
        page_images: list[Path],
        settings: Settings,
        progress_callback=None,
    ) -> list[dict]:
        """
        Use Ollama vision model to detect document boundaries.

        Sends batches of pages as contact sheet images to the vision
        model in parallel (up to VISION_CONCURRENCY concurrent requests)
        for maximum GPU utilization.

        Args:
            page_images: List of paths to rendered page PNGs.
            settings: Application settings.
            progress_callback: Optional callable(phase: str, pct: int)
                for per-batch progress. phase is "vision"; pct is 35–85.

        Returns:
            List of dicts with keys: page (int), confidence (float).
        """
        total_pages = len(page_images)
        total_batches = (total_pages + CONTACT_SHEET_BATCH - 1) // CONTACT_SHEET_BATCH
        pcb = progress_callback or (lambda phase, pct: None)

        batches = []
        for batch_num, batch_start in enumerate(
            range(0, total_pages, CONTACT_SHEET_BATCH), start=1
        ):
            batch_end = min(batch_start + CONTACT_SHEET_BATCH, total_pages)
            batch_slice = page_images[batch_start:batch_end]
            batches.append((batch_num, batch_start, batch_end, batch_slice, len(batches) + 1))

        semaphore = asyncio.Semaphore(VISION_CONCURRENCY)
        completed_count = [0]  # mutable counter for progress

        async def process_batch(
            batch_num: int, batch_start: int, batch_end: int, batch_slice: list[Path]
        ):
            async with semaphore:
                try:
                    contact_sheet = self._build_contact_sheet(
                        batch_slice, batch_start, batch_end
                    )
                    boundaries = await self._call_vision_model(
                        contact_sheet, batch_start, batch_end, total_pages, settings
                    )
                    completed_count[0] += 1
                    batch_pct = 35 + int((completed_count[0] / total_batches) * 47)
                    pcb("vision", min(batch_pct, 82))
                    logger.info(
                        f"Vision batch {batch_num}/{total_batches} "
                        f"(pages {batch_start}-{batch_end - 1}): "
                        f"{len(boundaries)} boundaries found"
                    )
                    return boundaries
                except Exception as exc:
                    completed_count[0] += 1
                    logger.warning(
                        f"Vision batch {batch_num}/{total_batches} "
                        f"(pages {batch_start}-{batch_end - 1}) failed: {exc}"
                    )
                    return []

        tasks = [
            process_batch(bn, bs, be, bslice)
            for bn, bs, be, bslice, _ in batches
        ]
        results = await asyncio.gather(*tasks)

        all_boundaries: list[dict] = []
        for batch_result in results:
            all_boundaries.extend(batch_result)

        return all_boundaries

    def _build_contact_sheet(
        self,
        images: list[Path],
        start_idx: int,
        end_idx: int,
        thumb_width: int = 400,
    ) -> Image.Image:
        """
        Build a vertical contact sheet from a batch of page images.

        Each page is resized to a uniform width and stacked vertically.

        Args:
            images: List of page image paths in this batch.
            start_idx: Starting page index (0-based) for labeling.
            end_idx: Ending page index (exclusive).
            thumb_width: Width in pixels for each thumbnail.

        Returns:
            PIL Image of the composite contact sheet.
        """
        thumbs: list[Image.Image] = []

        for i, img_path in enumerate(images):
            try:
                with Image.open(img_path) as img:
                    w_percent = thumb_width / float(img.width)
                    h_size = int(float(img.height) * w_percent)
                    thumb = img.resize((thumb_width, h_size), Image.LANCZOS)
                    thumbs.append(thumb)
            except Exception as exc:
                logger.warning(
                    f"Failed to load page {start_idx + i} for contact sheet: {exc}"
                )
                placeholder = Image.new("RGB", (thumb_width, 200), (200, 200, 200))
                thumbs.append(placeholder)

        if not thumbs:
            return Image.new("RGB", (thumb_width, 100), (255, 255, 255))

        spacing = 4
        total_height = sum(t.height for t in thumbs) + spacing * (len(thumbs) - 1)

        sheet = Image.new("RGB", (thumb_width, total_height), (255, 255, 255))
        y_offset = 0
        for t in thumbs:
            sheet.paste(t, (0, y_offset))
            y_offset += t.height + spacing

        return sheet

    async def _call_vision_model(
        self,
        contact_sheet: Image.Image,
        batch_start: int,
        batch_end: int,
        total_pages: int,
        settings: Settings,
    ) -> list[dict]:
        """
        Send a contact sheet to the Ollama vision model for boundary detection.

        Args:
            contact_sheet: Composite PIL Image of page thumbnails.
            batch_start: Starting page index (0-based) of this batch.
            batch_end: Ending page index (exclusive).
            total_pages: Total pages in the full PDF.
            settings: Application settings.

        Returns:
            List of dicts with page (int) and confidence (float).

        Raises:
            SplitDetectionError: If the Ollama call fails.
        """
        buf = io.BytesIO()
        contact_sheet.save(buf, format="PNG")
        image_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

        prompt = (
            f"You are analyzing a multi-page PDF with {total_pages} total pages. "
            f"This contact sheet shows pages {batch_start + 1} through "
            f"{batch_end} (absolute 1-indexed page numbers).\n\n"
            f"Identify where one document ends and another begins. Key signals:\n"
            f"  - Blank or near-blank pages are strong separators between documents\n"
            f"  - A page following a blank page almost always starts a new document\n"
            f"  - Unless the page after a blank continues the SAME formatting and numbering\n"
            f"  - Changes in letterhead, logos, or header styling indicate new documents\n"
            f"  - Page number resets (back to 1 or lower numbers) signal new documents\n"
            f"  - Single-page documents between others are common — look for\n"
            f"    complete topic/layout changes on consecutive non-blank pages\n"
            f"  - Cover pages, signature pages, and form separators\n\n"
            f"Return ONLY a valid JSON object in this exact format:\n"
            f'{{"boundaries": [{{"page": 5, "confidence": 0.9}}]}}\n\n'
            f"Each boundary is an absolute page number (1-indexed) where a "
            f"new document starts.\n"
            f"Confidence should be between 0.0 and 1.0.\n"
            f"If no boundaries are detected return: {{\"boundaries\": []}}"
        )

        payload = {
            "model": getattr(settings, "split_model", settings.ocr_model),
            "prompt": prompt,
            "images": [image_b64],
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.0},
        }

        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"{settings.ollama_host}/api/generate",
                    json=payload,
                )
                response.raise_for_status()
        except httpx.ConnectError as exc:
            raise SplitDetectionError(
                f"Cannot connect to Ollama at {settings.ollama_host}"
            ) from exc
        except httpx.HTTPStatusError as exc:
            raise SplitDetectionError(
                f"Ollama API returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.TimeoutException as exc:
            raise SplitDetectionError(
                "Ollama timed out after 120s"
            ) from exc
        except httpx.HTTPError as exc:
            raise SplitDetectionError(
                f"Ollama API call failed: {exc}"
            ) from exc

        result = response.json()
        raw_response = result.get("response", "")

        return _parse_vision_response(raw_response, batch_start, batch_end)

    # ------------------------------------------------------------------
    # Boundary merging
    # ------------------------------------------------------------------

    def _merge_boundaries(
        self,
        heuristic_boundaries: list[int],
        blank_boundaries: list[dict],
        vision_boundaries: list[dict],
        total_pages: int,
    ) -> tuple[list[int], list[float]]:
        """
        Merge heuristic, blank-inferred, and vision boundary lists.

        Blank-inferred boundaries get highest base confidence (0.85–0.90),
        vision boundaries preserve their model-assigned confidence,
        and heuristic boundaries get 0.8 if near a vision or blank
        boundary, 0.5 otherwise.

        Args:
            heuristic_boundaries: Page indices from page num + header detection.
            blank_boundaries: List of {page, confidence} dicts from blank pages.
            vision_boundaries: List of {page, confidence} dicts from vision model.
            total_pages: Total number of pages in the document.

        Returns:
            Tuple of (boundaries, confidences).
        """
        all_candidates: list[tuple[int, float]] = []
        high_conf_pages: set[int] = set()

        for bd in blank_boundaries:
            page = bd.get("page", -1)
            conf = bd.get("confidence", 0.85)
            try:
                page_int = int(page)
                conf_float = float(conf)
            except (ValueError, TypeError):
                continue
            if 0 < page_int < total_pages:
                all_candidates.append((page_int, conf_float))
                high_conf_pages.add(page_int)

        for vb in vision_boundaries:
            page = vb.get("page", -1)
            conf = vb.get("confidence", 0.5)
            try:
                page_int = int(page)
                conf_float = float(conf)
            except (ValueError, TypeError):
                continue
            if 0 < page_int < total_pages:
                all_candidates.append((page_int, conf_float))
                high_conf_pages.add(page_int)

        for hb in heuristic_boundaries:
            if hb <= 0 or hb >= total_pages:
                continue
            near_high_conf = any(
                abs(hb - vp) <= BOUNDARY_PROXIMITY for vp in high_conf_pages
            )
            conf = 0.8 if near_high_conf else 0.5
            all_candidates.append((hb, conf))

        if not all_candidates:
            return [], []

        all_candidates.sort()
        merged = self._deduplicate_boundaries_with_conf(all_candidates)
        merged = [(p, c) for p, c in merged if p > 0]

        boundaries = [p for p, c in merged]
        confidences = [c for p, c in merged]
        return boundaries, confidences

    def _deduplicate_boundaries(self, boundaries: list[int]) -> list[int]:
        """
        Remove duplicate boundaries within BOUNDARY_PROXIMITY pages.

        Keeps the first occurrence in each cluster.

        Args:
            boundaries: Sorted list of page indices.

        Returns:
            Deduplicated sorted list.
        """
        if not boundaries:
            return []

        boundaries = sorted(boundaries)
        result = [boundaries[0]]
        for b in boundaries[1:]:
            if b - result[-1] > BOUNDARY_PROXIMITY:
                result.append(b)
        return result

    def _deduplicate_boundaries_with_conf(
        self, candidates: list[tuple[int, float]]
    ) -> list[tuple[int, float]]:
        """
        Cluster nearby boundaries and keep the highest-confidence one per cluster.

        Args:
            candidates: Sorted list of (page, confidence) tuples.

        Returns:
            Deduplicated list of (page, confidence) tuples.
        """
        if not candidates:
            return []

        clusters: list[list[tuple[int, float]]] = []
        current_cluster = [candidates[0]]

        for c in candidates[1:]:
            if c[0] - current_cluster[-1][0] <= BOUNDARY_PROXIMITY:
                current_cluster.append(c)
            else:
                clusters.append(current_cluster)
                current_cluster = [c]
        clusters.append(current_cluster)

        merged = []
        for cluster in clusters:
            best = max(cluster, key=lambda x: x[1])
            merged.append(best)

        return merged

    # ------------------------------------------------------------------
    # OCR helpers
    # ------------------------------------------------------------------

    def _ocr_region(
        self, img: Image.Image, y_pct_start: float, y_pct_end: float
    ) -> str:
        """
        OCR a horizontal strip of a page image.

        Crops the image to the specified vertical percentage range
        and runs Tesseract on the crop.

        Args:
            img: PIL Image of a rendered page.
            y_pct_start: Top of region as fraction of page height (0.0–1.0).
            y_pct_end: Bottom of region as fraction of page height (0.0–1.0).

        Returns:
            OCR text from the region.
        """
        w, h = img.size
        y_start = int(h * y_pct_start)
        y_end = int(h * y_pct_end)

        if y_start >= y_end:
            return ""

        region = img.crop((0, y_start, w, y_end))
        try:
            return pytesseract.image_to_string(region).strip()
        except Exception as exc:
            logger.warning(f"OCR region failed: {exc}")
            return ""


# =============================================================================
# Standalone synchronous OCR
# =============================================================================


def run_sync_ocr(image_path: Path, dpi: int = 150) -> str:
    """
    Run Tesseract OCR synchronously on a page image.

    Used by heuristic split detection methods to avoid blocking
    the async pipeline. Runs directly via pytesseract without
    going through the async ocr_page() function.

    Args:
        image_path: Path to the rendered page PNG.
        dpi: Resolution hint (not used by Tesseract directly,
            but available for callers that need to re-render).

    Returns:
        Full OCR text from the page.
    """
    try:
        with Image.open(image_path) as img:
            return pytesseract.image_to_string(img).strip()
    except Exception as exc:
        logger.warning(f"Sync OCR failed for {image_path.name}: {exc}")
        return ""


# =============================================================================
# Helper functions
# =============================================================================


def _extract_page_number(text: str) -> int | None:
    """
    Extract a page number from footer OCR text.

    Looks for patterns like "1", "Page 1", "1/5", "- 1 -", etc.

    Args:
        text: OCR text from the footer region.

    Returns:
        Integer page number, or None if not found.
    """
    if not text:
        return None

    patterns = [
        r"page\s+(\d+)",
        r"(\d+)\s*/\s*\d+",
        r"[-\s](\d+)[-\s]",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return int(match.group(1))

    # Fallback: find all numbers and take the last one
    numbers = re.findall(r"\d+", text)
    if numbers:
        return int(numbers[-1])

    return None


def _first_meaningful_line(text: str) -> str:
    """
    Extract the first non-empty, meaningful line from OCR text.

    Args:
        text: OCR text from a page region.

    Returns:
        First meaningful line, or empty string.
    """
    if not text:
        return ""

    for line in text.split("\n"):
        stripped = line.strip()
        if stripped and len(stripped) > 1:
            return stripped

    for line in text.split("\n"):
        if line.strip():
            return line.strip()

    return ""


def _word_overlap(a: str, b: str) -> float:
    """
    Compute word-level overlap between two header strings.

    Returns Jaccard-like similarity: |a_words ∩ b_words| / max(|a|, |b|).

    Args:
        a: First header string.
        b: Second header string.

    Returns:
        Overlap ratio between 0.0 and 1.0.
    """
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())

    if not words_a and not words_b:
        return 1.0
    if not words_a or not words_b:
        return 0.0

    intersection = words_a & words_b
    return len(intersection) / max(len(words_a), len(words_b))


def _parse_vision_response(
    raw: str, batch_start: int, batch_end: int
) -> list[dict]:
    """
    Parse the vision model's JSON response into boundary dicts.

    Handles JSON wrapped in markdown code fences and converts
    1-indexed page numbers to 0-indexed.

    Args:
        raw: Raw response text from Ollama.
        batch_start: Starting page index (0-based) of the batch.
        batch_end: Ending page index (exclusive).

    Returns:
        List of dicts with page (0-based int) and confidence (float).
    """
    cleaned = raw.strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    if cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning(f"Vision response is not valid JSON: {raw[:200]}")
        return []

    boundaries = data.get("boundaries", [])
    if not isinstance(boundaries, list):
        return []

    result = []
    for b in boundaries:
        if not isinstance(b, dict):
            continue
        page = b.get("page")
        confidence = b.get("confidence", 0.5)
        if page is None:
            continue

        try:
            page_int = int(page)
            conf_float = float(confidence)
        except (ValueError, TypeError):
            continue

        # Convert 1-indexed model output to 0-indexed
        if 1 <= page_int <= batch_end:
            page_idx = page_int - 1
        elif 0 <= page_int < batch_end:
            page_idx = page_int
        else:
            logger.debug(f"Ignoring out-of-range boundary: page={page_int}")
            continue

        conf_float = max(0.0, min(1.0, conf_float))
        result.append({"page": page_idx, "confidence": conf_float})

    return result

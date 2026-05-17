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
BLANK_VARIANCE_THRESHOLD = 100
HEADER_PCT = 0.10
FOOTER_PCT = 0.15
CONTACT_SHEET_BATCH = 10
BOUNDARY_PROXIMITY = 2


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
    ) -> dict:
        """
        Main entry point for split detection.

        Dispatches to heuristic and/or vision detection based on
        the configured split_engine.

        Args:
            pdf_path: Path to the source PDF.
            page_images: List of rendered page image paths.
            settings: Application settings.

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

        # ---- Heuristic detection (runs for both "heuristic" and "hybrid") ----
        if engine in ("heuristic", "hybrid"):
            blank_pages = self._detect_blank_pages(page_images)
            logger.info(f"Blank pages detected: {blank_pages}")

            page_reset_boundaries = self._detect_page_number_reset(
                page_images, settings
            )
            logger.info(f"Page number reset boundaries: {page_reset_boundaries}")

            header_boundaries = self._detect_header_changes(
                page_images, settings
            )
            logger.info(f"Header change boundaries: {header_boundaries}")

            # Combine heuristic signals (deduplicate within proximity)
            raw_heuristic = sorted(set(page_reset_boundaries + header_boundaries))
            heuristic_boundaries = self._deduplicate_boundaries(raw_heuristic)
            # Exclude blank pages from boundaries
            heuristic_boundaries = [
                b for b in heuristic_boundaries if b not in blank_pages
            ]
            logger.info(f"Combined heuristic boundaries: {heuristic_boundaries}")

        # ---- Vision detection (hybrid only) ----
        if engine == "hybrid":
            vision_boundaries = await self._detect_vision(page_images, settings)
            logger.info(f"Vision boundaries: {vision_boundaries}")

        # ---- Merge and finalize ----
        if engine == "heuristic":
            boundaries = heuristic_boundaries
            confidences = [0.5] * len(boundaries)
        else:
            boundaries, confidences = self._merge_boundaries(
                heuristic_boundaries, vision_boundaries, total_pages
            )

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
        Detect blank pages using pixel variance.

        Pages with variance below BLANK_VARIANCE_THRESHOLD are
        considered blank (near-uniform color).

        Args:
            page_images: List of paths to rendered page PNGs.

        Returns:
            List of 0-based blank page indices.
        """
        blank = []
        for i, img_path in enumerate(page_images):
            try:
                with Image.open(img_path) as img:
                    variance = self._compute_pixel_variance(img)
                if variance < BLANK_VARIANCE_THRESHOLD:
                    blank.append(i)
                    logger.debug(f"Page {i}: blank (variance={variance:.1f})")
            except Exception as exc:
                logger.warning(f"Page {i}: failed to check blank status: {exc}")
        return blank

    def _compute_pixel_variance(self, img: Image.Image) -> float:
        """
        Compute approximate pixel variance for blank page detection.

        Uses a downsampled grayscale image for speed. Lower variance
        indicates a more uniform (potentially blank) page.

        Args:
            img: PIL Image of a rendered page.

        Returns:
            Approximate pixel variance as a float.
        """
        thumb = img.copy()
        thumb.thumbnail((200, 200), Image.LANCZOS)
        gray = thumb.convert("L")
        pixels = list(gray.getdata())
        if not pixels:
            return 0.0
        mean = sum(pixels) / len(pixels)
        variance = sum((p - mean) ** 2 for p in pixels) / len(pixels)
        return variance

    # ------------------------------------------------------------------
    # Heuristic: Page number reset detection
    # ------------------------------------------------------------------

    def _detect_page_number_reset(
        self, page_images: list[Path], settings: Settings
    ) -> list[int]:
        """
        Detect document boundaries by finding page number resets.

        OCRs the footer region (bottom FOOTER_PCT) of each page and
        looks for page numbers. If a page number resets to 1 or drops
        significantly, marks that page as a boundary.

        Args:
            page_images: List of paths to rendered page PNGs.
            settings: Application settings.

        Returns:
            List of 0-based page indices where new documents start.
        """
        boundaries: list[int] = []
        page_numbers: list[int | None] = []

        for i, img_path in enumerate(page_images):
            try:
                with Image.open(img_path) as img:
                    footer_text = self._ocr_region(img, 1.0 - FOOTER_PCT, 1.0)
                num = _extract_page_number(footer_text)
                page_numbers.append(num)
                logger.debug(
                    f"Page {i}: footer OCR='{footer_text[:80]}', number={num}"
                )
            except Exception as exc:
                logger.warning(f"Page {i}: footer OCR failed: {exc}")
                page_numbers.append(None)

        # Detect resets: track expected page number, flag resets
        expected: int | None = None
        for i, num in enumerate(page_numbers):
            if num is None:
                continue

            if expected is None:
                expected = num
                continue

            if num == 1:
                boundaries.append(i)
                expected = 1
            elif expected > 0 and num < expected - 2:
                boundaries.append(i)
                expected = num
            else:
                expected = num

        return boundaries

    # ------------------------------------------------------------------
    # Heuristic: Header change detection
    # ------------------------------------------------------------------

    def _detect_header_changes(
        self, page_images: list[Path], settings: Settings
    ) -> list[int]:
        """
        Detect document boundaries by finding header text changes.

        OCRs the header region (top HEADER_PCT) of each page and
        compares consecutive pages. If the header text changes
        substantially, marks that page as a boundary.

        Args:
            page_images: List of paths to rendered page PNGs.
            settings: Application settings.

        Returns:
            List of 0-based page indices where new documents start.
        """
        boundaries: list[int] = []
        headers: list[str] = []

        for i, img_path in enumerate(page_images):
            try:
                with Image.open(img_path) as img:
                    header_text = self._ocr_region(img, 0.0, HEADER_PCT)
                first_line = _first_meaningful_line(header_text)
                headers.append(first_line)
                logger.debug(f"Page {i}: header='{first_line[:80]}'")
            except Exception as exc:
                logger.warning(f"Page {i}: header OCR failed: {exc}")
                headers.append("")

        for i in range(1, len(headers)):
            prev = headers[i - 1]
            curr = headers[i]

            if not prev or not curr:
                continue

            overlap = _word_overlap(prev, curr)
            if overlap < 0.3:
                boundaries.append(i)
                logger.debug(
                    f"Header change: page {i - 1}->{i}, "
                    f"overlap={overlap:.2f}, "
                    f"prev='{prev[:60]}', curr='{curr[:60]}'"
                )

        return boundaries

    # ------------------------------------------------------------------
    # Vision model detection
    # ------------------------------------------------------------------

    async def _detect_vision(
        self, page_images: list[Path], settings: Settings
    ) -> list[dict]:
        """
        Use Ollama vision model to detect document boundaries.

        Sends batches of pages as contact sheet images to the vision
        model, which identifies where one document ends and another begins.

        Args:
            page_images: List of paths to rendered page PNGs.
            settings: Application settings.

        Returns:
            List of dicts with keys: page (int), confidence (float).
        """
        total_pages = len(page_images)
        all_boundaries: list[dict] = []

        for batch_start in range(0, total_pages, CONTACT_SHEET_BATCH):
            batch_end = min(batch_start + CONTACT_SHEET_BATCH, total_pages)
            batch_slice = page_images[batch_start:batch_end]

            try:
                contact_sheet = self._build_contact_sheet(
                    batch_slice, batch_start, batch_end
                )
                boundaries = await self._call_vision_model(
                    contact_sheet, batch_start, batch_end, total_pages, settings
                )
                all_boundaries.extend(boundaries)
                logger.info(
                    f"Vision batch pages {batch_start}-{batch_end - 1}: "
                    f"{len(boundaries)} boundaries found"
                )
            except Exception as exc:
                logger.warning(
                    f"Vision batch pages {batch_start}-{batch_end - 1} failed: {exc}"
                )

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
            f"Identify where one document ends and another begins. Look for:\n"
            f"  - Changes in letterhead, logos, or header styling\n"
            f"  - Page number resets\n"
            f"  - Changes in font, layout, or formatting\n"
            f"  - Cover pages or facing pages\n\n"
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
        vision_boundaries: list[dict],
        total_pages: int,
    ) -> tuple[list[int], list[float]]:
        """
        Merge heuristic and vision boundary lists.

        Deduplicates boundaries within BOUNDARY_PROXIMITY pages and
        assigns confidence scores. Vision confidences are preserved;
        heuristic get 0.8 if near a vision boundary, 0.5 otherwise.

        Args:
            heuristic_boundaries: Page indices from heuristic detection.
            vision_boundaries: List of {page, confidence} dicts from vision.
            total_pages: Total number of pages in the document.

        Returns:
            Tuple of (boundaries, confidences).
        """
        all_candidates: list[tuple[int, float]] = []
        vision_pages: set[int] = set()

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
                vision_pages.add(page_int)

        for hb in heuristic_boundaries:
            if hb <= 0 or hb >= total_pages:
                continue
            near_vision = any(
                abs(hb - vp) <= BOUNDARY_PROXIMITY for vp in vision_pages
            )
            conf = 0.8 if near_vision else 0.5
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

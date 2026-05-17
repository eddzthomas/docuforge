"""
DocuForge — Text Layer Quality Verifier
========================================
Statistical validation of OCR bounding boxes before they are
layered onto the PDF/A. Detects catastrophic alignment failures
without re-rendering or re-OCR.

Checks are fast CPU-only operations on bounding box data.
A score 0-100 gates whether text is applied to each page.
"""

import logging

logger = logging.getLogger(__name__)

MIN_POSITIONS = 3
MIN_FONT_PT = 3
MAX_FONT_PT = 72
MIN_COVERAGE_PCT = 1.0
MAX_COVERAGE_PCT = 95.0
MAX_BBOX_AREA_PCT = 90.0


def validate_text_layer(
    ocr_results: list[dict],
    dpi: int,
    page_pixel_dims: list[tuple[int, int]],
) -> dict:
    """
    Run statistical checks on all pages' OCR bounding boxes.

    Only validates pages with tier=="word" and non-empty words lists.
    Pages with tier "line", "full_page", or "skip" are excluded from
    validation (no bboxes to check) and do not affect the score.

    Args:
        ocr_results: List of OCR result dicts, one per page.
        dpi: Rendering DPI used for OCR.
        page_pixel_dims: List of (width_px, height_px) per page from
            the rendered images.

    Returns:
        dict: {
            valid: bool — True if overall score >= 50
            score: int — 0-100 aggregate across pages
            warnings: list[str] — human-readable per-page warnings
            page_scores: list[int] — per-page scores (100 for non-word pages)
        }
    """
    scale = 72.0 / dpi
    total_pages = len(ocr_results)
    page_scores = []
    all_warnings = []
    deducted_pages = 0

    for page_idx, ocr in enumerate(ocr_results):
        dims = page_pixel_dims[page_idx] if page_idx < len(page_pixel_dims) else (0, 0)
        page_w, page_h = dims

        # Only validate word-tier pages with bboxes
        if ocr.get("tier") != "word" or not ocr.get("words"):
            page_scores.append(100)
            continue

        words = ocr["words"]
        page_score = 100
        page_warnings = []

        # Check 1: Bounds — all bboxes within page dimensions
        bounds_ok, bounds_warn = _check_bounds(words, page_w, page_h)
        if not bounds_ok:
            page_score -= 10
            page_warnings.append(bounds_warn)

        # Check 2: Position diversity — at least N unique positions
        div_ok, div_warn = _check_position_diversity(words)
        if not div_ok:
            page_score -= 15
            page_warnings.append(div_warn)

        # Check 3: Area sanity — no zero-area or oversized bboxes
        area_ok, area_warn = _check_area_sanity(words, page_w, page_h)
        if not area_ok:
            page_score -= 5
            page_warnings.append(area_warn)

        # Check 4: Coverage — text layer covers reasonable portion
        cov_ok, cov_warn = _check_coverage(words, page_w, page_h)
        if not cov_ok:
            page_score -= 5
            page_warnings.append(cov_warn)

        # Check 5: Font sizes — computed fonts 3-72 pt
        font_ok, font_warn = _check_font_sizes(words, scale)
        if not font_ok:
            page_score -= 5
            page_warnings.append(font_warn)

        if page_score < 100:
            deducted_pages += 1

        page_score = max(page_score, 0)
        page_scores.append(page_score)

        if page_warnings:
            all_warnings.append(
                f"Page {page_idx + 1} (score {page_score}): {'; '.join(page_warnings)}"
            )

    # Compute aggregate score
    if total_pages == 0:
        overall_score = 100
    elif deducted_pages == 0:
        overall_score = 100
    else:
        avg = sum(page_scores) / total_pages
        overall_score = int(avg)

    logger.info(
        f"Verification: score={overall_score}, {deducted_pages}/{total_pages} pages "
        f"with warnings, {len(all_warnings)} total warnings"
    )

    return {
        "valid": overall_score >= 50,
        "score": overall_score,
        "warnings": all_warnings,
        "page_scores": page_scores,
    }


def _check_bounds(
    words: list[dict], page_w: int, page_h: int
) -> tuple[bool, str]:
    """
    Verify all bboxes fall within page image pixel dimensions.

    Args:
        words: Per-word OCR entries with bbox [x, y, w, h].
        page_w: Page width in pixels.
        page_h: Page height in pixels.

    Returns:
        (ok, warning_string).
    """
    out_of_bounds = 0
    for w in words:
        bbox = w.get("bbox", [])
        if len(bbox) != 4:
            continue
        x, y, bw, bh = bbox
        if x < -5 or y < -5 or x + bw > page_w + 5 or y + bh > page_h + 5:
            out_of_bounds += 1

    if out_of_bounds > 0:
        return False, f"{out_of_bounds} bboxes out of page bounds"
    return True, ""


def _check_position_diversity(words: list[dict]) -> tuple[bool, str]:
    """
    Ensure at least MIN_POSITIONS unique (x, y) positions exist.

    Catches GLM-OCR hallucination where all words get the same coordinate.

    Args:
        words: Per-word OCR entries.

    Returns:
        (ok, warning_string).
    """
    positions = set()
    for w in words:
        bbox = w.get("bbox", [])
        if len(bbox) >= 2:
            positions.add((int(bbox[0]), int(bbox[1])))

    if len(positions) < MIN_POSITIONS:
        return False, f"Only {len(positions)} unique positions (need {MIN_POSITIONS}+)"
    return True, ""


def _check_area_sanity(
    words: list[dict], page_w: int, page_h: int
) -> tuple[bool, str]:
    """
    Detect zero-area or oversized (>90% page) bounding boxes.

    Args:
        words: Per-word OCR entries.
        page_w: Page width in pixels.
        page_h: Page height in pixels.

    Returns:
        (ok, warning_string).
    """
    page_area = page_w * page_h
    if page_area <= 0:
        return True, ""

    zero_area = 0
    oversized = 0
    for w in words:
        bbox = w.get("bbox", [])
        if len(bbox) != 4:
            continue
        bw, bh = bbox[2], bbox[3]
        if bw <= 0 or bh <= 0:
            zero_area += 1
        elif (bw * bh) > (page_area * MAX_BBOX_AREA_PCT / 100.0):
            oversized += 1

    warnings = []
    if zero_area > 0:
        warnings.append(f"{zero_area} zero-area bboxes")
    if oversized > 0:
        warnings.append(f"{oversized} oversized bboxes")
    if warnings:
        return False, "; ".join(warnings)
    return True, ""


def _check_coverage(
    words: list[dict], page_w: int, page_h: int
) -> tuple[bool, str]:
    """
    Verify text layer covers a reasonable portion of the page (1-95%).

    Too-low coverage: likely OCR failed or text was missed.
    Too-high coverage: likely hallucinated giant bbox covering everything.

    Args:
        words: Per-word OCR entries.
        page_w: Page width in pixels.
        page_h: Page height in pixels.

    Returns:
        (ok, warning_string).
    """
    page_area = page_w * page_h
    if page_area <= 0:
        return True, ""

    # Compute union bounding box of all words
    min_x, min_y = float("inf"), float("inf")
    max_x, max_y = float("-inf"), float("-inf")
    for w in words:
        bbox = w.get("bbox", [])
        if len(bbox) != 4:
            continue
        x, y, bw, bh = bbox
        if bw <= 0 or bh <= 0:
            continue
        min_x = min(min_x, x)
        min_y = min(min_y, y)
        max_x = max(max_x, x + bw)
        max_y = max(max_y, y + bh)

    if min_x == float("inf"):
        return True, ""

    covered_area = (max_x - min_x) * (max_y - min_y)
    coverage_pct = (covered_area / page_area) * 100

    if coverage_pct < MIN_COVERAGE_PCT:
        return False, f"Text covers only {coverage_pct:.1f}% of page (min {MIN_COVERAGE_PCT}%)"
    if coverage_pct > MAX_COVERAGE_PCT:
        return False, f"Text covers {coverage_pct:.1f}% of page (max {MAX_COVERAGE_PCT}%)"
    return True, ""


def _check_font_sizes(
    words: list[dict], scale: float
) -> tuple[bool, str]:
    """
    Verify computed PDF font sizes fall within 3-72 pt range.

    Font size = bbox_height_px * scale * 0.85.

    Args:
        words: Per-word OCR entries.
        scale: Pixel-to-point scale factor (72 / dpi).

    Returns:
        (ok, warning_string).
    """
    out_of_range = 0
    for w in words:
        bbox = w.get("bbox", [])
        if len(bbox) != 4:
            continue
        h_px = bbox[3]
        if h_px <= 0:
            continue
        font_pt = h_px * scale * 0.85
        if font_pt < MIN_FONT_PT or font_pt > MAX_FONT_PT:
            out_of_range += 1

    if out_of_range > 0:
        return (
            False,
            f"{out_of_range} words have font sizes outside {MIN_FONT_PT}-{MAX_FONT_PT}pt",
        )
    return True, ""

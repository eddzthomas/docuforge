"""
DocuForge — OCR Engine (GLM-OCR via Ollama)
=============================================
Sends rendered page images to the local Ollama instance running
GLM-OCR, parses the structured JSON response, and returns
recognized text with per-word bounding boxes.

Defensive fallback: if bounding boxes are missing or malformed,
falls back to per-line → full-page → skip-page tiers.
"""

import base64
import json
import logging
from pathlib import Path

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class OCRError(Exception):
    """
    Raised when OCR fails due to an unreachable Ollama instance,
    model not pulled, or a malformed API response.

    The original exception is chained via 'from' for debugging.
    """

    pass


async def ocr_page(image_path: Path, settings: Settings) -> dict:
    """
    Send a single rendered page image to GLM-OCR via Ollama
    and return structured text with bounding boxes.

    Args:
        image_path: Absolute path to the rendered page PNG (e.g., 300 DPI).
        settings: Application settings with Ollama host and model name.

    Returns:
        dict with keys:
            - "full_text": str   — Complete recognized text for this page
            - "words": list[dict] — [{"text": str, "bbox": [x, y, w, h]}, ...]
              Empty list if the model did not return bounding boxes.
            - "tier": str        — "word" | "line" | "full_page" for debugging
            - "ocr_success": bool — True if the API call succeeded

    Raises:
        OCRError: If the Ollama API is completely unreachable
                  or the model returns a non-JSON response.
    """
    # 1. Read the rendered page PNG and encode as base64
    #    Ollama's vision API accepts base64-encoded images in the 'images' list
    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    # 2. Build the OCR prompt — instructs GLM-OCR to return per-word bounding boxes
    #    The prompt is specific about the JSON schema to get structured output
    prompt = (
        "Extract ALL text from this document image.\n"
        "For each word on the page, provide:\n"
        "  - The word text exactly as it appears\n"
        "  - Its bounding box as [x, y, width, height] in pixels\n"
        "    where (0,0) is the top-left corner of the image.\n\n"
        "Return ONLY valid JSON in this exact format:\n"
        '{\n'
        '  "full_text": "all text with line breaks as \\n",\n'
        '  "words": [\n'
        '    {"text": "Example", "bbox": [100, 50, 72, 14]},\n'
        '    {"text": "word", "bbox": [180, 50, 38, 14]}\n'
        '  ]\n'
        '}\n\n'
        "IMPORTANT: Return ONLY the JSON object, no markdown, no explanation."
    )

    # 3. Construct the Ollama API payload
    payload = {
        "model": settings.ocr_model,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "format": "json",  # Request structured JSON output from Ollama
        "options": {
            "temperature": 0.0,  # Zero temperature for deterministic OCR
        },
    }

    # 4. Call the Ollama /api/generate endpoint
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{settings.ollama_host}/api/generate",
                json=payload,
            )
            response.raise_for_status()
    except httpx.ConnectError as exc:
        raise OCRError(
            f"Cannot connect to Ollama at {settings.ollama_host}. "
            f"Is the ollama service running?"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise OCRError(
            f"Ollama API returned HTTP {exc.response.status_code}: "
            f"{exc.response.text[:300]}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise OCRError(
            f"Ollama timed out after 120s for page: {image_path.name}"
        ) from exc
    except httpx.HTTPError as exc:
        raise OCRError(f"Ollama API call failed: {exc}") from exc

    # 5. Parse the response — Ollama returns JSON in the "response" field
    result = response.json()
    raw_response = result.get("response", "")

    # 6. Defensive parsing — handle cases where the model wraps JSON in markdown
    parsed = _parse_ocr_response(raw_response, image_path.name)
    return parsed


def _parse_ocr_response(raw: str, page_name: str) -> dict:
    """
    Parse the raw OCR response with defensive fallback tiers.

    Tier 1 — Per-word bounding boxes (ideal)
    Tier 2 — Full text only, no valid words array
    Tier 3 — Raw text, no JSON structure at all

    Args:
        raw: Raw text from the Ollama response.
        page_name: Page identifier for log messages.

    Returns:
        Standardized dict with full_text, words, tier, ocr_success.
    """
    # ---- Tier 1: Attempt to parse as JSON ----
    try:
        data = json.loads(raw)
        full_text = data.get("full_text", "")
        words = data.get("words", [])

        # Validate that words is a list of dicts with text + bbox
        valid_words = []
        for w in words:
            if (
                isinstance(w, dict)
                and "text" in w
                and isinstance(w.get("bbox"), list)
                and len(w["bbox"]) == 4
            ):
                valid_words.append(w)

        if valid_words:
            logger.info(f"[{page_name}] Tier 1 — {len(valid_words)} words with bounding boxes")
            return {
                "full_text": full_text,
                "words": valid_words,
                "tier": "word",
                "ocr_success": True,
            }
        else:
            # JSON parsed but words array empty/invalid — fallback to per-line
            logger.warning(
                f"[{page_name}] Tier 2 — JSON received but no valid word bounding boxes. "
                f"Falling back to per-line text placement."
            )
            return {
                "full_text": full_text,
                "words": [],
                "tier": "line",
                "ocr_success": True,
            }

    except json.JSONDecodeError:
        # ---- Tier 2/3: JSON parse failed — treat raw text as full_page ----
        # Strip common markdown wrappers that LLMs sometimes add
        cleaned = raw.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        if cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

        # Try parsing again after cleanup
        try:
            data = json.loads(cleaned)
            full_text = data.get("full_text", "")
            logger.info(
                f"[{page_name}] Tier 2 — JSON recovered after stripping markdown. "
                f"No bounding boxes."
            )
            return {
                "full_text": full_text,
                "words": [],
                "tier": "line",
                "ocr_success": True,
            }
        except json.JSONDecodeError:
            pass

        # ---- Tier 3: Use the raw text as full_page fallback ----
        if cleaned:
            logger.warning(
                f"[{page_name}] Tier 3 — Could not parse JSON. "
                f"Using raw text as full-page text block."
            )
            return {
                "full_text": cleaned,
                "words": [],
                "tier": "full_page",
                "ocr_success": True,
            }
        else:
            # ---- Tier 4: Empty response ----
            logger.warning(f"[{page_name}] Tier 4 — Empty OCR response. Skipping text layer.")
            return {
                "full_text": "",
                "words": [],
                "tier": "skip",
                "ocr_success": False,
            }

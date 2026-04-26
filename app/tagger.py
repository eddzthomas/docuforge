"""
DocuForge — LLM Renaming & Tagging
===================================
Sends OCR'd document text to a local LLM via Ollama to generate
a descriptive filename and up to 5 tags for the document.

Uses the same defensive parsing pattern as the OCR module:
  - Attempt structured JSON first
  - Strip markdown wrappers and retry
  - Fall back to sensible defaults if all parsing fails
"""

import json
import logging
import re
from pathlib import Path

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

# Maximum OCR text length sent to the LLM (chars).
# Truncating avoids blowing the model's context window while
# keeping enough text for accurate classification.
MAX_OCR_TEXT_LENGTH = 4000

# Maximum number of tags allowed (matches PRD spec)
MAX_TAGS = 5


class TaggingError(Exception):
    """
    Raised when the rename/tag LLM call fails due to an
    unreachable Ollama instance, model not pulled, or
    unparseable response.
    """

    pass


async def generate_name_and_tags(
    ocr_text: str,
    prompt_template: str,
    settings: Settings,
) -> dict:
    """
    Send document OCR text to an LLM and get back a filename + tags.

    Args:
        ocr_text: The full OCR'd text from the document.
        prompt_template: A prompt template string containing {ocr_text}.
        settings: Application settings (tagging model, Ollama host).

    Returns:
        dict with keys:
            - "filename": str  — Proposed filename (without extension)
            - "tags": list[str] — Up to 5 relevant tags

    Raises:
        TaggingError: If Ollama is unreachable or the response is broken.
    """
    if not ocr_text or not ocr_text.strip():
        logger.warning("No OCR text available for tagging — using defaults")
        return {"filename": "untitled", "tags": []}

    # Truncate OCR text to avoid blowing the LLM context window
    truncated = ocr_text[:MAX_OCR_TEXT_LENGTH].strip()

    # Inject OCR text into the prompt template
    prompt = prompt_template.replace("{ocr_text}", truncated)

    # Build the Ollama API payload
    payload = {
        "model": settings.tagging_model,
        "prompt": prompt,
        "stream": False,
        "format": "json",  # Request structured JSON output
        "options": {
            "temperature": 0.0,  # Deterministic output for naming
        },
    }

    # Call the Ollama API
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{settings.ollama_host}/api/generate",
                json=payload,
            )
            response.raise_for_status()
    except httpx.ConnectError as exc:
        raise TaggingError(
            f"Cannot connect to Ollama at {settings.ollama_host}"
        ) from exc
    except httpx.HTTPStatusError as exc:
        raise TaggingError(
            f"Ollama API returned HTTP {exc.response.status_code}"
        ) from exc
    except httpx.TimeoutException as exc:
        raise TaggingError("Ollama timed out after 60s for tagging") from exc
    except httpx.HTTPError as exc:
        raise TaggingError(f"Ollama API call failed: {exc}") from exc

    # Parse the response
    result = response.json()
    raw_response = result.get("response", "")

    return _parse_tagging_response(raw_response)


def _parse_tagging_response(raw: str) -> dict:
    """
    Parse the LLM tagging response with defensive fallback.

    Tier 1: Valid JSON with filename + tags
    Tier 2: Strip markdown wrappers, retry JSON parse
    Tier 3: Use safe defaults

    Args:
        raw: Raw text response from Ollama.

    Returns:
        Normalized dict with filename and tags.
    """
    # ---- Tier 1: Direct JSON parse ----
    try:
        data = json.loads(raw)
        return _validate_and_clean(data)
    except json.JSONDecodeError:
        pass

    # ---- Tier 2: Strip markdown code fences and retry ----
    cleaned = raw.strip()
    # Remove ```json or ``` wrappers that LLMs sometimes add
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    try:
        data = json.loads(cleaned)
        logger.info("JSON recovered after stripping markdown wrappers")
        return _validate_and_clean(data)
    except json.JSONDecodeError:
        pass

    # ---- Tier 3: Extract JSON object from text via regex ----
    # Some models return JSON embedded in explanatory text.
    match = re.search(r'\{[^{}]*"filename"\s*:.*?\}', raw, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            logger.info("JSON extracted from text via regex")
            return _validate_and_clean(data)
        except json.JSONDecodeError:
            pass

    # ---- Tier 4: Ultimate fallback ----
    logger.warning(
        f"Could not parse tagging response. "
        f"Raw (first 200 chars): {raw[:200]}"
    )
    return {"filename": "untitled", "tags": []}


def _validate_and_clean(data: dict) -> dict:
    """
    Validate and normalize the LLM response for filename and tags.

    Enforces constraints:
      - filename must be a non-empty string
      - tags must be a list of strings, max MAX_TAGS items
      - Strips whitespace and unsafe characters

    Args:
        data: Raw parsed JSON from the LLM.

    Returns:
        Cleaned dict with filename and tags.
    """
    # Validate filename
    filename = str(data.get("filename", "")).strip()
    if not filename or len(filename) > 200:
        filename = "untitled"

    # Validate tags
    raw_tags = data.get("tags", [])
    if not isinstance(raw_tags, list):
        raw_tags = []

    tags = []
    for tag in raw_tags:
        tag_str = str(tag).strip()
        # Skip empty, overly long, or duplicate tags
        if tag_str and len(tag_str) <= 50 and tag_str not in tags:
            tags.append(tag_str)
        if len(tags) >= MAX_TAGS:
            break

    logger.info(f"Tagging result: filename='{filename}', tags={tags}")
    return {"filename": filename, "tags": tags}


def sanitize_filename(raw: str, extension: str = ".pdf") -> str:
    """
    Sanitize a user or LLM-provided filename for safe filesystem storage.

    Operations:
      1. Strip path separators (keep only the base name)
      2. Replace unsafe characters with underscores
      3. Collapse multiple underscores and spaces
      4. Ensure the extension is present
      5. Truncate to 200 characters total

    Args:
        raw: The proposed filename (may contain unsafe chars, spaces, etc.).
        extension: File extension to ensure (e.g., ".pdf").

    Returns:
        A safe, cleaned filename string.
    """
    # Strip any directory path, keep only the base filename
    name = Path(raw).name

    # Remove the extension if present so we can re-add it cleanly
    if name.lower().endswith(extension.lower()):
        name = name[: -len(extension)]
    elif "." in name:
        # Remove any other extension that might be present
        name = name.rsplit(".", 1)[0]

    # Replace unsafe characters with underscores
    unsafe_chars = r'<>:"/\|?*'
    for ch in unsafe_chars:
        name = name.replace(ch, "_")

    # Collapse multiple underscores into one
    while "__" in name:
        name = name.replace("__", "_")

    # Collapse multiple spaces into one underscore
    name = re.sub(r"\s+", "_", name)

    # Strip leading/trailing underscores and dots
    name = name.strip("_. ")

    # If empty after sanitization, use default
    if not name:
        name = "untitled"

    # Truncate (leave room for extension)
    max_stem = 200 - len(extension)
    if len(name) > max_stem:
        name = name[:max_stem].rstrip("_")

    return name + extension

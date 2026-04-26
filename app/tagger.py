"""
DocuForge — LLM Renaming & Tagging
===================================
Two independent functions:
  - generate_filename() — LLM suggests a descriptive filename
  - generate_tags()     — LLM suggests up to 5 relevant tags

Each has its own focused Ollama call for better output quality.
The functions can be called independently (skip rename but still tag,
or skip tags but still rename).
"""

import json
import logging
import re
from pathlib import Path

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)

MAX_OCR_TEXT_LENGTH = 4000
MAX_TAGS = 5


class TaggingError(Exception):
    """Raised when the rename/tag LLM call fails."""
    pass


async def generate_filename(
    ocr_text: str,
    settings: Settings,
) -> str:
    """
    Ask the LLM to suggest a descriptive filename from OCR text.

    Args:
        ocr_text: The full OCR'd text from the document.
        settings: Application settings (tagging model, Ollama host).

    Returns:
        Suggested filename (without extension), sanitized for filesystem.

    Raises:
        TaggingError: If Ollama is unreachable or the response is broken.
    """
    if not ocr_text or not ocr_text.strip():
        return "untitled"

    truncated = ocr_text[:MAX_OCR_TEXT_LENGTH].strip()

    prompt = (
        "Analyze the following document text and suggest a concise, "
        "descriptive filename (without extension, max 150 chars, "
        "use underscores between words). Include the date if present. "
        "Return ONLY a single filename string, no JSON, no explanation.\n\n"
        f"Document text:\n{truncated}"
    )

    raw = await _call_ollama(prompt, settings)

    # Parse: LLM may return plain text, JSON, or JSON-wrapped text
    name = _extract_filename(raw)
    if not name:
        return "untitled"
    return name[:200]


def _extract_filename(raw: str) -> str:
    """Extract a filename from raw LLM output, handling JSON and plain text."""
    raw = raw.strip()
    # Try JSON: {"filename": "..."} or just a quoted string
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            name = data.get("filename") or list(data.keys())[0]
            return str(name).strip().strip("\"'")
        if isinstance(data, str):
            return data.strip().strip("\"'")
    except json.JSONDecodeError:
        pass
    # Plain text: take first non-empty line
    lines = [l.strip().strip("\"'") for l in raw.split("\n") if l.strip()]
    if lines:
        return lines[0]
    return ""


async def generate_tags(
    ocr_text: str,
    settings: Settings,
) -> list[str]:
    """
    Ask the LLM to suggest up to 5 tags from OCR text.

    Args:
        ocr_text: The full OCR'd text from the document.
        settings: Application settings (tagging model, Ollama host).

    Returns:
        List of up to 5 tag strings.

    Raises:
        TaggingError: If Ollama is unreachable or the response is broken.
    """
    if not ocr_text or not ocr_text.strip():
        return []

    truncated = ocr_text[:MAX_OCR_TEXT_LENGTH].strip()

    prompt = (
        "Analyze the following document text and suggest up to 5 tags "
        "that describe the document type, parties, subject, and year. "
        "Return ONLY a JSON array of strings, like: "
        '["invoice", "acme-corp", "2025"]. No explanation.\n\n'
        f"Document text:\n{truncated}"
    )

    raw = await _call_ollama(prompt, settings)

    return _parse_tag_array(raw)


async def _call_ollama(prompt: str, settings: Settings) -> str:
    """Send a prompt to Ollama and return the raw response text."""
    payload = {
        "model": settings.tagging_model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0.0},
    }

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                f"{settings.ollama_host}/api/generate",
                json=payload,
            )
            response.raise_for_status()
    except httpx.ConnectError as exc:
        raise TaggingError(f"Cannot connect to Ollama at {settings.ollama_host}") from exc
    except httpx.HTTPStatusError as exc:
        raise TaggingError(f"Ollama API returned HTTP {exc.response.status_code}") from exc
    except httpx.TimeoutException as exc:
        raise TaggingError("Ollama timed out after 60s") from exc
    except httpx.HTTPError as exc:
        raise TaggingError(f"Ollama API call failed: {exc}") from exc

    result = response.json()
    return result.get("response", "")


def _parse_tag_array(raw: str) -> list[str]:
    """Parse a JSON array of tags from LLM output with defensive fallback."""
    # Tier 1: Direct JSON parse
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            tags = [str(t).strip() for t in data if str(t).strip()]
            return tags[:MAX_TAGS]
        if isinstance(data, dict) and "tags" in data:
            tags = [str(t).strip() for t in data["tags"] if str(t).strip()]
            return tags[:MAX_TAGS]
    except json.JSONDecodeError:
        pass

    # Tier 2: Strip markdown wrappers
    cleaned = raw.strip()
    for prefix in ("```json", "```"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):].strip()
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3].strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, list):
            return [str(t).strip() for t in data if str(t).strip()][:MAX_TAGS]
    except json.JSONDecodeError:
        pass

    # Tier 3: Regex extraction
    match = re.search(r'\[[^\]]*\]', raw)
    if match:
        try:
            data = json.loads(match.group())
            if isinstance(data, list):
                return [str(t).strip() for t in data if str(t).strip()][:MAX_TAGS]
        except json.JSONDecodeError:
            pass

    # Tier 4: Comma-separated plain text
    parts = [p.strip().strip('"') for p in raw.split(",") if p.strip()]
    if parts:
        return parts[:MAX_TAGS]

    logger.warning(f"Could not parse tag response. Raw: {raw[:200]}")
    return []


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

"""
DocuForge — Configuration Loader
=================================
Loads settings from environment variables with sensible defaults,
then overrides with values from /data/settings.json if present.

Settings saved via the web UI persist to settings.json and
survive container rebuilds via a Docker volume.
"""

import json
import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

from pydantic import field_validator
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)

# Path where UI-saved settings are persisted
CONFIG_JSON_PATH = Path(os.getenv("CONFIG_PATH", "/data/settings.json"))


class Settings(BaseSettings):
    """
    Application-wide configuration loaded from environment variables.

    All values have defaults so the app starts with zero configuration.
    Override via .env file or docker-compose environment block.
    UI-saved settings in settings.json take final priority.
    """

    # ---- Ollama ----
    ollama_host: str = "http://ollama:11434"
    ocr_model: str = "glm-ocr"
    ocr_engine: str = "tesseract"  # "tesseract" (CPU) or "glm-ocr" (Ollama vision model)
    tagging_model: str = "llama3.2"
    classify_model: str = "llama3.2"  # Sprint 8 — model for document classification

    # ---- Processing ----
    dpi: int = 300
    pdfa_level: str = "2b"

    # ---- File Handling ----
    upload_folder: str = "/data/uploads"
    output_folder: str = "/data/output"
    max_file_size_mb: int = 100
    watch_interval: int = 5

    # ---- Renaming & Tagging ----
    auto_rename: bool = False
    rename_prompt: str = (
        "You are a document archivist. Given the following OCR'd text from a document, suggest:\n"
        "1. A concise, descriptive filename (without extension, max 150 chars). "
        "Use underscores between words. Include the date if present.\n"
        "2. Up to 5 relevant tags that describe the document type, parties, subject, and year.\n\n"
        "Return ONLY valid JSON in this exact format:\n"
        '{"filename": "suggested_name", "tags": ["tag1", "tag2", "tag3"]}\n\n'
        "Document text:\n{ocr_text}"
    )
    # Sprint 8 — Type-specific rename prompts (optional, fall back to rename_prompt)
    rename_prompt_invoice: Optional[str] = None
    rename_prompt_contract: Optional[str] = None

    # Sprint 8 — Text layer quality verification
    verify_text_layer: bool = True
    verify_min_score: int = 50

    # Sprint 8 — Structured field extraction
    extract_fields: bool = True
    extract_model: str = "llama3.2"

    # Sprint 7 — Smart PDF splitting
    split_engine: str = "hybrid"  # "heuristic", "hybrid", or "off"
    split_dpi: int = 150
    split_confidence: float = 0.7
    split_model: str = "glm-ocr"
    split_min_pages: int = 3

    # ---- Allowed file extensions ----
    allowed_extensions: set[str] = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp"}

    # ---- Field Validators ----
    @field_validator("dpi")
    @classmethod
    def validate_dpi(cls, v: int) -> int:
        """DPI must be between 72 and 600 for sensible OCR quality."""
        if not 72 <= v <= 600:
            raise ValueError(f"DPI must be between 72 and 600, got {v}")
        return v

    @field_validator("max_file_size_mb")
    @classmethod
    def validate_max_file_size(cls, v: int) -> int:
        """File size limit between 1 and 500 MB."""
        if not 1 <= v <= 500:
            raise ValueError(f"Max file size must be between 1 and 500 MB, got {v}")
        return v

    @field_validator("watch_interval")
    @classmethod
    def validate_watch_interval(cls, v: int) -> int:
        """Watch interval must be between 1 and 60 seconds."""
        if not 1 <= v <= 60:
            raise ValueError(f"Watch interval must be between 1 and 60 seconds, got {v}")
        return v

    @field_validator("ollama_host")
    @classmethod
    def validate_ollama_host(cls, v: str) -> str:
        """Ollama host must be a valid HTTP URL."""
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError(f"Ollama host must start with http:// or https://, got '{v}'")
        return v.rstrip("/")

    @field_validator("ocr_model", "tagging_model")
    @classmethod
    def validate_model_name(cls, v: str) -> str:
        """Model names must be non-empty."""
        v = v.strip()
        if not v:
            raise ValueError("Model name cannot be empty")
        return v

    @field_validator("rename_prompt")
    @classmethod
    def validate_rename_prompt(cls, v: str) -> str:
        """Rename prompt must contain the {ocr_text} placeholder."""
        v = v.strip()
        if "{ocr_text}" not in v:
            raise ValueError("Rename prompt must contain the {ocr_text} placeholder")
        return v

    @field_validator("rename_prompt_invoice", "rename_prompt_contract")
    @classmethod
    def validate_type_specific_prompts(cls, v: str | None) -> str | None:
        """Type-specific prompts are optional, but if set must contain {ocr_text}."""
        if v is not None:
            v = v.strip()
            if v and "{ocr_text}" not in v:
                raise ValueError("Type-specific rename prompt must contain the {ocr_text} placeholder")
        return v

    @field_validator("split_dpi")
    @classmethod
    def validate_split_dpi(cls, v: int) -> int:
        """Split detection DPI must be between 72 and 600."""
        if not 72 <= v <= 600:
            raise ValueError(f"Split DPI must be between 72 and 600, got {v}")
        return v

    @field_validator("split_confidence")
    @classmethod
    def validate_split_confidence(cls, v: float) -> float:
        """Split confidence must be between 0 and 1."""
        if not 0 <= v <= 1:
            raise ValueError(f"Split confidence must be between 0 and 1, got {v}")
        return v

    @field_validator("split_min_pages")
    @classmethod
    def validate_split_min_pages(cls, v: int) -> int:
        """Split minimum pages must be at least 2."""
        if v < 2:
            raise ValueError(f"Split min pages must be at least 2, got {v}")
        return v

    @field_validator("split_engine")
    @classmethod
    def validate_split_engine(cls, v: str) -> str:
        """Split engine must be one of: heuristic, hybrid, off."""
        v = v.strip().lower()
        if v not in ("heuristic", "hybrid", "off"):
            raise ValueError(f"Split engine must be heuristic, hybrid, or off, got '{v}'")
        return v

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


def load_settings_from_json() -> dict:
    """
    Load UI-saved settings overrides from the JSON config file.

    Returns empty dict if the file doesn't exist or is corrupt.
    """
    if not CONFIG_JSON_PATH.exists():
        return {}

    try:
        data = json.loads(CONFIG_JSON_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.warning("settings.json is not a dict — ignoring")
            return {}
        return data
    except json.JSONDecodeError:
        logger.warning("settings.json is corrupt — ignoring")
        return {}
    except Exception:
        logger.exception("Failed to read settings.json")
        return {}


def save_settings_to_json(data: dict):
    """
    Persist settings overrides to the JSON config file.

    Only values that differ from the pydantic defaults are saved,
    keeping the file small and readable.

    Args:
        data: Dict of settings key-value pairs to persist.
    """
    # Compute defaults by instantiating a fresh Settings (env only, no JSON)
    defaults = Settings().model_dump()

    # Keep only values that differ from defaults
    overrides = {}
    for key, value in data.items():
        if key in defaults and value != defaults.get(key):
            overrides[key] = value

    CONFIG_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_JSON_PATH.write_text(
        json.dumps(overrides, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    logger.info(f"Settings saved to {CONFIG_JSON_PATH}: {list(overrides.keys())}")


@lru_cache()
def get_settings() -> Settings:
    """
    Return a cached Settings instance.

    Loading order:
      1. pydantic-settings reads from .env (if present)
      2. settings.json overrides are merged on top

    Cached via lru_cache. Call reload_settings() to bust the cache
    after saving new settings via the UI.
    """
    # Start with .env defaults
    settings = Settings()

    # Merge JSON overrides on top
    json_overrides = load_settings_from_json()
    if json_overrides:
        try:
            # Rebuild with overrides — pydantic validates the merged result
            merged = Settings(**json_overrides)
            return merged
        except Exception as exc:
            logger.warning(f"settings.json has invalid values, falling back to .env: {exc}")

    return settings


def reload_settings():
    """
    Bust the settings cache so the next get_settings() call
    reads fresh config from disk.

    Called after POST /api/config saves new settings.
    """
    get_settings.cache_clear()
    logger.info("Settings cache cleared — next get_settings() loads fresh config")


def get_settings_dict() -> dict:
    """
    Return all current settings as a JSON-safe dict, for the
    GET /api/config endpoint.
    """
    s = get_settings()
    d = s.model_dump()
    # Convert sets to lists for JSON serialization
    d["allowed_extensions"] = sorted(d["allowed_extensions"])
    return d


def get_editable_fields() -> list[str]:
    """
    Fields that are safe to edit via the web UI.

    Path-related fields (upload_folder, output_folder) are excluded
    because they are container mounts, not runtime configuration.
    """
    return [
        "ollama_host",
        "ocr_model",
        "ocr_engine",
        "tagging_model",
        "classify_model",
        "dpi",
        "pdfa_level",
        "auto_rename",
        "rename_prompt",
        "rename_prompt_invoice",
        "rename_prompt_contract",
        "verify_text_layer",
        "verify_min_score",
        "extract_fields",
        "split_engine",
        "split_dpi",
        "split_confidence",
        "split_min_pages",
        "max_file_size_mb",
        "watch_interval",
    ]

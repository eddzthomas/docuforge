"""
DocuForge — Configuration Loader
=================================
Loads all settings from environment variables with sensible defaults.
Uses pydantic-settings for validation and typing.
"""

import os
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """
    Application-wide configuration loaded from environment variables.

    All values have defaults so the app starts with zero configuration.
    Override via .env file or docker-compose environment block.
    """

    # ---- Ollama ----
    ollama_host: str = "http://ollama:11434"
    ocr_model: str = "glm-ocr"
    tagging_model: str = "llama3.2"

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

    # ---- Allowed file extensions ----
    allowed_extensions: set[str] = {".pdf", ".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp"}

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


@lru_cache()
def get_settings() -> Settings:
    """
    Return a cached, singleton Settings instance.

    Caching ensures the .env file is parsed only once and the same
    Settings object is reused across all imports throughout the app.
    """
    return Settings()

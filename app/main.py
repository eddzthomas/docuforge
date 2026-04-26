"""
DocuForge — FastAPI Application
===============================
Main entry point for the web application.
Defines routes, mounts static files, and serves the UI.
"""

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.config import get_settings

# ---- App initialization ----
# Load settings once at startup via cached singleton
settings = get_settings()

# FastAPI app with metadata for OpenAPI docs
app = FastAPI(
    title="DocuForge",
    description="Intelligent Document Processing Pipeline — PDF/A + OCR + Tagging",
    version="1.0.0",
)

# Mount static files at /static so CSS/JS are served
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Jinja2 template engine pointing at our templates directory
templates = Jinja2Templates(directory="app/templates")


# =============================================================================
# Routes
# =============================================================================


@app.get("/")
async def index(request: Request):
    """
    Serve the main web UI (single-page app shell with three tabs).
    """
    return templates.TemplateResponse("index.html", {"request": request})


@app.get("/api/health")
async def health_check():
    """
    Health check endpoint for Docker HEALTHCHECK and load balancers.

    Returns the app status and Ollama connectivity state.
    This endpoint is called every 30s by Docker's HEALTHCHECK.
    """
    # Attempt to reach Ollama to report its status
    ollama_status = "disconnected"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{settings.ollama_host}/api/tags")
            if response.status_code == 200:
                ollama_status = "connected"
    except Exception:
        # Any failure means Ollama is unreachable — not fatal for the app
        ollama_status = "disconnected"

    return {
        "status": "ok",
        "service": "docuforge",
        "version": "1.0.0",
        "ollama": ollama_status,
    }


# =============================================================================
# Future Sprint Routes (placeholders — not yet implemented)
# =============================================================================
# Sprint 1: POST /api/upload, GET /api/jobs, GET /api/jobs/{id}, GET /api/download/{id}
# Sprint 2: POST /api/jobs/{id}/rename, PUT /api/jobs/{id}/metadata, GET /api/jobs/{id}/preview
# Sprint 4: POST /api/config, GET /api/config

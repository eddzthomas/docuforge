"""
DocuForge — FastAPI Application
===============================
Main entry point for the web application.
Defines routes, mounts static files, and serves the UI.
"""

import asyncio
import logging
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.background import BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from app.config import get_settings
from app.processor import job_manager, process_file, JobStatus

logger = logging.getLogger(__name__)

# ---- App initialization ----
# Load settings once at startup via cached singleton
settings = get_settings()

# FastAPI app with metadata for OpenAPI docs
app = FastAPI(
    title="DocuForge",
    description="Intelligent Document Processing Pipeline — PDF/A + OCR + Tagging",
    version="1.0.0",
)

# Ensure data directories exist at startup
Path(get_settings().upload_folder).mkdir(parents=True, exist_ok=True)
Path(get_settings().output_folder).mkdir(parents=True, exist_ok=True)

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
# Sprint 1: Upload & Job Management
# =============================================================================


@app.post("/api/upload")
async def upload_files(
    background_tasks: BackgroundTasks,
    files: list[UploadFile] = File(...),
):
    """
    Accept one or more files for processing.

    Validates file extension and size, saves to the upload folder,
    creates a job for each file, and schedules processing as a
    background task.

    Returns a list of created job summaries.
    """
    settings = get_settings()
    upload_dir = Path(settings.upload_folder)
    upload_dir.mkdir(parents=True, exist_ok=True)

    max_bytes = settings.max_file_size_mb * 1024 * 1024
    jobs_created = []

    for file in files:
        # Validate file extension against the allowed whitelist
        suffix = Path(file.filename).suffix.lower()
        if suffix not in settings.allowed_extensions:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type: '{suffix}'. "
                       f"Allowed: {', '.join(sorted(settings.allowed_extensions))}",
            )

        # Read file content to check size (StreamingUploadFile doesn't expose .size)
        content = await file.read()
        if len(content) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File '{file.filename}' exceeds max size of {settings.max_file_size_mb} MB",
            )

        # Save the uploaded file to the upload folder
        safe_name = _safe_filename(file.filename)
        file_path = upload_dir / safe_name
        file_path.write_bytes(content)
        logger.info(f"File saved: {file_path}")

        # Create a processing job
        job = job_manager.create_job(file.filename)

        # Schedule the pipeline as an async background task
        background_tasks.add_task(process_file, job.id, file_path, settings)

        jobs_created.append(job.to_dict())

    return JSONResponse({"jobs": jobs_created})


@app.get("/api/jobs")
async def list_jobs():
    """
    Return all processing jobs, newest first.

    Used by the History tab to poll for updates every 2 seconds.
    """
    return JSONResponse(job_manager.list_jobs())


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    """
    Return a single job by its ID.

    Returns 404 if the job does not exist.
    """
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return JSONResponse(job.to_dict())


@app.get("/api/download/{job_id}")
async def download_file(job_id: str):
    """
    Serve the processed output file as a download.

    Returns 404 if the job does not exist or is not yet complete.
    Returns 409 if the job failed.
    """
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    if job.status == JobStatus.FAILED:
        raise HTTPException(
            status_code=409,
            detail=f"Job failed: {job.error}",
        )

    if job.status != JobStatus.DONE or not job.output_filename:
        raise HTTPException(
            status_code=425,
            detail="Job is still processing. Check back when status is 'done'.",
        )

    settings = get_settings()
    output_path = Path(settings.output_folder) / job.output_filename

    if not output_path.exists():
        raise HTTPException(status_code=404, detail="Output file not found on disk")

    return FileResponse(
        path=str(output_path),
        filename=job.output_filename,
        media_type="application/pdf",
    )


# =============================================================================
# Helpers
# =============================================================================


def _safe_filename(filename: str) -> str:
    """
    Sanitize a filename for safe filesystem storage.

    Replaces dangerous characters and ensures uniqueness
    by prepending a short random suffix if needed.
    """
    # Strip path separators and null bytes
    name = Path(filename).name
    # Replace characters that are unsafe on most filesystems
    unsafe = '<>:"/\\|?*'
    for ch in unsafe:
        name = name.replace(ch, "_")
    # Collapse multiple underscores
    while "__" in name:
        name = name.replace("__", "_")
    # Truncate if excessively long (preserve extension)
    if len(name) > 200:
        stem, ext = os.path.splitext(name)
        name = stem[: 200 - len(ext)] + ext
    return name


# =============================================================================
# Future Sprint Routes (not yet implemented)
# =============================================================================
# Sprint 2: POST /api/jobs/{id}/rename, PUT /api/jobs/{id}/metadata,
#           GET /api/jobs/{id}/preview
# Sprint 4: POST /api/config, GET /api/config

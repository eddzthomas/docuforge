"""
DocuForge — FastAPI Application
===============================
Main entry point for the web application.
Defines routes, mounts static files, and serves the UI.
"""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from starlette.requests import Request

from app.config import (
    Settings,
    get_settings,
    get_settings_dict,
    get_editable_fields,
    save_settings_to_json,
    reload_settings,
)
from app.processor import (
    job_manager,
    job_queue,
    process_file,
    JobStatus,
    start_watcher,
    stop_watcher,
    get_watcher_status,
    log_event,
    get_recent_events,
)
from app.tagger import generate_filename, generate_tags, sanitize_filename, TaggingError
from app.pdf_utils import embed_tags_in_pdf

logger = logging.getLogger(__name__)

# ---- App initialization ----
# Load settings once at startup via cached singleton
settings = get_settings()

# ---- Lifespan — starts queue worker on app boot, stops on shutdown ----
@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifecycle manager.

    On startup:
      - Ensure data directories exist
      - Start the job queue worker (processes files one at a time)

    On shutdown:
      - Stop the queue worker
      - Stop the folder watcher if running
    """
    # Startup
    settings = get_settings()
    Path(settings.upload_folder).mkdir(parents=True, exist_ok=True)
    Path(settings.output_folder).mkdir(parents=True, exist_ok=True)

    # Start the queue worker as a background task
    worker_task = asyncio.create_task(job_queue.worker())
    log_event("info", "DocuForge started, queue worker running")
    logger.info("Job queue worker started via lifespan")

    yield  # App runs here

    # Shutdown
    logger.info("Shutting down — stopping queue worker and watcher")
    job_queue.stop()
    await stop_watcher()
    # Wait for the worker to drain and stop naturally via sentinel
    try:
        await asyncio.wait_for(worker_task, timeout=30.0)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


# FastAPI app with metadata for OpenAPI docs
app = FastAPI(
    title="DocuForge",
    description="Intelligent Document Processing Pipeline — PDF/A + OCR + Tagging",
    version="1.0.1",
    lifespan=lifespan,
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
    return templates.TemplateResponse(request=request, name="index.html")


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
    files: list[UploadFile] = File(...),
    skip_rename: str = Form("false"),
    skip_tags: str = Form("false"),
):
    """
    Accept one or more files for processing.

    Validates file extension and size, saves to the upload folder,
    creates a job for each file, and enqueues it into the job queue.
    Jobs are processed one at a time by the queue worker.

    Optional form fields:
        skip_rename: "true" to skip LLM filename generation
        skip_tags: "true" to skip LLM tag generation
    """
    settings = get_settings()
    upload_dir = Path(settings.upload_folder)
    upload_dir.mkdir(parents=True, exist_ok=True)

    max_bytes = settings.max_file_size_mb * 1024 * 1024
    jobs_created = []
    do_skip_rename = skip_rename.lower() == "true"
    do_skip_tags = skip_tags.lower() == "true"

    for file in files:
        suffix = Path(file.filename).suffix.lower()
        if suffix not in settings.allowed_extensions:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported file type: '{suffix}'. "
                       f"Allowed: {', '.join(sorted(settings.allowed_extensions))}",
            )

        content = await file.read()
        if len(content) > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"File '{file.filename}' exceeds max size of {settings.max_file_size_mb} MB",
            )

        safe_name = _safe_filename(file.filename)
        file_path = upload_dir / safe_name
        file_path.write_bytes(content)
        logger.info(f"File saved: {file_path}")

        job = job_manager.create_job(file.filename)
        job_manager.update_job(
            job.id,
            file_path=str(file_path),
            skip_rename=do_skip_rename,
            skip_tags=do_skip_tags,
        )
        job_queue.enqueue(job.id, file_path)
        log_event("info", f"File queued: {file.filename}", job.id)

        jobs_created.append(job.to_dict())

    return JSONResponse({
        "jobs": jobs_created,
        "queue_size": job_queue.queue_size,
    })


@app.get("/api/jobs")
async def list_jobs(page: int = 1, per_page: int = 20):
    """
    Return paginated processing jobs, newest first.

    Query params:
        page: Page number (1-indexed, default 1).
        per_page: Jobs per page (default 20, max 100).
    """
    per_page = min(per_page, 100)
    return JSONResponse(job_manager.list_jobs(page=page, per_page=per_page))


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
# Sprint 2: Renaming & Tagging
# =============================================================================


class RenameRequest(BaseModel):
    """Request body for regenerating name/tags with a custom prompt."""
    prompt: str = Field(
        default="",
        description="Custom prompt template. Uses {ocr_text} placeholder. "
                    "Empty = use the default prompt from settings.",
    )


class MetadataRequest(BaseModel):
    """Request body for manually overriding filename and tags."""
    filename: str = Field(..., description="New filename (without extension)")
    tags: list[str] = Field(default_factory=list, description="Up to 5 tags")


@app.get("/api/jobs/{job_id}/preview")
async def preview_job(job_id: str):
    """
    Return OCR text + proposed name/tags for the approval modal.

    Returns 404 if the job has no OCR text available.
    """
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    if not job.ocr_full_text:
        raise HTTPException(
            status_code=404,
            detail="No OCR text available for this job",
        )

    return JSONResponse({
        "ocr_full_text": job.ocr_full_text,
        "proposed_name": job.proposed_name,
        "proposed_tags": job.proposed_tags,
    })


@app.post("/api/jobs/{job_id}/rename")
async def regenerate_name(job_id: str, body: RenameRequest):
    """
    Regenerate the filename and tags with a new LLM prompt.

    The new suggestions are stored on the job but NOT applied to the
    file until the user approves via PUT /api/jobs/{id}/metadata.

    Body:
        prompt: Custom prompt template (optional — uses default if empty).
    """
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    if not job.ocr_full_text:
        raise HTTPException(
            status_code=400,
            detail="No OCR text available for renaming",
        )

    settings = get_settings()

    try:
        new_name = await generate_filename(job.ocr_full_text, settings)
        new_tags = await generate_tags(job.ocr_full_text, settings)
    except TaggingError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Tagging LLM call failed: {exc}",
        )

    # Update job with new proposed values (don't apply to file yet)
    job_manager.update_job(
        job_id,
        proposed_name=new_name,
        proposed_tags=new_tags,
    )

    return JSONResponse({
        "proposed_name": new_name,
        "proposed_tags": new_tags,
    })


@app.put("/api/jobs/{job_id}/metadata")
async def update_metadata(job_id: str, body: MetadataRequest):
    """
    Finalize a job by applying the approved filename and tags.

    This renames the output file on disk, embeds tags into the
    PDF/A XMP metadata, and marks the job as done.

    Body:
        filename: Final filename (will be sanitized).
        tags: Final list of tags.
    """
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    if not job.output_filename:
        raise HTTPException(
            status_code=400,
            detail="Job has no output file yet",
        )

    settings = get_settings()

    # Sanitize the user-provided filename
    final_name = sanitize_filename(body.filename)

    # Validate tags
    cleaned_tags = []
    for tag in body.tags:
        tag_str = str(tag).strip()
        if tag_str and len(tag_str) <= 50 and tag_str not in cleaned_tags:
            cleaned_tags.append(tag_str)
        if len(cleaned_tags) >= 5:
            break

    # Rename the file on disk
    old_path = Path(settings.output_folder) / job.output_filename
    new_path = Path(settings.output_folder) / final_name

    if not old_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Output file not found: {job.output_filename}",
        )

    # Avoid collision — if new_path already exists for a different job, add suffix
    if new_path.exists() and new_path != old_path:
        stem = new_path.stem
        suffix = new_path.suffix
        counter = 1
        while new_path.exists():
            new_path = Path(settings.output_folder) / f"{stem}_{counter}{suffix}"
            counter += 1
        final_name = new_path.name

    old_path.rename(new_path)

    # Embed tags and title into the PDF
    doc_title = body.filename.strip() if body.filename else job.original_name
    embed_tags_in_pdf(new_path, cleaned_tags, title=doc_title)

    # Mark job as done with the final values
    job_manager.update_job(
        job_id,
        status=JobStatus.DONE,
        finished_at=datetime.now(timezone.utc) if not job.finished_at else None,
        output_filename=final_name,
        tags=cleaned_tags,
        proposed_name=body.filename,
        proposed_tags=cleaned_tags,
    )
    log_event("info", f"Document approved: {final_name}", job_id)

    return JSONResponse(job_manager.get_job(job_id).to_dict())


@app.get("/api/tags")
async def list_tags():
    """
    Return all unique tags across all jobs with usage counts.

    Used to populate the tag filter dropdown/bar in the UI.
    """
    tag_counts: dict[str, int] = {}
    for job_dict in job_manager.list_jobs():
        for tag in job_dict.get("tags", []):
            if tag:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

    # Sort by count descending, then alphabetically
    sorted_tags = sorted(tag_counts.items(), key=lambda x: (-x[1], x[0]))
    return JSONResponse([
        {"tag": tag, "count": count} for tag, count in sorted_tags
    ])


# =============================================================================
# Sprint 5: Error Handling & Retry
# =============================================================================


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str):
    """
    Re-enqueue a failed job for processing.

    Locates the original uploaded file from the stored file_path,
    resets the job status to queued, and puts it back in the queue.
    Returns 400 if the job is not in failed status or the file is missing.
    """
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    if job.status != JobStatus.FAILED:
        raise HTTPException(
            status_code=400,
            detail=f"Job {job_id} is not in failed state (current: {job.status.value})",
        )

    if not job.file_path:
        raise HTTPException(
            status_code=400,
            detail="Job has no stored file path — cannot retry. Re-upload the file instead.",
        )

    file_path = Path(job.file_path)
    if not file_path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Original file not found at {job.file_path}. It may have been deleted. Re-upload instead.",
        )

    # Reset job state and re-enqueue
    job_manager.update_job(
        job_id,
        status=JobStatus.QUEUED,
        error=None,
        finished_at=None,
        pages=0,
        pages_done=0,
        page_errors=[],
        pdfa_saved=False,
        tier_summary={},
        ocr_full_text=None,
        tags=[],
        proposed_name=None,
        proposed_tags=[],
    )
    job_queue.enqueue(job_id, file_path)

    logger.info(f"Job {job_id} re-enqueued for retry")
    return JSONResponse(job_manager.get_job(job_id).to_dict())


# =============================================================================
# Sprint 3: Folder Watcher
# =============================================================================


@app.get("/api/watcher/status")
async def watcher_status():
    """
    Return whether the folder watcher is running and its current stats.
    """
    return JSONResponse(get_watcher_status())


@app.post("/api/watcher/start")
async def watcher_start():
    """
    Start monitoring the upload folder for new files.

    When a compatible file (PDF/image) appears in the upload folder,
    it is automatically enqueued for processing.
    """
    result = await start_watcher()
    if result.get("status") == "started":
        log_event("info", "Folder watcher started")
    return JSONResponse(result)


@app.post("/api/watcher/stop")
async def watcher_stop():
    """
    Stop the folder watcher.

    Files can still be uploaded via the web UI.
    """
    result = await stop_watcher()
    if result.get("status") == "stopped":
        log_event("info", "Folder watcher stopped")
    return JSONResponse(result)


# =============================================================================
# Sprint 4: Configuration Panel
# =============================================================================


@app.get("/api/config")
async def get_config():
    """
    Return all current application settings.

    The UI calls this to populate the Settings form fields.
    """
    return JSONResponse(get_settings_dict())


@app.post("/api/config")
async def update_config(body: dict):
    """
    Update application settings and persist them to disk.

    Accepts a partial dict — only the fields that changed need
    to be sent. Unknown or non-editable fields are silently ignored.
    Missing editable fields keep their current value.

    Body:
        Any subset of: ollama_host, ocr_model, tagging_model, dpi,
        pdfa_level, max_file_size_mb, watch_interval, auto_rename,
        rename_prompt
    """
    editable = get_editable_fields()
    settings = get_settings()

    # Start with current values
    merged = {}
    for field in editable:
        merged[field] = body.get(field, getattr(settings, field, None))

    # Validate by attempting to construct a Settings object
    try:
        validated = Settings(**merged)
    except Exception as exc:
        # Pydantic validation failed — return field-level errors
        errors = []
        if hasattr(exc, "errors"):
            for err in exc.errors():
                errors.append({
                    "field": ".".join(str(loc) for loc in err["loc"]),
                    "message": err["msg"],
                })
        raise HTTPException(
            status_code=422,
            detail={"error": "Validation failed", "validation_errors": errors},
        )

    # Persist and reload
    save_settings_to_json(merged)
    reload_settings()

    # Return the fresh config so the UI can confirm
    return JSONResponse(get_settings_dict())


@app.get("/api/config/test-ollama")
async def test_ollama_connection():
    """
    Test connectivity to the configured Ollama instance.

    Pings GET {ollama_host}/api/tags and returns reachable status
    plus a list of available models.
    """
    settings = get_settings()
    result = {
        "reachable": False,
        "host": settings.ollama_host,
        "models": [],
        "error": None,
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{settings.ollama_host}/api/tags")
            if response.status_code == 200:
                data = response.json()
                models = data.get("models", [])
                result["reachable"] = True
                result["models"] = [
                    m.get("name", "unknown") for m in models
                ]
            else:
                result["error"] = f"HTTP {response.status_code}: {response.text[:200]}"
    except httpx.ConnectError:
        result["error"] = f"Connection refused — is Ollama running at {settings.ollama_host}?"
    except httpx.TimeoutException:
        result["error"] = "Connection timed out after 10 seconds"
    except Exception as exc:
        result["error"] = str(exc)[:300]

    return JSONResponse(result)


# =============================================================================
# Sprint 7: Logs
# =============================================================================


@app.get("/api/logs")
async def get_logs(limit: int = 100):
    """
    Return recent application events, newest first.

    Query params:
        limit: Max number of events to return (default 100).
    """
    return JSONResponse(get_recent_events(limit=min(limit, 500)))

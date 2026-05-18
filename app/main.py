"""
DocuForge — FastAPI Application
===============================
Main entry point for the web application.
Defines routes, mounts static files, and serves the UI.
"""

import asyncio
import logging
import os
import tempfile
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
from app.pdf_utils import embed_tags_in_pdf, split_pdf

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

    # Warn if GLM-OCR is configured without GPU compose file
    if settings.ocr_engine in ("glm-ocr", "glm-ocr-vllm"):
        logger.warning(
            "GLM-OCR selected as OCR engine. Ensure you started with "
            "'docker compose -f docker-compose.yml -f docker-compose.gpu.yml up'. "
            "Without GPU passthrough, GLM-OCR will fall back to CPU and be extremely slow. "
            "Switch to ocr_engine=tesseract if no GPU is available."
        )
        log_event("warn", "GLM-OCR engine configured — verify GPU passthrough is active")

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

    Returns the app status, Ollama connectivity state, and GPU
    availability info for the Ollama runtime.
    """
    ollama_status = "disconnected"
    gpu_available = False
    gpu_info = None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{settings.ollama_host}/api/tags")
            if response.status_code == 200:
                ollama_status = "connected"

            # Check if Ollama has GPU-accelerated models loaded
            try:
                ps_resp = await client.get(f"{settings.ollama_host}/api/ps")
                if ps_resp.status_code == 200:
                    ps_data = ps_resp.json()
                    for model_info in ps_data.get("models", []):
                        details = model_info.get("details", {})
                        if "gpu" in str(details).lower() or details.get("gpu"):
                            gpu_available = True
                            gpu_info = str(details.get("gpu", ""))
                            break
            except Exception:
                pass  # /api/ps may not be available on older Ollama versions
    except Exception:
        ollama_status = "disconnected"

    return {
        "status": "ok",
        "service": "docuforge",
        "version": "1.0.0",
        "ollama": ollama_status,
        "gpu_available": gpu_available,
        "gpu_info": gpu_info,
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
        "doc_type": job.doc_type,
        "extracted_fields": job.extracted_fields,
    })


@app.get("/api/jobs/{job_id}/fields")
async def get_fields(job_id: str):
    """
    Return extracted structured fields for an invoice job.

    Returns 404 if the job doesn't exist.
    Returns empty fields dict if extraction wasn't run.
    """
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    return JSONResponse({
        "doc_type": job.doc_type,
        "fields": job.extracted_fields or {},
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
    plus a list of available models. Also checks GPU availability
    via /api/ps for loaded model hardware info.
    """
    settings = get_settings()
    result = {
        "reachable": False,
        "host": settings.ollama_host,
        "models": [],
        "gpu_available": False,
        "gpu_info": None,
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

                # Check GPU availability via running model info
                try:
                    ps_resp = await client.get(f"{settings.ollama_host}/api/ps")
                    if ps_resp.status_code == 200:
                        ps_data = ps_resp.json()
                        for model_info in ps_data.get("models", []):
                            details = model_info.get("details", {})
                            if details:
                                result["gpu_info"] = (
                                    f"{model_info.get('name', 'unknown')}: "
                                    f"{str(details)[:200]}"
                                )
                                if "gpu" in str(details).lower():
                                    result["gpu_available"] = True
                                break
                except Exception:
                    pass
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


# =============================================================================
# Sprint 7: Smart PDF Splitting
# =============================================================================


@app.post("/api/jobs/{job_id}/split-detect")
async def run_split_detection(job_id: str):
    """
    Manually trigger split boundary detection on a specific job.

    Re-renders pages at split_dpi and runs the detection engine.
    Returns the detected boundaries and confidences.
    """
    settings = get_settings()
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    if not job.file_path:
        raise HTTPException(status_code=400, detail="Job has no file path — cannot re-detect")

    from app.splitter import SplitDetector
    from app.processor import pdf_to_images

    file_path = Path(job.file_path)
    if not file_path.suffix.lower() == ".pdf":
        raise HTTPException(status_code=400, detail="Split detection only available for PDF files")

    # Render at split DPI
    job_manager.update_job(job_id, split_phase="rendering", split_progress_pct=0)
    split_images = pdf_to_images(file_path, settings.split_dpi)
    job_manager.update_job(job_id, split_phase="detecting", split_progress_pct=5)

    detector = SplitDetector(settings)

    def on_progress(phase, pct):
        job_manager.update_job(
            job_id,
            split_phase=phase,
            split_progress_pct=min(pct, 99),
        )

    split_result = await detector.detect(
        file_path, split_images, settings,
        progress_callback=on_progress,
    )
    job_manager.update_job(job_id, split_phase="done", split_progress_pct=100)

    # Save thumbnails for preview
    from app.processor import _save_split_thumbnails
    job_tmp = Path(tempfile.mkdtemp(prefix=f"docuforge_{job_id}_"))
    if split_result.get("boundaries"):
        _save_split_thumbnails(split_images, split_result["boundaries"], job_tmp, job_id)

    job_manager.update_job(job_id,
        split_boundaries=split_result.get("boundaries", []),
        split_confidences=split_result.get("confidences", []),
        blank_pages_removed=split_result.get("blank_pages", []),
    )

    return JSONResponse(split_result)


@app.get("/api/jobs/{job_id}/split-preview")
async def get_split_preview(job_id: str):
    """
    Return split preview data: boundaries, confidences, and first-page
    thumbnail info for each detected child document.
    """
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    if not job.split_boundaries:
        return JSONResponse({"boundaries": [], "confidences": [], "children": []})

    # Build child document ranges
    boundaries = job.split_boundaries
    confidences = job.split_confidences
    children = []
    start = 0
    for i, b in enumerate(boundaries):
        if b > start:
            confidence = confidences[i] if i < len(confidences) else 0.5
            children.append({
                "index": len(children) + 1,
                "start_page": start + 1,  # 1-indexed for display
                "end_page": b,
                "page_count": b - start,
                "confidence": confidence,
                "auto_approved": confidence >= get_settings().split_confidence,
            })
        start = b
    if start < (job.pages or 0):
        children.append({
            "index": len(children) + 1,
            "start_page": start + 1,
            "end_page": job.pages,
            "page_count": (job.pages or 0) - start,
            "confidence": confidences[-1] if confidences else 0.5,
            "auto_approved": False,
        })

    return JSONResponse({
        "boundaries": boundaries,
        "confidences": confidences,
        "children": children,
        "total_pages": job.pages,
    })


@app.put("/api/jobs/{job_id}/split-points")
async def adjust_split_points(job_id: str, body: dict):
    """
    Adjust the detected split boundaries manually.

    Body: {"boundaries": [3, 7, 12]} — new 0-based page indices.
    """
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    new_boundaries = body.get("boundaries", [])
    if not isinstance(new_boundaries, list) or not all(isinstance(b, int) for b in new_boundaries):
        raise HTTPException(status_code=400, detail="boundaries must be a list of integers")

    new_boundaries = sorted(set(b for b in new_boundaries if 0 < b < (job.pages or float("inf"))))
    job_manager.update_job(job_id, split_boundaries=new_boundaries, split_confidences=[])

    return JSONResponse({"boundaries": new_boundaries, "adjusted": True})


@app.post("/api/jobs/{job_id}/split-confirm")
async def confirm_splits(job_id: str):
    """
    Confirm split boundaries → physically split PDF and create child jobs.
    Each child enters the standard processing pipeline.
    """
    settings = get_settings()
    job = job_manager.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    if not job.file_path:
        raise HTTPException(status_code=400, detail="Job has no file path")
    if not job.split_boundaries:
        raise HTTPException(status_code=400, detail="No split boundaries defined")

    file_path = Path(job.file_path)
    child_paths = split_pdf(
        file_path,
        job.split_boundaries,
        Path(settings.output_folder),
        file_path.stem,
        job.blank_pages_removed,
    )

    # Create child jobs and enqueue
    child_ids = []
    for i, child_path in enumerate(child_paths):
        child_job = job_manager.create_job(child_path.name)
        child_job.job_type = "split_child"
        child_job.parent_job_id = job_id
        child_ids.append(child_job.id)
        job_queue.enqueue(child_job.id, child_path.resolve())

    job_manager.update_job(job_id,
        status=JobStatus.DONE,
        child_job_ids=child_ids,
    )
    log_event("info", f"Split confirmed: {len(child_paths)} child documents created", job_id)

    return JSONResponse({
        "child_job_ids": child_ids,
        "child_count": len(child_paths),
    })


@app.post("/api/jobs/batch-approve")
async def batch_approve(body: dict):
    """
    Finalize metadata for multiple awaiting jobs in one action.

    Body: {"approvals": [{"id": "...", "filename": "...", "tags": [...]}, ...]}
    """
    job_ids = body.get("job_ids", [])
    if not isinstance(job_ids, list) or not job_ids:
        raise HTTPException(status_code=400, detail="job_ids must be a non-empty list")

    approved_count = 0
    errors = []
    for jid in job_ids:
        job = job_manager.get_job(jid)
        if not job:
            errors.append(f"{jid}: not found")
            continue
        if job.status not in (JobStatus.AWAITING_APPROVAL, JobStatus.AWAITING_SPLIT_APPROVAL):
            errors.append(f"{jid}: not in awaiting_approval state")
            continue

        # Finalize the job
        output_dir = Path(get_settings().output_folder)
        proposed = job.proposed_name or Path(job.original_name).stem
        final_name = sanitize_filename(proposed)
        final_path = output_dir / final_name

        # Rename output file if needed
        if job.output_filename:
            current_path = output_dir / job.output_filename
            if current_path.exists() and current_path != final_path:
                current_path.rename(final_path)
                embed_tags_in_pdf(final_path, job.proposed_tags or job.tags, title=proposed)
                job_manager.update_job(jid,
                    output_filename=final_name,
                    status=JobStatus.DONE,
                    tags=job.proposed_tags or job.tags,
                )
            else:
                embed_tags_in_pdf(current_path, job.proposed_tags or job.tags, title=proposed)
                job_manager.update_job(jid,
                    status=JobStatus.DONE,
                    tags=job.proposed_tags or job.tags,
                )
        else:
            job_manager.update_job(jid, status=JobStatus.DONE)

        approved_count += 1
        log_event("info", f"Batch approved: {job.original_name}", jid)

    return JSONResponse({
        "approved": approved_count,
        "errors": errors,
    })


# =============================================================================
# Standalone PDF Split (no detection — direct boundary split)
# =============================================================================


class SplitRequest(BaseModel):
    """Request body for direct PDF splitting at specified boundaries."""
    job_id: str = Field(..., description="ID of the uploaded job to split")
    boundaries: list[int] = Field(..., description="0-based page indices to split at")
    strip_blanks: bool = Field(default=True, description="Auto-detect and remove blank pages from children")
    base_name: str = Field(default="", description="Override output filename prefix (uses file stem if empty)")


@app.post("/api/pdf/split")
async def split_pdf_direct(body: SplitRequest):
    """
    Split a PDF at exact page boundaries without running split detection.

    Useful for manual splitting when you already know where documents
    begin and end. The original file is preserved; each child is saved
    as a separate PDF.

    Body:
        job_id: Reference to an uploaded PDF job.
        boundaries: List of 0-based page indices to split at.
            Example: [3, 7] splits into pages [0-2], [3-6], [7-end].
        strip_blanks: If true, auto-detect and remove blank pages
            from each child document.
        base_name: Optional filename prefix for child PDFs.
    """
    settings = get_settings()
    job = job_manager.get_job(body.job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job not found: {body.job_id}")
    if not job.file_path:
        raise HTTPException(status_code=400, detail="Job has no file path")

    file_path = Path(job.file_path)
    if file_path.suffix.lower() != ".pdf":
        raise HTTPException(
            status_code=400,
            detail=f"PDF splitting only supports .pdf files, got {file_path.suffix}",
        )

    if not body.boundaries:
        raise HTTPException(status_code=400, detail="boundaries must be a non-empty list")

    # Get actual page count (job.pages may be 0 on a fresh upload that
    # hasn't been through the pipeline yet)
    total_pages = job.pages
    if not total_pages or total_pages == 0:
        import pikepdf
        with pikepdf.open(file_path) as pdf:
            total_pages = len(pdf.pages)
        job_manager.update_job(body.job_id, pages=total_pages)

    validated = sorted(set(b for b in body.boundaries if 0 < b < total_pages))
    if not validated:
        raise HTTPException(status_code=400, detail=f"No valid boundaries in range 1–{total_pages - 1}")

    base = body.base_name.strip() if body.base_name.strip() else file_path.stem

    # Detect blank pages if requested
    blank_list = None
    if body.strip_blanks:
        from app.processor import pdf_to_images
        from app.splitter import SplitDetector
        split_images = pdf_to_images(file_path, settings.split_dpi)
        detector = SplitDetector(settings)
        blank_list = detector._detect_blank_pages(split_images)
        logger.info(f"Auto-detected {len(blank_list)} blank pages for stripping")

    output_dir = Path(settings.output_folder)
    child_paths = split_pdf(file_path, validated, output_dir, base, blank_list)

    return JSONResponse({
        "child_paths": [str(p) for p in child_paths],
        "count": len(child_paths),
        "boundaries": validated,
        "blank_pages_removed": blank_list or [],
    })

"""
DocuForge — Processing Pipeline + Job Manager
==============================================
Orchestrates the full document processing pipeline:
  Upload → (Image→PDF) → Render Pages → OCR → PDF/A → Text Layer → Output

JobManager tracks all jobs in memory with thread-safe access.
process_file() is called as a background task from the API route.
"""

import asyncio
import json
import logging
import pikepdf
import shutil
import tempfile
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import uuid6
from pdf2image import convert_from_path

from app.config import Settings, get_settings
from app.ocr import ocr_page, OCRError
from app.pdf_utils import image_to_pdf, convert_to_pdfa, layer_text_on_pdf, embed_tags_in_pdf, split_pdf
from app.tagger import generate_filename, generate_tags, sanitize_filename, classify_document, extract_invoice_fields, TaggingError
from app.verifier import validate_text_layer
from app.splitter import SplitDetector

logger = logging.getLogger(__name__)

# ---- Job Status Enum ----
class JobStatus(str, Enum):
    """
    Lifecycle of a processing job.
    queued             — File uploaded, waiting to be processed
    processing         — Pipeline is actively running
    awaiting_approval  — OCR+PDF/A done, waiting for user to approve name/tags
    done               — Processing completed successfully
    failed             — Processing encountered an unrecoverable error
    """

    QUEUED = "queued"
    PROCESSING = "processing"
    AWAITING_APPROVAL = "awaiting_approval"
    AWAITING_SPLIT_APPROVAL = "awaiting_split_approval"
    DONE = "done"
    FAILED = "failed"


# ---- Job Data ----
class JobData:
    """
    Holds all state for a single processing job.

    Attributes:
        id: Unique job identifier (UUID v6).
        original_name: The uploaded file's original name.
        status: Current lifecycle status.
        created_at: UTC timestamp when the job was created.
        finished_at: UTC timestamp when processing ended (or None).
        pages: Total number of pages in the document.
        pages_done: Number of pages that finished OCR successfully.
        output_filename: Name of the output file (relative to output dir).
        error: Error message if status is failed.
        tags: List of tags (populated in Sprint 2).
        ocr_full_text: Concatenated OCR text from all pages (for Sprint 2 preview).
        tier_summary: Dict summarizing which OCR tiers were used (word/line/full_page/skip).
    """

    def __init__(self, job_id: str, original_name: str):
        self.id = job_id
        self.original_name = original_name
        self.status = JobStatus.QUEUED
        self.created_at = datetime.now(timezone.utc)
        self.finished_at: Optional[datetime] = None
        self.pages = 0
        self.pages_done = 0
        self.output_filename: Optional[str] = None
        self.error: Optional[str] = None
        self.tags: list[str] = []
        self.ocr_full_text: Optional[str] = None
        self.tier_summary: dict[str, int] = {}
        # Sprint 2 — Renaming & Tagging
        self.proposed_name: Optional[str] = None
        self.proposed_tags: list[str] = []
        # Sprint 5 — Error tracking & retry
        self.file_path: Optional[str] = None       # Saved upload path for retry
        self.page_errors: list[dict] = []          # Per-page OCR failures
        self.pdfa_saved: bool = False              # True if PDF/A was generated (partial output)
        # Sprint 7 — Skip tagging/renaming
        self.skip_rename: bool = False
        self.skip_tags: bool = False
        # Sprint 8 — Document classification
        self.doc_type: Optional[str] = None
        # Sprint 8 — Text layer verification
        self.text_layer_score: Optional[int] = None
        self.text_layer_warnings: list[str] = []
        # Sprint 8 — Structured field extraction
        self.extracted_fields: Optional[dict] = None
        # Sprint 7 — Smart PDF splitting
        self.job_type: str = "standard"  # "standard" or "split_child"
        self.parent_job_id: Optional[str] = None
        self.child_job_ids: list[str] = []
        self.split_boundaries: list[int] = []
        self.split_confidences: list[float] = []
        self.blank_pages_removed: list[int] = []

    def to_dict(self) -> dict:
        """
        Serialize job data to a JSON-safe dict for API responses.
        """
        return {
            "id": self.id,
            "original_name": self.original_name,
            "status": self.status.value,
            "created_at": self.created_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "pages": self.pages,
            "pages_done": self.pages_done,
            "output_filename": self.output_filename,
            "error": self.error,
            "tags": self.tags,
            "tier_summary": self.tier_summary,
            "proposed_name": self.proposed_name,
            "proposed_tags": self.proposed_tags,
            "ocr_full_text": self.ocr_full_text,
            "page_errors": self.page_errors,
            "pdfa_saved": self.pdfa_saved,
            "doc_type": self.doc_type,
            "text_layer_score": self.text_layer_score,
            "text_layer_warnings": self.text_layer_warnings,
            "extracted_fields": self.extracted_fields,
            "job_type": self.job_type,
            "parent_job_id": self.parent_job_id,
            "child_job_ids": self.child_job_ids,
            "split_boundaries": self.split_boundaries,
            "split_confidences": self.split_confidences,
            "blank_pages_removed": self.blank_pages_removed,
        }


# ---- Job Manager ----
class JobManager:
    """
    Thread-safe in-memory job store.

    Jobs are stored in a dict keyed by job ID.
    A reentrant lock protects concurrent access from the
    background processing thread and the API request handlers.
    """

    def __init__(self):
        self._jobs: dict[str, JobData] = {}
        self._lock = threading.RLock()

    def create_job(self, original_name: str) -> JobData:
        """
        Create a new job with QUEUED status and return it.

        The caller must schedule process_file() separately.
        """
        job_id = str(uuid6.uuid7())
        job = JobData(job_id, original_name)
        with self._lock:
            self._jobs[job_id] = job
        logger.info(f"Job created: {job_id} — {original_name}")
        return job

    def get_job(self, job_id: str) -> Optional[JobData]:
        """Return a job by ID, or None if not found."""
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self, page: int = 1, per_page: int = 20) -> dict:
        """
        Return paginated jobs as dicts, newest first.

        Args:
            page: Page number (1-indexed).
            per_page: Number of jobs per page.

        Returns:
            dict with keys: jobs (list of job dicts), total (int),
            page (int), per_page (int), pages (int).
        """
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        total = len(jobs)
        pages = max(1, (total + per_page - 1) // per_page)
        start = (page - 1) * per_page
        end = start + per_page
        page_jobs = jobs[start:end]
        return {
            "jobs": [j.to_dict() for j in page_jobs],
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": pages,
        }

    def update_job(self, job_id: str, **kwargs):
        """
        Update fields on a job. Accepts any keyword matching JobData attributes.
        Thread-safe.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job:
                for key, value in kwargs.items():
                    if hasattr(job, key):
                        setattr(job, key, value)


# ---- Singleton ----
# One JobManager instance shared across the application
job_manager = JobManager()

# ---- Image suffixes for detection ----
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp"}

# ---- Temp directory for page renders ----
# We use a temp directory within the output folder so it is cleaned up
# when the container is restarted, but persists during a container's life.
TEMP_RENDER_DIR = Path("/tmp/docuforge/renders")


# ---- PDF Page Rendering ----
def pdf_to_images(pdf_path: Path, dpi: int) -> list[Path]:
    """
    Render each page of a PDF to a high-resolution PNG image.

    Uses poppler via pdf2image for rendering. Each page is saved
    as a temporary PNG file in the render directory.

    Args:
        pdf_path: Path to the source PDF.
        dpi: Rendering resolution (dots per inch).

    Returns:
        List of paths to the rendered page PNGs, in page order.
    """
    TEMP_RENDER_DIR.mkdir(parents=True, exist_ok=True)
    page_stem = pdf_path.stem

    logger.info(f"Rendering PDF to images @ {dpi} DPI: {pdf_path.name}")

    # pdf2image uses poppler's pdftoppm under the hood
    images = convert_from_path(
        pdf_path,
        dpi=dpi,
        fmt="png",
        thread_count=2,  # Conservative thread count for container environments
    )

    # Save each rendered page to a temp PNG file
    image_paths = []
    for i, img in enumerate(images):
        img_path = TEMP_RENDER_DIR / f"{page_stem}_page_{i + 1:03d}.png"
        img.save(img_path, "PNG")
        image_paths.append(img_path)

    logger.info(f"Rendered {len(image_paths)} page(s) from {pdf_path.name}")
    return image_paths


def _get_page_dims(image_paths: list[Path]) -> list[tuple[int, int]]:
    """
    Get pixel dimensions from rendered page images.

    Args:
        image_paths: List of paths to rendered page PNGs.

    Returns:
        List of (width_px, height_px) tuples, one per page.
    """
    from PIL import Image

    dims = []
    for path in image_paths:
        try:
            with Image.open(path) as img:
                dims.append(img.size)  # (width, height)
        except Exception:
            dims.append((0, 0))
    return dims


def _save_split_thumbnails(
    page_images: list[Path],
    boundaries: list[int],
    output_dir: Path,
    job_id: str,
):
    """
    Save first-page thumbnails of each detected split child for the UI preview.

    Creates small PNG thumbnails (200px wide) of the first page of each
    detected sub-document. Saved to the job temp directory.

    Args:
        page_images: Rendered page images at split_dpi.
        boundaries: List of 0-based page indices where splits occur.
        output_dir: Directory to save thumbnail PNGs into.
        job_id: Job ID for naming.
    """
    from PIL import Image

    # Build child page ranges from boundaries
    ranges = []
    start = 0
    for b in boundaries:
        if b > start:
            ranges.append((start, b))
        start = b
    if start < len(page_images):
        ranges.append((start, len(page_images)))

    thumbnail_dir = output_dir / f"split_thumbnails_{job_id[:8]}"
    thumbnail_dir.mkdir(parents=True, exist_ok=True)

    for i, (pg_start, pg_end) in enumerate(ranges):
        first_page = page_images[pg_start]
        try:
            with Image.open(first_page) as img:
                img.thumbnail((200, 200), Image.LANCZOS)
                thumb_path = thumbnail_dir / f"child_{i + 1}.png"
                img.save(thumb_path, "PNG")
                logger.debug(f"Saved split thumbnail: {thumb_path.name}")
        except Exception as exc:
            logger.warning(f"Failed to create thumbnail for child {i + 1}: {exc}")


# ---- Full Processing Pipeline ----
async def process_file(job_id: str, file_path: Path, settings: Settings | None = None):
    """
    Execute the full DocuForge pipeline on a single file.

    This function is designed to run as an asyncio background task.
    It updates the job status as the pipeline progresses.

    Pipeline steps:
      1. Detect file type — convert images to interim PDF
      2. Render all pages to PNGs at the configured DPI
      3. OCR each page via GLM-OCR on Ollama
      4. Convert the original/interim PDF to PDF/A-2b
      5. Layer OCR text invisibly onto the PDF/A
      6. Save the final file and update job

    On any unrecoverable error, the job is marked as failed.
    Individual page OCR failures do not stop the pipeline;
    those pages simply skip the text layer.

    Args:
        job_id: The job to process.
        file_path: Path to the uploaded file.
        settings: Application settings. Uses singleton if not provided.
    """
    if settings is None:
        settings = get_settings()

    job = job_manager.get_job(job_id)
    if not job:
        logger.error(f"Job {job_id} not found — cannot process")
        return

    # Mark job as processing
    job_manager.update_job(job_id, status=JobStatus.PROCESSING)
    log_event("info", f"Processing started: {file_path.name}", job_id)

    logger.info(f"Starting pipeline for job {job_id}: {file_path.name}")

    # Create a temp directory for this job's intermediate files
    job_tmp = Path(tempfile.mkdtemp(prefix=f"docuforge_{job_id}_"))

    try:
        # Track per-page errors for the job detail UI
        page_errors: list[dict] = []
        # Track whether PDF/A was generated (for partial output on failure)
        pdfa_saved = False

        # ---- Step 1: Normalize to PDF ----
        suffix = file_path.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            # Convert image to an interim PDF
            pdf_path = image_to_pdf(file_path, job_tmp)
        elif suffix == ".pdf":
            pdf_path = file_path
        else:
            raise ValueError(f"Unsupported file type: {suffix}")

        # ---- Step 1.5: Split detection (PDFs only) ----
        split_result = None
        if suffix == ".pdf" and settings.split_engine != "off":
            try:
                # Get page count without rendering (fast)
                with pikepdf.open(pdf_path) as tmp_pdf:
                    pdf_page_count = len(tmp_pdf.pages)

                if pdf_page_count >= settings.split_min_pages:
                    log_event("info", f"Running split detection ({settings.split_engine}): {pdf_path.name}", job_id)
                    # Render pages at split DPI (lower than processing DPI for speed)
                    split_images = pdf_to_images(pdf_path, settings.split_dpi)
                    detector = SplitDetector(settings)
                    split_result = await detector.detect(pdf_path, split_images, settings)

                    if split_result and split_result.get("boundaries"):
                        boundaries = split_result["boundaries"]
                        confidences = split_result["confidences"]
                        blank_pages = split_result.get("blank_pages", [])
                        # Check auto-approval vs manual review
                        high_conf = [c for c in confidences if c >= settings.split_confidence]
                        if len(high_conf) == len(confidences) and settings.auto_rename:
                            # All high confidence + auto_rename → auto-split
                            log_event("info", f"Auto-splitting: {len(boundaries)} boundaries (all high confidence)", job_id)
                            child_paths = split_pdf(pdf_path, boundaries, Path(settings.output_folder),
                                                    file_path.stem, blank_pages)
                            job_manager.update_job(job_id,
                                job_type="split_parent",
                                split_boundaries=boundaries,
                                split_confidences=confidences,
                                blank_pages_removed=blank_pages,
                            )
                            # Create child jobs
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
                                pages=pdf_page_count,
                            )
                            return  # Parent done, children process separately
                        else:
                            # Manual approval needed
                            log_event("info", f"Split detection found {len(boundaries)} boundaries — awaiting approval", job_id)
                            job_manager.update_job(job_id,
                                pages=pdf_page_count,
                                job_type="split_parent",
                                split_boundaries=boundaries,
                                split_confidences=confidences,
                                blank_pages_removed=blank_pages,
                            )
                            # Save split preview images for UI (first page thumbnails)
                            _save_split_thumbnails(split_images, boundaries, job_tmp, job_id)
                            job_manager.update_job(job_id, status=JobStatus.AWAITING_SPLIT_APPROVAL)
                            log_event("info", f"Awaiting split approval: {file_path.name} ({len(boundaries)} documents)", job_id)
                            return
            except Exception as exc:
                logger.warning(f"Split detection failed (non-blocking): {exc}")
                log_event("warn", f"Split detection failed, proceeding as single doc: {exc}", job_id)

        # ---- Step 2: Render pages to images ----
        page_images = pdf_to_images(pdf_path, settings.dpi)
        total_pages = len(page_images)
        job_manager.update_job(job_id, pages=total_pages, pages_done=0)

        if total_pages == 0:
            raise ValueError("PDF contains zero renderable pages")

        # Collect rendered page pixel dimensions for text layer verification
        page_pixel_dims = _get_page_dims(page_images)

        # ---- Step 3: OCR each page ----
        ocr_results = []
        tier_summary = {"word": 0, "line": 0, "full_page": 0, "skip": 0}
        all_text_parts = []

        for i, img_path in enumerate(page_images):
            try:
                result = await ocr_page(img_path, settings)
                ocr_results.append(result)
                tier_summary[result["tier"]] = tier_summary.get(result["tier"], 0) + 1

                if result.get("full_text"):
                    all_text_parts.append(result["full_text"])

                # Update progress
                job_manager.update_job(job_id, pages_done=i + 1)
                logger.info(
                    f"Job {job_id}: OCR page {i + 1}/{total_pages} — "
                    f"tier={result['tier']}, words={len(result.get('words', []))}"
                )
            except OCRError as exc:
                # OCR failed for this page — log, track error, and continue
                error_detail = f"{type(exc).__name__}: {exc}"
                logger.warning(
                    f"Job {job_id}: OCR failed for page {i + 1}/{total_pages}: {error_detail}"
                )
                page_errors.append({"page": i + 1, "error": error_detail})
                ocr_results.append({
                    "full_text": "",
                    "words": [],
                    "tier": "skip",
                    "ocr_success": False,
                })
                tier_summary["skip"] = tier_summary.get("skip", 0) + 1
                job_manager.update_job(job_id, pages_done=i + 1, page_errors=page_errors)

        # Store OCR full text for Sprint 2 (renaming & tagging)
        ocr_full_text = "\n\n".join(all_text_parts)

        # ---- Step 3.5: Classify document type ----
        doc_type = "other"
        if ocr_full_text and settings.tagging_model:
            try:
                doc_type = await classify_document(ocr_full_text, settings)
                log_event("info", f"Document classified: {doc_type}", job_id)
            except Exception as exc:
                logger.warning(f"Classification failed (non-blocking): {exc}")
                doc_type = "other"
        job_manager.update_job(job_id, doc_type=doc_type)

        # ---- Step 3.6: Verify text layer quality ----
        verify_result = {"valid": True, "score": 100, "warnings": [], "page_scores": []}
        if settings.verify_text_layer:
            try:
                verify_result = validate_text_layer(ocr_results, settings.dpi, page_pixel_dims)
                job_manager.update_job(
                    job_id,
                    text_layer_score=verify_result["score"],
                    text_layer_warnings=verify_result["warnings"],
                )
                if verify_result["score"] < settings.verify_min_score:
                    log_event("warn", f"Text layer skipped: score {verify_result['score']} < {settings.verify_min_score}", job_id)
                elif verify_result["warnings"]:
                    log_event("warn", f"Text layer applied with warnings: score {verify_result['score']}", job_id)
                else:
                    log_event("info", f"Text layer verified: score {verify_result['score']}", job_id)
            except Exception as exc:
                logger.warning(f"Verification check failed (non-blocking): {exc}")

        # ---- Step 4: Convert to PDF/A ----
        output_filename = f"{file_path.stem}_ocr.pdf"
        pdfa_tmp = job_tmp / f"{file_path.stem}_pdfa.pdf"
        convert_to_pdfa(pdf_path, pdfa_tmp, settings.pdfa_level)
        pdfa_saved = True  # From here on, we have a valid PDF/A for partial output

        # ---- Step 5: Layer OCR text onto PDF/A (gated by verification) ----
        output_path = Path(settings.output_folder) / output_filename
        if verify_result["score"] >= settings.verify_min_score:
            layer_text_on_pdf(pdfa_tmp, ocr_results, output_path, settings.dpi)
        else:
            logger.warning(
                f"Job {job_id}: Skipping text layer — "
                f"verification score {verify_result['score']} below minimum {settings.verify_min_score}"
            )
            pdfa_tmp.rename(output_path)

        # ---- Step 5.5: Extract structured fields (invoices only) ----
        extracted_fields = {}
        if (
            settings.extract_fields
            and doc_type == "invoice"
            and ocr_full_text
            and settings.tagging_model
        ):
            try:
                extracted_fields = await extract_invoice_fields(ocr_full_text, settings)
                log_event("info", f"Fields extracted: invoice_date={extracted_fields.get('invoice_date', '')}", job_id)
            except Exception as exc:
                logger.warning(f"Field extraction failed (non-blocking): {exc}")
                extracted_fields = {}

        # Write extracted fields as .json alongside output PDF
        if extracted_fields:
            fields_path = output_path.with_suffix(".json")
            fields_path.write_text(json.dumps(extracted_fields, indent=2, ensure_ascii=False), encoding="utf-8")

        job_manager.update_job(job_id, extracted_fields=extracted_fields)

        # ---- Step 6: Generate name & tags via LLM (independently optional) ----
        proposed_name = file_path.stem
        proposed_tags = []

        # Rename: only if enabled and OCR text exists
        if not job.skip_rename and ocr_full_text and settings.tagging_model:
            try:
                proposed_name = await generate_filename(ocr_full_text, settings)
            except TaggingError as exc:
                logger.warning(f"Job {job_id}: Rename failed: {exc}")
                proposed_name = file_path.stem

        # Tags: only if enabled and OCR text exists
        if not job.skip_tags and ocr_full_text and settings.tagging_model:
            try:
                proposed_tags = await generate_tags(ocr_full_text, settings)
            except TaggingError as exc:
                logger.warning(f"Job {job_id}: Tagging failed: {exc}")
                proposed_tags = []

        # ---- Step 7: Finalize based on AUTO_RENAME setting ----
        if settings.auto_rename:
            # Auto-rename: embed tags, rename file, mark done immediately
            final_name = sanitize_filename(proposed_name)
            final_path = Path(settings.output_folder) / final_name

            # Rename the output file from temp name to LLM-generated name
            if final_path != output_path:
                output_path.rename(final_path)
                output_path = final_path
                output_filename = final_name

            # Embed tags and title into the PDF/A XMP metadata
            embed_tags_in_pdf(output_path, proposed_tags, title=proposed_name)

            job_manager.update_job(
                job_id,
                status=JobStatus.DONE,
                finished_at=datetime.now(timezone.utc),
                output_filename=output_filename,
                ocr_full_text=ocr_full_text,
                tier_summary=tier_summary,
                tags=proposed_tags,
                proposed_name=proposed_name,
                proposed_tags=proposed_tags,
                page_errors=page_errors,
                pdfa_saved=True,
            )
            log_event("info", f"Completed: {output_filename}", job_id)
        else:
            # Manual approval: save with original name, pause for user approval
            job_manager.update_job(
                job_id,
                status=JobStatus.AWAITING_APPROVAL,
                output_filename=output_filename,
                ocr_full_text=ocr_full_text,
                tier_summary=tier_summary,
                proposed_name=proposed_name,
                proposed_tags=proposed_tags,
                tags=[],
                page_errors=page_errors,
                pdfa_saved=True,
            )
            log_event("info", f"Awaiting approval: {file_path.stem}", job_id)

    except Exception as exc:
        # Unrecoverable error — mark job as failed
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.error(f"Job {job_id} failed: {error_msg}", exc_info=True)
        log_event("error", f"Failed: {error_msg}", job_id)

        # Save partial output if PDF/A conversion succeeded
        partial_output = None
        if pdfa_saved and job_tmp.exists():
            try:
                partial_name = f"{file_path.stem}_partial.pdf"
                partial_path = Path(settings.output_folder) / partial_name
                # Find the PDF/A temp file (it might still exist if text layering failed)
                if pdfa_tmp.exists():
                    shutil.copy(pdfa_tmp, partial_path)
                    partial_output = partial_name
                    logger.info(f"Partial output saved: {partial_name}")
            except Exception as copy_exc:
                logger.warning(f"Failed to save partial output: {copy_exc}")

        job_manager.update_job(
            job_id,
            status=JobStatus.FAILED,
            finished_at=datetime.now(timezone.utc),
            error=error_msg,
            output_filename=partial_output,
            pdfa_saved=pdfa_saved,
            page_errors=page_errors,
        )

    finally:
        # Clean up temporary files for this job
        try:
            if job_tmp.exists():
                shutil.rmtree(job_tmp, ignore_errors=True)
        except Exception:
            logger.debug(f"Failed to clean up temp dir: {job_tmp}")


# =============================================================================
# Sprint 3: Job Queue + Folder Watcher
# =============================================================================


class JobQueue:
    """
    Async job queue that serializes document processing.

    Uses asyncio.Queue as a FIFO buffer and a semaphore to limit
    concurrent processing (default: 1). This prevents hammering
    Ollama with multiple simultaneous OCR calls.

    Replaces the ad-hoc BackgroundTasks.add_task approach from
    Sprint 1. All jobs (from uploads and folder watcher) flow
    through this queue.
    """

    def __init__(self, max_concurrency: int = 1):
        self._queue: asyncio.Queue[tuple[str, Path]] = asyncio.Queue()
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._worker_task: Optional[asyncio.Task] = None
        self._running = False

    def enqueue(self, job_id: str, file_path: Path):
        """
        Add a job to the processing queue.

        The queue worker will pick it up and call process_file()
        when a processing slot opens. Jobs wait in queued status
        until the worker begins them.
        """
        self._queue.put_nowait((job_id, file_path))
        logger.debug(f"Job {job_id} enqueued — queue size: {self._queue.qsize()}")

    @property
    def queue_size(self) -> int:
        """Return the number of jobs waiting in the queue."""
        return self._queue.qsize()

    async def worker(self):
        """
        Continuous loop that pops jobs from the queue and processes them.

        The semaphore limits concurrency. With max_concurrency=1,
        jobs are processed strictly one at a time. When no jobs are
        queued, the worker sleeps on the queue's async get().
        """
        self._running = True
        logger.info(f"Job queue worker started (max concurrency={self._semaphore._value})")

        while self._running:
            # Wait for the next job — this blocks until a job is enqueued
            job_id, file_path = await self._queue.get()

            # Check for shutdown sentinel
            if job_id == "__SHUTDOWN__":
                logger.info("Queue worker received shutdown sentinel")
                self._queue.task_done()
                break

            # Acquire a processing slot (respects max concurrency)
            async with self._semaphore:
                logger.info(f"Queue worker: starting job {job_id}")
                await process_file(job_id, file_path)
                logger.info(f"Queue worker: completed job {job_id}")

            self._queue.task_done()

        logger.info("Job queue worker stopped")

    def stop(self):
        """Signal the worker to stop after the current job finishes."""
        self._running = False
        # Put a sentinel to unblock the queue if it's empty
        try:
            self._queue.put_nowait(("__SHUTDOWN__", Path("/dev/null")))
        except Exception:
            pass


# ---- Singleton queue ----
job_queue = JobQueue(max_concurrency=1)

# ---- Folder Watcher ----
_watcher_task: Optional[asyncio.Task] = None
_watcher_running = False
_watcher_files_seen = 0  # Counter for UI display


async def watch_folder():
    """
    Monitor the upload folder for new files and auto-enqueue them.

    Uses watchfiles.awatch() which yields filesystem change events.
    When a new file is created in the upload folder, the watcher:
      1. Validates the file extension against allowed types
      2. Waits a short debounce period (to handle partial network writes)
      3. Creates a job via job_manager
      4. Enqueues the job into the job_queue

    The watcher runs as a long-lived asyncio task, toggled on/off
    via the /api/watcher endpoints.
    """
    global _watcher_running, _watcher_files_seen
    _watcher_running = True
    _watcher_files_seen = 0

    settings = get_settings()
    upload_dir = Path(settings.upload_folder)
    upload_dir.mkdir(parents=True, exist_ok=True)

    logger.info(f"Folder watcher started — watching: {upload_dir}")

    try:
        from watchfiles import awatch
    except ImportError:
        logger.error("watchfiles not installed — folder watcher unavailable")
        _watcher_running = False
        return

    try:
        async for changes in awatch(str(upload_dir)):
            if not _watcher_running:
                break

            for change_type, changed_path in changes:
                # We only care about newly created or modified files
                if change_type not in (1, 2):  # 1=added, 2=modified
                    continue

                changed = Path(changed_path)
                suffix = changed.suffix.lower()

                # Skip non-file entries and unsupported types
                if not changed.is_file():
                    continue
                if suffix not in IMAGE_SUFFIXES and suffix != ".pdf":
                    continue
                # Skip temp/interim files from our own processing
                if "_interim" in changed.name or "_page_" in changed.name:
                    continue

                # Debounce: wait a moment for the file write to finish
                await asyncio.sleep(settings.watch_interval * 0.1)

                # Verify the file still exists and has size > 0
                if not changed.exists() or changed.stat().st_size == 0:
                    continue

                # Create and enqueue the job
                job = job_manager.create_job(changed.name)
                job_queue.enqueue(job.id, changed)
                _watcher_files_seen += 1

                logger.info(f"Watcher: auto-enqueued {changed.name} → job {job.id}")

    except asyncio.CancelledError:
        logger.info("Folder watcher task cancelled")
    except Exception:
        logger.exception("Folder watcher encountered an error")
    finally:
        _watcher_running = False
        logger.info(f"Folder watcher stopped. Total files seen: {_watcher_files_seen}")


def get_watcher_status() -> dict:
    """Return the current state of the folder watcher for the API."""
    return {
        "running": _watcher_running,
        "watching_path": str(Path(get_settings().upload_folder)),
        "files_seen": _watcher_files_seen,
    }


async def start_watcher():
    """Start the folder watcher as a background asyncio task."""
    global _watcher_task, _watcher_running
    if _watcher_running:
        logger.info("Folder watcher is already running")
        return {"status": "already_running"}

    _watcher_task = asyncio.create_task(watch_folder())
    # Give it a moment to start
    await asyncio.sleep(0.1)
    return {"status": "started"}


async def stop_watcher():
    """Stop the folder watcher background task."""
    global _watcher_task, _watcher_running
    if not _watcher_running:
        logger.info("Folder watcher is not running")
        return {"status": "not_running"}

    _watcher_running = False
    if _watcher_task:
        _watcher_task.cancel()
        try:
            await _watcher_task
        except asyncio.CancelledError:
            pass
        _watcher_task = None
    return {"status": "stopped"}


# =============================================================================
# Sprint 7: Event Logger
# =============================================================================

from collections import deque

_event_log: deque[dict] = deque(maxlen=500)  # Ring buffer, latest 500 events


def log_event(level: str, message: str, job_id: str | None = None):
    """
    Record a timestamped event in the in-memory ring buffer.

    Args:
        level: "info", "warn", or "error"
        message: Human-readable event description
        job_id: Optional associated job ID
    """
    _event_log.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": level,
        "message": message,
        "job_id": job_id,
    })
    # Also emit to the standard logger for container logs
    log_fn = getattr(logger, level, logger.info)
    log_fn(message)


def get_recent_events(limit: int = 100) -> list[dict]:
    """Return the most recent events, newest first."""
    events = list(_event_log)
    events.reverse()
    return events[:limit]

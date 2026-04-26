"""
DocuForge — Processing Pipeline + Job Manager
==============================================
Orchestrates the full document processing pipeline:
  Upload → (Image→PDF) → Render Pages → OCR → PDF/A → Text Layer → Output

JobManager tracks all jobs in memory with thread-safe access.
process_file() is called as a background task from the API route.
"""

import logging
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

import uuid6
from pdf2image import convert_from_path

from app.config import Settings, get_settings
from app.ocr import ocr_page, OCRError
from app.pdf_utils import image_to_pdf, convert_to_pdfa, layer_text_on_pdf, embed_tags_in_pdf
from app.tagger import generate_name_and_tags, sanitize_filename, TaggingError

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

    def list_jobs(self) -> list[dict]:
        """
        Return all jobs as dicts, newest first.
        """
        with self._lock:
            jobs = list(self._jobs.values())
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return [j.to_dict() for j in jobs]

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

    logger.info(f"Starting pipeline for job {job_id}: {file_path.name}")

    # Create a temp directory for this job's intermediate files
    job_tmp = Path(tempfile.mkdtemp(prefix=f"docuforge_{job_id}_"))

    try:
        # ---- Step 1: Normalize to PDF ----
        suffix = file_path.suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            # Convert image to an interim PDF
            pdf_path = image_to_pdf(file_path, job_tmp)
        elif suffix == ".pdf":
            pdf_path = file_path
        else:
            raise ValueError(f"Unsupported file type: {suffix}")

        # ---- Step 2: Render pages to images ----
        page_images = pdf_to_images(pdf_path, settings.dpi)
        total_pages = len(page_images)
        job_manager.update_job(job_id, pages=total_pages, pages_done=0)

        if total_pages == 0:
            raise ValueError("PDF contains zero renderable pages")

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
                # OCR failed for this page — log and continue with remaining pages
                logger.warning(
                    f"Job {job_id}: OCR failed for page {i + 1}/{total_pages}: {exc}"
                )
                ocr_results.append({
                    "full_text": "",
                    "words": [],
                    "tier": "skip",
                    "ocr_success": False,
                })
                tier_summary["skip"] = tier_summary.get("skip", 0) + 1
                job_manager.update_job(job_id, pages_done=i + 1)

        # Store OCR full text for Sprint 2 (renaming & tagging)
        ocr_full_text = "\n\n".join(all_text_parts)

        # ---- Step 4: Convert to PDF/A ----
        output_filename = f"{file_path.stem}_ocr.pdf"
        pdfa_tmp = job_tmp / f"{file_path.stem}_pdfa.pdf"
        convert_to_pdfa(pdf_path, pdfa_tmp, settings.pdfa_level)

        # ---- Step 5: Layer OCR text onto PDF/A ----
        output_path = Path(settings.output_folder) / output_filename
        layer_text_on_pdf(pdfa_tmp, ocr_results, output_path, settings.dpi)

        # ---- Step 6: Generate name & tags via LLM ----
        # Only attempt if we have OCR text and a tagging model configured
        if ocr_full_text and settings.tagging_model:
            try:
                tagging_result = await generate_name_and_tags(
                    ocr_full_text,
                    settings.rename_prompt,
                    settings,
                )
                proposed_name = tagging_result.get("filename", file_path.stem)
                proposed_tags = tagging_result.get("tags", [])
            except TaggingError as exc:
                # Tagging LLM call failed — fall back to original name, no tags
                logger.warning(f"Job {job_id}: Tagging failed: {exc}")
                proposed_name = file_path.stem
                proposed_tags = []
        else:
            proposed_name = file_path.stem
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

            # Embed tags into the PDF/A XMP metadata
            if proposed_tags:
                embed_tags_in_pdf(output_path, proposed_tags)

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
            )
            logger.info(
                f"Job {job_id} completed (auto-rename): {output_filename} "
                f"tags={proposed_tags}"
            )
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
            )
            logger.info(
                f"Job {job_id} awaiting approval — proposed: "
                f"'{proposed_name}', tags={proposed_tags}"
            )

    except Exception as exc:
        # Unrecoverable error — mark job as failed
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.error(f"Job {job_id} failed: {error_msg}", exc_info=True)
        job_manager.update_job(
            job_id,
            status=JobStatus.FAILED,
            finished_at=datetime.now(timezone.utc),
            error=error_msg,
        )

    finally:
        # Clean up temporary files for this job
        try:
            if job_tmp.exists():
                shutil.rmtree(job_tmp, ignore_errors=True)
        except Exception:
            logger.debug(f"Failed to clean up temp dir: {job_tmp}")

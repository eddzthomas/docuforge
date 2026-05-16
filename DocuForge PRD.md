# DocuForge — Product Requirements Document

## Project Name
**DocuForge** — Intelligent Document Processing Pipeline

**Tagline:** Upload. Convert. OCR. Archive.

---

## 1. Executive Summary

DocuForge is a self-hosted web application that ingests scanned PDF files and images from a watched folder, converts them to PDF/A (ISO-standard archival format), performs Optical Character Recognition (OCR) using a locally-running Tesseract engine or GLM-OCR vision model via Ollama, layers the recognized text invisibly on top of the PDF/A, and outputs the final searchable/selectable document to a destination folder. It can also detect and split multi-document PDFs into individual files using a hybrid AI pipeline.

The entire stack runs inside Docker containers for portability and isolation.

---

## 2. Problem Statement

Organizations and individuals dealing with scanned documents face several challenges:
- Scanned PDFs and image-based documents are **not searchable**
- Archival compliance often requires **PDF/A format**
- Cloud-based OCR services introduce **privacy risks** and **latency**
- Existing tools (OCRmyPDF) are command-line only and lack a modern UI
- No turnkey solution combines **local LLM-based OCR** with **PDF/A conversion** in a single web interface

DocuForge solves all of these with a simple, self-hosted Docker application.

---

## 3. Target Users

| Persona | Need |
|---------|------|
| Archivists / Librarians | Batch-process scanned collections into PDF/A |
| Legal / Compliance teams | Make scanned contracts searchable & archival-grade |
| Small businesses | Self-hosted OCR without cloud dependency |
| Developers / Hobbyists | Local document processing with a clean UI |

---

## 4. Functional Requirements

### FR-01: File Ingestion
- User can **upload** one or more files via the web UI
- Supported formats: `.pdf`, `.png`, `.jpg`, `.jpeg`, `.tiff`, `.tif`, `.bmp`
- Files are placed into a configurable **input folder** (`/data/uploads/`)
- The app can also **watch** the input folder for new files dropped via SMB/NFS/filesystem

### FR-02: PDF/A Conversion
- Non-PDF images are **converted to PDF** first
- All PDFs are converted to **PDF/A-2b** (or PDF/A-3b, configurable)
- Conversion ensures:
  - All fonts are embedded
  - Color profiles are included (sRGB)
  - No external dependencies or JavaScript
  - XMP metadata is present
  - Document catalog conforms to ISO 19005-2

### FR-03: OCR via Tesseract or GLM-OCR on Ollama
- Each page of the document is rendered to a high-resolution image (300 DPI default)
- The page image is sent to **GLM-OCR** running on a local Ollama instance
- GLM-OCR returns recognized text with per-word or per-line bounding boxes
- The app parses the OCR output and prepares it for text layering

### FR-04: Text Layering
- Recognized text is layered **invisibly** onto the PDF/A as a hidden text layer
- Text is positioned precisely using the bounding box coordinates from the OCR
- The rendered text layer is **selectable, searchable, and copyable**
- The visual appearance of the original scanned document is **preserved exactly**

### FR-05: Output Delivery
- Final files are written to a configurable **output folder** (`/data/output/`)
- Output filenames follow the pattern: `{original_name}_ocr.pdf`
- A **download link** is provided in the web UI
- Job history shows past processing runs with status

### FR-06: Web Interface
- Clean, responsive web UI
- Drag-and-drop file upload zone
- Real-time processing status (queued → converting → ocred → done)
- Job history table with download links
- Configuration panel for Ollama endpoint, DPI, PDF/A level

### FR-08: LLM-Powered Renaming & Tagging
- After OCR completes, the extracted plain text is sent to an **LLM** (via Ollama, e.g. `llama3.2`, `mistral`, `phi4`)
- A user-defined **prompt template** instructs the LLM to inspect the document content and propose:
  - A **suitable filename** (e.g., `2024-03-15_Acme_Contract_v2.pdf`)
  - **Up to 5 tags** (e.g., `contract`, `acme-corp`, `legal`, `2024`, `signed`)
- LLM returns structured JSON: `{"filename": "...", "tags": ["...", "..."]}`
- Output file is renamed to the LLM-suggested name (sanitized for filesystem safety)
- Tags are embedded into the PDF/A **XMP metadata** (`dc:subject` / custom namespace)
- Tags are displayed in job history UI and are searchable/filterable
- **Preview mode** lets user approve, edit, or regenerate name/tags before finalizing
- Prompt templates are saved per session for reuse

### FR-09: Docker Deployment
- Single `docker-compose up` to start the entire stack
- Two services: `docuforge-app` (web app) + `ollama` (serves both OCR and tagging models)
- Persistent volumes for uploads, output, and Ollama models
- Health checks on all services
- Auto-pull required models on first run

### FR-10: Smart PDF Splitting
- PDFs with multiple documents are auto-detected and split into individual files
- **Hybrid detection engine** — fast heuristics (blank pages, page number resets, header changes) combined with vision model verification for ambiguous boundaries
- Heuristic pass uses Tesseract OCR on footer/header regions at low DPI — zero LLM cost
- Vision model pass sends page contact sheets to Ollama vision model for document-type/layout boundary detection
- **Confidence-gated approval** — high-confidence boundaries auto-accept; low-confidence boundaries pause for user review
- **Blank page removal** — detected blank pages (separators and noise) are stripped from child documents
- Splitting uses `pikepdf` to extract page ranges into child PDFs
- Original multi-doc PDF preserved alongside split children for audit trail
- Configurable minimum page threshold — splitting only offered for PDFs with ≥N pages
- Split detection DPI is independent of processing DPI (lower for speed)

### FR-11: Batch Approval
- Split preview shows all detected documents in a table with checkboxes and a select-all toggle
- Each row displays: thumbnail, proposed name, page count, confidence level, editable tags
- User can batch-approve, merge adjacent documents, or adjust individual split points
- **Batch approve endpoint** (`POST /api/jobs/batch-approve`) finalizes multiple `awaiting_approval` jobs in one action — applies to both split children and individual rename/tag approvals
- Jobs approved via batch skip the individual preview modal flow

## 5. Non-Functional Requirements

| ID | Requirement | Detail |
|----|-------------|--------|
| NFR-01 | **Privacy** | All processing is local. No data leaves the Docker network. |
| NFR-02 | **Performance** | One A4 page at 300 DPI should OCR in <1s on consumer GPU (Tesseract ~2-5s on CPU). Split detection for 200 pages should complete heuristic pass in <60s. |
| NFR-03 | **Reliability** | Failed pages should not break the entire document; partial output saved. Split detection failures fall back to manual boundary input. |
| NFR-04 | **Scalability** | Queue-based processing; multiple files handled sequentially |
| NFR-05 | **Portability** | Runs on any host with Docker + GPU (or CPU fallback for Ollama) |
| NFR-06 | **Compliance** | PDF/A output validates against VeraPDF |

---

## 6. System Architecture

```
┌──────────────────────────────────────────────────┐
│                   Docker Host                      │
│                                                    │
│  ┌─────────────┐     ┌─────────────────────────┐  │
│  │   Browser    │────▶│   docuforge-app         │  │
│  │   (Web UI)   │     │   (FastAPI + Uvicorn)   │  │
│  └─────────────┘     │   Port: 8080             │  │
│                      └───────────┬──────────────┘  │
│                                  │                  │
│                      ┌───────────▼──────────┐       │
│                      │   ollama              │       │
│                      │   (GLM-OCR + Llama)   │       │
│                      │   Port: 11434         │       │
│                      └───────────────────────┘       │
│                                                    │
│  Volumes:                                          │
│    ./data/  →  /data/ (uploads, output, settings)  │
│    ollama-data → /root/.ollama (model cache)       │
└──────────────────────────────────────────────────┘
```

---

## 7. Technology Stack

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| Web framework | **FastAPI** (Python 3.11+) | Async, modern, file-upload friendly |
| PDF/A conversion | **pikepdf** | Lightweight, no Ghostscript dependency |
| Image to PDF | **img2pdf** | Lossless image-to-PDF embedding |
| PDF rendering | **pdf2image** + **Poppler** | Render PDF pages to PNG |
| OCR engine | **Tesseract** (CPU) or **GLM-OCR** via **Ollama** | Local, no cloud, dual-engine |
| Text layering | **pikepdf** | Invisible text overlay at bounding box positions |
| Renaming & tagging | **llama3.2 / mistral / phi4** via **Ollama** | LLM-powered document classification |
| Split detection | **Tesseract** (heuristics) + **Vision model** via **Ollama** | Hybrid boundary detection for multi-doc PDFs |
| PDF splitting | **pikepdf** | Page extraction and blank page removal |
| Frontend | **Vanilla HTML/CSS/JS** | Zero build step, single-page app |
| Container | **Docker** + **docker-compose** | One-command deployment |
| GPU support | **NVIDIA Container Toolkit** | GPU passthrough to Ollama |

---

## 8. Data Flow

### Standard Pipeline (single documents, images)

```
1. User uploads file (PDF or image)
       │
       ▼
2. File saved to /data/uploads/
       │
       ▼
3. If image → convert to PDF (img2pdf)
       │
       ▼
4. Render each page to PNG @ configured DPI (pdf2image)
       │
       ▼
5. OCR each page via configured engine (Tesseract or GLM-OCR via Ollama)
       │
       ▼
6. Receive JSON: {full_text, words: [{text, bbox}], tier}
       │
       ▼
7. Convert original to PDF/A (pikepdf)
       │
       ▼
8. Layer OCR text invisibly onto PDF/A using bounding boxes (pikepdf)
       │
       ▼
9. (Optional) Extract OCR'd plain text → send to LLM for renaming & tagging
       │
       ▼
10. LLM returns suggested filename + up to 5 tags
       │
       ▼
11. Embed tags into PDF/A XMP metadata (dc:subject, docuforge:tags, pdf:Keywords)
       │
       ▼
12. Save to /data/output/ — rename if AUTO_RENAME enabled, else await approval
       │
       ▼
13. Show download link + job history in UI (with tags, preview, approve controls)
```

### Split Pipeline (multi-document PDFs)

```
1. Upload multi-doc PDF
       │
       ▼
2. If pages ≥ split_min_pages → enter split detection
       │
       ▼
3. Render all pages at split_dpi (e.g., 150 DPI)
       │
       ├── 3a. Heuristic pass (Tesseract — free)
       │       ├─ Blank page detection (pixel variance)
       │       ├─ Page number reset (OCR footer 15%)
       │       └─ Header change (OCR header 10%)
       │       → Candidate boundaries + blank page list
       │
       └── 3b. Vision model pass (Ollama — for gaps)
               └─ Send page contact sheets (10-page batches)
               → Fill in missed boundaries, assign confidence
       │
       ▼
4. Merge results → boundaries with confidence scores
       │
       ▼
5. Job enters "awaiting_split_approval"
       │
       ▼
6. User reviews split preview (batch table with checkboxes)
       ├─ Adjusts boundaries, merges docs, edits names
       └─ Clicks "Approve All" or "Approve Selected"
       │
       ▼
7. PDF physically split into child PDFs (pikepdf)
       ├─ Blank pages stripped from children
       └─ Original preserved as {name}_source.pdf
       │
       ▼
8. Each child job enters standard pipeline (steps 3–13 above)

---

## 9. API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Web UI |
| `POST` | `/api/upload` | Upload file(s) for processing |
| `GET` | `/api/jobs` | List all processing jobs (paginated) |
| `GET` | `/api/jobs/{id}` | Get job status & details |
| `GET` | `/api/download/{id}` | Download processed file |
| `GET` | `/api/config` | Get current configuration |
| `POST` | `/api/config` | Update configuration (persisted to disk) |
| `GET` | `/api/health` | Health check (includes Ollama status) |
| `GET` | `/api/jobs/{id}/preview` | Get OCR text + proposed name/tags for approval |
| `POST` | `/api/jobs/{id}/rename` | Regenerate name/tags with custom prompt |
| `PUT` | `/api/jobs/{id}/metadata` | Manually edit filename and tags |
| `GET` | `/api/tags` | List all unique tags across all jobs |
| `POST` | `/api/jobs/{id}/retry` | Retry a failed job |
| `GET` | `/api/config/test-ollama` | Test Ollama connectivity |
| `GET` | `/api/watcher/status` | Folder watcher status |
| `POST` | `/api/watcher/start` | Start folder watcher |
| `POST` | `/api/watcher/stop` | Stop folder watcher |
| `GET` | `/api/logs` | Recent application events |
| `POST` | `/api/jobs/{id}/split-detect` | Run split boundary detection |
| `GET` | `/api/jobs/{id}/split-preview` | Get boundaries + thumbnails + proposed names |
| `PUT` | `/api/jobs/{id}/split-points` | Adjust split boundaries |
| `POST` | `/api/jobs/{id}/split-confirm` | Confirm splits → create child jobs |
| `POST` | `/api/jobs/batch-approve` | Finalize metadata for multiple awaiting jobs |

---

## 10. Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_HOST` | `http://ollama:11434` | Ollama API endpoint |
| `OCR_MODEL` | `glm-ocr` | Ollama vision model for OCR |
| `OCR_ENGINE` | `tesseract` | OCR backend: `tesseract` (CPU) or `glm-ocr` (Ollama) |
| `TAGGING_MODEL` | `llama3.2` | Ollama model for renaming & tagging |
| `DPI` | `300` | Rendering DPI for OCR (72–600) |
| `PDFA_LEVEL` | `2b` | PDF/A conformance level (`2b` or `3b`) |
| `UPLOAD_FOLDER` | `/data/uploads` | Input directory |
| `OUTPUT_FOLDER` | `/data/output` | Output directory |
| `MAX_FILE_SIZE_MB` | `100` | Max upload size (1–500) |
| `WATCH_INTERVAL` | `5` | Folder watch interval in seconds (1–60) |
| `RENAME_PROMPT` | *(see .env.example)* | Prompt template — must contain `{ocr_text}` |
| `AUTO_RENAME` | `false` | If false, user must approve name/tags before finalizing |
| `OLLAMA_AUTO_PULL` | `true` | Auto-pull models on first container boot |
| `OLLAMA_AUTO_PULL_MODELS` | `glm-ocr,llama3.2` | Comma-separated model names to pull |
| `SPLIT_ENGINE` | `hybrid` | Split detection: `heuristic` (Tesseract only), `hybrid` (Tesseract + vision), `off` |
| `SPLIT_DPI` | `150` | Rendering DPI for split detection (lower = faster) |
| `SPLIT_CONFIDENCE` | `0.7` | Auto-accept split boundaries above this confidence |
| `SPLIT_MODEL` | `glm-ocr` | Vision model for boundary detection |
| `SPLIT_MIN_PAGES` | `3` | Only offer splitting for PDFs with ≥ this many pages |

---

## 11. Future Enhancements (Phase 3)

- Parallel page OCR for multi-page documents
- Multi-language OCR support
- VeraPDF auto-validation of PDF/A output
- vLLM integration for higher-quality GLM-OCR (see `GLM-OCR_vLLM_plan.md`)
- Webhook notifications on job completion
- User authentication (Basic Auth / OIDC)
- S3 / WebDAV output targets
- PDF compression / optimization

---

## 12. Success Metrics

- A 10-page scanned PDF processes end-to-end in under 3 minutes
- A 200-page multi-document PDF is split into individual documents in under 5 minutes
- Split detection correctly identifies >90% of document boundaries without user intervention
- Output PDF passes PDF/A-2b compliance
- OCR text is selectable and copyable with >95% accuracy
- App starts with a single `docker compose up` command
- Zero data leaves the local Docker network

---

## 13. Glossary

| Term | Definition |
|------|-----------|
| **PDF/A** | ISO 19005 — PDF for long-term archiving. Self-contained, no external dependencies. |
| **OCR** | Optical Character Recognition — extracting machine-readable text from images. |
| **GLM-OCR** | A vision-language model specialized for OCR tasks, runnable via Ollama. |
| **Ollama** | Local LLM runtime, similar to Docker for AI models. |
| **Bounding Box** | Coordinates (x, y, width, height) of a word/line on a page. |
| **Text Layering** | Placing invisible selectable text over a scanned image in a PDF. |

---

## 14. Implementation Plan

### Priority Hierarchy

| Priority | Label | Rule |
|----------|-------|------|
| **P0** | Blocker | Must be complete before any P1 work begins |
| **P1** | Critical | Core product value — ship before anything else |
| **P2** | High | Important but scheduled after P0/P1 |
| **P3** | Medium | Completes the experience — after P2 |
| **P4** | Low | Polish, docs, deployment hardening |

> **Rule:** No sprint begins until all items in the previous priority tier are *done and tested*.

---

### Sprint 0 — Environment & Scaffolding (P0 — Blocker)

**Goal:** A developer can clone the repo and run `docker compose up` to see a Hello World web page. All tooling is wired.

| # | Task | File(s) | Depends On |
|---|------|---------|------------|
| 0.1 | Create project directory structure | `app/`, `app/templates/`, `app/static/`, `data/uploads/`, `data/output/` | — |
| 0.2 | Write `Dockerfile` (Python 3.11-slim, installs poppler-utils for pdf2image) | `Dockerfile` | 0.1 |
| 0.3 | Write `docker-compose.yml` (docuforge + ollama services, volumes, networks) | `docker-compose.yml` | 0.2 |
| 0.4 | Write `requirements.txt` (fastapi, uvicorn, pikepdf, pypdf, pdf2image, Pillow, img2pdf, httpx, python-multipart) | `requirements.txt` | 0.1 |
| 0.5 | Write `.env.example` and `.env` with all config vars | `.env`, `.env.example` | 0.1 |
| 0.6 | Write `app/config.py` — loads env vars into a typed `Settings` dataclass | `app/config.py` | 0.5 |
| 0.7 | Write `app/main.py` — minimal FastAPI app with `/` returning `index.html`, `/api/health` | `app/main.py` | 0.6 |
| 0.8 | Write `app/templates/index.html` — blank shell with upload zone placeholder | `app/templates/index.html` | 0.7 |
| 0.9 | Write `app/static/style.css` — minimal dark/light theme skeleton | `app/static/style.css` | 0.8 |
| 0.10 | Verify `docker compose up` serves the UI on `http://localhost:8080` | — | all above |

**Code Commenting Standard (applies to ALL sprints):**
```
Every function MUST have a docstring explaining:
  1. What it does (one line)
  2. Args (type + meaning)
  3. Returns (type + meaning)
  4. Raises (if any)

Every logical block (>5 lines) MUST have a # comment on the line above
explaining the intent. Use full sentences. Future you will thank present you.
```

<details>
<summary>Example: Well-commented function</summary>

```python
async def ocr_page(image_path: str, model: str = "glm-ocr") -> dict:
    """
    Send a single page image to GLM-OCR via Ollama and return structured text + bounding boxes.

    Args:
        image_path: Absolute path to the rendered page PNG (300 DPI).
        model: Ollama model name for OCR. Defaults to "glm-ocr".

    Returns:
        dict with keys:
            - "full_text": str  — all recognized text concatenated
            - "words": list[dict] — [{"text": str, "bbox": [x,y,w,h]}, ...]

    Raises:
        OCRError: If the Ollama API is unreachable or the model is not pulled.
    """
    # 1. Read the image file as base64 for the Ollama vision API
    with open(image_path, "rb") as f:
        image_b64 = base64.b64encode(f.read()).decode("utf-8")

    # 2. Construct the prompt that instructs GLM-OCR to return bounding boxes
    #    The prompt forces JSON output with per-word coordinates for precise text layering
    prompt = (
        "Extract ALL text from this image. "
        "For each word, provide: the word text, and its bounding box as [x, y, width, height] "
        "in pixels relative to the top-left corner. "
        "Return ONLY valid JSON: {\"full_text\": \"...\", \"words\": [{\"text\": \"...\", \"bbox\": [...]}]}"
    )

    # 3. Call Ollama /api/generate endpoint
    payload = {
        "model": model,
        "prompt": prompt,
        "images": [image_b64],
        "stream": False,
        "format": "json",  # Force JSON output mode
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            response = await client.post(f"{settings.ollama_host}/api/generate", json=payload)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OCRError(f"Ollama API call failed: {exc}") from exc

    # 4. Parse the JSON response — Ollama returns it in the "response" field
    result = response.json()
    return json.loads(result["response"])
```
</details>

---

### Sprint 1 — Core Processing Pipeline (P1 — Critical)

**Goal:** Upload a scanned PDF → get back a PDF/A with invisible OCR text layer. End-to-end functional.

| # | Task | File(s) | Depends On |
|---|------|---------|------------|
| 1.1 | Implement `pdf_to_images()` — renders PDF pages to PNGs at configurable DPI using `pdf2image` | `app/processor.py` | 0.10 |
| 1.2 | Implement `image_to_pdf()` — converts a single image (jpg/png/tiff) to an interim PDF using `img2pdf` | `app/pdf_utils.py` | 0.10 |
| 1.3 | Implement `convert_to_pdfa()` — takes any PDF and converts it to PDF/A-2b using `pikepdf` (embed fonts, add XMP metadata, strip JS/external refs, set output intent to sRGB) | `app/pdf_utils.py` | 0.10 |
| 1.4 | Implement `ocr_page()` — sends a page image to GLM-OCR via Ollama, parses the JSON response into `{full_text, words: [{text, bbox}]}` | `app/ocr.py` | 1.1 |
| 1.5 | Implement `layer_text_on_pdf()` — uses `pypdf` to add invisible text annotations at each word's bounding box position on every page | `app/pdf_utils.py` | 1.4, 1.3 |
| 1.6 | Implement `process_file()` — orchestrates the full pipeline for one file: detect type → convert → render pages → OCR each page → PDF/A convert → layer text → save | `app/processor.py` | 1.2–1.5 |
| 1.7 | Implement `POST /api/upload` endpoint — accepts multipart file upload, saves to `/data/uploads`, kicks off async processing, returns `job_id` | `app/main.py` | 1.6 |
| 1.8 | Implement `JobManager` class — tracks jobs in memory with statuses: `queued → processing → done/failed`, stores metadata (filename, pages, timestamps) | `app/processor.py` | 1.6 |
| 1.9 | Implement `GET /api/jobs`, `GET /api/jobs/{id}`, `GET /api/download/{id}` | `app/main.py` | 1.8 |
| 1.10 | Build the upload UI: drag-and-drop zone with file list, progress indicators per file | `app/templates/index.html` | 1.7 |
| 1.11 | Build the job history table: shows status, filename, pages, time, download button — polls `/api/jobs` every 2s | `app/templates/index.html` | 1.9 |

---

### Sprint 2 — Renaming & Tagging (P2 — High)

**Goal:** After OCR, the LLM reads the text and suggests a filename + up to 5 tags. User can approve/edit.

| # | Task | File(s) | Depends On |
|---|------|---------|------------|
| 2.1 | Implement `generate_name_and_tags()` — sends OCR'd plain text + user prompt to Ollama (e.g. `llama3.2`) → parses `{filename, tags}` JSON response | `app/tagger.py` | 1.4 |
| 2.2 | Implement `embed_tags_in_pdf()` — injects tags into PDF/A XMP metadata under `dc:subject` and a custom `docuforge` namespace using `pikepdf` | `app/pdf_utils.py` | 1.3 |
| 2.3 | Implement `sanitize_filename()` — strips unsafe chars, enforces max 200 chars, preserves extension | `app/tagger.py` | 2.1 |
| 2.4 | Integrate renaming into `process_file()` pipeline — after OCR layer, call tagger, embed tags, rename output file | `app/processor.py` | 2.1–2.3 |
| 2.5 | Implement `POST /api/jobs/{id}/rename` — regenerates name/tags with a new user prompt | `app/main.py` | 2.4 |
| 2.6 | Implement `PUT /api/jobs/{id}/metadata` — manually edits filename/tags | `app/main.py` | 2.4 |
| 2.7 | Implement `GET /api/jobs/{id}/preview` — returns OCR text + proposed name/tags for user approval | `app/main.py` | 2.4 |
| 2.8 | Build the "Preview & Approve" modal in the UI — shows proposed name, editable tags, approve / regenerate / manually edit buttons | `app/templates/index.html` | 2.5–2.7 |
| 2.9 | Add `AUTO_RENAME=false` support — if set, job pauses at `awaiting_approval` status until user acts | `app/processor.py`, `app/main.py` | 2.8 |
| 2.10 | Build tag filter/search bar above job history table | `app/templates/index.html` | 2.8 |

---

### Sprint 3 — Folder Watcher & Bulk Operations (P3 — Medium)

**Goal:** Drop files into a network folder → they auto-process. Batch upload and queue management.

| # | Task | File(s) | Depends On |
|---|------|---------|------------|
| 3.1 | Implement `watch_folder()` background task — uses `watchfiles` to monitor `/data/uploads` for new files, auto-enqueues them | `app/processor.py` | 1.8 |
| 3.2 | Add folder watcher toggle in web UI (start/stop watch, show status) | `app/main.py`, `app/templates/index.html` | 3.1 |
| 3.3 | Implement job queue with max concurrency setting (default: 1 sequential, configurable) | `app/processor.py` | 1.8 |
| 3.4 | Build batch upload — drag multiple files, queue them all, show collective progress | `app/templates/index.html` | 1.10 |
| 3.5 | Implement `GET /api/tags` — returns all unique tags with counts for the filter bar | `app/main.py` | 2.10 |

---

### Sprint 4 — Configuration Panel & Settings Persistence (P3 — Medium)

**Goal:** Users can tweak all knobs from the web UI without editing `.env`.

| # | Task | File(s) | Depends On |
|---|------|---------|------------|
| 4.1 | Build the Settings page UI — form fields for all config vars (Ollama host, DPI, PDF/A level, models, auto-rename toggle, prompt template textarea) | `app/templates/index.html` | 0.10 |
| 4.2 | Implement `POST /api/config` and `GET /api/config` — reads/writes config; on write, validates connectivity (tests Ollama reachable) | `app/main.py`, `app/config.py` | 4.1 |
| 4.3 | Persist config to a JSON file on disk so settings survive container restarts | `app/config.py` | 4.2 |
| 4.4 | Add "Test Ollama Connection" button in settings that calls `GET /api/health` | `app/templates/index.html` | 4.2 |

---

### Sprint 5 — Error Handling, Resilience & Validation (P3 — Medium)

**Goal:** Failed pages don't kill the whole document. Clear error messages. Optional VeraPDF validation.

| # | Task | File(s) | Depends On |
|---|------|---------|------------|
| 5.1 | Wrap each page OCR in try/except — on failure, log the error, skip that page's text layer, continue with remaining pages | `app/processor.py` | 1.6 |
| 5.2 | Add job-level retry button in UI — `POST /api/jobs/{id}/retry` | `app/main.py`, `app/templates/index.html` | 5.1 |
| 5.3 | Add error detail in job history — expand row to see per-page errors | `app/templates/index.html` | 5.2 |
| 5.4 | Integrate VeraPDF CLI (optional, via subprocess) — validate output PDF/A, report in job details | `app/pdf_utils.py` | 1.3 |
| 5.5 | Add file size limit enforcement (configurable, default 100 MB) with clear user-facing error | `app/main.py` | 1.7 |
| 5.6 | Add supported format validation — reject unsupported file types early with a clear message | `app/main.py` | 1.7 |

---

### Sprint 6 — Deployment Hardening & Documentation (P4 — Low)

**Goal:** Production-ready `docker-compose.yml`, health checks, README, one-command startup.

| # | Task | File(s) | Depends On |
|---|------|---------|------------|
| 6.1 | Add Docker health checks to both services (`curl /api/health` for app, `ollama ps` for Ollama) | `docker-compose.yml` | 0.10 |
| 6.2 | Write `docker-compose.gpu.yml` override — adds NVIDIA runtime for GPU passthrough | `docker-compose.gpu.yml` | 6.1 |
| 6.3 | Add `depends_on` with `condition: service_healthy` so app waits for Ollama | `docker-compose.yml` | 6.1 |
| 6.4 | Write pull-models.sh — script that runs `ollama pull glm-ocr` and `ollama pull llama3.2` on first boot | `scripts/pull-models.sh` | 6.1 |
| 6.5 | Write `README.md` — quickstart, architecture overview, config reference, FAQ | `README.md` | all |
| 6.6 | Add `.dockerignore` to keep build context lean | `.dockerignore` | 0.2 |
| 6.7 | Final integration smoke test — upload a 3-page scanned PDF, verify: PDF/A output, text is selectable, tags are embedded, download works | — | all |

---

### Sprint Timeline (Estimated)

```
Week 1: Sprint 0 (Env) + Sprint 1 (Core Pipeline)
Week 2: Sprint 2 (Renaming & Tagging)
Week 3: Sprint 3 (Folder Watcher) + Sprint 4 (Config Panel)
Week 4: Sprint 5 (Error Handling) + Sprint 6 (Deployment)
Week 5: Sprint 7 (Smart Splitting & Batch Approval)
─────────────────────────────────────────────────────────
Week 6: Buffer / Bug fixes / Polish
```

### Dependency Graph

```
Sprint 0 (P0) ──► Sprint 1 (P1) ──┬──► Sprint 2 (P2) ──► Sprint 3 (P3)
                                   │
                                   ├──► Sprint 4 (P3) ──► Sprint 5 (P3) ──► Sprint 6 (P4)
                                   │                                    │
                                   └──► Sprint 5 (P3) ──► Sprint 7 (P1) └──► Sprint 6 (P4)
```

- **Sprint 0** blocks everything (no app without scaffolding).
- **Sprint 1** blocks Sprints 2–6 (no processing pipeline to extend).
- **Sprint 2** blocks Sprint 3's tag features.
- **Sprints 3, 4, 5** can run partially in parallel after Sprint 1 is done.
- **Sprint 6** should be the last thing before shipping.
- **Sprint 7** depends on Sprint 5 (error handling) and Sprint 1 (core pipeline). It can run in parallel with other post-Sprint-1 work.

### Sprint 7 — Smart Splitting & Batch Approval (P1 — Critical)

**Goal:** Multi-document PDFs are auto-detected, split into individual documents, and batch-approved.

| # | Task | File(s) | Depends On |
|---|------|---------|------------|
| 7.1 | Implement `SplitDetector` class — heuristic boundary detection (blank pages, page number reset, header change) | `app/splitter.py` | 1.1, 1.4 |
| 7.2 | Implement vision model boundary detection — sends page contact sheets to Ollama for document-type/layout boundaries | `app/splitter.py` | 7.1 |
| 7.3 | Implement `split_pdf()` — extracts page ranges into child PDFs, strips blank pages, preserves original | `app/pdf_utils.py` | 1.3 |
| 7.4 | Add `JobData` fields: `job_type`, `parent_job_id`, `child_job_ids`, `split_boundaries`, `split_confidences`, `blank_pages_removed` | `app/processor.py` | 1.8 |
| 7.5 | Add config fields: `split_engine`, `split_dpi`, `split_confidence`, `split_model`, `split_min_pages` with validators | `app/config.py` | 0.6 |
| 7.6 | Implement split API routes: `POST split-detect`, `GET split-preview`, `PUT split-points`, `POST split-confirm`, `POST batch-approve` | `app/main.py` | 7.1–7.5 |
| 7.7 | Build split preview UI — table with checkboxes, select-all, thumbnails, confidence indicators, merge/split controls | `app/templates/index.html` | 7.6 |
| 7.8 | Build batch approve UI — select-all checkbox, approve selected, approve all, inline name/tag editing | `app/templates/index.html` | 7.6 |

---

*Document Version: 1.3 | Last Updated: 2026-05-16 — Added Sprint 7 (Smart Splitting), FR-10, FR-11; fixed stale references*

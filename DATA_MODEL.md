# DocuForge — Data Model Reference

Single source of truth for all data structures used across modules.

---

## 1. JobData (The Core Entity)

A `JobData` instance represents one document through its entire processing lifecycle.

### 1.1 Lifecycle States

```
queued → processing → awaiting_approval → done
                   ↘  failed
```

| State | Meaning |
|-------|---------|
| `queued` | File uploaded, waiting in the processing queue |
| `processing` | Pipeline is actively running (rendering, OCR, PDF/A, layering) |
| `awaiting_approval` | OCR + PDF/A done; waiting for user to approve name/tags (only when `AUTO_RENAME=false`) |
| `done` | Processing completed, output file available for download |
| `failed` | Unrecoverable error — no output produced |

### 1.2 Fields

| Field | Type | Required | Added In | Description |
|-------|------|----------|----------|-------------|
| `id` | `str` | Always | Sprint 1 | UUID7 via `uuid6.uuid7()`. Primary key for all API routes. |
| `original_name` | `str` | Always | Sprint 1 | The uploaded file's original filename (e.g., `scan_001.pdf`). |
| `status` | `JobStatus` | Always | Sprint 1 | Current lifecycle state (enum from `queued` to `failed`). |
| `created_at` | `datetime(UTC)` | Always | Sprint 1 | ISO timestamp when the job was created. |
| `finished_at` | `datetime(UTC) \| None` | On completion | Sprint 1 | ISO timestamp when processing ended. `None` while in progress. |
| `pages` | `int` | After render | Sprint 1 | Total number of pages in the document. |
| `pages_done` | `int` | After OCR start | Sprint 1 | Number of pages that finished OCR (used for progress bar). |
| `output_filename` | `str \| None` | On completion | Sprint 1 | Name of the output file relative to `OUTPUT_FOLDER`. |
| `error` | `str \| None` | On failure | Sprint 1 | Human-readable error message if status is `failed`. |
| `tags` | `list[str]` | After tagging | Sprint 2 | Up to 5 LLM-generated or user-edited tags. |
| `ocr_full_text` | `str \| None` | After OCR | Sprint 2 | Concatenated OCR text from all pages, used for renaming/tagging preview. Max 4000 chars sent to LLM. |
| `tier_summary` | `dict[str, int]` | After OCR | Sprint 1 | Count of pages per OCR tier: `{"word": 5, "line": 2, "full_page": 0, "skip": 1}`. |
| `proposed_name` | `str \| None` | After LLM rename | Sprint 2 | LLM-suggested filename (before sanitization). Shown in preview modal. |
| `proposed_tags` | `list[str]` | After LLM tagging | Sprint 2 | LLM-suggested tags (before user approval). Shown in preview modal. |
| `file_path` | `str \| None` | On upload | Sprint 5 | Absolute path to the uploaded file on disk. Used for job retry. |
| `page_errors` | `list[dict]` | On per-page failure | Sprint 5 | Per-page OCR failures: `[{"page": 3, "error": "OCRError: ..."}, ...]`. |
| `pdfa_saved` | `bool` | After PDF/A step | Sprint 5 | `True` if PDF/A was generated (enables partial output on failure). |
| `skip_rename` | `bool` | On upload | Sprint 7 | If `true`, LLM filename suggestion is skipped for this job. |
| `skip_tags` | `bool` | On upload | Sprint 7 | If `true`, LLM tag suggestion is skipped for this job. |

### 1.3 Planned Fields (Sprint 8)

| Field | Type | Required | Phase | Description |
|-------|------|----------|-------|-------------|
| `doc_type` | `str \| None` | After classify | 8.1 | One of: `letter`, `invoice`, `form`, `quote`, `contract`, `report`, `other`. |
| `text_layer_score` | `int \| None` | After verify | 8.2 | Verification score 0–100. `None` if verification is disabled. |
| `text_layer_warnings` | `list[str]` | After verify | 8.2 | Human-readable warning messages from verification checks. |
| `extracted_fields` | `dict \| None` | After extraction | 8.3 | Invoice fields: `{"invoice_date": "...", "total_amount": "...", "vendor_name": "..."}`. `None` for non-invoices. |

### 1.4 Planned Fields (Future Sprint 7)

| Field | Type | Description |
|-------|------|-------------|
| `job_type` | `str` | `"standard"` or `"split_child"`. |
| `parent_job_id` | `str \| None` | ID of the parent multi-doc PDF job. |
| `child_job_ids` | `list[str]` | IDs of child jobs created by splitting. |
| `split_boundaries` | `list[int]` | Page numbers where splits occur. |
| `split_confidences` | `list[float]` | Confidence scores per boundary (0.0–1.0). |
| `blank_pages_removed` | `list[int]` | Page numbers of detected blank pages that were stripped. |

### 1.5 API Serialization (`to_dict()`)

```json
{
  "id": "019dcb22-0abe-7460-a19e-e4ffb3a2dc9e",
  "original_name": "invoice_scan.pdf",
  "status": "done",
  "created_at": "2026-05-16T18:51:39.070770+00:00",
  "finished_at": "2026-05-16T18:52:30.000000+00:00",
  "pages": 3,
  "pages_done": 3,
  "output_filename": "2025-03-15_Acme_Invoice.pdf",
  "error": null,
  "tags": ["invoice", "acme-corp", "2025"],
  "tier_summary": {"word": 3, "line": 0, "full_page": 0, "skip": 0},
  "proposed_name": "2025-03-15_Acme_Invoice",
  "proposed_tags": ["invoice", "acme-corp", "2025"],
  "ocr_full_text": "ACME CORPORATION\nINVOICE #1042\n...",
  "page_errors": [],
  "pdfa_saved": true
}
```

---

## 2. OCR Result Dict

Returned by `ocr_page()` in `app/ocr.py`. One dict per page.

### 2.1 Schema

```json
{
  "full_text": "All recognized text from this page...",
  "words": [
    {"text": "INVOICE", "bbox": [100, 50, 72, 14]},
    {"text": "ACME", "bbox": [180, 50, 45, 14]}
  ],
  "tier": "word",
  "ocr_success": true
}
```

### 2.2 Fields

| Field | Type | Description |
|-------|------|-------------|
| `full_text` | `str` | All recognized text concatenated with spaces. Empty string if OCR failed. |
| `words` | `list[dict]` | Per-word bounding boxes. Empty list if tier is `line` or `full_page`. |
| `words[].text` | `str` | The recognized word text. |
| `words[].bbox` | `[x, y, w, h]` | Bounding box in **image pixels**: `[left, top, width, height]`. Origin at top-left. |
| `tier` | `str` | Quality tier: `"word"`, `"line"`, `"full_page"`, or `"skip"`. |
| `ocr_success` | `bool` | `True` if the OCR engine call succeeded (even if no text was found). |

### 2.3 Tier Definitions

| Tier | Source | Text Layering Behavior |
|------|--------|----------------------|
| `word` | Tesseract TSV or GLM-OCR with valid bboxes | Each word placed at exact (x,y) in PDF. Best quality. |
| `line` | GLM-OCR returned text but no/invalid bboxes | Text split by newlines, evenly spaced down the page margin. |
| `full_page` | GLM-OCR returned raw text, no JSON structure | Entire text block placed at top of page. Degraded experience. |
| `skip` | OCR engine call failed or returned nothing | No text layer for this page. |

### 2.4 Coordinate Transform

OCR bounding boxes are in **image pixels** (origin top-left, y-down).  
They must be transformed to **PDF points** (origin bottom-left, y-up) for text layering:

```
scale  = 72.0 / dpi              # pixels → points
x_pt   = bbox.x * scale          # horizontal position
y_pt   = page_height_pt - ((bbox.y + bbox.h) * scale)  # bottom of word in PDF coords
font   = max(bbox.h * scale * 0.85, 4)  # font size in points
```

See `app/pdf_utils.py:_build_text_operators()` for the implementation.

---

## 3. Settings / Configuration

Loaded by `app/config.py:get_settings()`. Load order: `.env` → `data/settings.json` (JSON overrides env).

### 3.1 All Fields

| Field | Type | Default | Range | Editable via UI | Description |
|-------|------|---------|-------|-----------------|-------------|
| `ollama_host` | `str` | `http://ollama:11434` | Must start with `http://` or `https://` | Yes | Ollama API endpoint URL. |
| `ocr_model` | `str` | `glm-ocr` | Non-empty | Yes | Ollama vision model name for OCR (GLM-OCR engine). |
| `ocr_engine` | `str` | `tesseract` | `"tesseract"` or `"glm-ocr"` | Yes | OCR backend selection. |
| `tagging_model` | `str` | `llama3.2` | Non-empty | Yes | Ollama model for renaming, tagging, classification, and extraction. |
| `dpi` | `int` | `300` | 72–600 | Yes | Rendering resolution for OCR. Higher = better quality, slower. |
| `pdfa_level` | `str` | `2b` | `"2b"` or `"3b"` | Yes | PDF/A conformance level. |
| `upload_folder` | `str` | `/data/uploads` | — | **No** | Container path where uploaded files are stored. |
| `output_folder` | `str` | `/data/output` | — | **No** | Container path where processed files are written. |
| `max_file_size_mb` | `int` | `100` | 1–500 | Yes | Maximum upload file size in megabytes. |
| `watch_interval` | `int` | `5` | 1–60 | Yes | Folder watcher poll interval in seconds. |
| `auto_rename` | `bool` | `false` | — | Yes | If `true`, applies LLM name/tags immediately. If `false`, pauses at `awaiting_approval`. |
| `rename_prompt` | `str` | *(see config.py)* | Must contain `{ocr_text}` | Yes | Prompt template for LLM renaming. |
| `allowed_extensions` | `set[str]` | `{.pdf, .png, .jpg, .jpeg, .tiff, .tif, .bmp}` | — | No | File extensions accepted for upload. |

### 3.2 Planned Fields (Sprint 8)

| Field | Type | Default | Range | Description |
|-------|------|---------|-------|-------------|
| `classify_model` | `str` | `llama3.2` | Non-empty | Model for document type classification. |
| `verify_text_layer` | `bool` | `true` | — | Enable statistical bounding box verification. |
| `verify_min_score` | `int` | `50` | 0–100 | Minimum score to apply text layer. |
| `extract_fields` | `bool` | `true` | — | Enable structured field extraction for invoices. |
| `extract_model` | `str` | `llama3.2` | Non-empty | Model for structured field extraction. |
| `rename_prompt_invoice` | `str \| None` | `None` | Must contain `{ocr_text}` | Type-specific rename prompt for invoices. Falls back to `rename_prompt`. |
| `rename_prompt_contract` | `str \| None` | `None` | Must contain `{ocr_text}` | Type-specific rename prompt for contracts. Falls back to `rename_prompt`. |

### 3.3 Persisted Fields (settings.json)

Only values that differ from pydantic defaults are persisted. See `app/config.py:save_settings_to_json()`.

```json
{
  "dpi": 400,
  "auto_rename": true,
  "rename_prompt": "Custom prompt with {ocr_text}"
}
```

---

## 4. API Shapes

### 4.1 `POST /api/upload`

**Request:** `multipart/form-data`

| Field | Type | Description |
|-------|------|-------------|
| `file` | `file` (required) | The document to process. |
| `skip_rename` | `bool` (optional) | If `"true"`, skip LLM filename suggestion. |
| `skip_tags` | `bool` (optional) | If `"true"`, skip LLM tag suggestion. |

**Response:** `201 Created`
```json
{
  "job_id": "019dcb22-0abe-7460-a19e-e4ffb3a2dc9e",
  "original_name": "scan_001.pdf",
  "status": "queued"
}
```

### 4.2 `GET /api/jobs`

**Query params:** `page` (default 1), `per_page` (default 20, max 100)

**Response:** `200 OK`
```json
{
  "jobs": [{ "...JobData.to_dict()..." }],
  "total": 47,
  "page": 1,
  "per_page": 15,
  "pages": 4
}
```

### 4.3 `GET /api/jobs/{id}`

**Response:** `200 OK` — Single `JobData.to_dict()` object.

### 4.4 `GET /api/jobs/{id}/preview`

**Response:** `200 OK`
```json
{
  "ocr_full_text": "First 4000 chars of OCR text...",
  "proposed_name": "2025-03-15_Acme_Invoice",
  "proposed_tags": ["invoice", "acme-corp"]
}
```

### 4.5 `POST /api/jobs/{id}/rename`

**Request:** `application/json`
```json
{"prompt": "Optional custom prompt override"}
```

### 4.6 `PUT /api/jobs/{id}/metadata`

**Request:** `application/json`
```json
{
  "filename": "custom_name",
  "tags": ["tag1", "tag2"]
}
```

### 4.7 `GET /api/tags`

**Response:** `200 OK`
```json
{
  "tags": [
    {"name": "invoice", "count": 12},
    {"name": "contract", "count": 8}
  ]
}
```

### 4.8 `GET /api/logs`

**Query params:** `limit` (default 100)

**Response:** `200 OK`
```json
[
  {
    "timestamp": "2026-05-16T18:52:09.879773+00:00",
    "level": "info",
    "message": "Awaiting approval: invoice_test",
    "job_id": "019dcb22-0abe-7460-a19e-e4ffb3a2dc9e"
  }
]
```

### 4.9 `GET /api/health`

**Response:** `200 OK`
```json
{
  "status": "healthy",
  "ollama": "connected"
}
```

### 4.10 `GET /api/config` / `POST /api/config`

**Response / Request body:** Flat JSON of editable settings fields (see §3).

### 4.11 `GET /api/download/{id}`

**Response:** Binary file download (PDF/A with OCR text layer).

### 4.12 `GET /api/watcher/status`

**Response:** `200 OK`
```json
{
  "active": true,
  "watching": "/data/uploads",
  "interval": 5
}
```

### 4.13 Planned (Sprint 8)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/jobs/{id}/classify` | Get or regenerate document type classification. |
| `GET` | `/api/jobs/{id}/fields` | Get extracted structured fields JSON. |

### 4.14 Planned (Sprint 7)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/jobs/{id}/split-detect` | Run split boundary detection. |
| `GET` | `/api/jobs/{id}/split-preview` | Get boundaries + thumbnails + proposed names. |
| `PUT` | `/api/jobs/{id}/split-points` | Adjust split boundaries. |
| `POST` | `/api/jobs/{id}/split-confirm` | Confirm splits → create child jobs. |
| `POST` | `/api/jobs/batch-approve` | Finalize metadata for multiple awaiting jobs. |

---

## 5. Event Log Entry

In-memory ring buffer (500 entries max). Written by `log_event()` in `app/processor.py`.

```json
{
  "timestamp": "2026-05-16T18:51:39.070323+00:00",
  "level": "info",
  "message": "File queued: invoice_test.png",
  "job_id": "019dcb22-0abe-7460-a19e-e4ffb3a2dc9e"
}
```

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | `str` | ISO 8601 UTC timestamp. |
| `level` | `str` | `"info"`, `"warn"`, or `"error"`. |
| `message` | `str` | Human-readable event description. |
| `job_id` | `str \| None` | Associated job ID, or `null` for global events (startup, shutdown, etc.). |

**Standard events emitted by the pipeline:**

| Event | Level | When |
|-------|-------|------|
| `DocuForge started, queue worker running` | info | Application startup |
| `File queued: {name}` | info | File uploaded or detected by watcher |
| `Processing started: {name}` | info | Pipeline begins for a job |
| `Document classified: {type}` | info | Classification completes (Sprint 8) |
| `Text layer verification: score {N}` | info/warn | Verification check completes (Sprint 8) |
| `Text layer skipped: score {N} < {min}` | warn | Score below threshold (Sprint 8) |
| `Fields extracted: {...}` | info | Invoice fields extracted (Sprint 8) |
| `Awaiting approval: {name}` | info | Job paused for user to approve metadata |
| `Approved: {name}` | info | User approved name/tags |
| `Processing completed: {name}` | info | Pipeline finished successfully |
| `Processing failed: {name} — {error}` | error | Unrecoverable error |
| `Watcher started: {folder}` | info | Folder watcher activated |
| `Watcher stopped: {folder}` | info | Folder watcher deactivated |

---

## 6. File Naming Conventions

### 6.1 Output Files

| Pattern | Example | When |
|---------|---------|------|
| `{stem}_ocr.pdf` | `scan_001_ocr.pdf` | Default output name (before rename) |
| `{LLM_suggested}.pdf` | `2025-03-15_Acme_Invoice.pdf` | After LLM rename approved |
| `{output}.json` | `2025-03-15_Acme_Invoice.json` | Structured field extraction sidecar (Sprint 8) |
| `{stem}_source.pdf` | `batch_scan_source.pdf` | Original multi-doc PDF preserved after splitting (Sprint 7) |

### 6.2 Interim Files (temp directory)

| Pattern | Directory | Purpose |
|---------|-----------|---------|
| `docuforge_{job_id}_*` | `/tmp/docuforge/` | Job temp directory (cleaned on container restart) |
| `{stem}_interim.pdf` | Job temp dir | Image-to-PDF conversion output |
| `{stem}_pdfa.pdf` | Job temp dir | PDF/A conversion output (before text layering) |
| `page_*.png` | `/tmp/docuforge/renders/` | Rendered page images for OCR |

### 6.3 Sanitization Rules (`app/tagger.py:sanitize_filename()`)

1. Strip directory path (keep basename only)
2. Remove existing extension (re-add `.pdf` cleanly)
3. Replace `<>:"/\|?*` with `_`
4. Collapse multiple underscores
5. Replace whitespace with `_`
6. Strip leading/trailing `_. `
7. Fallback to `"untitled"` if empty
8. Truncate to 200 chars total (stem + extension)

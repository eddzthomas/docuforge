# AGENTS.md — DocuForge

## Dev environment

- Zero dev tooling: **no linter, formatter, test runner, or CI**. Do not try `pytest` or `ruff`.
- All development happens inside Docker. The app runs as `uvicorn app.main:app` on port 8080.
- `pip install -r requirements.txt` if running locally, but prefer Docker.

## Start / stop

```bash
docker compose up --build                        # CPU only
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up --build  # GPU
docker compose -f docker-compose.yml -f docker-compose.remote-ollama.yml up --build  # remote Ollama
```

First boot: wait 30-60s for Ollama to warm up. Pull models manually if auto-pull fails:
```bash
docker exec -it docuforge-ollama ollama pull glm-ocr
docker exec -it docuforge-ollama ollama pull llama3.2
```

## Architecture

```
app/
  main.py       -- FastAPI routes + lifespan (queues worker, watcher)
  config.py     -- Settings (env vars → pydantic → settings.json override)
  processor.py  -- Pipeline orchestrator, JobManager, JobQueue, folder watcher
  ocr.py        -- OCR dispatch: tesseract (CPU) or GLM-OCR via Ollama
  tagger.py     -- LLM rename + tag + classify + field extraction via Ollama
  verifier.py   -- Statistical bounding-box validation before text layering
  pdf_utils.py  -- PDF/A conversion (pikepdf), text layering (pikepdf), tag embedding
  splitter.py   -- SplitDetector for multi-page PDF boundary detection
  sample_matcher.py -- Sample-based similarity matching (perceptual hash + LLM fallback)
  templates/index.html  -- Single-page web UI (vanilla JS, zero build step)
  static/style.css
```

Two Docker services: `docuforge-app` (FastAPI :8080) + `ollama` (Ollama :11434).
Volumes: `./data/` → `/data/` (persists uploads, output, settings.json).

## Config quirks

- Config load order: `.env` → `data/settings.json` (JSON overrides env).
- `get_settings()` is cached via `@lru_cache()`. After POST `/api/config`, must call `reload_settings()` to bust the cache.
- `RENAME_PROMPT` **must** contain `{ocr_text}` placeholder — validated by pydantic field validator.
- `ocr_engine` defaults to `"tesseract"` (CPU). Set to `"glm-ocr"` for Ollama vision model.
- **Recommended LLM model:** `gemma4:e2b` (~7.2GB). Serves tagging, classification, field extraction, and sample LLM fallback. Vision + text. 128K context. Thinking mode. Falls back to `llama3.2` (~2GB) for low-VRAM setups.
- Text layer verification is on by default (`VERIFY_TEXT_LAYER=true`). Stats-only check — no re-rendering. Score < `VERIFY_MIN_SCORE` (default 50) skips text layer for that page.
- Document classification runs on first ~1000 chars of OCR text. Doc types: `letter`, `invoice`, `form`, `quote`, `contract`, `report`, `other`.
- Structured field extraction ONLY fires for `doc_type == "invoice"`. Extracts 3 fields: invoice_date, total_amount, vendor_name. No line items (v2).
- Type-specific rename prompts: `RENAME_PROMPT_INVOICE`, `RENAME_PROMPT_CONTRACT` (optional). Each must contain `{ocr_text}`. Falls back to generic `RENAME_PROMPT`.
- Web UI Settings tab only exposes editable fields: `ollama_host`, `ocr_model`, `ocr_engine`, `tagging_model`, `dpi`, `pdfa_level`, `auto_rename`, `rename_prompt`, `max_file_size_mb`, `watch_interval`, `verify_text_layer`, `verify_min_score`, `extract_fields`. Path fields (`upload_folder`, `output_folder`) are NOT editable via UI.

## Sample-Guided Split Detection

The Split tab supports optional sample documents to guide boundary detection:

- **Sample sources:** Upload up to 5 sample PDFs/images, or select page ranges from the bulk PDF itself.
- **Storage:** Samples are stored in `data/samples/{job_id}/` — survives restarts unlike `/tmp` caches.
- **Similarity method:** Perceptual hashing (`imagehash` library) for speed, with LLM vision fallback for borderline cases.
- **Sliding window:** Multi-page samples are matched using a sliding window approach across bulk pages.
- **Augmentation:** Sample-based boundaries are **merged** with the existing SplitDetector boundaries (union, deduplicate).

**Config settings for sample matching:**

| Setting | Default | Description |
|---------|---------|-------------|
| `SPLIT_SAMPLE_THRESHOLD` | `0.7` | Phash confidence threshold (0-1). Higher = stricter matching. |
| `SPLIT_SAMPLE_LLM_FALLBACK` | `true` | Use LLM vision for borderline phash matches (0.5–0.8 confidence). |
| `SPLIT_SAMPLE_MAX_COUNT` | `5` | Maximum samples per split job. |

**Key files:**
- `app/sample_matcher.py` — SampleManager, SimilarityEngine, LLMFallbackComparer

## Processing pipeline (one-at-a-time)

1. `process_file()` runs in a `JobQueue` worker (max_concurrency=1) — serializes OCR calls.
2. Image files are converted to interim PDF via `img2pdf` before processing.
3. PDF pages are rendered to PNG at configured DPI via poppler (`pdf2image`).
4. OCR on each page is dispatched to the selected engine. Per-page failures are tracked but don't abort the pipeline.
5. **Document classification** — sends first ~1000 chars of OCR text to LLM for doc type detection (invoice, contract, letter, etc.). Stored on `JobData.doc_type`.
6. **Text layer verification** — statistical bounding box checks run before layering. Score 0-100 gates whether text is applied. New module: `app/verifier.py`.
7. PDF/A-2b conversion uses `pikepdf` (strips JS, embedded files, sets XMP metadata).
8. OCR text is layered invisibly (Tr=3) onto the PDF/A with Helvetica font.
9. **Structured field extraction** — if `doc_type == "invoice"`, LLM extracts date, total_amount, vendor_name. Saved as `.json` alongside output PDF.
10. If `AUTO_RENAME=false` (default), job pauses at `awaiting_approval` — user must approve name/tags via UI.
11. Tags are embedded in XMP metadata (`dc:subject`, `pdf:Keywords`) and in `docuforge:tags` custom namespace.

**Note:** Split detection was previously Step 1.5 in the pipeline. It has been moved entirely to the Split tab as a separate workflow. Child jobs created by splitting pause at `AWAITING_APPROVAL` until approved via batch-approve.

## File conventions

- All modules have module-level docstrings. Functions have Google-style docstrings (Args / Returns / Raises).
- Job IDs are UUID7 via `uuid6` library (`uuid6.uuid7()`).
- `JobManager` uses `threading.RLock()` — thread-safe but not async-aware.
- Logging uses `logging.getLogger(__name__)` everywhere. Events also go to an in-memory ring buffer (`/api/logs`).
- Frontend is vanilla HTML/CSS/JS in `app/templates/index.html` — no React, no npm, no build step.
- `docker-compose.remote-ollama.yml` disables local ollama via `profiles: [local-only]` and clears `depends_on`.

## File validation

- Allowed extensions: `.pdf`, `.png`, `.jpg`, `.jpeg`, `.tiff`, `.tif`, `.bmp`.
- Max file size: 1–500 MB (validated by pydantic).
- DPI range: 72–600.
- Watch interval: 1–60 seconds.

## Gotchas

- `img2pdf`, `pikepdf`, and `pypdf` are all in use — they are different libraries with different APIs. Do not confuse them.
- The temp render directory is `/tmp/docuforge/renders` (in-container), not a Docker volume — cleaned on container restart.
- `pdf2image` calls poppler's `pdftoppm` binary — the `poppler-utils` apt package is required in the Docker image.
- Tesseract OCR uses TSV output to get per-word bounding boxes via `pytesseract.image_to_data()`.
- The entrypoint script `docker-entrypoint.sh` runs before uvicorn and handles auto-pulling models.
- Split detection is now decoupled from `process_file()` — it runs exclusively in the Split tab. Children from splitting pause at `AWAITING_APPROVAL` (not `AWAITING_SPLIT_APPROVAL`).
- Sample storage uses `data/samples/{job_id}/` (persistent Docker volume), unlike render caches which are in `/tmp/docuforge/renders`.

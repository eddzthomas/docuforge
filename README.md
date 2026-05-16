# DocuForge

> **Intelligent Document Processing Pipeline**  
> Upload scanned PDFs & images → PDF/A with searchable OCR text — all local, no cloud.

---

## Quickstart

```bash
# 1. Clone and start
git clone https://github.com/eddzthomas/docuforge.git
cd docuforge
docker compose up --build

# 2. Pull the AI models (in a second terminal)
docker exec -it docuforge-ollama ollama pull glm-ocr
docker exec -it docuforge-ollama ollama pull llama3.2

# 3. Open your browser
# http://localhost:8080
```

> **GPU users:** `docker compose -f docker-compose.yml -f docker-compose.gpu.yml up --build`

---

## What it does

| Step | Description |
|------|-------------|
| 1. Upload | Drag-and-drop scanned PDFs or images (PNG/JPG/TIFF/BMP) into the web UI |
| 2. OCR | Each page is sent to **GLM-OCR** (running locally via Ollama) — text + bounding boxes |
| 3. PDF/A | The original document is converted to **PDF/A-2b** archival format |
| 4. Text layer | OCR'd text is layered onto the PDF/A — selectable, searchable, copyable |
| 5. Tag & rename | An LLM reads the text and suggests a filename + up to 5 tags |
| 6. Output | Final PDF saved to `./data/output/` with tags embedded in XMP metadata |

**Everything runs locally. Zero data leaves your machine.**

---

## Architecture

```
 Browser (localhost:8080)           Docker Host
 ───────────────────────            ────────────
                                    ┌──────────────────┐
  Web UI ──── FastAPI ──────────────│  docuforge-app   │
  Upload      app.main              │  Port 8080       │
  History     POST /api/upload      └────────┬─────────┘
  Settings    GET  /api/jobs                 │
              GET  /api/tags                 │
              POST /api/config        ┌──────▼─────────┐
              ...                     │  ollama        │
                                      │  GLM-OCR       │
                                      │  llama3.2      │
                                      │  Port 11434    │
                                      └────────────────┘
 Volumes:
   ./data/ → /data/     (uploads, output, settings)
```

### Tech Stack

| Layer | Technology |
|-------|-----------|
| Web server | FastAPI + Uvicorn (Python 3.11) |
| PDF rendering | pdf2image + Poppler |
| PDF/A conversion | pikepdf |
| Text layering | pypdf |
| Image→PDF | img2pdf |
| OCR engine | GLM-OCR via Ollama |
| Tagging/rename LLM | llama3.2 / mistral / phi4 via Ollama |
| Frontend | Vanilla HTML/CSS/JS (zero build step) |
| Container | Docker + docker-compose |

---

## Configuration

All settings are loaded from environment variables (`.env` file) and can be overridden from the **Settings tab** in the web UI. UI-saved settings persist in `./data/settings.json`.

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `OLLAMA_HOST` | `http://ollama:11434` | Ollama API endpoint |
| `OCR_MODEL` | `glm-ocr` | Vision model for OCR |
| `TAGGING_MODEL` | `llama3.2` | Text model for renaming/tagging |
| `OLLAMA_AUTO_PULL` | `true` | Auto-pull models on first boot |
| `OLLAMA_AUTO_PULL_MODELS` | `glm-ocr,llama3.2` | Comma-separated model names |
| `DPI` | `300` | Render resolution for OCR (72–600) |
| `PDFA_LEVEL` | `2b` | PDF/A conformance level |
| `MAX_FILE_SIZE_MB` | `100` | Max upload size in MB |
| `WATCH_INTERVAL` | `5` | Folder watcher poll interval (seconds) |
| `AUTO_RENAME` | `false` | Auto-apply LLM name/tags without approval |
| `RENAME_PROMPT` | *(see .env.example)* | Prompt template for rename/tag LLM |
| `UPLOAD_FOLDER` | `/data/uploads` | Input file directory |
| `OUTPUT_FOLDER` | `/data/output` | Output file directory |
| `CONFIG_PATH` | `/data/settings.json` | UI settings persistence file |

---

## GPU Setup

For GPU-accelerated OCR with NVIDIA GPUs:

```bash
# 1. Install NVIDIA Container Toolkit
# https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html

# 2. Start with GPU override
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up --build
```

Expected performance on a consumer GPU: **~5-10 seconds per page at 300 DPI** (vs ~30-60s on CPU).

---

## Folder Watcher

Instead of uploading via the web UI, you can **drop files directly into `./data/uploads/`** from:

- Windows File Explorer
- A network share (SMB/NFS mount)
- `scp` / `rsync`
- Any filesystem write

Toggle the watcher on from the Upload tab in the web UI. Files are auto-detected and processed.

---

## Supported File Types

| Format | Extension |
|--------|-----------|
| PDF | `.pdf` |
| PNG | `.png` |
| JPEG | `.jpg`, `.jpeg` |
| TIFF | `.tiff`, `.tif` |
| BMP | `.bmp` |

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Web UI |
| `GET` | `/api/health` | Health check + Ollama status |
| `POST` | `/api/upload` | Upload file(s) |
| `GET` | `/api/jobs` | List all jobs |
| `GET` | `/api/jobs/{id}` | Job detail |
| `GET` | `/api/jobs/{id}/preview` | OCR text + proposed name/tags |
| `POST` | `/api/jobs/{id}/rename` | Regenerate name/tags |
| `PUT` | `/api/jobs/{id}/metadata` | Approve/override name & tags |
| `POST` | `/api/jobs/{id}/retry` | Retry a failed job |
| `GET` | `/api/download/{id}` | Download processed file |
| `GET` | `/api/tags` | All unique tags with counts |
| `GET` | `/api/config` | Current settings |
| `POST` | `/api/config` | Update settings |
| `GET` | `/api/config/test-ollama` | Test Ollama connectivity |
| `GET` | `/api/watcher/status` | Folder watcher status |
| `POST` | `/api/watcher/start` | Start folder watcher |
| `POST` | `/api/watcher/stop` | Stop folder watcher |

---

## Project Structure

```
docuforge/
├── app/
│   ├── main.py            FastAPI application & routes
│   ├── config.py          Settings loader + JSON persistence
│   ├── processor.py       Pipeline orchestrator + JobQueue + watcher
│   ├── ocr.py             GLM-OCR integration (Ollama)
│   ├── tagger.py          LLM rename & tag (Ollama)
│   ├── pdf_utils.py       PDF/A conversion + text layering + tag embedding
│   ├── templates/
│   │   └── index.html     Single-page web UI
│   └── static/
│       └── style.css      Dark theme stylesheet
├── scripts/
│   ├── docker-entrypoint.sh
│   └── pull-models.sh
├── data/                   Host-mounted volume
│   ├── uploads/            Input files
│   └── output/             Processed PDFs
├── docker-compose.yml
├── docker-compose.gpu.yml
├── Dockerfile
├── requirements.txt
└── .env.example
```

---

## FAQ

### "Ollama shows disconnected"

Wait 30-60 seconds after `docker compose up`. Ollama needs to load models into RAM. The health check ensures the app waits for Ollama to be ready. If the issue persists, check:

```bash
docker exec -it docuforge-ollama ollama list
```

### "GLM-OCR model not found"

Pull it manually:
```bash
docker exec -it docuforge-ollama ollama pull glm-ocr
```

### "How do I use a different OCR model?"

Change `OCR_MODEL` in `.env` or the Settings tab in the UI. Any Ollama-compatible vision model works. Make sure to pull the model first.

### "How do I change the rename/tag prompt?"

Edit the `RENAME_PROMPT` in `.env` or via the Settings tab in the UI. The prompt **must** contain the `{ocr_text}` placeholder.

### "How do I add more tagging models?"

Pull the model into Ollama:
```bash
docker exec -it docuforge-ollama ollama pull llama3.1:8b
```
Then set `TAGGING_MODEL=llama3.1:8b` in `.env` or the Settings tab.

### "Can I run without a GPU?"

Yes. Docker Compose defaults to CPU. Expect slower OCR (~30-60 seconds per page at 300 DPI).

### "Where are my files saved?"

- Uploads: `./data/uploads/` (host)
- Output: `./data/output/` (host)
- Settings: `./data/settings.json` (host)

### "Settings don't persist across restarts"

Make sure `./data` is mounted as a volume in `docker-compose.yml`. Settings are saved to `/data/settings.json` inside the container, which maps to `./data/settings.json` on the host.

---

---
